# Evaluation

Use this when defining eval cases, configuring criteria, running evaluations programmatically, generating reports, or using user simulation. All evaluation types live in `tenxgraph.qa.evaluation`.

---

## EvalSet and EvalCase

Define test cases with `EvalSetBuilder` (fluent) or `EvalCase` directly.

```python
from tenxgraph.qa.evaluation import EvalSetBuilder, EvalSet, EvalCase, ToolCall
```

**EvalSetBuilder — fluent API:**

```python
eval_set = (
    EvalSetBuilder("agent-regression")
    .add_case(
        query="Weather in London?",
        expected="sunny",
        case_id="london",                          # optional; auto-generated if omitted
        expected_tools=["get_weather"],            # list of tool names or ToolCall objects
        expected_node_order=["MAIN", "TOOL", "MAIN"],
    )
    .add_tool_test(
        query="Weather in Tokyo?",
        tool_name="get_weather",
        tool_args={"location": "Tokyo"},
        expected_response="Tokyo",
    )
    .add_multi_turn(
        conversation=[("Hello", "Hi!"), ("Weather?", "Sunny.")],
        expected_tools=["get_weather"],
    )
    .build()
)
```

Quick builders:

```python
EvalSetBuilder.quick(("Hello", "Hi!"), ("Bye", "Goodbye!"))
EvalSetBuilder.from_conversations([{"user": "Hello", "assistant": "Hi!"}])
EvalSetBuilder.from_file("evals/cases.json")   # returns builder for modification
```

Save / load:

```python
eval_set.to_file("evals/cases.json")
eval_set = EvalSet.from_file("evals/cases.json")
builder = EvalSetBuilder.from_file("evals/cases.json")  # load + modify
```

**ToolCall:**

```python
ToolCall(name="get_weather")                              # name only
ToolCall(name="get_weather", args={"location": "London"}) # with args (checked when check_args=True)
```

**EvalCase directly:**

```python
case = EvalCase.single_turn(
    eval_id="london",
    user_query="Weather in London?",
    expected_response="sunny",
    expected_tools=[ToolCall(name="get_weather", args={"location": "London"})],
    expected_node_order=["MAIN", "TOOL", "MAIN"],
)
case = EvalCase.multi_turn(
    eval_id="multi",
    conversation=[("Hello", "Hi!"), ("Weather?", "Sunny.")],
    expected_tools=[ToolCall(name="get_weather")],
)
```

---

## EvalConfig and CriterionConfig

```python
from tenxgraph.qa.evaluation import EvalConfig, CriterionConfig, MatchType, Rubric
```

**EvalConfig** wraps a dict of criterion key → `CriterionConfig`:

```python
config = EvalConfig(
    criteria={
        "tool_trajectory_avg_score": CriterionConfig.trajectory(threshold=1.0, match_type=MatchType.EXACT, check_args=False),
        "rouge_match": CriterionConfig.rouge_match(threshold=0.5),
        "response_match_score": CriterionConfig.response_match(threshold=0.8),
    },
    parallel=False,
    max_concurrency=4,
    timeout=300.0,
)
```

**MatchType** (for trajectory and node-order criteria):

- `EXACT` — same tools/nodes, same order, same count; extras fail
- `IN_ORDER` — expected appear in order; extras allowed
- `ANY_ORDER` — all expected present; order and extras don't matter

**Built-in class-method presets on EvalConfig:**

```python
EvalConfig.default()   # EXACT trajectory + ROUGE response (threshold 0.8)
EvalConfig.strict()    # EXACT + args + high-threshold LLM judge (threshold 0.9, 5 samples)
EvalConfig.relaxed()   # IN_ORDER trajectory + lower response threshold (0.6)
```

**CriterionConfig factory methods:**

| Method | LLM? | Key to use in criteria dict |
|---|---|---|
| `CriterionConfig.tool_name_match(threshold)` | No | `tool_name_match_score` |
| `CriterionConfig.trajectory(threshold, match_type, check_args)` | No | `tool_trajectory_avg_score` |
| `CriterionConfig.node_order(threshold, match_type)` | No | `node_order_match_score` |
| `CriterionConfig.rouge_match(threshold)` | No | `rouge_match` |
| `CriterionConfig.contains_keywords(keywords, threshold)` | No | `contains_keywords` |
| `CriterionConfig.response_match(threshold, judge_model, num_samples)` | Yes | `response_match_score` |
| `CriterionConfig.llm_judge(threshold, judge_model, num_samples)` | Yes | `llm_judge` |
| `CriterionConfig.factual_accuracy(threshold, judge_model, num_samples)` | Yes | `factual_accuracy_v1` |
| `CriterionConfig.hallucination(threshold, judge_model, num_samples)` | Yes | `hallucinations_v1` |
| `CriterionConfig.safety(threshold, judge_model, num_samples)` | Yes | `safety_v1` |
| `CriterionConfig.rubric_based(rubrics, threshold, judge_model)` | Yes | any key |

