import asyncio
import json
import threading
from queue import Queue

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent import run_planner

app = FastAPI(title="TravelMind")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class PlanRequest(BaseModel):
    query: str
    gemini_key: str
    tavily_key: str


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.post("/api/plan")
async def plan(req: PlanRequest):
    if not req.query.strip() or not req.gemini_key or not req.tavily_key:
        return {"error": "Missing query or API keys"}

    queue: Queue = Queue()
    SENTINEL = object()

    def on_event(event_type: str, payload: dict):
        queue.put((event_type, payload))

    def worker():
        try:
            summary = run_planner(
                user_query=req.query,
                gemini_key=req.gemini_key,
                tavily_key=req.tavily_key,
                on_event=on_event,
            )
            queue.put(("done", {
                "searches": summary["total_searches"],
                "sources": summary["total_sources"],
            }))
        except Exception as e:
            queue.put(("error", {"message": str(e)}))
        finally:
            queue.put(SENTINEL)

    threading.Thread(target=worker, daemon=True).start()

    async def stream():
        loop = asyncio.get_event_loop()
        while True:
            item = await loop.run_in_executor(None, queue.get)
            if item is SENTINEL:
                break
            event, data = item
            yield _sse(event, data)
            if event == "error":
                break

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/")
async def root():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
