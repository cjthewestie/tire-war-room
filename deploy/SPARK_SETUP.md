# Standing up a new demo Spark

The finals Spark (`aitopatom-3ef3`, `nvidia@…`) was lost on 2026-10-22. This is how it was set up for the
demo, rebuilt from the session transcripts. It is the simple path the demo used: one vLLM container, the app
straight from the checkout as a `systemd --user` service. The full compose stack (`deploy/spark/`,
`deploy/systemd/`, `deploy/bin/install_spark.sh`) is still the documented production path.

No secrets are in git. Keys and the PIN live in `~/.westie/*.env` (mode 600) on the Spark only.

## 1. Checkout and Python / web

```bash
git clone https://github.com/cjthewestie/tire-war-room.git ~/tire-war-room
cd ~/tire-war-room
uv sync                      # creates .venv (the run script uses .venv/bin/python)
cd web && pnpm install && pnpm build && cd ..
```

Node was nvm's v22 (`~/.nvm/versions/node/v22.*/bin`) with pnpm in `~/.local/share/pnpm`.

## 2. Secrets

```bash
mkdir -p ~/.westie && chmod 700 ~/.westie
cp deploy/spark-demo/vllm.env.example    ~/.westie/vllm.env     # set VLLM_API_KEY
cp deploy/spark-demo/warroom.env.example ~/.westie/warroom.env  # WARROOM_NO_PIN=1 for the finals
chmod 600 ~/.westie/*.env
```

## 3. The model: `westie-llm` (vLLM on :8000, served name `lightning`)

The container ran `vllm/vllm-openai:v0.27.1` with restart policy `unless-stopped`. Its command line was read
back with `docker inspect` and is exact. The `docker run` flags around it (GPU, IPC, HF cache mount) were not
recorded, so they are the usual ones:

```bash
set -a; . ~/.westie/vllm.env; set +a
docker run -d --name westie-llm --restart unless-stopped --gpus all --ipc=host \
  -p 8000:8000 -v ~/.cache/huggingface:/root/.cache/huggingface \
  vllm/vllm-openai:v0.27.1 \
  nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4 \
  --served-model-name lightning --host 0.0.0.0 --port 8000 \
  --max-model-len 65536 --moe-backend marlin --kv-cache-dtype fp8 --enable-prefix-caching \
  --gpu-memory-utilization 0.4 \
  --speculative_config.model nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4-DSpark \
  --speculative_config.num_speculative_tokens 3 \
  --mamba-backend flashinfer --mamba-cache-mode align \
  --reasoning-parser nemotron_v3 --tool-call-parser qwen3_coder --enable-auto-tool-choice \
  --api-key "$VLLM_API_KEY"
curl -s -H "Authorization: Bearer $VLLM_API_KEY" localhost:8000/v1/models
```

Port 8080 is taken by NemoClaw's OpenShell on that box, so the app uses 8090.

## 4. The app: `warroom.service` (systemd --user, port 8090)

`deploy/spark-demo/run_warroom_spark.sh` sets `LLM_MODE=llm BASE_URL=http://127.0.0.1:8000/v1 MODEL=lightning`,
reads the key from `~/.westie/vllm.env`, sources `~/.westie/warroom.env`, and runs
`.venv/bin/python -m warroom.api serve` on `0.0.0.0:8090`.

```bash
mkdir -p ~/.config/systemd/user
cp deploy/spark-demo/warroom.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now warroom.service
sudo loginctl enable-linger nvidia      # keeps the user service up with nobody logged in
loginctl show-user nvidia -p Linger     # Linger=yes
```

After a backend change: `systemctl --user restart warroom.service`. A web-only change needs only
`cd web && pnpm build` (the service serves `web/dist`).

## 5. Check

- `http://<spark>:8090/` war room, `/phone` supervisor phone, `/factory?demo=cure07` the 3D auto demo,
  `/factory?fps=1` with a frame-rate readout.
- `curl -s localhost:8090/api/health` shows `llm_reachable: true` and `model: lightning`.

## 6. The screen (optional)

The demo screen was Firefox on the Spark's own display:

```bash
export DISPLAY=:0 XDG_RUNTIME_DIR=/run/user/$(id -u)
pkill -x firefox; sleep 3
setsid nohup firefox --kiosk "http://localhost:8090/factory?demo=cure07" >/dev/null 2>&1 < /dev/null &
```

## Never commit

`data/runtime/`, any `.env`, `~/.westie/*`, API keys, the supervisor PIN.
