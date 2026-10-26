"""Framework knowledge base for QA 模式 (CJ, 2026-10-22: 「QA mode是評審遇到技術框架問題的時候可以幫我回答的」).

A separate Qdrant collection, `framework`, so judges' questions about the agent framework never mix with the plant's
SOP answers (the SOP collection `sop` is untouched). Same embedding model, same embedded client and the same hybrid
score as warroom/kb/store.py: cosine from fastembed + character-bigram lexical score + a title term.

Sources are in-repo only (SOURCES): the curated docs/qa/framework-qa.md (one chunk per `## Q:` pair, carrying the
pair's own 證據 file:line list), the project docs split by `##` heading, the module docstrings of the agent / safety /
natkit modules, and the OpenShell policy. Every chunk keeps its file and start line, which is the evidence id the QA
answer must cite.

No-answer gate (gate()): the best hit must score at least GATE_MIN_SCORE and share at least one key term of the
question (an ASCII word of 3+ letters or a CJK bigram outside STOP_BIGRAMS; a lone CJK character never counts) with the hit text. A question that fails
is answered with the fixed fallback, never by the model.
"""
from __future__ import annotations

import ast
import hashlib
import logging
import re
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from warroom.kb.store import (
    DEFAULT_EMBED_MODEL,
    DEFAULT_QDRANT_PATH,
    MAX_CHUNK_CHARS,
    ROOT,
    _get_client,
    _get_embedder,
    _split,
    lexical_score,
)

log = logging.getLogger(__name__)

COLLECTION = "framework"
QA_FILE = "docs/qa/framework-qa.md"
DOC_FILES = ("README.md", "README.zh-TW.md", "CLAUDE.md", "PRODUCT.md", "config/plant/PLANT.md")
DOC_GLOBS = ("docs/**/*.md",)
DOCSTRING_FILES = ("warroom/agent/state_machine.py", "warroom/agent/commander.py", "warroom/agent/main_agent.py",
                   "warroom/agent/gate.py", "warroom/safety/core.py", "warroom/natkit/register.py",
                   "warroom/agent/safety_filter.py", "warroom/agent/plant.py", "warroom/agent/audit.py")
YAML_FILES = ("deploy/openshell/warroom-policy.yaml",)

# hybrid score: cosine carries meaning, lexical the exact terms (「SAFE_STOP」「R2」), the title the question match;
# a curated QA pair gets a small boost over a raw doc section that says the same thing
W_COS, W_LEX, W_TITLE, QA_BOOST = 0.5, 0.3, 0.2, 0.05
GATE_MIN_SCORE = 0.32
STOP_BIGRAMS = frozenset({"什麼", "怎麼", "為什", "麼不", "你們", "我們", "是什", "有沒", "沒有", "可以", "會不", "不會",
                          "如何", "一下", "這個", "那個", "哪裡", "哪些", "多少", "嗎？", "請問", "介紹", "說明", "是不",
                          "不是", "的是", "一個", "還能", "能不", "怎樣", "麼樣", "要怎", "在哪", "了嗎", "樣的", "這樣",
                          "系統", "們的", "們用", "們有", "們打", "們團", "以接", "得出", "出來", "來嗎", "的話", "時候", "麼時"})
_ASCII_RX = re.compile(r"[A-Za-z][A-Za-z0-9_\-.]{2,}")
_CJK_RX = re.compile(r"[一-鿿]+")
_Q_RX = re.compile(r"^##\s*Q:\s*(.+?)\s*$")
_EVID_RX = re.compile(r"`([^`]+?):(\d+)`")


@dataclass(frozen=True)
class FrameworkChunk:
    source: str                 # repo-relative file
    line: int                   # 1-based start line of the section / pair
    title: str                  # QA question, doc heading or "module docstring"
    body: str
    n: int                      # position in the corpus (point id)
    kind: str = "doc"           # qa | doc | docstring | policy
    evidence: tuple[str, ...] = field(default=())   # a QA pair's own 證據 (file:line)

    @property
    def ref(self) -> str:
        return f"{self.source}:{self.line}"

    @property
    def embed_text(self) -> str:
        return f"{self.title}\n{self.body}"


def _qa_pairs(path: Path, rel: str) -> list[tuple[int, str, str, tuple[str, ...]]]:
    out: list[tuple[int, str, str, tuple[str, ...]]] = []
    cur: list[str] = []
    start, q = 0, ""
    lines = path.read_text(encoding="utf-8").splitlines()

    def flush() -> None:
        if q:
            body = "\n".join(cur).strip()
            evid = tuple(f"{f}:{n}" for f, n in _EVID_RX.findall(next((ln for ln in cur if ln.startswith("證據")), "")))
            out.append((start, q, body, evid))

    for i, ln in enumerate(lines, start=1):
        m = _Q_RX.match(ln)
        if m:
            flush()
            start, q, cur = i, m.group(1), []
        elif q:
            cur.append(ln)
    flush()
    return out


