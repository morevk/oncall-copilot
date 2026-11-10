---
kind: postmortem
incident_id: inc-2026-11-02-01
title: checkout error rate rose and payment transactions stopped
service: payment
symptom: checkout error rate above 5% and payment transactions stop
severity: sev1
lessons:
  - id: les-read-the-flag-baseline-first
    status: approved
    statement: >-
      When checkout returns 422 and payment transactions fall to zero together, read the
      flagd baseline before suspecting a deployment. The two signals move together for a
      flag change and separately for a deploy, and the flag file is one read away.
    service: payment
    preconditions:
      service: payment
      symptom: checkout error rate
    evidence_refs:
      - data/runbooks/payment-failure.md
      - eval/faults/manifest.yaml
  - id: les-confirm-the-variant-the-page-names
    status: approved
    statement: >-
      When the page quotes a flagd variant description rather than a metric, the alert text
      already names a candidate fault. Read the flag baseline before spending tool budget on
      traces: one read either confirms the named variant is live or rules it out, and the
      investigation starts from a fact instead of a guess.
    service: payment
    preconditions:
      service: payment
      symptom: payment service charge requests
    evidence_refs:
      - data/runbooks/payment-failure.md
      - deploy/flagd/demo.flagd.json
---

Authored as seed material for the playbook, from the fault family this project arms on
purpose. The signals below are the ones `eval/faults/manifest.yaml` states for
`payment-failure`, not an observed production incident.

## What happened

With `paymentFailure` armed at `100%`, frontend 422s rise and `demo_payment_transactions_total`
stops advancing. The manifest's detect condition is the ratio of 422s to 422s plus payment
transactions, over a five-minute rate, crossing 0.5; recovery is payment transactions
advancing again at all.

## Why the lesson is about reading, not about concluding

The fix is a flag write, and the approval gate exists so a person makes that call. What the
lesson generalises is the cheap read that distinguishes this family from a deploy
regression, which is the part an investigation can get wrong for several expensive minutes.

## Why this incident justifies two lessons

This fault family pages under two different registers, and `precondition_matches` evaluates
one `symptom` string by containment. The downstream page is a metric —
`checkout error rate above 5% for five minutes` — and the page for the fault itself quotes
the flagd variant description, `Fail payment service charge requests n%`. The two share no
substring, so no single `symptom` precondition can cover both, and the alternative of
stating only `service: payment` would also match `Payment service is unavailable`, which is
`paymentUnreachable` and a different fault with a different first read.

So the family gets one lesson per register, each with a precondition that names the text it
applies to. Measured against the committed corpus before this change, the alert the demo
posts kept **no** lesson at all: `les-read-the-flag-baseline-first` carries a checkout
symptom and the headline page does not mention checkout. `docs/memory-policy.md` records the
general form of this, and the durable fix is for `precondition_matches` to express
alternatives, which is P2's file and P1 will not widen the authoring rule ahead of it.
