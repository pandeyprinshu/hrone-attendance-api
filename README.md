# Employee Attendance & Analytics API

FastAPI + MongoDB backend for the HROne engineering assignment. All application code is in `app/main.py`.

## Run

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt

export MONGO_URI="mongodb://localhost:27017"          # e.g. docker run -p 27017:27017 mongo:7
export MONGO_DB="attendance_db"

python sample_seed.py                                 # optional: loads the 9 sample documents
uvicorn app.main:app --port 8000
```

`MONGO_URI` and `MONGO_DB` are read from the environment. A local `.env` is also loaded, but real environment
variables always win. Nothing is hard-coded. MongoDB 6.0 or newer is required (`$setWindowFields`, `$rank`).

All indexes are created at startup and are idempotent, so there are no manual steps. `GET /health` returns 200 only
when MongoDB answers a ping, otherwise 503.

## Status

All endpoints in `openapi.yaml` are implemented: health, employees (create/list), punch-in, punch-out, attendance list,
PATCH regularization with audit history, the four analytics endpoints (monthly, department summary, late leaderboard,
department trend) and `GET /admin/explain/{endpoint}`. Nothing is missing.

## Notes

- Instants are stored as BSON datetimes and exposed as epoch milliseconds; the client uses `tz_aware=True`.
- The analytics endpoints are MongoDB aggregation pipelines. Gap filling (trend), ranking (leaderboard) and the
  cross-collection headcount (summary) are done in the database, not in Python loops.
- Concurrency relies on the database: unique indexes for punch-in and employee creation, a conditional update for
  punch-out, and an optimistic check on PATCH (the loser of two concurrent corrections gets 409).
- Judgment calls: `moving_avg_7d` averages the unrounded daily rates and rounds the result; a punch-out whose
  `punched_at` is before every punch-in returns 404 (no record qualifies).
- There is no automated test suite in this repository.

See `REVIEW.md` (Part A) and `DECISIONS.md` (Part C).
