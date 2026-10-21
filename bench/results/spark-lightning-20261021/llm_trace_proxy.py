# ruff: noqa: E501
"""LLM trace proxy for the war-room audit (2026-10-21, Claude).

Sits between a war-room instance and westie-llm (vLLM, :8000). Forwards every request unchanged and records,
per chat completion: caller role, system prompt, messages, tools offered, tool calls made, prompt / system /
output tokens (via vLLM /tokenize, computed after the response so the app is never slowed), TTFT, total time,
decode rate and finish_reason. Never records request headers (the Authorization header carries the API key).

  python llm_trace_proxy.py            # listens 0.0.0.0:8198; app: BASE_URL=http://127.0.0.1:8198/v1
  dashboard: http://<spark>:8198/dash
"""
from __future__ import annotations

import asyncio
import itertools
import json
import pathlib
import subprocess
import time
from collections import deque
from datetime import datetime

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

UP = "http://127.0.0.1:8000"
LOG = pathlib.Path.home() / "twr-audit-results" / "llm-trace.jsonl"
LOG.parent.mkdir(parents=True, exist_ok=True)
app = FastAPI()
client = httpx.AsyncClient(base_url=UP, timeout=httpx.Timeout(None, connect=5.0))
seq = itertools.count(1)
RECS: deque[dict] = deque(maxlen=300)
HOP = {"host", "content-length", "connection", "accept-encoding", "transfer-encoding"}


def _text(c) -> str:
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(x.get("text", "") for x in c if isinstance(x, dict))
    return "" if c is None else json.dumps(c, ensure_ascii=False)


def classify(msgs: list[dict]) -> str:
    s = _text(next((m.get("content") for m in msgs if m.get("role") == "system"), ""))
    head = s[:600]
    for key, role in (("主 agent", "主 agent"), ("戰情室的 agent", "主 agent"), ("事件指揮官", "指揮官"), ("指揮官", "指揮官"),
                      ("討論", "討論"), ("問戰情室", "問戰情室"), ("戰情室助理", "問戰情室")):
        if key in head:
            return role
    return "其他"


def loop_step(msgs: list[dict]) -> tuple[int, int]:
    """(this call's position in the agent loop, tool results carried). The main agent nudges the model with extra
    user messages between steps, so count every assistant turn after the first user message, not the last."""
    first_user = next((i for i, m in enumerate(msgs) if m.get("role") == "user"), len(msgs))
    after = msgs[first_user + 1:]
    return 1 + sum(1 for m in after if m.get("role") == "assistant"), sum(1 for m in after if m.get("role") == "tool")


async def tokenize(auth: str | None, payload: dict) -> int | None:
    h = {"Authorization": auth} if auth else {}
    try:
        r = await client.post("/tokenize", json=payload, headers=h, timeout=20)
        if r.status_code == 200:
            return r.json().get("count")
    except Exception:  # noqa: BLE001
        pass
    return None


