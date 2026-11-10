---
kind: postmortem
incident_id: inc-2026-11-03-01
title: checkout failed with no payment spans in the trace at all
service: payment
symptom: checkout fails and the payment service is unavailable
severity: sev1
lessons:
  - id: les-absent-spans-are-not-failing-spans
    status: approved
    statement: >-
      When checkout fails, read whether payment spans are ABSENT from the trace or
      PRESENT and erroring before reading any error rate. Absent spans mean the caller
      never reached payment, which is a reachability fault; present-and-erroring spans
      mean the charge path ran and refused. The two have different flags and the error
      rate looks similar in both.
    service: payment
    preconditions:
      service: payment
      symptom: payment service is unavailable
    evidence_refs:
      - data/runbooks/payment-unreachable.md
      - docs/p2-demo-objectives-definition-of-done.md
      - "flagd: paymentUnreachable variants [off, on]"
  - id: les-payment-errors-mean-the-charge-path
    status: rejected
    statement: >-
      When checkout error rate rises and payment is implicated, suspect the charge
      request path.
    service: payment
    reason: >-
      True on inc-2026-11-02-01 and wrong here, which is the whole point of keeping it.
      Both incidents are the payment service under a flag, and this lesson sends the
      investigation to demo_payment_transactions_total and the charge handler in a case
      where payment is never invoked. The precondition that separates them is span
      presence, not service or symptom, so it cannot be stated in a form
      precondition_matches would check. Refused as a counterexample rather than deleted.
    evidence_refs:
      - data/runbooks/payment-unreachable.md
      - data/runbooks/payment-failure.md
---

Authored as seed material for the playbook. The fault is one of the two in
`eval/faults/manifest.yaml` whose runbook carries `status: verified` — paymentUnreachable
has been smoke-tested live against the Charge pair, recorded in
`docs/p2-demo-objectives-definition-of-done.md`. The telemetry signature below is
therefore observed, not inferred.

## What happened

With `paymentUnreachable` set to `on`, checkout fails for every session. The frontend
reports a failure that reads like the payment outage of inc-2026-11-02-01, and the
checkout error rate climbs the same way. The difference is in the trace: payment spans do
not appear under the checkout span at all, because the call never lands.

## Why it reads as the other payment fault

Two flags in the same document — `paymentFailure` and `paymentUnreachable` — produce a
failing checkout against the same service, and the alert text for both names payment.
Error-rate telemetry does not separate them. Span presence does, and it is a different
query from the one the error-rate alert invites.

## Why this lesson is approved

Its discriminating evidence is a trace-shape read that was actually observed during the
live smoke, and its precondition quotes the alert this fault pages under, so the filter
can evaluate it. The lesson it displaces is kept as a refusal.
