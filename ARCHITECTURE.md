# TravelMind — Architecture Walkthrough

A complete top-to-bottom explanation of how the application works. Read this front-to-back the first time; thereafter use the file index at the bottom to jump.

---

## 1. The mental model

TravelMind is a **multi-agent pipeline** with **two-way real-time communication** between a Python backend and a vanilla JavaScript frontend.

The backend runs a sequence of LLM-driven steps:

1. Three specialist agents research the trip **in parallel**.
2. The pipeline **pauses** and waits for the user to pick preferences.
3. One agent synthesizes everything into a final markdown plan, streamed token-by-token.
4. A critic agent inspects the plan and triggers a revision if needed.
5. The plan is persisted and a share URL is generated.

The frontend listens to a single Server-Sent Events (SSE) stream and renders every event live: tool calls, search results, the preference checkpoint form, the streamed plan, and the critic's verdict.

There is no database, no message broker, no queue worker. Everything for a given trip runs inside one worker thread in one Python process, with state held in memory.

---

## 2. The request lifecycle (one full trip)

Here is what happens when a user clicks **Plan my trip**, end to end:

```
Browser                          FastAPI                       Worker Thread                MCP subprocesses
   │                                │                                │                              │
   │ POST /api/plan ──────────────► │                                │                              │
   │                                │ spawn worker thread ─────────► │                              │
   │ ◄── SSE: session{job_id} ───── │                                │                              │
   │                                │                                │ run_planner() begins         │
   │                                │                                │ _init_mcp() ────spawn──────► │ Tavily MCP (npx)
   │                                │                                │                       ────► │ Filesystem MCP
   │                                │                                │                       ────► │ Google Maps MCP (if key)
   │ ◄── SSE: mcp_status ────────── │ ◄──── enqueue events ───────── │                              │
   │                                │                                │                              │
   │                                │                                │ ThreadPoolExecutor(3):       │
   │                                │                                │ ┌─ FlightAgent loop          │
   │ ◄── SSE: tool_call ─────────── │                                │ │  Gemini ↔ web_search       │
   │ ◄── SSE: tool_result ───────── │                                │ │                            │
   │                                │                                │ ├─ HotelAgent loop           │
   │                                │                                │ └─ ItineraryAgent loop       │
   │                                │                                │    (web + Maps)              │
   │                                │                                │                              │
   │                                │                                │ request_prefs() blocks       │
   │ ◄── SSE: prefs_request ─────── │                                │   on threading.Event         │
   │                                │                                │                              │
   │ POST /api/plan/{id}/prefs ───► │ set Event ───────────────────► │                              │
   │ ◄── 200 OK ────────────────── │                                │                              │
   │                                │                                │ Event.wait() returns         │
   │                                │                                │                              │
   │                                │                                │ _stream_synthesis():         │
   │ ◄── SSE: plan_start ────────── │                                │   Gemini stream              │
   │ ◄── SSE: plan_chunk × N ────── │                                │                              │
   │ ◄── SSE: final_plan ────────── │                                │                              │
   │                                │                                │                              │
   │                                │                                │ _critique_and_revise():      │
   │ ◄── SSE: critique ──────────── │                                │   Gemini JSON                │
   │ ◄── (revision stream if any) ─ │                                │   re-stream if not approved  │
   │                                │                                │                              │
   │                                │                                │ _save_plan() ──MCP write──► │ Filesystem MCP
   │ ◄── SSE: plan_saved ────────── │                                │ mcp.close() ───signal────► │ keeper tasks exit
   │ ◄── SSE: done{searches} ────── │                                │                              │
   │                                │ stream end ─────────────────── │ worker exits                 │
```

The whole exchange uses a single long-lived HTTP connection (SSE). The frontend never polls.

---

## 3. The entry points (server.py)

`server.py` is a thin FastAPI app. Its responsibilities are:

- **Spawn a worker thread** for each planning request and stream its events back as SSE.
- **Route the HITL preference response** back into the running worker.
- **Serve saved plans** for the share-URL feature.
- **Serve the SPA** for both the root path and `/p/{id}` (share routes).

### The `_JOBS` dict

```python
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
```

