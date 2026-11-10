# P1 — reranking and query rewriting: design

**Date:** 2026-11-10
**Owner:** P1 (domain, knowledge & memory)
**Status:** awaiting approval
**Supersedes part of:** `2026-11-04-p1-ingestion-and-retrieval-design.md` §8

---

## 1. Intent, and why now

The capstone minimum bar names a **reranker** and **one query-side technique** as components to
build. Our own ingestion-and-retrieval design listed both as non-goals, on the honest grounds
that hybrid BM25 + vector + RRF already scored `Recall@5 0.972` and there was no headroom worth
chasing. That argument was sound engineering and is a weak marking argument: the bar asks for
evidence of having built them, and "we did not need it" is not that evidence.

This document reverses that non-goal deliberately, with a measurement rather than a vibe, and
it refuses to reverse it in the one place the measurement does not support.

### The measurement that shapes everything

Gold-document rank under config B (hybrid, `k0=60`), all 71 dev queries:

| rank of first gold | queries | share |
|---|---|---|
| 1 | 53 | 0.746 |
| 2 | 8 | 0.113 |
| 3 | 6 | 0.085 |
| 4 | 1 | 0.014 |
| 5 | 1 | 0.014 |
| 6 | 1 | 0.014 |
| 12 | 1 | 0.014 |
| **not in top 20** | **0** | **0.000** |

`Recall@1 = 0.7465` · `Recall@3 = 0.9437` · `Recall@5 = 0.9718` · `Recall@20 = 1.0000`

Two conclusions follow, and they point in opposite directions:

1. **A reranker has real, bounded headroom.** Ceiling `Recall@1 = 1.000`, headroom **+0.2535 —
   exactly 18 queries**, each individually inspectable. MRR headroom `0.841 → 1.000`.
2. **Query rewriting has a ceiling of zero on this query set.** Rewriting earns its keep by
   pulling in documents the original query missed. Nothing is missed: `Recall@20 = 1.000`.

The structural reason is worth stating at the viva: the corpus is **18 documents**, so
"retrieve the top 20" *is* the whole corpus. Depth-based recall cannot discriminate anything at
that size, and the entire retrieval problem is **ordering**. A cross-encoder therefore needs no
top-k window — it can score all 18 candidates.

---

## 2. Goals

- **G1.** A reranking stage, measured as **config C** against config B on the same 71 dev
  queries, same corpus, same ruler — reported as `Recall@1`, `Recall@3`, MRR, and the per-query
  rank table, with the 18 movable queries named individually.
- **G2.** A query-rewriting stage, **built** and unit-tested, and **scored only against a
  held-out query set that does not yet exist** (§6).
- **G3.** An ADR that records the reversal of the §8 non-goals, so the decision is defensible
  rather than silent.
- **G4.** Honest naming everywhere: the 71 queries are **author-derived development queries**,
  never "blind".

## 3. Non-goals

- A vector database, persistence, multi-hop retrieval, chunking, answer generation. The
  2026-11-04 spec's other non-goals stand unchanged.
- Tuning `k0`, the hybrid weights, or the embedding model. Config C must differ from config B
  **in the reranker and nothing else**, or the delta measures the wrong thing.
- Folding the 10 seed postmortems into the retrieval index. See §5, C3 — this is now blocked,
  not merely deferred.

## 4. Anti-goals

These mean failure even with good numbers. The first four are inherited from the 2026-11-04
spec §8 and still bind:

- A `Recall@1` improvement that comes from the index seeing the queries.
- **A retrieval layer that cannot be scored without an API key.** This one directly constrains
  the rewriter and is the reason for decision D4.
- Parameters tuned before anything needs tuning.
- A single aggregate number with no per-query output.

New to this document:

- **Reporting a reranker gain measured on author-derived queries as though it were a graded
  number.** The gain will be real; its provenance must travel with it.
- **Calling any query set "blind" that we or the upstream demo authored.** See §6, and the
  rejected option with its number, so nobody re-proposes it.
- **Scoring the rewriter on the dev set and reporting the flat line as a result.** A measurement
  whose ceiling is known to be zero is not evidence of anything.

---

## 5. Constraints the builder must respect

- **C1 — same ruler.** `compare(before, after)` refuses two runs whose `query_set_hash`
  differs. Config C must score the identical 71 cases.
