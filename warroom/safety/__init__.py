"""warroom/safety — deterministic PLC-style rules, direct stop path (red path, page 5).
Owner: safety builder. MUST NOT import warroom.agent, warroom.tools or any LLM client
(tests/test_contracts.py enforces). Public: RuleSafetyCore, default_rules."""
from warroom.safety.core import RuleSafetyCore, default_rules

__all__ = ["RuleSafetyCore", "default_rules"]
