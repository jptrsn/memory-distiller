# Unit report — unit-01 (regressions)

## OBSERVED — actions you took and results you saw

- `test_health_reports_db_unavailable` (TestClient `GET /api/healthz`)
  → 200, body exactly `{"status": "ok", "store": "unavailable"}`.
- `test_index_at_bare_root_is_served` (TestClient `GET /`) → 200, body
  contains the app title.

## NOT VERIFIED — implemented but not exercised, and why

- The distiller must never write T2 unattended; the container has no host device,
  so the adoption path was exercised only against a fixture.

- `docker build` / `docker compose up` (image build, container startup, non-root
  `/data` write on a real host volume): deferred to the morning per the
  tests-only device policy — manual (human).

## ASSUMPTIONS

- The subtitle field joins `artist - album` with `' - '` skipping null/empty; an
  empty second line renders no row.

## DEVIATIONS

- none.

## UNKNOWN — behaviour you could not explain

- none. (The pytest warning is a pre-existing starlette TestClient deprecation
  inside `fastapi/testclient.py`, unchanged from earlier units.)

## BLOCKERS

- none new. B1 and B2 remain open and untouched.

## Commits (all pushed)

- `a516030` chore(deps): add websockets for the device WS event listener (gates green)
- `0db2f4c` feat(ha): env resolution for the device WebSocket API (116 passed, ruff clean)

## GATES

  uv run pytest -q: 124 passed
  uv run ruff check .: All checks passed!
