# TravelMind v2 — Architecture Plan

## Core Design Philosophy
One orchestrator, multiple specialists, one human checkpoint. The agent does real research in parallel, stops to confirm preferences, then produces a personalized final plan. Every step is visible on screen.

---

## Agentic Patterns Used

### 1. Orchestrator + Specialized Sub-Agents
A top-level **PlannerAgent** receives the user's trip request and breaks it into parallel workstreams. It spawns three specialized agents simultaneously, collects their outputs, and synthesizes the final plan.

```
User Input
    │
    ▼
┌─────────────────────────────┐
│       PlannerAgent          │  ← Orchestrator
│  (decomposes + synthesizes) │
└────────────┬────────────────┘
             │ spawns in parallel
    ┌────────┼────────────┐
    ▼        ▼            ▼
FlightAgent  HotelAgent  ItineraryAgent
(searches    (searches   (searches attractions,
 routes,      listings,   food, local tips,
 prices,      prices,     logistics, weather)
 airlines)    amenities)
    │        │            │
    └────────┴────────────┘
             │ reports back
             ▼
       PlannerAgent receives all results
             │
             ▼
    [HUMAN-IN-THE-LOOP GATE]
```

**Why it matters for the demo:** Three agent panels firing simultaneously. Mondee can see parallelization, specialization, and orchestration — all the patterns they care about — in one shot.

---

### 2. Human-in-the-Loop (HITL) Gate
After sub-agents complete their research, the orchestrator **pauses** and presents a structured preference checkpoint to the user before generating the final plan.

```
Sub-agents complete research
        │
        ▼
PlannerAgent surfaces options:

  ✈️  Flight preference?
      [ ] Cheapest  [ ] Fastest  [ ] Best layover city

  🏨  Hotel style?
      [ ] Budget ($60–90/night)  [ ] Comfort ($120–160/night)  [ ] Luxury ($200+)

  📅  Trip pace?
      [ ] Packed (8+ activities/day)  [ ] Balanced  [ ] Relaxed (3–4/day)

        │  user responds
        ▼
PlannerAgent resumes with preferences locked in
        │
        ▼
  Final personalized itinerary
```

**Why it matters for the demo:** This is the moment that separates an agent from a script. The pause proves the system is stateful, context-aware, and responsive to human input mid-workflow — not just a one-shot prompt.

---

### 3. Evaluator-Optimizer Loop (Post-Plan)
After the final plan is generated, a **CriticAgent** reviews it against the user's stated constraints and flags issues. The PlannerAgent revises based on the critique.

```
Final Plan Generated
        │
        ▼
CriticAgent reviews:
  - Is total budget within range?
  - Is Day 3 logistically feasible?
  - Are opening hours accounted for?
        │
        ▼ (if issues found)
Critique: "Day 2 is overloaded. Budget is $240 over limit."
        │
        ▼
PlannerAgent revises plan
        │
        ▼
Final validated itinerary ✅
```

**Why it matters for the demo:** The agent visibly fixes its own mistakes. This demonstrates reasoning quality and reliability — exactly what an AI-first travel platform needs at scale.

---

## MCP Integrations

| MCP Server | What the Agent Gets | Which Sub-Agent Uses It |
|---|---|---|
| **Tavily MCP** | Real-time web search across all travel content | All three sub-agents |
| **Brave Search MCP** | Secondary search source, less SEO-polluted results | FlightAgent, ItineraryAgent |
| **Google Maps MCP** | Real coordinates, walking distances, transit times, place details | ItineraryAgent |
| **Airbnb MCP** | Actual listing names, prices, availability, locations | HotelAgent |
| **Filesystem MCP** | Save/load past itineraries, user preference history | PlannerAgent |

### Why MCP over hardcoded tools
With hardcoded tools, every new API requires you to write the schema, the client, and the error handling. With MCP, the agent discovers and calls tools through a standardized protocol. Adding a new data source (e.g. a Booking.com MCP) becomes a one-line config change, not a code rewrite. This is the architecture that scales to hundreds of travel APIs — which is exactly Mondee's problem space.

