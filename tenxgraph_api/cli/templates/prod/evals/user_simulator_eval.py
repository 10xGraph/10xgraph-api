"""
User simulator evaluation example.

The CLI detects get_scenarios() and handles everything:
  - runs each scenario through UserSimulator
  - scores goal achievement with an LLM judge
  - produces the same HTML + JSON report as regular eval cases

You only need to define your scenarios. No asyncio, no EvalSetBuilder, no boilerplate.

Usage:
    10xgraph eval evals/user_simulator_eval.py
    10xgraph eval evals/user_simulator_eval.py --parallel --max-concurrency 4
"""

from tenxgraph.qa.evaluation import ConversationScenario, UserSimulatorConfig

# ---------------------------------------------------------------------------
# Agent — the CLI reads `app` from this module if present, otherwise it loads
# the graph from 10xgraph.json ("agent": "graph.agent:app") automatically.
# Uncomment and set this if you want to target a specific graph:
#
#   from graph.agent import app
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Optional: override the simulator model and settings for this file.
# If omitted, the CLI uses UserSimulatorConfig defaults (gemini-2.5-flash).
# The provider prefix picks the SDK ("gemini/", "openai/", "anthropic/").
# ---------------------------------------------------------------------------
SIMULATOR_CONFIG = UserSimulatorConfig(
    model="$llm_sim_model",
    max_invocations=8,
    temperature=0.7,
)


# ---------------------------------------------------------------------------
# Scenarios — the only thing you need to write.
# ---------------------------------------------------------------------------

def get_scenarios() -> list[ConversationScenario]:
    return [
        ConversationScenario(
            scenario_id="weather_travel_planning",
            description="User planning a trip wants weather info and packing advice",
            starting_prompt="Hi! I'm planning a trip to Paris this weekend.",
            conversation_plan=(
                "1. Ask about current weather in Paris\n"
                "2. Ask whether to bring a jacket\n"
                "3. Ask about the best time for outdoor sightseeing"
            ),
            goals=[
                "User receives weather information for Paris",
                "User gets clothing or packing advice",
                "User learns about outdoor activity timing",
            ],
            max_turns=8,
        ),
        ConversationScenario(
            scenario_id="flight_booking_assistance",
            description="User wants help finding a flight from London to New York",
            starting_prompt="I need to fly from London to New York next Friday. Can you help?",
            conversation_plan=(
                "1. Share travel dates and seat preferences\n"
                "2. Ask about available flights and prices\n"
                "3. Confirm booking details"
            ),
            goals=[
                "User receives flight options for the requested route",
                "User gets pricing information",
                "Booking process is initiated or completed",
            ],
            max_turns=10,
        ),
    ]
