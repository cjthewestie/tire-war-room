"""人員回報 (CJ, 2026-10-22: 「加回報」): the floor worker reports a task's 到達・開始 and 完成 on /worker, the
supervisor can complete one for them (代為完成, reason required), and the 後台 switches the mode.

Who may report: only the task's assigned worker (body worker_id must be the one code assigned), from
  - the on-site seat (loopback outside a container, or a paired kiosk: the shared floor tablet), or
  - a phone that sends that worker's own PIN in `X-Worker-Pin` (env WARROOM_WORKER_PINS="W-001:4821,W-002:…"),
    throttled by the same PinGuard as the supervisor PIN, or
  - a supervisor session (`X-Supervisor-Pin`), reporting what the worker told them.
With no worker PINs configured, remote phones are refused (403) — the same rule as supervisor writes.
The mode switch (自動（展示） ↔ 人員回報) and 代為完成 need the supervisor seat.

A photo is optional: a data URL of a JPEG / PNG / WebP image, at most PHOTO_MAX_BYTES after decoding (the page
shrinks a phone photo before upload), checked by its magic bytes, stored under data/runtime/task_photos/ and
audited by its SHA-256 (the audit row carries the hash, never the image). Every report, override and mode change
is audited (kind task / system) by the runtime's task listener with the worker id as the actor."""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from warroom.contracts import AuditKind

PHOTO_MAX_BYTES = 2 * 1024 * 1024
PHOTO_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
_DATA_URL = re.compile(r"^data:(image/[a-z+.-]+);base64,(.*)$", re.DOTALL)
_TASK_ID = re.compile(r"^TK-\d{1,6}$")


class TaskReportBody(BaseModel):
    worker_id: str = Field(..., min_length=1, max_length=40)
    note: str | None = Field(default=None, max_length=300)
    photo: str | None = Field(default=None, max_length=3 * 1024 * 1024)   # data:image/…;base64,…


class TaskOverrideBody(BaseModel):
    reason: str = Field(..., max_length=300)


class ReportModeBody(BaseModel):
    on: bool


def worker_pins(raw: str | None = None) -> dict[str, str]:
    """WARROOM_WORKER_PINS="W-001:4821,W-002:7730" -> {worker id: PIN}."""
    raw = os.environ.get("WARROOM_WORKER_PINS", "") if raw is None else raw
    out: dict[str, str] = {}
    for part in raw.split(","):
        wid, _, pin = part.strip().partition(":")
        if wid.strip() and pin.strip():
            out[wid.strip()] = pin.strip()
    return out


