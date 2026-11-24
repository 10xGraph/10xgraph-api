
from pydantic import BaseModel

from tenxgraph_api.src.app.utils.swagger_helper import generate_swagger_responses


class DemoModel(BaseModel):
    id: int
    name: str


def test_generate_swagger_responses_basic():
    responses = generate_swagger_responses(DemoModel)
    assert 200 in responses
    assert responses[200]["model"].__name__.startswith("_SwaggerSuccessSchemas")
    assert responses[400]["description"] == "Invalid input"


def test_generate_swagger_responses_pagination():
    responses = generate_swagger_responses(DemoModel, show_pagination=True)
    assert responses[200]["model"].__name__.startswith("_SwaggerSuccessPaginationSchemas")


