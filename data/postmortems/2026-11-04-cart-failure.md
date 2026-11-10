---
kind: postmortem
incident_id: inc-2026-11-04-01
title: a tenth of cart additions failed and the incident was nearly closed as flaky clients
service: cart
symptom: adding items to the cart fails for a proportion of sessions
severity: sev1
lessons:
  - id: les-a-proportional-fault-reads-as-flaky-clients
    status: approved
    statement: >-
      A fault configured as a percentage does not fail any single user reliably, so the
      first reports look like client flakiness or user error. Read the flag's variant
      before concluding there is no fault: a non-off variant IS the incident, and at a
      low percentage the only honest signal is the aggregate rate, not any one session.
    service: cart
    preconditions:
      service: cart
      symptom: adding items to the cart fails
    evidence_refs:
      - data/runbooks/cart-failure.md
      - "flagd: cartFailure variants [off, 10%, 25%, 50%, 75%, 90%, 100%]"
---

Authored as seed material for the playbook. The runbook for this fault carries
`status: structural`, so the fault has not been run against the live stack. What is
grounded here is the flag document: `cartFailure` offers six non-off variants, five of
which are partial. The narrative is written from that configuration and from
`data/runbooks/cart-failure.md`, and it is not an observed incident.

## What happened

`cartFailure` at `10%` makes roughly one cart addition in ten fail while `cart` itself
stays up and reports healthy. Most sessions are unaffected. The failures are spread
across users rather than concentrated, which is the shape that invites "cannot
reproduce".

## Why it reads as no fault at all

Every individual check passes. The service is healthy, a manual cart addition succeeds,
and the next one succeeds too. The fault is only visible in aggregate, and the instinct
under a sev1 is to reproduce rather than aggregate.

## Why this lesson is approved

Its discriminating evidence is a flag read — the variant list is in
`deploy/flagd/demo.flagd.json`, which the agent can already fetch, and no unobserved
metric signature is claimed. The generalisation to other proportional faults
(`paymentFailure` carries the same variant set) is deliberately NOT stated here: this
lesson is scoped to cart, because a lesson that claims every service at once is the
failure `docs/memory-policy.md` exists to prevent.
