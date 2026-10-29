from tenxgraph.qa.evaluation import (
    CriteriaConfig,
    CriterionConfig,
    EvalConfig,
    EvalSet,
    EvalSetBuilder,
    MatchType,
)
from tenxgraph.qa.evaluation.config.presets import EvalPresets


def get_eval_config() -> EvalConfig:
    """Evaluation config for this file.

    EvalPresets provides ready-made criterion bundles.  Pick one that matches
    what your agent does, or call EvalPresets.combine() to mix them.

    Available presets:
        EvalPresets.response_quality()   - LLM judge on response accuracy
        EvalPresets.tool_usage()         - checks tool names + trajectory (no LLM)
        EvalPresets.conversation_flow()  - multi-turn conversation evaluation
        EvalPresets.comprehensive()      - all of the above combined
        EvalPresets.quick_check()        - fast ROUGE-based response check, no LLM cost

    This file checks, without any LLM call, every expectation the cases below set:
        - tool names and arguments           (tool_usage)
        - the expected final response        (quick_check, ROUGE-1 word overlap)
        - the expected node order            (node_order, extra nodes allowed)
    """
    return EvalPresets.combine(
        EvalPresets.tool_usage(threshold=0.6),
        EvalPresets.quick_check(),
        EvalConfig(
            criteria=CriteriaConfig(
                node_order=CriterionConfig.node_order(match_type=MatchType.IN_ORDER),
            )
        ),
    )


def get_eval_set() -> EvalSet:
    return (
        EvalSetBuilder(name="weather-agent-regression")
        .add_case(
            query="Hi",
            expected="Hello! How can I help you today?",
            expected_node_order=["MAIN"],
            name="greeting_response",
            description="The agent responds to a greeting.",
        )
        .add_tool_test(
            query="What is the weather in London?",
            tool_name="get_weather",
            tool_args={"location": "London"},
            expected_response="The weather in London is sunny.",
            case_id="weather_london",
        )
        .add_tool_test(
            query="Tell me the current weather in New York",
            tool_name="get_weather",
            tool_args={"location": "New York"},
            expected_response="The weather in New York is sunny.",
            case_id="weather_new_york",
        )
        .add_tool_test(
            query="How is the weather in Tokyo today?",
            tool_name="get_weather",
            tool_args={"location": "Tokyo"},
            expected_response="The weather in Tokyo is sunny.",
            case_id="weather_tokyo",
        )
        .build()
    )
