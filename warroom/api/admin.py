"""後台 (issue #37), first version: RAG maintenance, event memory + memory notes, status and runs.

Access: every route needs the factory network AND the supervisor seat. The network check runs first
(a client or any X-Forwarded-For hop outside loopback / private ranges gets 403 before a PIN is even
read), then the same `supervisor` rule as approvals (loopback / paired kiosk, or the PIN header).
Every write (SOP upload, SOP delete, re-index, memory note) is audited as kind=system, actor
"admin:<session>". No route returns an API key: status names the model and the LLM host only.
Work that touches disk, Qdrant or the embedder runs in a thread, never on the event loop."""
from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import re
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from warroom import factory as F
from warroom.contracts import AuditKind

ALLOWED_EXT = (".md", ".txt", ".pdf")
MAX_UPLOAD_BYTES = 2 * 1024 * 1024
_NAME_RE = re.compile(r"^[0-9A-Za-z一-鿿][0-9A-Za-z一-鿿._-]{0,80}$")
_EV_MACHINE_RE = re.compile(r"(" + "|".join(re.escape(m) for m in sorted(F.MACHINE_IDS, key=len, reverse=True)) + r")$")
TERMINAL = ("closed", "safe_stop")


class UploadBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=90)
    content_b64: str = Field(..., min_length=1)
    overwrite: bool = False


class NoteBody(BaseModel):
    machine_id: str
    text: str = Field(..., min_length=1, max_length=500)


