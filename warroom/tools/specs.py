"""ToolSpec for all 9 allow-listed tools (descriptions in Chinese+English for the LLM,
params_schema = the *Args model's JSON schema from contracts)."""
from __future__ import annotations

from warroom.contracts import ToolName, ToolSpec

TOOL_SPECS: dict[ToolName, ToolSpec] = {}   # builder fills: one entry per ToolName
