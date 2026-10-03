"""Memory budget check (page 9: 全系統常駐 36–50 GB / 128 GB).

Budget (warroom.factory.MEMORY_BUDGET_GB): model NVFP4 19 + draft 2 + KV/state 4 + vLLM/CUDA 5 = 30 for the
model server, DGX OS 10, Qdrant 3, scheduling + app 6 -> 49 GB.

Measures each role by process (psutil RSS, matched by command line), by container (`docker stats`) and,
on the Spark, by GPU allocation (`nvidia-smi --query-compute-apps`), taking the largest view per role.

  uv run python -m bench.memory_check                 # prints table, writes bench/results/memory-*.json
  uv run python -m bench.memory_check --strict        # exit 1 unless PASS (watchdog / CI use)

Verdict: PASS only with every required role running (model server, app, and Qdrant when QDRANT_URL is set),
the model loaded at >= 32768 context (Ollama /api/ps context_length, vLLM /v1/models max_model_len or
--max-model-len), and stack + OS inside both edges of 36-50 GB. Missing role or short/unknown context ->
UNJUDGED. The OS term is measured on the Spark and the 10 GB budget on a dev Mac; the basis is recorded.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import psutil

from bench.llm import save_result
from warroom.factory import MEMORY_BUDGET_GB

GB = 1024 ** 3
BAND_GB = (36.0, 50.0)

# role -> (budget GB, regex over process cmdline / container name)
ROLES: dict[str, tuple[float, str]] = {
    "model_server": (MEMORY_BUDGET_GB["model_nvfp4"] + MEMORY_BUDGET_GB["draft"] + MEMORY_BUDGET_GB["kv_state"]
                     + MEMORY_BUDGET_GB["vllm_cuda"], r"vllm|ollama (runner|serve)|ollama_llama_server|llama-server|mlx"),
    "qdrant": (MEMORY_BUDGET_GB["qdrant"], r"qdrant"),
    "app": (MEMORY_BUDGET_GB["sched_app"], r"-m warroom\.api|warroom-app|uvicorn"),
}


def budget_total() -> float:
    return float(sum(MEMORY_BUDGET_GB.values()))


SHELLS = {"zsh", "bash", "sh", "fish", "dash", "login", "sudo", "timeout", "tail", "grep"}


def role_of(name: str | None, cmdline: list[str] | None) -> str | None:
    """Role of a live process; shells whose command text merely mentions warroom are not the app."""
    if (name or "").lower() in SHELLS:
        return None
    return classify(" ".join(cmdline or [name or ""]))


def classify(cmdline: str) -> str | None:
    for role, (_, pat) in ROLES.items():
        if re.search(pat, cmdline, re.I):
            if role == "qdrant" and ".app/Contents/" in cmdline:
                continue          # another macOS app's bundled qdrant (e.g. OpenWhispr), not ours
            return role
    return None


def by_process() -> dict[str, dict[str, Any]]:
    roles: dict[str, dict[str, Any]] = {r: {"rss_gb": 0.0, "pids": []} for r in ROLES}
    for p in psutil.process_iter(["pid", "name", "cmdline", "memory_info"]):
        try:
            cmd = " ".join(p.info["cmdline"] or [p.info["name"] or ""])
            role = role_of(p.info["name"], p.info["cmdline"])
            if role and "memory_check" not in cmd and "pytest" not in cmd:
                roles[role]["rss_gb"] += p.info["memory_info"].rss / GB
                roles[role]["pids"].append(p.info["pid"])
        except (psutil.Error, TypeError):
            continue
    return roles


def by_container() -> dict[str, float]:
    if not shutil.which("docker"):
        return {}
    try:
        out = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.MemUsage}}"],
                             capture_output=True, text=True, timeout=20).stdout
    except Exception:  # noqa: BLE001
        return {}
    res: dict[str, float] = {}
    for line in out.splitlines():
        name, _, usage = line.partition("\t")
        role = classify(name)
        if role:
            res[role] = res.get(role, 0.0) + parse_size(usage.split("/")[0])
    return res


def parse_size(s: str) -> float:
    m = re.match(r"\s*([\d.]+)\s*([KMGT]?i?B)", s)
    if not m:
        return 0.0
    v, u = float(m.group(1)), m.group(2).upper().replace("I", "")
    return v * {"B": 1 / GB, "KB": 1 / 2**20, "MB": 1 / 1024, "GB": 1, "TB": 1024}[u]


def by_gpu() -> dict[int, float]:
    if not shutil.which("nvidia-smi"):
        return {}
    try:
        out = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:  # noqa: BLE001
        return {}
    res = {}
    for line in out.splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) == 2 and parts[1].replace(".", "").isdigit():
            res[int(parts[0])] = float(parts[1]) / 1024
    return res


def by_ollama_ps(urls: list[str] | None = None) -> dict[str, Any]:
    """Apple Silicon: Ollama/MLX weights + KV live in Metal-wired memory that RSS does not show; Ollama's
    /api/ps `size_vram` is the honest number for the model server on a Mac."""
    import os
    import httpx
    roots = set(urls or [])
    base = os.environ.get("BASE_URL", "")
    if base:
        roots.add(base[:-3] if base.rstrip("/").endswith("/v1") else base)
    roots |= {"http://127.0.0.1:11434", "http://127.0.0.1:11435"}
    total, models = 0.0, []
    for r in sorted(roots):
        try:
            d = httpx.get(r.rstrip("/") + "/api/ps", timeout=2).json()
        except Exception:  # noqa: BLE001
            continue
        for m in d.get("models", []):
            gb = (m.get("size_vram") or m.get("size") or 0) / GB
            total += gb
            models.append({"server": r, "model": m.get("name"), "gb": round(gb, 2),
                           "context": m.get("context_length")})
    return {"gb": total, "models": models}


REQUIRED_CONTEXT = 32768   # pp.9-10: 32k context (incl. supervisor dialogue); vLLM --max-model-len 32768


def max_model_len_from_cmdlines() -> list[int]:
    """vLLM's `--max-model-len N` (or `=N`) on any live model-server process (container procs are visible
    from the Linux host)."""
    out = []
    for p in psutil.process_iter(["cmdline"]):
        try:
            cmd = " ".join(p.info["cmdline"] or [])
        except (psutil.Error, TypeError):
            continue
        m = re.search(r"--max[-_]model[-_]len[ =](\d+)", cmd)
        if m and "memory_check" not in cmd:
            out.append(int(m.group(1)))
    return out


def by_vllm_models(urls: list[str] | None = None) -> list[dict[str, Any]]:
    """vLLM's /v1/models lists `max_model_len` per served model."""
    import os
    import httpx
    roots = set(urls or [])
    for env in ("BENCH_BASE_URL", "BASE_URL"):
        if os.environ.get(env):
            roots.add(os.environ[env].rstrip("/").removesuffix("/v1"))
    roots |= {"http://127.0.0.1:8001", "http://127.0.0.1:8002"}   # deploy/spark compose: vllm, vllm-fp8
    res = []
    for r in sorted(roots):
        try:
            d = httpx.get(r.rstrip("/") + "/v1/models", timeout=2).json()
        except Exception:  # noqa: BLE001
            continue
        for m in d.get("data", []) if isinstance(d, dict) else []:
            if isinstance(m, dict) and m.get("max_model_len"):
                res.append({"server": r, "model": m.get("id"), "max_model_len": int(m["max_model_len"])})
    return res


