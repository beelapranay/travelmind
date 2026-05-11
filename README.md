# TravelMind

[![CI](https://github.com/beelapranay/travelmind/actions/workflows/ci.yml/badge.svg)](https://github.com/beelapranay/travelmind/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776ab.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-009688.svg)](https://fastapi.tiangolo.com/)
[![MCP](https://img.shields.io/badge/MCP-Model%20Context%20Protocol-7c5cff.svg)](https://modelcontextprotocol.io/)

An autonomous, multi-agent AI travel planner. Describe a trip in plain English — TravelMind dispatches specialized agents in parallel, researches flights, lodging, and day-by-day activities from live web sources and Google Maps, pauses to confirm your preferences, self-critiques its own plan, then streams the final itinerary live into your browser.

```
"5 days in Tokyo for 2 people, $3000 budget, from Boston in July"
                         │
                         ▼
          ┌──────────────────────────────┐
          │       PlannerAgent           │
          │   (parses + orchestrates)    │
          └──────────────┬───────────────┘
                         │  parallel dispatch
        ┌────────────────┼────────────────┐
        ▼                ▼                ▼
   FlightAgent       HotelAgent      ItineraryAgent
   (web_search)     (web_search)   (web_search, maps_search_places,
                                    maps_distance_matrix)
        │                │                │
        └────────────────┼────────────────┘
                         ▼
              [HITL preference gate]
                         ▼
                PlannerAgent synthesizes
                         ▼
                    CriticAgent
                  (revise if needed)
                         ▼
              Final plan streamed to UI
```

---

## What it does

1. **Decomposes** your request into three parallel research workstreams.
2. **Searches** the live web (Tavily) and Google Maps for real flights, hotels, attractions, restaurants, and weather.
3. **Pauses** for a human-in-the-loop preference checkpoint — pick a flight priority, hotel tier, and trip pace.
4. **Synthesizes** all findings into a single markdown plan with a budget breakdown table.
5. **Reviews** the plan with a CriticAgent that returns a JSON verdict. If budget is over or a day is overloaded, the planner revises once.
6. **Streams** every tool call, every search result, and the final plan token-by-token into a live UI.

Every step is visible on screen as it happens — no opaque "loading" screen.

---

## Features

| | |
|---|---|
| **Multi-agent orchestration** | 1 planner + 3 specialists (flight, hotel, itinerary) + 1 critic. |
| **Parallel research** | Sub-agents run concurrently in a thread pool. ~3× faster than sequential. |
| **HITL preference gate** | The plan pauses mid-pipeline and waits for your input. |
| **Self-correcting loop** | CriticAgent returns `{approved, issues, critique}`. Planner revises once on failure. |
| **MCP integration** | Tavily search, Filesystem persistence, and Google Maps wired via Model Context Protocol — tool schemas auto-translate to Gemini function declarations. |
| **Streaming synthesis** | Final plan paints token-by-token via Gemini `generate_content_stream` + SSE. |
| **Currency + travel month** | First-class selectors. Currency flows into the budget table; month into seasonality searches. |
| **Export & share** | Download as Markdown, print to PDF, or share via `/p/{plan_id}` link. |
| **Live agent activity** | Every tool call streamed to a 4-lane UI (Flight / Hotel / Itinerary / Critic). |

---

## Tech stack

| Layer | Choice |
|---|---|
| LLM | Gemini 2.5 Flash (function calling + streaming) |
| Web search | Tavily (via MCP, with SDK fallback) |
| Maps | Google Maps Platform (via MCP) |
| Persistence | Filesystem MCP, ephemeral JSON files |
| Backend | FastAPI + Server-Sent Events |
| Concurrency | `ThreadPoolExecutor` for sub-agents, async loop in a background thread for MCP |
| Frontend | Vanilla JS SPA, Tailwind CDN, marked.js. No build step. |
| Deploy | Docker on Render (free tier compatible) |

---

## Quick start (local)

You need: Python 3.11+, Node 20+ (for `npx`-spawned MCP servers).

```bash
git clone https://github.com/beelapranay/travelmind.git
cd travelmind
pip install -r requirements.txt
uvicorn server:app --reload
```

Open `http://localhost:8000`, click **API Keys**, paste:

- **Gemini API key** — https://aistudio.google.com/app/apikey (free)
- **Tavily API key** — https://tavily.com (free)
- **Google Maps API key** — optional. https://console.cloud.google.com/google/maps-apis/credentials

Keys live in `localStorage` and are sent only to the corresponding providers. They never touch the server's environment.

Describe your trip, hit **Plan my trip**, and watch.

---

## Deploy on Render

1. Push this repo to GitHub.
2. On [render.com](https://render.com) → **New** → **Blueprint** → connect the repo → **Apply**. Render reads `render.yaml` and builds the Dockerfile (the first build prewarms all MCP packages so the first request after deploy is fast).
3. Open the live URL, enter your API keys in Settings, and run a trip.

**Free tier notes:**
- Instance sleeps after 15 minutes of inactivity. First hit after sleep takes ~30 seconds to wake.
- 512 MB RAM ceiling — Tavily + Filesystem MCPs fit comfortably; adding Google Maps MCP is tight but generally fine. If you hit OOM kills, leave the Maps key blank.
- `data/plans/` is ephemeral. Saved plans and `/p/{id}` share links are wiped on restart. Attach a Render disk for persistence.

The `Dockerfile` is platform-agnostic — Railway, Fly.io, DigitalOcean App Platform all work identically.

---

## Project structure

```
travelmind/
├── server.py              FastAPI app, SSE endpoint, history routes, share routes
├── agent.py               Orchestrator, sub-agents, critic, MCP bootstrap, streaming synthesis
├── mcp_client.py          Async MCPManager with sync wrappers for the threaded agents
├── requirements.txt       Python dependencies
├── Dockerfile             Python 3.11 + Node 20 + prewarmed MCP packages
├── render.yaml            Render blueprint
├── static/
│   ├── index.html         SPA shell, settings modal, agent activity + plan layout
│   ├── app.js             SSE consumer, lane rendering, markdown post-processing
│   ├── styles.css         Glassmorphism, lanes, day/option cards, print stylesheet
│   └── favicon.svg        Gradient TM mark
└── data/plans/            Ephemeral JSON files written by Filesystem MCP
```

---

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/plan` | Start a planning job. Returns SSE stream. |
| `POST` | `/api/plan/{job_id}/prefs` | Submit HITL preferences (unblocks the pipeline). |
| `GET` | `/api/history` | List saved plans (metadata). |
| `GET` | `/api/history/{plan_id}` | Fetch a saved plan in full. |
| `GET` | `/p/{plan_id}` | Public share page. Renders the SPA in read-only mode. |
| `GET` | `/` | Single-page app. |

---

## Roadmap

- **Airbnb MCP** — HotelAgent picks real listings.
- **Brave Search MCP** — secondary search for FlightAgent and ItineraryAgent.
- **Travel-time injection** — parse `maps_distance_matrix` into inline "12 min walk" hints between itinerary stops.
- **Multi-turn refinement** — "make Day 3 lighter" chat back over the existing research.
- **Redis-backed job registry** — drop the in-memory `_JOBS` dict, scale to multiple workers.
- **Auth + saved trips per user** — Supabase or Auth0.

---

## License

MIT.
