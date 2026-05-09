const $ = (id) => document.getElementById(id);

const EXAMPLES = [
  "5 days in Tokyo for 2, $3000, from Boston in July",
  "4-day Bali solo trip, $2000, from NYC in August",
  "Weekend in Montreal from Boston, $600",
  "10 days in Italy (Rome + Florence + Venice), $5000 for 2",
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

// Auto-open modal on first visit if no keys yet
window.addEventListener("DOMContentLoaded", () => {
  if (!localStorage.getItem("gemini_key") || !localStorage.getItem("tavily_key")) {
    $("settingsBtn").click();
  }
});

// ── Plan request ────────────────────────────────────────────────────────────
const planBtn = $("planBtn");
const activity = $("activity");
const callCount = $("callCount");
const planEl = $("plan");
const planLoader = $("planLoader");
const results = $("results");
const stats = $("stats");
const banner = $("statusBanner");

let toolCards = new Map();

function showBanner(msg, kind = "info") {
  banner.textContent = msg;
  banner.classList.remove("hidden");
  banner.className = "text-center text-sm mb-6 " + (kind === "error" ? "text-red-400" : "text-slate-400");
}

function addToolCard(query) {
  const card = document.createElement("div");
  card.className = "activity-card";
  card.innerHTML = `
    <div class="card-query"><span class="card-spinner"></span><span></span></div>
    <div class="card-preview">Searching the web…</div>
  `;
  card.querySelector(".card-query span:last-child").textContent = query;
  activity.appendChild(card);
  activity.scrollTop = activity.scrollHeight;
  toolCards.set(query, card);
}

function completeToolCard(query, summary) {
  const card = toolCards.get(query);
  if (!card) return;
  card.classList.add("completed");
  const spinner = card.querySelector(".card-spinner");
  if (spinner) spinner.outerHTML = "<span>✓</span>";
  card.querySelector(".card-preview").textContent = summary || "Done";
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
  toolCards.clear();
  activity.innerHTML = "";
  planEl.innerHTML = "";
  planEl.classList.add("hidden");
  planLoader.classList.remove("hidden");
  results.classList.remove("hidden");
  stats.classList.add("hidden");
  banner.classList.add("hidden");
  callCount.textContent = "0 searches";
  planBtn.disabled = true;
  planBtn.querySelector("span").textContent = "Planning…";

  const startedAt = Date.now();
  let searches = 0;

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

        if (eventType === "tool_call") {
          searches++;
          callCount.textContent = `${searches} search${searches === 1 ? "" : "es"}`;
          addToolCard(data.query);
        } else if (eventType === "tool_result") {
          completeToolCard(data.query, data.summary);
        } else if (eventType === "done") {
          planLoader.classList.add("hidden");
          planEl.classList.remove("hidden");
          planEl.innerHTML = marked.parse(data.plan || "");
          stats.classList.remove("hidden");
          $("statSearches").textContent = data.searches;
          $("statSources").textContent = data.sources;
          $("statTime").textContent = Math.round((Date.now() - startedAt) / 1000);
          $("liveDot").classList.remove("animate-pulse");
          $("liveDot").classList.add("bg-emerald-400");
        } else if (eventType === "error") {
          showBanner("Agent error: " + data.message, "error");
          planLoader.classList.add("hidden");
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
