# ADR-0008: reranking is in, query rewriting is in but unscored

**Status:** Accepted
**Date:** 2026-11-10
**Deciders:** P1 (domain, knowledge & memory)
**Supersedes part of:** [the ingestion and retrieval design](../superpowers/specs/2026-11-04-p1-ingestion-and-retrieval-design.md) §8, which named both as non-goals.
**Designed in:** [2026-11-10-p1-reranking-and-query-rewriting-design.md](../superpowers/specs/2026-11-10-p1-reranking-and-query-rewriting-design.md)

## Context

The ingestion and retrieval design listed, verbatim, these non-goals: "persistence, a vector
database, reranking, query expansion, multi-hop retrieval, chunking, answer generation." The
reasoning for the two this record reverses was that hybrid BM25 + vector + RRF already scored
`Recall@5 0.972` on 71 queries, so there was no headroom worth the dependency.

The capstone minimum bar names a reranker and one query-side technique as components to build.
"We did not need it" is honest engineering and is not evidence of having built it.

Before reversing anything, the headroom was measured. Rank of the first gold document under
config B across all 71 dev queries: **53 at rank 1, 8 at rank 2, 6 at rank 3, one each at 4, 5,
6 and 12, and none outside the top 20.**

`Recall@1 0.7465` · `Recall@3 0.9437` · `Recall@5 0.9718` · `Recall@20 1.0000`

Two facts follow, and they do not point the same way.

A reranker has bounded, inspectable headroom: `Recall@1` can reach 1.000, which is **18 named
queries**, with MRR `0.841 → 1.000`.

Query rewriting has a ceiling of **zero** on this query set. Rewriting earns its keep by
retrieving documents the original query missed, and `Recall@20 = 1.000` says nothing is missed.
The structural cause is that the corpus is **18 documents**, so "the top 20" is the whole
corpus: depth-based recall cannot discriminate at this size and the entire retrieval problem is
ordering.

A query set where rewriting could show anything would have to be worded unlike our own
runbooks. The obvious candidate was the upstream OpenTelemetry flagd variant descriptions,
authored by the demo's maintainers rather than by us. All 18 flags carry one; 17 map to a
runbook and those 17 were measured. Median token overlap with the runbook each one retrieves is
**0.83**, against **0.67** for our own 71 development queries, with four at **1.00**. Each runbook is named after its flag and was
written from the flag's description, so that text is not independent of our corpus — it is its
ancestor, and the least independent material available rather than the most.

There is therefore no author-independent incident material in this repository today.

## Decision

**Reranking leaves the non-goals.** It ships as **config C**: config B's hybrid retriever
followed by a cross-encoder over all 18 candidates, with no top-k window, measured against B on
the identical 71 cases and the identical corpus.

**Query expansion leaves the non-goals conditionally.** The rewriter is **built and unit-tested
as config D, and deliberately not scored**, until a query set exists that was written by someone
who did not author the runbooks. Scoring it on the development set would produce a flat line
whose ceiling we have already computed to be zero, and reporting that as a result would be
worse than not building it.

**The 71 queries are named what they are** — author-derived development queries, as
`evaluation.py:70` already says. No set this team or the upstream demo authored is called blind.

**The rewriter does not call a model at scoring time.** A rewrite is authored once and committed
as a fixture keyed by a hash of the original query; scoring replays it; a missing entry raises.
This keeps the §8 anti-goal "a retrieval layer that cannot be scored without an API key" intact
while the rewrite is still genuinely model-authored.

**The seed postmortems stay out of the retrieval index until C and D are measured.**
`compare()` refuses two runs whose `n_documents` differs, because the corpus changing size moves
every metric on its own. Indexing the 10 postmortems takes the corpus from 18 documents to 28
and would make the B→C comparison refuse to run. This reverses an earlier intuition that the
index question should be settled first: it is now strictly last.

Rejected, with reasons, so none is re-proposed:

- **Upstream flagd descriptions as a blind set.** Overlap 0.83, higher than our own queries.
- **A live model call in the scoring path.** Violates the standing anti-goal and makes the
  numbers unreproducible by anyone without a key.
- **A top-k window for the reranker.** With 18 documents every window is either the whole corpus
  or an unjustifiable parameter.
- **Scoring D on the development set and publishing the flat line.** A measurement with a known
  ceiling of zero is not evidence.

## Consequences

- `CONFIG_IDS` becomes `("A", "B", "C", "D")`. `build_record` gains `reranker_id` and
  `reranker_revision`, present-and-null for A and B exactly as `model_id` is null for A.
- `compare()`'s two agreement checks are **not** extended. Requiring reranker identity to match
  would make B→C refuse by construction, which is the opposite of the intent.
- `tests/test_guards.py::test_postmortems_are_not_in_the_retrieval_index` becomes load-bearing
  for measurement, not only for corpus hygiene. It must stay green through this work.
- The 2026-11-04 design's §10 arithmetic, "18 → 26 documents" against 8 unwritten postmortems,
  is stale: ten landed in PR #41, so the number is 28.
- The §10 ask to P4 — "Does P4 accept authoring the 25 blind queries, and when?" — is promoted
  from a nice-to-have to the blocking dependency for config D.
- A rerank that makes `Recall@1` worse is published as a negative result. Config B is not
  deleted and not re-tuned to flatter C.
- Retrieval still needs no vector database, no persistence and no chunking. Those non-goals are
  untouched.
