"""Runtime settings from environment variables. Implemented (architect-owned).

Same code runs on Ollama (Mac), vLLM (DGX Spark) or NVIDIA cloud:
  LLM_MODE=mock                     deterministic commander, no model (default)
  LLM_MODE=llm BASE_URL=http://localhost:11434/v1 MODEL=<ollama model>
  LLM_MODE=llm BASE_URL=http://spark:8000/v1 MODEL=nvidia/Nemotron-3.5-Lightning-30B-A3B-NVFP4
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from warroom.contracts import LLMMode

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    llm_mode: LLMMode = LLMMode.MOCK
    base_url: str = "http://localhost:11434/v1"
    model: str = "nemotron"
    api_key: str = "none"                 # NVIDIA_API_KEY also accepted, see api_key_effective
    nvidia_api_key: str | None = None
    llm_timeout_s: float = 60.0           # LLM_TIMEOUT_S; 30 s timed out plan and replan on a live p.11 run
    # COMMANDER_MAX_TOKENS: completion cap for a commander plan. 1200 cut the live p.11 plan mid-JSON
    # (Nemotron pretty-prints, ~1.2-1.5k tokens for a full plan); a cut plan is audited as "truncated"
    commander_max_tokens: int = 4096
    # LLM_CONTEXT_TOKENS: the serving model's context window (vLLM --max-model-len, Ollama num_ctx). The commander
    # keeps system + user prompt within it minus commander_max_tokens, trimming per-event context first (#33)
    llm_context_tokens: int = 32768
    # PROMPT_TOKEN_FACTOR: serving-tokenizer tokens per approx_tokens() unit. 2.0 stays on the safe side until it is
    # measured against the server's prompt_tokens (every commander LLM_CALL audit entry carries both numbers)
    prompt_token_factor: float = 2.0
    llm_fallback_to_mock: bool = True     # invalid JSON / unreachable -> mock plan, logged
    guided_json: bool = True              # vLLM extra_body guided_json; else response_format json_schema

    qdrant_path: Path = ROOT / "data" / "runtime" / "qdrant"
    qdrant_url: str | None = None         # set on Spark to use the qdrant container
    embed_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    sop_dir: Path = ROOT / "warroom" / "kb" / "sop"
    # SOP no-answer gate (warroom/kb/store.py retrieve): a hit needs this hybrid score plus a key term of the
    # question, or kb_strong_score alone. Calibrated on tests/data/rag_eval.yaml: scripts/rag_eval.py --curve.
    kb_min_score: float = 0.18
    kb_strong_score: float = 0.9

    audit_path: Path = ROOT / "data" / "runtime" / "audit.jsonl"
    schedule_dir: Path = ROOT / "data" / "runtime" / "schedules"
    orders_xlsx: Path | None = None       # Excel import (no MES); None = sim seed data

    host: str = "0.0.0.0"
    port: int = 8000
    sim_speed: float = 1.0
    scenario: str = "day_loop"             # issue #36: 一日循環 is the normal running state
    # Team dataset replay (data/team_dataset): "" off | team_ev1 | team_ev2 | team_both.
    # Plays the dataset's sensor curves in the NORMAL scenario; see warroom/sim/team_replay.py.
    replay: str = Field("", validation_alias=AliasChoices("replay", "WARROOM_REPLAY"))
    approval_timeout_s: float = 600.0
    # REPAIR_FAST_S: demo fast repair (Kyle 10-21 晚: 「Agent 進廠維修（還是需要執行時間，壓在 10-15 秒完成）」). A repair task
    # (檢修 / 停用檢修 / 放慢投料 / 暫停投料) starts after 主管核准 and is done this many WALL seconds later, at any sim
    # speed. 0 = the real task durations (work.WORK_MIN).
    repair_fast_s: float = 12.0
    # RECOVER_FAST_S: after that repair the machine's readings ramp back to nominal over this many WALL seconds
    # (「機台恢復正常溫度後轉回綠燈」: 回溫中, amber); the light turns green once they are back. 0 = at once.
    recover_fast_s: float = 4.0
    # MODEL_SLOTS: commander / main agent calls the local model may run at once (issue #54 ModelQueue). The Spark's
    # vLLM serves MAX_NUM_SEQS=4 (deploy/spark/vllm-serve.sh: 3 concurrent anomalies + 1 spare); with 1 slot the
    # 3 件同時異常 plans ran one after another (real model 10-21: last card 132 s after the load, 3 slots: same second).
    model_slots: int = 3
    # Model usage caps (warroom/agent/budget.py has the degradation order and why these numbers, from
    # bench/results/spark-lightning-20261021): BUDGET_DAILY_TOKENS, BUDGET_DAILY_MODEL_S, BUDGET_EVENT_TOKENS,
    # BUDGET_CONVERSATION_TOKENS. Incident plans are never blocked by them.
    budget_daily_tokens: int = 3_000_000
    budget_daily_model_s: float = 7200.0
    budget_event_tokens: int = 60_000
    budget_conversation_tokens: int = 500_000

    @property
    def api_key_effective(self) -> str:
        return self.nvidia_api_key or self.api_key


@lru_cache
def get_settings() -> Settings:
    return Settings()
