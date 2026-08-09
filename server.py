import asyncio
import json
import logging
import os
import threading
from queue import Queue
from uuid import uuid4

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("travelmind")

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent import PLANS_DIR, run_planner

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
    gmaps_key: str | None = None
    currency: str = "USD"
    travel_month: str | None = None
    origin: str | None = None


class PrefsBody(BaseModel):
    prefs: dict


# In-memory job registry. Suitable for single-process dev; for prod swap with Redis.
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.post("/api/plan")
async def plan(req: PlanRequest):
    if not req.query.strip() or not req.gemini_key or not req.tavily_key:
        return {"error": "Missing query or API keys"}

    job_id = str(uuid4())
    queue: Queue = Queue()
    SENTINEL = object()
    prefs_event = threading.Event()
    prefs_holder: dict = {}

    with _JOBS_LOCK:
        _JOBS[job_id] = {"event": prefs_event, "prefs": prefs_holder}

    def on_event(event_type: str, payload: dict):
        if event_type in ("prefs_request", "agent_status", "final_plan", "error", "mcp_status", "plan_saved", "plan_start"):
            log.info("[%s] event=%s payload=%s", job_id[:8], event_type,
                     {k: v for k, v in payload.items() if k not in ("section", "plan", "text")})
        queue.put((event_type, payload))

    def request_prefs(options: dict) -> dict:
        log.info("[%s] requesting user prefs (HITL gate)", job_id[:8])
        on_event("prefs_request", {"options": options})
        # Wait up to 5 minutes for the user to submit; fall back to {} otherwise.
        signaled = prefs_event.wait(timeout=300)
        result = prefs_holder.get("value", {})
        log.info("[%s] prefs received signaled=%s value=%s", job_id[:8], signaled, result)
        return result

    def worker():
        try:
            summary = run_planner(
                user_query=req.query,
                gemini_key=req.gemini_key,
                tavily_key=req.tavily_key,
                gmaps_key=(req.gmaps_key or None),
                currency=req.currency,
                travel_month=req.travel_month,
                origin=(req.origin or None),
                on_event=on_event,
                request_prefs=request_prefs,
            )
            queue.put(("done", {
                "searches": summary["total_searches"],
                "sources": summary["total_sources"],
            }))
        except Exception as e:
            queue.put(("error", {"message": str(e)}))
        finally:
            with _JOBS_LOCK:
                _JOBS.pop(job_id, None)
            queue.put(SENTINEL)

    threading.Thread(target=worker, daemon=True).start()

    async def stream():
        # Announce session id first so client can post prefs back.
        yield _sse("session", {"job_id": job_id})

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


@app.post("/api/plan/{job_id}/prefs")
async def submit_prefs(job_id: str, body: PrefsBody):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Unknown job")
    log.info("[%s] prefs submitted: %s", job_id[:8], body.prefs)
    job["prefs"]["value"] = body.prefs
    job["event"].set()
    return {"ok": True}


@app.get("/api/history")
async def history():
    """List saved plans, newest first. Returns metadata only — no plan body."""
    if not os.path.isdir(PLANS_DIR):
        return {"plans": []}
    items = []
    for fn in os.listdir(PLANS_DIR):
        if not fn.endswith(".json"):
            continue
        path = os.path.join(PLANS_DIR, fn)
        try:
            with open(path) as f:
                data = json.load(f)
            items.append({
                "id": fn[:-5],
                "created_at": data.get("created_at", ""),
                "query": data.get("query", ""),
                "prefs": data.get("prefs", {}),
            })
        except Exception:
            continue
    items.sort(key=lambda x: x["created_at"], reverse=True)
    return {"plans": items}


@app.get("/api/history/{plan_id}")
async def history_item(plan_id: str):
    if "/" in plan_id or ".." in plan_id:
        raise HTTPException(status_code=400, detail="invalid id")
    path = os.path.join(PLANS_DIR, plan_id + ".json")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="not found")
    with open(path) as f:
        return json.load(f)


@app.get("/")
async def root():
    return FileResponse("static/index.html")


@app.get("/favicon.ico")
@app.get("/favicon.svg")
async def favicon():
    """Some browsers probe /favicon.ico at the root regardless of <link> tags."""
    return FileResponse("static/favicon.svg", media_type="image/svg+xml")


@app.get("/p/{plan_id}")
async def shared_plan_page(plan_id: str):
    """Public share route. Returns the SPA; client-side JS fetches the plan."""
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