Default judge model: `"gemini-2.5-flash"`. Override per criterion with `judge_model="gpt-4o"`.

`num_samples` — number of LLM judge calls; final score is the average (reduces non-determinism noise).

**Custom rubrics:**

```python
CriterionConfig.rubric_based(
    rubrics=[
        Rubric(rubric_id="concise", content="Response must be under 100 words.", weight=1.0),
        Rubric(rubric_id="empathetic", content="Acknowledge the user's issue first.", weight=0.5),
    ],
    threshold=0.8,
)
```

Score = weighted average of rubric scores.

**Disable / enable a criterion at runtime:**

```python
config.disable_criterion("rouge_match")
config.enable_criterion("rouge_match", CriterionConfig.rouge_match(0.4))
```

**Save / load:**

```python
config.to_file("eval_config.json")
config = EvalConfig.from_file("eval_config.json")
```

---

## EvalPresets

Ready-made `EvalConfig` objects for common scenarios.

```python
from tenxgraph.qa.evaluation import EvalPresets
```

| Preset | LLM? | Criteria included |
|---|---|---|
| `EvalPresets.quick_check()` | No | `rouge_match` (0.5) |
| `EvalPresets.tool_usage(threshold, strict, check_args)` | No | `tool_name_match_score` + `tool_trajectory_avg_score` |
| `EvalPresets.response_quality(threshold, use_llm_judge, judge_model)` | Yes | `response_match_score` + optional `llm_judge` |
| `EvalPresets.conversation_flow(threshold, judge_model)` | Yes | `response_match_score` + `tool_trajectory_avg_score` (IN_ORDER) |
| `EvalPresets.safety_check(threshold, judge_model)` | Yes | `hallucinations_v1` + `safety_v1` |
| `EvalPresets.comprehensive(threshold, use_llm_judge, judge_model)` | Yes | all no-LLM + all LLM criteria |
| `EvalPresets.custom(response_threshold, tool_threshold, llm_judge_threshold, ...)` | Both | only criteria whose threshold is not None |

Combine presets (later keys override earlier on conflict):

```python
config = EvalPresets.combine(
    EvalPresets.tool_usage(threshold=1.0),
    EvalPresets.safety_check(threshold=0.8),
)
```

---

## AgentEvaluator and QuickEval

```python
from tenxgraph.qa.evaluation import AgentEvaluator, QuickEval
from tenxgraph.qa.evaluation.collectors.trajectory_collector import TrajectoryCollector
```

**TrajectoryCollector** — must be created before evaluation and passed to `AgentEvaluator`. Captures tool calls, node visits, and LLM outputs through the graph callback system.

```python
collector = TrajectoryCollector(capture_all_events=True)
```

**AgentEvaluator** — runs the full eval set:

```python
evaluator = AgentEvaluator(compiled_graph, collector, config=config)
report = await evaluator.evaluate(eval_set)                    # EvalSet object
report = await evaluator.evaluate("evals/cases.json")          # path to JSON
report = await evaluator.evaluate_case(single_case)            # one EvalCase
```

**QuickEval** — one-liners for common patterns:

```python
# Single check
report = await QuickEval.check(
    graph=app, collector=collector,
    query="Weather in London?",
    expected_response_contains="sunny",
    expected_tools=["get_weather"],
    threshold=0.7,
)

# Preset-based batch
report = await QuickEval.preset(
    graph=app, collector=collector,
    preset=EvalPresets.tool_usage(),
    eval_set=eval_set,
)

# Query-response pairs
report = await QuickEval.batch(
    app, collector,
    [("Hello", "Hi!"), ("Bye", "Goodbye!")],
    threshold=0.7,
)

# Tool-specific
report = await QuickEval.tool_usage(
    app, collector,
    test_cases=[("Weather in London?", "sunny", ["get_weather"])],
    strict=True,
)

# Multi-turn conversation
report = await QuickEval.conversation_flow(
    app, collector,
    conversation=[("Hello", "Hi!"), ("Weather?", "Sunny.")],
    threshold=0.8,
)

# Builder-based
report = await QuickEval.from_builder(app, collector, builder=my_builder, config=config)

# Synchronous wrapper (for non-async contexts)
report = QuickEval.run_sync(app, collector, eval_set=eval_set, config=config)
```