This is the only piece of cross-request state. Each entry is `{event: threading.Event, prefs: {value: dict}}`. The `Event` is how the HITL gate works (Section 8). The dict is wiped when the worker thread exits.

Because this lives in process memory, the app is **single-instance**. To scale horizontally, this would move to Redis.

### `POST /api/plan`

The flow is:

1. Generate a UUID `job_id`.
2. Register an entry in `_JOBS`.
3. Define `on_event(event_type, payload)` — a closure that pushes events onto a thread-safe `queue.Queue`. The HTTP handler reads from this queue to produce SSE frames.
4. Define `request_prefs(options)` — a closure that emits a `prefs_request` event and then *blocks* on the registered `Event` for up to 5 minutes. This is the HITL gate.
5. Spawn a daemon worker thread that calls `run_planner(...)` with both closures.
6. Return a `StreamingResponse` whose generator reads from the queue and yields SSE frames.

The worker and the HTTP handler are decoupled by the queue, which is the only synchronization point.

### `POST /api/plan/{job_id}/prefs`

Looks up the job in `_JOBS`, sets `job["prefs"]["value"]` to the submitted dict, then sets `job["event"]` — unblocking the `request_prefs()` call inside the worker thread.

### `GET /api/history` and `/api/history/{plan_id}`

Plain disk reads from `data/plans/`. The list endpoint returns metadata only (no plan body). The item endpoint returns the full JSON.

### `GET /p/{plan_id}`

Returns the SPA shell. The client-side JS reads `window.location.pathname`, recognizes the `/p/` prefix, and switches to read-only share mode.

---

## 4. The SSE event protocol

The contract between backend and frontend is a set of event types, each with a JSON payload:

| Event | Payload | Meaning |
|---|---|---|
| `session` | `{job_id}` | First event. Frontend stores the job id so it can POST preferences back. |
| `mcp_status` | `{server, status, tools?, message?}` | An MCP server came up (or failed). |
| `agent_status` | `{agent, status, message?}` | An agent transitioned to running / done / error. |
| `tool_call` | `{agent, query, tool}` | An agent is about to call a tool. |
| `tool_result` | `{agent, query, tool, summary, sources_count, source}` | The tool returned. `source` is one of `mcp`, `sdk`, `mcp_error`, `error`. |
| `section_done` | `{agent, section}` | A sub-agent's markdown section is ready. (Not used by UI; useful for debugging.) |
| `prefs_request` | `{options}` | Pipeline is paused. Show the preference form. |
| `plan_start` | `{revised}` | Streaming synthesis is about to begin. Reset accumulator. |
| `plan_chunk` | `{text, revised}` | A token (or short token sequence) of the plan. |
| `final_plan` | `{plan, revised?}` | Complete synthesis. Frontend swaps streamed markdown for post-processed cards. |
| `critique` | `{approved, issues, critique}` | CriticAgent's verdict. |
| `plan_saved` | `{path, via}` | Plan persisted. `via` is `mcp` or `direct`. |
| `done` | `{searches, sources}` | Final stats. Pipeline complete. |
| `error` | `{message}` | Worker exited abnormally. |

Frame format is the standard SSE:

```
event: tool_call
data: {"agent":"itinerary","query":"...","tool":"maps_search_places"}

```

Two newlines terminate the frame.

---

## 5. The orchestrator: `run_planner` (agent.py)

This is the top-level function the worker thread calls. It's about 100 lines and is best read as a state machine in 6 phases:

