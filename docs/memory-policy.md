# Memory Policy

Owner: P1 — domain, knowledge & memory

Runbooks hold what the team knew before the incident. Memory holds what the copilot observed or
learned during one. This page governs the second.

## The rule everything else serves

**Memory must never silently promote a hypothesis into a fact.**

Every record carries a provenance status. **This is not the corpus's vocabulary** — an earlier
version of this page said it was, and that was wrong. A runbook's `status` is
`verified | structural | hypothesis` (`oncall_knowledge.schema.EVIDENCE_STATUSES`) and answers
*how well is this known?* A memory record's status answers a different question: *has a person
accepted it?* The two sets are disjoint on purpose, which is also why a postmortem is not
validated as a runbook (`oncall_memory.postmortems`).

| `status` | Means | The agent may |
|---|---|---|
| `observed` | read directly from telemetry during this incident | cite as evidence |
| `inferred` | the agent's conclusion, or a candidate lesson no person has reviewed | cite **as a conclusion**, naming the observations; **never apply as a lesson** |
| `approved` | a lesson a human reviewed and accepted | apply, if its preconditions match |
| `rejected` | reviewed and refused, kept with the reason | never apply; useful as a counterexample |
| `superseded` | replaced by a later record, kept for the timeline | never apply |

**Two independent refusals, and this policy needs both.** `oncall_graph.handoffs.partition_lessons`
keeps only an explicit `approved`: a missing status is dropped, and so is `pending`, `rejected`,
`inferred`, or anything unrecognised. `LessonStore.search` serves `approved` only and raises on
any other status, so nothing else leaves the store either. `tests/test_reflexion_to_playbook.py`
proves each half separately, because a guarantee that rests on one of them is one refactor away
from being no guarantee.

> **This was fail-open until #36,** and the history is the argument for pinning a hazard rather
> than describing it. `partition_lessons` read `status = lesson.get("status") or "approved"` and
> dropped only `rejected` and `pending`, so `inferred` — the status this policy gives an
> unreviewed candidate — fell through to the kept list, and the store was the only thing in the
> way. It could not fire while no code path produced an `inferred` lesson. `oncall_memory.reflexion`
> produces them, which made it reachable the same week it was written. The test that asserted the
> fail-open was the one test in the repo written to start failing; #36 made it fail, and it is
> now inverted.

An output guardrail enforces this: a claim sourced from a record that is not `observed` must not
be presented as telemetry. This is the same standard the corpus uses — 16 of 18 runbooks are
`structural` or `hypothesis`, and the agent may not cite those as evidence either.

## Two stores, and we are not building a third

| | Where | Lifetime |
|---|---|---|
| **Short-term** — the live investigation | `IncidentState` in the LangGraph checkpointer | one incident |
| **Long-term** — episodes and lessons | `oncall-memory`, seeded from `data/postmortems/` | across incidents |

The long-term half is a workspace member rather than a store object passed around, because the
import rules leave no other route: `oncall_graph` may not import `oncall_knowledge`, and neither
may `oncall_service`. The graph receives a store **object** and calls `search(service, symptom,
status)` on it, so it needs no import at all; the service constructs it. A lesson's committed
source is a postmortem, which is what makes `derived_from` resolve and `forget(incident_id)`
able to cascade.

`IncidentState` already carries `alert`, `scope`, `severity`, `hypotheses`, `evidence`, `plan`,
`proposed_action`, `verification` and `transcript`, with `operator.add` reducers on the
accumulating channels. **Working state does not go in the long-term store** — that would be a
second home for something already persisted, and the two would drift.

## What is saved

| Saved | Status | Why |
|---|---|---|
| Alert that opened the incident | `observed` | the incident's identity |
| Which hypotheses were tested and their outcome | `inferred` | stops the agent re-testing a dead end |
| **References** to evidence — trace IDs, the PromQL executed, the time window | `observed` | see *What is never saved* |
| The exact action approved, by whom, and the payload | `observed` | the audit trail the approval gate exists to produce |
| Verification result after the action | `observed` | whether the fix worked |
| Candidate lessons proposed by the Reflexion loop | `inferred` → `approved`/`rejected` | the playbook |

## What is never saved

- **Raw telemetry payloads.** Store the trace ID and the query, not the log line. Logs on this
  stack can contain PII — the demo ships an `emitRawPii` flag that emits `user.email`,
  `demo.payment.card_number` and `card_cvv` — so a memory store holding raw log text becomes a
  PII store with none of the controls. References are also smaller and stay valid.
- Secrets, credentials, tokens, API keys. Input guardrails redact before any model sees a log
  line; memory must not reintroduce what redaction removed.
- Customer identifiers of any kind. Nothing in diagnosis needs them.
- Anything the agent generated but a human has not seen, in a field that reads as fact.

## When it is saved

Writes happen at defined points, never opportunistically:

1. **On incident open** — the alert, as `observed`.
2. **After each hypothesis is tested** — outcome plus evidence references.
3. **After the approval gate resolves** — the exact approved payload and the approver. Never
   before: an approval that was edited is not the approval that was requested.
