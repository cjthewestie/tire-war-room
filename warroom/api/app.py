"""FastAPI app: REST per contracts.REST_ENDPOINTS (+ a few demo-control extras), one WebSocket
/ws relaying the InProcessBus, and the built web/ dist (dashboard at /, phone at /phone, 3D factory at /factory,
floor worker's tasks at /worker, shift handover at /handover, incident report at /report).

Everything is local: no CDN, no outbound calls except the optional LLM health probe.

Write access (approvals, schedule rollback, /api/sim/*): a request from this box itself (loopback:
the on-site touch screen / dashboard kiosk) is the supervisor seat. Any other LAN client must send
the shared supervisor PIN in `X-Supervisor-Pin` (env WARROOM_SUPERVISOR_PIN); with no PIN set,
remote writes are refused. `decided_by` and `via` are set by the server from that session, never
taken from the body as given, and `via="auto"` is server-only. /api/sim/* are demo controls and can
be switched off with WARROOM_DEMO_CONTROLS=0.
PIN guessing is throttled (PinGuard): per client address, 5 wrong PINs lock that address out with
a doubling back-off (30 s .. 15 min); 20 wrong PINs from anywhere within 10 min put every remote
address WITHOUT a paired device on a shared slow lane (one PIN attempt per 10 s, for 5 min). A
correct PIN is never refused for being someone else's bad luck: a phone that entered the PIN once
holds a paired-device cookie (warroom_device, second factor) and skips the slow lane. Every wrong
PIN and every lockout is written to the audit log (kind intercepted).
Behind Docker NAT every client arrives with the gateway's address, so an address cannot identify
the on-site seat. In a container loopback is not the seat by default; the touch screen pairs once
with WARROOM_KIOSK_TOKEN via GET /kiosk?token=... (HttpOnly cookie). With neither a PIN nor a kiosk
token configured in a container, a kiosk token is generated and its pairing URL logged.
The PIN travels only in the header (never in a URL); set WARROOM_SSL_CERTFILE/KEYFILE to serve TLS. Page 9 tiers: weak (弱網)
leaves the agent untouched and only delays/batches WebSocket pushes to the phone and other remote
clients (WARROOM_WEAK_PUSH_DELAY_S, default 5 s); wan_down keeps the phone on the factory LAN;
lan_down refuses phone/remote approvals (409) and lights the warning light; only the local touch
screen can decide."""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import os
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi import Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from warroom import factory as F
from warroom.api.runtime import Runtime
from warroom.config import ROOT
from warroom.api.tasks import install_tasks
from warroom.contracts import (
    Anomaly,
    AskAnswer,
    AskRequest,
    CommandReply,
    CommandRequest,
    DiscussReply,
    DiscussRequest,
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    AuditEntry,
    DecisionKind,
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
    ReviewRequest,
    RiskLevel,
    SafetyAction,
    SafetyCase,
    SafetyRelease,
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


class QaAsk(BaseModel):
    """QA 模式 (warroom/agent/qa.py): a judge's question about the agent framework."""
    question: str = Field(min_length=1, max_length=300)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=64)   # one per page load


class MainMessage(BaseModel):
    """Issue #54: a message to the Agent Nemo (it classifies it itself)."""
    text: str = Field(min_length=1, max_length=300)
    event_id: str | None = None       # the item the supervisor has selected (its waiting plan, if any)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=64)   # one per page load


class RollbackBody(BaseModel):
    version: int | None = None
    confirm: bool = False      # the supervisor saw the preview (versions, diff) and confirmed: the R2/R3 approval
    reason: str = ""


LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})
KIOSK_COOKIE = "warroom_kiosk"
DEVICE_COOKIE = "warroom_device"
_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


def in_container() -> bool:
    env = os.environ.get("WARROOM_IN_CONTAINER", "").strip().lower()
    if env in _TRUE:
        return True
    if env in _FALSE:
        return False
    return Path("/.dockerenv").exists()


@dataclass(frozen=True)
class Session:
    actor: str          # what goes into decided_by / the audit actor
    local: bool         # on the box itself (touch screen / kiosk)
    host: str


@dataclass
class AccessConfig:
    supervisor_pin: str | None
    demo_controls: bool
    local_hosts: frozenset[str] = LOCAL_HOSTS
    kiosk_token: str | None = None
    container: bool = False

    @classmethod
    def from_env(cls, supervisor_pin: str | None = None, demo_controls: bool | None = None,
                 local_hosts: frozenset[str] | None = None, kiosk_token: str | None = None,
                 container: bool | None = None) -> AccessConfig:
        pin = supervisor_pin if supervisor_pin is not None else (os.environ.get("WARROOM_SUPERVISOR_PIN") or None)
        demo = demo_controls if demo_controls is not None else \
            os.environ.get("WARROOM_DEMO_CONTROLS", "1").lower() not in ("0", "false", "no", "off")
        extra = frozenset(h.strip() for h in os.environ.get("WARROOM_LOCAL_HOSTS", "").split(",") if h.strip())
        # WARROOM_LOOPBACK_LOCAL=0: loopback is NOT the on-site seat. Set when a tunnel/port-forward (the
        # OpenShell sandbox, deploy/openshell/run_sandboxed.sh) delivers every client as 127.0.0.1, so
        # loopback would otherwise make every LAN phone PIN-free. Only WARROOM_LOCAL_HOSTS then counts.
        container = in_container() if container is None else container
        # In a container (Docker NAT) the on-box browser arrives as the bridge gateway, never as
        # loopback; only in-container processes are loopback. So loopback is not the seat there
        # unless WARROOM_LOOPBACK_LOCAL=1 says so explicitly.
        lb_env = os.environ.get("WARROOM_LOOPBACK_LOCAL", "").strip().lower()
        loopback = lb_env in _TRUE if lb_env else not container
        base = local_hosts or (LOCAL_HOSTS if loopback else frozenset())
        kiosk = kiosk_token if kiosk_token is not None else (os.environ.get("WARROOM_KIOSK_TOKEN") or None)
        if container and extra:
            log.warning("WARROOM_LOCAL_HOSTS=%s inside a container: behind Docker NAT a LAN client can arrive "
                        "with the gateway's address and would become the PIN-free seat. Prefer "
                        "WARROOM_KIOSK_TOKEN (GET /kiosk?token=...) or network_mode: host.", ",".join(sorted(extra)))
        if container and not pin and not kiosk:
            import secrets
            kiosk = secrets.token_urlsafe(18)
            log.warning("no WARROOM_SUPERVISOR_PIN and no WARROOM_KIOSK_TOKEN in a container: nobody could "
                        "approve. Generated a kiosk token for this run; pair the on-site screen once by "
                        "opening  /kiosk?token=%s", kiosk)
        return cls(supervisor_pin=pin, demo_controls=demo, local_hosts=base | extra, kiosk_token=kiosk,
                   container=container)


class PinGuard:
    """Brute-force throttle for the supervisor PIN. Keyed by the TCP peer address (never by
    X-Forwarded-For, which the client controls). Wall-clock (monotonic) on purpose: this is a
    security limit, not factory time.

    Per host: per_host_max wrong PINs lock that address with a doubling back-off.
    Global: global_max wrong PINs from anywhere within global_window_s start a slow lane for
    global_lock_s: callers without a paired device share ONE attempt per global_slow_s, so many
    addresses cannot share the guessing work, yet a correct PIN still gets through (delayed, never
    refused). A paired device (trusted=True: it entered the right PIN before) skips the slow lane."""

    def __init__(self, per_host_max: int = 5, base_lock_s: float = 30.0, max_lock_s: float = 900.0,
                 global_max: int = 20, global_window_s: float = 600.0, global_lock_s: float = 300.0,
                 global_slow_s: float = 10.0, now: Callable[[], float] = time.monotonic):
        self.per_host_max, self.base_lock_s, self.max_lock_s = per_host_max, base_lock_s, max_lock_s
        self.global_max, self.global_window_s, self.global_lock_s = global_max, global_window_s, global_lock_s
        self.global_slow_s = global_slow_s
        self.now = now
        self._lock = threading.Lock()
        self._fails: dict[str, int] = {}          # consecutive wrong PINs since the last lock/success
        self._strikes: dict[str, int] = {}        # lockouts so far (doubles the next one)
        self._until: dict[str, float] = {}
        self._global: deque[float] = deque()
        self._global_until = 0.0                  # slow lane in force until
        self._next_slot = 0.0                     # next slow-lane attempt allowed at

    def slow_lane(self) -> bool:
        with self._lock:
            return self._global_until > self.now()

    def _wait(self, host: str, trusted: bool, t: float) -> float:
        w = self._until.get(host, 0.0) - t
        if not trusted and self._global_until > t:
            w = max(w, self._next_slot - t)
        return max(0.0, w)

    def locked_for(self, host: str, trusted: bool = False) -> float:
        """Seconds this host must still wait before its next PIN attempt (0 = may try)."""
        with self._lock:
            return self._wait(host, trusted, self.now())

    def admit(self, host: str, trusted: bool = False) -> float:
        """Gate one PIN attempt: returns the wait (0 = go). In the slow lane, a go reserves the
        shared slot, so the next untrusted attempt anywhere waits global_slow_s."""
        with self._lock:
            t = self.now()
            w = self._wait(host, trusted, t)
            if w == 0 and not trusted and self._global_until > t:
                self._next_slot = t + self.global_slow_s
            return w

    def failure(self, host: str) -> tuple[float, str | None]:
        """Record a wrong PIN. Returns (seconds this host must now wait, 'host'|'global'|None if new)."""
        with self._lock:
            t = self.now()
            self._global.append(t)
            while self._global and t - self._global[0] > self.global_window_s:
                self._global.popleft()
            new = None
            if len(self._global) >= self.global_max and self._global_until <= t:
                self._global_until = t + self.global_lock_s
                self._global.clear()
                new = "global"
            n = self._fails.get(host, 0) + 1
            self._fails[host] = n
            if n >= self.per_host_max:
                k = self._strikes.get(host, 0)
                self._strikes[host] = k + 1
                self._until[host] = t + min(self.max_lock_s, self.base_lock_s * 2 ** k)
                self._fails[host] = 0
                new = new or "host"
            return max(0.0, self._until.get(host, 0.0) - t), new

    def success(self, host: str) -> None:
        with self._lock:
            self._fails.pop(host, None)
            self._strikes.pop(host, None)
            self._until.pop(host, None)


