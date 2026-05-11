"""TravelMind multi-agent core.

Orchestrator (PlannerAgent) dispatches three specialist sub-agents in parallel:
FlightAgent, HotelAgent, ItineraryAgent. Each sub-agent runs its own Gemini
function-calling loop with a Tavily web_search tool, then returns a markdown
section. The Planner finally synthesizes the sections into a unified plan.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Optional

from google import genai
from google.genai import types

from mcp_client import MCPManager

log = logging.getLogger("travelmind.agent")

PLANS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "data", "plans"))

GEMINI_MODEL = "gemini-2.5-flash"

# ── System prompts ──────────────────────────────────────────────────────────

FLIGHT_AGENT_PROMPT = """You are FlightAgent, a specialist in air travel research.

Your job: research flight options for the user's trip. Run 2 to 4 targeted web searches (origin to destination, dates/season, airlines, price ranges). Then output ONLY this markdown section:

## Flights

For each of 2-3 options, use this exact format:

**Option N — Airline(s)**
- Route: <origin> → <destination> (<layover city if any>)
- Price: ~$X per person
- Duration: Xh Ym total
- Notes: one short line (red-eye, baggage, etc.)

**Best pick:** one sentence naming the option number and why.

Rules: real airline names. Prices in USD. No prose paragraphs. No text outside `## Flights`."""

HOTEL_AGENT_PROMPT = """You are HotelAgent, a specialist in accommodation research.

Your job: research lodging within the user's stated budget. Run 2 to 4 targeted web searches (neighborhoods, hotel/Airbnb prices, reviews). Then output ONLY this markdown section:

## Accommodation

For each of 3 tiers (Budget / Comfort / Luxury), use this exact format:

**Tier — Property name**
- Neighborhood: <name>
- Rate: ~$X/night
- Why it fits: one short line

**Default pick:** one sentence naming the tier and why it matches the budget.

Rules: real property names. Prices in USD per night. No prose paragraphs. No text outside `## Accommodation`."""

ITINERARY_AGENT_PROMPT = """You are ItineraryAgent, a specialist in destination experiences.

Your job: research attractions, food, weather, transit. Run 3 to 5 targeted web searches. Then output ONLY this markdown section:

## Itinerary

For each day, use this exact format:

### Day N — <theme or neighborhood>
- Morning: <one short sentence, or a sub-list of 2-3 activities>
- Afternoon: <one short sentence, or a sub-list of 2-3 activities>
- Evening: <one short sentence, or a sub-list of 2-3 activities>
- Eat: <one specific restaurant>

After all days:

**Weather & packing:** 2-3 short bullets — typical temps for the trip month, rain risk, what to bring.

Rules:
- Use ONLY the labels "Morning", "Afternoon", "Evening", "Eat". Do NOT add parenthesized time ranges like "(8:00 AM - 12:00 PM)" after the label.
- Real place names, real restaurants.
- No prose paragraphs. No text outside `## Itinerary`."""

PLANNER_SYNTHESIS_PROMPT = """You are PlannerAgent, the orchestrator. Three specialists produced research sections. Compose the final plan.

Output a single markdown document in this exact order. NO preamble, NO commentary outside the structure below.

## Trip Summary
- Destination: <city/country>
- Dates: <month/duration>
- Travelers: <count>
- Budget: $<total> total

(Exactly these 4 bullets. Do NOT add notes, caveats, or extra bullets inside Trip Summary. Caveats go in the `## Reality Check` section only if absolutely required.)