async def finish(rec: dict, body: dict, auth: str | None) -> None:
    msgs = body.get("messages") or []
    base = {"model": body.get("model")}
    if rec.get("prompt_tokens") is None:
        p = dict(base, messages=msgs, add_generation_prompt=True)
        if body.get("tools"):
            p["tools"] = body["tools"]
        rec["prompt_tokens"] = await tokenize(auth, p)
        if rec["prompt_tokens"] is None and body.get("tools"):
            p.pop("tools")
            rec["prompt_tokens"] = await tokenize(auth, p)
            rec["prompt_tokens_note"] = "tools 未計入"
    sys_txt = rec.get("system_prompt") or ""
    rec["system_tokens"] = await tokenize(auth, dict(base, prompt=sys_txt, add_special_tokens=False)) if sys_txt else 0
    if rec.get("completion_tokens") is None:
        out = (rec.get("output") or "") + (rec.get("reasoning") or "") + json.dumps(rec.get("tool_calls") or "", ensure_ascii=False)
        rec["completion_tokens"] = await tokenize(auth, dict(base, prompt=out, add_special_tokens=False)) if out.strip() else 0
        rec["completion_tokens_note"] = "由輸出文字重新計算"
    gen_s = (rec["total_s"] - rec["ttft_s"]) if rec.get("ttft_s") is not None else None
    rec["decode_tps"] = round(rec["completion_tokens"] / gen_s, 1) if rec.get("completion_tokens") and gen_s and gen_s > 0.05 else None
    rec["done"] = True
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def new_rec(body: dict) -> dict:
    msgs = body.get("messages") or []
    step, tool_results = loop_step(msgs)
    user_msgs = [m for m in msgs if m.get("role") == "user"]
    return {
        "id": next(seq), "ts": datetime.now().strftime("%H:%M:%S"), "role": classify(msgs),
        "model": body.get("model"), "stream": bool(body.get("stream")), "max_tokens": body.get("max_tokens"),
        "temperature": body.get("temperature"), "template_kwargs": body.get("chat_template_kwargs"),
        "response_format": (body.get("response_format") or {}).get("type") if isinstance(body.get("response_format"), dict) else None,
        "n_messages": len(msgs), "roles": [m.get("role") for m in msgs], "loop_step": step, "tool_results_in": tool_results,
        "tools_offered": [t.get("function", {}).get("name") for t in body.get("tools") or []],
        "system_prompt": _text(next((m.get("content") for m in msgs if m.get("role") == "system"), "")),
        "last_user": _text(user_msgs[-1].get("content")) if user_msgs else "",
        "messages": [{"role": m.get("role"), "content": _text(m.get("content"))[:6000],
                      "tool_calls": m.get("tool_calls")} for m in msgs],
        "output": "", "reasoning": "", "tool_calls": [], "finish_reason": None,
        "prompt_tokens": None, "completion_tokens": None, "ttft_s": None, "total_s": None, "status": None, "done": False,
    }


@app.get("/dash", response_class=HTMLResponse)
async def dash() -> str:
    return DASH


