# P2 definition of done for the three demo objectives

**Written 2026-11-08, assessed against `main` at `a689680`. Re-checked 2026-11-10 on `team-graph`
at `3b6c796` (local, not yet pushed to the fork), which includes `main` at `e18eb85`.** For P2, who owns `packages/oncall-graph`. This
turns the P2 rows of [`demo-objectives-gap-analysis.md`](demo-objectives-gap-analysis.md) into a
checklist and records how much of it the code already meets. That analysis was read against
`600479e`; everything below was re-checked against the current tree, and the tests named here were
run.

"Done" means the code exists and a test covers it. PR #37 is on `main` (merge `74cfb43`). The
commits after `1e8e7a5` are on `team-graph` and are not yet on `main`. On 2026-11-10, after
`oncall-service` was rebuilt from `837ebc7`, two approved smokes ran against `c2_team` with
`paymentFailure` armed. The charge alert (`smoke-payment-charge-b8827c94`) and the checkout alert
(`smoke-checkout-cc5b51d6`) finished `RECOVERED` on `demo_payment_transactions_total`, in 55s and
49s. Both run records list `executed_actions` as `set_flag` `paymentFailure` to `off`, with
`verified_recovery` true and `rolled_back` false. Both returned on the first successes, so they
did not wait out the five-minute window and did not exercise rollback. Earlier the same day,
`smoke-payment-charge-fd68cc2e` (97s) and `smoke-checkout-6794cd7b` (57s) also recovered, before
the run record read `executed_writes`. After the Charge-pair verify was added, with
`paymentUnreachable` armed and `paymentFailure` off, `smoke-payment-unavailable-70fc21ce` was
approved and finished `RECOVERED`
on the payment server Charge rate in 125s. Its run record lists `set_flag` `paymentUnreachable`
to `off`, with `verified_recovery` true and `rolled_back` false. It returned on the first server
Charge sample above zero, so it did not wait out the window. Earlier,
`smoke-payment-unavailable-ca45b6a2` had stopped at `AWAITING_APPROVAL`, before this verify
existed.

The catalog flag (`productCatalogFailure`) is not one of the two payment flags, so its live verify
is scripted and reads no recovery signal. Three runs on 2026-11-10 covered that path.
`probe-catalog-d8102c85`, run before the smoke test file existed, approved `set_flag` `off`, got
`verify recovered=False`, rolled back to `on`, replanned, and stopped at a second
`AWAITING_APPROVAL` that was not approved. `smoke-product-catalog-40a6bac4` did the same and stopped
at the second approval after 28 of 40 tools. `smoke-product-catalog-d8b0621a` rolled back and
replanned, then triage hit `budget exhausted` at 37 tools, and the run ended `ESCALATED`. Its
`system_run` shows `rolled_back` true, `verified_recovery` false, and `executed_actions` `set_flag`
`off` then `rollback_flag` `on`. In each run the flag was armed to `on` first and set back to `off`
afterward. A flag already at `off` now skips `rollback_flag` and still takes the replan edge
(`cbaf4e5`, `test_a_scripted_miss_on_another_flag_replans_without_restoring_off`). The live smoke is
`tests/test_product_catalog_failure_alert_live_smoke.py`, renamed from
`packages/oncall-service/tests/test_product_catalog_failure_live_smoke.py` (`3b6c796`). It arms and
disarms the flag itself, and accepts either the second approval or budget escalation. No catalog
run recovered, so the score does not change.

**Score: 26 of 28 done.** Objective 1 is what remains. Objective 2 is done in code
([ADR-0007](decisions/0007-no-service-actions.md)); the log line itself is still P1's to put
where `log_search` reads it. Objective 3 is shown on `c2_team`: the live team is given the
playbook, a dropped lesson names the precondition that failed, and a lesson with no status is
dropped.

1.10 is settled: poll until the five-minute window has elapsed. One success is recovered
immediately. For `paymentFailure`, still zero with checkout 422s still arriving rolls back, and a
quiet shop stays applied. For `paymentUnreachable`, a payment server Charge rate above zero is
the success. On the last poll, checkout Charge client errors still above zero while that server
rate is still zero rolls back. Both rates at zero stays applied.

---

## Objective 1 — arm the fault, diagnose, propose, approve, verify recovery

The project file asks the demo to turn on `paymentFailure`, show the agent finding payment errors
in the traces and proposing to switch the flag off, then show the engineer's approval and a
verified recovery.

**11 of 13 done.** A live `paymentFailure` verify polls the manifest recovery query. What remains
is 1.8, because the service's `Shop.armed()` still reads a mirror, and 1.11, because the scripted
runbook path still uses the fixture. Neither is counted below.

