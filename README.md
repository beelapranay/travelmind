# TravelMind AI Agent

An agentic AI travel planner. Give it a natural-language trip request and it autonomously searches the web, then synthesizes a complete itinerary — streamed live to a polished web UI.

## What it does

> "Plan a 5-day trip to Tokyo for 2 people, $3000 total budget, flying from Boston in July"

The agent:
1. Searches flights, hotels, attractions, food, weather (live, via Tavily)
2. Synthesizes results into a structured day-by-day plan
3. Streams every tool call to the UI as it happens (Server-Sent Events)

## Stack

- **Backend**: FastAPI + SSE streaming
- **LLM**: Gemini 2.5 Flash (function calling)
- **Search**: Tavily Search API
- **Frontend**: Single-page app — Tailwind, glassmorphism, marked.js, no build step

## Setup

### 1. API keys (both free)
- Gemini: https://aistudio.google.com/app/apikey
- Tavily: https://tavily.com

### 2. Install
```bash
pip install -r requirements.txt
```

### 3. Run
```bash
uvicorn server:app --reload
```

Open http://localhost:8000, click **API Keys** to enter your keys (stored in `localStorage`), describe your trip, hit **Plan my trip**.

## Architecture

```
Browser ──POST /api/plan──▶ FastAPI
                              │
                              ▼
                    Gemini 2.5 Flash (orchestrator)
                              │
                              ├─ tool: web_search ──▶ Tavily ──▶ results
                              ├─ tool: web_search ──▶ Tavily ──▶ results
                              └─ ... (up to 10 iters)
                              │
        SSE stream ◀──────────┘  (tool_call, tool_result, done events)
```

The agent autonomously decides how many searches to run, what to search for next based on prior results, and when it has enough to write the final plan.

## Files

- `server.py` — FastAPI app, SSE endpoint, history endpoints
- `agent.py` — multi-agent orchestrator, sub-agents, CriticAgent, MCP wiring
- `mcp_client.py` — async MCPManager with sync wrapper for the threaded agents
- `static/index.html` + `app.js` + `styles.css` — frontend
- `Dockerfile`, `render.yaml` — deployment

## Deploy (Render)

1. Push this repo to GitHub.
2. On [render.com](https://render.com) → **New** → **Blueprint** → connect the repo → **Apply**. Render reads `render.yaml` and builds the Dockerfile.
3. Once live, open the URL, click **API Keys**, paste your Gemini + Tavily keys (and optionally Google Maps), and run a trip.

**Free tier notes:**
- The instance sleeps after 15 min of inactivity. First request after sleep takes ~30s to wake.
- 512 MB memory ceiling — Tavily + Filesystem MCPs fit comfortably. Adding Google Maps MCP is tight but usually works. If you see OOM kills, leave the Google Maps key blank.
- `data/plans/` is ephemeral — saved plans are wiped on every restart.

**Other platforms:** the Dockerfile is platform-agnostic. Railway / Fly.io / DigitalOcean App Platform all work — just point them at the repo or `Dockerfile`.
