---
kind: postmortem
incident_id: inc-2026-11-05-01
title: product-catalog went slow without failing, and the GC runbook did not fit
service: product-catalog
symptom: product catalog latency rises sharply without hard errors
severity: sev2
lessons:
  - id: les-one-service-two-faults-read-the-flag-pair
    status: approved
    statement: >-
      product-catalog has two separate fault families with two separate flags, and they
      present oppositely: a memory ceiling leaves nothing listening on the port, while
      lock contention leaves requests slow but mostly succeeding. Read which of
      productCatalogFailure and productCatalogLockContention is set before reaching for
      either runbook, because the service name alone picks the wrong one half the time.
    service: product-catalog
    preconditions:
      service: product-catalog
      symptom: latency
    evidence_refs:
      - data/runbooks/product-catalog-lock-contention.md
      - docs/decisions/0002-raise-product-catalog-memory-ceiling.md
      - "flagd: productCatalogFailure, productCatalogLockContention"
  - id: les-lock-contention-shows-as-queueing-not-cpu
    status: inferred
    statement: >-
      Under lock contention, latency rises while CPU stays flat, because the time is
      spent waiting rather than working. A latency rise with busy CPU is a different
      fault.
    service: product-catalog
    preconditions:
      service: product-catalog
      symptom: latency rises sharply without hard errors
    evidence_refs:
      - data/runbooks/product-catalog-lock-contention.md
---

Authored as seed material for the playbook. The runbook carries `status: structural`: the
fault has not been injected and its telemetry has not been observed. The grounded part is
that `product-catalog` appears twice in the flag document under two unrelated faults, and
that this repository already has a decision record for the other one.

## What happened

`productCatalogLockContention` at `on` makes catalog requests slow while they continue to
succeed. The service stays up and the port stays open, which is precisely the opposite of
inc-2026-10-27-01, where the service had garbage-collected itself into a standstill under
a soft memory ceiling and nothing was listening at all.

## Why it reads as the GC incident

The playbook already holds a product-catalog lesson from ADR-0002, and the service name
matches. A lesson selected on service alone would have offered the memory ceiling as the
first read, which is wrong here and costs a diagnosis cycle.

## Why one lesson is approved and one is not

The flag-pair lesson rests on the flag document, which is readable now. The
latency-without-CPU signature is a claim about telemetry nobody has looked at yet, so it
is `inferred` and the store will not serve it. Promote it when the fault has been run and
the CPU series checked — and not before.
