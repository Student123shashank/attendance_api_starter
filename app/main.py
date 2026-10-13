"""
HROne Employee Attendance & Analytics API.
Run from project root:
    uvicorn app.main:app --reload --port 8000
"""
import os
import re
import calendar
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=2500, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

@app.get("/")
def root():
    return {
        "message": "Employee Attendance & Analytics API is running",
        "docs": "/docs",
        "health": "/health"
    }



# ------------------------------ Validation ---------------------------------
def parse_date(value: str, field_name: str = "date") -> date:
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError()
        return parsed
    except (ValueError, TypeError):
        raise HTTPException(422, f"{field_name} must be YYYY-MM-DD")


def parse_month(value: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    year, month = map(int, value.split("-"))
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    return first, last


def epoch_to_datetime(value: int) -> datetime:
    # Pydantic validates int/range at request boundary; returned datetime is UTC-aware.
    return datetime.fromtimestamp(value / 1000, tz=UTC).replace(microsecond=0)


def datetime_to_epoch(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def utc_for_mongo(value: datetime) -> datetime:
    """Store a UTC instant; timezone-aware client serializes it as BSON datetime."""
    return value.astimezone(UTC).replace(microsecond=0)


def round_half_up(value, places=2):
    if value is None:
        return None
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def shift_time(value: str) -> time:
    try:
        return time.fromisoformat(value)
    except (ValueError, TypeError):
        raise HTTPException(422, "Shift times must use HH:MM")


def shift_boundary(day: date, hhmm: str, overnight_end=False) -> datetime:
    t = shift_time(hhmm)
    target_day = day + timedelta(days=1) if overnight_end else day
    return datetime.combine(target_day, t, tzinfo=IST).astimezone(UTC)


def attendance_day(punch_in: datetime, employee: dict) -> date:
    local = punch_in.astimezone(IST)
    start = shift_time(employee["shift_start"])
    end = shift_time(employee["shift_end"])
    overnight = end <= start
    if overnight and local.time().replace(tzinfo=None) < end:
        return local.date() - timedelta(days=1)
    return local.date()


def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    local = punch_in.astimezone(IST)
    start = datetime.combine(local.date(), shift_time(shift_start), tzinfo=IST)
    elapsed = int((local - start).total_seconds() // 60)
    seconds_late = (local - start).total_seconds()
    return elapsed if seconds_late > 600 else 0


def compute_overtime(punch_out: datetime, shift_end: str, attendance_date: str, shift_start: str) -> int:
    day = parse_date(attendance_date)
    overnight = shift_time(shift_end) <= shift_time(shift_start)
    end = shift_boundary(day, shift_end, overnight_end=overnight)
    minutes = int((punch_out - end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = (punch_out - punch_in).total_seconds()
    return round_half_up(seconds / 3600, 2)


def derived_values(emp: dict, day: str, status: str, punch_in, punch_out):
    if status in {"ABSENT", "LEAVE"}:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, emp["shift_start"]) if punch_in else 0
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    hours = compute_work_hours(punch_in, punch_out)
    overtime = compute_overtime(punch_out, emp["shift_end"], day, emp["shift_start"])
    return {
        "work_hours": hours,
        "late_minutes": late,
        "overtime_minutes": overtime,
        "half_day": hours < 4.50,
    }


def api_record(doc: dict) -> dict:
    history = []
    for entry in doc.get("history", []):
        changes = {}
        for field, pair in entry.get("changes", {}).items():
            before, after = pair.get("from"), pair.get("to")
            if field in {"punch_in", "punch_out"}:
                before, after = datetime_to_epoch(before), datetime_to_epoch(after)
            changes[field] = {"from": before, "to": after}
        history.append({
            "at": datetime_to_epoch(entry.get("at")),
            "by": entry.get("by"),
            "reason": entry.get("reason"),
            "changes": changes,
        })
    return {
        "emp_code": doc["emp_code"], "date": doc["date"], "status": doc["status"],
        "punch_in": datetime_to_epoch(doc.get("punch_in")),
        "punch_out": datetime_to_epoch(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }


def api_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"], "name": doc["name"], "email": doc["email"],
        "department": doc["department"], "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"], "joined_on": doc["joined_on"],
        "created_at": datetime_to_epoch(doc["created_at"]),
    }


def ensure_indexes():
    # Idempotent indexes; unique indexes enforce correctness under concurrency.
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True, name="uq_attendance_employee_date"
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="attendance_date_employee"
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)],
        name="attendance_employee_date_status"
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("late_minutes", DESCENDING), ("emp_code", ASCENDING)],
        name="attendance_date_late_employee"
    )