- **C2 — same corpus.** `compare` also refuses two runs whose `n_documents` differs, "because
  the corpus changed size, which moves every metric on its own."
- **C3 — therefore the postmortems stay out of the index until C and D are measured.** This
  **corrects advice given earlier in this project**: the retrieval-index question is not "better
  settled before the reranker work". Settling it first takes the index from 18 documents to 28
  and makes the B→C comparison *refuse to run*. The guard
  `tests/test_guards.py::test_postmortems_are_not_in_the_retrieval_index` is what keeps config C
  comparable, and it must stay green through this work.
- **C4 — no API key in the scoring path.** CI runs `uv sync --frozen` with no extras, so
  `sentence-transformers` is absent and the semantic tests already `pytest.importorskip`. The
  reranker follows that exact precedent. The rewriter must be scoreable with no network at all.
- **C5 — pinned model revisions.** `SemanticIndex.MODEL_REVISION` pins an exact commit, and
  `build_record` records `model_id` and `model_revision` on every run, present-and-null for
  config A rather than omitted. The reranker is recorded the same way.
- **C6 — per-query output is mandatory.** `evaluate()` already retains a `CaseResult` per query;
  config C must not reduce that to an aggregate.

---

## 6. The blind query set: what we may and may not claim

**There is no independent incident material in this repository.** This was tested, not assumed.

The candidate was the upstream OpenTelemetry flagd variant descriptions — 18 of 18 flags carry
one, authored by the demo's maintainers rather than by us. Measured token overlap with the
runbook each one retrieves:

| source | n | median overlap with gold document |
|---|---|---|
| our dev queries | 71 | **0.67** |
| upstream flagd descriptions | 17 | **0.83** |

Four score **1.00**: `paymentUnreachable`, `emailMemoryLeak`, `productCatalogFailure`,
`aiSlowResponse`. The cause is plain once seen — each runbook is named after its flag and was
written from the flag's description. That text is not independent of our corpus; it is the
*ancestor* of our corpus, and therefore the **least** independent material available.

**Rejected.** Recorded here with its number so it is not re-proposed.

### What would actually be independent