4. **After verification completes** — the measured outcome. An action is recorded as successful
   only when a success signal confirms it, never on the absence of errors. An idle shop also
   has no errors.
5. **After human review of a candidate lesson** — as `approved` or `rejected`.

## Who can read it

| | Access |
|---|---|
| All five owners | read and write, via the repo and the incident UI |
| The agent, during an incident | read `observed`, `inferred`, `approved`; **never** `rejected` as guidance |
| The evaluation harness | read-only, and **never** during a scored run (see *Evaluation boundary*) |
| Anyone else | no access; the store is local to the deployment |

There is no multi-tenancy and no external consumer. If that changes, this section changes first.

## How it is corrected or deleted

- **Correct a record:** append a superseding record referencing the original; never edit in
  place. The original stays with its status changed to `superseded`, because "we believed X at
  10:04" is itself evidence when reconstructing a timeline.
- **Reject a lesson:** set `rejected` with the reason. Keep it — the guide requires one rejected
  lesson and its reason as a deliverable, and a rejected lesson is a useful counterexample.
- **Delete an incident:** `forget(incident_id)` removes the episode **and every lesson derived
  from it**, by the `derived_from` field each lesson carries. A lesson that outlives the
  incident that justified it is unfalsifiable.
- **Delete everything:** the store is a file in `artifacts/`, which is gitignored and
  reproducible. Deleting it loses the playbook and nothing else.

## Lessons: the lifecycle

A lesson is not an observation. It is a generalisation the agent proposes, and it enters the
playbook only through a person.

```
Reflexion loop proposes          (status: inferred)
  -> human reviews               (approved | rejected, with a reason)
    -> enters the playbook       (approved only)
      -> PLAYBOOK FROZEN         before any held-out run
```

Every lesson states **preconditions**. Before applying one, the agent checks each precondition
against *current* telemetry. A lesson whose preconditions do not match is not applicable, and
saying so is a correct outcome — it is demo scene 3.

A worked example from this project's own history. After the first incident, a reasonable lesson
was *"when a service is unhealthy but up, check recent flag and config changes first."* That
lesson is correct for `docs/decisions/0001-*` — a broken health check. Applied to
`docs/decisions/0002-*`, a garbage-collection spiral against a memory ceiling, it sends you
entirely the wrong way: nothing had changed, and `RestartCount: 0`, `OOMKilled: false`,
`ExitCode: 0` all looked reassuring. **The precondition that separates them is "a configuration
or flag change precedes the alert."** Without it, the lesson is a trap.

### One fault family may need one lesson per alert register

`precondition_matches` evaluates a single `symptom` string by containment, in both directions.
A fault that pages under two differently worded alerts therefore cannot be covered by one
lesson, because the precondition would have to be a substring of both.

