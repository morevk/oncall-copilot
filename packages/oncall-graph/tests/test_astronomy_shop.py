"""Live telemetry is opt-in. The default shop stays the scripted fixture.

The client's own tests moved with it, to
`packages/oncall-tools/tests/test_telemetry.py`. What is left here is the part
that is P2's: that `Solo` defaults to the stub, and that `LiveShop` reads live
while its writes stay scripted.
"""

import pytest
from oncall_graph.astronomy_shop import (
    AstronomyShop,
    LiveShop,
    RegistryShop,
    TelemetryNotConfigured,
)
from oncall_graph.stubs import Shop
from oncall_graph.topologies.c1_single import Solo
from oncall_tools.registry import ToolRegistry, ToolSpec


class _Silent:
    model_id = "test"

    def invoke(self, messages):
        raise AssertionError("the stub shop test does not call a model")


def test_default_solo_shop_is_the_stub(capsys):
    solo = Solo(model=_Silent())
    assert type(solo.shop) is Shop
    assert solo.shop.promql("checkout_error_rate")["fault"] == "paymentFailure"
    logged = capsys.readouterr().out
    assert "scripted shop promql" in logged
    assert "astronomy shop" not in logged


def test_live_shop_reads_telemetry_and_keeps_stub_writes(monkeypatch, capsys):
    monkeypatch.setattr(
        AstronomyShop,
        "promql",
        lambda self, query: {"query": query, "series": []},
    )
    shop = LiveShop(host="lab.example")
    assert shop.promql("up") == {"query": "up", "series": []}
    assert shop.reads[-1] == ("promql", {"query": "up"})
    assert shop.flags["paymentFailure"] == "100%"
    shop.apply("set_flag", {"flag": "paymentFailure", "variant": "off"})
    assert shop.flags["paymentFailure"] == "off"

    # The trace line stays on this side of the move: these prints are what the
    # terminal demo shows, and stdout is safe here in a way it is not in a
    # stdio MCP server.
    assert "astronomy shop promql" in capsys.readouterr().out


def test_live_shop_without_the_three_urls_refuses_rather_than_guessing(monkeypatch):
    """`LiveShop()` used to query 10.241.2.69, a NodePort address of a cluster
    that is not this stack. Compose sets all three URLs on oncall-service, so
    the container path is unchanged and only a host-side run sees this."""
    for name in ("PROMETHEUS_URL", "JAEGER_URL", "OPENSEARCH_URL"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(TelemetryNotConfigured):
        LiveShop()


def _spec(name, result, *, write=False):
    return ToolSpec(
        name=name,
        description=name,
        schema={},
        write=write,
        server="ops_actions" if write else "telemetry",
        invoke=lambda _args: result,
    )


def test_registry_shop_executes_and_audits_the_approved_mcp_write():
    result = {"previous": "100%", "variant": "off", "changed": True}
    shop = RegistryShop(ToolRegistry([_spec("set_flag", result, write=True)]))

    assert shop.apply("set_flag", {"flag": "paymentFailure", "variant": "off"}) == result
    assert shop.writes == [
        {"tool": "set_flag", "args": {"flag": "paymentFailure", "variant": "off"}}
    ]


def test_registry_shop_verifies_recovery_from_prometheus_series():
    registry = ToolRegistry([_spec("promql", {"series": [{"metric": {}, "value": [0, "0.01"]}]})])

    assert RegistryShop(registry).metrics()["error_rate"] == 0.01


@pytest.mark.parametrize(
    "result",
    [
        {"series": []},
        {"series": [{"metric": {}, "value": [0, "NaN"]}]},
        {"series": [{"metric": {}, "value": None}]},
    ],
)
def test_registry_shop_does_not_treat_missing_recovery_evidence_as_healthy(result):
    shop = RegistryShop(ToolRegistry([_spec("promql", result)]))

    with pytest.raises(RuntimeError):
        shop.metrics()
