"""TravelMind multi-agent core.

Orchestrator (PlannerAgent) dispatches three specialist sub-agents in parallel:
FlightAgent, HotelAgent, ItineraryAgent. Each sub-agent runs its own Gemini
function-calling loop with a Tavily web_search tool, then returns a markdown
section. The Planner finally synthesizes the sections into a unified plan.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Optional

from google import genai
from google.genai import types

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
- Morning: <activity / place>
- Afternoon: <activity / place>
- Evening: <activity / place>
- Eat: <one specific restaurant>

After all days:

**Weather & packing:** 2-3 short bullets — typical temps for the trip month, rain risk, what to bring.

Rules: real place names, real restaurants. No prose paragraphs. No text outside `## Itinerary`."""

PLANNER_SYNTHESIS_PROMPT = """You are PlannerAgent, the orchestrator. Three specialists produced research sections. Compose the final plan.

Output a single markdown document in this exact order. NO preamble, NO commentary outside the structure below.

## Trip Summary
- Destination: <city/country>
- Dates: <month/duration>
- Travelers: <count>
- Budget: $<total> total

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
  tool_result:   {agent, query, summary, sources_count}
  section_done:  {agent, section}
  prefs_request: {options}                    blocks awaiting user input
  critique:      {approved, issues, critique}
  final_plan:    {plan, revised?}
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

def _run_sub_agent(
    *,
    name: str,
    system_prompt: str,
    user_query: str,
    gemini_client,
    tavily_client,
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
                    result = tavily_client.search(query, max_results=5, search_depth="advanced")
                    results_list = result.get("results", [])
                    formatted = []
                    for r in results_list:
                        title = r.get("title", "")
                        content = r.get("content", "")[:300]
                        url = r.get("url", "")
                        formatted.append(f"**{title}**\n{content}\nSource: {url}")
                    search_content = "\n\n".join(formatted) if formatted else "No results found."
                    summary = results_list[0].get("content", "")[:150] if results_list else "No data"
                    sources_count = len(results_list)
                except Exception as e:
                    search_content = f"Search failed: {e}"
                    summary = "Search failed"
                    sources_count = 0

                on_event("tool_result", {
                    "agent": name, "query": query,
                    "summary": summary, "sources_count": sources_count,
                })

                tool_calls.append({"query": query, "summary": summary, "sources_count": sources_count})

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

    Returns: {plan, total_searches, total_sources}
    """
    try:
        from tavily import TavilyClient
    except ImportError as e:
        raise ImportError("Run: pip install tavily-python") from e

    gemini_client = genai.Client(api_key=gemini_key)
    tavily_client = TavilyClient(api_key=tavily_key)

    on_event("agent_status", {"agent": "planner", "status": "running"})

    results: dict[str, SubAgentResult] = {}
    lock = threading.Lock()

    def safe_emit(event_type: str, payload: dict):
        with lock:
            on_event(event_type, payload)

    with ThreadPoolExecutor(max_workers=len(SUB_AGENTS)) as pool:
        futures = {
            pool.submit(
                _run_sub_agent,
                name=name,
                system_prompt=prompt,
                user_query=user_query,
                gemini_client=gemini_client,
                tavily_client=tavily_client,
                on_event=safe_emit,
            ): name
            for name, prompt in SUB_AGENTS
        }
        for fut in as_completed(futures):
            name = futures[fut]
            results[name] = fut.result()

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

    total_searches = sum(len(r.tool_calls) for r in results.values())
    total_sources = sum(c.get("sources_count", 0) for r in results.values() for c in r.tool_calls)

    return {
        "plan": final_plan,
        "total_searches": total_searches,
        "total_sources": total_sources,
    }


# ── CriticAgent ─────────────────────────────────────────────────────────────

def _critique_and_revise(
    *,
    user_query: str,
    prefs: dict,
    plan: str,
    gemini_client,
    on_event: EventCallback,
) -> str:
    """Run CriticAgent on the plan. Revise once if not approved. Returns final plan."""
    import json as _json

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
                max_output_tokens=2048,
                response_mime_type="application/json",
            ),
        )
        raw = (critic_response.text or "").strip()
        verdict = _json.loads(raw)
        approved = bool(verdict.get("approved", False))
        issues = verdict.get("issues") or []
        critique_text = verdict.get("critique", "")
    except Exception as e:
        on_event("agent_status", {"agent": "critic", "status": "error", "message": str(e)})
        return plan

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