Two sources, and the first is already an open ask in the 2026-11-04 spec §10 ("Does P4 accept
authoring the 25 blind queries, and when?"):

- **Author-independent prose.** Written by owners who did not write the runbooks, from memory of
  the faults, without opening `data/runbooks`. P4 owns evaluation ground truth and the ask is
  already on record. Target 20–25 queries. Overlap must be **measured and published** alongside
  the set, not asserted.
- **Shop-emitted text.** The real Grafana annotation and Jaeger span error strings captured
  while each fault is injected. Authored by neither us nor upstream prose, and it is the
  distribution the agent genuinely receives in production. This is the stronger of the two on
  independence and the weaker on coverage, so they complement rather than compete.

Until one exists: the dev set keeps the name `evaluation.py:70` already gives it — "a
DEVELOPMENT set: each was written by the author of the runbook it retrieves, in the same
sitting" — and **config D is built but not scored**.

---

## 7. Decisions taken

- **D1 — Config C is config B plus a cross-encoder rerank over all 18 candidates.** Its first
  stage is the *same* hybrid retriever as B, built with `semantic=True` and the same `k0=60`, so
  the only difference between the two runs is the rerank. No top-k window: the corpus is smaller
  than any window worth choosing, and a window would be a parameter nobody can justify. The
  rerank is applied to everything the first stage returns and the result truncated afterwards,
  never the reverse — truncating first would reintroduce the window through the back door. The
  builder pins an exact model revision and records it. *(Settled as my call during the builder
  check — it follows from "differ in the reranker and nothing else", but was not written down.)*
- **D2 — Config C reuses the existing record shape.** `build_record` gains `reranker_id` and
  `reranker_revision`, present-and-null for A and B exactly as `model_id` is null for A. `compare`'s two
  agreement checks are not extended: adding a field that must agree would make B→C refuse by
  construction, which is the opposite of the point.
- **D3 — `CONFIG_IDS` becomes `("A", "B", "C", "D")`.** One `--config` flag, one measurement
  path, no branching on config id in `evaluate`.
- **D4 — The rewriter is a recorded-rewrite design, not a live LLM call.** A rewrite is produced
  once, keyed by a hash of the original query, and **committed** as a fixture. Scoring replays
  the fixture. This is what lets config D satisfy anti-goal "cannot be scored without an API
  key" while still being a genuine model-authored rewrite. A missing fixture entry is an error,
  never a silent fall-through to the original query.
- **D5 — The 18 movable queries are named in the artifact,** not just counted. The bar asks for
  evidence of *why* retrieval improved; a reranker that fixes 12 of 18 and breaks 2 is a
  different result from one that fixes 14 cleanly, and only the per-query table shows which.
- **D6 — A rerank that makes `Recall@1` worse ships as a published negative result.** The
  artifact is written either way. Config B is not deleted and not re-tuned to flatter C.
- **D7 — ADR-0008 records the non-goal reversal**, quoting §8 verbatim, carrying the rank table
  from §1, and stating that the query-expansion half of the reversal is conditional on §6.

---

## 8. Left to the builder

- The cross-encoder model choice and its pinned revision. `ms-marco-MiniLM-L-6-v2` is the
  obvious starting point on size and licence, but the choice is the builder's and must be
  recorded with the revision hash and a one-line reason.
- Whether the reranker scores `(query, title + symptom)` or `(query, full body)`. This is worth
  measuring both ways on 18 documents, because it is cheap and the answer is not obvious.
- Where the reranker sits in the package: a new `indexing/rerank.py` alongside
  `hybrid.py`/`semantic.py`, or a wrapper around `Retriever`. Either is fine provided
  `evaluate()` still receives one plain `search(query, k)` callable.
- The rewrite prompt, and which model authors the rewrites. Note it must not be the agent's own
  model family if the rewrite is later used in a judged path.
- The fixture format for D4, and its staleness check: a rewrite fixture whose source query no
  longer exists should fail loudly.

---

## 9. Sequence

1. ADR-0008, so the reversal is on record before code exists.
2. Config C, measured, artifact committed — B→C is the honest deliverable of this work.
3. Config D built and unit-tested, **unscored**, with the reason recorded in the artifact
   directory rather than only here.
4. The held-out set collected (§6). Only then: B, C and D re-scored on it, as a second
   comparison that does not touch the dev numbers.
5. Only after step 4: revisit whether the postmortems join the index (C3).

---

## 10. Open asks

1. **P4:** the 20–25 author-independent queries — the §10 ask from 2026-11-04, now the blocking
   dependency for config D rather than a nice-to-have.
2. **P1 (own):** the stale `n_documents` arithmetic in the 2026-11-04 spec §10 says "18 → 26
   documents" against 8 unwritten postmortems. Ten landed in PR #41, so the number is 28, and
   the clause is now load-bearing for C3 rather than advisory.

---

## 11. Definitions and acceptance

*Added during the builder check: a builder could not reproduce §6 or know when to stop without
these. Both are my calls unless you say otherwise.*

### How "overlap" in §6 is computed

Lowercase both texts, take `[a-z]+` tokens of length > 2, drop duplicates, and report
`|query_tokens ∩ document_tokens| / |query_tokens|` — the share of the query's vocabulary that
already appears in the document it is supposed to retrieve. Median across the set. No stemming,
no stop-word list: both would need justifying and neither changes the conclusion at 0.67 vs
0.83. Any published overlap number must name this definition, because a different tokenisation
gives a different number and the comparison is the only thing that matters.

### Acceptance — config C

1. `uv run python -m oncall_knowledge.experiment --config C` writes a record carrying
   `reranker_id`, `reranker_revision`, `n_documents: 18`, and the same `query_set_hash` as the
   committed B record.
2. `--compare B.json C.json` runs without raising. A refusal here means C1 or C2 was violated
   and the result is void, not merely worse.
3. The per-query table names each of the 18 movable queries with its rank before and after.
4. Tests `pytest.importorskip` the reranker dependency, so CI stays green with no extras
   installed, and `make lint` and `make guards` stay green — in particular
   `test_postmortems_are_not_in_the_retrieval_index`, which is what keeps C2 satisfiable.

### Acceptance — config D

1. The rewriter has unit tests covering a hit, a miss, and a stale fixture entry, and the miss
   raises rather than falling through to the original query.
2. No test or scoring path touches the network or reads an API key.
3. `artifacts/retrieval/` carries a short note stating that D is deliberately unscored and
   naming §6 as the blocker — so the gap is legible to a reader who never sees this document.
