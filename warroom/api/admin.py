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

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from warroom import __version__
from warroom import factory as F
from warroom.api import admin_audit as AA
from warroom.api import admin_params as AP
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


class PlantDraftBody(BaseModel):
    text: str = Field(..., max_length=4000)


class PlantShaBody(BaseModel):
    sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")


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


def install_admin(app: FastAPI, rt: Callable[[], Any], supervisor: Callable[..., Any]) -> None:
    """Add the /api/admin routes to app. `rt` returns the Runtime, `supervisor(request, response)` is app.py's
    write-access rule."""
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

    P = "/api/admin"
    ADMIN = Depends(admin)

    # ------------------------------------------------------------------ status, runs
    @app.get(P + "/status")
    async def status(request: Request, _: Any = ADMIN) -> dict[str, Any]:
        r = rt()
        kb = r.c.kb
        llm_host = urlparse(r.settings.base_url).netloc if r.llm_mode.value != "mock" else None
        chain = await asyncio.to_thread(_safe, r.audit.verify_chain, False)
        recent = r.audit.tail(0, 500)
        last_seq = _audit_len(r.audit)
        chunks = list(getattr(kb, "_chunks", []) or [])
        team = list(getattr(kb, "_team_chunks", []) or [])
        today = r.now().date()
        main = getattr(request.app.state, "holder", {}).get("main")
        st = {"llm_mode": r.llm_mode.value, "model": r.settings.model, "llm_host": llm_host,
              "llm_reachable": await r.llm_reachable(), "kb_ok": r.kb_ok(),
              "kb_mode": getattr(kb, "mode", "keyword"), "team_mode": getattr(kb, "team_mode", "off"),
              "kb_docs": len({c.sop_id for c in chunks}), "kb_chunks": len(chunks),
              "team_docs": len({c.sop_id for c in team}), "team_chunks": len(team),
              "audit_chain_ok": chain, "audit_entries": last_seq,
              "audit_last_seq": last_seq, "llm_latency": AP.llm_latency(recent),
              "degraded": dict(r.degraded), "scenario": r.scenario.value, "factory_time": r.now().isoformat(),
              "speed": getattr(r, "speed", None), "paused": bool(getattr(r, "paused", False)),
              "day_loop": _safe(r.day_loop_info, None),
              "safety_rules": [x.id for x in getattr(r.c.safety, "rules", None) or []],
              "safety_actions_today": sum(1 for a in getattr(r, "safety_log", []) if a.ts.date() == today),
              "conversations": len(getattr(main, "_memories", {}) or {}) + _safe(lambda: len(r.ask_memory), 0),
              "uptime_s": round(r.uptime_s()), "network": r.network, "memory_notes": len(notes().all()),
              "version": __version__, "commit": AP.commit_sha(), "reindex": job.as_dict()}
        st["health"] = AP.health(st)
        return st

    # ------------------------------------------------------------------ 稽核紀錄 (read-only) and 系統參數
    def audit_filter(kind: str, actor: str, event: str, q: str, t_from: str, t_to: str) -> AA.Filter:
        if kind and kind not in {k.value for k in AuditKind}:
            raise HTTPException(status_code=400, detail=f"沒有這種紀錄：{kind}")
        return AA.Filter(kind, actor, event, q, t_from, t_to)

    @app.get(P + "/audit")
    async def audit_list(kind: str = Query("", max_length=40), actor: str = Query("", max_length=80),
                         event: str = Query("", max_length=120), q: str = Query("", max_length=200),
                         t_from: str = Query("", alias="from", max_length=40), t_to: str = Query("", alias="to", max_length=40),
                         cursor: int | None = Query(None, ge=1), limit: int = Query(AA.PAGE, ge=1, le=500),
                         _: Any = ADMIN) -> dict[str, Any]:
        flt = audit_filter(kind, actor, event, q, t_from, t_to)
        newest, _ = AA.source(rt().audit)
        out = await asyncio.to_thread(lambda: AA.page(newest(), flt, cursor, limit))
        out["kinds"] = AA.kinds()
        out["last_seq"] = _audit_len(rt().audit)
        return out

    @app.get(P + "/audit/verify")
    async def audit_verify(_: Any = ADMIN) -> dict[str, Any]:
        a = rt().audit
        if hasattr(a, "verify_report"):
            return await asyncio.to_thread(a.verify_report)
        ok = await asyncio.to_thread(_safe, a.verify_chain, False)
        return {"ok": ok, "checked": None, "last_seq": None, "broken_at": None, "problem": None if ok else "verify failed"}

    @app.get(P + "/audit/export")
    async def audit_export(kind: str = Query("", max_length=40), actor: str = Query("", max_length=80),
                           event: str = Query("", max_length=120), q: str = Query("", max_length=200),
                           t_from: str = Query("", alias="from", max_length=40), t_to: str = Query("", alias="to", max_length=40),
                           session: Any = ADMIN) -> Response:
        flt = audit_filter(kind, actor, event, q, t_from, t_to)
        _, oldest = AA.source(rt().audit)
        lines, cut = await asyncio.to_thread(lambda: AA.export_lines(oldest(), flt))
        audit(session, {"action": "audit_export", "rows": len(lines), "cut": cut, "filter": flt.as_dict()})
        stamp = rt().now().strftime("%Y%m%d-%H%M")
        return Response("\n".join(lines) + ("\n" if lines else ""), media_type="application/x-ndjson; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="audit-{stamp}.jsonl"',
                                 "X-Rows": str(len(lines)), "X-Cut": "1" if cut else "0"})

    @app.get(P + "/params")
    async def params(request: Request, _: Any = ADMIN) -> dict[str, Any]:
        return await asyncio.to_thread(AP.params, rt(), request.app.state)

    @app.get(P + "/runs")
    async def runs(limit: int = Query(200, ge=1, le=2000), _: Any = ADMIN) -> dict[str, Any]:
        r = rt()
        return await asyncio.to_thread(_runs, r, limit)

    # ------------------------------------------------------------------ RAG
    @app.get(P + "/rag")
    async def rag(_: Any = ADMIN) -> dict[str, Any]:
        return await asyncio.to_thread(_rag_overview, rt().c.kb, sop_dir(), job)

    @app.get(P + "/rag/search")
    async def rag_search(q: str = Query(..., min_length=1, max_length=200), k: int = Query(5, ge=1, le=20),
                         _: Any = ADMIN) -> dict[str, Any]:
        kb = rt().c.kb
        if kb is None:
            raise HTTPException(status_code=503, detail="SOP 知識庫不可用")

        def run() -> dict[str, Any]:
            t = time.perf_counter()
            hits = kb.search(q, k)
            team = kb.search_team(q, k) if hasattr(kb, "search_team") else []
            # what the agents get: both collections on one score, through the no-answer gate
            r = kb.retrieve(q, k) if hasattr(kb, "retrieve") else None
            return {"q": q, "hits": [h.model_dump(mode="json") for h in hits],
                    "team_hits": [h.model_dump(mode="json") for h in team],
                    "answer": None if r is None else {"hits": [h.model_dump(mode="json") for h in r.hits],
                                                      "reason": r.reason},
                    "mode": getattr(kb, "mode", "keyword"), "ms": round((time.perf_counter() - t) * 1000, 1)}
        return await asyncio.to_thread(run)

    @app.post(P + "/rag/docs")
    async def upload(body: UploadBody, session: Any = ADMIN) -> dict[str, Any]:
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

    @app.delete(P + "/rag/docs/{name}")
    async def delete_doc(name: str, confirm: str = Query("", max_length=90),
                         session: Any = ADMIN) -> dict[str, Any]:
        name = safe_doc_name(name)
        if confirm != name:
            raise HTTPException(status_code=400, detail="刪除要確認：confirm 參數必須等於檔名")
        path = sop_dir() / name
        if not await asyncio.to_thread(path.is_file):
            raise HTTPException(status_code=404, detail=f"找不到 {name}")
        await asyncio.to_thread(path.unlink)
        audit(session, {"action": "sop_delete", "file": name})
        return {"ok": True, "file": name, "note": "已從資料夾刪除；重新索引後 Agent 就查不到"}

    @app.post(P + "/rag/reindex")
    async def reindex(session: Any = ADMIN) -> dict[str, Any]:
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

    @app.get(P + "/rag/reindex")
    async def reindex_status(_: Any = ADMIN) -> dict[str, Any]:
        return job.as_dict()

    # ------------------------------------------------------------------ 工廠指示 PLANT.md (refs #34)
    def pp():
        from warroom.agent import plant
        return plant.current()

    @app.get(P + "/plant")
    async def plant_get(_: Any = ADMIN) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            p = pp()
            ov = p.overview()
            d = ov["draft"]
            ov["draft_diff"] = _line_diff(ov["active"]["text"], d["text"]) if d else []
            return ov
        return await asyncio.to_thread(run)

    @app.post(P + "/plant/draft")
    async def plant_draft(body: PlantDraftBody, session: Any = ADMIN) -> dict[str, Any]:
        d = await asyncio.to_thread(pp().save_draft, body.text, session.actor, rt().now())
        audit(session, {"action": "plant_draft_save", "sha256": d["sha256"], "chars": d["chars"]})
        return d

    @app.post(P + "/plant/test")
    async def plant_test(session: Any = ADMIN) -> dict[str, Any]:
        """試跑: the page-11 plan once with the draft (rule commander always; the model too when LLM mode is on
        and it answers), against the active version. Mock plans do not read prompts, so they only check format."""
        p = pp()
        draft = p.overview()["draft"]
        if not draft:
            raise HTTPException(status_code=409, detail="沒有草稿可以試跑；先存草稿")
        result = await _plant_test(rt(), draft["text"], p.active_text())
        result.update({"sha256": draft["sha256"], "at": rt().now().isoformat(), "by": session.actor})
        await asyncio.to_thread(p.record_test, draft["sha256"], result)
        audit(session, {"action": "plant_test", "sha256": draft["sha256"], "ok": result["ok"],
                        "llm_ran": result["llm"] is not None})
        return result

    @app.post(P + "/plant/activate")
    async def plant_activate(body: PlantShaBody, session: Any = ADMIN) -> dict[str, Any]:
        p = pp()
        test = p.overview()["tests"].get(body.sha256)
        if not test or not test.get("ok"):
            raise HTTPException(status_code=409, detail="這個版本還沒有通過試跑；先按「試跑」")
        try:
            r = await asyncio.to_thread(p.activate, body.sha256, session.actor, rt().now(), "activate")
        except KeyError:
            raise HTTPException(status_code=404, detail="找不到這個版本") from None
        audit(session, {"action": "plant_activate", "sha256": r["sha256"], "previous": r["previous"]})
        return r

    @app.post(P + "/plant/rollback")
    async def plant_rollback(body: PlantShaBody, session: Any = ADMIN) -> dict[str, Any]:
        try:
            r = await asyncio.to_thread(pp().activate, body.sha256, session.actor, rt().now(), "rollback")
        except KeyError:
            raise HTTPException(status_code=404, detail="找不到這個版本") from None
        audit(session, {"action": "plant_rollback", "sha256": r["sha256"], "previous": r["previous"]})
        return r

    # ------------------------------------------------------------------ memory
    @app.get(P + "/memory")
    async def memory(_: Any = ADMIN) -> dict[str, Any]:
        r = rt()
        return await asyncio.to_thread(_memory, r, notes())

    @app.post(P + "/memory/notes")
    async def add_note(body: NoteBody, session: Any = ADMIN) -> dict[str, Any]:
        if body.machine_id not in F.MACHINE_IDS:
            raise HTTPException(status_code=400, detail=f"沒有這台機台：{body.machine_id}")
        now: datetime = rt().now()
        try:
            note = await asyncio.to_thread(notes().add, body.machine_id, body.text, session.actor, now)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        audit(session, {"action": "memory_note_add", "note_id": note.id, "machine_id": note.machine_id, "text": note.text})
        return {"ok": True, "note": note.__dict__}