def _private(host: str) -> bool:
    if host in ("testclient", "localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def safe_doc_name(name: str) -> str:
    """A plain file name inside the SOP folder: no path parts, an allowed extension."""
    base = Path(name).name
    if base != name or not _NAME_RE.match(base) or ".." in base:
        raise HTTPException(status_code=400, detail="檔名只能有中英文、數字、- _ .，不能有路徑")
    if Path(base).suffix.lower() not in ALLOWED_EXT:
        raise HTTPException(status_code=400, detail=f"只接受 {'、'.join(ALLOWED_EXT)}")
    return base


def machine_of_event(event_id: str | None) -> str | None:
    m = _EV_MACHINE_RE.search(event_id or "")
    return m.group(1) if m else None


class ReindexJob:
    """One background re-index at a time. Phases are coarse: kb.ingest() is one call."""

    def __init__(self):
        self._lock = threading.Lock()
        self.state = "idle"          # idle | running | done | failed
        self.phase = ""
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.chunks: int | None = None
        self.team_chunks: int | None = None
        self.mode: str | None = None
        self.error: str | None = None
        self.files = 0

    def as_dict(self) -> dict[str, Any]:
        now = time.time()
        end = self.finished_at or now
        return {"state": self.state, "phase": self.phase, "files": self.files, "chunks": self.chunks,
                "team_chunks": self.team_chunks, "mode": self.mode, "error": self.error,
                "started_at": self.started_at, "finished_at": self.finished_at,
                "elapsed_s": round(end - self.started_at, 1) if self.started_at else None}

    def start(self, run: Callable[[ReindexJob], None]) -> bool:
        with self._lock:
            if self.state == "running":
                return False
            self.state, self.phase, self.error = "running", "讀取 SOP 文件", None
            self.started_at, self.finished_at, self.chunks, self.team_chunks = time.time(), None, None, None
        threading.Thread(target=run, args=(self,), name="admin-reindex", daemon=True).start()
        return True


def build_admin_router(rt: Callable[[], Any], supervisor: Callable[..., Any]) -> APIRouter:
    """`rt` returns the Runtime, `supervisor(request, response)` is app.py's write-access rule."""
    job = ReindexJob()
    notes_holder: dict[str, Any] = {}

    def admin(request: Request, response: Response):
        host = (request.client.host if request.client else "").strip()
        host = host[7:] if host.lower().startswith("::ffff:") else host
        fwd = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
        if not _private(host) or not all(_private(h[7:] if h.lower().startswith("::ffff:") else h) for h in fwd):
            raise HTTPException(status_code=403, detail="後台只限廠內網路")
        return supervisor(request, response)

    def notes():
        if "n" not in notes_holder:
            from warroom.data.memory_notes import MemoryNotes
            notes_holder["n"] = MemoryNotes(Path(rt().settings.audit_path).parent / "memory_notes.json")
        return notes_holder["n"]

    def sop_dir() -> Path:
        r = rt()
        kb = r.c.kb
        return Path(getattr(kb, "sop_dir", None) or r.settings.sop_dir)

    def audit(session, payload: dict[str, Any]) -> None:
        rt().audit.append(AuditKind.SYSTEM, f"admin:{session.actor}", {"admin": payload.pop("action"), **payload})

    router = APIRouter(prefix="/api/admin")

    # ------------------------------------------------------------------ status, runs
    @router.get("/status")
    async def status(_: Any = Depends(admin)) -> dict[str, Any]:
        r = rt()
        kb = r.c.kb
        llm_host = urlparse(r.settings.base_url).netloc if r.llm_mode.value != "mock" else None
        chain = await asyncio.to_thread(_safe, r.audit.verify_chain, False)
        tail = r.audit.tail(0, 1)
        return {"llm_mode": r.llm_mode.value, "model": r.settings.model, "llm_host": llm_host,
                "llm_reachable": await r.llm_reachable(), "kb_ok": r.kb_ok(),
                "kb_mode": getattr(kb, "mode", "keyword"), "team_mode": getattr(kb, "team_mode", "off"),
                "audit_chain_ok": chain, "audit_entries": len(r.audit.tail(0, 100_000)),
                "audit_last_seq": tail[-1].seq if tail else 0,
                "degraded": dict(r.degraded), "scenario": r.scenario.value, "factory_time": r.now().isoformat(),
                "uptime_s": round(r.uptime_s()), "network": r.network, "memory_notes": len(notes().all()),
                "reindex": job.as_dict()}

    @router.get("/runs")
    async def runs(limit: int = Query(200, ge=1, le=2000), _: Any = Depends(admin)) -> dict[str, Any]:
        r = rt()
        return await asyncio.to_thread(_runs, r, limit)

    # ------------------------------------------------------------------ RAG
    @router.get("/rag")
    async def rag(_: Any = Depends(admin)) -> dict[str, Any]:
        return await asyncio.to_thread(_rag_overview, rt().c.kb, sop_dir(), job)

    @router.get("/rag/search")
    async def rag_search(q: str = Query(..., min_length=1, max_length=200), k: int = Query(5, ge=1, le=20),
                         _: Any = Depends(admin)) -> dict[str, Any]:
        kb = rt().c.kb
        if kb is None:
            raise HTTPException(status_code=503, detail="SOP 知識庫不可用")

        def run() -> dict[str, Any]:
            t = time.perf_counter()
            hits = kb.search(q, k)
            team = kb.search_team(q, k) if hasattr(kb, "search_team") else []
            return {"q": q, "hits": [h.model_dump(mode="json") for h in hits],
                    "team_hits": [h.model_dump(mode="json") for h in team],
                    "mode": getattr(kb, "mode", "keyword"), "ms": round((time.perf_counter() - t) * 1000, 1)}
        return await asyncio.to_thread(run)

    @router.post("/rag/docs")
    async def upload(body: UploadBody, session: Any = Depends(admin)) -> dict[str, Any]:
        name = safe_doc_name(body.name)
        try:
            data = base64.b64decode(body.content_b64, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(status_code=400, detail="檔案內容不是有效的 base64") from None
        if not data:
            raise HTTPException(status_code=400, detail="檔案是空的")
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail=f"檔案超過 {MAX_UPLOAD_BYTES // 1024 // 1024} MB")
        if name.lower().endswith((".md", ".txt")):
            try:
                data.decode("utf-8")
            except UnicodeDecodeError:
                raise HTTPException(status_code=400, detail="文字檔必須是 UTF-8") from None
        elif not data.startswith(b"%PDF"):
            raise HTTPException(status_code=400, detail="不是 PDF 檔")
        folder = sop_dir()
        path = folder / name

        def write() -> bool:
            existed = path.exists()
            if existed and not body.overwrite:
                return False
            folder.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".upload")
            tmp.write_bytes(data)
            tmp.replace(path)
            return True
        if not await asyncio.to_thread(write):
            raise HTTPException(status_code=409, detail=f"{name} 已存在；要取代請勾選覆蓋")
        audit(session, {"action": "sop_upload", "file": name, "bytes": len(data), "overwrite": body.overwrite})
        return {"ok": True, "file": name, "bytes": len(data), "note": "要讓 Agent 查得到，請按重新索引"}

    @router.delete("/rag/docs/{name}")
    async def delete_doc(name: str, confirm: str = Query("", max_length=90),
                         session: Any = Depends(admin)) -> dict[str, Any]:
        name = safe_doc_name(name)
        if confirm != name:
            raise HTTPException(status_code=400, detail="刪除要確認：confirm 參數必須等於檔名")
        path = sop_dir() / name
        if not await asyncio.to_thread(path.is_file):
            raise HTTPException(status_code=404, detail=f"找不到 {name}")
        await asyncio.to_thread(path.unlink)
        audit(session, {"action": "sop_delete", "file": name})
        return {"ok": True, "file": name, "note": "已從資料夾刪除；重新索引後 Agent 就查不到"}

    @router.post("/rag/reindex")
    async def reindex(session: Any = Depends(admin)) -> dict[str, Any]:
        r = rt()
        kb = r.c.kb
        if kb is None:
            raise HTTPException(status_code=503, detail="SOP 知識庫不可用")
        folder = sop_dir()

        def run(j: ReindexJob) -> None:
            try:
                j.files = sum(1 for p in folder.glob("*") if p.suffix.lower() in ALLOWED_EXT)
                j.phase = "切段、嵌入並寫入 Qdrant"
                n = kb.ingest()
                j.chunks, j.mode = n, getattr(kb, "mode", "keyword")
                j.team_chunks = len(getattr(kb, "_team_chunks", []) or [])
                if hasattr(r, "_kb_ok"):
                    r._kb_ok = n > 0
                j.state, j.phase = "done", "完成"
                r.audit.append(AuditKind.SYSTEM, f"admin:{session.actor}",
                               {"admin": "sop_reindex_done", "chunks": n, "team_chunks": j.team_chunks,
                                "mode": j.mode, "elapsed_s": round(time.time() - (j.started_at or time.time()), 1)})
            except Exception as e:  # noqa: BLE001 - report on the job, never crash the thread silently
                j.state, j.phase, j.error = "failed", "失敗", f"{type(e).__name__}: {e}"[:300]
                r.audit.append(AuditKind.SYSTEM, f"admin:{session.actor}", {"admin": "sop_reindex_failed", "error": j.error})
            finally:
                j.finished_at = time.time()
        if not job.start(run):
            raise HTTPException(status_code=409, detail="重新索引進行中")
        audit(session, {"action": "sop_reindex_start"})
        return job.as_dict()

    @router.get("/rag/reindex")
    async def reindex_status(_: Any = Depends(admin)) -> dict[str, Any]:
        return job.as_dict()

    # ------------------------------------------------------------------ memory
    @router.get("/memory")
    async def memory(_: Any = Depends(admin)) -> dict[str, Any]:
        r = rt()
        return await asyncio.to_thread(_memory, r, notes())

    @router.post("/memory/notes")
    async def add_note(body: NoteBody, session: Any = Depends(admin)) -> dict[str, Any]:
        if body.machine_id not in F.MACHINE_IDS:
            raise HTTPException(status_code=400, detail=f"沒有這台機台：{body.machine_id}")
        now: datetime = rt().now()
        try:
            note = await asyncio.to_thread(notes().add, body.machine_id, body.text, session.actor, now)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        audit(session, {"action": "memory_note_add", "note_id": note.id, "machine_id": note.machine_id, "text": note.text})
        return {"ok": True, "note": note.__dict__}

    return router