---

## EvalReport

```python
report.summary.total_cases         # int
report.summary.passed_cases         # int
report.summary.failed_cases         # int
report.summary.pass_rate            # float 0.0–1.0
report.summary.criteria_scores      # dict[str, float] — average per criterion

for result in report.results:
    result.case_id
    result.passed                   # bool
    result.criteria_results         # dict[str, CriterionResult]
    result.criteria_results["rouge_match"].score   # float
    result.criteria_results["rouge_match"].passed  # bool
```

Print to console:

```python
from tenxgraph.qa.evaluation import print_report
print_report(report)
```

---

## Reporters

```python
from tenxgraph.qa.evaluation import ReporterManager, ReporterConfig

manager = ReporterManager(ReporterConfig(
    output_dir="eval_reports",
    html=True,
    json_report=True,
    junit_xml=False,
    console=True,
    timestamp_files=True,
    include_trajectory=True,
    include_tool_call_details=True,
))
output = manager.run_all(report)
# output.html_path, output.json_path, output.has_errors, output.errors
```

Available reporters: `ConsoleReporter`, `JSONReporter`, `HTMLReporter`, `JUnitXMLReporter`.

HTML report — visual dashboard with summary cards, criterion bars, per-case details.
JSON report — full machine-readable results including trajectory data.
JUnit XML — for CI test summary tabs (GitHub Actions, Jenkins).

---

## 10xgraph eval CLI

Auto-discovers `*_eval.py` / `eval_*.py` in `evals/` (or configured directory). Each file must expose `get_eval_set()`, optionally `get_eval_config()` or `EVAL_CONFIG`, or a full `run()` function. The CLI writes a JSON report only (no HTML, no browser); open it in the playground with Evals > Open report file.

```bash
10xgraph eval                          # scan evals/, write a JSON report to eval_reports/
10xgraph eval evals/my_eval.py         # single file
10xgraph eval evals/regression/        # subdirectory
10xgraph eval --threshold 0.8          # exit non-zero if pass rate < 0.8
10xgraph eval --output ci/reports      # custom output directory
10xgraph eval --no-report              # console only, no files
```

Eval file entry points (in order of priority):

1. `run()` — sync or async function returning `EvalReport`; full control
2. `get_eval_set()` + optional `get_eval_config()` or `EVAL_CONFIG` — CLI loads agent from `10xgraph.json` and runs with default or provided config
3. Files without any entry point are skipped with a warning

`10xgraph.json` configuration:

```json
{
  "evaluation": {
    "directory": "evals",
    "output_dir": "eval_reports",
    "threshold": 0.8,
    "timestamp_files": true
  }
}
```

Exit code: 0 only when pass rate is 100% (or meets threshold with no errors). Always 1 when any case fails.

---

## User Simulation

Use `UserSimulator` when fixed test cases are not enough — the simulator uses an LLM to play the role of a user, drives a real conversation with the agent, and checks whether goals are achieved turn by turn.

```python
from tenxgraph.qa.evaluation import (
    UserSimulator, ConversationScenario, BatchSimulator,
    SimulationGoalsCriterion, CriterionConfig, UserSimulatorConfig,
)
```

**ConversationScenario** — defines what the simulated user wants to accomplish:

```python
scenario = ConversationScenario(
    scenario_id="travel_planning",
    description="User wants to plan a weekend trip.",
    starting_prompt="I'm thinking of going somewhere warm this weekend.",
    conversation_plan="1. Ask about destinations\n2. Narrow down\n3. Ask about flights",
    goals=[
        "Get weather info for at least one destination",
        "Receive flight or travel suggestions",
    ],
    max_turns=8,       # hard cap; default 10
    metadata={},       # arbitrary; passed through to results
)
```

If `starting_prompt` is empty, the simulator LLM generates the first message from `description` and `conversation_plan`.

**UserSimulator** — runs the conversation loop:

```python
simulator = UserSimulator(
    model="gemini/gemini-2.5-flash",  # "gpt-4o" also supported
    temperature=0.7,
    max_turns=10,
    criteria=[],                       # BaseCriterion instances scored after the simulation
)

result = await simulator.run(compiled_graph, scenario, config={"configurable": {"thread_id": "sim-1"}})
```

Via `UserSimulatorConfig`:

```python
simulator = UserSimulator(config=UserSimulatorConfig(
    model="gemini/gemini-2.5-flash",
    max_invocations=12,
    temperature=0.5,
    thinking_enabled=False,
    thinking_budget=10240,
))
```

