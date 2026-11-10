# ADR-0009: the incident record is a write, and `mcp-ops-actions` is still the only server that writes to the shop

**Status:** Accepted
**Date:** 2026-11-10
**Deciders:** P1 (domain, knowledge & memory), proposing the contract. **P3 (tools & guardrails) owns the server and the registry entry** — the same split as [ADR-0004](0004-the-only-write-server.md), which P1 wrote while covering for P3, and as the `mcp-runbooks` contract in CONTRIBUTING.md.
**Amends the scope of:** [ADR-0004](0004-the-only-write-server.md), whose title says "the only write server".

## Context

The project guide names four MCP servers for this domain. Three exist — `telemetry` (8101),
`runbooks` (8102), `ops_actions` (8104). The fourth, `tickets` (8103), is unwritten, and
`registry.py:61` says so in a comment that also states the rule it is waiting on: "ONLY SERVERS
THAT EXIST ARE LISTED… Adding a server is one entry here, in the commit that adds the server."

The brief describes it as **incident record, status draft**. Both of those persist something, and
that collides with ADR-0004's title.

The collision is not cosmetic. `ToolSpec.write` is a boolean and `permissions.for_role` returns
`registry.reads()` for the diagnosis role, so the codebase recognises exactly two classes of
tool: a read, which any role may hold, and a write, which only `remediation` may hold and which
`hooks.pre_tool` refuses unless an `Approval` whose `fingerprint` matches the exact payload is
presented. There is no third class, and inventing one would be a change to P3's `permissions.py`
landed by the package that wants it — the thing the `mcp-runbooks` contract explicitly refused
to do.

So a tool that appends to an incident record is a write, or it is annotated read while mutating
durable state. The second is not available: `assert_read_only` would pass on it, and the whole
point of the two layers is that the annotation is true.

## Decision

**`mcp-tickets` serves three read tools and exactly one write tool.**

The write is `record_incident`, called **once, at the end of an incident, by the remediation
role, through the existing approval gate**. It is not a new gate and not a new mechanism: the
same `pre_tool` check, the same payload fingerprint, the same refusal types.

**ADR-0004's claim is narrowed, not withdrawn.** The sentence that survives is: *`ops_actions`
is the only server that writes to the shop.* Everything ADR-0004 actually reasons about — the
flag allowlist built from the corpus, the two flagd documents, `emitRawPii` having no route, the
disjointness of fix and rollback pairs — is about writing to the running Astronomy Shop, and
none of it is weakened by a server that writes a record of what happened into our own journal.
The title is now too strong on its own and should be read with this record.

**There is no publish or send tool.** Deliberately. Publishing a status update is a change to
the outside world in the guide's sense, and it would need its own payload schema, its own
approval, and an argument about which external system. The brief says *status draft*, and a draft
is where this stops. `draft_status_update` returns text and transmits nothing.

**The draft is assembled from structured fields only — never from raw log or span text.** This is
the constraint that matters most and it is a security property, not a formatting preference. The
guide requires secrets redacted before any model sees a log, and we have no redaction code
anywhere in the tree. A status draft that quotes a log line or a span attribute would carry
whatever was in it straight into human-facing prose and, through the transcript, back into the
model's context. ADR-0004 observed that `emitRawPii` has no route to the agent because no runbook
names it; this rule is the same instinct applied to the one tool whose output is meant for
people.

**The incident identity is minted by the service, not by a tool call.** There is no
`open_incident`. `oncall_service.main` already sets
`incident_id = f"alert-{uuid.uuid4().hex[:12]}"` (`main.py:584`) before any tool runs, so the
server holds no state machine and cannot be left with a half-open incident when a run fails.

Note what that id is and is not: it is a **random uuid per run**, not a fingerprint of the alert.
Two runs against the same firing alert therefore get two incident ids and would produce two
records. For an append-only journal that is the safe failure — two honest records rather than one
silently overwritten — but it means the journal keys on *run*, not on *alert*, and a reviewer
counting tickets will count retries. Deriving the id from the alert's labels and `startsAt`
instead is a change in `oncall-service`, which is not P1's or P3's file; it is recorded as an ask
in CONTRIBUTING.md rather than assumed here.

**The record is append-only.** Recording an incident that already has a record raises rather than
overwriting. An audit trail that can be rewritten by the thing it audits is not one.

Rejected:

- **No write tool at all — have the service write the record from `RunRecord`.** Tempting, and it
  would have kept ADR-0004's title intact. Refused because the only component that persists
  `RunRecord`s today is `oncall-eval`'s run database, which is P4's and sits behind the oracle
  boundary; a path from the agent's incident record into the evaluator's store is exactly the
  cheating route `oncall-memory.reflexion` was built to prevent. A second writer in
  `oncall-service` would be a cross-owner ask for a component neither P2 nor P5 asked for.
- **A third tool class, between read and write.** One line in P3's `permissions.py` and a new
  concept for every reader of the registry, to avoid one approval on one call per incident.
- **`record_incident` as a read, because it "only writes our own file".** The annotation would be
  false, and `assert_read_only` is the test that would then be passing for the wrong reason.

## Consequences

- `permissions.py`, `registry.ToolSpec` and `hooks.pre_tool` need **no change**. `mcp-tickets`
  uses the two-class model as it stands.
- The demo gains a **second approval of a different shape**: one for `set_flag` against the shop,
  one for `record_incident` against the journal. The gate is demonstrated twice on two kinds of
  payload, which is stronger evidence than one.
- `record_incident` carries the approval fingerprint of the fix it documents, so a ticket cannot
  claim an action nobody approved. The ticket and the gate are tied by construction.
- The `registry.py:61` placeholder comment and the `SERVERS` entry for port 8103 are **P3's to
  land in the implementing commit**, per the rule that comment itself states. This record does
  not add them.
- `artifacts/` is gitignored except `!artifacts/retrieval/`, so incident records will not travel
  with the repo. That is correct for run output and wrong for the review, which needs one example
  to look at. Whether to add `!artifacts/incidents/` or commit a single worked record under
  `docs/` is left to P3 and P5.
- ADR-0004 keeps its title. Anyone citing "the only write server" should cite this record too.
