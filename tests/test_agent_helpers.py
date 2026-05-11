"""Unit tests for pure helpers in agent.py.

These exercise the deterministic, side-effect-free pieces of the pipeline —
constraint formatting, JSON schema translation, critic JSON parsing, Tavily
MCP result parsing — without spinning up Gemini, Tavily, or any MCP
subprocesses.
"""

from __future__ import annotations

import json

from google.genai import types as gtypes

from agent import (
    PREFS_OPTIONS,
    _format_constraints,
    _format_prefs,
    _jsonschema_to_gemini,
    _parse_critic_json,
    _parse_tavily_mcp_result,
    _truncate,
)

# ── _truncate ──────────────────────────────────────────────────────────────

def test_truncate_short_string_unchanged():
    assert _truncate("hello") == "hello"


def test_truncate_long_string_clipped_with_ellipsis():
    long = "x" * 100
    out = _truncate(long, n=10)
    assert len(out) == 10
    assert out.endswith("…")


def test_truncate_non_string_is_stringified():
    assert _truncate(12345) == "12345"
    assert _truncate({"a": 1})  # repr of dict, doesn't raise


# ── _format_constraints ────────────────────────────────────────────────────

def test_constraints_usd_no_origin_no_month_still_includes_block():
    out = _format_constraints(currency="USD", travel_month=None)
    assert "Trip constraints" in out
    assert "USD" in out
    assert "NOT PROVIDED" in out  # origin missing
    # Should not say "do NOT prefix amounts with $" for USD
    assert "Do NOT prefix amounts with $" not in out


def test_constraints_inr_emits_no_dollar_directive():
    out = _format_constraints(currency="INR", travel_month=None, origin="Mumbai")
    assert "INR" in out
    assert "Do NOT prefix amounts with $" in out
    assert "Mumbai" in out


def test_constraints_with_month():
    out = _format_constraints(currency="EUR", travel_month="July", origin="Paris")
    assert "July" in out
    assert "EUR" in out
    assert "Paris" in out


def test_constraints_no_origin_disables_flight_invention():
    out = _format_constraints(currency="USD", travel_month=None, origin=None)
    assert "NOT PROVIDED" in out
    assert "invent flights" in out or "Do NOT invent" in out


# ── _format_prefs ──────────────────────────────────────────────────────────

def test_format_prefs_empty_returns_default_message():
    out = _format_prefs({})
    assert "no preferences provided" in out.lower()


def test_format_prefs_translates_ids_to_labels():
    prefs = {"flight": "cheapest", "hotel": "comfort", "pace": "relaxed"}
    out = _format_prefs(prefs)
    assert "Cheapest" in out
    assert "Comfort" in out
    assert "Relaxed" in out.lower() or "Relaxed" in out


def test_format_prefs_unknown_id_falls_back_to_raw_value():
    out = _format_prefs({"flight": "weirdvalue"})
    assert "weirdvalue" in out


def test_prefs_options_has_three_categories():
    assert set(PREFS_OPTIONS.keys()) == {"flight", "hotel", "pace"}
    for cat, opts in PREFS_OPTIONS.items():
        assert len(opts) >= 3
        assert all("id" in o and "label" in o for o in opts)


# ── _parse_critic_json ─────────────────────────────────────────────────────

def test_critic_parse_bare_json():
    raw = '{"approved": true, "issues": [], "critique": "looks good"}'
    out = _parse_critic_json(raw)
    assert out == {"approved": True, "issues": [], "critique": "looks good"}


def test_critic_parse_code_fenced_json():
    raw = '```json\n{"approved": false, "issues": ["over budget"], "critique": "bad"}\n```'
    out = _parse_critic_json(raw)
    assert out is not None
    assert out["approved"] is False
    assert out["issues"] == ["over budget"]


def test_critic_parse_prose_around_object():
    raw = 'Here is my review:\n{"approved": true, "issues": [], "critique": "ok"}\nThanks!'
    out = _parse_critic_json(raw)
    assert out is not None
    assert out["approved"] is True


def test_critic_parse_returns_none_for_garbage():
    assert _parse_critic_json("not json at all") is None
    assert _parse_critic_json("") is None


def test_critic_parse_returns_none_for_json_array():
    # We only accept dicts as verdicts.
    assert _parse_critic_json("[1, 2, 3]") is None


# ── _parse_tavily_mcp_result ───────────────────────────────────────────────

def test_tavily_parse_results_dict():
    raw = json.dumps({"results": [
        {"title": "A", "content": "alpha", "url": "https://a"},
        {"title": "B", "content": "beta", "url": "https://b"},
    ]})
    out = _parse_tavily_mcp_result(raw)
    assert len(out) == 2
    assert out[0]["title"] == "A"


def test_tavily_parse_bare_list():
    raw = json.dumps([{"title": "X", "content": "y", "url": "https://x"}])
    out = _parse_tavily_mcp_result(raw)
    assert out == [{"title": "X", "content": "y", "url": "https://x"}]


def test_tavily_parse_empty_returns_empty():
    assert _parse_tavily_mcp_result("") == []
    assert _parse_tavily_mcp_result("garbage") == []


def test_tavily_parse_dict_without_results_key_returns_empty():
    raw = json.dumps({"error": "rate limit"})
    assert _parse_tavily_mcp_result(raw) == []


def test_tavily_parse_extracts_embedded_object():
    raw = 'Preamble. {"results": [{"title":"X","content":"","url":""}]} Trailing text.'
    out = _parse_tavily_mcp_result(raw)
    assert len(out) == 1


# ── _jsonschema_to_gemini ──────────────────────────────────────────────────

def test_schema_translator_handles_none():
    assert _jsonschema_to_gemini(None) is None
    assert _jsonschema_to_gemini({}) is not None  # empty obj defaults to STRING


def test_schema_translator_primitive_string():
    out = _jsonschema_to_gemini({"type": "string", "description": "a query"})
    assert out.type == gtypes.Type.STRING
    assert out.description == "a query"


def test_schema_translator_object_with_properties():
    schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "search query"},
            "max_results": {"type": "integer"},
        },
        "required": ["query"],
    }
    out = _jsonschema_to_gemini(schema)
    assert out.type == gtypes.Type.OBJECT
    assert "query" in out.properties
    assert out.properties["query"].type == gtypes.Type.STRING
    assert out.properties["max_results"].type == gtypes.Type.INTEGER
    assert out.required == ["query"]


def test_schema_translator_array_with_items():
    schema = {"type": "array", "items": {"type": "string"}}
    out = _jsonschema_to_gemini(schema)
    assert out.type == gtypes.Type.ARRAY
    assert out.items.type == gtypes.Type.STRING


def test_schema_translator_enum():
    schema = {"type": "string", "enum": ["a", "b", "c"]}
    out = _jsonschema_to_gemini(schema)
    assert out.enum == ["a", "b", "c"]


def test_schema_translator_drops_invalid_required():
    # Properties missing the listed required field -> required filtered.
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "required": ["a", "b"],  # b not in properties
    }
    out = _jsonschema_to_gemini(schema)
    assert out.required == ["a"]


def test_schema_translator_nullable_type_list():
    # JSON Schema allows type as a list. Pick the non-null member.
    schema = {"type": ["string", "null"]}
    out = _jsonschema_to_gemini(schema)
    assert out.type == gtypes.Type.STRING


def test_schema_translator_unknown_type_defaults_to_string():
    schema = {"type": "unknown-thing"}
    out = _jsonschema_to_gemini(schema)
    assert out.type == gtypes.Type.STRING
