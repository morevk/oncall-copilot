# Threat model

**Date:** 2026-11-10 · **Owner:** P1, covering P3 (tools, human control & guardrails)
**Status:** current as of `main`. Every claim below was read off the tree, with file and line.

The project guide gives this domain two rules, and everything here serves them:

> *Treat outside text as data, never as instructions.*
> *A person approves each exact change to the outside world.*

## 1. What is being protected

| Asset | Why it matters |
|---|---|
| `deploy/flagd/demo.flagd.json` (live document) | The only thing the agent can change in the shop. Arming a fault here breaks the demo for everyone. |
| The committed flagd baseline | Validated against the corpus; a fault left armed here breaks every fresh clone (ADR-0004 §4). |
| The playbook (`data/postmortems/`) | A lesson that reaches the model is guidance. An approved-but-wrong lesson misleads every future incident. |
| The approval decision | The gate is the whole safety story. A write that happens without one, or with a different payload than was shown, voids it. |
| Secrets in telemetry | Logs and span attributes may carry credentials or PII. Nothing in this tree redacts them yet (F4). |
| The incident record | Evidence. If the audited thing can rewrite it, it is not evidence (ADR-0009). |

## 2. Where untrusted text enters

Text the system did not write, which an attacker can influence:

| Entry point | Reaches the model via | Status |
|---|---|---|
| Log lines (`User-Agent`, request fields) | `log_search` in `mcp-telemetry` | **The injected "restart everything" line is not yet seeded** in the `otel-logs-*` index (F5) |
| Span attributes and error text | `traces` in `mcp-telemetry` | `traces` reports *which* spans errored and not what they said (CONTRIBUTING §"`traces` reports which spans errored") |
| Alert annotations | the incident payload itself | Trusted today; Grafana is ours, but the summary text is prose the model reads |
| Runbook bodies | retrieval → `runbook_guidance` | A poisoned step is a named attack in the guide; the corpus is ours and reviewed, so this is an insider/supply-chain path |
| Lesson statements | `partition_lessons` → the runbook brief | Only `approved` passes, and preconditions must match (`handoffs.py`) |
| **MCP tool descriptions** | `load_tools()` → `bind_tools` → the model | `SERVERS` is a fixed mapping of three servers (`registry.py:81`). A third-party server cannot be added without a code change — a structural defence against the guide's third named attack |

## 3. Defence inventory

The column that matters is **in the run path**: a defence that nothing calls scores the same in both arms of any experiment.

| # | Defence | Where | In the run path? | Off position | The refusal |
|---|---|---|---|---|---|
| D1 | `ACTION_TOOLS = ("set_flag",)` | `planning/plan.py:32` (P2) | yes | **none** — reversing ADR-0007 | `has no tool called restart` |
| D2 | Human approval bound to the payload | `nodes/remediate.py:124` (P2) | yes | parameter (auto-approve) | interrupt; edit invalidates the hash |
| D3 | Corpus-derived (flag, variant) allowlist | `servers/ops_actions/__init__.py:136-144` | yes, on **both** write paths | parameter | `no runbook names 'X' as a fix for flag 'Y'` |
| D4 | Per-specialist tool selection by name | `tools_from_registry(...)`, `topologies/c2_team.py:237` (P2) | yes | parameter | the model is never shown the tool |
| D5 | `Shop.apply` rejects unknown tools | `astronomy_shop.py:97` | yes | parameter | `unknown tool: 'restart'` |
| D6 | Span text kept off the wire | `mcp-telemetry` | yes | parameter | nothing to inject with |
| D7 | Lesson status + precondition filter | `handoffs.partition_lessons` | yes | parameter | `dropped lesson X: precondition service A != B` |
| D8 | Fixed server registry (no third-party servers) | `registry.py:81` | yes | **none** — code change | the server does not exist |
| D9 | `permissions.for_role` / `assert_read_only` | `permissions.py` | **NO — see F1** | n/a | — |
| D10 | `hooks.pre_tool` approval enforcement | `hooks/pre_tool.py` | **NO — see F1** | n/a | — |
| D11 | Secret redaction before the model | — | **does not exist (F4)** | already off | — |

## 4. Findings

**F1 — two implemented defences are not in the run path.** Every reference to `permissions.for_role` and `hooks.pre_tool` outside `oncall-tools`' own docstrings is in `oncall-tools`' own tests (22 of them, in `test_pre_tool.py` and `test_permissions.py`). `oncall-graph` imports `ToolRegistry`, `load_tools`, `TRANSPORT_ENV` and `ToolCallError`, and never imports `permissions` or `hooks`. Nothing anywhere calls `.reads()`, `.writes()` or `.select()` outside the package, and `build.py:39` returns the full `load_tools()` registry — write tools included.

What protects a diagnosis specialist today is **D4**, selection by explicit name in P2's topology, not D9. That works, and it has a different failure mode: **tool annotations are not load-bearing in the run path.** `_is_write` fails closed (`registry.py:137`) and nothing consumes the result during a run, so a write tool whose name were added to a specialist list would be handed to the model regardless of its annotation. The second layer ADR-0003 and ADR-0004 describe is tested, correct, and inert.

**F2 — there are two write paths, and only one goes through the registry.** `apply_through` (`nodes/remediate.py:169`) calls `tools[tool].invoke(...)` when the registry has the tool, and otherwise falls back to `shop.apply` (`:207`), which imports `set_flag`/`rollback_flag` from `oncall_tools.servers.ops_actions` and calls them **in-process** (`astronomy_shop.py:93`). Any defence placed on the registry's `invoke()` chokepoint (`registry.py:395`) covers the first path and not the second. D3 survives on both, because the allowlist lives inside `_apply` rather than in the MCP layer.

