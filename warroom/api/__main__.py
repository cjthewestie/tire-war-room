"""CLI entry: `uv run python -m warroom.api [serve|demo|check] [options]`.

  serve  start the whole system (sim loop + detection + safety + agent + REST/WS + web) on uvicorn
  demo   run a scenario headless for N sim seconds and print the timeline (no server)
  check  build the runtime once and print which components are real vs fallback

Env (warroom.config.Settings): LLM_MODE, BASE_URL, MODEL, NVIDIA_API_KEY, HOST, PORT, SIM_SPEED, SCENARIO,
WARROOM_REPLAY (team_ev1 | team_ev2 | team_both: team dataset replay in the normal scenario).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys


def _settings(args):
    from warroom.config import Settings
    over = {}
    if getattr(args, "scenario", None):
        over["scenario"] = args.scenario
    if getattr(args, "speed", None):
        over["sim_speed"] = args.speed
    if getattr(args, "host", None):
        over["host"] = args.host
    if getattr(args, "port", None):
        over["port"] = args.port
    if getattr(args, "llm_mode", None):
        over["llm_mode"] = args.llm_mode
    if getattr(args, "replay", None):
        over["replay"] = args.replay
    return Settings(**over)


def cmd_serve(args) -> int:
    import uvicorn

    from warroom.api.app import create_app
    from warroom.api.runtime import Runtime
    s = _settings(args)
    app = create_app(Runtime(s, allow_fallback=not args.strict))
    # TLS on the factory LAN (the supervisor PIN header must not be sniffable): point these at a
    # cert/key pair, e.g. one minted for the Spark's LAN address and trusted on the supervisor phone.
    cert, key = os.environ.get("WARROOM_SSL_CERTFILE"), os.environ.get("WARROOM_SSL_KEYFILE")
    tls = {"ssl_certfile": cert, "ssl_keyfile": key} if cert and key else {}
    scheme = "https" if tls else "http"
    print(f"戰情室 on {scheme}://{s.host}:{s.port}  (dashboard /, phone /phone, API /docs)  "
          f"scenario={s.scenario} speed={s.sim_speed} llm={s.llm_mode.value}", flush=True)
    uvicorn.run(app, host=s.host, port=s.port, log_level=args.log_level, ws="websockets", **tls)
    return 0


# --auto-approve takes the recommended option, except where the proposal scripts the supervisor's
# choice: page 11 round 2, the supervisor vetoes the cross-team transfer and picks A 夜班加班.
DEMO_SUPERVISOR_PICKS: dict[str, dict[int, tuple[str, str]]] = {
    "press7_temp_drop": {2: ("A", "否決跨組調人：影響 2 線，改夜班加班")},
}


async def _demo(args) -> int:
    from warroom.api.runtime import Runtime
    from warroom.contracts import ApprovalDecision, DecisionKind
    s = _settings(args)
    rt = Runtime(s, allow_fallback=not args.strict, run_loop=False)
    await rt.start()
    try:
        if rt.degraded:
            print("degraded components:", json.dumps(rt.degraded, ensure_ascii=False, indent=1))
        for sec in range(int(args.seconds)):
            await rt.tick(1.0, publish=False)
            if args.auto_approve:
                for req in rt.approvals.pending():
                    opt = next((o for o in req.options if o.recommended), req.options[0] if req.options else None)
                    reason = "demo auto-approve"
                    pick = DEMO_SUPERVISOR_PICKS.get(str(s.scenario), {}).get(req.round)
                    if pick and any(o.id == pick[0] for o in req.options):
                        opt = next(o for o in req.options if o.id == pick[0])
                        reason = pick[1]
                    rt.approvals.decide(ApprovalDecision(request_id=req.id, decision=DecisionKind.APPROVE,
                                                         option_id=opt.id if opt else None, reason=reason,
                                                         via="auto", decided_at=rt.now()))
            if sec % 60 == 0:
                await asyncio.sleep(0.01)
        await rt.wait_events(5)
        snap = rt.snapshot()
        h = snap.header
        print(f"factory time {h.factory_time:%Y-%m-%d %H:%M:%S}  normal {h.normal_count}  abnormal {h.abnormal_count}")
        for a in rt.safety_log:
            print(f"  SAFETY {a.ts:%H:%M:%S} {a.machine_id} {a.action.value}: {a.message}")
        for ev in rt.events():
            print(f"EVENT {ev.id} state={ev.state.value} outcome={ev.outcome}")
            for t in ev.timeline:
                print(f"  {t.ts:%H:%M:%S} [{t.actor}] {t.text}")
        if snap.focus:
            print("FOCUS", snap.focus.title, "|", snap.focus.detail, "|", snap.focus.schedule_impact)
        for req in rt.approvals.all():
            print(f"CARD {req.id} 第 {req.round} 輪「{req.title}」 已做：{'；'.join(req.done) or '—'}")
            for line in req.actions:
                print(f"  核准內容 {line}")
        for p in snap.pending_approvals:
            print("PENDING", p.id, p.title, [o.label for o in p.options])
        w = rt.world_snapshot()
        if w is not None:                           # issue #17: worker tasks and goods
            for t in w.tasks.items:
                print(f"TASK {t.id} {t.status} {t.kind} {t.worker_name or '—'} @ {t.where}: {t.title}"
                      f"（{t.event_id}）")
            print("GOODS", w.goods.counts)
    finally:
        await rt.stop()
    return 0


def cmd_check(args) -> int:
    from warroom.api.runtime import Runtime
    rt = Runtime(_settings(args), run_loop=False)
    print(json.dumps({"degraded": rt.degraded, "llm_mode": rt.llm_mode.value,
                      "scenario": rt.scenario.value}, ensure_ascii=False, indent=1))
    return 0 if not rt.degraded else 2


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m warroom.api", description="實心胎智慧製造戰情室")
    sub = p.add_subparsers(dest="cmd")
    for name in ("serve", "demo", "check"):
        sp = sub.add_parser(name)
        sp.add_argument("--scenario", choices=["normal", "press7_temp_drop", "open_mill_overheat", "bad_data",
                                               "concurrent_3", "shift_start", "rush_order", "staffing"])
        sp.add_argument("--speed", type=float)
        sp.add_argument("--llm-mode", choices=["mock", "llm"])
        sp.add_argument("--strict", action="store_true", help="fail instead of using fallback components")
        sp.add_argument("--replay", choices=["team_ev1", "team_ev2", "team_both"],
                        help="play the team dataset's sensor curves (normal scenario; data/team_dataset)")
        if name == "serve":
            sp.add_argument("--host")
            sp.add_argument("--port", type=int)
            sp.add_argument("--log-level", default="info")
        if name == "demo":
            sp.add_argument("--seconds", type=float, default=1200)
            sp.add_argument("--auto-approve", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "WARNING"))
    if args.cmd in (None, "serve"):
        if args.cmd is None:
            args = p.parse_args(["serve", *(argv or sys.argv[1:])])
        return cmd_serve(args)
    if args.cmd == "demo":
        return asyncio.run(_demo(args))
    return cmd_check(args)


if __name__ == "__main__":
    raise SystemExit(main())