def _md_sections(path: Path) -> list[tuple[int, str, str]]:
    """(start line, heading, body) per `#`/`##`/`###` section; fenced code is kept as text."""
    out: list[tuple[int, str, str]] = []
    head, start, cur = path.name, 1, []
    for i, ln in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        m = re.match(r"^#{1,3}\s+(.+?)\s*$", ln)
        if m:
            if "\n".join(cur).strip():
                out.append((start, head, "\n".join(cur).strip()))
            head, start, cur = m.group(1), i, []
        else:
            cur.append(ln)
    if "\n".join(cur).strip():
        out.append((start, head, "\n".join(cur).strip()))
    return out


def _docstring(path: Path) -> tuple[int, str] | None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    doc = ast.get_docstring(tree, clean=True)
    if not doc or not tree.body:
        return None
    return tree.body[0].lineno, doc


def _yaml_comments(path: Path) -> list[tuple[int, str, str]]:
    """The policy file as sections of its comment + config lines, split at blank lines."""
    out: list[tuple[int, str, str]] = []
    start, cur = 1, []
    for i, ln in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not ln.strip():
            if cur:
                out.append((start, path.name, "\n".join(cur)))
            start, cur = i + 1, []
        else:
            cur.append(ln)
    if cur:
        out.append((start, path.name, "\n".join(cur)))
    return out


def load_framework_corpus(root: Path = ROOT) -> list[FrameworkChunk]:
    """Every source chunk with its file and start line. Missing files are skipped (logged)."""
    chunks: list[FrameworkChunk] = []

    def add(rel: str, line: int, title: str, body: str, kind: str, evidence: tuple[str, ...] = ()) -> None:
        pieces = [body] if kind == "qa" else _split(body)
        for p in pieces:
            if p.strip():
                chunks.append(FrameworkChunk(rel, line, title, p.strip(), len(chunks), kind, evidence))

    qa = root / QA_FILE
    if qa.exists():
        for line, q, body, evid in _qa_pairs(qa, QA_FILE):
            add(QA_FILE, line, q, body, "qa", evid)
    docs = [root / f for f in DOC_FILES]
    for g in DOC_GLOBS:
        docs += sorted(p for p in root.glob(g) if p.resolve() != qa.resolve())
    for p in docs:
        if not p.exists():
            log.info("framework KB: %s missing", p)
            continue
        rel = p.relative_to(root).as_posix()
        for line, head, body in _md_sections(p):
            add(rel, line, head, body, "doc")
    for f in DOCSTRING_FILES:
        p = root / f
        if p.exists() and (d := _docstring(p)):
            for i, para in enumerate(_split(d[1])):
                add(f, d[0], f"{Path(f).stem} 模組說明" + (f"（{i + 1}）" if i else ""), para, "docstring")
    for f in YAML_FILES:
        p = root / f
        if p.exists():
            for line, head, body in _yaml_comments(p):
                add(f, line, head, body, "policy")
    return chunks


def key_terms(question: str) -> set[str]:
    """The question's content terms: ASCII words of 3+ letters and CJK bigrams outside STOP_BIGRAMS."""
    terms = {w.lower().strip(".-") for w in _ASCII_RX.findall(question)}
    for run in _CJK_RX.findall(question):
        terms |= {run[i:i + 2] for i in range(len(run) - 1)} - STOP_BIGRAMS
    return {t for t in terms if t}


@dataclass(frozen=True)
class FrameworkHit:
    ref: str                    # file:line, the evidence id
    source: str
    line: int
    title: str
    text: str
    score: float
    kind: str
    evidence: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {"ref": self.ref, "source": self.source, "line": self.line, "title": self.title, "text": self.text,
                "score": self.score, "kind": self.kind, "evidence": list(self.evidence)}