def _audit_len(a: Any) -> int:
    """The last seq: len() of the file log, the newest entry of a log without it (the in-memory fallback)."""
    if hasattr(a, "__len__"):
        return len(a)
    return max((e.seq for e in a.tail(0, 10**9)), default=0)


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
                     "modified": datetime.fromtimestamp(st.st_mtime, tz=F.TZ).isoformat(timespec="seconds"),
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


def _line_diff(a: str, b: str) -> list[dict[str, str]]:
    import difflib
    out = []
    for ln in difflib.ndiff(a.splitlines(), b.splitlines()):
        if ln.startswith(("+ ", "- ")):
            out.append({"op": ln[0], "text": ln[2:]})
    return out


def _p11_request():
    from warroom.contracts import (
        Anomaly,
        AnomalyKind,
        Channel,
        CommanderRequest,
        Prediction,
        Severity,
        ToolName,
    )
    t0 = datetime(2026, 10, 3, 2, 14, tzinfo=F.TZ)
    pred = Prediction(machine_id="CURE-07", channel=Channel.TEMP, now_value=122.0, predicted_value=122.0,
                      band_lo=121.0, band_hi=123.0, horizon_s=1800, trend_per_min=0.0)
    a = Anomaly(id="AN-PLANT-TEST", ts=t0, machine_id="CURE-07", kind=AnomalyKind.TEMP_DROP, channel=Channel.TEMP,
                value=122.0, baseline=128.0, prediction=pred, severity=Severity.WARN, message="7 號 128 → 122°C，預測穩定在 122±1°C")
    return CommanderRequest(phase="plan", event_id="PLANT-TEST", anomaly=a, allowed_tools=list(ToolName))


