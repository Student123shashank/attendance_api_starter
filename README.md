# Employee Attendance & Analytics API

Python 3.11+, FastAPI, and MongoDB implementation for the HROne assignment.

## Setup (Windows PowerShell)

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
python sample_seed.py
uvicorn app.main:app --reload --port 8000
```

Set `MONGO_URI` and `MONGO_DB` in `.env`. Keep `.env` private.

Swagger UI: http://127.0.0.1:8000/docs

## Collections

- `employees`
- `attendance_logs`

The API uses `emp_code` as the employee identifier; MongoDB `_id` is internal.

## Important

Review every business rule in the supplied `openapi.yaml` and `DATA_MODEL.md`.
Run edge-case tests, especially concurrent requests, timestamp validation, analytics
rounding, the 92-day trend limit, and explain-plan requirements before submission.
Do not submit `.env`, secrets, or a Dockerfile.

## Known limitations to verify before submission

This starter implementation is a working baseline, not a guarantee of passing the
hidden grader. In particular, verify aggregation explain plans, the trend pipeline's
index usage, all endpoint-specific explain query equivalence, strict request timestamp
validation, and all R1-R10 edge cases against the original contract.