def _safe(fn, default):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return default


def _rag_overview(kb: Any, folder: Path, job: ReindexJob) -> dict[str, Any]:
    from warroom.kb.store import load_team_chunks, parse_sop
    docs = []
    for p in sorted(folder.glob("*")):
        if p.suffix.lower() not in ALLOWED_EXT or not p.is_file():
            continue
        try:
            chunks = parse_sop(p) if p.suffix.lower() in (".md", ".txt") else _pdf_chunks(p)
            err = None
        except Exception as e:  # noqa: BLE001 - one bad file must not hide the list
            chunks, err = [], f"{type(e).__name__}: {e}"[:200]
        st = p.stat()
        docs.append({"file": p.name, "sop_id": chunks[0].sop_id if chunks else p.stem.upper(),
                     "title": chunks[0].title if chunks else p.stem, "chunks": len(chunks), "bytes": st.st_size,
                     "modified": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                     "sections": sorted({c.section for c in chunks}), "error": err})
    team: dict[str, dict[str, Any]] = {}
    for c in _safe(lambda: load_team_chunks(getattr(kb, "team_dataset_dir", None)), []):
        t = team.setdefault(c.sop_id, {"sop_id": c.sop_id, "title": c.title, "chunks": 0, "source": c.source})
        t["chunks"] += 1
    qdrant: dict[str, int | None] = {}
    if getattr(kb, "mode", None) == "vector":
        try:
            from warroom.kb.store import _get_client
            client = _get_client(kb.path, kb.url)
            for col in (kb.collection, kb.team_collection):
                qdrant[col] = client.count(col).count if client.collection_exists(col) else 0
        except Exception:  # noqa: BLE001 - counts are informative only
            qdrant = {}
    indexed = len(getattr(kb, "_chunks", []) or [])
    return {"folder": folder.name, "docs": docs, "team": sorted(team.values(), key=lambda t: t["sop_id"]),
            "kb_mode": getattr(kb, "mode", "keyword"), "team_mode": getattr(kb, "team_mode", "off"),
            "indexed_chunks": indexed, "folder_chunks": sum(d["chunks"] for d in docs), "qdrant": qdrant,
            "reindex": job.as_dict()}