**F3 — the approval gate is the graph's, not the tool layer's.** D2 is `interrupt()` in P2's node. The tool layer cannot presently enforce approval because the caller never passes an `Approval` to `invoke()`; wiring D10 therefore requires a change at a call site in `oncall-graph`, not only in `oncall-tools`.

**F4 — no redaction exists anywhere.** The guide requires secrets redacted before any model sees a log. D6 limits exposure by keeping span text off the wire, which is a containment, not a redaction: `log_search` returns log text. This is the one gap that is both a bar item and a plausible live embarrassment during the demo.

**F5 — the injected log line is not seeded.** Demo objective 2 is complete in code — `restart` is not a tool, the planner rejects it, the shop raises — but the attack currently arrives via alert text rather than through `log_search`. One document in the `otel-logs-*` index closes it. P1's.

## 5. Out of scope

Deliberately, with reasons: no shell is ever given to the agent, so command execution is not a path to model (guide, p. 14); container escape and host compromise are the demo stack's problem, not the agent's; CI and dependency supply chain are real but are a different exercise; denial of service against the shop is not interesting here, because the agent's one action is bounded by D3.

## 6. How the red-team suite measures this

Decided with the owner, and recorded so the suite does not re-argue it per attack:

- **Toggleable layers — D2, D3, D4, D6, D7, and D11 once it exists — are measured off and on.** Their off position is a parameter and creating it reverses no decision record. This is where a moved number comes from.
- **Structural layers — D1 and D8 — have no off position.** Turning D1 off means re-adding the tools ADR-0007 removed, which is a decision we are not taking to produce a statistic. These are reported as *"this attack cannot be expressed against this system"*, with the transcript showing the refusal by name. That is a stronger claim than a percentage, and it is the honest one.
- **D9 and D10 cannot be measured in either arm until F1 is fixed.** They are currently indistinguishable from absent.
- **A benign corpus runs alongside the attacks.** Thirty refusals and a false-refusal rate of unknown is not a result; a system that refuses everything scores identically.

## 7. What F1 needs, and from whom

Wiring D9 and D10 is **not** a change inside `oncall-tools` alone, and the reason is worth stating
because it is easy to assume otherwise.

`permissions.for_role(registry, role)` is a pure function over a registry. For it to run, the
caller must ask for a narrowed registry. `build.py:39` is `return load_tools()`, so the one-line
seam is `load_tools(role=...)` — and the parameter has to be passed by the caller, because
narrowing by **default** is actively harmful here: `apply_through` falls back to `shop.apply`
when the registry lacks the tool (F2), so a reads-only default would not block the write, it
would silently route it around MCP and into the in-process path.

`hooks.pre_tool(spec, args, approval, request)` needs an `Approval` whose `fingerprint` matches
the payload. Nothing passes one today: the approval lives in the graph's state as
`approval_payload_hash` (`state.py:56`) and the write is invoked as `invoke(clean)` with no
approval argument (`nodes/remediate.py:185`). So the tool layer cannot check an approval it is
never handed.

Two changes are therefore required, and the second is in a package P1 does not own:

1. **`oncall-tools` (P1 covering P3):** `load_tools(..., role: Role | None = None)` applying
   `for_role` when given; and an approval-carrying parameter on the invoke path so `pre_tool` has
   something to check.
2. **`oncall-graph` (P2):** pass `role=` where the registry is built, and hand the approval to
   the write call in `apply_through`.

**Status: the `oncall-tools` half is built.** `load_tools` now takes `role` and
`require_approval`, both defaulting to off, so the graph's behaviour is unchanged until it opts
in. The approval travels out of band through `hooks.approval_context.approved()`, a ContextVar
rather than a tool argument: the registry hands the model a callable whose keyword arguments are
the tool's own schema, so an `approval=` keyword would collide with any tool that ever has a
field of that name, and the LangChain adapter passes tool arguments as `**kwargs`. Outside a
context there is no approval, so an enforcing registry refuses every write without being told to.

`test_registry_enforcement.py` exercises both defences **through `load_tools`**, the function the
graph actually calls, so a regression there is a regression in the run path rather than in a
helper.

One bug surfaced while wiring it: `for_role` returns a lens, because `ToolRegistry.select()`
passes `close=None`. Returning that lens from `load_tools` would have made the caller's `close()`
a silent no-op and leaked one MCP session per server, since `build.py` closes exactly what
`load_tools` returned. `load_tools` therefore takes `for_role`'s policy and keeps ownership of
the sessions itself.

**What remains is P2's, and it is two lines** in `oncall-graph`:

```python
# build.py:39
return load_tools(role="remediation", require_approval=True)

# nodes/remediate.py, around the invoke in apply_through
with approved(Approval(fingerprint=..., approved_by=...)):
    reported = invoke(clean)
```

`role="remediation"` and not `"diagnosis"`: the registry built there is shared and remediation
needs the write tool. Per-specialist narrowing already happens by name in the topology (D4), and
asking for a diagnosis registry here would trigger F2 — `apply_through` would fall back to the
in-process `set_flag`, and the write would leave MCP entirely.

Until those two lines land, D9 and D10 stay off and their rows above still read **NO**. What has
changed from F1 is that the seam now exists, is tested, and is named.
