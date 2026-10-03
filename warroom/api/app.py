"""FastAPI app: REST per contracts.REST_ENDPOINTS (+ a few demo-control extras), one WebSocket
/ws relaying the InProcessBus, and the built web/ dist (dashboard at /, phone at /phone).

Everything is local: no CDN, no outbound calls except the optional LLM health probe."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import Body, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from warroom import factory as F
from warroom.api.runtime import Runtime
from warroom.config import ROOT
from warroom.contracts import (
    Anomaly,
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    AuditEntry,
    AuditKind,
    CapacityReport,
    DashboardSnapshot,
    Event,
    FaultKind,
    FaultSpec,
    FocusPanel,
    HealthStatus,
    MachineDetail,
    MachineState,
    Order,
    SafetyAction,
    ScenarioName,
    ScheduleVersion,
    Shift,
    SimControl,
    SopHit,
    WsMessage,
    WsType,
)

log = logging.getLogger("warroom.api")
DIST = ROOT / "web" / "dist"


class RollbackBody(BaseModel):
    version: int | None = None


class FaultBody(BaseModel):
    """FaultSpec without the id (server assigns one when missing)."""
    id: str | None = None
    machine_id: str
    kind: FaultKind
    start_offset_s: float = 0.0
    duration_s: float | None = None
    params: dict[str, float] = Field(default_factory=dict)


class StepBody(BaseModel):
    seconds: float = Field(gt=0, le=6 * 3600)


class ClearBody(BaseModel):
    machine_id: str | None = None


class NetworkBody(BaseModel):
    network: Literal["online", "wan_down", "lan_down"]


def _404(what: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"not found: {what}")


def create_app(runtime: Runtime | None = None) -> FastAPI:
    """Routes per contracts.REST_ENDPOINTS, WebSocket /ws, serves web/dist at / and /phone."""
    holder: dict[str, Runtime] = {}

    def rt() -> Runtime:
        if "rt" not in holder:
            holder["rt"] = runtime or Runtime()
        return holder["rt"]

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        r = rt()
        app.state.runtime = r
        await r.start()
        try:
            yield
        finally:
            await r.stop()

    app = FastAPI(title="實心胎智慧製造戰情室 API", version="0.1.0", lifespan=lifespan)
    if runtime is not None:
        app.state.runtime = runtime

    # ------------------------------------------------------------------ health
    @app.get("/api/health", response_model=HealthStatus)
    async def health() -> HealthStatus:
        r = rt()
        return HealthStatus(ok=True, llm_mode=r.llm_mode, model=r.settings.model,
                            llm_reachable=await r.llm_reachable(), qdrant_ok=r.kb_ok(),
                            sim_running=r.running and not r.paused, factory_time=r.now(), uptime_s=r.uptime_s())

    @app.get("/api/health/components")
    async def health_components() -> dict[str, Any]:
        r = rt()
        return {"degraded": r.degraded, "ticks": r.ticks, "paused": r.paused, "speed": r.speed,
                "scenario": r.scenario.value, "loop_running": r.running,
                "audit_chain_ok": _safe(r.audit.verify_chain, False)}

    # ------------------------------------------------------------------ dashboard
    @app.get("/api/snapshot", response_model=DashboardSnapshot)
    async def snapshot() -> DashboardSnapshot:
        return rt().snapshot()

    @app.get("/api/focus", response_model=FocusPanel | None)
    async def focus() -> FocusPanel | None:
        return rt().focus_panel()

    @app.get("/api/lines")
    async def lines() -> list[dict[str, Any]]:
        return rt().lines()

    @app.get("/api/machines", response_model=list[MachineState])
    async def machines(line: str | None = None) -> list[MachineState]:
        return rt().machine_states(line)

    @app.get("/api/machines/{machine_id}", response_model=MachineDetail)
    async def machine(machine_id: str, minutes: float = Query(30.0, gt=0, le=24 * 60)) -> MachineDetail:
        if machine_id not in F.MACHINE_BY_ID:
            raise _404(machine_id)
        return rt().machine_detail(machine_id, minutes)

    @app.get("/api/anomalies", response_model=list[Anomaly])
    async def anomalies() -> list[Anomaly]:
        return list(reversed(rt().anomalies))

    @app.get("/api/safety", response_model=list[SafetyAction])
    async def safety() -> list[SafetyAction]:
        return list(reversed(rt().safety_log))

    # ------------------------------------------------------------------ events
    @app.get("/api/events", response_model=list[Event])
    async def events(open_only: bool = False) -> list[Event]:
        evs = sorted(rt().events(), key=lambda e: e.opened_at, reverse=True)
        if open_only:
            from warroom.contracts import TERMINAL_STATES
            evs = [e for e in evs if e.state not in TERMINAL_STATES]
        return evs

    @app.get("/api/events/{event_id}", response_model=Event)
    async def event(event_id: str) -> Event:
        ev = next((e for e in rt().events() if e.id == event_id), None)
        if ev is None:
            raise _404(event_id)
        return ev

    @app.get("/api/events/{event_id}/focus", response_model=FocusPanel)
    async def event_focus(event_id: str) -> FocusPanel:
        r = rt()
        ev = next((e for e in r.events() if e.id == event_id), None)
        if ev is None:
            raise _404(event_id)
        return r.focus_panel(ev)

    # ------------------------------------------------------------------ approvals
    @app.get("/api/approvals", response_model=list[ApprovalRequest])
    async def approvals(status: str | None = None) -> list[ApprovalRequest]:
        r = rt()
        if status == ApprovalStatus.PENDING.value:
            return list(r.approvals.pending())
        items = r.all_approvals()
        if status and status != "all":
            items = [a for a in items if a.status.value == status]
        return items

    @app.post("/api/approvals/{request_id}/decision", response_model=ApprovalRequest)
    async def decide(request_id: str, decision: ApprovalDecision) -> ApprovalRequest:
        r = rt()
        if decision.request_id != request_id:
            raise HTTPException(status_code=422, detail="request_id in body does not match path")
        try:
            r.approvals.get(request_id)
        except Exception:  # noqa: BLE001 - KeyError or broker-specific not-found
            raise _404(request_id) from None
        decision = decision.model_copy(update={"decided_at": r.now()})
        try:
            return r.approvals.decide(decision)
        except KeyError:
            raise _404(request_id) from None
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e)) from None

    # ------------------------------------------------------------------ schedule
    def _store():
        s = rt().c.store
        if s is None:
            raise HTTPException(status_code=503, detail="schedule store unavailable")
        return s

    @app.get("/api/schedule", response_model=ScheduleVersion)
    async def schedule() -> ScheduleVersion:
        return _store().current()

    @app.get("/api/schedule/versions", response_model=list[ScheduleVersion])
    async def schedule_versions() -> list[ScheduleVersion]:
        return _store().versions()

    @app.post("/api/schedule/rollback", response_model=ScheduleVersion)
    async def schedule_rollback(body: RollbackBody | None = Body(default=None)) -> ScheduleVersion:
        r, store = rt(), _store()
        before = store.current().version
        try:
            v = store.rollback(body.version if body else None)
        except KeyError:
            raise _404(f"schedule version {body.version if body else '?'}") from None
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e)) from None
        r.audit.append(AuditKind.ROLLBACK, "supervisor", {"from_version": before, "to_version": v.version})
        r.bus.publish(WsType.SCHEDULE, v)
        return v

    @app.get("/api/labor")
    async def labor() -> dict[str, Any]:
        return rt().labor_view()

    @app.get("/api/capacity", response_model=CapacityReport)
    async def capacity() -> CapacityReport:
        return rt().c.capacity()

    @app.get("/api/orders", response_model=list[Order])
    async def orders() -> list[Order]:
        return rt().sim.orders()

    @app.get("/api/shifts", response_model=list[Shift])
    async def shifts() -> list[Shift]:
        return rt().sim.shifts()

    # ------------------------------------------------------------------ audit, sop
    @app.get("/api/audit", response_model=list[AuditEntry])
    async def audit(since_seq: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=5000),
                    event_id: str | None = None, kind: AuditKind | None = None) -> list[AuditEntry]:
        items = rt().audit.tail(since_seq=since_seq, limit=limit if not (event_id or kind) else 100_000)
        if event_id:
            items = [e for e in items if e.event_id == event_id]
        if kind:
            items = [e for e in items if e.kind == kind]
        return items[:limit]

    @app.get("/api/sop/search", response_model=list[SopHit])
    async def sop_search(q: str = Query(..., min_length=1), k: int = Query(3, ge=1, le=20)) -> list[SopHit]:
        kb = rt().c.kb
        if kb is None:
            raise HTTPException(status_code=503, detail="SOP knowledge base unavailable")
        return await asyncio.to_thread(kb.search, q, k)

    # ------------------------------------------------------------------ sim control (demo)
    @app.post("/api/sim/control")
    async def sim_control(ctl: SimControl) -> dict[str, Any]:
        r = rt()
        if ctl.action == "play":
            r.play()
        elif ctl.action == "pause":
            r.pause()
        elif ctl.action == "speed":
            if ctl.speed is None:
                raise HTTPException(status_code=422, detail="speed required")
            try:
                r.set_speed(ctl.speed)
            except ValueError as e:
                raise HTTPException(status_code=422, detail=str(e)) from None
        elif ctl.action == "reset":
            await r.reset()
        elif ctl.action == "scenario":
            if ctl.scenario is None:
                raise HTTPException(status_code=422, detail="scenario required")
            await r.load_scenario(ctl.scenario)
        if ctl.speed is not None and ctl.action != "speed":
            r.set_speed(ctl.speed)
        r.bus.publish(WsType.CLOCK, r.clock_info())
        return {"ok": True, **r.clock_info()}

    @app.get("/api/sim/scenarios")
    async def sim_scenarios() -> list[dict[str, Any]]:
        try:
            from warroom.sim import list_scenarios
            return [s.model_dump(mode="json") for s in list_scenarios()]
        except Exception:  # noqa: BLE001 - sim module still a stub
            from warroom.api.fallbacks import fallback_scenario
            return [fallback_scenario(n).model_dump(mode="json") for n in ScenarioName]

    @app.post("/api/sim/step")
    async def sim_step(body: StepBody) -> dict[str, Any]:
        r = rt()
        await r.run_seconds(body.seconds)
        return {"ok": True, **r.clock_info()}

    @app.post("/api/sim/fault")
    async def sim_fault(body: FaultBody) -> dict[str, Any]:
        r = rt()
        if body.machine_id not in F.MACHINE_BY_ID:
            raise _404(body.machine_id)
        fid = body.id or f"F-API-{r.now():%H%M%S}-{body.machine_id}"
        spec = FaultSpec(id=fid, machine_id=body.machine_id, kind=body.kind, start_offset_s=body.start_offset_s,
                         duration_s=body.duration_s, params=body.params)
        r.inject_fault(spec)
        return {"ok": True, "fault": spec.model_dump(mode="json")}

    @app.post("/api/sim/clear")
    async def sim_clear(body: ClearBody | None = Body(default=None)) -> dict[str, Any]:
        mid = body.machine_id if body else None
        if mid and mid not in F.MACHINE_BY_ID:
            raise _404(mid)
        rt().clear_faults(mid)
        return {"ok": True}

    @app.post("/api/sim/network")
    async def sim_network(body: NetworkBody) -> dict[str, Any]:
        """Offline-tier demo (page 9): only changes what the header reports."""
        r = rt()
        r.network = body.network
        r.audit.append(AuditKind.SYSTEM, "supervisor", {"msg": "network tier", "network": body.network})
        r.bus.publish(WsType.SNAPSHOT, r.snapshot())
        return {"ok": True, "network": r.network}

    # ------------------------------------------------------------------ websocket
    @app.websocket("/ws")
    async def ws(sock: WebSocket) -> None:
        await sock.accept()
        r = rt()
        sub = r.bus.subscribe()
        nxt = asyncio.ensure_future(sub.__anext__())
        await asyncio.sleep(0)               # registers the subscriber queue before the snapshot
        drain = asyncio.create_task(_drain(sock))
        try:
            await _send(sock, WsType.SNAPSHOT, r.snapshot().model_dump(mode="json"), r.now())
            await _send(sock, WsType.CLOCK, r.clock_info(), r.now())
            while True:
                done, _ = await asyncio.wait({nxt, drain}, return_when=asyncio.FIRST_COMPLETED)
                if drain in done:
                    break
                msg: WsMessage = nxt.result()
                await sock.send_text(msg.model_dump_json())
                nxt = asyncio.ensure_future(sub.__anext__())
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            drain.cancel()
            nxt.cancel()
            with contextlib.suppress(BaseException):
                await nxt
            with contextlib.suppress(Exception):
                await sub.aclose()

    # ------------------------------------------------------------------ web dist
    _mount_web(app)
    return app


async def _drain(sock: WebSocket) -> None:
    """Read and ignore client frames; returns when the client disconnects."""
    try:
        while True:
            await sock.receive_text()
    except (WebSocketDisconnect, RuntimeError):
        return


async def _send(sock: WebSocket, type_: WsType, data: dict[str, Any], ts: datetime) -> None:
    await sock.send_text(WsMessage(type=type_, ts=ts, data=data).model_dump_json())


def _safe(fn, default):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return default


_MISSING = """<!doctype html><meta charset="utf-8"><title>戰情室</title>
<body style="background:#0b0f0c;color:#e6e6e6;font-family:sans-serif;padding:2rem">
<h1>實心胎智慧製造戰情室</h1><p>web/dist not built. Run <code>cd web && npm install && npm run build</code>.
API is live: <a style="color:#76b900" href="/api/snapshot">/api/snapshot</a> · <a style="color:#76b900" href="/docs">/docs</a></p>"""


def _mount_web(app: FastAPI, dist: Path = DIST) -> None:
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    def page(name: str):
        p = dist / name
        if p.is_file():
            return FileResponse(p, headers={"Cache-Control": "no-cache"})
        return HTMLResponse(_MISSING, status_code=200)

    @app.get("/", include_in_schema=False)
    async def index():
        return page("index.html")

    @app.get("/phone", include_in_schema=False)
    @app.get("/phone.html", include_in_schema=False)
    async def phone():
        return page("phone.html")

    @app.get("/favicon.svg", include_in_schema=False)
    async def favicon():
        p = dist / "favicon.svg"
        if not p.is_file():
            raise _404("favicon.svg")
        return FileResponse(p)