| # | Done when | Where it stands |
| --- | --- | --- |
| 1.1 | `promql`, `log_search` and `traces` read the running shop. | **Done.** `LiveShop` overrides those three (`astronomy_shop.py`). |
| 1.2 | The diagnosis tells `paymentFailure` from `paymentUnreachable`, and does not read the loudest error group as the cause. Jaeger's most frequent failing operation is routine flag evaluation; the payment rows sort last. | **Done in `c2_team`.** `evidence.py` (commit `d9e3925`). `c1_single` does not use it. |
| 1.3 | The model proposes exactly one `set_flag` with `{flag, variant}`, and `validate_action` accepts it. | **Done.** |
| 1.4 | Approval binds to the payload hash. An edited payload or a stale hash interrupts again and writes nothing. | **Done.** `nodes/remediate.py`. |
| 1.5 | `agent._as_langchain_tool` grows its third branch, so an MCP tool reaches a specialist. | **Done.** The third branch passes the server's JSON Schema as `args_schema` and uses `invoke` as the function (`agent.py`). `test_tool_adapter.py`. Commit `af1177a`. |
| 1.6 | `build_graph` stops refusing a tool registry, and the service passes a registry instead of `LiveShop()`. | **Done.** `build_graph` accepts a `ToolRegistry` and reads from it (`build.py`). With a registry (`MCP_TRANSPORT` set), `_open_runner` still builds a plain `Shop()` and passes the registry alongside it. Without one it builds `LiveShop()` (`main.py`). Commits `0e61290`, `1e8e7a5`. `test_registry_wiring.py`, `test_service.py`. |
| 1.7 | `apply` writes the flagd document through `mcp-ops-actions`, which already takes `{flag, variant}` and returns `previous`. | **Done.** `LiveShop.apply` calls `set_flag` and `rollback_flag` from `ops_actions` (`astronomy_shop.py`). On the service path, `execute` and `rollback` call the same two tools on the registry (`nodes/remediate.py`, `apply_through`). The scripted `Shop.apply` remains for graphs built without a registry. Approved writes are recorded on `IncidentChannel.executed_writes` (`837ebc7`), and the run record reads that list. The registry path no longer appends to `shop.writes`. The charge, checkout, and unavailable smokes after the rebuild list that write in `executed_actions`. |
| 1.8 | `armed()` and `metrics()` read that document. A write that never lands must not read back as recovery. | **Partly done.** `LiveShop.armed()` reads the document. The service's shop is still a plain `Shop()`, and its `armed()` reads a mirror. The write audit no longer uses that shop (1.7). A live `paymentFailure` verify no longer uses that mirror: it uses the recovery query in 1.9, so a write that never lands leaves the rate at zero and is not `RECOVERED`. A live `paymentUnreachable` verify uses the Charge pair in 1.9. Flags other than these two still use the scripted rate, and a miss on them now rolls back and
replans (`cbaf4e5`). The catalog runs are listed under the score. Not counted in the score. |
| 1.9 | `verify_metrics` runs the fault's recovery query instead of `shop.metrics()`. | **Done** for `paymentFailure`. The query is `sum(rate(demo_payment_transactions_total[5m])) > 0`, from `eval/faults/manifest.yaml`, read through the registry's `promql` (`nodes/remediate.py`). A graph with no `promql` still uses `shop.metrics()`. `test_a_live_payment_fix_recovers_when_transactions_resume`. The charge and checkout smokes on 2026-11-10 finished `RECOVERED` on this query. `paymentUnreachable` polls `SERVER_CHARGE` and, on the last poll, `CLIENT_CHARGE`, both from `evidence.py`. A server Charge rate above zero is `RECOVERED` (`test_an_unreachable_fix_recovers_when_the_server_takes_charges`). `smoke-payment-unavailable-70fc21ce` finished `RECOVERED` on that signal. A graph with no `promql` still uses `shop.metrics()` for this flag too. |
| 1.10 | **Decision.** A 5-minute rate cannot confirm recovery instantly. Verification either polls, or the run reports "applied, not yet verified". | **Done.** Polls ten seconds apart until the window has elapsed (31 polls). One success returns `RECOVERED` immediately. On the last poll, checkout 422s still arriving is a failed fix and takes the rollback edge (`test_a_live_payment_fix_rolls_back_when_checkout_failures_continue`). A zero success rate with a quiet checkout is `APPLIED`, and the flag stays (`test_a_live_payment_fix_waits_out_and_does_not_roll_back`). The two live `paymentFailure` smokes and `smoke-payment-unavailable-70fc21ce` recovered before the window elapsed. Rollback and `APPLIED` are covered by the unit tests, not by the live runs. |
| 1.11 | `search_runbooks` reads `mcp-runbooks`. The three fixture passages name `paymentFailure`, so a diagnosis that cites them cannot be wrong. | **Partly done.** On the service path, `search_runbooks` hits from `mcp-runbooks` become passages, and a missing fix is loaded with `get_runbook` (`handoffs.py`, `052a341`). The checkout smoke reached `RECOVERED` through that path. The scripted path still uses the stub fixture (`stubs.py`), and no test pins the citation. Not counted in the score. |
| 1.12 | `test_live_shop_reads_telemetry_and_keeps_stub_writes` is retired or rewritten. It asserts the in-memory write this objective removes. | **Done.** Rewritten as `test_live_shop_reads_telemetry_and_writes_the_document` in `packages/oncall-graph/tests/test_astronomy_shop.py`. |
| 1.13 | `tests/test_apply_reaches_the_real_shop.py` passes. It pins 1.7, 1.8 and the refusals the seam buys: an invented flag, a re-arm through `set_flag`, and `restart`. | **Done, 7 of 7, no skips.** The file exercises `LiveShop`. It does not exercise the service's `Shop()` path, which is the gap in 1.8. |

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

