# ✈️ TravelMind AI Agent

An agentic AI travel planner that autonomously searches the web, makes decisions, and synthesizes a complete trip plan from a single natural language request.

## What it does

Give it a trip request like:
> "Plan a 5-day trip to Tokyo for 2 people, $3000 total budget, flying from Boston in July"

The agent will:
1. 🔍 Search for real-time flight options and prices
2. 🔍 Search for hotels within the budget
3. 🔍 Search for top attractions and activities
4. 🔍 Search for local food recommendations  
5. 🔍 Search for weather and travel tips
6. 📋 Synthesize all results into a structured day-by-day itinerary

## Stack

- **LLM**: Gemini 2.5 Flash via function calling
- **Search**: Tavily Search API (real-time web search)
- **UI**: Streamlit with live agent activity feed

## Setup

### 1. Get API keys (both free)
- **Gemini**: https://aistudio.google.com/app/apikey → API Keys
- **Tavily**: https://tavily.com → Sign up → Dashboard → API Keys

### 2. Install dependencies
```bash
pip install -r requirements.txt
```

### 3. Run
```bash
streamlit run app.py
```

Enter your API keys in the sidebar, type a trip request, and hit "Plan My Trip".

## Architecture

```
User Input
    │
    ▼
Gemini 2.5 Flash (orchestrator)
    │
    ├── Tool Call: web_search("Boston to Tokyo flights July 2025")
    │       └── Tavily API → Real web results → Back to Gemini
    │
    ├── Tool Call: web_search("Best hotels Tokyo Shinjuku under $150/night")
    │       └── Tavily API → Real web results → Back to Gemini
    │
    ├── Tool Call: web_search("Top things to do Tokyo 5 days itinerary")
    │       └── Tavily API → Real web results → Back to Gemini
    │
    ├── Tool Call: web_search("Tokyo local food must try restaurants")
    │       └── Tavily API → Real web results → Back to Gemini
    │
    └── Tool Call: web_search("Tokyo weather July packing tips")
            └── Tavily API → Real web results → Back to Gemini
                │
                ▼
        Final Synthesis → Structured Travel Plan
```

The agent autonomously decides:
- How many searches to run
- What to search for based on prior results
- When it has enough information to produce the final plan

## Demo script (for recording)

1. Open the app at localhost:8501
2. Enter your keys in the sidebar
3. Type: "Plan a 5-day trip to Tokyo for 2 people, $3000 budget, from Boston in July"
4. Hit "Plan My Trip"
5. Show the agent activity panel as tool calls fire in real time
6. Show the final structured itinerary when complete