class DeviceRegistry:
    """Paired supervisor devices: a random token issued after a correct PIN (HttpOnly cookie). It is
    a second factor that exempts the device from the global slow lane; the PIN is still required."""

    def __init__(self, max_devices: int = 64):
        self._tokens: dict[str, float] = {}
        self._lock = threading.Lock()
        self.max_devices = max_devices

    def issue(self) -> str:
        import secrets
        tok = secrets.token_urlsafe(24)
        with self._lock:
            self._tokens[tok] = time.monotonic()
            while len(self._tokens) > self.max_devices:
                self._tokens.pop(next(iter(self._tokens)))
        return tok

    def valid(self, tok: str | None) -> bool:
        if not tok:
            return False
        with self._lock:
            return any(hmac.compare_digest(tok.encode(), k.encode()) for k in self._tokens)


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


class SimEventsBody(BaseModel):
    """Issue #36 模擬事件 settings; omitted fields keep their value."""
    enabled: bool | None = None
    every_min: float | None = Field(default=None, ge=5.0, le=240.0)
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1)


class FireEventBody(BaseModel):
    kind: str = Field(min_length=1, max_length=32)


class NetworkBody(BaseModel):
    network: Literal["online", "weak", "wan_down", "lan_down"]


# full-state messages: on a weak link only the newest of each is worth sending
_COALESCE = frozenset({WsType.SNAPSHOT, WsType.MACHINES, WsType.CLOCK})


class WeakLinkBuffer:
    """Page 9 弱網: pushes to a remote client are held and sent as one batch `delay_s` after the
    first held message (wall clock: this is the network, not factory time). Full-state messages
    (snapshot, machines, clock) are coalesced to the newest; every event, approval, audit and
    schedule message is kept, in order. Nothing upstream waits on this: the plant loop, the safety
    core and the agent publish to the bus as usual."""

    def __init__(self, delay_s: float):
        self.delay_s = max(0.0, float(delay_s))
        self._held: list[WsMessage] = []
        self._since: float | None = None
        self.held_total = 0
        self.coalesced = 0
        self.batches = 0

    def __len__(self) -> int:
        return len(self._held)

    def push(self, msg: WsMessage, now: float) -> None:
        if msg.type in _COALESCE:
            before = len(self._held)
            self._held = [m for m in self._held if m.type is not msg.type]
            self.coalesced += before - len(self._held)
        self._held.append(msg)
        self.held_total += 1
        if self._since is None:
            self._since = now

    def wait_s(self, now: float) -> float | None:
        """Seconds until the held batch is due (None = nothing held)."""
        if self._since is None:
            return None
        return max(0.0, self._since + self.delay_s - now)

    def due(self, now: float) -> list[WsMessage]:
        if self._since is None or now < self._since + self.delay_s:
            return []
        return self.flush()

    def flush(self) -> list[WsMessage]:
        out, self._held, self._since = self._held, [], None
        if out:
            self.batches += 1
        return out


def _norm_host(h: str) -> str:
    """'::ffff:127.0.0.1' (IPv4-mapped, what Node's proxy forwards) -> '127.0.0.1'."""
    h = h.strip()
    return h[7:] if h.lower().startswith("::ffff:") else h


def _404(what: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"not found: {what}")