def qdrant_external() -> bool:
    """True when the app talks to a Qdrant server (QDRANT_URL, the Spark compose); otherwise Qdrant runs
    embedded in the app process and its memory is inside the app's RSS."""
    try:
        from warroom.config import get_settings
        return bool(get_settings().qdrant_url)
    except Exception:  # noqa: BLE001
        import os
        return bool(os.environ.get("QDRANT_URL"))


def judge(roles: dict[str, Any], stack: float, os_gb: float, os_basis: str,
          contexts: list[int], required_roles: list[str]) -> dict[str, Any]:
    """PASS only when every required role is running, the model is loaded at >= 32k context, and
    stack + OS sits inside BOTH edges of the 36-50 GB band. Anything not measurable -> UNJUDGED."""
    unjudged, fail = [], []
    for r in required_roles:
        x = roles[r]
        if not (x["pids"] or x["container_gb"] > 0) or x["measured_gb"] <= 0:
            unjudged.append(f"role {r} not running (no pid/container with memory)")
    if not contexts:
        unjudged.append(f"model context unknown (need {REQUIRED_CONTEXT}: Ollama /api/ps context_length "
                        "or vLLM max_model_len)")
    elif min(contexts) < REQUIRED_CONTEXT:
        unjudged.append(f"model loaded at context {min(contexts)} < required {REQUIRED_CONTEXT}")
    total = stack + os_gb
    lo, hi = BAND_GB
    if total > hi:
        fail.append(f"stack+OS {total:.2f} GB > {hi} GB upper edge")
    if total < lo:
        fail.append(f"stack+OS {total:.2f} GB < {lo} GB lower edge")
    verdict = "UNJUDGED" if unjudged else ("FAIL" if fail else "PASS")
    return {"verdict": verdict, "reasons": unjudged + fail, "total_gb": round(total, 2),
            "os_gb": round(os_gb, 2), "os_basis": os_basis, "band_gb": BAND_GB,
            "required_roles": required_roles,
            "context": {"required": REQUIRED_CONTEXT, "loaded": contexts,
                        "min": min(contexts) if contexts else None}}