## Flights
(insert FlightAgent's section verbatim, keeping its bullets)

## Accommodation
(insert HotelAgent's section verbatim, keeping its bullets)

## Itinerary
(insert ItineraryAgent's section verbatim, keeping its day blocks)

## Budget Breakdown

| Category | Estimated Cost | Notes |
|---|---|---|
| Flights | $X | per person × N |
| Lodging | $X | N nights |
| Food | $X | ~$Y/day |
| Activities | $X | entries, tours |
| Transit | $X | local |
| Buffer | $X | 10% |
| **Total** | **$X** | vs budget $Y |

If the realistic total exceeds the stated budget, add one bold line below the table: **Over budget by $X.** Otherwise add **Within budget.**

Rules: do not invent new flights, hotels, or attractions. Only restructure and add Trip Summary + Budget Breakdown. No prose explanations."""


CRITIC_AGENT_PROMPT = """You are CriticAgent, a quality reviewer for travel plans.

You will receive the original user request and a generated travel plan. Review the plan against the user's constraints and surface concrete, actionable issues. Be strict but fair.

Check for:
- **Budget**: Does the Budget Breakdown sum realistically match the user's stated budget? Is anything understated (flights, food per day, transit)?
- **Feasibility**: Is any day overloaded? Are activities geographically clustered, or does the route zigzag? Are opening hours / day-of-week constraints respected for major attractions?
- **Completeness**: Are flights, lodging, daily activities, food, weather all covered? Any glaring gap?
- **Preference fit**: Do the picks reflect the user's flight/hotel/pace preferences if provided?

Output ONLY valid JSON, no markdown fence, in this exact shape:
{
  "approved": true | false,
  "issues": ["short concrete issue 1", "short concrete issue 2"],
  "critique": "one or two sentence summary of the overall quality"
}

Rules:
- "approved": true only if there are no material issues. Cosmetic nits do not block approval.
- "issues": empty list if approved. Otherwise 1-4 concrete, fixable items. Each item under 20 words.
- "critique": always present. Honest one-line verdict."""


PLANNER_REVISION_PROMPT = """You are PlannerAgent revising a plan based on critic feedback.

Inputs: original request, previous plan, list of CriticAgent issues.

Output the revised plan in the EXACT same markdown structure as before:

## Trip Summary
## Flights
## Accommodation
## Itinerary
## Budget Breakdown

Then ONE final section if and only if the budget is genuinely impossible to meet:

## Reality Check
- Budget short by ~$X
- Options to fix (3 short bullets): cut days, raise budget, change destination, etc.

Rules:
- NO preamble before `## Trip Summary`. NO commentary outside the structured sections.
- Fix every critic issue. Keep what was good.
- Bullets and tables only. No long prose paragraphs.
- Do not invent new flights, hotels, or attractions unless an issue specifically requires it."""


# ── Tool definition ─────────────────────────────────────────────────────────

def _web_search_tool() -> list[types.Tool]:
    return [
        types.Tool(
            function_declarations=[
                types.FunctionDeclaration(
                    name="web_search",
                    description="Search the web for real-time travel information.",
                    parameters=types.Schema(
                        type=types.Type.OBJECT,
                        properties={
                            "query": types.Schema(
                                type=types.Type.STRING,
                                description="Specific search query.",
                            )
                        },
                        required=["query"],
                    ),
                )
            ]
        )
    ]


# ── Event protocol ──────────────────────────────────────────────────────────

EventCallback = Callable[[str, dict], None]
"""Signature: on_event(event_type, payload).

Event types:
  agent_status:  {agent, status}              status in {running, done, error}
  tool_call:     {agent, query}
  tool_result:   {agent, query, summary, sources_count, source}
  section_done:  {agent, section}
  prefs_request: {options}                    blocks awaiting user input
  critique:      {approved, issues, critique}
  final_plan:    {plan, revised?}
  mcp_status:    {server, status, tools?, message?}
  plan_saved:    {path, via}                  via in {mcp, direct}
"""

PrefsRequester = Callable[[dict], dict]
"""request_prefs(options) -> prefs. Blocks until user submits."""

PREFS_OPTIONS = {
    "flight": [
        {"id": "cheapest",      "label": "Cheapest"},
        {"id": "fastest",       "label": "Fastest"},
        {"id": "best_layover",  "label": "Best layover"},
    ],
    "hotel": [
        {"id": "budget",   "label": "Budget"},
        {"id": "comfort",  "label": "Comfort"},
        {"id": "luxury",   "label": "Luxury"},
    ],
    "pace": [
        {"id": "packed",    "label": "Packed (8+ activities/day)"},
        {"id": "balanced",  "label": "Balanced"},
        {"id": "relaxed",   "label": "Relaxed (3 to 4/day)"},
    ],
}


@dataclass
class SubAgentResult:
    name: str
    section: str
    tool_calls: list[dict] = field(default_factory=list)


# ── Sub-agent runner ────────────────────────────────────────────────────────

def _perform_web_search(
    *,
    query: str,
    mcp: Optional[MCPManager],
    tavily_client,
) -> tuple[str, str, int, str]:
    """Run a single web search. Returns (formatted_content, summary, sources_count, source).

    Tries MCP Tavily server first; falls back to direct Tavily SDK on any failure.
    `source` is one of {"mcp", "sdk"} for telemetry.
    """
    if mcp is not None and mcp.has_server("tavily"):
        try:
            tool_name = _resolve_tavily_tool(mcp)
            raw = mcp.call_tool("tavily", tool_name, {"query": query, "max_results": 5, "search_depth": "advanced"}, timeout=45)
            results_list = _parse_tavily_mcp_result(raw)
            if results_list:
                formatted = [
                    f"**{r.get('title','')}**\n{(r.get('content') or '')[:300]}\nSource: {r.get('url','')}"
                    for r in results_list
                ]
                return (
                    "\n\n".join(formatted),
                    (results_list[0].get("content") or "")[:150],
                    len(results_list),
                    "mcp",
                )
            # If parsing yields nothing, surface raw text to the model instead of erroring.
            if raw:
                return (raw[:2000], raw[:150], 1, "mcp")
        except Exception as e:
            log.warning("MCP tavily call failed, falling back to SDK: %s", e)

    # SDK fallback
    result = tavily_client.search(query, max_results=5, search_depth="advanced")
    results_list = result.get("results", [])
    formatted = [
        f"**{r.get('title','')}**\n{(r.get('content') or '')[:300]}\nSource: {r.get('url','')}"
        for r in results_list
    ]
    return (
        "\n\n".join(formatted) if formatted else "No results found.",
        (results_list[0].get("content") or "")[:150] if results_list else "No data",
        len(results_list),
        "sdk",
    )


def _resolve_tavily_tool(mcp: MCPManager) -> str:
    """Pick the right search-tool name from the Tavily MCP server."""
    tools = mcp.list_tools("tavily")
    names = [t["name"] for t in tools]
    for candidate in ("tavily-search", "tavily_search", "search"):
        if candidate in names:
            return candidate
    # Fall back to the first tool whose name contains 'search'.
    for n in names:
        if "search" in n.lower():
            return n
    raise RuntimeError(f"No search tool exposed by Tavily MCP. Available: {names}")


def _parse_tavily_mcp_result(raw: str) -> list[dict]:
    """Tavily MCP returns text content. Try JSON first; otherwise return empty."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        # Some MCP servers wrap JSON in a code fence or prefix; try to extract.
        m = re.search(r"\{[\s\S]*\}|\[[\s\S]*\]", raw)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
        except Exception:
            return []
    if isinstance(data, dict):
        if isinstance(data.get("results"), list):
            return data["results"]
        return []
    if isinstance(data, list):
        return data
    return []


def _run_sub_agent(
    *,
    name: str,
    system_prompt: str,
    user_query: str,
    gemini_client,
    tavily_client,
    mcp: Optional[MCPManager],
    on_event: EventCallback,
    max_iterations: int = 6,
) -> SubAgentResult:
    contents = [
        types.Content(role="user", parts=[types.Part.from_text(text=user_query)]),
    ]
    tool_calls: list[dict] = []

    on_event("agent_status", {"agent": name, "status": "running"})

    try:
        for iteration in range(1, max_iterations + 1):
            sys_msg = system_prompt
            if iteration == max_iterations:
                sys_msg += "\n\nIMPORTANT: Search budget exhausted. Output the final markdown section now using what you have. Do NOT call web_search again."

            response = gemini_client.models.generate_content(
                model=GEMINI_MODEL,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=sys_msg,
                    max_output_tokens=4096,
                    tools=_web_search_tool() if iteration < max_iterations else None,
                ),
            )

            function_calls = response.function_calls or []

            if not function_calls:
                section = response.text or ""
                on_event("section_done", {"agent": name, "section": section})
                on_event("agent_status", {"agent": name, "status": "done"})
                return SubAgentResult(name=name, section=section, tool_calls=tool_calls)

            model_content = response.candidates[0].content if response.candidates else None
            if model_content is None:
                break
            contents.append(model_content)

            response_parts = []
            for call in function_calls:
                if call.name != "web_search":
                    continue
                query = (call.args or {}).get("query", "")

                on_event("tool_call", {"agent": name, "query": query})

                try:
                    search_content, summary, sources_count, source = _perform_web_search(
                        query=query, mcp=mcp, tavily_client=tavily_client,
                    )
                except Exception as e:
                    search_content = f"Search failed: {e}"
                    summary = "Search failed"
                    sources_count = 0
                    source = "error"

                on_event("tool_result", {
                    "agent": name, "query": query,
                    "summary": summary, "sources_count": sources_count, "source": source,
                })

                tool_calls.append({"query": query, "summary": summary, "sources_count": sources_count, "source": source})

                response_parts.append(
                    types.Part.from_function_response(
                        name="web_search",
                        response={"result": search_content},
                    )
                )

            if response_parts:
                contents.append(types.Content(role="user", parts=response_parts))

    except Exception as e:
        on_event("agent_status", {"agent": name, "status": "error", "message": str(e)})
        return SubAgentResult(name=name, section=f"_{name} failed: {e}_", tool_calls=tool_calls)

    on_event("agent_status", {"agent": name, "status": "done"})
    return SubAgentResult(name=name, section="_(no output)_", tool_calls=tool_calls)


# ── Planner orchestrator ────────────────────────────────────────────────────

SUB_AGENTS = [
    ("flight", FLIGHT_AGENT_PROMPT),
    ("hotel", HOTEL_AGENT_PROMPT),
    ("itinerary", ITINERARY_AGENT_PROMPT),
]


def _format_prefs(prefs: dict) -> str:
    if not prefs:
        return "(no preferences provided; use sensible defaults)"
    parts = []
    for category, value in prefs.items():
        opts = PREFS_OPTIONS.get(category, [])
        label = next((o["label"] for o in opts if o["id"] == value), value)
        parts.append(f"- {category.capitalize()}: {label}")
    return "\n".join(parts)


def run_planner(
    *,
    user_query: str,
    gemini_key: str,
    tavily_key: str,
    on_event: EventCallback,
    request_prefs: Optional[PrefsRequester] = None,
) -> dict:
    """Run the full multi-agent planning pipeline.

    Returns: {plan, total_searches, total_sources, plan_path}
    """
    try:
        from tavily import TavilyClient
    except ImportError as e:
        raise ImportError("Run: pip install tavily-python") from e

    gemini_client = genai.Client(api_key=gemini_key)
    tavily_client = TavilyClient(api_key=tavily_key)

    # Spin up MCP servers for this job. Failures are non-fatal: sub-agents
    # fall back to the direct Tavily SDK and persistence is skipped.
    os.makedirs(PLANS_DIR, exist_ok=True)
    mcp = _init_mcp(tavily_key=tavily_key, on_event=on_event)

    on_event("agent_status", {"agent": "planner", "status": "running"})

    results: dict[str, SubAgentResult] = {}
    lock = threading.Lock()

    def safe_emit(event_type: str, payload: dict):
        with lock:
            on_event(event_type, payload)

    try:
        with ThreadPoolExecutor(max_workers=len(SUB_AGENTS)) as pool:
            futures = {
                pool.submit(
                    _run_sub_agent,
                    name=name,
                    system_prompt=prompt,
                    user_query=user_query,
                    gemini_client=gemini_client,
                    tavily_client=tavily_client,
                    mcp=mcp,
                    on_event=safe_emit,
                ): name
                for name, prompt in SUB_AGENTS
            }
            for fut in as_completed(futures):
                name = futures[fut]
                results[name] = fut.result()
    except Exception:
        if mcp is not None:
            mcp.close()
        raise

    # HITL gate: ask user for preferences before synthesis
    prefs: dict = {}
    if request_prefs is not None:
        prefs = request_prefs(PREFS_OPTIONS) or {}

    # Synthesize
    flight_sec = results.get("flight", SubAgentResult("flight", "")).section
    hotel_sec = results.get("hotel", SubAgentResult("hotel", "")).section
    itin_sec = results.get("itinerary", SubAgentResult("itinerary", "")).section

    synthesis_input = (
        f"Original request: {user_query}\n\n"
        f"User preferences:\n{_format_prefs(prefs)}\n\n"
        f"Apply the preferences when picking the headline flight, hotel default, and itinerary density.\n\n"
        f"=== FlightAgent output ===\n{flight_sec}\n\n"
        f"=== HotelAgent output ===\n{hotel_sec}\n\n"
        f"=== ItineraryAgent output ===\n{itin_sec}\n"
    )

    final_response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[types.Content(role="user", parts=[types.Part.from_text(text=synthesis_input)])],
        config=types.GenerateContentConfig(
            system_instruction=PLANNER_SYNTHESIS_PROMPT,
            max_output_tokens=8192,
        ),
    )

    final_plan = final_response.text or "Final synthesis returned empty."
    on_event("final_plan", {"plan": final_plan})
    on_event("agent_status", {"agent": "planner", "status": "done"})

    # CriticAgent: review plan, revise once if issues found.
    final_plan = _critique_and_revise(
        user_query=user_query,
        prefs=prefs,
        plan=final_plan,
        gemini_client=gemini_client,
        on_event=on_event,
    )

    # Persist via Filesystem MCP if available. Falls back to direct write.
    plan_path = _save_plan(
        mcp=mcp,
        user_query=user_query,
        prefs=prefs,
        plan=final_plan,
        on_event=on_event,
    )

    if mcp is not None:
        mcp.close()

    total_searches = sum(len(r.tool_calls) for r in results.values())
    total_sources = sum(c.get("sources_count", 0) for r in results.values() for c in r.tool_calls)

    return {
        "plan": final_plan,
        "total_searches": total_searches,
        "total_sources": total_sources,
        "plan_path": plan_path,
    }


# ── CriticAgent ─────────────────────────────────────────────────────────────

def _parse_critic_json(raw: str) -> Optional[dict]:
    """Robust parse for the critic's JSON verdict.

    Handles: bare JSON, ```json fences, leading/trailing prose, single-quote drift.
    Returns None if no usable object can be extracted.
    """
    if not raw:
        return None
    candidates: list[str] = [raw]
    fence = re.search(r"```(?:json)?\s*([\s\S]+?)```", raw)
    if fence:
        candidates.append(fence.group(1))
    obj = re.search(r"\{[\s\S]*\}", raw)
    if obj:
        candidates.append(obj.group(0))
    for c in candidates:
        c = c.strip()
        try:
            parsed = json.loads(c)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            continue
    return None


def _critique_and_revise(
    *,
    user_query: str,
    prefs: dict,
    plan: str,
    gemini_client,
    on_event: EventCallback,
) -> str:
    """Run CriticAgent on the plan. Revise once if not approved. Returns final plan."""
    on_event("agent_status", {"agent": "critic", "status": "running"})

    critic_input = (
        f"Original request: {user_query}\n\n"
        f"User preferences:\n{_format_prefs(prefs)}\n\n"
        f"=== Plan to review ===\n{plan}\n"
    )

    try:
        critic_response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[types.Content(role="user", parts=[types.Part.from_text(text=critic_input)])],
            config=types.GenerateContentConfig(
                system_instruction=CRITIC_AGENT_PROMPT,
                max_output_tokens=4096,
                response_mime_type="application/json",
            ),
        )
        raw = (critic_response.text or "").strip()
        finish_reason = None
        if critic_response.candidates:
            fr = getattr(critic_response.candidates[0], "finish_reason", None)
            finish_reason = str(fr) if fr is not None else None
        log.info("critic raw len=%d finish=%s", len(raw), finish_reason)
    except Exception as e:
        log.warning("Critic API call failed: %s", e)
        on_event("agent_status", {"agent": "critic", "status": "error", "message": f"API: {e}"})
        return plan

    verdict = _parse_critic_json(raw)
    if verdict is None:
        log.warning("Critic JSON unparseable. finish=%s len=%d raw[:800]=%r",
                    finish_reason, len(raw), raw[:800])
        msg = f"Could not parse critic JSON (finish={finish_reason}). Keeping original plan."
        on_event("agent_status", {"agent": "critic", "status": "error", "message": msg})
        return plan

    approved = bool(verdict.get("approved", False))
    issues = verdict.get("issues") or []
    critique_text = verdict.get("critique", "")

    on_event("critique", {
        "approved": approved,
        "issues": issues,
        "critique": critique_text,
    })

    if approved or not issues:
        on_event("agent_status", {"agent": "critic", "status": "done"})
        return plan

    # Revise once.
    on_event("agent_status", {"agent": "planner", "status": "running"})
    revision_input = (
        f"Original request: {user_query}\n\n"
        f"User preferences:\n{_format_prefs(prefs)}\n\n"
        f"=== Previous plan ===\n{plan}\n\n"
        f"=== Critic issues to fix ===\n"
        + "\n".join(f"- {i}" for i in issues)
    )

    try:
        revised = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[types.Content(role="user", parts=[types.Part.from_text(text=revision_input)])],
            config=types.GenerateContentConfig(
                system_instruction=PLANNER_REVISION_PROMPT,
                max_output_tokens=8192,
            ),
        )
        revised_plan = revised.text or plan
    except Exception as e:
        on_event("agent_status", {"agent": "critic", "status": "error", "message": str(e)})
        return plan

    on_event("final_plan", {"plan": revised_plan, "revised": True})
    on_event("agent_status", {"agent": "planner", "status": "done"})
    on_event("agent_status", {"agent": "critic", "status": "done"})
    return revised_plan


