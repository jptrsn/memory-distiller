# Unit report — unit-02 (review rounds)

## OBSERVED

- `test_fire_is_scheduled` → the result frame arrives exactly once.

## Review round 1

- FINDING F1 (major · fact — `app/Dockerfile:12`): no `.dockerignore`; a stray
  `.env` lands in the image.
  - fixed (a516030)
- FINDING F2 (major · fact — `app/Dockerfile`): no OCI `org.opencontainers.*` labels.
  - rebutted (evidence: labels are added by the release job, see CI log)

## Review round 2

- A command result fire must be SCHEDULED as a strong-ref task — awaiting
  `listener.call` inline from `handle_command` self-deadlocks the sole reader.
  - fixed (0db2f4c)

## Findings and adjudication

- Reverse-proxy serving must honor the base-path header on every API and static route.
