"""SOP knowledge base: embedded Qdrant (Mac) or Qdrant server (Spark) + fastembed multilingual
embeddings, fully offline once the model is cached. Port of legacy/sop_kb.py.

Retrieval is hybrid: cosine similarity from the embedding model, plus a character-bigram
lexical score over the SOP title / section / body, plus a boost when the query names an SOP
title outright (page 11: the commander asks for 「硫化中斷處置」 by name). Results are one hit per
SOP (its best chunk), each carrying the SOP id, title, file and section as the citation.

If the embedding model cannot be loaded (no cache and no network), the KB degrades to a
lexical-only in-memory index instead of failing: `mode` says which one is active.
"""
from __future__ import annotations

import atexit
import hashlib
import itertools
import logging
import os
import re
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from warroom.contracts import SopHit

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_SOP_DIR = Path(__file__).resolve().parent / "sop"
DEFAULT_QDRANT_PATH = ROOT / "data" / "runtime" / "qdrant"
DEFAULT_EMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
DEFAULT_CACHE_DIR = ROOT / "data" / "runtime" / "fastembed_cache"
MAX_CHUNK_CHARS = 450

_TITLE_RE = re.compile(r"^#\s*(SOP-[A-Z]+-\d+)\s+(.+?)\s*$")
_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$")
_TOKEN_RE = re.compile(r"[0-9A-Za-z一-鿿]")

# Weights of the hybrid score. Cosine carries meaning, lexical carries exact terms such as
# "蒸汽" or "夜班" that a small multilingual model blurs, the title term rewards topical match.
W_COS, W_LEX, W_TITLE, EXACT_TITLE_BOOST = 0.5, 0.3, 0.2, 0.5


@dataclass(frozen=True)
class SopChunk:
    sop_id: str
    title: str
    source: str
    section: str
    n: int
    body: str

    @property
    def embed_text(self) -> str:
        return f"{self.sop_id} {self.title}｜{self.section}\n{self.body}"

    @property
    def cited_text(self) -> str:
        return f"〔{self.sop_id} {self.title}｜{self.section}〕\n{self.body}"


def parse_sop(path: Path) -> list[SopChunk]:
    """Split one SOP markdown file into section chunks (≤ MAX_CHUNK_CHARS each)."""
    lines = path.read_text(encoding="utf-8").splitlines()
    sop_id, title = path.stem.upper(), path.stem
    sections: list[tuple[str, list[str]]] = [("概要", [])]
    titled = False
    for ln in lines:
        m = _TITLE_RE.match(ln)
        if m and not titled:
            sop_id, title, titled = m.group(1), re.sub(r"〔.*?〕", "", m.group(2)).strip(), True
            continue
        if ln.startswith("# ") and not titled:
            title, titled = ln[2:].strip(), True
            continue
        s = _SECTION_RE.match(ln)
        if s:
            sections.append((s.group(1), []))
            continue
        sections[-1][1].append(ln)

    chunks: list[SopChunk] = []
    for heading, body_lines in sections:
        body = "\n".join(body_lines).strip()
        if not body:
            continue
        for piece in _split(body):
            chunks.append(SopChunk(sop_id, title, path.name, heading, len(chunks), piece))
    return chunks


def _split(body: str) -> list[str]:
    if len(body) <= MAX_CHUNK_CHARS:
        return [body]
    out, cur = [], ""
    for ln in body.splitlines():
        if cur and len(cur) + len(ln) + 1 > MAX_CHUNK_CHARS:
            out.append(cur.strip())
            cur = ""
        cur += ln + "\n"
    if cur.strip():
        out.append(cur.strip())
    return out


def load_corpus(sop_dir: Path) -> list[SopChunk]:
    chunks: list[SopChunk] = []
    for p in sorted(Path(sop_dir).glob("*")):
        if p.suffix in (".md", ".txt"):
            chunks.extend(parse_sop(p))
        elif p.suffix == ".pdf":
            from pypdf import PdfReader
            text = "\n".join(page.extract_text() or "" for page in PdfReader(p).pages)
            for i, piece in enumerate(_split(text)):
                chunks.append(SopChunk(p.stem.upper(), p.stem, p.name, "全文", i, piece))
    return chunks