def decode_photo(data_url: str) -> tuple[bytes, str]:
    """(image bytes, file extension). ValueError (Chinese message) for anything but a small JPEG / PNG / WebP."""
    m = _DATA_URL.match(data_url.strip())
    if m is None:
        raise ValueError("照片格式不對（需要 data:image/…;base64,…）")
    mime = m.group(1)
    if mime not in PHOTO_TYPES:
        raise ValueError(f"只接受 JPEG、PNG、WebP 照片（收到 {mime}）")
    try:
        raw = base64.b64decode(m.group(2), validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("照片內容不是有效的 base64") from None
    if len(raw) > PHOTO_MAX_BYTES:
        raise ValueError(f"照片太大（{len(raw) / 1024 / 1024:.1f} MB，上限 2 MB）")
    magic = {"image/jpeg": raw[:3] == b"\xff\xd8\xff", "image/png": raw[:8] == b"\x89PNG\r\n\x1a\n",
             "image/webp": raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"}[mime]
    if not raw or not magic:
        raise ValueError("檔案內容不是它宣稱的圖片格式")
    return raw, PHOTO_TYPES[mime]


def install_tasks(app: FastAPI, rt: Callable[[], Any], supervisor: Callable[..., Any],
                  on_site: Callable[[Any], bool], guard: Any, audit_pin: Callable[..., None]) -> None:
    def board() -> Any:
        b = getattr(getattr(getattr(rt(), "sim", None), "world", None), "tasks", None)
        if b is None or not callable(getattr(b, "report", None)):
            raise HTTPException(status_code=503, detail="task board unavailable for this sim")
        return b

    def photo_dir() -> Path:
        return Path(rt().settings.audit_path).parent / "task_photos"

    def auth_worker(request: Request, response: Response, worker_id: str) -> str:
        """How this report is authenticated: on_site | worker_pin | supervisor. Raises 401/403/429."""
        if on_site(request):
            return "on_site"
        if request.headers.get("x-supervisor-pin") or request.headers.get("x-supervisor-device"):
            supervisor(request, response)
            return "supervisor"
        pins = worker_pins()
        host = request.client.host if request.client else ""
        if not pins:
            raise HTTPException(status_code=403, detail="遠端回報未開放：請設定 WARROOM_WORKER_PINS，或在現場平板回報")
        wait = guard.admit(host, False)
        if wait > 0:
            raise HTTPException(status_code=429, detail=f"too many wrong PINs: locked for {wait:.0f} s",
                                headers={"Retry-After": str(int(wait) + 1)})
        got, want = request.headers.get("x-worker-pin", ""), pins.get(worker_id, "")
        if not want or not hmac.compare_digest(got.encode(), want.encode()):
            lock_s, new = guard.failure(host)
            audit_pin("worker PIN rejected", host, path=request.url.path, worker_id=worker_id, pin_sent=bool(got))
            if new:
                audit_pin("worker PIN lockout", host, scope=new, lock_s=round(lock_s))
            raise HTTPException(status_code=401, detail="需要你自己的人員 PIN（X-Worker-Pin）")
        guard.success(host)
        return "worker_pin"

    async def report(task_id: str, action: str, body: TaskReportBody, request: Request, response: Response):
        if not _TASK_ID.match(task_id):
            raise HTTPException(status_code=404, detail=f"not found: {task_id}")
        via = auth_worker(request, response, body.worker_id)
        r, b = rt(), board()
        photo: tuple[bytes, str] | None = None
        if body.photo:
            if action != "done":
                raise HTTPException(status_code=422, detail="照片只在回報完成時附上")
            try:
                photo = decode_photo(body.photo)
            except ValueError as e:
                raise HTTPException(status_code=422, detail=str(e)) from None
        sha = hashlib.sha256(photo[0]).hexdigest() if photo else None
        note = (body.note or "").strip() or None
        try:
            t = b.report(task_id, action, body.worker_id, r.clock.now(), note=note, photo_sha256=sha)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"not found: {task_id}") from None
        except PermissionError as e:
            r.audit.append(AuditKind.INTERCEPTED, body.worker_id, {"msg": "task report refused", "task_id": task_id,
                                                                   "action": action, "why": str(e), "via": via})
            raise HTTPException(status_code=403, detail=str(e)) from None
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e)) from None
        if photo is not None:
            d = photo_dir()

            def write() -> None:
                d.mkdir(parents=True, exist_ok=True)
                (d / f"{task_id}-{sha[:16]}.{photo[1]}").write_bytes(photo[0])
            await asyncio.to_thread(write)
        r.audit.append(AuditKind.TASK, body.worker_id, {"msg": "人員回報", "task_id": task_id, "action": action,
                                                        "via": via, "photo_sha256": sha,
                                                        "photo_bytes": len(photo[0]) if photo else None})
        return t.model_dump(mode="json")

    @app.post("/api/tasks/{task_id}/start")
    async def task_start(task_id: str, body: TaskReportBody, request: Request, response: Response) -> dict[str, Any]:
        """到達・開始: the assigned worker arrived and started (前往中 → 執行中). Report mode only."""
        return await report(task_id, "start", body, request, response)

    @app.post("/api/tasks/{task_id}/done")
    async def task_done(task_id: str, body: TaskReportBody, request: Request, response: Response) -> dict[str, Any]:
        """完成: the assigned worker finished (執行中 → 完成), optional note and photo. VERIFY then checks the reading."""
        return await report(task_id, "done", body, request, response)

    @app.post("/api/tasks/{task_id}/override")
    async def task_override(task_id: str, body: TaskOverrideBody, session: Any = Depends(supervisor)) -> dict[str, Any]:
        """代為完成: the supervisor completes a task for a worker who cannot use the phone. Reason required."""
        r, b = rt(), board()
        if not (body.reason or "").strip():
            raise HTTPException(status_code=422, detail="代為完成需要填寫原因")
        try:
            t = b.override(task_id, session.actor, body.reason, r.clock.now())
        except KeyError:
            raise HTTPException(status_code=404, detail=f"not found: {task_id}") from None
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e)) from None
        return t.model_dump(mode="json")

    @app.get("/api/tasks/{task_id}/photo", include_in_schema=False)
    async def task_photo(task_id: str):
        if not _TASK_ID.match(task_id):
            raise HTTPException(status_code=404, detail=f"not found: {task_id}")
        try:
            sha = board().get(task_id).photo_sha256
        except KeyError:
            sha = None
        hits = sorted(photo_dir().glob(f"{task_id}-{sha[:16]}.*")) if sha else []
        if not hits:
            raise HTTPException(status_code=404, detail=f"no photo for {task_id}")
        return FileResponse(hits[0], headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/api/tasks/mode")
    async def task_mode() -> dict[str, Any]:
        return {"mode": "report" if rt().worker_report else "auto", "remote_pins": bool(worker_pins())}

    @app.post("/api/tasks/mode")
    async def set_task_mode(body: ReportModeBody, session: Any = Depends(supervisor)) -> dict[str, Any]:
        """後台: 自動（展示） ↔ 人員回報. Audited."""
        on = rt().set_worker_report(body.on, f"admin:{session.actor}")
        return {"mode": "report" if on else "auto", "remote_pins": bool(worker_pins())}
