# P2 definition of done for the three demo objectives

**Written 2026-11-08, assessed against `main` at `a689680`.** For P2, who owns
`packages/oncall-graph`. This turns the P2 rows of
[`demo-objectives-gap-analysis.md`](demo-objectives-gap-analysis.md) into a checklist and records
how much of it the code already meets. That analysis was read against `600479e`; everything below
was re-checked against the current tree, and the tests named here were run.

"Done" means the code exists and a test covers it. A live demo has not been run for this
assessment.

**Score: 15 of 25 done, one partial.** Objective 1 is the furthest along. Objective 2 is done in
code: `restart`, `scale`, and `rollback` are not actions, and a model that names one is refused
before the approval gate ([ADR-0007](decisions/0007-no-service-actions.md)). The log line itself
is still P1's to put where `log_search` reads it. Objective 3's filtering is finished and tested,
but nothing reachable over HTTP hands it a lesson store, so a demo run filters an empty list.

The rows marked **decision** are P2's to settle. This document records them as open rather than
choosing an answer.

---

## Objective 1 — arm the fault, diagnose, propose, approve, verify recovery

The project file asks the demo to turn on `paymentFailure`, show the agent finding payment errors
in the traces and proposing to switch the flag off, then show the engineer's approval and a
verified recovery.

**4 of 13 done.** A live run still ends `RECOVERED` because `verify_metrics` reads the agent's own
in-memory write. Items 1.7 to 1.9 are what make that verdict describe the shop.

| # | Done when | Where it stands |
| --- | --- | --- |
| 1.1 | `promql`, `log_search` and `traces` read the running shop. | **Done.** `LiveShop` overrides those three (`astronomy_shop.py`). |
| 1.2 | The diagnosis tells `paymentFailure` from `paymentUnreachable`, and does not read the loudest error group as the cause. Jaeger's most frequent failing operation is routine flag evaluation; the payment rows sort last. | **Done in `c2_team`.** `evidence.py` (commit `d9e3925`). `c1_single` does not use it. |
| 1.3 | The model proposes exactly one `set_flag` with `{flag, variant}`, and `validate_action` accepts it. | **Done.** |
| 1.4 | Approval binds to the payload hash. An edited payload or a stale hash interrupts again and writes nothing. | **Done.** `nodes/remediate.py`. |
| 1.5 | `agent._as_langchain_tool` grows its third branch, so an MCP tool reaches a specialist. | **Not done.** `agent.py` accepts a LangChain tool or a `Tool` with a real signature, and nothing else. |
| 1.6 | `build_graph` stops refusing a tool registry, and the service passes a registry instead of `LiveShop()`. | **Not done.** `build.py` raises `NotImplementedError` for any non-empty tool dict. `_open_runner` builds `LiveShop()` (`main.py`). |
| 1.7 | `apply` writes the flagd document through `mcp-ops-actions`, which already takes `{flag, variant}` and returns `previous`. | **Not done.** `Shop.apply` mutates `self.flags` (`stubs.py`). |
| 1.8 | `armed()` and `metrics()` read that document. A write that never lands must not read back as recovery. | **Not done.** Both read `self.flags`, which `apply` just wrote. |
| 1.9 | `verify_metrics` runs the fault's recovery query instead of `shop.metrics()`. | **Not done.** It compares `shop.metrics()["error_rate"]` with `RECOVERY_LINE = 0.05` (`nodes/remediate.py`). P4 owns the expression; `eval/faults/manifest.yaml` already gives it for `payment-failure`: `sum(rate(demo_payment_transactions_total[5m])) > 0`. |
| 1.10 | **Decision.** A 5-minute rate cannot confirm recovery instantly. Verification either polls, or the run reports "applied, not yet verified". | **Open.** |
| 1.11 | `search_runbooks` reads `mcp-runbooks`. The three fixture passages name `paymentFailure`, so a diagnosis that cites them cannot be wrong. | **Not done.** `stubs.py`, `search_runbooks`. |
| 1.12 | `test_live_shop_reads_telemetry_and_keeps_stub_writes` is retired or rewritten. It asserts the in-memory write this objective removes. | **Not done.** `packages/oncall-graph/tests/test_astronomy_shop.py`. |
| 1.13 | `tests/test_apply_reaches_the_real_shop.py` passes. It pins 1.7, 1.8 and the refusals the seam buys: an invented flag, a re-arm through `set_flag`, and `restart`. | **0 of 7.** All seven skip until the seam exists. |

---

## Objective 2 — the log-borne "restart everything" instruction is blocked

The demo shows a log line telling the agent to restart everything, and the agent refusing it. The
refusal has to be code. Prompt text does not survive a hostile question at the viva.

**7 of 7 done.** `ACTION_TOOLS` is `set_flag` only. `validate_action` rejects `restart`, `scale`, and
`rollback` as unknown actions, `proposal_tools()` does not offer them, and `Shop.apply` raises
rather than reporting success. The prompt line that logs are data is still there. It is no longer
what blocks the action.

