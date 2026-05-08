import streamlit as st
from agent import run_agent

# ── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="TravelMind AI",
    page_icon="✈️",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ── Custom CSS ────────────────────────────────────────────────────────────────
st.markdown("""
<style>
    .main { background-color: #0f1117; }
    .tool-card {
        background: #1e2130;
        border-left: 3px solid #4f8ef7;
        border-radius: 6px;
        padding: 10px 14px;
        margin: 6px 0;
        font-size: 0.88rem;
    }
    .tool-card .query { color: #4f8ef7; font-weight: 600; }
    .tool-card .preview { color: #8b92a5; margin-top: 4px; }
    .result-box {
        background: #1a1d2e;
        border: 1px solid #2d3250;
        border-radius: 10px;
        padding: 20px;
    }
    .stButton > button {
        background: linear-gradient(135deg, #4f8ef7, #7b5ea7);
        color: white;
        border: none;
        border-radius: 8px;
        font-weight: 600;
        font-size: 1rem;
        padding: 0.6rem 2rem;
    }
    .stButton > button:disabled {
        background: #2d3250;
        color: #8b92a5;
        cursor: not-allowed;
        opacity: 0.75;
    }
    .metric-box {
        background: #1e2130;
        border-radius: 8px;
        padding: 12px;
        text-align: center;
    }
</style>
""", unsafe_allow_html=True)

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## ✈️ TravelMind")
    st.markdown("*AI-powered travel planning agent*")
    st.markdown("---")

    st.markdown("### 🔑 API Keys")
    gemini_key = st.text_input("Gemini API Key", type="password", placeholder="AIza...")
    tavily_key = st.text_input("Tavily API Key", type="password", placeholder="tvly-...")

    st.markdown("---")
    st.markdown("### 💡 Try these")
    examples = [
        "5 days in Tokyo for 2 people, $3000 budget, from Boston in July",
        "4-day trip to Bali, $2000 budget, solo traveler from NYC in August",
        "Weekend getaway to Montreal from Boston, $600 budget",
        "10 days in Italy (Rome + Florence + Venice), $5000 for 2, from Chicago in September",
    ]
    for ex in examples:
        if st.button(f"📍 {ex[:45]}...", key=ex, use_container_width=True):
            st.session_state["prefill"] = ex

    st.markdown("---")
    st.markdown("Built with Gemini + Tavily Search")

# ── Header ────────────────────────────────────────────────────────────────────
st.markdown("# ✈️ TravelMind AI Agent")
st.markdown("Describe your trip and watch the agent research flights, hotels, and attractions in real time.")

# ── Input ─────────────────────────────────────────────────────────────────────
prefill = st.session_state.pop("prefill", "")
query = st.text_area(
    "**Your trip request**",
    value=prefill,
    placeholder="e.g. Plan a 5-day trip to Tokyo for 2 people, $3000 total budget, flying from Boston in July",
    height=90,
    label_visibility="visible"
)

missing_fields = []
if not gemini_key:
    missing_fields.append("Gemini API key")
if not tavily_key:
    missing_fields.append("Tavily API key")
if not query.strip():
    missing_fields.append("trip request")

ready = not missing_fields
plan_btn = st.button("🚀 Plan My Trip", type="primary")

if missing_fields:
    st.caption(f"⬅️ Add: {', '.join(missing_fields)}.")

# ── Agent Execution ───────────────────────────────────────────────────────────
if plan_btn and not ready:
    st.warning(f"Missing: {', '.join(missing_fields)}.")

if plan_btn and ready:
    st.markdown("---")
    st.info("Starting TravelMind. Calling Gemini now...")
    col_agent, col_plan = st.columns([1, 1.6], gap="large")

    with col_agent:
        st.markdown("### 🤖 Agent Activity")
        st.caption("Live tool calls as the agent researches your trip")
        activity_area = st.empty()
        tool_log = []  # list of dicts: {query, preview}

        def refresh_activity():
            html_parts = []
            for i, entry in enumerate(tool_log):
                preview = entry.get("preview", "")[:120]
                html_parts.append(f"""
                <div class="tool-card">
                    <div class="query">🔍 {entry['query']}</div>
                    <div class="preview">{preview}…</div>
                </div>
                """)
            if not html_parts:
                html_parts = ["<div style='color:#8b92a5;font-size:0.9rem;'>Waiting for agent to start…</div>"]
            activity_area.markdown("\n".join(html_parts), unsafe_allow_html=True)

        refresh_activity()

        def on_tool_call(query: str):
            tool_log.append({"query": query, "preview": "Fetching results…"})
            refresh_activity()

        def on_tool_result(query: str, preview: str):
            for entry in reversed(tool_log):
                if entry["query"] == query:
                    entry["preview"] = preview
                    break
            refresh_activity()

    with col_plan:
        st.markdown("### 📋 Your Travel Plan")
        plan_placeholder = st.empty()
        plan_placeholder.info("⏳ Agent is researching your trip. This takes about 20–40 seconds…")

    # Run the agent (blocking — Streamlit updates via callbacks)
    try:
        with st.spinner("Gemini is planning search steps..."):
            final_plan, calls = run_agent(
                user_query=query,
                gemini_key=gemini_key,
                tavily_key=tavily_key,
                on_tool_call=on_tool_call,
                on_tool_result=on_tool_result
            )

        # Update agent panel with final count
        with col_agent:
            st.success(f"✅ Completed {len(calls)} searches")
            st.markdown(f"**Searches performed:** {len(calls)}")

        # Show the plan
        with col_plan:
            plan_placeholder.empty()
            st.markdown(final_plan)

        # Stats row
        st.markdown("---")
        m1, m2, m3 = st.columns(3)
        m1.metric("Searches Run", len(calls))
        m2.metric("Sources Consulted", len(calls) * 5)
        m3.metric("Status", "✅ Complete")

    except Exception as e:
        st.error(f"Agent error: {str(e)}")
        st.caption("Check that your API keys are valid and have available credits.")
