# Testing and Evaluation

Use this when writing tests, mocks, eval datasets, automated quality checks, or trajectory assertions.

## Testing Helpers

Import from `tenxgraph.qa.testing` or `tenxgraph.qa`.

Key helpers:

- `TestAgent`: model-free agent double that returns predefined responses and can simulate tool calls.
- `QuickTest`: quick single-turn, multi-turn, tool, and custom graph tests.
- `TestResult`: fluent assertions for output text, tool calls, message counts, and errors.
- `TestContext`: context manager for creating graphs, agents, stores, and mocks.
- `MockToolRegistry`: register sync/async mock tools and assert calls.
- `MockMCPClient`: mock MCP tool listing/calls.
- `InMemoryStore`: deterministic memory store for tests.

Use these instead of real model/provider calls in unit tests.

## Evaluation Framework

Import from `tenxgraph.qa.evaluation` or `tenxgraph.qa`.

Core dataset/results types:

- `EvalCase`, `EvalSet`, `EvalSetBuilder`
- `ToolCall`, `Invocation`, `TrajectoryStep`, `StepType`
- `EvalConfig`, `CriterionConfig`, `MatchType`, `Rubric`
- `EvalReport`, `EvalCaseResult`, `CriterionResult`

Runner and shortcuts:

- `AgentEvaluator`
- `QuickEval`
- `run_eval`
- `create_eval_app`
- `create_simple_eval_set`
- `eval_test`
- `parametrize_eval_cases`

Criteria/reporters:

- Tool, trajectory, node-order, response, exact-match, keyword, ROUGE, rubric, safety, hallucination, factual accuracy, LLM judge, and simulation criteria.
- Console, JSON, HTML, JUnit XML, and reporter manager outputs.

## Trajectory Collection

Use `TrajectoryCollector` plus `make_trajectory_callback` to record node/tool execution through the callback system. Compile the graph once with the callback manager and reuse it for eval runs.

## Rules

- Keep unit tests model-free with `TestAgent` and mocks.
- Use evals for behavior quality, trajectory matching, safety, and regression checks.
- Avoid mixing live providers into fast unit tests; isolate them as integration tests.
- For trajectory evals, compile once with the collector callback to avoid losing callback state.

## Source Map

- Testing package: https://github.com/10xGraph/10xGraph/tree/main/tenxgraph/qa/testing
- Evaluation package: https://github.com/10xGraph/10xGraph/tree/main/tenxgraph/qa/evaluation
- Public exports: https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/qa/__init__.py
- Docs: https://10xgraph.com/
