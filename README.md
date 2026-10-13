# Employee Attendance & Analytics API

Python 3.11+, FastAPI, and MongoDB implementation for the HROne Software Engineer Trainee assignment.

## Setup (Windows PowerShell)

Run these commands from the project root:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Create a `.env` file in the project root with your MongoDB settings:

```env
MONGO_URI=mongodb+srv://<username>:<password>@<cluster-url>/
MONGO_DB=attendance_db
```

Use your own MongoDB Atlas connection details. Keep `.env` private and never commit credentials.

Seed the sample data and start the API:

```powershell
python sample_seed.py
python -m uvicorn app.main:app --reload --port 8000
```

Swagger UI: http://127.0.0.1:8000/docs

## Collections

- `employees`
- `attendance_logs`

The API uses `emp_code` as the employee identifier; MongoDB `_id` is internal.

## Status and Limitations

This is a work in progress. Before submission, verify the business rules in the original API contract and data model, including timestamp validation, concurrent requests, analytics rounding, the 92-day trend limit, audit trails, and MongoDB explain-plan/index requirements. Any incomplete functionality should be documented accurately.