def _bigrams(text: str) -> set[str]:
    chars = _TOKEN_RE.findall(text.lower())
    grams = {a + b for a, b in itertools.pairwise(chars)}
    return grams or set(chars)


def lexical_score(query: str, text: str) -> float:
    """Share of the query's character bigrams that appear in text (0..1)."""
    q = _bigrams(query)
    if not q:
        return 0.0
    return len(q & _bigrams(text)) / len(q)


def _norm(s: str) -> str:
    return "".join(_TOKEN_RE.findall(s))


# One embedded Qdrant client per path per process: the local mode takes a file lock.
_CLIENTS: dict[str, object] = {}
_EMBEDDERS: dict[tuple[str, str], object] = {}
_CACHE_LOCK = threading.Lock()


@atexit.register
def _close_clients() -> None:
    """Close embedded clients before interpreter teardown (avoids qdrant's noisy __del__)."""
    for c in list(_CLIENTS.values()):
        try:
            c.close()
        except Exception:  # noqa: BLE001, S110 - best effort at exit
            pass
    _CLIENTS.clear()


def _get_client(path, url: str | None):
    from qdrant_client import QdrantClient
    key = url or str(path)
    with _CACHE_LOCK:
        if key not in _CLIENTS:
            if url:
                _CLIENTS[key] = QdrantClient(url=url, timeout=10)
            elif str(path) == ":memory:":
                _CLIENTS[key] = QdrantClient(location=":memory:")
            else:
                Path(path).mkdir(parents=True, exist_ok=True)
                _CLIENTS[key] = QdrantClient(path=str(path))
        return _CLIENTS[key]


def _get_embedder(model: str):
    from fastembed import TextEmbedding
    cache = os.environ.get("FASTEMBED_CACHE_PATH") or str(DEFAULT_CACHE_DIR)
    key = (model, cache)
    with _CACHE_LOCK:
        if key not in _EMBEDDERS:
            Path(cache).mkdir(parents=True, exist_ok=True)
            _EMBEDDERS[key] = TextEmbedding(model, cache_dir=cache)
        return _EMBEDDERS[key]