```python
def run_planner(*, user_query, gemini_key, tavily_key, on_event, request_prefs=None,
                gmaps_key=None, currency="USD", travel_month=None) -> dict:
    # Phase 1 — clients & MCP
    gemini_client = genai.Client(api_key=gemini_key)
    tavily_client = TavilyClient(api_key=tavily_key)
    mcp = _init_mcp(tavily_key=tavily_key, gmaps_key=gmaps_key, on_event=on_event)

    # Phase 2 — enrich user query with currency / month constraints
    constraints = _format_constraints(currency=currency, travel_month=travel_month)
    enriched_query = (constraints + "\n\n" + user_query) if constraints else user_query

    # Phase 3 — parallel sub-agents
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(_run_sub_agent, ...): name for name, prompt in SUB_AGENTS}
        for fut in as_completed(futures):
            results[name] = fut.result()

    # Phase 4 — HITL gate
    prefs = request_prefs(PREFS_OPTIONS) if request_prefs else {}

    # Phase 5 — streaming synthesis
    final_plan = _stream_synthesis(...)

    # Phase 6 — critic review + optional revision
    final_plan = _critique_and_revise(...)

    # Phase 7 — persist + close MCP
    plan_path = _save_plan(...)
    if mcp is not None: mcp.close()

    return {"plan": ..., "total_searches": ..., "total_sources": ..., "plan_path": ...}
```

Every `on_event(...)` call inside this function ends up on the SSE queue and back to the browser.

---

## 6. Sub-agents (agent.py)

Three sub-agents live in `SUB_AGENTS`:

```python
SUB_AGENTS = [
    ("flight",    FLIGHT_AGENT_PROMPT),
    ("hotel",     HOTEL_AGENT_PROMPT),
    ("itinerary", ITINERARY_AGENT_PROMPT),
]
```

Each is just a name plus a system prompt. The runtime behavior is identical — they all go through `_run_sub_agent()`, which:

1. Builds the agent's tool list with `_build_agent_tools()` (Section 7).
2. Enters a function-calling loop with Gemini (up to 6 iterations).
3. On each iteration:
   - Sends the conversation + tool declarations to Gemini.
   - If Gemini wants to call a tool, dispatches through `tools_by_name[call.name].backend(args)`.
   - Feeds the tool result back as a function response.
   - Repeats until Gemini returns text instead of a function call, or the iteration cap fires.
4. The text output is the sub-agent's markdown section, which goes into the synthesis step later.

### Why three agents instead of one?

Two reasons:

- **Parallelism.** Three independent Gemini conversations can run in three threads. End-to-end latency is ~max(flight, hotel, itinerary) instead of the sum.
- **Focus.** Each agent has a narrow, structured system prompt that produces exactly one markdown section in a known format. The synthesis prompt can then trust the structure of its inputs.

### Each agent's tools

- **FlightAgent** — `web_search` only.
- **HotelAgent** — `web_search` only.
- **ItineraryAgent** — `web_search` plus `maps_search_places` and `maps_distance_matrix` (when Google Maps MCP is up).

The selection is hard-coded in `_build_agent_tools()`. Adding more is a one-line change.

---

## 7. The tool abstraction (agent.py)

This is the layer that lets one agent loop handle multiple kinds of tools without caring whether each one is a direct SDK call, an MCP RPC, or something else.

```python
@dataclass
class AgentTool:
    name: str                                # Gemini-visible name
    declaration: types.FunctionDeclaration   # what Gemini sees
    backend: Callable[[dict], str]           # how it actually runs
```

`_build_agent_tools(agent_name, mcp, tavily_client, on_event_emit)` returns a list of these. Each backend is a closure that:

- Emits a `tool_call` event so the lane card shows up.
- Performs the call (MCP or SDK).
- Emits a `tool_result` event with a short summary and the `source` tag.
- Returns the text result that gets fed back into the Gemini conversation.

The `web_search` tool's backend is `_perform_web_search` (Section 8). Maps tool backends are produced by `_wrap_mcp_tool()`.

### JSON Schema → Gemini Schema

The Google Maps MCP server exposes seven tools, each with a JSON Schema describing its input. We can't hand those to Gemini directly because Gemini's `types.Schema` is a different format. `_jsonschema_to_gemini()` is a recursive translator that handles object / array / primitive types, properties, required fields, and enums.

This is what makes adding new MCP servers trivial. The flow is:

1. Boot the MCP server.
2. Call `mcp.list_tools()` — returns each tool's `input_schema`.
3. Run each `input_schema` through `_jsonschema_to_gemini()` to get a Gemini-compatible `Schema`.
4. Wrap in a `FunctionDeclaration` with the MCP tool's name and description.
5. Wrap in an `AgentTool` whose backend calls `mcp.call_tool(server, name, args)`.

No per-tool plumbing.

---