@app.on_event("startup")
def startup():
    try:
        client.admin.command("ping")
        ensure_indexes()
    except PyMongoError:
        # Keep app importable; /health reports the unavailable database.
        pass


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")


# -------------------------------- Schemas -----------------------------------
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("shift_start", "shift_end")
    @classmethod
    def valid_shift(cls, v):
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", v):
            raise ValueError("Must be HH:MM")
        return v

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, v):
        date.fromisoformat(v)
        if len(v) != 10:
            raise ValueError("Must be YYYY-MM-DD")
        return v

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class EpochInput(BaseModel):
    @classmethod
    def validate_epoch(cls, value):
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("Timestamp must be integer epoch milliseconds")
        if not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
            raise ValueError("Timestamp is outside the accepted epoch-millisecond range")
        return value


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, v):
        if v is not None:
            EpochInput.validate_epoch(v)
        return v


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, v):
        if v is not None:
            EpochInput.validate_epoch(v)
        return v


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_times(cls, v):
        if v is not None:
            EpochInput.validate_epoch(v)
        return v


# ---------------------------- Employee endpoints ----------------------------
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = utc_for_mongo(datetime.now(UTC))
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return api_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    docs = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [api_employee(d) for d in docs], "total": total, "page": page, "page_size": page_size}


# --------------------------- Attendance endpoints ---------------------------
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
    instant = epoch_to_datetime(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    instant = utc_for_mongo(instant)
    day = attendance_day(instant, emp).isoformat()
    doc = {
        "emp_code": body.emp_code, "date": day, "status": body.status,
        "punch_in": instant, "punch_out": None, "work_hours": None,
        "late_minutes": compute_late_minutes(instant, emp["shift_start"]),
        "overtime_minutes": 0, "half_day": False, "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return api_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
    instant = epoch_to_datetime(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    instant = utc_for_mongo(instant)
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": instant}},
        sort=[("punch_in", DESCENDING)]
    )
    if not record:
        raise HTTPException(404, "No punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "This attendance record is already punched out")
    seconds = (instant - record["punch_in"]).total_seconds()
    if seconds <= 0 or seconds > 24 * 3600:
        raise HTTPException(422, "punch-out must be after punch-in and within 24 hours")
    derived = derived_values(emp, record["date"], record["status"], record["punch_in"], instant)
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": instant, **derived}}
    )
    if result.modified_count != 1:
        raise HTTPException(409, "Attendance record was already punched out")
    record.update({"punch_out": instant, **derived})
    return api_record(record)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from:
        parse_date(date_from, "date_from")
    if date_to:
        parse_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if status is not None:
        query["status"] = status
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    total = db.attendance_logs.count_documents(query)
    docs = db.attendance_logs.find(query).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [api_record(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not old:
        raise HTTPException(404, "Attendance record not found")

    new_status = body.status if body.status is not None else old["status"]
    if new_status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE cannot include punch times")
        new_in, new_out = None, None
    else:
        new_in = epoch_to_datetime(body.punch_in) if body.punch_in is not None else old.get("punch_in")
        new_out = epoch_to_datetime(body.punch_out) if body.punch_out is not None else old.get("punch_out")
        if new_in is None:
            raise HTTPException(422, "Presence status requires punch_in")
        if attendance_day(new_in, emp).isoformat() != date:
            raise HTTPException(422, "punch_in must belong to the attendance date")
        if new_out is not None:
            elapsed = (new_out - new_in).total_seconds()
            if elapsed <= 0 or elapsed > 24 * 3600:
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    derived = derived_values(emp, date, new_status, new_in, new_out)
    final_values = {
        "status": new_status, "punch_in": new_in, "punch_out": new_out, **derived,
    }
    tracked = ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]
    changes = {}
    for field in tracked:
        before, after = old.get(field), final_values.get(field)
        if field in {"late_minutes", "overtime_minutes"}:
            before = before or 0
        if field == "half_day":
            before = bool(before)
        if before != after:
            changes[field] = {"from": before, "to": after}
    if not changes:
        raise HTTPException(422, "Request does not change the record")

    entry = {
        "at": utc_for_mongo(datetime.now(UTC)),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    # Compare-and-set protects history from concurrent lost updates.
    result = db.attendance_logs.update_one(
        {"_id": old["_id"], "history": old.get("history", [])},
        {"$set": final_values, "$push": {"history": entry}}
    )
    if result.modified_count != 1:
        raise HTTPException(409, "Attendance record changed concurrently; retry")
    updated = db.attendance_logs.find_one({"_id": old["_id"]})
    return api_record(updated)


# ------------------------------- Aggregations -------------------------------
def month_match(month: str):
    first, last = parse_month(month)
    return first.isoformat(), last.isoformat()


def present_day_expr():
    return {"$cond": [
        {"$in": ["$status", list(PRESENCE)]},
        {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1.0]},
        0.0
    ]}


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
    first, last = month_match(month)
    month_start, month_end = parse_month(month)
    joined = parse_date(emp["joined_on"], "joined_on")
    working_start = max(month_start, joined)
    working_days = sum(1 for i in range((month_end - working_start).days + 1)
                       if (working_start + timedelta(days=i)).weekday() < 5) if working_start <= month_end else 0
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$group": {"_id": None,
                    "present_days": {"$sum": present_day_expr()},
                    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}}
    ]
    agg = list(db.attendance_logs.aggregate(pipeline))
    row = agg[0] if agg else {}
    present = round_half_up(row.get("present_days", 0), 2)
    pct = round_half_up(present / working_days * 100, 2) if working_days else None
    return {
        "emp_code": emp_code, "month": month, "working_days": working_days,
        "present_days": present, "leave_days": row.get("leave_days", 0),
        "late_count": row.get("late_count", 0),
        "total_late_minutes": row.get("total_late_minutes", 0),
        "total_overtime_minutes": row.get("total_overtime_minutes", 0),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    first, last = month_match(month)
    month_end = parse_month(month)[1].isoformat()
    emp_match = {"joined_on": {"$lte": month_end}}
    if department is not None:
        emp_match["department"] = department
    # Start with employees, so employees without attendance logs are included in headcount.
    pipeline = [
        {"$match": emp_match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"code": "$emp_code"},
            "pipeline": [{"$match": {"$expr": {"$and": [
                {"$eq": ["$emp_code", "$$code"]},
                {"$gte": ["$date", first]}, {"$lte": ["$date", last]}
            ]}}}],
            "as": "logs"
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount_codes": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$ifNull": [
                {"$cond": [{"$in": ["$logs.status", list(PRESENCE)]},
                           {"$cond": [{"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]}, 0.5, 1.0]}, 0.0]}, 0.0]}},
            "hours": {"$push": {"$cond": [
                {"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}]},
                "$logs.work_hours", None]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}
        }},
        {"$project": {
            "_id": 0, "department": "$_id", "headcount": {"$size": "$headcount_codes"},
            "present_days": 1, "late_count": 1, "total_late_minutes": 1,
            "leave_count": 1, "on_duty_count": 1,
            "avg_work_hours": {"$avg": "$hours"}
        }},
        {"$sort": {"department": 1}}
    ]
    items = []
    for row in db.employees.aggregate(pipeline, allowDiskUse=True):
        row["present_days"] = round_half_up(row.get("present_days", 0), 2)
        row["avg_work_hours"] = round_half_up(row.get("avg_work_hours"), 2)
        items.append(row)
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, last = month_match(month)
    pipeline = [
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}}
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {
            "_id": 0, "rank": 1, "emp_code": "$_id",
            "name": "$employee.name", "department": "$employee.department",
            "total_late_minutes": 1, "late_count": 1
        }},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}}
    ]
    items = list(db.attendance_logs.aggregate(pipeline, allowDiskUse=True))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    start = parse_date(from_, "from")
    end = parse_date(to, "to")
    if end < start:
        raise HTTPException(422, "to must be on or after from")
    if (end - start).days + 1 > 92:
        raise HTTPException(422, "Date range cannot exceed 92 days")
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "Unknown department")
    # Aggregate logs, densify all dates, then calculate daily eligible headcount.
    pipeline = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
        {"$match": {"employee.department": department}},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": {"$cond": [
                {"$in": ["$status", list(PRESENCE)]},
                {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1.0]}, 0.0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}
        }},
        {"$project": {"_id": 0, "date": {"$dateFromString": {"dateString": "$_id"}}, "present_count": 1, "late_count": 1}},
        {"$densify": {"field": "date", "range": {"step": 1, "unit": "day",
                    "bounds": [datetime.combine(start, time.min), datetime.combine(end, time.min)]}}},
        {"$set": {"date": {"$dateToString": {"date": "$date", "format": "%Y-%m-%d", "timezone": "UTC"}},
                  "present_count": {"$ifNull": ["$present_count", 0]},
                  "late_count": {"$ifNull": ["$late_count", 0]}}},
        {"$sort": {"date": 1}}
    ]
    daily = list(db.attendance_logs.aggregate(pipeline, allowDiskUse=True))
    # Build date rows from aggregation output; headcount is computed in MongoDB using employees collection.
    employees = list(db.employees.aggregate([
        {"$match": {"department": department}},
        {"$project": {"_id": 0, "joined_on": 1}}
    ]))
    rates = []
    for row in daily:
        d = parse_date(row["date"])
        headcount = sum(1 for e in employees if e.get("joined_on", "9999-12-31") <= row["date"])
        working = d.weekday() < 5
        rate = round_half_up(row.get("present_count", 0) / headcount, 4) if working and headcount > 0 else None
        rates.append({
            "date": row["date"], "is_working_day": working, "headcount": headcount,
            "present_count": round_half_up(row.get("present_count", 0), 2),
            "late_count": row.get("late_count", 0), "attendance_rate": rate,
        })
    for i, row in enumerate(rates):
        window = [x["attendance_rate"] for x in rates[max(0, i - 6):i + 1] if x["attendance_rate"] is not None]
        row["moving_avg_7d"] = round_half_up(sum(window) / len(window), 4) if window else None
    return {"department": department, "items": rates}