@app.get("/dash/data")
async def dash_data() -> JSONResponse:
    m = {}
    try:
        t = (await client.get("/metrics", timeout=3)).text
        for line in t.splitlines():
            for k in ("vllm:num_requests_running", "vllm:num_requests_waiting{", "vllm:prefix_cache_hits_total",
                      "vllm:prefix_cache_queries_total", "vllm:request_success_total"):
                if line.startswith(k):
                    m[k.rstrip("{").split(":")[1]] = m.get(k.rstrip("{").split(":")[1], 0) + float(line.split()[-1])
    except Exception:  # noqa: BLE001
        pass
    try:
        g = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader"],
                           capture_output=True, text=True, timeout=3).stdout.strip()
    except Exception:  # noqa: BLE001
        g = "?"
    return JSONResponse({"metrics": m, "gpu": g, "recs": list(RECS)[::-1]})


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy(path: str, request: Request):
    raw = await request.body()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
    auth = request.headers.get("authorization")
    is_chat = request.method == "POST" and path.rstrip("/").endswith("chat/completions")
    if not is_chat:
        r = await client.request(request.method, "/" + path, content=raw, headers=headers, params=request.query_params)
        return Response(r.content, status_code=r.status_code, headers={k: v for k, v in r.headers.items() if k.lower() not in HOP})
    body = json.loads(raw or b"{}")
    rec = new_rec(body)
    RECS.append(rec)
    t0 = time.perf_counter()
    if not body.get("stream"):
        r = await client.post("/" + path, content=raw, headers=headers)
        rec["total_s"] = round(time.perf_counter() - t0, 3)
        rec["ttft_s"] = None
        rec["status"] = r.status_code
        try:
            j = r.json()
            ch = (j.get("choices") or [{}])[0]
            msg = ch.get("message") or {}
            rec["output"] = msg.get("content") or ""
            rec["reasoning"] = msg.get("reasoning_content") or msg.get("reasoning") or ""
            rec["tool_calls"] = [{"name": (c.get("function") or {}).get("name"), "args": (c.get("function") or {}).get("arguments")}
                                 for c in msg.get("tool_calls") or []]
            rec["finish_reason"] = ch.get("finish_reason")
            u = j.get("usage") or {}
            rec["prompt_tokens"], rec["completion_tokens"] = u.get("prompt_tokens"), u.get("completion_tokens")
        except Exception:  # noqa: BLE001
            rec["output"] = r.text[:2000]
        asyncio.create_task(finish(rec, body, auth))
        return Response(r.content, status_code=r.status_code, headers={k: v for k, v in r.headers.items() if k.lower() not in HOP})

    req = client.build_request("POST", "/" + path, content=raw, headers=headers)
    up = await client.send(req, stream=True)
    rec["status"] = up.status_code
    calls: dict[int, dict] = {}

    async def gen():
        buf = ""
        try:
            async for chunk in up.aiter_bytes():
                yield chunk
                buf += chunk.decode("utf-8", "replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line.startswith("data:") or line[5:].strip() == "[DONE]":
                        continue
                    try:
                        j = json.loads(line[5:])
                    except Exception:  # noqa: BLE001
                        continue
                    if j.get("usage"):
                        rec["prompt_tokens"] = j["usage"].get("prompt_tokens")
                        rec["completion_tokens"] = j["usage"].get("completion_tokens")
                    for ch in j.get("choices") or []:
                        d = ch.get("delta") or {}
                        piece = d.get("content") or ""
                        rs = d.get("reasoning_content") or d.get("reasoning") or ""
                        if (piece or rs or d.get("tool_calls")) and rec["ttft_s"] is None:
                            rec["ttft_s"] = round(time.perf_counter() - t0, 3)
                        rec["output"] += piece
                        rec["reasoning"] += rs
                        for tc in d.get("tool_calls") or []:
                            c = calls.setdefault(tc.get("index", 0), {"name": "", "args": ""})
                            f = tc.get("function") or {}
                            c["name"] += f.get("name") or ""
                            c["args"] += f.get("arguments") or ""
                        if ch.get("finish_reason"):
                            rec["finish_reason"] = ch["finish_reason"]
        finally:
            await up.aclose()
            rec["total_s"] = round(time.perf_counter() - t0, 3)
            rec["tool_calls"] = list(calls.values())
            asyncio.create_task(finish(rec, body, auth))

    return StreamingResponse(gen(), status_code=up.status_code, media_type=up.headers.get("content-type", "text/event-stream"))


DASH = r"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Lightning 呼叫監看</title><style>
:root{--bg:#0f1115;--fg:#e6e6e6;--mut:#9aa0a6;--line:#2a2f3a;--acc:#76b900;--warn:#f0a000;--bad:#e5534b}
body{margin:0;background:var(--bg);color:var(--fg);font:13px/1.45 system-ui,"Noto Sans TC",sans-serif}
header{position:sticky;top:0;background:#151922;border-bottom:1px solid var(--line);padding:10px 16px;display:flex;gap:18px;flex-wrap:wrap;align-items:center;z-index:2}
header b{color:var(--acc)} .k{color:var(--mut)} main{padding:12px 16px}
table{border-collapse:collapse;width:100%} th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top;white-space:nowrap}
th{color:var(--mut);font-weight:500;position:sticky;top:44px;background:var(--bg)} tr.r{cursor:pointer} tr.r:hover{background:#171b24}
td.q{white-space:normal;max-width:340px} .num{text-align:right;font-variant-numeric:tabular-nums}
.tag{padding:1px 6px;border-radius:4px;background:#222a36} .t-主{background:#2d3a14;color:#c7f06a} .t-指{background:#3a2a10;color:#ffcf70} .t-問{background:#102a3a;color:#8fd3ff}
.bad{color:var(--bad)} .warn{color:var(--warn)} pre{white-space:pre-wrap;word-break:break-word;background:#0b0d11;border:1px solid var(--line);padding:8px;max-height:420px;overflow:auto;margin:6px 0}
.det td{background:#12151c} .sum{margin:8px 0 14px;color:var(--mut)} h3{margin:10px 0 4px;font-size:13px;color:var(--acc)}
</style></head><body>
<header><span><b>Lightning 呼叫監看</b>（8099 → 代理 :8198 → westie-llm :8000）</span>
<span><span class="k">執行中</span> <b id="run">-</b></span><span><span class="k">排隊</span> <b id="wait">-</b></span>
<span><span class="k">GPU</span> <b id="gpu">-</b></span><span><span class="k">prefix 命中</span> <b id="pc">-</b></span><span><span class="k">累計完成</span> <b id="ok">-</b></span></header>
<main><div class="sum" id="sum"></div><table><thead><tr><th>#</th><th>時間</th><th>角色</th><th>loop</th><th>主管的話／最後一則 user</th><th class="num">輸入 tok</th><th class="num">system tok</th><th class="num">輸出 tok</th><th class="num">首字 s</th><th class="num">整次 s</th><th class="num">tok/s</th><th>finish</th><th>模型要求的工具</th></tr></thead><tbody id="tb"></tbody></table></main>
<script>
const open=new Set();const esc=s=>(s??'').toString().replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
async function tick(){let d;try{d=await (await fetch('/dash/data')).json()}catch(e){return}
const m=d.metrics;run.textContent=m.num_requests_running??'-';wait.textContent=m.num_requests_waiting??'-';gpu.textContent=d.gpu;ok.textContent=m.request_success_total??'-';
pc.textContent=m.prefix_cache_queries_total?((100*m.prefix_cache_hits_total/m.prefix_cache_queries_total).toFixed(2)+'%'):'-';
const R=d.recs;const done=R.filter(r=>r.done);const sumf=k=>done.reduce((a,r)=>a+(r[k]||0),0);
sum.textContent=`共 ${R.length} 次呼叫；輸入合計 ${sumf('prompt_tokens')} tok、輸出合計 ${sumf('completion_tokens')} tok；點一列看 system prompt、完整 context 與模型輸出。`;
tb.innerHTML=R.map(r=>{const tag=r.role[0];const fr=r.finish_reason;const frc=fr==='length'?'bad':'';
const tools=(r.tool_calls||[]).map(c=>esc(c.name)+'('+esc((c.args||'').slice(0,60))+')').join('<br>');
let row=`<tr class="r" onclick="tg(${r.id})"><td>${r.id}</td><td>${r.ts}</td><td><span class="tag t-${tag}">${esc(r.role)}</span></td><td>第 ${r.loop_step} 次${r.tool_results_in?`・帶 ${r.tool_results_in} 個工具結果`:''}</td><td class="q">${esc(r.last_user.slice(0,90))}</td>
<td class="num">${r.prompt_tokens??'…'}</td><td class="num">${r.system_tokens??'…'}</td><td class="num">${r.completion_tokens??'…'}</td><td class="num">${r.ttft_s??'-'}</td><td class="num">${r.total_s??'<span class=warn>進行中</span>'}</td><td class="num">${r.decode_tps??'-'}</td><td class="${frc}">${esc(fr)}</td><td>${tools}</td></tr>`;
if(open.has(r.id)){row+=`<tr class="det"><td colspan="13"><div>max_tokens=${r.max_tokens} temperature=${r.temperature} stream=${r.stream} template=${esc(JSON.stringify(r.template_kwargs))} 訊息 ${r.n_messages} 則：${esc(r.roles.join(' → '))}；提供工具 ${r.tools_offered.length} 個：${esc(r.tools_offered.join('、'))} ${r.prompt_tokens_note?'（'+r.prompt_tokens_note+'）':''}</div>
<h3>system prompt（${r.system_prompt.length} 字）</h3><pre>${esc(r.system_prompt)}</pre>
<h3>其餘 context</h3>${r.messages.filter(x=>x.role!=='system').map(x=>`<div class="k">${esc(x.role)}</div><pre>${esc(x.content)}${x.tool_calls?'\n[tool_calls] '+esc(JSON.stringify(x.tool_calls)):''}</pre>`).join('')}
<h3>模型輸出</h3><pre>${esc(r.output)}${r.reasoning?'\n\n[reasoning]\n'+esc(r.reasoning):''}${(r.tool_calls||[]).length?'\n\n[tool_calls]\n'+esc(JSON.stringify(r.tool_calls,null,1)):''}</pre></td></tr>`}
return row}).join('')}
function tg(i){open.has(i)?open.delete(i):open.add(i);tick()}
tick();setInterval(tick,2000);
</script></body></html>"""

def reload_log() -> None:
    """Keep the dashboard history across proxy restarts (relabel with the current rules)."""
    global seq
    if not LOG.exists():
        return
    last = 0
    for line in LOG.read_text(encoding="utf-8").splitlines()[-300:]:
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        msgs = r.get("messages") or []
        r["role"] = classify(msgs)
        r["loop_step"], r["tool_results_in"] = loop_step(msgs)
        RECS.append(r)
        last = max(last, r.get("id", 0))
    seq = itertools.count(last + 1)


if __name__ == "__main__":
    reload_log()
    uvicorn.run(app, host="0.0.0.0", port=8198, log_level="warning")
