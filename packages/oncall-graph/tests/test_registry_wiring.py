"""A tool registry reaches the specialists. A missing tool is not stubbed."""

from langchain_core.messages import AIMessage
from oncall_graph.build import build_graph
from oncall_graph.stubs import TELEMETRY_TOOLS, telemetry_tools
from oncall_graph.topologies.c1_single import Solo
from oncall_graph.topologies.c2_team import Team
from oncall_tools.registry import ToolRegistry, ToolSpec


class _Quiet:
    def invoke(self, _messages):
        return AIMessage(content="ok")


def _spec(name, *, write=False):
    return ToolSpec(
        name=name,
        description=name,
        schema={"type": "object", "properties": {}, "required": []},
        write=write,
        server="ops_actions" if write else "telemetry",
        invoke=lambda args=None, **_kwargs: {"name": name},
    )


def _registry():
    return ToolRegistry(
        [
            _spec("promql"),
            _spec("log_search"),
            _spec("traces"),
            _spec("search_runbooks"),
            _spec("set_flag", write=True),
        ]
    )


def test_the_solo_agent_reads_from_the_registry_and_still_proposes():
    registry = _registry()
    solo = Solo(model=_Quiet(), tools=registry)
    names = list(solo.agent.tools)

    assert names == ["promql", "log_search", "traces", "search_runbooks", "set_flag"]
    assert solo.agent.tools["promql"] is registry["promql"]
    assert "incident_record" not in solo.agent.tools
    assert "status_draft" not in solo.agent.tools
    assert solo.agent.tools["set_flag"] is not registry["set_flag"]


def test_the_team_omits_a_read_the_registry_does_not_have():
    team = Team(tools=_registry())
    names = [tool.name for tool in team._specialist_tools(TELEMETRY_TOOLS, telemetry_tools)]

    assert names == ["promql", "log_search", "traces"]
    assert "recent_changes" not in names


def test_build_graph_accepts_a_registry_for_both_topologies():
    registry = _registry()
    solo = build_graph(registry, topology="c1_single", model=_Quiet())
    team = build_graph(registry, topology="c2_team", model=_Quiet())

    assert "solo" in solo.get_graph().draw_mermaid()
    assert "triage" in team.get_graph().draw_mermaid()