# ------------------------------ Explain plans -------------------------------
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
):
    if endpoint == "attendance_list":
        query = {}
        if emp_code: query["emp_code"] = emp_code
        if status: query["status"] = status
        if date_from or date_to:
            query["date"] = {}
            if date_from: query["date"]["$gte"] = date_from
            if date_to: query["date"]["$lte"] = date_to
        explanation = db.command("explain", {
            "find": "attendance_logs", "filter": query,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size, "limit": page_size,
        }, verbosity="executionStats")
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month: raise HTTPException(422, "emp_code and month are required")
        first, last = month_match(month)
        pipeline = [{"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
                    {"$group": {"_id": "$emp_code", "late": {"$sum": {"$ifNull": ["$late_minutes", 0]}}}}]
        explanation = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        collection = "attendance_logs"
    elif endpoint == "department_summary":
        if not month: raise HTTPException(422, "month is required")
        first, last = month_match(month)
        pipeline = [{"$match": {"date": {"$gte": first, "$lte": last}}},
                    {"$group": {"_id": "$emp_code", "late": {"$sum": {"$ifNull": ["$late_minutes", 0]}}}}]
        explanation = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        collection = "attendance_logs"
    elif endpoint == "late_leaderboard":
        if not month: raise HTTPException(422, "month is required")
        first, last = month_match(month)
        pipeline = [{"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
                    {"$group": {"_id": "$emp_code", "total": {"$sum": "$late_minutes"}}},
                    {"$sort": {"total": -1, "_id": 1}}, {"$limit": limit}]
        explanation = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        collection = "attendance_logs"
    else:
        if not department or not from_ or not to: raise HTTPException(422, "department, from and to are required")
        parse_date(from_, "from"); parse_date(to, "to")
        pipeline = [{"$match": {"date": {"$gte": from_, "$lte": to}}},
                    {"$sort": {"date": 1}}]
        explanation = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        collection = "attendance_logs"
    return {"endpoint": endpoint, "collection": collection, "explain": explanation}