class QdrantSopKB:
    """Implements contracts.SopKB. Embedded mode (path, or ":memory:") on the Mac, server mode
    (url) on the Spark. Answers "硫化中斷處置" with the SOP of that title (page 11)."""

    def __init__(self, path: Path | str | None = None, url: str | None = None, sop_dir: Path | None = None,
                 embed_model: str | None = None, collection: str = "sop"):
        self.path = path if path is not None else DEFAULT_QDRANT_PATH
        self.url = url
        self.sop_dir = Path(sop_dir) if sop_dir else DEFAULT_SOP_DIR
        self.embed_model = embed_model or DEFAULT_EMBED_MODEL
        self.collection = collection
        self.mode = "unloaded"           # "vector" | "lexical" once ready
        self._chunks: list[SopChunk] = []
        self._ready = False
        self._lock = threading.RLock()

    @classmethod
    def from_settings(cls, settings=None) -> QdrantSopKB:
        from warroom.config import get_settings
        s = settings or get_settings()
        return cls(path=s.qdrant_path, url=s.qdrant_url, sop_dir=s.sop_dir, embed_model=s.embed_model)

    # ---------------------------------------------------------------- build
    def _embed(self, texts: list[str]) -> list[list[float]]:
        return [v.tolist() for v in _get_embedder(self.embed_model).embed(texts)]

    def _corpus_hash(self) -> str:
        h = hashlib.sha256(self.embed_model.encode())
        for c in self._chunks:
            h.update(c.embed_text.encode())
        return h.hexdigest()[:16]

    def ingest(self) -> int:
        """(Re)build the index from sop_dir. Returns the chunk count."""
        with self._lock:
            self._chunks = load_corpus(self.sop_dir)
            try:
                self._ingest_vectors()
                self.mode = "vector"
            except Exception as e:  # noqa: BLE001 - no model cache offline, qdrant down: degrade
                log.warning("SOP KB vector index unavailable (%s); using lexical-only search", e)
                self.mode = "lexical"
            self._ready = True
            return len(self._chunks)

    def _ingest_vectors(self) -> None:
        from qdrant_client import models
        client = _get_client(self.path, self.url)
        vectors = self._embed([c.embed_text for c in self._chunks])
        if client.collection_exists(self.collection):
            client.delete_collection(self.collection)
        client.create_collection(self.collection, vectors_config=models.VectorParams(
            size=len(vectors[0]), distance=models.Distance.COSINE))
        tag = self._corpus_hash()
        points = [models.PointStruct(
            id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{c.source}#{c.n}")), vector=v,
            payload={"sop_id": c.sop_id, "title": c.title, "source": c.source, "section": c.section,
                     "n": c.n, "body": c.body, "corpus": tag})
            for c, v in zip(self._chunks, vectors)]
        client.upsert(self.collection, points=points, wait=True)

    def _ensure_ready(self) -> None:
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            # Reuse an existing index when it matches the current corpus; rebuild otherwise.
            self._chunks = load_corpus(self.sop_dir)
            try:
                client = _get_client(self.path, self.url)
                if client.collection_exists(self.collection):
                    n = client.count(self.collection).count
                    pts, _ = client.scroll(self.collection, limit=1, with_payload=True)
                    if n == len(self._chunks) and pts and pts[0].payload.get("corpus") == self._corpus_hash():
                        _get_embedder(self.embed_model)
                        self.mode, self._ready = "vector", True
                        return
            except Exception as e:  # noqa: BLE001 - degrade, never fail a search
                log.info("existing SOP index not reusable: %s", e)
            self.ingest()

    # ---------------------------------------------------------------- search
    def search(self, query: str, k: int = 3) -> list[SopHit]:
        """Best chunk of each of the top-k SOPs, scored by the hybrid score."""
        self._ensure_ready()
        if not self._chunks or not query.strip():
            return []
        cos: dict[tuple[str, int], float] = {}
        if self.mode == "vector":
            try:
                client = _get_client(self.path, self.url)
                res = client.query_points(self.collection, query=self._embed([query])[0],
                                          limit=len(self._chunks), with_payload=True)
                for p in res.points:
                    cos[(p.payload["source"], p.payload["n"])] = float(p.score)
            except Exception as e:  # noqa: BLE001 - degrade, never fail a search
                log.warning("vector search failed (%s); lexical only for this query", e)

        qn = _norm(query)
        best: dict[str, tuple[float, SopChunk]] = {}
        for c in self._chunks:
            tn = _norm(c.title)
            exact = EXACT_TITLE_BOOST if tn and (tn in qn or (len(qn) >= 4 and qn in tn)) else 0.0
            score = (W_COS * cos.get((c.source, c.n), 0.0)
                     + W_LEX * lexical_score(query, c.embed_text)
                     + W_TITLE * lexical_score(query, c.title)
                     + exact)
            if c.sop_id not in best or score > best[c.sop_id][0]:
                best[c.sop_id] = (score, c)
        ranked = sorted(best.values(), key=lambda t: -t[0])[:max(1, k)]
        return [SopHit(sop_id=c.sop_id, title=c.title, source=c.source, score=round(s, 4), text=c.cited_text)
                for s, c in ranked]

    # ---------------------------------------------------------------- extras
    def titles(self) -> dict[str, str]:
        """sop_id -> title for every SOP in the corpus."""
        chunks = self._chunks or load_corpus(self.sop_dir)
        return {c.sop_id: c.title for c in chunks}

    def get(self, sop_id: str) -> str | None:
        """Full markdown of one SOP by id (for a citation link on the dashboard)."""
        for p in sorted(self.sop_dir.glob("*.md")):
            first = p.read_text(encoding="utf-8").split("\n", 1)[0]
            m = _TITLE_RE.match(first)
            if m and m.group(1) == sop_id:
                return p.read_text(encoding="utf-8")
        return None