def _plan_view(plan: Any) -> dict[str, Any]:
    ids = {s.id for s in plan.steps}
    problems = [f"{s.id} 依賴不存在的步驟 {d}" for s in plan.steps for d in s.depends_on if d not in ids]
    return {"alert": plan.alert_card.title, "hypothesis": f"{plan.hypothesis.id} {plan.hypothesis.cause_code}：{plan.hypothesis.statement}",
            "steps": [f"{s.id} {s.tool}" for s in plan.steps], "safe_stop": plan.safe_stop, "valid": not problems and bool(plan.steps),
            "problems": problems}


async def _plant_test(r: Any, draft: str, active: str) -> dict[str, Any]:
    from warroom.agent.commander import MockCommander
    req = _p11_request()
    mock_plan, _ = await MockCommander().plan(req)
    mock = {**_plan_view(mock_plan), "note": "規則模式不讀提示詞：只檢查計畫格式"}
    llm = None
    if r.llm_mode.value == "llm" and await r.llm_reachable():
        from warroom.agent.commander import OpenAICommander

        async def one(text: str) -> dict[str, Any]:
            c = OpenAICommander(r.settings)
            c.plant_override = text
            t = time.perf_counter()
            try:
                plan, info = await c.plan(req)
            except Exception as e:  # noqa: BLE001 - reported, never raised to the admin page
                return {"valid": False, "error": f"{type(e).__name__}: {e}"[:300], "ms": round((time.perf_counter() - t) * 1000)}
            v = _plan_view(plan)
            v.update({"valid": v["valid"] and not info.fell_back_to_mock, "fell_back": info.fell_back_to_mock,
                      "error": c.last_error, "ms": round((time.perf_counter() - t) * 1000)})
            return v
        d, a = await asyncio.gather(one(draft), one(active))
        llm = {"draft": d, "active": a, "model": r.settings.model,
               "same_steps": d.get("steps") == a.get("steps"), "same_hypothesis": d.get("hypothesis") == a.get("hypothesis")}
    ok = mock["valid"] and (llm is None or bool(llm["draft"].get("valid")) or bool(llm["active"].get("fell_back")))
    return {"ok": ok, "mock": mock, "llm": llm, "prompt_diff": _line_diff(active, draft)}