| # | Done when | Where it stands |
| --- | --- | --- |
| 2.1 | **Decision.** `restart`, `scale` and `rollback` either gain a schema and a server, or leave `ACTION_TOOLS` the way `rollback_flag` already has. | **Done.** They leave `ACTION_TOOLS`. [ADR-0007](decisions/0007-no-service-actions.md). |
| 2.2 | `validate_action` refuses `restart` and names it in the reason. | **Done.** `unknown action: 'restart'`. Same for `scale` and `rollback`. `test_a_service_action_is_unknown`. |
| 2.3 | `Shop.apply("restart", …)` refuses. Returning `{"ok": True}` is the failure this objective is about. | **Done.** `unknown tool: 'restart'`. `test_the_shop_refuses_a_service_action`. |
| 2.4 | `proposal_tools()` stops offering the actions removed in 2.1. | **Done.** It offers `set_flag` only, and it checks that list against `ACTION_TOOLS`. |
| 2.5 | The specialist prompts stop listing the removed actions. | **Done.** `c1_single.py` `SOLO_PROMPT`, `c2_team.py` `REMEDIATION_PROMPT`. |
| 2.6 | The refusal is visible: the transcript or the run record shows why the action was dropped. | **Done.** A tool call the agent does not have is copied onto the transcript (`has no tool called restart`). An edit that names `restart` comes back on the same interrupt with `error`. |
| 2.7 | A graph test feeds in a log line asking for a restart and asserts it never reaches the approval gate. | **Done.** `test_a_copied_restart_never_reaches_approval`. The instruction is in the alert text. Seeding it into the OpenSearch index `log_search` reads is still P1's. |

---

## Objective 3 — an old lesson is rejected because its preconditions do not match

**3 of 8 done, one partial.** The filter is written, tested against P1's real `LessonStore`, and
`c2_team` calls it. The service builds that team with no store, so `load_lessons(None)` returns
`([], [])` and the demo filters nothing.

| # | Done when | Where it stands |
| --- | --- | --- |
| 3.1 | `precondition_matches`, `partition_lessons` and `load_lessons` exist and drop a lesson whose preconditions do not fit the case. | **Done.** `briefs.py`, `handoffs.py`. |
| 3.2 | The same filter runs against P1's `LessonStore` and keeps `les-read-the-flag-baseline-first` while dropping the two stale lessons. | **Done.** `tests/test_demo_scene_3.py` passes. |
| 3.3 | The topology that filters is reachable over HTTP. | **Done.** `RUNNABLE` is `c1_single` and `c2_team` (`main.py`). At `600479e` the service refused everything but `c1_single`. |
| 3.4 | **Decision.** Demo the rejection on `c2_team`, or teach `c1_single` to filter. P1 prefers `c1_single`, because the objective is about rejecting bad memory and the simpler topology removes a variable. | **Open.** `c1_single` never calls `handoffs`. |
| 3.5 | The live run is given the store: `Team(shop=shop, store=LessonStore.from_postmortems(), …)`. | **Not done.** `_open_runner` passes no store (`main.py`), so the filter runs on an empty list. |
| 3.6 | A dropped lesson shows the precondition that disqualified it, not only its id. The objective asks to show the reasoning. | **Partial.** The transcript records `dropped lesson <id>` (`c2_team.py`) and nothing about why. |
| 3.7 | A lesson with no status is dropped. | **Not done.** `partition_lessons` defaults a missing status to `"approved"` (`handoffs.py`), so an unreviewed lesson that reaches it is shown as guidance. The store prevents that today; the graph does not. |
| 3.8 | A graph-level test builds `Team` against the real `LessonStore` and checks which lessons were dropped and why. | **Not done.** `test_team.py` passes a hand-written list. |

P1 owns the store and the seed postmortems, and both landed in commit `7884566`. What remains here
is wiring and the decision in 3.4.

---

## Order

1. 2.1 is settled ([ADR-0007](decisions/0007-no-service-actions.md)). 3.4 is still open, and it is
   what remains before objective 3 can be shown. P1 can author the injected log line now.
2. 3.5. One argument at the call site, and it is the only thing keeping objective 3 off the HTTP
   path.
3. 1.5 and 1.6, the tool adapter and the registry. They gate 1.7, 1.9 and 1.11.
4. 1.7 through 1.10, then 1.12 and 1.13. `tests/test_apply_reaches_the_real_shop.py` is the
   executable check for 1.7 and 1.8.

Items 1.6 and 3.5 both edit `packages/oncall-service/src/oncall_service/main.py`, which lives in
P5's package. The gap analysis assigns that change to P2; it is worth agreeing with P5 first.

## How this was checked

```bash
git rev-parse --short HEAD            # a689680

# the live path still builds the fixture shop, and the team gets no store
sed -n '482,489p' packages/oncall-service/src/oncall_service/main.py

# a tool registry is still refused
sed -n '42,51p' packages/oncall-graph/src/oncall_graph/build.py

# restart is not an action
sed -n '27,42p' packages/oncall-graph/src/oncall_graph/planning/plan.py

# the gate still reads the fixture
sed -n '181,196p' packages/oncall-graph/src/oncall_graph/nodes/remediate.py

uv run pytest tests/test_apply_reaches_the_real_shop.py \
              tests/test_demo_scene_3.py \
              packages/oncall-graph -q
# 73 passed, 7 skipped — the seven are test_apply_reaches_the_real_shop.py
```