## 8. Web search with MCP/SDK fallback (agent.py)

`_perform_web_search(query, mcp, tavily_client)` is the workhorse used by every sub-agent. It:

1. **Tries MCP first.** If a `tavily` MCP server is registered, it calls `mcp.call_tool("tavily", "tavily-search", {query, max_results, search_depth})`.
2. **Parses** the MCP server's response — usually JSON, sometimes wrapped in a code fence. `_parse_tavily_mcp_result()` is tolerant.
3. **Falls back** to the direct `TavilyClient.search()` SDK call on any failure (no MCP, MCP errored, JSON unparseable). The user never sees the failure.
4. Returns `(formatted_content, summary, sources_count, source)` where `source` is `"mcp"` or `"sdk"` so the UI can tell which path served the request.

The fallback path is critical for resilience. If `npx` can't reach the npm registry or the subprocess crashes mid-request, the app keeps working.

---

## 9. The HITL preference gate (server.py + agent.py)

This is the most subtle piece of plumbing in the app. It crosses three threads:

- **The HTTP handler** (FastAPI async task)
- **The worker thread** (running `run_planner`)
- **A second HTTP handler** (POST /api/plan/{id}/prefs)

The sequence:

1. The worker hits Phase 4 and calls `request_prefs(PREFS_OPTIONS)`.
2. `request_prefs()` (defined in `server.py`) emits a `prefs_request` event, then calls `prefs_event.wait(timeout=300)`. The worker thread blocks.
3. Meanwhile, the user picks options in the browser and submits — `POST /api/plan/{id}/prefs`.
4. The handler for that POST looks up the job, writes `prefs_holder["value"] = body.prefs`, then calls `event.set()`.
5. The worker thread unblocks, reads `prefs_holder["value"]`, and continues.

If the user never submits, the wait times out after 5 minutes and the worker continues with empty preferences (sensible defaults).

The `prefs_holder` dict is captured by closure into both `request_prefs()` and the POST handler, so they share the same mutable reference. The `_JOBS_LOCK` only guards add/remove operations, not the value handoff.

---

## 10. Streaming synthesis (agent.py)

`_stream_synthesis()` replaces what used to be a blocking `generate_content()` call.

```python
stream = gemini_client.models.generate_content_stream(
    model=GEMINI_MODEL,
    contents=[...],
    config=types.GenerateContentConfig(system_instruction=..., max_output_tokens=8192),
)
for chunk in stream:
    text = getattr(chunk, "text", None) or ""
    parts.append(text)
    on_event("plan_chunk", {"text": text, "revised": revised})
```

Each iteration of the for-loop fires one `plan_chunk` event. The frontend appends them into a buffer and re-renders behind a `requestAnimationFrame` coalescer (Section 14).

After the stream ends, `_stream_synthesis()` emits a `final_plan` with the complete text. The frontend treats this as the cue to swap the streamed (raw markdown) view for the **decorated** view (trip-summary grid, day cards, etc. — see Section 15).

If the stream fails, the function falls back to a single blocking `generate_content` call. The user still gets a plan, just no progressive paint.

---

## 11. The critic loop (agent.py)

`_critique_and_revise()` runs after the first synthesis:

1. Emits `agent_status: critic running`.
2. Calls Gemini with `response_mime_type="application/json"` and the `CRITIC_AGENT_PROMPT`. The critic returns:
   ```json
   { "approved": true | false,
     "issues": ["short concrete issue", ...],
     "critique": "one-line verdict" }
   ```
3. Parses the JSON with `_parse_critic_json()`, which tries bare JSON, then code-fenced JSON, then the first `{...}` block. Robust to model formatting drift.
4. Emits a `critique` event so the critic lane card shows the verdict.
5. **If approved**, returns the original plan unchanged.
6. **If not approved**, builds a `PLANNER_REVISION_PROMPT` invocation containing the original plan + the issue list, streams the revision, emits a new `final_plan` with `revised: true`. The frontend re-renders the plan and shows a banner.

The loop is intentionally **single-pass** — revise once, ship. Iterating until approved would risk infinite loops and timeout the SSE connection.