class FrameworkKB:
    """The `framework` collection. Embedded mode on the Mac (shares the SOP KB's client for the same path), server mode
    (url) on the Spark; degrades to lexical-only when the embedding model or Qdrant is not there."""

    def __init__(self, path: Path | str | None = None, url: str | None = None, embed_model: str | None = None,
                 root: Path = ROOT, collection: str = COLLECTION):
        self.path = path if path is not None else DEFAULT_QDRANT_PATH
        self.url = url
        self.embed_model = embed_model or DEFAULT_EMBED_MODEL
        self.root = Path(root)
        self.collection = collection
        self.mode = "unloaded"
        self._chunks: list[FrameworkChunk] = []
        self._ready = False
        self._lock = threading.RLock()

    @classmethod
    def from_settings(cls, settings=None) -> FrameworkKB:
        from warroom.config import get_settings
        s = settings or get_settings()
        return cls(path=s.qdrant_path, url=s.qdrant_url, embed_model=s.embed_model)

    @property
    def chunks(self) -> list[FrameworkChunk]:
        self._ensure_ready()
        return self._chunks

    def _embed(self, texts: list[str]) -> list[list[float]]:
        return [v.tolist() for v in _get_embedder(self.embed_model).embed(texts)]

    def _hash(self) -> str:
        h = hashlib.sha256(self.embed_model.encode())
        for c in self._chunks:
            h.update(f"{c.ref}\n{c.embed_text}".encode())
        return h.hexdigest()[:16]

    def _ensure_ready(self) -> None:
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            self._chunks = load_framework_corpus(self.root)
            try:
                from qdrant_client import models
                client = _get_client(self.path, self.url)
                tag = self._hash()
                reuse = False
                if client.collection_exists(self.collection):
                    pts, _ = client.scroll(self.collection, limit=1, with_payload=True)
                    reuse = (client.count(self.collection).count == len(self._chunks) and bool(pts)
                             and pts[0].payload.get("corpus") == tag)
                if not reuse:
                    vectors = self._embed([c.embed_text for c in self._chunks])
                    if client.collection_exists(self.collection):
                        client.delete_collection(self.collection)
                    client.create_collection(self.collection, vectors_config=models.VectorParams(
                        size=len(vectors[0]), distance=models.Distance.COSINE))
                    client.upsert(self.collection, wait=True, points=[models.PointStruct(
                        id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"framework#{c.n}")), vector=v,
                        payload={"n": c.n, "source": c.source, "line": c.line, "title": c.title, "corpus": tag})
                        for c, v in zip(self._chunks, vectors)])
                else:
                    _get_embedder(self.embed_model)
                self.mode = "vector"
            except Exception as e:  # noqa: BLE001 - no model cache offline, qdrant down: degrade
                log.warning("framework KB vector index unavailable (%s); lexical only", e)
                self.mode = "lexical"
            self._ready = True

    def search(self, query: str, k: int = 4) -> list[FrameworkHit]:
        """Top-k chunks (at most one per file section) by the hybrid score."""
        self._ensure_ready()
        if not query.strip() or not self._chunks:
            return []
        cos: dict[int, float] = {}
        if self.mode == "vector":
            try:
                res = _get_client(self.path, self.url).query_points(
                    self.collection, query=self._embed([query])[0], limit=len(self._chunks), with_payload=True)
                cos = {int(p.payload["n"]): float(p.score) for p in res.points}
            except Exception as e:  # noqa: BLE001 - degrade, never fail a search
                log.warning("framework vector search failed (%s); lexical only", e)
        best: dict[str, tuple[float, FrameworkChunk]] = {}
        for c in self._chunks:
            s = (W_COS * cos.get(c.n, 0.0) + W_LEX * lexical_score(query, c.embed_text)
                 + W_TITLE * lexical_score(query, c.title) + (QA_BOOST if c.kind == "qa" else 0.0))
            if c.ref not in best or s > best[c.ref][0]:
                best[c.ref] = (s, c)
        ranked = sorted(best.values(), key=lambda t: -t[0])[:max(1, k)]
        return [FrameworkHit(c.ref, c.source, c.line, c.title, c.body[:MAX_CHUNK_CHARS * 2], round(s, 4), c.kind,
                             c.evidence) for s, c in ranked]

    def get(self, source: str) -> str | None:
        """Full text of a source file, only when it is one of the indexed files (the evidence viewer)."""
        self._ensure_ready()
        if source not in {c.source for c in self._chunks}:
            return None
        return (self.root / source).read_text(encoding="utf-8")


def gate(question: str, hits: list[FrameworkHit], min_score: float = GATE_MIN_SCORE) -> tuple[list[FrameworkHit], str]:
    """The no-answer gate: (hits that pass, reason). Empty list = answer with the fallback.
    A hit passes when it scores >= min_score and shares a key term of the question."""
    terms = key_terms(question)
    if not hits:
        return [], "no hits"
    if not terms:
        return [], "no key terms in the question"
    ok = [h for h in hits if h.score >= min_score and any(t in f"{h.title}\n{h.text}".lower() for t in terms)]
    if not ok:
        return [], f"best score {hits[0].score} < {min_score} or no shared key term"
    return ok, "ok"


_lock = threading.Lock()
_default: FrameworkKB | None = None


def default_framework_kb() -> FrameworkKB:
    global _default
    with _lock:
        if _default is None:
            _default = FrameworkKB.from_settings()
        return _default
