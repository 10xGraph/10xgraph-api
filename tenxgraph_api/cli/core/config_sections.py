"""The ``test`` and ``evaluation`` blocks of 10xgraph.json (``10xgraph test`` and ``eval``)."""

from pydantic import BaseModel, Field, StrictBool, StrictFloat, StrictInt, StrictStr


class TestConfig(BaseModel):
    __test__ = False  # not a pytest class

    path: StrictStr | None = None
    coverage: StrictBool | None = None
    coverage_threshold: StrictInt | None = Field(default=None, ge=0, le=100)


class EvaluationConfig(BaseModel):
    directory: StrictStr | None = None
    output_dir: StrictStr | None = None
    threshold: StrictInt | StrictFloat | None = Field(default=None, ge=0, le=1)
    parallel: StrictBool | None = None
    open_report: StrictBool | None = None
    max_concurrency: StrictInt | None = Field(default=None, ge=1)