`paymentFailure` is the live example. It pages as a metric,
`checkout error rate above 5% for five minutes`, and as the flagd variant description,
`Fail payment service charge requests n%`. Those share no substring. Until `#33` made the
second one the demo's headline page, the corpus held one payment lesson keyed on `checkout
error rate`, and on that page the playbook kept **nothing** — three rejections and no
guidance, which is indistinguishable from an empty playbook.

The authoring rule: write one lesson per register, each with a precondition naming the text it
applies to. Do **not** widen an existing lesson to cover a register it was not written for, and
do not reduce a precondition to `service` alone — `payment` would then also match
`Payment service is unavailable`, which is `paymentUnreachable`, a different fault with a
different first read. `tests/test_demo_scene_3.py::test_every_alert_the_demo_posts_has_a_stated_memory_outcome`
pins coverage against the alerts `team_demo` actually publishes, importing them rather than
restating them, so the next rewording fails a test instead of quietly emptying the scene.

Expressing alternatives in one precondition would remove the duplication, and that is
`precondition_matches` — P2's file, and the open ask in CONTRIBUTING.md.

## Evaluation boundary

**Memory is reset between evaluation cases.** Without that, a lesson learned on case 7 changes
case 8, and the held-out score measures the order the cases ran in.

- Development runs: memory on, lessons may be proposed.
- **Held-out runs: the playbook is frozen and no new lessons are written.**
- The required memory-on versus memory-off comparison uses the same frozen playbook for both
  arms, so the difference is the memory, not a moving target.

This is the same failure class as held-out contamination, and it is silent in the same way: it
makes scores go *up* and raises no error. It belongs in `tests/test_guards.py` alongside
`test_playbook_frozen_before_heldout`, not in a convention.

**A note P4 should have before reading E2.** `les-confirm-the-variant-the-page-names` is strong
guidance for one fault family: on the `paymentFailure` charge-requests page, the alert text
quotes the flag variant, and the lesson says to read the flag baseline first. That is honest —
it is what the page affords, and a human on call would do the same — but it means the memory-on
arm has an unusually short path on this family specifically. The arm difference on
`payment-failure` cases should not be read as a general estimate of what memory is worth. It is
disclosed here rather than tuned out of the corpus, because the alternative is a playbook
quietly shaped to make its own evaluation look good.

## What reflexion may read, and why most of a run record is off limits

A candidate lesson is derived from a finished run, and the obvious input is `RunRecord`. **Most
of `RunRecord` is the evaluator's answer key.** P4's harness injects the fault, so `fault_id`,
`case_id`, `complexity`, `correct_root_cause`, `root_cause_rank`, `correct_action_proposed`,
`harmful_actions`, `cleanup_verified`, `verified_recovery`, `passed` and `failure_reasons` are
all scored against ground truth that does not exist outside the harness.

A lesson derived from any of them would be correct **because the grader said so**. The playbook
would fill with accurate guidance, every entry would check out on inspection, and the memory-on
arm of the E2 comparison would be measuring `eval/faults/manifest.yaml` rather than measuring
learning. Scores would go up. Nothing would error. It is the same failure class as held-out
contamination and it is silent in the same way.

So the split is explicit and enforced in code, not in a convention:

| | Fields | Why |
|---|---|---|
| **`OBSERVABLE_FIELDS`** — reflexion may read | run identity, `config_id`, own cost and budget, `system_reported_recovery`, `independently_verified_recovery`, `rolled_back`, `model_ids`, `playbook_hash` | all of it exists after a real incident, with no oracle anywhere |
| **`ORACLE_FIELDS`** — refused | the eleven above | the evaluator's answers |

Three rules hold it:

1. **The two sets must partition `RunRecord` exactly.** A field in neither would make the
   classification advisory; a field in both would let dict order decide. A field added to the
   contract later fails the test until somebody classifies it, which is the point — the default
   must not be "into the agent's memory".
2. **An unclassified field raises** rather than being passed through. The field nobody
   classified is precisely the one that turns out to be the oracle's.
3. **`verified_recovery` is refused even though it is derivable** from two permitted fields.
   It is the harness's verdict. Reflexion composes its own reading from
   `system_reported_recovery` and `independently_verified_recovery`, in three states rather than
   two, because the interesting case is where those two disagree — a run that reported recovery
   telemetry never confirmed, which is exactly what `docs/objective-1-live-run.md` captured. A
   module that read the grade would not have to notice.

`service` and the alert text are **arguments** to `propose`, not read from the run, for the same
reason: they are the incident's inputs, what paged and which service it named, and they are what
a human had in front of them too. The run record's own notion of which service was at fault is
the oracle's.

The test that enforces this alters every refused field, one at a time and then all at once, and
asserts the proposed lesson is byte-identical. It is what fails if someone reaches for
`run["fault_id"]` to fill in the service — verified by doing exactly that.

## The model writes one sentence

`reflexion.propose` takes a drafter and ignores everything it returns except a string. `status`
is forced to `inferred`, `preconditions` are restricted to the two keys
`oncall_graph.briefs.precondition_matches` actually evaluates, `derived_from` comes from the
run's identity, and `evidence_refs` are assembled from observed values.

Letting a model write `preconditions` is the specific thing refused. `precondition_matches` reads
`service` and `symptom` and **silently ignores every other key**, so a drafted
`{"deploy_in_last_30m": "true"}` would read as a careful, well-guarded lesson and match every
future incident. `EVALUABLE_PRECONDITIONS` exists so that authoring one is refused rather than
ignored, and reflexion may not route around it.

A drafter that raises, times out or returns nothing usable falls back to the deterministic
sentence rather than losing the proposal — reflexion runs after the incident is over, and a dead
provider must not take away the run's only chance to leave a note. But asking for a model when
none is configured **raises**: a caller that requested a model and silently got the template
would report a reflexion loop it did not run.

## Reviewing a proposal

```
python -m oncall_memory.review list                      # pending, marked `inferred`
python -m oncall_memory.review show les-4cf49afd         # the lesson and every decision on it
python -m oncall_memory.review approve les-4cf49afd --by disen
python -m oncall_memory.review reject  les-4cf49afd --by disen --reason "..."
python -m oncall_memory.review rejected                  # refusals, with their reasons
```

`--reason` is required to reject, and there is deliberately **no `--approve-all`**: the value of
this gate is that a person read the sentence, and a flag that approved an unread list would
leave an audit trail saying a person was there, which is worse than no gate at all.

The log is append-only, under `artifacts/`. Approving does not rewrite the proposal; it appends
a decision carrying the lesson **as it was reviewed**, so "what was I shown?" has an answer and
an edit upstream cannot silently change what someone approved. A malformed line raises rather
than being skipped: a playbook quietly missing the lessons in an unreadable row would report as
a playbook with nothing to review.

## What is code and what is judgement

Per the project's first rule — *the model proposes; code and people decide*:

**Code:** the write gates above; precondition evaluation against current telemetry; the
provenance status on every record; the per-case reset; `forget(incident_id)` cascading to
derived lessons.

**Judgement:** whether a candidate lesson generalises; whether two incidents are alike enough
for one to inform the other; what a precondition should say.

**A person, always:** approving a lesson into the playbook, and approving any action.
