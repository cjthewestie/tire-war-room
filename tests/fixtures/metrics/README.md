Recorded inputs for tests/test_metrics.py.

- `vm_stat_mac.txt`, `ollama_ps_mac.json`: captured on CJ's Mac (Apple Silicon, Ollama 0.34.4, nemotron-3.5-lightning:30b-a3b-nvfp4 loaded), 2026-10-22.
- `nvidia_smi_spark.csv`, `nvidia_smi_compute_apps_spark.csv`, `vllm_metrics_spark.txt`: DGX Spark (GB10) shape, written by hand from the nvidia-smi `--format=csv,noheader,nounits` and vLLM Prometheus formats — GB10 prints `[N/A]` for GPU memory (unified memory). Not yet a capture from the Spark: replace with `nvidia-smi --query-gpu=name,utilization.gpu,temperature.gpu,memory.used,memory.total,power.draw --format=csv,noheader,nounits` and `curl :8000/metrics` output when on the box.
