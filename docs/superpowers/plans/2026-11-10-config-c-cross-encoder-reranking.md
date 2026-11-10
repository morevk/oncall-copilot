# Config C — cross-encoder reranking implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: use `superpowers:executing-plans` to implement this
> plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> **Not** `superpowers:subagent-driven-development`: the repository owner has a standing
> instruction that no subagent-driven development or parallel agent dispatch runs on this
> repository, because each owner must be able to defend their own component from memory at an
> individual viva.

**Goal:** add a cross-encoder reranking stage as retrieval **config C**, and measure it against
the committed config B on the identical 71 dev queries and the identical 18-document corpus.

**Architecture:** config C is config B's hybrid retriever with a second stage bolted on. The
first stage is unchanged — BM25 and MiniLM fused by RRF at `k0=60`. The cross-encoder then
scores every candidate the fusion returns and reorders them; truncation to `k` happens *after*
the rerank, never before. A new `indexing/rerank.py` holds the model, mirroring
`indexing/semantic.py` exactly: a pinned revision, a lazy import, and a hard failure rather than
a silent fallback.

**Tech Stack:** Python 3.12, `sentence_transformers.CrossEncoder` (already present in the
`semantic` optional extra — **no new dependency and no lockfile change**), pytest.

**Spec:** [`docs/superpowers/specs/2026-11-10-p1-reranking-and-query-rewriting-design.md`](../specs/2026-11-10-p1-reranking-and-query-rewriting-design.md),
§7–§11, and [ADR-0008](../../decisions/0008-reranking-and-query-rewriting.md). Read both: this
plan argues from them and does not restate their reasoning.

## Global Constraints

Every task's requirements implicitly include these. Values are copied verbatim from the spec.

- **`n_documents` must remain 18.** The seed postmortems stay out of the retrieval index until C
  and D are measured. `compare()` refuses runs whose `n_documents` differs, so indexing them
  first makes the B→C comparison refuse to run (spec C3, ADR-0008).
  `tests/test_guards.py::test_postmortems_are_not_in_the_retrieval_index` must stay green.
- **Config C must differ from config B in the reranker and nothing else.** Not `k0`, not the
  hybrid weights, not the embedding model (spec §3).
- **The `query_set_hash` of the C record must equal that of the committed B record**
  (`8193aa9e033928cd2d39f1605ef0d11921458a69ae0c04304e3fd0ef959b48a6`), or `compare` refuses
  (spec C1).
- **No top-k window for the reranker.** It scores every candidate the first stage returns
  (spec D1).
- **Rerank first, truncate second.** Truncating first reintroduces the window through the back
  door (spec D1).
- **Pinned model revision, recorded on the run.** A moving reference changes the ruler between
  runs (spec C5).
- **`compare()`'s two agreement checks are not extended.** Requiring reranker identity to agree
  would make B→C refuse by construction (spec D2).
- **Per-query output is mandatory.** `evaluate()` retains a `CaseResult` per query; config C must
  not reduce that to an aggregate (spec C6).
- **No API key and no network in the scoring path** beyond the one-time model download
  (spec C4).
- **A rerank that makes `Recall@1` worse ships as a published negative result.** Config B is not
  deleted and not re-tuned to flatter C (spec D6).

## Review Focus

Five conditions the spec implies that no task's happy path exercises. Each has a test assigned to
the task owning the code.

1. **`sentence-transformers` absent.** Must raise, never fall back to the unreranked order. A
   fallback would make C score *identically* to B and the artifact would read "no gain from
   reranking" when the truth is "the reranker never ran" — the exact failure
   `semantic.py`'s docstring was written to prevent. → Task 1.
2. **Empty candidate list.** A query matching no document lexically yields no candidates;
   reranking nothing must return nothing, not raise and not call the model. → Task 1.
3. **Tied cross-encoder scores.** Ordering must be deterministic across runs, or two runs of
   config C produce different artifacts and the record stops being evidence. → Task 1.
4. **`k` greater than the candidate count** — the real case, since `evaluate()` asks for 20 over
   18 documents. Must return all of them, neither padded nor raising. → Task 2.
5. **Provenance survives the rerank.** `lexical_rank`, `semantic_rank` and `status` must still be
   carried on each result, or a reranked hit becomes unattributable and `status` stops reaching
   the commander that decides how far to trust a `structural` runbook. → Task 2.

---

## File structure

- **Create** `packages/oncall-knowledge/src/oncall_knowledge/indexing/rerank.py` — the model and
  the ordering contract. Owns pinning, the lazy import, the unavailable error, and nothing else.