If the critic's JSON is unparseable (rare), the original plan ships unchanged with a visible error on the critic lane. The pipeline never blocks on the critic.

---

## 12. Persistence (agent.py + mcp_client.py)

`_save_plan()` writes a JSON blob like:

```json
{
  "created_at": "20260511-014233",
  "query": "5 days in Tokyo for 2 people, $3000 budget, from Boston in July",
  "prefs": {"flight": "cheapest", "hotel": "comfort", "pace": "balanced"},
  "plan": "## Trip Summary\n- Destination: Tokyo, Japan\n..."
}
```

The write path goes through **Filesystem MCP** when it's up, falling back to direct `open().write()`. The save is fire-and-forget — failure doesn't block the response.

Filename format: `{timestamp}-{slugified-query}.json`. The slug is the plan id used by `/p/{id}` and `/api/history/{id}`.

On Render's free tier, `data/plans/` is ephemeral. To persist across restarts, attach a Render disk and mount it there. The Filesystem MCP needs to be re-pointed at the mount.

---

## 13. The MCP layer (mcp_client.py)

This is the most architecturally interesting file. The constraint is:

- MCP servers expose tools via JSON-RPC over stdio.
- The official Python SDK is **async** (`asyncio`, `anyio`).
- Our sub-agents are **thread-based** (`ThreadPoolExecutor`).

Bridging the two is what `MCPManager` does:

### The keeper-task pattern

```python
async def _keeper(name, params, handle):
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            resp = await session.list_tools()
            handle.session = session
            handle.tools = list(resp.tools)
            handle.ready.set()
            await handle.shutdown.wait()
```

For each MCP server, a single long-running coroutine holds the `stdio_client` and `ClientSession` contexts open. It signals "ready" via an `asyncio.Event` and then sleeps on a "shutdown" `asyncio.Event` until `close()` is called.

This solves a tricky problem: `stdio_client` is an async context manager. If you enter and exit it in different coroutines, anyio's cancel-scope semantics complain. By owning the contexts in one long-running task, we sidestep that entirely.

### The background event loop

`MCPManager.start()` spawns a daemon thread that creates an `asyncio.new_event_loop()` and calls `run_forever()`. All async work happens on this loop. Sync callers use:

```python
asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=...)
```

### The sync API

The threaded sub-agents see:

- `mcp.has_server(name)` → bool
- `mcp.list_tools(name)` → list of tool dicts
- `mcp.call_tool(server, tool, args, timeout=60)` → string result

All sync, all thread-safe. The async details are completely hidden.

### Lifecycle

- One `MCPManager` per planning request.
- Started at the top of `run_planner` via `_init_mcp()`.
- Closed at the bottom. `close()` sets every keeper's shutdown event and stops the loop.
- If a request errors mid-flight, the manager is closed in the exception handler.

### Why per-request and not app-global?

The Tavily MCP server requires the user's Tavily API key at subprocess launch. With user keys coming from the frontend per-request, we can't share a single subprocess across users. Filesystem MCP could be app-global but the cost of co-locating it with Tavily MCP is negligible (~50 ms startup).

---

## 14. The frontend SSE consumer (static/app.js)

The fetch-and-parse loop:

```javascript
const res = await fetch("/api/plan", { method: "POST", body: ... });
const reader = res.body.getReader();
const decoder = new TextDecoder();
let buffer = "";
while (true) {
  const { value, done } = await reader.read();
  if (done) break;
  buffer += decoder.decode(value, { stream: true });
  const events = buffer.split("\n\n");
  buffer = events.pop() || "";
  for (const evt of events) {
    // parse "event: foo\ndata: {...}\n\n" → eventType + data
    switch (eventType) {
      case "session": ...
      case "tool_call": addToolCard(...); break;
      case "plan_chunk": streamBuffer += data.text; renderStreamSoon(); break;
      ...
    }
  }
}
```

The split-on-`\n\n` pattern handles the SSE frame boundary. Frames may straddle TCP chunks; the leftover after `events.pop()` becomes the start of the next buffer.

### Streaming render

```javascript
function renderStreamSoon() {
  if (streamRaf) return;
  streamRaf = requestAnimationFrame(() => {
    streamRaf = 0;
    planEl.innerHTML = marked.parse(streamBuffer);
  });
}
```

