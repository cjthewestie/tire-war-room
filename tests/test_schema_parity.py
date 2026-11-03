"""Every response_format JSON schema we send to the model stays inside what llama.cpp's
JSON-schema -> GBNF converter really supports. Pure Python, offline.

Why: llama.cpp skips unsupported schema features *silently* (its own README says so), so a schema
edit can quietly stop constraining the model with no error anywhere. This test turns that silent
downgrade into a red CI run.

Versions and source (checked 2026-11-03):
- Ollama 0.33.2 is the version recorded in bench/results/*.json ("ollama_version": "0.33.2"), Mac.
  Ollama hands `format` / response_format json_schema to llama.cpp's json-schema-to-grammar. Which
  llama.cpp commit Ollama 0.33.2 vendors is NOT confirmed (the ollama repo was not reachable from the
  session that wrote this); the list below is from llama.cpp upstream.
- llama.cpp grammars/README.md, section "JSON Schemas -> GBNF", lists "known limitations" and
  "other unsupported features", at ggml-org/llama.cpp commit fac3a894fc04901c3e5abaff457e024c6b52e04f
  (master HEAD on 2026-11-03):
  https://github.com/ggml-org/llama.cpp/blob/fac3a894fc04901c3e5abaff457e024c6b52e04f/grammars/README.md
- Spark serves through vLLM (xgrammar), whose support is a superset for these keywords; the llama.cpp
  list is the stricter one, so it is the one we check.

Only limitations the README states plainly are enforced. Left out on purpose:
- "Nested $refs are broken" (issue #8073): the README does not say what counts as nested, and
  pydantic's local `#/$defs/...` refs (which the commander plan uses today) work in practice.
- `"additionalProperties": true` "may produce keys that contain unescaped newlines": a quality
  caveat, not an unsupported keyword (PlanStep.args needs it).
"""
from __future__ import annotations

import copy
from typing import Any

import pytest

from warroom.agent.ask import ask_plan_schema
from warroom.agent.commander import plan_schema
from warroom.agent.discuss import classify_schema
from warroom.agent.orchestrator import dispatch_schema
from warroom.agent.qa import qa_schema
from warroom.agent.reporter import report_schema

# README "other unsupported features ... unlikely to be implemented" + "No patternProperties" +
# "prefixItems is broken". `maxContains` goes with contains/minContains.
UNSUPPORTED_KEYWORDS = frozenset({
    "prefixItems", "patternProperties", "uniqueItems", "contains", "minContains", "maxContains",
    "$anchor", "not", "if", "then", "else", "dependentSchemas",
})
# README: "minimum, exclusiveMinimum, maximum, exclusiveMaximum: only supported for type integer"
INTEGER_ONLY_BOUNDS = frozenset({"minimum", "exclusiveMinimum", "maximum", "exclusiveMaximum"})
# README: "string formats lack uri, email"
UNSUPPORTED_FORMATS = frozenset({"uri", "email"})

SCHEMAS = {
    "plan_schema": plan_schema,
    "ask_plan_schema": ask_plan_schema,
    "classify_schema": classify_schema,
    "dispatch_schema": dispatch_schema,
    "qa_schema": lambda: qa_schema(["SOP-CP-02#1", "docs/qa/framework-qa.md#3"]),
    "qa_schema_no_refs": lambda: qa_schema([]),
    "report_schema": report_schema,
}


def _subschemas(node: dict[str, Any]):
    """(path suffix, subschema) for every place a JSON schema nests another schema."""
    for key in ("properties", "$defs", "definitions"):
        for name, sub in (node.get(key) or {}).items():
            yield f"{key}.{name}", sub
    for key in ("items", "additionalProperties"):
        sub = node.get(key)
        if isinstance(sub, dict):
            yield key, sub
        elif isinstance(sub, list):
            for i, s in enumerate(sub):
                yield f"{key}[{i}]", s
    for key in ("anyOf", "oneOf", "allOf"):
        for i, s in enumerate(node.get(key) or []):
            yield f"{key}[{i}]", s


def _resolve_local(root: dict[str, Any], ref: str) -> bool:
    cur: Any = root
    for part in ref[2:].split("/"):
        if not isinstance(cur, dict) or part not in cur:
            return False
        cur = cur[part]
    return isinstance(cur, dict)


def problems(schema: dict[str, Any]) -> list[str]:
    """Every use of a feature llama.cpp's converter does not support, as 'path: why'."""
    out: list[str] = []

    def walk(node: Any, path: str) -> None:
        if not isinstance(node, dict):
            return
        for kw in sorted(UNSUPPORTED_KEYWORDS & node.keys()):
            out.append(f"{path}: `{kw}` is not supported")
        types = node.get("type")
        types = set(types) if isinstance(types, list) else {types}
        for kw in sorted(INTEGER_ONLY_BOUNDS & node.keys()):
            if types != {"integer"}:
                out.append(f"{path}: `{kw}` only works on type integer (type={node.get('type')!r})")
        pat = node.get("pattern")
        if isinstance(pat, str) and not (pat.startswith("^") and pat.endswith("$")):
            out.append(f"{path}: pattern {pat!r} must start with ^ and end with $")
        if node.get("format") in UNSUPPORTED_FORMATS:
            out.append(f"{path}: format {node['format']!r} is not supported")
        if "properties" in node and ("anyOf" in node or "oneOf" in node):
            out.append(f"{path}: `properties` mixed with anyOf/oneOf in the same type")
        ref = node.get("$ref")
        if isinstance(ref, str):
            if not ref.startswith("#/"):
                out.append(f"{path}: remote $ref {ref!r} is not supported")
            elif not _resolve_local(schema, ref):
                out.append(f"{path}: $ref {ref!r} does not resolve")
        for suffix, sub in _subschemas(node):
            walk(sub, f"{path}.{suffix}")

    walk(schema, "$")
    return out


@pytest.mark.parametrize("name", sorted(SCHEMAS))
def test_schema_uses_only_supported_keywords(name):
    schema = SCHEMAS[name]()
    assert schema.get("type") == "object", name
    assert problems(schema) == [], f"{name}: " + "; ".join(problems(schema))


@pytest.mark.parametrize("bad", [
    {"uniqueItems": True},
    {"not": {"type": "null"}},
    {"patternProperties": {"^x$": {"type": "string"}}},
    {"type": "number", "minimum": 0},
    {"type": "string", "pattern": "[a-z]+"},
    {"type": "string", "format": "email"},
    {"$ref": "https://example.com/s.json"},
    {"$ref": "#/$defs/Nope"},
    {"type": "object", "properties": {"a": {"type": "string"}}, "anyOf": [{"required": ["a"]}]},
])
def test_checker_catches_an_unsupported_keyword(bad):
    """The self-check itself: plant one unsupported feature deep in a real schema -> it is reported."""
    s = copy.deepcopy(plan_schema())
    s["$defs"]["AlertCard"]["properties"]["title"] = bad
    assert problems(s), bad


def test_checker_allows_integer_bounds():
    s = copy.deepcopy(dispatch_schema())
    s["properties"]["n"] = {"type": "integer", "minimum": 0, "maximum": 9}
    assert problems(s) == []
