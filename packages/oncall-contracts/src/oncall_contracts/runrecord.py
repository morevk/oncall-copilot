"""One record per (case, configuration) run. Written to artifacts/runs/.

This is the only source of numbers for the evaluation report: nobody retypes a
figure into a document. Grouping these by config_id x complexity produces the
E1 table directly.
"""

from typing import NotRequired, TypedDict

from .config import Complexity, Topology


class AgentExecution(TypedDict):
    """Sanitized usage for one named agent; no prompts or hidden reasoning."""

    model_calls: int
    tool_calls: int
    input_tokens: int
    output_tokens: int
    largest_prompt_tokens: int
    latency_ms: int
    model_id: str


class ExecutionTrace(TypedDict):
    """Observable orchestration facts shared by C1 and C2."""

    model_calls: int
    largest_prompt_tokens: int
    agents: dict[str, AgentExecution]
    graph_path: list[str]
    replans: int
    rollbacks: int


class RunRecord(TypedDict):
    run_id: str
    case_id: str
    fault_id: str
    config_id: Topology
    complexity: Complexity

    # Cost side of E1 — what the orchestration charges you.
    tool_calls: int
    tokens: int
    usd: float
    latency_ms: int

    # Outcome side of E1.
    correct_root_cause: bool  # top-1
    root_cause_rank: int | None  # for top-3; None if never proposed
    correct_action_proposed: bool
    # Recovery has three distinct witnesses.  The system's claim is useful
    # evidence, but it is not enough to pass a scored run.  The harness checks
    # the recovery signal while the fault is still under the system's control,
    # then separately proves that its own cleanup restored the baseline.
    system_reported_recovery: bool
    independently_verified_recovery: bool
    cleanup_verified: bool
    verified_recovery: bool  # system claim AND independent observation
    harmful_actions: int  # target is zero
    rolled_back: bool
    within_tool_budget: bool

    # Final deterministic verdict. Persisting this prevents read-only consumers
    # from needing the held-out EvalCase in order to reproduce PASS/FAIL.
    passed: bool
    failure_reasons: list[str]

    # Provenance, so a result can be tied to what produced it.
    model_ids: dict[str, str]
    tool_budget: NotRequired[int]
    execution_trace: NotRequired[ExecutionTrace]
    playbook_hash: NotRequired[str | None]
    error: NotRequired[str | None]
    source_incident_id: NotRequired[str]
