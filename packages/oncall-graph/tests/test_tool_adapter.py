"""``_as_langchain_tool`` accepts a schema-carrying MCP tool.

The specialist loop calls the original object's ``invoke``. This adapter only
decides what parameter names the model is shown.
"""

import pytest
from langchain_core.tools import StructuredTool
from oncall_graph.agent import Tool, _as_langchain_tool
from oncall_tools.registry import ToolSpec


def _search_schema():
    return {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "k": {"type": "integer", "default": 5},
        },
        "required": ["query"],
    }


def test_a_schema_carrying_tool_advertises_the_servers_parameters():
    seen = {}

    def invoke(args=None, /, **kwargs):
        seen["args"] = args
        seen["kwargs"] = dict(kwargs)
        return {"hits": []}

    schema = _search_schema()
    spec = ToolSpec(
        name="search_runbooks",
        description="search the corpus",
        schema=schema,
        write=False,
        server="runbooks",
        invoke=invoke,
    )
    bound = _as_langchain_tool(spec)

    assert isinstance(bound, StructuredTool)
    assert bound.name == "search_runbooks"
    assert bound.description == "search the corpus"
    advertised = bound.tool_call_schema
    assert set(advertised["properties"]) == {"query", "k"}
    assert "args" not in advertised["properties"]
    assert advertised["required"] == ["query"]

    schema["required"] = ["query", "k"]
    assert bound.tool_call_schema["required"] == ["query"]

    assert bound.invoke({"query": "payment", "k": 2}) == {"hits": []}
    assert seen == {"args": None, "kwargs": {"query": "payment", "k": 2}}


def test_a_tool_with_a_typed_fn_still_uses_its_signature():
    def promql(query: str) -> dict:
        """Run a PromQL query."""
        return {"query": query}

    bound = _as_langchain_tool(Tool("promql", promql))

    assert isinstance(bound, StructuredTool)
    advertised = bound.tool_call_schema.model_json_schema()
    assert set(advertised["properties"]) == {"query"}
    assert bound.invoke({"query": "error_rate"}) == {"query": "error_rate"}


def test_a_langchain_tool_passes_through():
    def ping() -> str:
        """Answer ping."""
        return "pong"

    tool = StructuredTool.from_function(ping, name="ping", description="Answer ping.")
    assert _as_langchain_tool(tool) is tool


def test_a_tool_with_neither_fn_nor_schema_is_refused():
    class _Bare:
        name = "nope"

    with pytest.raises(TypeError, match="JSON Schema and invoke"):
        _as_langchain_tool(_Bare())
