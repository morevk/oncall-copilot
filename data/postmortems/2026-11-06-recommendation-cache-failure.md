---
kind: postmortem
incident_id: inc-2026-11-06-01
title: recommendation degraded behind the health-check patch that ADR-0001 installed
service: recommendation
symptom: recommendation service degrades as its cache fails
severity: sev3
lessons:
  - id: les-the-healthcheck-patch-hides-cache-degradation
    status: approved
    statement: >-
      The recommendation health check in this repository is overridden by ADR-0001, so it
      reports healthy on grounds that have nothing to do with the cache. A healthy
      recommendation container is therefore not evidence that recommendation is serving
      well; read the cache behaviour directly rather than trusting the probe.
    service: recommendation
    preconditions:
      service: recommendation
      symptom: cache
    evidence_refs:
      - data/runbooks/recommendation-cache-failure.md
      - docs/decisions/0001-patch-recommendation-healthcheck.md
      - "flagd: recommendationCacheFailure variants [off, on]"
---

Authored as seed material for the playbook. The runbook carries `status: structural`, so
no live run backs the degradation signature. What is grounded is the interaction: this
repository patched the recommendation health check in ADR-0001, and
`data/runbooks/recommendation-cache-failure.md` lists that patch among its own
preconditions.

## What happened

`recommendationCacheFailure` at `on` degrades recommendation while the container keeps
reporting healthy, because the health check that ADR-0001 installed tests liveness rather
than cache health.

## Why it reads as a non-incident

inc-2026-10-26-01 taught this repository to distrust the recommendation health check when
it reports UNHEALTHY. This incident is the mirror image: the same patched check reports
HEALTHY while the service is degraded. A fix for one failure mode became a blind spot for
the other, which is the ordinary fate of a health check written to make a dependency
start.

## Why this lesson is approved

It rests on two facts already in the repository — the ADR and the runbook's own stated
precondition — and not on telemetry nobody has watched. Its precondition names `cache`
rather than `unhealthy`, so it will not be offered on the inc-2026-10-26-01 alert, where
it would be wrong.
