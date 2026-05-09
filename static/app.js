const $ = (id) => document.getElementById(id);

const EXAMPLES = [
  "5 days in Tokyo for 2, $3000, from Boston in July",
  "4-day Bali solo trip, $2000, from NYC in August",
  "Weekend in Montreal from Boston, $600",
  "10 days in Italy (Rome + Florence + Venice), $5000 for 2",
];

const LANES = [
  { id: "flight",    label: "FlightAgent",    desc: "Routes, airlines, prices" },
  { id: "hotel",     label: "HotelAgent",     desc: "Lodging within budget" },
  { id: "itinerary", label: "ItineraryAgent", desc: "Sights, food, weather" },
];

// ── Examples chips ──────────────────────────────────────────────────────────
(function renderExamples() {
  const c = $("examples");
  EXAMPLES.forEach((ex) => {
    const chip = document.createElement("button");
    chip.className = "example-chip";
    chip.textContent = ex.length > 38 ? ex.slice(0, 38) + "…" : ex;
    chip.title = ex;
    chip.onclick = () => { $("queryInput").value = ex; $("queryInput").focus(); };
    c.appendChild(chip);
  });
})();

// ── Settings modal ──────────────────────────────────────────────────────────
const settingsModal = $("settingsModal");

function flashSaved(badgeId) {
  const el = $(badgeId);
  el.style.opacity = "1";
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.style.opacity = "0"; }, 1400);
}

function bindAutoSave(inputId, storageKey, badgeId) {
  const input = $(inputId);
  let debounce;
  input.addEventListener("input", () => {
    clearTimeout(debounce);
    debounce = setTimeout(() => {
      localStorage.setItem(storageKey, input.value.trim());
      flashSaved(badgeId);
    }, 300);
  });
}

$("settingsBtn").onclick = () => {
  $("geminiKey").value = localStorage.getItem("gemini_key") || "";
  $("tavilyKey").value = localStorage.getItem("tavily_key") || "";
  settingsModal.classList.remove("hidden");
};
$("closeSettings").onclick = () => settingsModal.classList.add("hidden");
$("closeSettingsBtn").onclick = () => settingsModal.classList.add("hidden");
settingsModal.onclick = (e) => { if (e.target === settingsModal) settingsModal.classList.add("hidden"); };

bindAutoSave("geminiKey", "gemini_key", "geminiSaved");
bindAutoSave("tavilyKey", "tavily_key", "tavilySaved");

window.addEventListener("DOMContentLoaded", () => {
  if (!localStorage.getItem("gemini_key") || !localStorage.getItem("tavily_key")) {
    $("settingsBtn").click();
  }
});

// ── Lane rendering ──────────────────────────────────────────────────────────
const lanesEl = $("lanes");
const planBtn = $("planBtn");
const callCount = $("callCount");
const planEl = $("plan");
const planLoader = $("planLoader");
const results = $("results");
const stats = $("stats");
const banner = $("statusBanner");

let laneRefs = {};      // id -> { card, list, statusEl }
let toolCardsByAgent = {}; // id -> Map(query -> card)

function buildLanes() {
  lanesEl.innerHTML = "";
  laneRefs = {};
  toolCardsByAgent = {};
  LANES.forEach((lane) => {
    const card = document.createElement("div");
    card.className = "lane lane-pending";
    card.innerHTML = `
      <div class="lane-header">
        <div>
          <div class="lane-title">${lane.label}</div>
          <div class="lane-desc">${lane.desc}</div>
        </div>
        <span class="lane-status">queued</span>
      </div>
      <div class="lane-list"></div>
    `;
    lanesEl.appendChild(card);
    laneRefs[lane.id] = {
      card,
      list: card.querySelector(".lane-list"),
      statusEl: card.querySelector(".lane-status"),
    };
    toolCardsByAgent[lane.id] = new Map();
  });
}

function setLaneStatus(agent, status) {
  const ref = laneRefs[agent];
  if (!ref) return;
  ref.card.classList.remove("lane-pending", "lane-running", "lane-done", "lane-error");
  ref.card.classList.add(`lane-${status}`);
  ref.statusEl.textContent = status;
}