Model routing: model strings starting with `gemini/` or matching a Google model name use Google GenAI; `anthropic/`, `claude/`, or a `claude-*` name use Anthropic; everything else uses OpenAI. A recognised `provider/` prefix is stripped before the name reaches the SDK. Falls back to the other provider if the primary call fails.

**SimulationResult:**

```python
result.scenario_id         # str
result.turns               # int — how many turns ran
result.completed           # bool — True when all goals achieved before max_turns
result.goals_achieved      # list[str] — goal strings confirmed by the LLM goal-checker
result.conversation        # list[dict] — [{"role": "user"/"assistant", "content": str}]
result.error               # str | None
result.criterion_scores    # dict[str, float] — only populated when criteria passed to simulator
result.criterion_details   # dict[str, Any] — full criterion output including reasoning
```

**SimulationGoalsCriterion** — LLM judge that scores the full conversation transcript against the goals. Designed exclusively for `UserSimulator`; do NOT add to a regular `EvalConfig` (it expects the full transcript, not a single response).

```python
judge = SimulationGoalsCriterion(
    config=CriterionConfig(threshold=0.7, judge_model="gemini-2.5-flash")
)
simulator = UserSimulator(model="gemini/gemini-2.5-flash", criteria=[judge])
result = await simulator.run(graph, scenario)
# result.criterion_scores["simulation_goals"] → float 0.0–1.0
# result.criterion_details["simulation_goals"] → {"achieved_goals": [...], "unachieved_goals": [...], "reasoning": "..."}
```

Score = `achieved_goals / total_goals`.

**BatchSimulator** — runs multiple scenarios concurrently; each gets its own thread ID:

```python
batch = BatchSimulator(simulator=simulator, max_concurrency=5)
results = await batch.run_batch(compiled_graph, scenarios)

summary = batch.summary(results)
# summary["total_scenarios"], ["completed"], ["completion_rate"],
# ["total_goals_achieved"], ["average_turns"], ["errors"]
```

**Goal-checking mechanism** — after each agent turn, the simulator sends the conversation and each unachieved goal to the judge LLM. Falls back to keyword matching if the LLM call fails. Simulation ends early when all goals are achieved.

**When to use simulation vs standard evaluation:**

- `AgentEvaluator` + `EvalSet` → regression testing with known queries and expected responses
- `UserSimulator` → open-ended multi-turn behaviour, goal achievement, conversation quality under dynamic input

---

## pytest Integration Helpers

```python
from tenxgraph.qa.evaluation import (
    eval_test, parametrize_eval_cases, run_eval,
    assert_eval_passed, assert_criterion_passed,
    EvalTestCase, EvalFixtures, EvalPlugin,
    create_eval_app, create_simple_eval_set,
)
```

- `eval_test` — decorator to mark a test as an eval test
- `parametrize_eval_cases(eval_set)` — `@pytest.mark.parametrize` over all eval cases
- `run_eval(graph, eval_set, config)` — sync helper that calls `asyncio.run`
- `assert_eval_passed(report)` — raises `AssertionError` if `pass_rate < 1.0`
- `assert_criterion_passed(result, criterion_key)` — raises if named criterion failed

---

## Rules

- Use no-LLM criteria (`tool_usage`, `quick_check`) in fast CI; add LLM-judge criteria only in slower quality gates.
- Compile the graph once with `TrajectoryCollector` wired in; reuse across all eval cases.
- Never add `SimulationGoalsCriterion` to `EvalConfig` — it is only valid inside `UserSimulator.criteria`.
- Set `threshold` in `10xgraph.json` so CI fails automatically without extra flags.
- Use `timestamp_files: true` so report files from different runs do not overwrite each other.

## Source Map

- Evaluation package: https://github.com/10xGraph/10xGraph/tree/main/tenxgraph/qa/evaluation
- EvalSetBuilder: https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/qa/evaluation/dataset/builder.py
- EvalConfig / CriterionConfig: https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/qa/evaluation/config/eval_config.py
- EvalPresets: https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/qa/evaluation/config/presets.py
- AgentEvaluator: https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/qa/evaluation/evaluator.py
- QuickEval: https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/qa/evaluation/quick_eval.py
- UserSimulator / BatchSimulator: https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/qa/evaluation/simulators/user_simulator.py
- SimulationGoalsCriterion: https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/qa/evaluation/criteria/simulation_goals.py
- CLI eval command: https://github.com/10xGraph/10xgraph-api/blob/main/tenxgraph_api/cli/commands/eval.py
- Public exports: https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/qa/__init__.py