**8 of 8 done.** The demo is `c2_team`. The service builds that team with
`LessonStore.from_postmortems()`, and the runbook step drops a lesson whose preconditions do not
fit, with the reason on the transcript.

| # | Done when | Where it stands |
| --- | --- | --- |
| 3.1 | `precondition_matches`, `partition_lessons` and `load_lessons` exist and drop a lesson whose preconditions do not fit the case. | **Done.** `briefs.py`, `handoffs.py`. |
| 3.2 | The same filter runs against P1's `LessonStore` and keeps `les-read-the-flag-baseline-first` while dropping the two stale lessons. | **Done.** `tests/test_demo_scene_3.py` passes. |
| 3.3 | The topology that filters is reachable over HTTP. | **Done.** `RUNNABLE` is `c1_single` and `c2_team` (`main.py`). At `600479e` the service refused everything but `c1_single`. |
| 3.4 | **Decision.** Demo the rejection on `c2_team`, or teach `c1_single` to filter. P1 prefers `c1_single`, because the objective is about rejecting bad memory and the simpler topology removes a variable. | **Done.** `c2_team`. `c1_single` does not filter lessons. |
| 3.5 | The live run is given the store: `Team(shop=shop, store=LessonStore.from_postmortems(), …)`. | **Done.** `_open_runner` in `main.py`. The run record's `playbook_hash` is that store's hash. |
| 3.6 | A dropped lesson shows the precondition that disqualified it, not only its id. The objective asks to show the reasoning. | **Done.** `lesson_drop_line` in `handoffs.py`, copied onto the runbook transcript. |
| 3.7 | A lesson with no status is dropped. | **Done.** `partition_lessons` keeps only an explicit `approved`. `test_a_lesson_with_no_status_is_dropped`. |
| 3.8 | A graph-level test builds `Team` against the real `LessonStore` and checks which lessons were dropped and why. | **Done.** `test_the_team_runbook_drops_the_stale_lessons_and_says_why`. |

P1 owns the store and the seed postmortems, and both landed in commit `7884566`.

---

## Order

1. 2.1 and 3.4 are settled. Objective 2 and objective 3 are done in code. P1 can author the
   injected log line now.
2. 1.5 to 1.7, 1.9, 1.10, 1.12 and 1.13 are done on `team-graph`. On 2026-11-10, after the
   service rebuild, the charge smoke (`smoke-payment-charge-b8827c94`, 55s) and the checkout
   smoke (`smoke-checkout-cc5b51d6`, 49s) approved `set_flag` and finished `RECOVERED` before
   the five-minute window. Both run records carry that write in `executed_actions`.
   A live failure still rolls back only when checkout 422s are arriving after that window, and a
   quiet shop stays applied. The unavailable smoke (`smoke-payment-unavailable-70fc21ce`, 125s)
   approved `set_flag` and finished `RECOVERED` on the payment server Charge rate.
3. What remains in Objective 1 is partial: 1.8 (the service shop's `armed()` still reads a
   mirror; flags other than `paymentFailure` and `paymentUnreachable` still verify on the
   scripted rate; a miss on them now rolls back and replans) and 1.11 (the scripted runbook path
   still uses the fixture).

`_open_runner` in `packages/oncall-service/src/oncall_service/main.py` now builds the team with
the playbook. That file is in P5's package. The gap analysis assigns the change to P2.

## How this was checked

```bash
git rev-parse --short HEAD

# the live team is given the playbook
sed -n '/def _open_runner/,+20p' packages/oncall-service/src/oncall_service/main.py

uv run pytest tests/test_demo_scene_3.py \
              packages/oncall-graph/tests/test_evidence.py \
              packages/oncall-graph/tests/test_team.py \
              packages/oncall-service/tests/test_service.py -q
```

The rollback skip and the scripted-miss replan tests:

```bash
uv run pytest packages/oncall-graph/tests/test_solo.py packages/oncall-graph/tests/test_team.py \
  packages/oncall-graph/tests/test_state_plan.py -q
```
