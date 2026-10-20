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
    llm_timeout_s: float = 30.0
    llm_fallback_to_mock: bool = True     # invalid JSON / unreachable -> mock plan, logged
    guided_json: bool = True              # vLLM extra_body guided_json; else response_format json_schema

    qdrant_path: Path = ROOT / "data" / "runtime" / "qdrant"
    qdrant_url: str | None = None         # set on Spark to use the qdrant container
    embed_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    sop_dir: Path = ROOT / "warroom" / "kb" / "sop"

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

    @property
    def api_key_effective(self) -> str:
        return self.nvidia_api_key or self.api_key


@lru_cache
def get_settings() -> Settings:
    return Settings()