def _pdf_chunks(p: Path) -> list:
    """The chunks load_corpus() makes of one PDF (same split, same ids)."""
    from pypdf import PdfReader

    from warroom.kb.store import SopChunk, _split
    text = "\n".join(page.extract_text() or "" for page in PdfReader(p).pages)
    return [SopChunk(p.stem.upper(), p.stem, p.name, "全文", i, piece) for i, piece in enumerate(_split(text))]


def _runs(r: Any, limit: int) -> dict[str, Any]:
    entries = r.audit.tail(0, 100_000)
    asks: dict[str, dict[str, Any]] = {}
    chitchat: list[dict[str, Any]] = []
    tools: dict[str, int] = {}
    for e in entries:
        p = e.payload or {}
        if e.kind == AuditKind.TOOL_CALL and e.event_id:
            tools[e.event_id] = tools.get(e.event_id, 0) + 1
        if e.kind != AuditKind.SYSTEM:
            continue
        if e.event_id and e.event_id.startswith("ASK-"):
            a = asks.setdefault(e.event_id, {"id": e.event_id, "kind": "ask", "ts": e.ts.isoformat(), "by": e.actor,
                                             "question": None, "answer": None})
            if p.get("msg") == "ask":
                a["question"], a["by"], a["ts"] = p.get("question"), e.actor, e.ts.isoformat()
            elif p.get("msg") == "answered":
                a["answer"] = str(p.get("answer") or "")[:300]
        elif "command" in p and p.get("intent") == "chitchat":
            chitchat.append({"id": f"CHAT-{e.seq}", "kind": "chat", "ts": e.ts.isoformat(), "by": e.actor,
                             "question": p.get("command"), "answer": p.get("reply")})
    for a in asks.values():
        a["tools"] = tools.get(a["id"], 0)
    events = []
    for ev in sorted(r.events(), key=lambda x: x.opened_at, reverse=True):
        events.append({"id": ev.id, "kind": "command" if ev.source == "supervisor" else "event", "source": ev.source,
                       "machine_id": ev.machine_id, "state": ev.state.value, "ts": ev.opened_at.isoformat(),
                       "closed_at": ev.closed_at.isoformat() if ev.closed_at else None,
                       "title": ev.command or (ev.alert_card.title if ev.alert_card else ev.anomaly.message),
                       "outcome": ev.outcome, "replans": ev.replans, "tools": tools.get(ev.id, 0)})
    runs = sorted(events + list(asks.values()) + chitchat, key=lambda x: x["ts"], reverse=True)[:limit]
    return {"runs": runs, "counts": {"event": sum(1 for x in events if x["kind"] == "event"),
                                     "command": sum(1 for x in events if x["kind"] == "command") + len(chitchat),
                                     "ask": len(asks)}}


def _memory(r: Any, notes: Any) -> dict[str, Any]:
    from warroom.data.team_dataset import IncidentStore
    incidents = [{"id": i.incident_id, "date": i.date.isoformat(), "machine_id": i.machine_id, "equipment": i.equipment,
                  "symptom": i.symptom, "action": i.action, "downtime_min": i.downtime_min, "outcome": i.outcome,
                  "source": "team_dataset"} for i in _safe(lambda: IncidentStore().all(), [])]
    # closed events: the audit log is the record (it outlives a scenario restart); the live event adds the outcome
    live = {ev.id: ev for ev in r.events()}
    closed: dict[str, dict[str, Any]] = {}
    anomaly: dict[str, str] = {}
    for e in r.audit.tail(0, 100_000):
        p = e.payload or {}
        if not e.event_id or e.event_id.startswith("ASK-"):
            continue
        if e.kind == AuditKind.ANOMALY and p.get("message"):
            anomaly[e.event_id] = str(p["message"])
        if e.kind == AuditKind.STATE_TRANSITION and p.get("to") in TERMINAL:
            ev = live.get(e.event_id)
            closed[e.event_id] = {"id": e.event_id, "machine_id": ev.machine_id if ev else machine_of_event(e.event_id),
                                  "closed_at": e.ts.isoformat(), "state": p.get("to"),
                                  "what": (ev.command or (ev.alert_card.title if ev.alert_card else None)) if ev else None,
                                  "outcome": ev.outcome if ev else None, "source": "audit"}
    for cid, c in closed.items():
        c["what"] = c["what"] or anomaly.get(cid)
    return {"incidents": incidents, "closed_events": sorted(closed.values(), key=lambda c: c["closed_at"], reverse=True),
            "notes": [n.__dict__ for n in sorted(notes.all(), key=lambda n: n.created_at, reverse=True)],
            "agent_reads_notes": False}