- **Create** `packages/oncall-knowledge/tests/test_rerank.py` — unit tests for the module.
- **Modify** `packages/oncall-knowledge/src/oncall_knowledge/retrieval.py` — `Retriever` gains a
  `rerank` flag; `retrieve` applies the second stage.
- **Modify** `packages/oncall-knowledge/tests/test_retrieval.py` — config C through the public
  surface.
- **Modify** `packages/oncall-knowledge/src/oncall_knowledge/experiment.py` — `CONFIG_IDS`,
  `build_record` fields, `main` wiring.
- **Modify** `packages/oncall-knowledge/tests/test_experiment_compare.py` — record fields and a
  B→C comparison that runs.
- **Create** `artifacts/retrieval/C-8193aa9e-<timestamp>.json` — the measurement (Task 4).

---

### Task 1: the reranker module

**Files:**
- Create: `packages/oncall-knowledge/src/oncall_knowledge/indexing/rerank.py`
- Test: `packages/oncall-knowledge/tests/test_rerank.py`

**Interfaces:**
- Consumes: `IndexEntry` from `oncall_knowledge.indexing.text` (has `document_id`, `status`, and
  the indexed text — confirm the text attribute's name before writing Step 3).
- Produces:
  - `class RerankUnavailableError(RuntimeError)`
  - `class CrossEncoderReranker` with `MODEL_ID: str`, `MODEL_REVISION: str`,
    `classmethod build(entries: Sequence[IndexEntry]) -> CrossEncoderReranker`, and
    `order(query: str, document_ids: Sequence[str]) -> list[str]` returning the same ids, most
    relevant first.

- [ ] **Step 1: pin the model revision**

Choose the cross-encoder and resolve its exact commit. `cross-encoder/ms-marco-MiniLM-L-6-v2` is
the recommended starting point on size and licence; the choice is yours per spec §8 and must be
recorded with a one-line reason in the module docstring.

```bash
uv run --extra semantic python -c "from huggingface_hub import model_info; \
    print(model_info('cross-encoder/ms-marco-MiniLM-L-6-v2').sha)"
```

Record the value as `MODEL_REVISION`, with the same comment `semantic.py` carries explaining why
a moving reference like `main` is not allowed.

- [ ] **Step 2: write the failing tests**

```python
def test_a_missing_extra_raises_rather_than_returning_the_original_order():
    """A fallback would make config C score identically to config B."""
    # monkeypatch the sentence_transformers import to raise ImportError
    with pytest.raises(RerankUnavailableError, match="optional"):
        CrossEncoderReranker.build(entries)


def test_reranking_no_candidates_returns_no_candidates():
    reranker = CrossEncoderReranker.build(entries)
    assert reranker.order("anything", []) == []


def test_tied_scores_keep_the_incoming_order():
    """Determinism: two runs of config C must produce the same artifact."""
    # a stub scorer returning one constant for every pair
    assert reranker.order("q", ["a", "b", "c"]) == ["a", "b", "c"]


def test_the_order_is_a_permutation_of_what_it_was_given():
    out = reranker.order("payment charge requests fail", ids)
    assert sorted(out) == sorted(ids)
```

The first and third must not require the real model: inject the scorer, or monkeypatch the
import. Mark only the tests that load the real model with
`pytest.importorskip("sentence_transformers")`, following `test_retrieval.py:114`.

- [ ] **Step 3: run them and watch them fail**

Run: `uv run pytest packages/oncall-knowledge/tests/test_rerank.py -v`
Expected: FAIL, `ModuleNotFoundError: oncall_knowledge.indexing.rerank`

- [ ] **Step 4: implement `rerank.py`**

`RerankUnavailableError` is deliberately **not** an `ImportError` subclass, for the reason
`SemanticUnavailableError` gives: `except ImportError` is exactly how an optional dependency
becomes a silent fallback. `build` loads
`CrossEncoder(MODEL_ID, revision=MODEL_REVISION)` inside a `try/except ImportError` and re-raises
as `RerankUnavailableError` naming `uv sync --extra semantic`. `order` scores `(query, text)`
pairs and sorts **stably** by descending score — `sorted(..., key=..., reverse=True)` on a list
already in candidate order is stable in Python and is what makes the tie test pass.

- [ ] **Step 5: run them and watch them pass**

Run: `uv run pytest packages/oncall-knowledge/tests/test_rerank.py -v`
Expected: PASS, with the real-model tests skipped unless `--extra semantic` is installed.

- [ ] **Step 6: commit**

```bash
git add packages/oncall-knowledge/src/oncall_knowledge/indexing/rerank.py \
        packages/oncall-knowledge/tests/test_rerank.py
git commit -m "feat(knowledge): a pinned cross-encoder that refuses to fall back"
```

---

### Task 2: config C through the Retriever

**Files:**
- Modify: `packages/oncall-knowledge/src/oncall_knowledge/retrieval.py`
- Test: `packages/oncall-knowledge/tests/test_retrieval.py`

**Interfaces:**
- Consumes: `CrossEncoderReranker` from Task 1, built as
  `CrossEncoderReranker.build(lexical.entries)` — `Retriever.__init__` already reads
  `lexical.entries` for its `status` map, so the entries the reranker needs are in hand and no
  second ingestion pass is required.
- Produces: `Retriever.build(documents, *, semantic: bool, k0: int = DEFAULT_K0, rerank: bool = False)`.
  `rerank=True` requires `semantic=True`; the combination `semantic=False, rerank=True` raises
  `ValueError`, because config C is defined as config B plus a reranker and nothing else.
  `Retriever.search(query, k)` keeps its signature, so `evaluate()` is untouched.

- [ ] **Step 1: write the failing tests**

```python
def test_rerank_without_the_semantic_half_is_refused():
    with pytest.raises(ValueError, match="config C is config B plus"):
        Retriever.build(documents, semantic=False, rerank=True)


def test_a_k_larger_than_the_corpus_returns_the_whole_corpus(documents):
    pytest.importorskip("sentence_transformers")
    hits = Retriever.build(documents, semantic=True, rerank=True).retrieve("payment", 20)
    assert len(hits) == len(documents)


def test_the_rerank_preserves_fusion_provenance(documents):
    pytest.importorskip("sentence_transformers")
    top = Retriever.build(documents, semantic=True, rerank=True).retrieve("paymentFailure")[0]
    assert top.status
    assert top.lexical_rank is not None or top.semantic_rank is not None


def test_the_rerank_scores_every_candidate_not_only_the_reported_k(documents):
    """Truncation happens AFTER the rerank. A document the fusion placed 12th
    can therefore reach rank 1, which is the whole point of the stage."""
    # inject a scorer that ranks the fusion's LAST candidate highest
    assert reranked[0].document_id == fused[-1].document_id
```

- [ ] **Step 2: run them and watch them fail**

Run: `uv run pytest packages/oncall-knowledge/tests/test_retrieval.py -v -k rerank`
Expected: FAIL, `Retriever.build() got an unexpected keyword argument 'rerank'`

- [ ] **Step 3: implement the second stage**

In `build`, reject `rerank and not semantic`; otherwise construct the reranker inside the
`semantic` branch, importing it there so config A stays runnable with no torch installed — the
same reason the `SemanticIndex` import sits in that branch. In `retrieve`, when a reranker is
present, compute the fused ranking **without** the existing `[:k]` truncation, pass every fused
`document_id` to `order()`, rebuild the `RetrievalResult` list in the returned order preserving
`status`, `lexical_rank` and `semantic_rank`, and truncate to `k` last.

`score` needs a decision you should record in a comment: the RRF score no longer explains the
order. Either carry the cross-encoder score or set it and say so. Do not leave a fused score
sitting next to a reranked order as though it produced it.

- [ ] **Step 4: run them and watch them pass**

Run: `uv run pytest packages/oncall-knowledge/tests/test_retrieval.py -v`
Expected: PASS, config A and B tests unchanged.

- [ ] **Step 5: commit**

```bash
git add packages/oncall-knowledge/src/oncall_knowledge/retrieval.py \
        packages/oncall-knowledge/tests/test_retrieval.py
git commit -m "feat(knowledge): config C is config B plus a rerank over every candidate"
```

---

### Task 3: the experiment records config C

**Files:**
- Modify: `packages/oncall-knowledge/src/oncall_knowledge/experiment.py`
- Test: `packages/oncall-knowledge/tests/test_experiment_compare.py`

**Interfaces:**
- Consumes: `Retriever.build(..., rerank=...)` from Task 2.
- Produces: `CONFIG_IDS = ("A", "B", "C")`; `build_record(..., reranker_id: str | None = None,
  reranker_revision: str | None = None)` emitting both keys on every record.

Config D is **not** added to `CONFIG_IDS` here. It arrives with its own plan, and a `--config D`
that runs nothing would be a worse lie than its absence.

- [ ] **Step 1: write the failing tests**

```python
def test_every_record_carries_the_reranker_fields_present_and_null():
    """Null means 'there is no reranker'; a missing key means 'we forgot to
    record it', and compare() has to tell those apart -- the reason model_id is
    null for config A rather than omitted."""
    record = build_record("B", result, query_set_name="dev", n_documents=18)
    assert record["reranker_id"] is None
    assert record["reranker_revision"] is None


def test_a_config_c_record_names_the_reranker_and_its_revision():
    record = build_record(
        "C", result, query_set_name="dev", n_documents=18, reranker_id="x", reranker_revision="y"
    )
    assert record["reranker_id"] == "x"


def test_b_and_c_are_comparable_despite_differing_rerankers():
    """The two agreement checks are query_set_hash and n_documents. Adding a
    third on reranker identity would make this comparison refuse by
    construction."""
    report = compare(b_record, c_record)
    assert report  # does not raise
```

- [ ] **Step 2: run them and watch them fail**

Run: `uv run pytest packages/oncall-knowledge/tests/test_experiment_compare.py -v`
Expected: FAIL with `KeyError: 'reranker_id'`

- [ ] **Step 3: implement**

Add the two parameters to `build_record` and the two keys to the returned dict, beside
`model_revision`. Set `CONFIG_IDS = ("A", "B", "C")`. In `main`: `semantic = args.config in ("B", "C")`,
`rerank = args.config == "C"`, and when reranking, read
`CrossEncoderReranker.MODEL_ID` and `.MODEL_REVISION` for the new fields, importing it in that
branch as `SemanticIndex` already is. Extend the printed summary with the reranker id when
present.

- [ ] **Step 4: run the whole package and the gate**

Run: `uv run pytest packages/oncall-knowledge -q && make lint && make guards`
Expected: PASS. No existing test pins the record's key set, so the two new keys break nothing —
verified before this plan was written.

- [ ] **Step 5: commit**

```bash
git add packages/oncall-knowledge/src/oncall_knowledge/experiment.py \
        packages/oncall-knowledge/tests/test_experiment_compare.py
git commit -m "feat(knowledge): record which reranker produced a run, or that none did"
```

---

### Task 4: measure it, and publish whatever it says

**Files:**
- Create: `artifacts/retrieval/C-8193aa9e-<timestamp>.json`
- Modify: `docs/superpowers/specs/2026-11-10-p1-reranking-and-query-rewriting-design.md` (record
  the result against §1's prediction)

This task has no new tests. Its deliverable is a measurement and it is the point of the other
three.

- [ ] **Step 1: run config C on the dev set**

```bash
uv sync --extra semantic
uv run python -m oncall_knowledge.experiment --config C --cases dev
```

- [ ] **Step 2: verify the record satisfies spec §11 acceptance**

The written record must carry `reranker_id`, `reranker_revision`, `n_documents: 18`, and
`query_set_hash` beginning `8193aa9e`. A different hash or document count means a Global
Constraint was violated and **the result is void, not merely worse** — stop and fix rather than
publishing it.

- [ ] **Step 3: run the comparison**

```bash
uv run python -m oncall_knowledge.experiment --compare \
    artifacts/retrieval/B-8193aa9e-20261104T144753.json \
    artifacts/retrieval/C-8193aa9e-<timestamp>.json
```

Expected: it runs without raising. A refusal here is C1 or C2 violated.

- [ ] **Step 4: name the 18 movable queries individually**

The baseline has 53 queries at rank 1 and 18 at ranks 2–12. For each of those 18, record the rank
before and after. The bar asks for evidence of *why* retrieval improved, and a reranker that
fixes 12 and breaks 2 is a different result from one that fixes 14 cleanly — only the per-query
table distinguishes them. Report `Recall@1`, `Recall@3`, MRR, and how many of the 53 rank-1
queries the rerank *lost*.

- [ ] **Step 5: settle which text the cross-encoder scores, by measuring both**

Spec §8 leaves this to you and says it is worth measuring both ways because it is cheap on 18
documents: `(query, title + symptom)` against `(query, full body)`. Run config C once each way
and keep both records. Ship the better one as the config C result, and say in the design which
you chose and by how much — an unexplained choice of input text is a tuning knob nobody can
audit.

- [ ] **Step 6: commit the artifact and the result**

Publish it whether it improved or not, per spec D6. If `Recall@1` fell, the artifact stays, the
design records the negative result, and config B remains the shipped configuration.

```bash
git add artifacts/retrieval/ docs/superpowers/specs/2026-11-10-p1-reranking-and-query-rewriting-design.md
git commit -m "measure(knowledge): config C against config B on the 71 dev queries"
```

---

## Verification before the PR

```bash
make lint && make test && make guards && make eval-fixture
```

All four must pass with no extras installed, which is what CI runs — the real-model tests skip
there, exactly as config B's already do.
