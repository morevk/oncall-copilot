---
kind: postmortem
incident_id: inc-2026-11-08-01
title: accounting and fraud-detection both paged, and neither was broken
service: kafka
symptom: order events back up; accounting and fraud detection fall behind
severity: sev2
lessons:
  - id: les-minimal-mode-makes-this-fault-impossible
    status: approved
    statement: >-
      kafka exists only in MODE=full; minimal mode omits compose.full.yaml and with it
      kafka, accounting and fraud-detection. A kafka hypothesis against a minimal stack
      is not a weak hypothesis, it is an impossible one, so read the mode before
      spending a diagnosis cycle on the queue.
    service: kafka
    preconditions:
      service: kafka
      symptom: order events back up
    evidence_refs:
      - data/runbooks/kafka-queue-problems.md
      - "deploy/Makefile: MODE=minimal drops the kafkaQueueProblems fault family"
      - "flagd: kafkaQueueProblems variants [off, on]"
  - id: les-a-shared-bus-pages-as-its-consumers
    status: inferred
    statement: >-
      When a message bus backs up, the services that page are its consumers, not the bus.
      Two unrelated services falling behind together is stronger evidence for a shared
      dependency than for two coincident faults.
    service: kafka
    preconditions:
      service: kafka
      symptom: accounting and fraud detection fall behind
    evidence_refs:
      - data/runbooks/kafka-queue-problems.md
---

Authored as seed material for the playbook. The runbook carries `status: structural`. Two
things are grounded: the flag exists in `deploy/flagd/demo.flagd.json`, and
`deploy/Makefile` states at line 17 that minimal mode removes this fault family and two
diagnosable services with it.

## What happened

`kafkaQueueProblems` at `on` backs up order events. `accounting` and `fraud-detection`
consume those events, so both fall behind and both look unhealthy. Neither has a fault of
its own.

## Why it reads as two incidents

The pages name the consumers. A diagnosis that takes each page at face value opens two
investigations into services that are working correctly, which is the multi-service
confusion this fault family exists to produce.

## Why the mode lesson is approved

It is the cheapest possible check and it is a configuration fact, verifiable from
`deploy/Makefile` without running anything. It also guards a real failure mode of this
project rather than of the shop: development on minimal is explicitly sanctioned, so an
agent reasoning about kafka on a developer's laptop is reasoning about a service that is
not there. The consumer-fan-out lesson is the better diagnostic insight and is
`inferred`, because it describes a correlation nobody here has yet watched happen.
