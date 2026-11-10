# oncall-memory

P1. The long-term half of memory: the **playbook** of lessons, loaded from the committed
postmortems in `data/postmortems/`.

`docs/memory-policy.md` governs what may be stored and when. This package implements the
read side of it:

```python
from oncall_memory import LessonStore

store = LessonStore.from_postmortems()  # data/postmortems/*.md
store.playbook_hash  # pins the playbook in a RunRecord
store.search(service="payment", symptom="checkout error rate", status="approved")
```

`search` satisfies the interface `oncall_graph.handoffs.load_tools` already expects — an
object with `search(service, symptom, status)` — so the graph never imports this package.
The service constructs the store and injects it.

## The write side: propose, then a person decides

```python
from oncall_memory import LessonStore, ProposalLog, propose

lesson = propose(run_record, alert="checkout error rate above 5%...", service="payment")
lesson.status  # always "inferred" -- never guidance
log = ProposalLog()  # artifacts/lessons.jsonl, append-only
log.propose(lesson)
```

Then, as a person:

```
python -m oncall_memory.review list
python -m oncall_memory.review approve les-4cf49afd --by you
python -m oncall_memory.review reject  les-4cf49afd --by you --reason "too general"
```

An approved lesson joins the playbook and `playbook_hash` moves:
`LessonStore.from_postmortems(proposals=log)` merges the committed corpus with what a person
approved, and the graph cannot tell them apart — provenance is in `derived_from`, not in which
file a lesson came from. Built **without** a log, the store is exactly the committed corpus,
which is what a held-out run wants: the policy freezes the playbook and writes no new lessons
during one.

**`propose` reads a deliberately narrow slice of `RunRecord`.** Most of that contract is P4's
scoring against a fault the harness injected, and a lesson derived from the answer key would be
right because the grader said so. `OBSERVABLE_FIELDS` and `ORACLE_FIELDS` must partition the
contract exactly, so a field added later fails a test until somebody classifies it. See
*What reflexion may read* in `docs/memory-policy.md`.

**The model writes one sentence and nothing else.** Every structured field is built by code,
`preconditions` are restricted to the two keys the graph actually evaluates, and no drafter
output can set a status. `forget(incident_id)` and supersede are the next change.

## The one thing to understand before changing `search`

It filters on **status only**, never on preconditions, even though it is handed the service and
the symptom. Narrowing here would be an optimisation that deletes a demo objective: scene 3 shows
*an old lesson being rejected because its preconditions do not match*, and what it renders is the
`dropped` list that `partition_lessons` returns. A store that never hands over the stale lesson
leaves nothing to reject, and the scene silently becomes an empty list.