Coalesces many `plan_chunk` events into at most one re-parse per animation frame. Without this, every token would trigger a `marked.parse()` over the growing string — quadratic and janky.

### State reset

`runPlan()` clears all UI state at the top: lanes, plan element, MCP chip bar, action buttons, stream buffer, job id. This is why running a second trip on the same tab works cleanly.

---

## 15. Markdown decoration passes (static/app.js)

When `final_plan` arrives, the frontend swaps the streaming raw markdown for a **decorated** view by running four post-processing functions over the rendered HTML:

1. **`decorateTripSummary`** — finds `## Trip Summary`, splits its bullets into a 4-cell info grid (Destination / Dates / Travelers / Budget). Non-standard bullets become a small italic note below the grid.
2. **`decorateOptionBlocks`** — finds `**Option N — Name**` and `**Tier — Property**` paragraphs followed by `<ul>`, wraps each in a card with a cyan badge and key/value rows.
3. **`decorateItinerary`** — finds `### Day N — Theme` headings followed by `<ul>`, wraps each day in a card. Parses each top-level bullet's leading time-of-day label ("Morning", "Eat") and splits into a two-column row (label | content).
4. **`highlightRealityCheck`** — finds `## Reality Check` and wraps it in an amber warning callout with a circular `!` icon.

These passes are why the plan looks more structured than what the model actually emits. The model emits clean markdown; the frontend layers semantic structure on top.

The raw markdown is also stashed on `planEl.dataset.rawMarkdown` so the Download .md button can recover it.

---

## 16. Share URLs (server.py + static/app.js)

`GET /p/{plan_id}` serves the same SPA shell as `/`. The SPA detects the `/p/` path on `DOMContentLoaded` and enters **read-only share mode** via `loadSharedPlan()`:

1. Hide the header, hero, input form, examples, agent activity panel.
2. Expand the plan card from `md:col-span-3` to `md:col-span-5` (full width since activity panel is gone).
3. Fetch `/api/history/{plan_id}`.
4. Render the plan markdown and run the decoration passes.
5. Show a banner indicating this is a shared trip.

The Copy link button puts `${origin}/p/${planId}` in clipboard. The button only appears once `plan_saved` fires (so we know the file exists).

Caveat: on free-tier Render, `data/plans/` is wiped on restart and share links die with it. Persistent disk fixes this.

---

## 17. Print & download (static/app.js + static/styles.css)

**Download .md** — `new Blob([rawMarkdown], { type: "text/markdown" })`, create an anchor with `download="travelmind-{slug}.md"`, click it programmatically.

**Print** — `window.print()`. The print stylesheet at the bottom of `styles.css` does the heavy lifting:

```css
@media print {
  body { background: white; color: #111; }
  .bg-blob, header, .glass, footer, #activity, ... { display: none; }
  #planCard, #results { all: unset; display: block; }
  .day-card, .opt-card, .reality-callout { background: white; border: 1px solid #ccc; }
  ...
}
```

The result: only the plan content prints, on a white background with black text and gray-bordered cards — clean output for PDF export.

---

## 18. Mobile layout (static/styles.css)

Two problems on small screens:

1. **The activity panel is huge.** Four lanes stacked above the plan push the plan off-screen.
2. **Each lane's card list is tall.** Even with completed lanes, you have to scroll past 20+ activity cards.

Fixes:

```css
/* Cap the activity panel height on mobile so the plan is reachable. */
max-h-[300px] overflow-y-auto md:max-h-[calc(100vh-3rem)]

/* Collapse completed/pending lanes' card lists; only running lane stays open. */
@media (max-width: 768px) {
  .lane-done .lane-list, .lane-pending .lane-list { max-height: 0; overflow: hidden; }
  .lane-running .lane-list { max-height: 320px; overflow-y: auto; }
}
```

The result: the activity panel is a compact 300px scrollable strip, and only the lane currently working is expanded.

---

## 19. Failure modes (and how each is handled)