def create_app(runtime: Runtime | None = None, *, supervisor_pin: str | None = None,
               demo_controls: bool | None = None, local_hosts: frozenset[str] | None = None,
               pin_guard: PinGuard | None = None, kiosk_token: str | None = None,
               container: bool | None = None, weak_push_delay_s: float | None = None) -> FastAPI:
    """Routes per contracts.REST_ENDPOINTS, WebSocket /ws, serves web/dist at /, /phone and /factory."""
    holder: dict[str, Any] = {}
    access = AccessConfig.from_env(supervisor_pin, demo_controls, local_hosts, kiosk_token, container)
    guard = pin_guard or PinGuard()
    devices = DeviceRegistry()
    if weak_push_delay_s is None:
        try:
            weak_push_delay_s = float(os.environ.get("WARROOM_WEAK_PUSH_DELAY_S", "5"))
        except ValueError:
            weak_push_delay_s = 5.0
    weak_stats = {"held": 0, "coalesced": 0, "batches": 0}
    if access.supervisor_pin and len(access.supervisor_pin) < 8:
        log.warning("WARROOM_SUPERVISOR_PIN is %d characters; use 8+ random characters (PIN guessing is "
                    "throttled, but a short PIN is still guessable over time)", len(access.supervisor_pin))

    def _audit_pin(msg: str, host: str, **extra: Any) -> None:
        try:
            rt().audit.append(AuditKind.INTERCEPTED, f"remote@{host}", {"msg": msg, "host": host, **extra})
        except Exception:  # noqa: BLE001 - auditing must not turn a 401 into a 500
            log.exception("audit pin failure")

    def kiosk_ok(request: Request | WebSocket) -> bool:
        if not access.kiosk_token:
            return False
        got = request.cookies.get(KIOSK_COOKIE) or request.headers.get("x-kiosk-token") or ""
        return bool(got) and hmac.compare_digest(got.encode(), access.kiosk_token.encode())

    def on_site(conn: Request | WebSocket) -> bool:
        """The on-site seat (loopback outside a container, or a paired kiosk), same rule as writes."""
        host = _norm_host(conn.client.host if conn.client else "")
        fwd = [_norm_host(h) for h in conn.headers.get("x-forwarded-for", "").split(",") if h.strip()]
        return (host in access.local_hosts and all(h in access.local_hosts for h in fwd)) or kiosk_ok(conn)

    def supervisor(request: Request, response: Response) -> Session:
        """Who may write. The on-site seat = loopback (outside a container) or a paired kiosk;
        LAN clients need the supervisor PIN."""
        host = _norm_host(request.client.host if request.client else "")
        fwd = [_norm_host(h) for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
        local = host in access.local_hosts and all(h in access.local_hosts for h in fwd)
        if local or kiosk_ok(request):
            return Session(actor="supervisor", local=True, host=host)
        if not access.supervisor_pin:
            hint = " or pair the on-site screen via /kiosk?token=..." if access.kiosk_token else ""
            raise HTTPException(status_code=403, detail="remote writes are disabled: set WARROOM_SUPERVISOR_PIN "
                                                        f"and send it as X-Supervisor-Pin{hint}")
        trusted = devices.valid(request.cookies.get(DEVICE_COOKIE) or request.headers.get("x-supervisor-device"))
        wait = guard.admit(host, trusted)
        if wait > 0:
            raise HTTPException(status_code=429, detail=f"too many wrong PINs: locked for {wait:.0f} s",
                                headers={"Retry-After": str(int(wait) + 1)})
        got = request.headers.get("x-supervisor-pin", "")
        if not hmac.compare_digest(got.encode(), access.supervisor_pin.encode()):
            lock_s, new = guard.failure(host)
            _audit_pin("supervisor PIN rejected", host, path=request.url.path, pin_sent=bool(got))
            if new:
                _audit_pin("supervisor PIN lockout", host, scope=new, lock_s=round(lock_s))
            if lock_s > 0:
                raise HTTPException(status_code=429, detail=f"too many wrong PINs: locked for {lock_s:.0f} s",
                                    headers={"Retry-After": str(int(lock_s) + 1)})
            raise HTTPException(status_code=401, detail="supervisor PIN required (X-Supervisor-Pin)")
        guard.success(host)
        if not trusted:                          # pair this device: skips the global slow lane next time
            tok = devices.issue()
            response.set_cookie(DEVICE_COOKIE, tok, httponly=True, samesite="strict",
                                secure=request.url.scheme == "https", max_age=12 * 3600)
            response.headers["X-Supervisor-Device"] = tok
        return Session(actor=f"supervisor@{fwd[0] if fwd else host}", local=False, host=fwd[0] if fwd else host)

    def demo(session: Session = Depends(supervisor)) -> Session:
        if not access.demo_controls:
            raise HTTPException(status_code=403, detail="demo controls are disabled (WARROOM_DEMO_CONTROLS=0)")
        return session

    def rt() -> Runtime:
        if "rt" not in holder:
            holder["rt"] = runtime or Runtime()
        return holder["rt"]

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        r = rt()
        app.state.runtime = r
        from warroom.agent import plant           # PLANT.md versions live next to the audit log (issue #37)
        plant.configure(Path(r.settings.audit_path).parent)
        await r.start()
        try:
            yield
        finally:
            await r.stop()

    app = FastAPI(title="實心胎智慧製造戰情室 API", version="0.1.0", lifespan=lifespan)
    if runtime is not None:
        app.state.runtime = runtime
    app.state.access = access
    app.state.pin_guard, app.state.weak_push_delay_s, app.state.holder = guard, weak_push_delay_s, holder   # 後台 系統參數 / 健康

    # ------------------------------------------------------------------ on-site seat pairing
    @app.get("/kiosk", include_in_schema=False)
    async def kiosk(request: Request, token: str = Query("", max_length=256)):
        """Pair the on-site touch screen once: the right WARROOM_KIOSK_TOKEN sets an HttpOnly cookie
        that makes this browser the local seat (no source-IP trust behind Docker NAT)."""
        host = _norm_host(request.client.host if request.client else "")
        if not access.kiosk_token:
            raise HTTPException(status_code=404, detail="kiosk pairing is not configured (WARROOM_KIOSK_TOKEN)")
        wait = guard.admit(host)
        if wait > 0:
            raise HTTPException(status_code=429, detail=f"too many wrong tokens: locked for {wait:.0f} s",
                                headers={"Retry-After": str(int(wait) + 1)})
        if not hmac.compare_digest(token.encode(), access.kiosk_token.encode()):
            lock_s, new = guard.failure(host)
            _audit_pin("kiosk token rejected", host)
            if new:
                _audit_pin("kiosk token lockout", host, scope=new, lock_s=round(lock_s))
            raise HTTPException(status_code=401, detail="wrong kiosk token")
        guard.success(host)
        _audit_pin("kiosk paired", host)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(KIOSK_COOKIE, access.kiosk_token, httponly=True, samesite="strict",
                        secure=request.url.scheme == "https", max_age=365 * 24 * 3600)
        return resp

    # ------------------------------------------------------------------ health
    @app.get("/api/health", response_model=HealthStatus)
    async def health() -> HealthStatus:
        r = rt()
        return HealthStatus(ok=True, llm_mode=r.llm_mode, model=r.settings.model, strict=getattr(r, "strict", False),
                            llm_reachable=await r.llm_reachable(), qdrant_ok=r.kb_ok(),
                            sim_running=r.running and not r.paused, factory_time=r.now(), uptime_s=r.uptime_s(),
                            agent_runtime=os.environ.get("WARROOM_AGENT_RUNTIME") or None,
                            repair_fast_s=float(r.settings.repair_fast_s),
                            recover_fast_s=float(r.settings.recover_fast_s))

    @app.get("/api/health/components")
    async def health_components() -> dict[str, Any]:
        r = rt()
        ingest = getattr(r, "ingest", None)
        return {"degraded": r.degraded, "ticks": r.ticks, "paused": r.paused, "speed": r.speed,
                "scenario": r.scenario.value, "loop_running": r.running,
                "audit_chain_ok": _safe(r.audit.verify_chain, False),
                "ingest": ingest.stats.as_dict() if ingest else None,
                "schedule_store_reopened": getattr(r.builder, "reopened_store", None) if r.builder else None,
                "network": r.network, "warning_light": r.warning_light(),
                "weak_link": {"push_delay_s": weak_push_delay_s, **weak_stats},
                "access": {"remote_pin_set": bool(access.supervisor_pin), "demo_controls": access.demo_controls}}

    # ------------------------------------------------------------------ dashboard
    @app.get("/api/snapshot", response_model=DashboardSnapshot)
    async def snapshot() -> DashboardSnapshot:
        return rt().snapshot()

    @app.get("/api/world")
    async def world() -> dict[str, Any]:
        """The whole plant around the machines (issue #11): orders, materials, dock, people, energy, quality,
        maintenance. Read only. Shape: warroom.sim.world.WorldSnapshot."""
        snap = rt().world_snapshot()
        if snap is None:
            raise HTTPException(status_code=503, detail="world view unavailable for this sim")
        return snap.model_dump(mode="json")

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

    @app.post("/api/events/{event_id}/review", response_model=Event)
    async def event_review(event_id: str, body: ReviewRequest | None = Body(default=None),
                           session: Session = Depends(supervisor)) -> Event:
        """主管已讀報告: the supervisor read the incident report of an event that closed after its repair (review
        pending → done; nothing waited for it). Same session rule as approvals; 409 when the event has no pending
        report. Clients get it as any event change."""
        r = rt()
        if r.network == "lan_down" and not session.local:
            raise HTTPException(status_code=409, detail="LAN 中斷：請至現場觸控螢幕標記已讀")
        sm = r.state_machine
        if not callable(getattr(sm, "review", None)):
            raise HTTPException(status_code=503, detail="agent unavailable (degraded mode)")
        note = (body.note or "").strip() if body is not None else ""
        try:
            return sm.review(event_id, session.actor, note or None)
        except KeyError:
            raise _404(event_id) from None
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e)) from None

    @app.post("/api/machines/{machine_id}/return-to-service")
    async def return_to_service(machine_id: str, session: Session = Depends(supervisor)) -> dict[str, Any]:
        """#104 恢復運轉: the supervisor puts a machine whose 檢修 is done back in service (OUT_OF_SERVICE -> RUNNING).
        Same session rule as approvals; 409 when it is not out of service or not repaired yet. Audited."""
        r = rt()
        if r.network == "lan_down" and not session.local:
            raise HTTPException(status_code=409, detail="LAN 中斷：請至現場觸控螢幕操作")
        try:
            r.return_to_service(machine_id, session.actor)
        except KeyError:
            raise _404(machine_id) from None
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e)) from None
        return {"machine_id": machine_id, "status": "running"}

    # ------------------------------------------------------------------ resolution paths (warroom/api/resolve.py)
    # CJ, 2026-10-22: 「安全核心要設計可以被解決 安全停止也要有可以走向解決的按鈕」. Every write here is a HUMAN supervisor
    # action (same session rule as approvals); none of it is a tool, and the agent / commander cannot reach it.
    def _resolve_call(session: Session, fn: Callable[[], Any], what: str) -> Any:
        r = rt()
        if r.network == "lan_down" and not session.local:
            raise HTTPException(status_code=409, detail=f"LAN 中斷：請至現場觸控螢幕{what}")
        try:
            return fn()
        except KeyError as e:
            raise HTTPException(status_code=404, detail=f"not found: {e.args[0] if e.args else ''}") from None
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e)) from None

    @app.get("/api/safety/cases", response_model=list[SafetyCase])
    async def safety_cases() -> list[SafetyCase]:
        r = rt()
        return r.cases.view(r._board())

    @app.post("/api/safety/{case_id}/dispatch", response_model=SafetyCase)
    async def safety_dispatch(case_id: str, session: Session = Depends(supervisor)) -> SafetyCase:
        """① 派現場檢查: worker tasks from the rule's SOP."""
        return _resolve_call(session, lambda: rt().safety_dispatch(case_id, session.actor), "派工")

    @app.post("/api/safety/{case_id}/release", response_model=SafetyCase)
    async def safety_release(case_id: str, body: SafetyRelease, session: Session = Depends(supervisor)) -> SafetyCase:
        """③ 確認恢復運轉 / QC 合格放行 / 報廢. 409 until the reading is back in range for the core's re-arm count AND the
        field tasks are done (or 「已現場確認」 with a reason). Audited with the session as actor."""
        return _resolve_call(session, lambda: rt().safety_release(case_id, body, session.actor), "確認復機")

    @app.post("/api/events/{event_id}/replan", response_model=Event)
    async def stop_replan(event_id: str, session: Session = Depends(supervisor)) -> Event:
        """SAFE_STOP → 「請 AI 重新規劃」: returns the NEW event (its `previous` names the stopped one and why)."""
        return _resolve_call(session, lambda: rt().stop_replan(event_id, session.actor), "操作")

    @app.post("/api/events/{event_id}/handoff", response_model=Event)
    async def stop_handoff(event_id: str, session: Session = Depends(supervisor)) -> Event:
        """SAFE_STOP → 「交給現場處理」: worker tasks from the SOP; the event shows 人工處理中."""
        return _resolve_call(session, lambda: rt().stop_handoff(event_id, session.actor), "派工")

    @app.post("/api/events/{event_id}/close", response_model=Event)
    async def stop_close(event_id: str, body: ReviewRequest | None = Body(default=None),
                         session: Session = Depends(supervisor)) -> Event:
        """SAFE_STOP → 「確認結案」 (supervisor acknowledge; after the floor's tasks when it was handed over)."""
        note = ((body.note or "").strip() if body is not None else "") or None
        return _resolve_call(session, lambda: rt().stop_close(event_id, session.actor, note), "結案")

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
    async def decide(request_id: str, decision: ApprovalDecision,
                     session: Session = Depends(supervisor)) -> ApprovalRequest:
        r = rt()
        if decision.request_id != request_id:
            raise HTTPException(status_code=422, detail="request_id in body does not match path")
        if decision.via == "auto":
            raise HTTPException(status_code=422, detail="via='auto' is reserved for the server")
        if r.network == "lan_down" and (decision.via == "phone" or not session.local):
            raise HTTPException(status_code=409, detail="LAN 中斷：手機／遠端核准不可用，請至現場觸控螢幕核准（警示燈已亮）")
        # who decided is the authenticated session, not what the body claims
        decision = decision.model_copy(update={"decided_by": session.actor})
        try:
            r.approvals.get(request_id)
        except Exception:  # noqa: BLE001 - KeyError or broker-specific not-found
            raise _404(request_id) from None
        decision = decision.model_copy(update={"decided_at": r.now()})
        try:
            out = r.approvals.decide(decision)
        except KeyError:
            raise _404(request_id) from None
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e)) from None
        _safe(r.sync_warning_light, False)
        _safe(lambda: r.orch.on_decided(out), None)        # issue #54: 核准並派發 -> the main agent dispatches execution
        return out

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

    def _preview(version: int | None) -> dict[str, Any]:
        _store()
        try:
            return rt().rollback_preview(version)
        except KeyError:
            raise _404(f"schedule version {version}") from None
        except ValueError as e:
            raise HTTPException(status_code=409, detail={"message": str(e), "feasible": False,
                                                         "reasons": [str(e)]}) from None

    @app.get("/api/schedule/rollback/preview")
    async def schedule_rollback_preview(version: int | None = None) -> dict[str, Any]:
        """What a rollback would do (from/to version, moved jobs) and whether the gate allows it."""
        return _preview(version)

    @app.post("/api/schedule/rollback", response_model=ScheduleVersion)
    async def schedule_rollback(body: RollbackBody | None = Body(default=None),
                                session: Session = Depends(supervisor)) -> ScheduleVersion:
        """One-click rollback (page 7), gated: only a version that was ACTIVE before; never one that
        loads a press now out of service / stopped; needs an explicit confirm (the R2/R3 approval,
        recorded through the approval broker with the session as decider)."""
        r, store = rt(), _store()
        body = body or RollbackBody()
        pv = _preview(body.version)
        eid = store.get(pv["to_version"]).event_id or "SCHEDULE"
        if not pv["feasible"]:
            r.audit.append(AuditKind.GATE, "action_gate",
                           {"action": "schedule_rollback", "decision": "reject", "from_version": pv["from_version"],
                            "to_version": pv["to_version"], "reasons": pv["reasons"], "by": session.actor},
                           event_id=None if eid == "SCHEDULE" else eid)
            raise HTTPException(status_code=409, detail={"message": "；".join(pv["reasons"]), **pv})
        if not body.confirm:
            raise HTTPException(status_code=428, detail={"message": "confirm required", **pv})
        if r.network == "lan_down" and not session.local:
            raise HTTPException(status_code=409, detail="LAN 中斷：請至現場觸控螢幕操作")
        r.audit.append(AuditKind.GATE, "action_gate",
                       {"action": "schedule_rollback", "decision": "allow", "risk": pv["risk"],
                        "from_version": pv["from_version"], "to_version": pv["to_version"]},
                       event_id=None if eid == "SCHEDULE" else eid)
        rid = f"AP-RB-{r.now():%H%M%S}-v{pv['from_version']}to{pv['to_version']}"
        n = 1
        while _safe(lambda: r.approvals.get(rid), None) is not None:
            n += 1
            rid = f"AP-RB-{r.now():%H%M%S}-v{pv['from_version']}to{pv['to_version']}-{n}"
        moved = "、".join(f"{m['order_id']} {m['from_press']}→{m['to_press']}" for m in pv["moved"][:3])
        impact = f"換機 {pv['moved_count']} 筆" + (f"：{moved}" if moved else "")
        if pv.get("retimed_count"):
            impact += f"；同機改時間 {pv['retimed_count']} 筆"
        try:
            r.approvals.request(ApprovalRequest(
                id=rid, event_id=eid, created_at=r.now(), risk=RiskLevel(pv["risk"]),
                title=f"排程回復 v{pv['from_version']} → v{pv['to_version']}",
                reason=body.reason or "主管一鍵回復上一版排程",
                done=[], gaps=[f"撤回人力：{t}" for t in pv.get("staffing_revoked") or []],
                delivery_impact=impact, options=[]))
            r.approvals.decide(ApprovalDecision(
                request_id=rid, decision=DecisionKind.APPROVE, reason=body.reason or "確認回復（對話框）",
                decided_by=session.actor, via="touchscreen" if session.local else "phone", decided_at=r.now()))
            v = store.rollback(pv["to_version"])
        except KeyError:
            raise _404(f"schedule version {pv['to_version']}") from None
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e)) from None
        r.audit.append(AuditKind.ROLLBACK, session.actor,
                       {"from_version": pv["from_version"], "to_version": v.version, "approval_id": rid,
                        "risk": pv["risk"], "staffing_revoked": pv.get("staffing_revoked") or [],
                        "staffing_restored": pv.get("staffing_restored") or []},
                       event_id=None if eid == "SCHEDULE" else eid)
        try:
            r.after_rollback(pv, v, session.actor, rid)
        except Exception:  # noqa: BLE001 - the rollback is done; a failed notice is logged, not a 500
            log.exception("rollback follow-up")
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

    @app.get("/api/orders/today")
    async def orders_today() -> dict[str, Any]:
        """Today's orders (issue #95), risk first: in production today, due today, progress, machine, eta, why.
        Computed by warroom/tools/orders_today.py from the dashboard's order list, the ACTIVE schedule and the tyres
        in a press now; the same result 問戰情室 answers from. Read only."""
        return await asyncio.to_thread(rt().orders_today)

    @app.get("/api/shifts", response_model=list[Shift])
    async def shifts() -> list[Shift]:
        """Shifts with the active schedule's approved staffing changes applied; `required` is set
        from the solver's own gap rule, so 缺 N 人 disappears once 夜班加班 is approved."""
        r = rt()
        eff = r.effective_shifts()
        short = {sh.id: n for sh, _, n in _safe(lambda: r.staffing_gap_rows(eff), [])}
        return [sh.model_copy(update={"required": sh.headcount + short[sh.id]}) if sh.id in short
                else sh.model_copy(update={"required": min(sh.required, sh.headcount)
                                           if sh.required is not None else None})
                for sh in eff]

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

    # ------------------------------------------------------------------ issue #54: 主 agent and its subagents
    @app.get("/api/subagents")
    async def subagents() -> dict[str, Any]:
        """The main agent's subagents (one per state-machine run) with status and progress, and the model queue."""
        o = rt().orch
        return {"items": o.subagents(), "queue": o.queue.view()}

    @app.post("/api/subagents/{event_id}/interrupt")
    async def interrupt_subagent(event_id: str, session: Session = Depends(supervisor)) -> dict[str, Any]:
        """Stop a subagent safely (no new tool call, waiting cards superseded, open worker tasks cancelled,
        schedule kept, SAFE_STOP 「主管中斷」). Same session rule as approvals."""
        r = rt()
        if r.network == "lan_down" and not session.local:
            raise HTTPException(status_code=409, detail="LAN 中斷：請至現場觸控螢幕操作")
        try:
            return await r.orch.interrupt(event_id, session.actor)
        except KeyError:
            raise _404(event_id) from None
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e)) from None

    @app.get("/api/preclass")
    async def preclass() -> dict[str, Any]:
        """The detector's pre-classification before any LLM: per anomaly the rule, signals and thresholds, the
        validation it passed, the forecast and where the main agent routes it; plus readings rejected as bad data."""
        from warroom.agent.orchestrator import PRIORITY_LABEL, TYPES, route
        r = rt()
        evs = {e.anomaly.id: e for e in r.events()}
        items = []
        for a in reversed(r.anomalies):
            t, p = route(a.kind.value, a.severity)
            ev = evs.get(a.id)
            items.append({**a.model_dump(mode="json"), "route": {"type": t, "type_label": TYPES[t], "priority": p,
                                                                  "priority_label": PRIORITY_LABEL[p]},
                          "event_id": ev.id if ev else None, "event_state": ev.state.value if ev else None})
        h = r.snapshot().header
        det = r.c.detector
        log_ = list(getattr(det, "bad_data_log", []))[-30:]
        bad = [{"ts": ts.isoformat(), "machine_id": mid, "channel": getattr(ch, "value", str(ch)),
                "quality": getattr(q, "value", str(q))} for ts, mid, ch, q in reversed(log_)]
        return {"items": items, "bad_data": [b.model_dump(mode="json") for b in (h.bad_data or [])], "bad_log": bad}

    # ------------------------------------------------------------------ 每日事件紀錄 (issue #37): read-only, from the audit log
    def _daylog():
        from warroom.api import daylog
        r = rt()
        sources = {ev.id: ("command" if ev.source == "supervisor" else ev.source if ev.source == "plant" else "event")
                   for ev in _safe(r.events, [])}
        return daylog, daylog.build(r.audit.tail(0, 1_000_000), sources)

    @app.get("/api/tasklog")
    async def tasklog() -> dict[str, Any]:
        """今天處理過的任務 (CJ 2026-10-22: 「…不可以清掉」): every task from the audit file, by date, loop and scenario.
        Read-only; there is no route that deletes a row."""
        return {"days": await asyncio.to_thread(rt().tasklog.days)}

    @app.get("/api/daylog")
    async def daylog_days() -> dict[str, Any]:
        """Days (or demo loops) with their run counts, newest first."""
        def run() -> dict[str, Any]:
            dl, days = _daylog()
            out = [{"key": d["key"], "label": d["label"], "first": d["first"].isoformat(), "last": d["last"].isoformat(),
                    "last_seq": d["last_seq"], **dl.summary(d)} for d in days.values()]
            return {"days": sorted(out, key=lambda d: d["last_seq"], reverse=True)}   # newest activity first
        return await asyncio.to_thread(run)

    @app.get("/api/daylog/{key}")
    async def daylog_day(key: str) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            dl, days = _daylog()
            if key == "current" and days:
                d = max(days.values(), key=lambda x: x["last_seq"])   # the day of the newest entry (clocks jump)
            elif key in days:
                d = days[key]
            else:
                raise _404(key)
            return {**dl.jsonable(d), "summary": dl.summary(d)}
        return await asyncio.to_thread(run)

    @app.get("/api/daylog/{key}/csv")
    async def daylog_csv(key: str) -> Response:
        def run() -> str:
            dl, days = _daylog()
            if key not in days:
                raise _404(key)
            return dl.to_csv(days[key])
        text = await asyncio.to_thread(run)
        return Response(text, media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="daylog-{key}.csv"'})

    # ------------------------------------------------------------------ 後台 (issue #37): factory network + supervisor
    from warroom.api.admin import install_admin
    from warroom.api.demo import install_demo
    install_admin(app, rt, supervisor)
    demo_state = install_demo(app, rt, demo)
    install_tasks(app, rt, supervisor, on_site, guard, _audit_pin)   # 人員回報 (/worker 開始・完成)

    # ------------------------------------------------------------------ 問戰情室 (read-only Q&A)
    @app.post("/api/ask", response_model=AskAnswer)
    async def ask(body: AskRequest, session: Session = Depends(supervisor)) -> AskAnswer:
        """The supervisor asks a question; the model picks read tools, code runs only ASK_TOOLS and
        logs everything under an ASK-nnnn id. Same session rule as approvals: LAN down → on-site only."""
        r = rt()
        if r.network == "lan_down" and not session.local:
            raise HTTPException(status_code=409, detail="LAN 中斷：請至現場觸控螢幕提問")
        if r.c.tools is None:
            raise HTTPException(status_code=503, detail="tools unavailable (degraded mode)")
        from warroom.agent.orchestrator import PRIORITY
        async with r.orch.queue.slot(PRIORITY["question"], "ask", None, "問戰情室"):   # questions wait last (issue #54)
            return await _ask_agent(r).ask(body.question, tools=r.c.tools, audit=r.audit, context=r.ask_context(),
                                           now=r.clock.now(), actor=session.actor,
                                           conversation_id=body.conversation_id, memory=r.ask_memory)

    # ------------------------------------------------------------------ issue #10: 下新任務 / 討論這個計劃
    def _ask_agent(r: Runtime):
        if "ask" not in holder:
            from warroom.agent.ask import AskAgent
            holder["ask"] = AskAgent(r.settings)
        return holder["ask"]

    def _talk(r: Runtime):
        if "talk" not in holder:
            from warroom.agent.discuss import PlanTalk
            holder["talk"] = PlanTalk(r.settings)
        return holder["talk"]

    def _sm_ready(r: Runtime):
        sm = r.state_machine
        if r.c.tools is None or not callable(getattr(sm, "open_command", None)):
            raise HTTPException(status_code=503, detail="agent unavailable (degraded mode)")
        return sm

    @app.post("/api/command", response_model=CommandReply)
    async def command(body: CommandRequest, session: Session = Depends(supervisor)) -> CommandReply:
        """The supervisor gives the agent a new task in plain Chinese. It opens an Event (source =
        supervisor) that runs PLAN -> SIMULATE -> GATE -> (approval) -> EXECUTE with the same tools,
        gate and audit as any event. Same session rule as approvals."""
        r = rt()
        if r.network == "lan_down" and not session.local:
            raise HTTPException(status_code=409, detail="LAN 中斷：請至現場觸控螢幕下指令")
        _sm_ready(r)
        from warroom.agent.ask import smalltalk_reply
        chat = smalltalk_reply(body.text)
        if chat is not None:       # a greeting is not a task: no event, no tool; still audited
            r.audit.append(AuditKind.SYSTEM, session.actor, {"command": body.text, "intent": "chitchat",
                                                             "reply": chat, "event": None})
            return CommandReply(event_id="", accepted=False, reply=chat)
        # one supervisor-command event, no dispatch to a typed subagent and no model call to pick one (CJ 2026-10-21:
        # 「我只需要一個幫忙分類的，現在先不要派發」); the same path as Agent Nemo's propose_plan
        ev = r.start_command(body.text.strip(), session.actor)
        from warroom.agent.talk import physical_request
        refusal = physical_request(body.text)
        return CommandReply(event_id=ev.id, accepted=refusal is None,
                            reply=refusal or "已開新任務：計劃擬好會送到計劃區給你核准，核准後才執行。")

    @app.post("/api/events/{event_id}/discuss", response_model=DiscussReply)
    async def discuss(event_id: str, body: DiscussRequest, session: Session = Depends(supervisor)) -> DiscussReply:
        """The supervisor talks with the agent about this event's waiting plan: a question is answered
        from read tools (plan unchanged); a change becomes a code-enforced constraint, the card is
        superseded and a new version is issued. Same session rule as approvals."""
        r = rt()
        if r.network == "lan_down" and not session.local:
            raise HTTPException(status_code=409, detail="LAN 中斷：請至現場觸控螢幕討論計劃")
        sm = _sm_ready(r)
        try:
            sm.get(event_id)
        except KeyError:
            raise _404(event_id) from None
        out = await _talk(r).discuss(sm=sm, event_id=event_id, text=body.text.strip(), actor=session.actor,
                                     ask_agent=_ask_agent(r), tools=r.c.tools, audit=r.audit,
                                     context=r.ask_context(), now=r.clock.now())
        _safe(r.sync_warning_light, False)
        return out

    # ------------------------------------------------------------------ issue #54: one input, one 主 agent
    def _main_agent(r: Runtime):
        if "main" not in holder:
            from warroom.agent.main_agent import MainAgent
            holder["main"] = MainAgent(r.settings, audit=r.audit)
            r.memory_resets.append(holder["main"].clear)     # scenario load / reset / day-loop wrap
        return holder["main"]

    @app.post("/api/main/message")
    async def main_message(body: MainMessage, session: Session = Depends(supervisor)) -> dict[str, Any]:
        """The supervisor talks to the Agent Nemo: a bounded tool-calling loop (warroom/agent/main_agent.py) that can
        chat, answer general questions, read (knowledge base, sensors, orders, query_plant over the rest of the plant),
        propose a plan, or discuss the selected waiting plan. It has no write tool; a proposed plan still needs the
        supervisor's approval. Same session rule as approvals."""
        r = rt()
        if r.network == "lan_down" and not session.local:
            raise HTTPException(status_code=409, detail="LAN 中斷：請至現場觸控螢幕操作")
        text = body.text.strip()
        sm = r.state_machine
        waiting = None
        if body.event_id:
            card = getattr(sm, "waiting_card", lambda _e: None)(body.event_id)
            if card is not None:
                # the card as the supervisor sees it: title, what it does and the A／B staffing options (the title alone
                # let the model invent changes: 「調低到 118°C、縮短硫化時間」, real run 2026-10-21)
                opts = "；".join(f"{o.id} {o.label}{'（系統建議）' if o.recommended else ''}" for o in card.options)
                waiting = (f"{card.title}（v{card.version}）。要做的事：{'；'.join(card.actions) or '無'}"
                           + (f"。人力方案：{opts}" if opts else ""))
        from warroom.agent.ask import AskAgent
        from warroom.agent.orchestrator import ACTOR, PRIORITY
        from warroom.contracts import ToolName, ToolResult

        async def run_tool(name: str, args: dict[str, Any], call_id: str) -> ToolResult:
            if r.c.tools is None:
                return ToolResult(call_id=call_id, tool=ToolName(name), ok=False, error="tools unavailable")
            eid, step = call_id.split(":")[0], call_id.split(":")[-1]
            return await AskAgent._run(r.c.tools, r.audit, eid, int(step[1:] or 1), ToolName(name), args)

        async def propose(task: str) -> dict[str, Any]:
            # the 下新任務 path: one supervisor-command event (PLAN → GATE → approval), no subagent dispatch
            ev = r.start_command(task, session.actor)
            return {"event_id": ev.id}

        async def talk(t: str) -> dict[str, Any]:
            out = await discuss(body.event_id, DiscussRequest(text=t), session)
            return out.model_dump(mode="json")

        def slot(low: bool = False):
            # small talk waits behind everything (an incident plan always goes first); plant questions keep "task"
            return r.orch.queue.slot(PRIORITY["smalltalk" if low else "task"], "main_agent", body.event_id,
                                     "Agent Nemo 閒聊" if low else "Agent Nemo 對話")

        async def read_plant(topic: str, call_id: str, order_id: str | None = None) -> dict[str, Any]:
            # query_plant (warroom/api/plant_query.py): not a ToolName, so not through the Registry (its allow-list
            # refuses it); audited here like an ask read (tool_call / tool_result, R0), read-only by construction.
            # Called on the loop, not in a thread: it walks live runtime state the loop also changes
            eid, step = call_id.split(":")[0], call_id.split(":")[-1]
            row = {"call_id": f"{eid}:Q{step[1:]}", "step_id": f"Q{step[1:]}", "attempt": 1, "risk": "R0"}
            r.audit.append(AuditKind.TOOL_CALL, "tool:query_plant", {**row, "args": {"topic": topic, **({"order_id": order_id} if order_id else {})},
                                                                 "state": "ASK"},
                           event_id=eid)
            t0 = time.perf_counter()
            try:
                data, err = plant_query.query(r, topic, order_id), None
            except Exception as e:  # noqa: BLE001 - a failed read is said in the reply, never a 500
                data, err = None, f"{type(e).__name__}: {e}"[:200]
            r.audit.append(AuditKind.TOOL_RESULT, "tool:query_plant",
                           {**row, "ok": err is None, "error": err, "duration_ms": round((time.perf_counter() - t0) * 1000, 2),
                            "data": {"topic": topic, "keys": sorted(data) if isinstance(data, dict) else []}}, event_id=eid)
            return {"ok": err is None, "data": data, "error": err}

        class Control:
            """Task control from chat (CJ 2026-10-22: 「A」): only existing supervisor actions, on an event that exists,
            run after the supervisor confirmed in chat, audited as the supervisor via Agent Nemo. Approval and a Safety
            Core release are not here: they stay buttons."""
            actor = f"{session.actor} via Agent Nemo"

            @staticmethod
            def _label(e) -> str:
                from warroom.agent.narrate import machine_label
                return f"「{(e.command or '')[:16]}」那件" if e.source == "supervisor" else f"{machine_label(e.machine_id)}那件"

            @staticmethod
            def candidates() -> list[tuple[str, str]]:
                """Tasks a bare command (「重新排」「結案」) could mean: open ones, and SAFE_STOPs nobody has followed up."""
                evs = [e for e in r.events() if e.closed_at is None
                       or (e.state.value == "safe_stop" and (e.follow_up is None or e.follow_up.status == "manual"))]
                return [(e.id, Control._label(e)) for e in sorted(evs, key=lambda e: e.opened_at)]

            @staticmethod
            def resolve(ref: str) -> tuple[str, str] | None:
                from warroom.agent.ask import machines_in, orders_in
                evs = list(r.events())
                ref = (ref or "").strip()
                names = bool(re.search(r"EV-\d", ref) or machines_in(ref) or orders_in(ref))
                if not names:
                    # 「重新排」: the selected task, else the one obvious task (CJ 10-22)
                    sel = [e for e in evs if e.id == body.event_id]
                    if sel:
                        return sel[0].id, Control._label(sel[0])
                    c = Control.candidates()
                    return c[0] if len(c) == 1 else None
                hit = [e for e in evs if e.id == ref or e.id in ref]
                if not hit:
                    mids, oids = machines_in(ref), orders_in(ref)

                    def named(e) -> bool:
                        return bool((mids and e.machine_id in mids) or (oids and any(o in (e.command or "") for o in oids)))
                    # an open task first; else the latest one that ended (「3 號機那件重新規劃」 after it was interrupted)
                    hit = [e for e in evs if e.closed_at is None and named(e)]
                    if not hit:
                        ended = sorted((e for e in evs if named(e)), key=lambda e: e.opened_at)
                        hit = ended[-1:]
                if len(hit) != 1:
                    return None
                return hit[0].id, Control._label(hit[0])

            @staticmethod
            def blocked(action: str, eid: str) -> str | None:
                """Why this action cannot run on this task now (asked before the confirmation, never after)."""
                if action == "release_safety_stop":
                    from warroom.agent.narrate import machine_label
                    from warroom.contracts import SafetyRelease
                    c = Control._case(eid)
                    if c is not None:
                        try:              # the button's own precondition check: ① field check, ② safe samples, decision
                            r.cases.check_release(c, SafetyRelease(decision=c.decisions[0]), r._board())
                        except ValueError as e:
                            return f"{machine_label(eid)}還不能恢復：{e}。"
                        return None
                    tile = Control._tile(eid)
                    if tile is not None and tile.status.value == "out_of_service":
                        return None if tile.repaired else f"{machine_label(eid)}還沒檢修完成，不能恢復運轉。"
                    return f"{machine_label(eid)}現在沒有安全核心停機，也不是停用狀態，不用恢復。"
                st = sm.get(eid).state.value
                if action == "interrupt_task" and st in ("closed", "safe_stop"):
                    return "這件已經結束了，不用中斷。"
                if action == "replan_task" and st == "await_approval":
                    return ("這件在等你核准，沒有「直接重新規劃」的現成做法。要換方案，直接說條件（例如「不要加班」「改選 A」），"
                            "或先中斷它再重開一件。")
                if action == "replan_task" and st not in ("closed", "safe_stop"):
                    return "這件正在處理中，等它有計劃或結果再重新規劃，或先中斷它。"
                fu = getattr(sm.get(eid), "follow_up", None)
                if action == "hand_to_field" and st != "safe_stop":
                    return "「交給現場」只用在安全停止的任務。"
                if action == "hand_to_field" and fu is not None:
                    return "這件已經交給現場處理了。"
                if action == "close_task" and st != "safe_stop":
                    return "「確認結案」只用在安全停止的任務；其他任務驗證通過會自己結案。"
                if action == "close_task" and fu is not None and fu.status in ("closed", "replanned"):
                    return "這件已經處理過了（已結案或已重新規劃）。"
                if action == "replan_task" and st == "safe_stop" and fu is not None and fu.status in ("closed", "replanned"):
                    return "這件已經處理過了（已結案或已重新規劃）。"
                return None

            # ---- a machine's way back (CJ 2026-10-22: 「開放」): the SAME paths as the ③ 確認恢復運轉 button
            # (warroom/api/resolve.py, every precondition) and the 恢復運轉 button (Runtime.return_to_service)
            @staticmethod
            def _case(mid: str):
                """The open Safety Core case to release — none for an OUT_OF_SERVICE machine: its way back is 恢復運轉
                (return_to_service), and the core's release never lifts OUT_OF_SERVICE (press 7 after H2 has both,
                and the chat asked for the core's field check instead; live run 10-22)."""
                tile = Control._tile(mid)
                if tile is not None and tile.status.value == "out_of_service":
                    return None
                return r.cases.open_for(mid)

            @staticmethod
            def _tile(mid: str):
                return next((t for t in r.machine_tiles() if t.machine_id == mid), None)

            @staticmethod
            def resolve_for(action: str, ref: str) -> tuple[str, str] | None:
                if action != "release_safety_stop":
                    return Control.resolve(ref)
                from warroom.agent.ask import machines_in
                from warroom.agent.narrate import machine_label
                mids = machines_in(ref or "")
                if not mids and body.event_id:
                    ev = _safe(lambda: sm.get(body.event_id), None)
                    mids = [ev.machine_id] if ev is not None and ev.machine_id in F.MACHINE_BY_ID else []
                if not mids:
                    stopped = [c.machine_id for c in r.cases.view(r._board()) if c.status == "open"]
                    stopped += [t.machine_id for t in r.machine_tiles() if t.status.value == "out_of_service"
                                and t.machine_id not in stopped]
                    mids = stopped if len(stopped) == 1 else []
                return (mids[0], machine_label(mids[0])) if len(mids) == 1 else None

            @staticmethod
            def question(action: str, target: str, label: str) -> str | None:
                if action != "release_safety_stop":
                    return None
                c = Control._case(target)
                if c is not None and "qc_pass" in c.decisions:
                    return (f"{label}是低溫停機，要 QC 合格放行嗎？現場檢查和讀值都過了。回「好」就放行恢復運轉，"
                            "要報廢這一模請說「報廢」，其他回覆就不做。")
                if c is not None:
                    return f"要恢復{label}運轉嗎？現場檢查和讀值都過了。回「好」或「確認」就恢復，其他回覆就不做。"
                return f"要讓{label}恢復運轉嗎？檢修已經完成。回「好」或「確認」就恢復，其他回覆就不做。"

            def audit_request(self, action: str, eid: str) -> None:
                if action == "release_safety_stop":   # a machine, not an event: the row names it, under the case's event
                    c = Control._case(eid)
                    r.audit.append(AuditKind.SYSTEM, self.actor, {"msg": "chat control asked", "action": action,
                                                                  "confirmed": False, "machine_id": eid},
                                   event_id=c.event_id if c is not None else None)
                    return
                r.audit.append(AuditKind.SYSTEM, self.actor, {"msg": "chat control asked", "action": action,
                                                              "confirmed": False}, event_id=eid)

            async def run(self, action: str, eid: str, words: str = "") -> str:
                if action == "release_safety_stop":
                    return self._release(eid, words)
                ev = sm.get(eid)
                state = ev.state.value
                try:
                    if action == "interrupt_task":
                        d = await r.orch.interrupt(eid, self.actor)
                        result = str(d.get("text") or "已中斷")
                    elif action == "replan_task":
                        if state == "safe_stop":                         # #105: SAFE_STOP → 請 AI 重新規劃
                            nev = r.stop_replan(eid, self.actor)
                            result = f"已請 AI 重新規劃（新的一件 {nev.id}），計劃擬好會送你核准。"
                        elif state in ("closed", "safe_stop"):
                            what = ev.command or (ev.alert_card.title if ev.alert_card else ev.anomaly.message)
                            nev = r.start_command(f"重新規劃：{what}", self.actor)
                            result = f"已重新開一件來規劃（{nev.id}），計劃擬好會送你核准。"
                        elif state == "await_approval":
                            # no existing path re-plans a waiting card without a condition (discuss needs one): say so
                            result = ("這件在等你核准，沒有「直接重新規劃」的現成做法。要換方案，直接說條件（例如「不要加班」「改選 A」），"
                                      "或先中斷它再重開一件。這次沒有執行。")
                        else:
                            result = "這件正在處理中，等它有計劃或結果再重新規劃，或先中斷它。"
                    elif action == "hand_to_field":                      # #105: SAFE_STOP → 交給現場處理
                        e2 = r.stop_handoff(eid, self.actor)
                        n = len(e2.follow_up.task_ids) if e2.follow_up else 0
                        result = f"已交給現場處理：依 SOP 派了 {n} 項任務，這件顯示人工處理中；做完後說「結案」。"
                    else:                                                # #105: SAFE_STOP → 確認結案
                        r.stop_close(eid, self.actor)
                        result = "已確認結案，這件安全停止處理完了。"
                except (KeyError, ValueError) as e:
                    result = f"沒有執行：{e}"
                r.audit.append(AuditKind.SYSTEM, self.actor, {"msg": "chat control", "action": action, "confirmed": True,
                                                              "state_before": state, "result": result[:200]}, event_id=eid)
                return result

        def _release(self, mid: str, words: str) -> str:
            """After the supervisor's 好 in chat: the ③ release (all its preconditions again) or the return to service."""
            from warroom.agent.narrate import machine_label
            from warroom.contracts import SafetyRelease
            c = Control._case(mid)
            try:
                if c is not None:
                    d = "scrap" if ("報廢" in words and "scrap" in c.decisions) else c.decisions[0]
                    r.safety_release(c.id, SafetyRelease(decision=d, reason="主管在對話中確認（Agent Nemo）"), self.actor)
                    result = (f"已報廢{machine_label(mid)}這一模，換模後再入模。" if d == "scrap"
                              else f"已恢復{machine_label(mid)}運轉（{'QC 合格放行' if d == 'qc_pass' else '確認恢復運轉'}）。")
                else:
                    r.return_to_service(mid, self.actor)
                    result = f"已讓{machine_label(mid)}恢復運轉（檢修完成）。"
            except (KeyError, ValueError) as e:
                result = f"沒有恢復：{e}"
            r.audit.append(AuditKind.SYSTEM, self.actor, {"msg": "chat control", "action": "release_safety_stop",
                                                          "confirmed": True, "machine_id": mid, "result": result[:200]},
                           event_id=(c.event_id if c is not None else None))
            return result

        Control._release = _release

        def waiting_of(eid: str) -> bool:
            return getattr(sm, "waiting_card", lambda _e: None)(eid) is not None

        # safety and sensitive terms, by code on both sides of the model (CJ 2026-10-21: 「一些安全跟敏感詞彙還是要用程式擋下來」)
        from warroom.agent import safety_filter
        sf = safety_filter.default()
        hit = sf.check_input(text)
        if hit is not None:
            r.audit.append(AuditKind.SYSTEM, "safety_filter", {**hit.audit(), "turn": None}, event_id=None)
            return {"id": None, "reply": hit.reply, "steps": [], "event_id": None, "by": "safety_filter",
                    "fell_back": False, "error": None, "intent": f"blocked:{hit.category}", "ms": 0.0}
        from warroom.api import plant_query
        # 今日工廠概況 rides along with the read-only context (next shipment, headcount, progress, open events)
        context = {**r.ask_context(), "today": _safe(lambda: plant_query.overview(r), None)}
        if body.event_id:
            # the task picked on the Agent page (or the 主控台): the chat talks about it (CJ 2026-10-22: 「A」)
            ev_sel = _safe(lambda: sm.get(body.event_id), None)
            if ev_sel is not None:
                context["selected_task"] = {"id": ev_sel.id, "state": ev_sel.state.value, "machine": ev_sel.machine_id,
                                            "what": (ev_sel.command or (ev_sel.alert_card.title if ev_sel.alert_card
                                                                        else ev_sel.anomaly.message))[:60]}
        res = await _main_agent(r).turn(text, context=context, waiting_plan=waiting, run_tool=run_tool,
                                        propose=propose, discuss=talk if waiting else None, slot=slot,
                                        read_plant=read_plant, conversation=f"{session.actor}|{body.conversation_id or ''}",
                                        control=Control())
        key = getattr(r.audit, "_key", None)
        out_hit = sf.check_output(res["reply"], [access.supervisor_pin or "", key.hex() if isinstance(key, bytes) else ""])
        if out_hit is not None:
            r.audit.append(AuditKind.SYSTEM, "safety_filter", {**out_hit.audit(), "turn": res["id"]}, event_id=None)
            res.update(reply=out_hit.reply, error=f"output blocked: {out_hit.category}")
        r.audit.append(AuditKind.SYSTEM, ACTOR, {"orch": "chat", "text": text, "reply": res["reply"], "by": res["by"],
                                                  "fell_back": res["fell_back"], "error": res["error"],
                                                  "steps": [{k: v for k, v in s.items() if k != "args"} for s in res["steps"]],
                                                  "dispatched": res["event_id"], "selected": body.event_id, "turn": res["id"]})
        return res

    # ------------------------------------------------------------------ QA 模式: judges' framework questions (read-only)
    def _qa_agent(r: Runtime):
        if "qa" not in holder:
            from warroom.agent.qa import QaAgent
            from warroom.kb.framework import FrameworkKB
            holder["qa"] = QaAgent(r.settings, kb=FrameworkKB.from_settings(r.settings))
        return holder["qa"]

    @app.post("/api/qa/ask")
    async def qa_ask(body: QaAsk, session: Session = Depends(supervisor)) -> dict[str, Any]:
        """QA 模式 (warroom/agent/qa.py): a short spoken answer to a judge's question about the agent framework, only
        from the framework knowledge base (its own collection, not the plant SOPs), with evidence ids. No tool, no event,
        no write; the model call waits in the ModelQueue at the plant-question priority, behind incidents."""
        r = rt()
        from warroom.agent.orchestrator import PRIORITY
        key = getattr(r.audit, "_key", None)
        return await _qa_agent(r).ask(
            body.question, conversation_id=body.conversation_id, audit=r.audit,
            slot=lambda: r.orch.queue.slot(PRIORITY["task"], "qa", None, "QA 模式"),
            secrets=[access.supervisor_pin or "", key.hex() if isinstance(key, bytes) else ""])

    @app.get("/api/qa/doc")
    async def qa_doc(path: str = Query(min_length=1, max_length=200)) -> dict[str, Any]:
        """The evidence viewer: the text of one file the framework KB indexes or a curated answer cites. Any other
        path is a 404 (no file browsing)."""
        from warroom.agent.qa import viewable
        text = await asyncio.to_thread(viewable, path, _qa_agent(rt())._kb())
        if text is None:
            raise _404(path)
        return {"path": path, "text": text}

    # QA 模式 live proofs + 「看一次怎麼運作」 (warroom/api/qa_demo.py): demo-control rights, everything tagged 「QA 展示」
    from warroom.api.qa_demo import CHAT_CONVERSATION, install_qa_demo

    async def _qa_chat(text: str, session: Session) -> dict[str, Any]:
        return await main_message(MainMessage(text=text, conversation_id=CHAT_CONVERSATION), session)

    async def _qa_plant_ask(question: str, session: Session) -> Any:
        return await ask(AskRequest(question=question), session)

    def _qa_network(network: str, session: Session) -> None:
        r = rt()
        r.network = network
        r.audit.append(AuditKind.SYSTEM, session.actor, {"msg": "network tier", "network": network, "qa_demo": True,
                                                         "tag": "QA 展示"})
        r.sync_warning_light()
        r.bus.publish(WsType.SNAPSHOT, r.snapshot())

    install_qa_demo(app, rt, supervisor, demo, chat=_qa_chat, ask=_qa_plant_ask, set_network=_qa_network)

    # ------------------------------------------------------------------ sim control (demo)
    @app.post("/api/sim/control")
    async def sim_control(ctl: SimControl, _: Session = Depends(demo)) -> dict[str, Any]:
        r = rt()
        overlay: dict[str, Any] | None = None
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
        elif ctl.action == "jump":
            if ctl.at is None:
                raise HTTPException(status_code=422, detail="at required")
            try:
                await r.jump_to(ctl.at)
            except RuntimeError as e:
                raise HTTPException(status_code=409, detail=str(e)) from None
        elif ctl.action == "overlay":
            # 情境疊加: add an overlayable scenario's faults / plant inputs from now, keeping everything running
            if ctl.scenario is None:
                raise HTTPException(status_code=422, detail="scenario required")
            try:
                overlay = await r.overlay_scenario(ctl.scenario)
            except ValueError as e:       # a story scenario: the caller loads it instead
                raise HTTPException(status_code=409, detail=str(e)) from None
        elif ctl.action in ("reset", "scenario"):
            if ctl.action == "scenario" and ctl.scenario is None:
                raise HTTPException(status_code=422, detail="scenario required")
            try:
                await (r.reset() if ctl.action == "reset" else r.load_scenario(ctl.scenario))
            except Exception as e:  # noqa: BLE001 - the runtime kept the previous scenario intact
                raise HTTPException(status_code=503, detail={
                    "message": f"情境切換失敗，維持原情境 {r.scenario.value}：{type(e).__name__}: {e}"[:400],
                    "scenario": r.scenario.value}) from None
            if "main" in holder:
                # a new scenario starts a new conversation: 「你好」 got 「您剛才說『降到 120』…」 from the last rehearsal
                holder["main"].clear()
        if ctl.preroll and ctl.action in ("reset", "scenario"):
            await r.preroll()
        if ctl.speed is not None and ctl.action != "speed":
            r.set_speed(ctl.speed)
        r.bus.publish(WsType.CLOCK, r.clock_info())
        return {"ok": True, **r.clock_info(), **({"overlay": overlay} if overlay is not None else {})}

    @app.get("/api/sim/scenarios")
    async def sim_scenarios() -> list[dict[str, Any]]:
        try:
            from warroom.sim import list_scenarios
            return [s.model_dump(mode="json") for s in list_scenarios()]
        except Exception:  # noqa: BLE001 - sim module still a stub
            from warroom.api.fallbacks import fallback_scenario
            return [fallback_scenario(n).model_dump(mode="json") for n in ScenarioName]

    @app.post("/api/sim/step")
    async def sim_step(body: StepBody, _: Session = Depends(demo)) -> dict[str, Any]:
        r = rt()
        await r.run_seconds(body.seconds)
        return {"ok": True, **r.clock_info()}

    @app.post("/api/sim/fault")
    async def sim_fault(body: FaultBody, _: Session = Depends(demo)) -> dict[str, Any]:
        r = rt()
        if body.machine_id not in F.MACHINE_BY_ID:
            raise _404(body.machine_id)
        fid = body.id or f"F-API-{r.now():%H%M%S}-{body.machine_id}"
        spec = FaultSpec(id=fid, machine_id=body.machine_id, kind=body.kind, start_offset_s=body.start_offset_s,
                         duration_s=body.duration_s, params=body.params)
        r.inject_fault(spec)
        return {"ok": True, "fault": spec.model_dump(mode="json")}

    # issue #36 模擬事件: random / chosen plant events through the normal detector -> state machine -> gate path
    @app.get("/api/sim/events")
    async def sim_events() -> dict[str, Any]:
        return rt().sim_events.view()

    @app.post("/api/sim/events")
    async def sim_events_set(body: SimEventsBody, _: Session = Depends(demo)) -> dict[str, Any]:
        r = rt()
        r.sim_events.configure(body.enabled, body.every_min, body.seed)
        r.audit.append(AuditKind.SYSTEM, "supervisor", {"msg": "模擬事件設定", **body.model_dump(exclude_none=True)})
        r.bus.publish(WsType.CLOCK, r.clock_info())
        return r.sim_events.view()

    @app.post("/api/sim/events/fire")
    async def sim_events_fire(body: FireEventBody, _: Session = Depends(demo)) -> dict[str, Any]:
        from warroom.sim.events import CATALOG_BY_KIND
        if body.kind not in CATALOG_BY_KIND:
            raise _404(body.kind)
        r = rt()
        try:
            fired = r.fire_sim_event(body.kind, source="manual")
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e)) from None
        r.bus.publish(WsType.CLOCK, r.clock_info())
        return {"ok": True, "fired": fired}

    @app.post("/api/sim/clear")
    async def sim_clear(body: ClearBody | None = Body(default=None), _: Session = Depends(demo)) -> dict[str, Any]:
        mid = body.machine_id if body else None
        if mid and mid not in F.MACHINE_BY_ID:
            raise _404(mid)
        rt().clear_faults(mid)
        return {"ok": True}

    @app.post("/api/sim/network")
    async def sim_network(body: NetworkBody, session: Session = Depends(demo)) -> dict[str, Any]:
        """Offline-tier demo (page 9). weak (弱網): the agent is unaffected; only pushes to the phone
        and remote clients are delayed and batched. online / wan_down: the agent runs locally and
        the phone approves over the factory LAN. lan_down: phone and remote approvals are refused (409), the
        local touch screen decides, and the warning light goes on while anything needs a person."""
        r = rt()
        r.network = body.network
        r.audit.append(AuditKind.SYSTEM, session.actor, {"msg": "network tier", "network": body.network})
        light = r.sync_warning_light()
        r.bus.publish(WsType.SNAPSHOT, r.snapshot())
        return {"ok": True, "network": r.network, "warning_light": light,
                "phone_approvals": body.network != "lan_down",
                "push_delay_s": weak_push_delay_s if body.network == "weak" else 0.0}

    # ------------------------------------------------------------------ websocket
    @app.websocket("/ws")
    async def ws(sock: WebSocket) -> None:
        await sock.accept()
        r = rt()
        local = on_site(sock)
        loop = asyncio.get_running_loop()
        buf = WeakLinkBuffer(weak_push_delay_s)
        sub = r.bus.subscribe()
        nxt = asyncio.ensure_future(sub.__anext__())
        await asyncio.sleep(0)               # registers the subscriber queue before the snapshot
        drain = asyncio.create_task(_drain(sock))

        async def send_held(msgs: list[WsMessage]) -> None:
            for m in msgs:
                await sock.send_text(m.model_dump_json())
            weak_stats["held"] += buf.held_total
            weak_stats["coalesced"] += buf.coalesced
            weak_stats["batches"] += 1 if msgs else 0
            buf.held_total = buf.coalesced = 0

        try:
            await _send(sock, WsType.SNAPSHOT, r.snapshot().model_dump(mode="json"), r.now())
            await _send(sock, WsType.CLOCK, r.clock_info(), r.now())
            if demo_state.running:            # a screen opened mid-demo joins the current cue
                await _send(sock, WsType.DEMO, demo_state.view(), r.now())
            while True:
                done, _ = await asyncio.wait({nxt, drain}, timeout=buf.wait_s(loop.time()),
                                             return_when=asyncio.FIRST_COMPLETED)
                if drain in done:
                    break
                if nxt in done:
                    msg: WsMessage = nxt.result()
                    nxt = asyncio.ensure_future(sub.__anext__())
                    if not local and r.network == "weak":
                        buf.push(msg, loop.time())      # page 9 弱網: push delayed, agent unaffected
                    else:
                        if len(buf):
                            await send_held(buf.flush())   # link recovered: held batch first, in order
                        await sock.send_text(msg.model_dump_json())
                if len(buf) and (r.network != "weak" or local):
                    await send_held(buf.flush())
                else:
                    held = buf.due(loop.time())
                    if held:
                        await send_held(held)
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
<h1>實心胎智慧製造戰情室</h1><p>web/dist not built. Run <code>cd web && pnpm install && pnpm build</code>.
API is live: <a style="color:#76b900" href="/api/snapshot">/api/snapshot</a> · <a style="color:#76b900" href="/docs">/docs</a></p>"""


def _mount_web(app: FastAPI, dist: Path = DIST) -> None:
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    # the 3D whiteboard's pitch deck (web/public/pitch/README.md): served straight from the source folder, so CJ's
    # slides show without a rebuild; the built copy is the fallback
    pitch = next((d for d in (ROOT / "web" / "public" / "pitch", dist / "pitch") if d.is_dir()), None)
    if pitch is not None:
        app.mount("/pitch", StaticFiles(directory=pitch), name="pitch")

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

    @app.get("/factory", include_in_schema=False)
    @app.get("/factory.html", include_in_schema=False)
    async def factory():
        return page("factory.html")

    @app.get("/worker", include_in_schema=False)
    @app.get("/worker.html", include_in_schema=False)
    async def worker():                     # issue #46: floor worker's tasks (read-only; reads /api/world + /ws)
        return page("worker.html")

    @app.get("/handover", include_in_schema=False)
    @app.get("/handover.html", include_in_schema=False)
    async def handover():                   # issue #49: shift handover (read-only; built in the page from the APIs)
        return page("handover.html")

    @app.get("/report", include_in_schema=False)
    @app.get("/report.html", include_in_schema=False)
    async def report():                     # issue #59: incident report (read-only; built in the page from the APIs)
        return page("report.html")

    @app.get("/favicon.svg", include_in_schema=False)
    async def favicon():
        p = dist / "favicon.svg"
        if not p.is_file():
            raise _404("favicon.svg")
        return FileResponse(p)
