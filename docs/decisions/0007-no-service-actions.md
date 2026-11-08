# ADR-0007: `restart`, `scale`, and `rollback` are not actions

**Status:** Accepted
**Date:** 2026-11-08
**Deciders:** P2 (orchestration, planning & improvement)
**Builds on:** [ADR-0005](0005-one-set_flag-payload.md), which left this item open.

## Context

`ACTION_TOOLS` was `set_flag`, `restart`, `scale`, and `rollback`. The last three had argument
schemas in the planner and a `Shop.apply` that reported success, and nothing else. No entry in
`ACTION_SCHEMAS`, no MCP server, and no runbook fix names them. Every executable fix in the
corpus is `set_flag`. Several runbooks say a restart while the fault is active makes the shop
worse.

Demo objective 2 is a log line telling the agent to restart everything, and the agent refusing
it. With those three names in `ACTION_TOOLS`, `validate_action` accepted `restart` and the
proposal reached the approval gate. The only thing that blocked it was prompt text.

`rollback_flag` was already excluded from `ACTION_TOOLS` for the same shape of reason: a model
must not be able to propose it, so `validate_action` rejects it as an unknown action and only
`code_rollback` names it. That tool restores a flag. It is not a service rollback.

## Decision

`restart`, `scale`, and a service `rollback` leave `ACTION_TOOLS`. They do not gain a schema or
a server.

`ACTION_TOOLS` is `set_flag`. `proposal_tools()` offers that one tool. The solo and remediation
prompts name that one tool. `Shop.apply` accepts `WRITE_TOOLS`, which is `set_flag` and
`rollback_flag`, and raises `unknown tool` for anything else. `code_rollback` no longer builds a
service rollback.

A model that emits a `restart` tool call has no such tool. The loop returns `has no tool called
restart`, that line is copied onto the transcript, no proposal is recorded, and the approval
gate does not open. An edit that renames an approved command to `restart` interrupts again with
the original command and `error: unknown action: 'restart'`. The hash does not change and
nothing is written.

Rejected: building a server for the three. An approved `restart` would then really restart
something, and the log line would no longer be blocked unless a second rule refused the action
just made legal. No runbook wants that effect.

## Consequences

- Tests that used `restart` as a stand-in for "an approved action that leaves the fault armed"
  now use `set_flag` with variant `50%`. The flag stays armed, verification fails, and
  `rollback_flag` restores the `previous` variant. That is the real undo path.
- The injected log line in OpenSearch is still P1's to author. The graph test uses the same
  instruction in the alert text.
- `test_the_planner_and_the_write_server_agree_on_every_payload` compares the actions the
  planner declares. It now has only `set_flag` to compare, which the write server implements.
