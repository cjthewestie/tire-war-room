"""Team Westie — 實心胎智慧製造戰情室. See docs/ARCHITECTURE.md."""
import os as _os

# onnxruntime (loaded by fastembed, and by qdrant_client's fastembed mixin) ships Microsoft 1DS telemetry on
# POSIX that dials mobile.events.data.microsoft.com:443 from native code soon after import. The plant runs
# offline, so it must never try. The switch is read when onnxruntime loads, so set it before anything imports
# it; calling onnxruntime.disable_telemetry_events() afterwards is too late (seen under sandbox-exec).
_os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")

__version__ = "0.1.0"
