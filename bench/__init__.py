"""Benchmarks for the page-9/10 verification targets (TTFT, full event, 3 concurrent, NVFP4 vs FP8,
tokens/W, memory, unplug). Every runner talks to any OpenAI-compatible endpoint (Ollama on the Mac,
vLLM on the DGX Spark) chosen by BASE_URL / MODEL, and writes JSON into bench/results/."""