# ── MCP bootstrap + persistence ─────────────────────────────────────────────

def _init_mcp(*, tavily_key: str, on_event: EventCallback) -> Optional[MCPManager]:
    """Boot Tavily MCP + Filesystem MCP. Returns None if neither came up."""
    mcp = MCPManager()
    mcp.start()
    any_up = False

    base_env = {**os.environ}

    try:
        tools = mcp.add_server(
            name="tavily",
            command="npx",
            args=["-y", "tavily-mcp@latest"],
            env={**base_env, "TAVILY_API_KEY": tavily_key},
            timeout=60.0,
        )
        any_up = True
        on_event("mcp_status", {"server": "tavily", "status": "ready", "tools": [t["name"] for t in tools]})
    except Exception as e:
        log.warning("Tavily MCP did not start: %s", e)
        on_event("mcp_status", {"server": "tavily", "status": "error", "message": str(e)})

    try:
        tools = mcp.add_server(
            name="fs",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-filesystem", PLANS_DIR],
            env=base_env,
            timeout=60.0,
        )
        any_up = True
        on_event("mcp_status", {"server": "fs", "status": "ready", "tools": [t["name"] for t in tools]})
    except Exception as e:
        log.warning("Filesystem MCP did not start: %s", e)
        on_event("mcp_status", {"server": "fs", "status": "error", "message": str(e)})

    if not any_up:
        mcp.close()
        return None
    return mcp


def _save_plan(
    *,
    mcp: Optional[MCPManager],
    user_query: str,
    prefs: dict,
    plan: str,
    on_event: EventCallback,
) -> Optional[str]:
    """Save plan + metadata as JSON. Prefers Filesystem MCP, falls back to direct write."""
    ts = time.strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "-", user_query.lower())[:40].strip("-") or "trip"
    filename = f"{ts}-{slug}.json"
    payload = json.dumps(
        {"created_at": ts, "query": user_query, "prefs": prefs, "plan": plan},
        indent=2,
    )

    full_path = os.path.join(PLANS_DIR, filename)

    if mcp is not None and mcp.has_server("fs"):
        try:
            mcp.call_tool("fs", "write_file", {"path": full_path, "content": payload}, timeout=15)
            on_event("plan_saved", {"path": full_path, "via": "mcp"})
            return full_path
        except Exception as e:
            log.warning("Filesystem MCP write failed, falling back: %s", e)

    try:
        with open(full_path, "w") as f:
            f.write(payload)
        on_event("plan_saved", {"path": full_path, "via": "direct"})
        return full_path
    except Exception as e:
        log.warning("plan save failed entirely: %s", e)
        return None