function addToolCard(agent, query) {
  const ref = laneRefs[agent];
  if (!ref) return;
  const card = document.createElement("div");
  card.className = "activity-card";
  card.innerHTML = `
    <div class="card-query"><span class="card-spinner"></span><span></span></div>
    <div class="card-preview">Searching the web...</div>
  `;
  card.querySelector(".card-query span:last-child").textContent = query;
  ref.list.appendChild(card);
  ref.list.scrollTop = ref.list.scrollHeight;
  toolCardsByAgent[agent].set(query, card);
}

function completeToolCard(agent, query, summary) {
  const card = toolCardsByAgent[agent]?.get(query);
  if (!card) return;
  card.classList.add("completed");
  const spinner = card.querySelector(".card-spinner");
  if (spinner) spinner.outerHTML = '<span class="card-done">done</span>';
  card.querySelector(".card-preview").textContent = summary || "Done";
}

// ── Plan request ────────────────────────────────────────────────────────────
function showBanner(msg, kind = "info") {
  banner.textContent = msg;
  banner.classList.remove("hidden");
  banner.className = "text-center text-sm mb-6 " + (kind === "error" ? "text-red-400" : "text-slate-400");
}

async function runPlan() {
  const query = $("queryInput").value.trim();
  const geminiKey = localStorage.getItem("gemini_key") || "";
  const tavilyKey = localStorage.getItem("tavily_key") || "";

  if (!query) return showBanner("Enter a trip description first.");
  if (!geminiKey || !tavilyKey) {
    showBanner("Add your API keys (top right).", "error");
    return;
  }

  // Reset UI
  buildLanes();
  planEl.innerHTML = "";
  planEl.classList.add("hidden");
  planLoader.classList.remove("hidden");
  results.classList.remove("hidden");
  stats.classList.add("hidden");
  banner.classList.add("hidden");
  callCount.textContent = "0 searches";
  planBtn.disabled = true;
  planBtn.querySelector("span").textContent = "Planning...";

  const startedAt = Date.now();
  let searchCount = 0;
  let finalSearches = 0;
  let finalSources = 0;

  try {
    const res = await fetch("/api/plan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, gemini_key: geminiKey, tavily_key: tavilyKey }),
    });

    if (!res.ok || !res.body) throw new Error("Server error: " + res.status);

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
        if (!evt.trim()) continue;
        const lines = evt.split("\n");
        let eventType = "message", dataStr = "";
        for (const line of lines) {
          if (line.startsWith("event:")) eventType = line.slice(6).trim();
          else if (line.startsWith("data:")) dataStr += line.slice(5).trim();
        }
        let data;
        try { data = JSON.parse(dataStr); } catch { continue; }

        switch (eventType) {
          case "agent_status":
            if (data.agent !== "planner") setLaneStatus(data.agent, data.status);
            break;
          case "tool_call":
            searchCount++;
            callCount.textContent = `${searchCount} search${searchCount === 1 ? "" : "es"}`;
            addToolCard(data.agent, data.query);
            break;
          case "tool_result":
            completeToolCard(data.agent, data.query, data.summary);
            break;
          case "section_done":
            // sub-agent finished its section; lane goes done via agent_status
            break;
          case "final_plan":
            planLoader.classList.add("hidden");
            planEl.classList.remove("hidden");
            planEl.innerHTML = marked.parse(data.plan || "");
            break;
          case "done":
            finalSearches = data.searches;
            finalSources = data.sources;
            stats.classList.remove("hidden");
            $("statSearches").textContent = finalSearches;
            $("statSources").textContent = finalSources;
            $("statTime").textContent = Math.round((Date.now() - startedAt) / 1000);
            $("liveDot").classList.remove("animate-pulse");
            $("liveDot").classList.add("bg-emerald-400");
            break;
          case "error":
            showBanner("Agent error: " + data.message, "error");
            planLoader.classList.add("hidden");
            break;
        }
      }
    }
  } catch (e) {
    showBanner("Request failed: " + e.message, "error");
    planLoader.classList.add("hidden");
  } finally {
    planBtn.disabled = false;
    planBtn.querySelector("span").textContent = "Plan my trip";
  }
}

planBtn.onclick = runPlan;
$("queryInput").addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") runPlan();
});
