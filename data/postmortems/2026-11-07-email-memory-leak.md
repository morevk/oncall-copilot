---
kind: postmortem
incident_id: inc-2026-11-07-01
title: email memory climbed for an hour before anything paged
service: email
symptom: email service memory grows steadily until it fails
severity: sev2
lessons:
  - id: les-a-multiplier-variant-is-not-a-boolean
    status: approved
    statement: >-
      emailMemoryLeak is configured as a multiplier, not a switch: 1x is enabled and
      almost harmless, 10000x exhausts memory quickly. Reporting this flag as on or off
      loses the only part that predicts the timeline, so read and report the variant
      itself before estimating how long there is.
    service: email
    preconditions:
      service: email
      symptom: memory
    evidence_refs:
      - data/runbooks/email-memory-leak.md
      - "flagd: emailMemoryLeak variants [off, 1x, 10x, 100x, 1000x, 10000x]"
  - id: les-a-leak-is-a-slope-not-a-level
    status: inferred
    statement: >-
      A leak is a rising slope, not a high level. A single memory reading cannot
      distinguish a service that has leaked for an hour from one that is simply sized
      large, so read the trend over the incident window rather than the current value.
    service: email
    preconditions:
      service: email
      symptom: memory grows steadily
    evidence_refs:
      - data/runbooks/email-memory-leak.md
---

Authored as seed material for the playbook. The runbook carries `status: structural`. The
grounded fact is the variant list: `emailMemoryLeak` has five non-off variants spanning
four orders of magnitude, which is in `deploy/flagd/demo.flagd.json`.

## What happened

With `emailMemoryLeak` at a non-off multiplier, email memory grows until the service
fails. At `1x` the growth is slow enough that nothing pages for a long time; at `10000x`
the same fault is an outage within minutes. The flag is one flag and the incident is
effectively several.

## Why a boolean read is the trap

Every other fault in this stack that matters to the demo is reported as on or off, and
`set_flag` takes a variant precisely because that is not general. A report of
"emailMemoryLeak is on" is true and useless: it does not say whether there is an hour or
a minute.

## Why one lesson is approved and one is not

The multiplier lesson is a flag-document fact. The slope-versus-level lesson is sound
reasoning about a metric series this repository has never plotted, so it stays `inferred`
and the store will not serve it until someone runs the fault and reads the series.