| Failure | Handling |
|---|---|
| Tavily MCP subprocess won't start (Node missing, package broken) | `_init_mcp` catches, emits `mcp_status: error`. Sub-agents fall back to direct Tavily SDK. |
| Filesystem MCP won't start | Same pattern. `_save_plan` falls back to direct `open().write()`. |
| Google Maps MCP won't start (no key, bad key, billing not enabled) | `_init_mcp` catches and skips. ItineraryAgent simply doesn't get Maps tools. |
| Tavily MCP call returns malformed JSON | `_parse_tavily_mcp_result` tries multiple parses, then surfaces raw text to the model. |
| Tavily MCP call throws | `_perform_web_search` catches, falls back to direct SDK. |
| Critic returns unparseable JSON | `_parse_critic_json` tries fenced / extracted / bare. On total failure, logs the raw response and ships the original plan with a visible critic error. |
| Synthesis stream fails mid-way | `_stream_synthesis` falls back to a single blocking call. |
| User never submits preferences | `request_prefs` returns `{}` after 5 minutes. Pipeline continues with defaults. |
| Sub-agent loop hits iteration cap | Last iteration sends a no-tools message instructing the model to write its section now. |
| Sub-agent crashes entirely | The other two still finish. The synthesis prompt receives `_<name> failed: ...` for the missing section. |

The general principle: every external dependency has a fallback. Nothing in the pipeline is allowed to block the user from getting *some* output.

---

## 20. File-by-file index

| File | What's in it |
|---|---|
| `server.py` | FastAPI app, SSE endpoint, HITL prefs route, history routes, share route, SPA route. |
| `agent.py` | System prompts; `run_planner()` orchestrator; `_run_sub_agent()` loop; `AgentTool` and tool builders; `_perform_web_search`; JSON Schema translator; `_init_mcp`; `_stream_synthesis`; `_critique_and_revise`; `_save_plan`. |
| `mcp_client.py` | `MCPManager` with background event loop; keeper-coroutine pattern; sync `list_tools` / `call_tool` wrappers. |
| `static/index.html` | SPA shell: hero, textarea, month/currency selects, agent activity panel, plan card, settings modal, footer. |
| `static/app.js` | SSE consumer; lane rendering; streaming buffer; markdown decoration passes; settings modal; share-mode loader; download/print/copy buttons. |
| `static/styles.css` | Glassmorphism; lane styles; day/option/summary card styles; streaming caret; print stylesheet; mobile collapsing rules; reality-check callout. |
| `static/favicon.svg` | Gradient TM mark. |
| `requirements.txt` | google-genai, tavily-python, fastapi, uvicorn, pydantic, mcp. |
| `Dockerfile` | Python 3.11-slim + Node 20 (NodeSource) + globally-installed MCP packages. |
| `render.yaml` | Render blueprint pointing at the Dockerfile. |
| `.dockerignore` / `.gitignore` | Standard exclusions plus `data/plans/`. |
| `README.md` | Public-facing project overview. |
| `ARCHITECTURE.md` | This file. |

---

## 21. Design tradeoffs I'd revisit

A few choices that are pragmatic but not perfect:

- **In-memory `_JOBS` dict.** Single-process only. To run multiple uvicorn workers, this moves to Redis (or even a SQLite file with FOR UPDATE locking).
- **Per-request MCP subprocesses.** Slow to spin up (~1–2s each, even with prewarmed packages). A pool of long-lived MCP processes keyed by API key would amortize startup but adds session-pinning complexity.
- **Single-pass critic.** Two rounds would catch more issues but risks SSE timeouts and infinite loops. A confidence threshold ("only revise if confidence > 0.7") would be a middle ground.
- **Tavily SDK as fallback while MCP is also configured.** Two code paths for the same operation. Cleaner long-term would be MCP-only, but the fallback was worth keeping for the demo's resilience.
- **Decoration in JS instead of in the model prompt.** The model could output structured JSON that the frontend renders. Decoration on top of markdown is more forgiving when the model drifts, but it's also a layer of regex held together by hope.
- **No streaming of sub-agent text.** Only the final synthesis streams. Sub-agents block on full responses because the iteration loop needs the complete function-call list.

Each of these would be a fine resume-driven follow-up.
