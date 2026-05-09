# ✈️ TravelMind AI Agent

An agentic AI travel planner. Give it a natural-language trip request and it autonomously searches the web, then synthesizes a complete itinerary — streamed live to a polished web UI.

## What it does

> "Plan a 5-day trip to Tokyo for 2 people, $3000 total budget, flying from Boston in July"

The agent:
1. 🔍 Searches flights, hotels, attractions, food, weather (live, via Tavily)
2. 📋 Synthesizes results into a structured day-by-day plan
3. ⚡ Streams every tool call to the UI as it happens (Server-Sent Events)

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

Open http://localhost:8000 — click ⚙ to enter your keys (stored in `localStorage`), describe your trip, hit **Plan my trip**.

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

- `server.py` — FastAPI app, SSE endpoint
- `agent.py` — Gemini agent loop, Tavily tool
- `static/index.html` + `app.js` + `styles.css` — frontend
