"""Compile one of the three E1 configurations.

``c1_single`` is the solo baseline. ``c2_team`` is the commander and the
specialists. ``c0_workflow`` is still later, and stays unimplemented beside
these two rather than by editing either of them.
"""

import os
from typing import Any

from langgraph.graph.state import CompiledStateGraph
from oncall_contracts.config import Topology
from oncall_tools.registry import TRANSPORT_ENV, ToolRegistry, load_tools

from .config import DEFAULT_MAX_REVISIONS, DEFAULT_TOOL_BUDGET
from .stubs import Shop
from .topologies.c1_single import Solo
from .topologies.c2_team import Team


def tools_requested() -> bool:
    """Whether this process was told to open the MCP registry.

    Unset means do not connect. The image check starts oncall-service with
    only ``ONCALL_SERVICE_HOST``, and the default transport is stdio. The
    service image cannot host the runbook server: it has neither the corpus
    nor the embedding model, so connecting there exits before ``/healthz``.
    Compose sets ``MCP_TRANSPORT=http``, which is the signal to connect.
    """
    return bool(os.environ.get(TRANSPORT_ENV, "").strip())


def open_tools():
    """Open the MCP sessions. The service may not import ``oncall_tools``.

    The caller closes the registry. One registry is shared across incidents.
    Opening one per alert would reload the runbook model every time.
    """
    return load_tools()


def build_graph(
    tools: dict[str, Any] | Shop | ToolRegistry | None = None,
    store: Any = None,
    *,
    topology: Topology = "c2_team",
    model: Any = None,
    tool_budget: int = DEFAULT_TOOL_BUDGET,
    max_revisions: int = DEFAULT_MAX_REVISIONS,
    checkpointer: Any = None,
    parallel_diagnosis: bool = True,
) -> CompiledStateGraph:
    """Compile ``topology``.

    ``tools`` is the injected tool bundle. ``None`` or ``{}`` selects the
    scripted shop. A ``ToolRegistry`` supplies the reads it actually has;
    apply and verify still use a scripted shop. Any other value is refused.
    ``store`` is the lesson store the runbook step filters.
    ``c1_single`` does not read it. ``model`` is a ready-made chat model.
    Omit it and an agent builds its OpenRouter client, which needs
    ``OPENROUTER_API_KEY``. ``c1_single`` builds that client while compiling.
    ``c2_team`` builds it on the first specialist call. ``parallel_diagnosis``
    is the fan-out switch: false gives one diagnosis agent the whole suspect
    list and the whole tool budget.
    """
    if topology in ("c1_single", "c2_team"):
        registry = None
        if isinstance(tools, ToolRegistry):
            # Reads come from the registry. The shop remains for apply,
            # verify, and rollback until those call the write server.
            shop = Shop()
            registry = tools
        elif isinstance(tools, Shop):
            shop = tools
        elif tools in (None, {}):
            shop = Shop()
        else:
            raise TypeError(
                f"tools must be a ToolRegistry, a Shop, or empty. Got {type(tools).__name__}."
            )
        if topology == "c1_single":
            solo = Solo(
                model=model,
                shop=shop,
                tools=registry,
                tool_budget=tool_budget,
                max_revisions=max_revisions,
                checkpointer=checkpointer,
            )
            return solo.graph
        team = Team(
            model=model,
            shop=shop,
            tools=registry,
            store=store,
            tool_budget=tool_budget,
            max_revisions=max_revisions,
            checkpointer=checkpointer,
            parallel_diagnosis=parallel_diagnosis,
        )
        return team.graph
    if topology == "c0_workflow":
        raise NotImplementedError("c0_workflow is not implemented.")
    raise ValueError(f"unknown topology: {topology!r}")