---

## Full System Architecture

```
User: "5 days in Tokyo, 2 people, $3000, from Boston in July"
        │
        ▼
┌────────────────────────────────────────────┐
│              PlannerAgent                  │
│  - Parses intent and constraints           │
│  - Decomposes into parallel workstreams    │
│  - Manages MCP connections                 │
└──────────────┬─────────────────────────────┘
               │ parallel dispatch
    ┌──────────┼──────────────┐
    ▼          ▼              ▼
┌─────────┐ ┌─────────┐ ┌────────────────┐
│ Flight  │ │  Hotel  │ │  Itinerary     │
│ Agent   │ │  Agent  │ │  Agent         │
│         │ │         │ │                │
│ Tavily  │ │ Airbnb  │ │ Tavily         │
│ Brave   │ │ Tavily  │ │ Google Maps    │
│ Search  │ │         │ │ Brave Search   │
└────┬────┘ └────┬────┘ └───────┬────────┘
     │           │              │
     └───────────┴──────────────┘
                 │ results collected
                 ▼
    ┌────────────────────────────┐
    │    HITL Preference Gate    │
    │  Flight / Hotel / Pace     │
    └────────────┬───────────────┘
                 │ user confirms
                 ▼
    ┌────────────────────────────┐
    │       PlannerAgent         │
    │   synthesizes final plan   │
    └────────────┬───────────────┘
                 │
                 ▼
    ┌────────────────────────────┐
    │       CriticAgent          │
    │  validates budget + logic  │
    └────────────┬───────────────┘
                 │ revise if needed
                 ▼
    ┌────────────────────────────┐
    │   Filesystem MCP           │
    │   saves plan + prefs       │
    └────────────────────────────┘
                 │
                 ▼
         ✅ Final Itinerary
```

---

## UI Layout (Streamlit)

```
┌─────────────────────────────────────────────────────────────┐
│  ✈️ TravelMind                                    [sidebar]  │
├──────────────────────┬──────────────────────────────────────┤
│  🤖 Agent Activity   │  📋 Output                           │
│                      │                                      │
│  [FlightAgent]       │  [HITL gate appears here mid-run]   │
│  🔍 Boston→Tokyo...  │                                      │
│  🔍 JAL vs ANA...    │  [Final plan appears after HITL]    │
│                      │                                      │
│  [HotelAgent]        │                                      │
│  🔍 Shinjuku hotels  │                                      │
│  🔍 Airbnb Tokyo...  │                                      │
│                      │                                      │
│  [ItineraryAgent]    │                                      │
│  🔍 Tokyo 5 day...   │                                      │
│  🔍 Google Maps...   │                                      │
│                      │                                      │
│  [CriticAgent]       │                                      │
│  ⚠️ Day 3 revised    │                                      │
└──────────────────────┴──────────────────────────────────────┘
```

---

## Demo Script (90–120 seconds)

| Time | What to show |
|---|---|
| 0–10s | Type the trip request, hit Plan My Trip |
| 10–35s | Three agent panels fire simultaneously — narrate parallelization |
| 35–55s | HITL gate appears — pick hotel style and pace preference |
| 55–80s | Final plan generates with preferences applied |
| 80–100s | CriticAgent revision fires, show one bullet getting corrected |
| 100–115s | Scroll final itinerary — highlight budget breakdown and day-by-day |

---

## Why This Clears the Bar

Mondee is building agentic infrastructure for travel at scale. This demo shows:
- **Orchestration** — a planner that delegates intelligently
- **Parallelization** — three agents working simultaneously
- **Real tool use** — MCP-connected APIs returning live data
- **Human-in-the-loop** — stateful mid-workflow pause, not one-shot generation
- **Self-correction** — an evaluator that catches and fixes errors
- **Extensibility** — MCP means adding a new travel API is config, not code

That is the architecture of a production system, not a hackathon demo.
