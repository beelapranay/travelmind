from google import genai
from google.genai import types

SYSTEM_PROMPT = """You are TravelMind, an expert AI travel planning agent. When given a travel request, you MUST:

1. Search for flight options (origin → destination, approximate costs, airlines)
2. Search for hotel/accommodation options within the stated budget
3. Search for top attractions and activities at the destination
4. Search for local food/restaurant recommendations
5. Search for weather and best time to visit tips
6. Synthesize everything into a detailed, actionable travel plan

Rules:
- Always make AT LEAST 4 separate searches before writing the final plan
- Each search should be targeted and specific (e.g. "Boston to Tokyo flights July 2025 price")
- After all searches, produce a structured response with:
  * ✈️ Flight Options (estimated costs, airlines, duration)
  * 🏨 Accommodation Picks (3 options at different price points)
  * 📅 Day-by-Day Itinerary
  * 🍜 Must-Try Food & Restaurants
  * 💰 Budget Breakdown
  * 🌤️ Weather & Packing Tips
- Be specific with prices, names, and logistics. This is a real plan, not generic advice."""


GEMINI_MODEL = "gemini-2.5-flash"


def get_tools():
    return [
        types.Tool(
            function_declarations=[
                types.FunctionDeclaration(
                    name="web_search",
                    description="Search the web for real-time travel information: flights, hotels, attractions, weather, travel tips, visa requirements, and more.",
                    parameters=types.Schema(
                        type=types.Type.OBJECT,
                        properties={
                            "query": types.Schema(
                                type=types.Type.STRING,
                                description="Specific search query for travel information",
                            )
                        },
                        required=["query"],
                    ),
                )
            ]
        )
    ]


def run_agent(user_query: str, gemini_key: str, tavily_key: str, on_tool_call=None, on_tool_result=None):
    """
    Run the travel planning agent.
    
    on_tool_call(query: str) -> called when agent fires a search
    on_tool_result(query: str, summary: str) -> called when search result is received
    
    Returns: (final_plan: str, tool_calls: list)
    """
    try:
        from tavily import TavilyClient
    except ImportError:
        raise ImportError("Run: pip install tavily-python")

    client = genai.Client(api_key=gemini_key)
    tavily = TavilyClient(api_key=tavily_key)

    contents = [
        types.Content(
            role="user",
            parts=[types.Part.from_text(text=user_query)],
        )
    ]
    tool_calls_log = []
    iteration = 0
    max_iterations = 10  # Safety cap

    while iteration < max_iterations:
        iteration += 1

        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                max_output_tokens=4096,
                tools=get_tools(),
            ),
        )

        function_calls = response.function_calls or []

        # Agent is done - extract final text
        if not function_calls:
            return response.text or "Agent did not produce a final plan.", tool_calls_log

        # Agent wants to use tools
        model_content = response.candidates[0].content if response.candidates else None
        if model_content:
            contents.append(model_content)

        function_response_parts = []

        for call in function_calls:
            if call.name != "web_search":
                continue

            query = (call.args or {}).get("query", "")

            # Notify UI: tool call fired
            if on_tool_call:
                on_tool_call(query)

            # Execute the search
            try:
                result = tavily.search(query, max_results=5, search_depth="basic")
                results_list = result.get("results", [])

                # Format results for the agent
                formatted = []
                for r in results_list:
                    title = r.get("title", "")
                    content = r.get("content", "")[:300]
                    url = r.get("url", "")
                    formatted.append(f"**{title}**\n{content}\nSource: {url}")

                search_content = "\n\n".join(formatted) if formatted else "No results found."
                summary = results_list[0].get("content", "")[:150] if results_list else "No data"

            except Exception as e:
                search_content = f"Search failed: {str(e)}"
                summary = "Search failed"

            # Notify UI: result received
            if on_tool_result:
                on_tool_result(query, summary)

            tool_calls_log.append({
                "query": query,
                "result_preview": summary
            })

            function_response_parts.append(
                types.Part.from_function_response(
                    name="web_search",
                    response={"result": search_content},
                )
            )

        if function_response_parts:
            contents.append(types.UserContent(parts=function_response_parts))

    return "Agent did not produce a final plan.", tool_calls_log