def measure(os_basis: str = "auto") -> dict[str, Any]:
    """os_basis: 'measured' uses RAM not matched to a role (honest on a dedicated Spark), 'budget' uses
    MEMORY_BUDGET_GB['dgx_os'] (a dev Mac's desktop would swamp the measured figure), 'auto' picks
    measured when nvidia-smi is present (the Spark) and budget otherwise. The basis is recorded."""
    procs = by_process()
    conts = by_container()
    gpu = by_gpu()
    olm = by_ollama_ps()
    vllm = by_vllm_models()
    vm = psutil.virtual_memory()
    q_ext = qdrant_external()
    roles: dict[str, Any] = {}
    stack = 0.0
    for role, (budget, _) in ROLES.items():
        if role == "qdrant" and not q_ext:
            roles[role] = {"measured_gb": 0.0, "budget_gb": budget, "rss_gb": 0.0, "container_gb": 0.0,
                           "gpu_gb": 0.0, "pids": [], "embedded_in": "app", "within_budget": True,
                           "note": "QDRANT_URL unset: Qdrant runs embedded in the app (counted in app RSS); "
                                   "other apps' qdrant processes are ignored"}
            continue
        gpu_gb = sum(gpu.get(pid, 0.0) for pid in procs[role]["pids"])
        olm_gb = olm["gb"] if role == "model_server" else 0.0
        measured = max(procs[role]["rss_gb"], conts.get(role, 0.0), gpu_gb, olm_gb)
        roles[role] = {"measured_gb": round(measured, 2), "budget_gb": budget,
                       "rss_gb": round(procs[role]["rss_gb"], 2), "container_gb": round(conts.get(role, 0.0), 2),
                       "gpu_gb": round(gpu_gb, 2), "pids": procs[role]["pids"],
                       **({"ollama_ps": olm["models"]} if role == "model_server" and olm["models"] else {}),
                       **({"vllm_models": vllm} if role == "model_server" and vllm else {}),
                       "within_budget": measured <= budget * 1.1}
        stack += measured
    used_gb = (vm.total - vm.available) / GB
    os_other = max(used_gb - stack, 0.0)
    roles["os_and_other"] = {"measured_gb": round(os_other, 2), "budget_gb": MEMORY_BUDGET_GB["dgx_os"],
                             "within_budget": os_other <= MEMORY_BUDGET_GB["dgx_os"] * 1.1,
                             "note": "everything not matched to a role (on a dev Mac this includes the desktop)"}
    if os_basis == "auto":
        os_basis = "measured" if gpu or shutil.which("nvidia-smi") else "budget"
    os_gb = os_other if os_basis == "measured" else float(MEMORY_BUDGET_GB["dgx_os"])
    contexts = [m["context"] for m in olm["models"] if m.get("context")] + \
        [m["max_model_len"] for m in vllm] + max_model_len_from_cmdlines()
    required = ["model_server", "app"] + (["qdrant"] if q_ext else [])
    j = judge(roles, stack, os_gb, os_basis, contexts, required)
    return {"total_ram_gb": round(vm.total / GB, 1), "used_gb": round(used_gb, 2), "stack_gb": round(stack, 2),
            "budget_total_gb": budget_total(), "band_gb": BAND_GB, "roles": roles, "judgement": j,
            # True PASS / False FAIL / None UNJUDGED (missing role, unknown or short context)
            "stack_within_band": {"PASS": True, "FAIL": False}.get(j["verdict"]),
            "headroom_gb": round(vm.total / GB - used_gb, 2)}


def main(argv: list[str] | None = None) -> dict[str, Any]:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--strict", action="store_true", help="exit 1 unless the verdict is PASS")
    p.add_argument("--os-basis", choices=("auto", "measured", "budget"), default="auto")
    p.add_argument("--no-save", action="store_true")
    p.add_argument("--out-dir", type=Path)
    a = p.parse_args(argv)
    m = measure(a.os_basis)
    for role, r in m["roles"].items():
        print(f"{role:<14} {r['measured_gb']:>7.2f} GB  budget {r['budget_gb']:>4} GB  ok={r['within_budget']}"
              + (f"  pids={r['pids']}" if "pids" in r else ""))
    j = m["judgement"]
    print(f"stack {m['stack_gb']} GB + OS ({j['os_basis']}) {j['os_gb']} = {j['total_gb']} GB, band {BAND_GB}, "
          f"context {j['context']['loaded'] or 'unknown'} (need {REQUIRED_CONTEXT}) -> {j['verdict']}"
          f"  (machine used {m['used_gb']}/{m['total_ram_gb']} GB)")
    for r in j["reasons"]:
        print("  -", r)
    if not a.no_save:
        print("saved", save_result("memory", None, m, a.out_dir))
    if a.strict and j["verdict"] != "PASS":
        raise SystemExit(1)
    return m


if __name__ == "__main__":
    main()
