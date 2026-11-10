---
kind: postmortem
incident_id: inc-2026-11-09-01
title: checkout stalled on a shipping quote and the delay was exactly as configured
service: shipping
symptom: international shipping quotes are slow, checkout stalls
severity: sev2
lessons:
  - id: les-the-delay-variant-names-the-duration
    status: approved
    statement: >-
      intlShippingSlowdown is configured as a duration — 5sec or 10sec — so the injected
      delay is a number the flag already tells you. If the observed shipping span does
      not match the configured variant, the slowdown has another cause and this flag is
      a coincidence, not the fault.
    service: shipping
    preconditions:
      service: shipping
      symptom: shipping quotes are slow
    evidence_refs:
      - data/runbooks/intl-shipping-slowdown.md
      - "flagd: intlShippingSlowdown variants [off, 5sec, 10sec]"
---

Authored as seed material for the playbook. The runbook carries `status: structural`, so
no observed span durations back this up. The grounded fact is that the variants are
durations, which makes this the one fault family in the document where the flag predicts
a measurable number.

## What happened

`intlShippingSlowdown` at `5sec` or `10sec` delays international shipping quotes.
Checkout waits on the quote, so checkout stalls and the page names checkout rather than
shipping.

## Why it reads as a checkout fault

The user-visible symptom is at checkout, and this stack already has a checkout error-rate
alert that fires for a payment fault. A slow checkout and a failing checkout arrive by
different routes and the alert text does not always distinguish them.

## Why this lesson is approved, and what makes it unusual

Most lessons here say where to look. This one states a falsifiable prediction: the span
duration should equal the configured variant, and if it does not, the hypothesis is
wrong. That is the cheapest kind of lesson to act on and the only kind that can refute
itself, which is why it is worth approving on a flag read alone — a wrong conclusion gets
caught by the very check the lesson asks for.
