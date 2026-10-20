"""FastAPI app: REST per contracts.REST_ENDPOINTS (+ a few demo-control extras), one WebSocket
/ws relaying the InProcessBus, and the built web/ dist (dashboard at /, phone at /phone, 3D factory at /factory).

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
    RiskLevel,
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
        await r.start()
        try:
            yield
        finally:
            await r.stop()

    app = FastAPI(title="實心胎智慧製造戰情室 API", version="0.1.0", lifespan=lifespan)
    if runtime is not None:
        app.state.runtime = runtime
    app.state.access = access

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
        return HealthStatus(ok=True, llm_mode=r.llm_mode, model=r.settings.model,
                            llm_reachable=await r.llm_reachable(), qdrant_ok=r.kb_ok(),
                            sim_running=r.running and not r.paused, factory_time=r.now(), uptime_s=r.uptime_s())

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
        return await _ask_agent(r).ask(body.question, tools=r.c.tools, audit=r.audit, context=r.ask_context(),
                                       now=r.clock.now(), actor=session.actor)

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
        ev = r.start_command(body.text.strip(), session.actor)
        from warroom.agent.talk import physical_request
        refusal = physical_request(body.text)
        return CommandReply(event_id=ev.id, accepted=refusal is None,
                            reply=refusal or f"收到，已開任務事件 {ev.id}，AI 指揮官規劃中；需要核准的動作會送出核准卡。")

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

    # ------------------------------------------------------------------ sim control (demo)
    @app.post("/api/sim/control")
    async def sim_control(ctl: SimControl, _: Session = Depends(demo)) -> dict[str, Any]:
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
        elif ctl.action in ("reset", "scenario"):
            if ctl.action == "scenario" and ctl.scenario is None:
                raise HTTPException(status_code=422, detail="scenario required")
            try:
                await (r.reset() if ctl.action == "reset" else r.load_scenario(ctl.scenario))
            except Exception as e:  # noqa: BLE001 - the runtime kept the previous scenario intact
                raise HTTPException(status_code=503, detail={
                    "message": f"情境切換失敗，維持原情境 {r.scenario.value}：{type(e).__name__}: {e}"[:400],
                    "scenario": r.scenario.value}) from None
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

    @app.get("/favicon.svg", include_in_schema=False)
    async def favicon():
        p = dist / "favicon.svg"
        if not p.is_file():
            raise _404("favicon.svg")
        return FileResponse(p)
