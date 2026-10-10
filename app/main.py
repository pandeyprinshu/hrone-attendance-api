"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
"""
Employee Attendance & Analytics API - Completed Implementation

Run:  uvicorn app.main:app --port 8000 --reload
Env:  MONGO_URI, MONGO_DB (loaded from environment or .env)
"""
"""
Employee Attendance & Analytics API - Completed Implementation

Run:  uvicorn app.main:app --port 8000 --reload
Env:  MONGO_URI, MONGO_DB (loaded from environment or .env; real env vars win)
"""
import calendar
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date as date_cls, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Any, Dict, List, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

log = logging.getLogger("attendance")

# Load environment variables (.env file if present; real env vars win)
load_dotenv()

# MongoDB connection configuration
MONGO_URI = os.getenv("MONGO_URI") or os.getenv("MONGODB_URL") or "mongodb://localhost:27017"
MONGO_DB = os.getenv("MONGO_DB") or os.getenv("DATABASE_NAME") or "attendance_db"

# tz_aware=True -> reads come back UTC-aware, not naive (fixes the +5.5h work-hours bug)
client = MongoClient(MONGO_URI, tz_aware=True, tzinfo=timezone.utc, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]

# Timezone standard definition (IST UTC+5:30)
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
# Strict int in ms range: rejects floats, strings and second-style values (422)
EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"
HHMM_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


def create_indexes() -> None:
    """Runs at startup; creating an identical index again is a no-op."""
    try:
        db.employees.create_index([("emp_code", ASCENDING)], unique=True)  # unique code + sort
        db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])  # list by dept
        db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])  # headcount/trend
        db.employees.create_index([("joined_on", ASCENDING)])  # headcount, all depts
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)  # 1/day
        db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])  # list sort, months
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])  # punch-out
    except Exception:  # keep app up so /health can answer 503
        log.exception("index creation failed")


@asynccontextmanager
async def lifespan(_: FastAPI):
    create_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    description="Attendance tracking, audit logs, and analytics with MongoDB aggregation pipelines",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helper Functions
# --------------------------------------------------------------------------- #

def get_current_ist_time() -> datetime:
    """Returns current localized IST datetime, truncated to whole seconds (R1)."""
    return datetime.now(IST).replace(microsecond=0)


def parse_timestamp(punched_at: Optional[int]) -> datetime:
    """
    Converts epoch milliseconds (already validated) into an IST datetime,
    truncated to whole seconds. Defaults to current IST time if None.
    """
    if punched_at is None:
        return get_current_ist_time()
    return (EPOCH + timedelta(seconds=punched_at // 1000)).astimezone(IST)


def aware(dt: datetime) -> datetime:
    """Naive datetimes from Mongo are UTC."""
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def to_ms(dt: datetime) -> int:
    """datetime -> epoch milliseconds."""
    d = aware(dt) - EPOCH
    return (d.days * 86400 + d.seconds) * 1000 + d.microseconds // 1000


def hhmm(s: str):
    h, m = s.split(":")
    return int(h), int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return hhmm(shift_end) <= hhmm(shift_start)


def get_shifts(emp: dict):
    return emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30")


def compute_attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST date of punch-in; overnight punch-ins before shift_end belong to the previous day."""
    ist = punch_in.astimezone(IST)
    d = ist.date()
    if is_overnight(shift_start, shift_end) and (ist.hour, ist.minute, ist.second) < (*hhmm(shift_end), 0):
        d -= timedelta(days=1)
    return d.isoformat()


def shift_bounds(date_str: str, shift_start: str, shift_end: str):
    """Shift start/end as IST datetimes for an attendance date (overnight ends next day)."""
    d = date_cls.fromisoformat(date_str)
    sh, sm = hhmm(shift_start)
    eh, em = hhmm(shift_end)
    start = datetime(d.year, d.month, d.day, sh, sm, tzinfo=IST)
    end = datetime(d.year, d.month, d.day, eh, em, tzinfo=IST)
    if is_overnight(shift_start, shift_end):
        end += timedelta(days=1)
    return start, end


def round_half_up(value: Any, places: int = 2) -> float:
    """R8: half-up rounding via Decimal (not float / banker's rounding)."""
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return float(d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def compute_late_minutes(punch_in: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """
    R2: late only if strictly more than 10:00 after shift start.
    Minutes (floor) are counted from shift start, not from the end of grace.
    """
    start, _ = shift_bounds(date_str, shift_start, shift_end)
    secs = int((punch_in - start).total_seconds())
    return secs // 60 if secs > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals half-up."""
    secs = int((punch_out - punch_in).total_seconds())
    return round_half_up(Decimal(secs) / Decimal(3600), 2)


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    """R3: whole minutes after shift end, counted only if >= 30."""
    _, end = shift_bounds(date_str, shift_start, shift_end)
    mins = int((punch_out - end).total_seconds()) // 60
    return mins if mins >= 30 else 0


def compute_derived_fields(emp: dict, date_str: str, status: str,
                           punch_in: Optional[datetime], punch_out: Optional[datetime]) -> dict:
    """All derived fields in one place (punch-in, punch-out, correction)."""
    shift_start, shift_end = get_shifts(emp)
    if status not in PRESENCE or punch_in is None:
        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    out = {"late_minutes": compute_late_minutes(punch_in, date_str, shift_start, shift_end),
           "work_hours": None, "overtime_minutes": 0, "half_day": False}
    if punch_out is not None:
        wh = compute_work_hours(punch_in, punch_out)
        out.update(work_hours=wh,
                   overtime_minutes=compute_overtime(punch_out, shift_start, shift_end, date_str),
                   half_day=wh < 4.50)  # R5: rounded hours < 4.50
    return out


def raise_422(msg: str):
    raise HTTPException(status_code=422, detail=[{"loc": ["query"], "msg": msg, "type": "value_error"}])


def validate_date(value: str, name: str) -> date_cls:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value or ""):
        raise_422(f"{name} must be YYYY-MM-DD")
    try:
        return date_cls.fromisoformat(value)
    except ValueError:
        raise_422(f"{name} is not a valid date")


def parse_month(month: Optional[str]):
    """Returns (first_day, last_day, year, month_no) for a YYYY-MM string."""
    if not re.fullmatch(MONTH_PATTERN, month or ""):
        raise_422("month must be YYYY-MM")
    y, mo = int(month[:4]), int(month[5:])
    if y < 1:
        raise_422("month year out of range")
    last = calendar.monthrange(y, mo)[1]
    return f"{y:04d}-{mo:02d}-01", f"{y:04d}-{mo:02d}-{last:02d}", y, mo


def _conv(v: Any) -> Any:
    return to_ms(v) if isinstance(v, datetime) else v


def serialize_doc(doc: dict) -> dict:
    """Attendance doc -> API shape: no _id, instants as epoch ms, defaults for legacy gaps."""
    history = []
    for h in doc.get("history") or []:
        history.append({
            "at": _conv(h.get("at")),
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": {k: {"from": _conv(v.get("from")), "to": _conv(v.get("to"))}
                        for k, v in (h.get("changes") or {}).items()},
        })
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": _conv(doc.get("punch_in")),
        "punch_out": _conv(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": history,
    }


def serialize_employee(doc: dict) -> dict:
    """Employee doc -> API shape (no _id, created_at in epoch ms)."""
    return {
        "emp_code": doc["emp_code"], "name": doc["name"], "email": doc["email"],
        "department": doc["department"], "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"], "joined_on": doc["joined_on"],
        "created_at": _conv(doc["created_at"]),
    }


# --------------------------------------------------------------------------- #
# Pydantic Schemas / Models
# --------------------------------------------------------------------------- #

class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM_PATTERN)
    shift_end: str = Field(default="18:30", pattern=HHMM_PATTERN)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _valid_joined_on(cls, v: str) -> str:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            raise ValueError("joined_on must be YYYY-MM-DD")
        date_cls.fromisoformat(v)
        return v

    @model_validator(mode="after")
    def _shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class AttendanceCorrectionIn(BaseModel):
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    status: Optional[Status] = None
    reason: str = Field(..., min_length=5, max_length=200, description="Audit reason for manual change")
    regularized_by: str = Field(..., min_length=1, max_length=50, description="Who made this change")


# --------------------------------------------------------------------------- #
# Core Endpoints: Health & Employees
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(status_code=503, detail="database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = get_current_ist_time()
    try:
        db.employees.insert_one(doc)  # unique index decides parallel duplicates
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None,
                   page: int = Query(1, ge=1),
                   page_size: int = Query(20, ge=1, le=100)):
    q = {}
    if department:
        q["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [serialize_employee(d) for d in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance Endpoints
# --------------------------------------------------------------------------- #

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    ts = parse_timestamp(body.punched_at)
    shift_start, shift_end = get_shifts(emp)
    d = compute_attendance_date(ts, shift_start, shift_end)

    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "history": [],
        **compute_derived_fields(emp, d, body.status, ts, None),
    }
    try:
        # Unique (emp_code, date) index: of parallel requests exactly one insert wins
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")
    return serialize_doc(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    """
    Clock-out endpoint:
    Closes the latest record whose punch_in <= punched_at (works across midnight).
    Calculates work hours, overtime and half day.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    ts = parse_timestamp(body.punched_at)

    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", DESCENDING)]
    )
    if not record:
        raise HTTPException(status_code=404, detail="No punch-in found to punch out from")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Already punched out")

    punch_in_time = aware(record["punch_in"])
    if ts <= punch_in_time or ts - punch_in_time > timedelta(hours=24):
        raise_422("punched_at must be after punch_in and within 24 hours of it")

    derived = compute_derived_fields(emp, record["date"], record["status"], punch_in_time, ts)
    fields = {
        "punch_out": ts,
        "work_hours": derived["work_hours"],
        "overtime_minutes": derived["overtime_minutes"],
        "half_day": derived["half_day"],
    }

    # Filter punch_out: None -> only one of two simultaneous punch-outs can match
    res = db.attendance_logs.update_one({"_id": record["_id"], "punch_out": None}, {"$set": fields})
    if res.matched_count == 0:
        raise HTTPException(status_code=409, detail="Already punched out")
    return serialize_doc({**record, **fields})


@app.patch("/attendance/{emp_code}/{date}", status_code=200)
def correct_attendance(emp_code: str, date: str, body: AttendanceCorrectionIn):
    """
    Manual adjustments endpoint with immutable audit trail.
    Recomputes derived fields and appends one history entry (only changed fields).
    """
    validate_date(date, "date")
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record for this employee and date not found")

    shift_start, shift_end = get_shifts(emp)
    old_in = aware(record["punch_in"]) if record.get("punch_in") else None
    old_out = aware(record["punch_out"]) if record.get("punch_out") else None
    new_status = body.status or record["status"]

    # Determine new values or keep existing
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise_422("punch times cannot be supplied with ABSENT/LEAVE")
        new_in = new_out = None  # these statuses clear punch times
    else:
        new_in = parse_timestamp(body.punch_in) if body.punch_in is not None else old_in
        if new_in is None:
            raise_422("a presence status requires punch_in")
        new_out = parse_timestamp(body.punch_out) if body.punch_out is not None else old_out
        if compute_attendance_date(new_in, shift_start, shift_end) != date:
            raise_422("punch_in must stay on the record's attendance date")
        if new_out is not None and (new_out <= new_in or new_out - new_in > timedelta(hours=24)):
            raise_422("punch_out must be after punch_in and within 24 hours")

    update_fields = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        **compute_derived_fields(emp, date, new_status, new_in, new_out),
    }
    previous = {
        "status": record["status"], "punch_in": old_in, "punch_out": old_out,
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes") or 0,
        "overtime_minutes": record.get("overtime_minutes") or 0,
        "half_day": bool(record.get("half_day", False)),
    }
    # Audit entry: only fields that actually changed
    changes = {k: {"from": previous[k], "to": v} for k, v in update_fields.items() if previous[k] != v}
    if not any(k in changes for k in ("status", "punch_in", "punch_out")):
        raise_422("request changes nothing")

    audit_entry = {
        "at": get_current_ist_time(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic lock: write only if the record is unchanged since we read it, else 409
    history = record.get("history")
    flt = {
        "_id": record["_id"], "status": record["status"],
        "punch_in": record.get("punch_in"), "punch_out": record.get("punch_out"),
        "history": {"$size": len(history)} if isinstance(history, list) else history,
    }
    res = db.attendance_logs.update_one(flt, {"$set": update_fields, "$push": {"history": audit_entry}})
    if res.matched_count == 0:
        raise HTTPException(status_code=409, detail="Record was modified concurrently, retry")

    old_history = history if isinstance(history, list) else []
    return serialize_doc({**record, **update_fields, "history": old_history + [audit_entry]})


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


def build_attendance_query(emp_code, date_from, date_to, status) -> dict:
    """Shared by GET /attendance and its explain."""
    if date_from:
        validate_date(date_from, "date_from")
    if date_to:
        validate_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise_422("date_from must not be after date_to")
    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q


@app.get("/attendance")
def list_attendance(
        emp_code: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        status: Optional[Status] = None,
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
):
    q = build_attendance_query(emp_code, date_from, date_to, status)

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size

    # Sorting and pagination happen inside MongoDB (index-backed)
    cursor = db.attendance_logs.find(q).sort(ATTENDANCE_SORT).skip(skip).limit(page_size)
    items = [serialize_doc(d) for d in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Analytics Endpoints (MongoDB Aggregation Pipelines)
# --------------------------------------------------------------------------- #

# Reusable expressions: they read STORED derived values, nothing is recomputed
DOW = {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d", "onError": None}}}
HALF = {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}
IS_PRESENT = {"$in": ["$status", PRESENCE]}
PV_WEEKDAY = {"$cond": [{"$and": [IS_PRESENT, {"$in": [DOW, [2, 3, 4, 5, 6]]}]}, HALF, 0]}  # Mon-Fri only
PV_ANY = {"$cond": [IS_PRESENT, HALF, 0]}  # any day of week
LATE1 = {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}


def count_working_days(year: int, month: int, joined_on: str) -> int:
    """R7: Mon-Fri from max(first of month, joined_on) to month end."""
    first = date_cls(year, month, 1)
    last = date_cls(year, month, calendar.monthrange(year, month)[1])
    d, n = max(first, date_cls.fromisoformat(joined_on)), 0
    while d <= last:
        n += d.weekday() < 5
        d += timedelta(days=1)
    return n


def build_monthly_pipeline(emp_code: str, first: str, last: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": PV_WEEKDAY},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": LATE1},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def get_employee_monthly_analytics(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    """
    Returns aggregated monthly attendance metrics for an employee.
    Month format: YYYY-MM (e.g. 2026-10)
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    first, last, year, month_no = parse_month(month)
    res = list(db.attendance_logs.aggregate(build_monthly_pipeline(emp_code, first, last)))
    data = res[0] if res else {}

    working_days = count_working_days(year, month_no, emp["joined_on"])
    present = Decimal(str(data.get("present_days", 0)))

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present, 2),
        "leave_days": int(data.get("leave_days", 0)),
        "late_count": int(data.get("late_count", 0)),
        "total_late_minutes": int(data.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(data.get("total_overtime_minutes", 0)),
        "attendance_pct": round_half_up(present / Decimal(working_days) * 100, 2) if working_days else None,
    }


def build_summary_pipeline(first: str, last: str, department: Optional[str]) -> list:
    # Start from employees so people with zero logs still count (R9)
    emp_match: Dict[str, Any] = {"joined_on": {"$lte": last}}
    if department:
        emp_match["department"] = department
    has_wh = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}
    return [
        {"$match": emp_match},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first, "$lte": last}}},
                {"$addFields": {
                    "_pv": PV_WEEKDAY,
                    "_late": LATE1,
                    "_leave": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]},
                    "_od": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]},
                    "_whn": {"$cond": [has_wh, 1, 0]},
                    # hours in hundredths (integers) -> exact average later
                    "_whc": {"$cond": [has_wh, {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}, 0]},
                }},
            ],
            "as": "logs"
        }},
        {"$addFields": {  # per-employee totals
            "_pd": {"$sum": "$logs._pv"}, "_lc": {"$sum": "$logs._late"},
            "_tl": {"$sum": "$logs.late_minutes"}, "_lv": {"$sum": "$logs._leave"},
            "_od": {"$sum": "$logs._od"}, "_whs": {"$sum": "$logs._whc"}, "_whn": {"$sum": "$logs._whn"},
        }},
        {"$group": {  # per-department totals
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$_pd"},
            "late_count": {"$sum": "$_lc"},
            "total_late_minutes": {"$sum": "$_tl"},
            "leave_count": {"$sum": "$_lv"},
            "on_duty_count": {"$sum": "$_od"},
            "wh_sum": {"$sum": "$_whs"},
            "wh_n": {"$sum": "$_whn"},
        }},
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def get_department_summary(month: str = Query(..., pattern=MONTH_PATTERN), department: Optional[str] = None):
    """
    Calculates department-wise aggregated metrics for a month.
    """
    first, last, _, _ = parse_month(month)
    items = []
    for r in db.employees.aggregate(build_summary_pipeline(first, last, department)):
        n = int(r["wh_n"])
        items.append({
            "department": r["_id"],
            "headcount": int(r["headcount"]),
            "present_days": round_half_up(Decimal(str(r["present_days"])), 2),
            # mean over records (not mean of employee means)
            "avg_work_hours": round_half_up(Decimal(int(r["wh_sum"])) / Decimal(n) / 100, 2) if n else None,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })
    return {"month": month, "items": items}


def build_leaderboard_pipeline(first: str, last: str, limit: int, department: Optional[str]) -> list:
    pipeline: List[dict] = [
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},  # logs of unknown employees are dropped
    ]
    if department:
        pipeline.append({"$match": {"employee.department": department}})  # rank inside department
    pipeline += [
        # $rank = standard competition ranking (1, 2, 2, 4)
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name",
                      "department": "$employee.department",
                      "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def get_late_leaderboard(month: str = Query(..., pattern=MONTH_PATTERN),
                         limit: int = Query(10, ge=1, le=50),
                         department: Optional[str] = None):
    """
    Ranks employees by cumulative late minutes in a month (ties share a rank).
    """
    first, last, _, _ = parse_month(month)
    rows = list(db.attendance_logs.aggregate(build_leaderboard_pipeline(first, last, limit, department)))
    return {"month": month, "items": [
        {"rank": int(r["rank"]), "emp_code": r["emp_code"], "name": r["name"],
         "department": r["department"], "total_late_minutes": int(r["total_late_minutes"]),
         "late_count": int(r["late_count"])} for r in rows
    ]}


def validate_trend_range(date_from: str, date_to: str):
    d1, d2 = validate_date(date_from, "from"), validate_date(date_to, "to")
    if d2 < d1:
        raise_422("to must not be before from")
    days = (d2 - d1).days + 1
    if days > 92:
        raise_422("range must not exceed 92 days")
    return d1, days


def build_trend_pipeline(department: str, d1: date_cls, days: int) -> list:
    start = datetime(d1.year, d1.month, d1.day, tzinfo=UTC)
    return [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "codes": {"$push": "$emp_code"}, "joined": {"$push": "$joined_on"}}},
        # Gap filling: one row per calendar day (weekends / empty days included), done in the DB
        {"$project": {"_id": 0, "codes": 1, "joined": 1, "offset": {"$range": [0, days]}}},
        {"$unwind": "$offset"},
        {"$addFields": {"day": {"$add": [start, {"$multiply": ["$offset", 86400000]}]}}},
        {"$addFields": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}},
                        "dow": {"$dayOfWeek": "$day"}}},
        {"$lookup": {  # that day's logs of department members
            "from": "attendance_logs", "localField": "date", "foreignField": "date",
            "let": {"codes": "$codes"},
            "pipeline": [
                {"$match": {"$expr": {"$in": ["$emp_code", "$$codes"]}}},
                {"$group": {"_id": None, "present": {"$sum": PV_ANY}, "late": {"$sum": LATE1}}},
            ],
            "as": "agg"
        }},
        {"$addFields": {
            "present_count": {"$ifNull": [{"$first": "$agg.present"}, 0]},
            "late_count": {"$ifNull": [{"$first": "$agg.late"}, 0]},
            "headcount": {"$size": {"$filter": {"input": "$joined", "as": "j",
                                                "cond": {"$lte": ["$$j", "$date"]}}}},  # R9
            "is_working_day": {"$and": [{"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]}]},
        }},
        {"$addFields": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$present_count", "$headcount"]}, None]}}},
        # 7-day moving average inside the range; $avg ignores nulls
        {"$setWindowFields": {"sortBy": {"date": 1},
                              "output": {"moving_avg_7d": {"$avg": "$attendance_rate",
                                                           "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"date": 1}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1,
                      "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def get_department_trend(department: str, date_from: str = Query(..., alias="from"), date_to: str = Query(..., alias="to")):
    """
    Daily attendance trend for a department: one row per calendar day.
    """
    d1, days = validate_trend_range(date_from, date_to)
    if not db.employees.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(status_code=404, detail="Unknown department")

    items = []
    for r in db.employees.aggregate(build_trend_pipeline(department, d1, days)):
        rate, moving = r.get("attendance_rate"), r.get("moving_avg_7d")
        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": round_half_up(Decimal(str(r["present_count"])), 2),
            "late_count": int(r["late_count"]),
            "attendance_rate": round_half_up(rate, 4) if rate is not None else None,
            "moving_avg_7d": round_half_up(moving, 4) if moving is not None else None,
        })
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin / Diagnostics Endpoints
# --------------------------------------------------------------------------- #

def require_params(**params):
    missing = [k for k, v in params.items() if v is None]
    if missing:
        raise_422("missing required parameter(s): " + ", ".join(missing))


@app.get("/admin/explain/{endpoint_name}")
def explain_endpoint_query(
        endpoint_name: Literal["attendance_list", "employee_monthly", "department_summary",
                               "late_leaderboard", "department_trend"],
        emp_code: Optional[str] = None,
        month: Optional[str] = None,
        department: Optional[str] = None,
        limit: int = Query(10, ge=1, le=50),
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        status: Optional[Status] = None,
        frm: Optional[str] = Query(None, alias="from"),
        to: Optional[str] = None,
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
):
    """
    Returns executionStats explain of the endpoint's main query, built by the
    same builder functions the real endpoint uses.
    """
    if endpoint_name == "attendance_list":
        q = build_attendance_query(emp_code, date_from, date_to, status)
        collection = "attendance_logs"
        cmd = {"find": collection, "filter": q, "sort": dict(ATTENDANCE_SORT), "limit": page_size}
        if (page - 1) * page_size:
            cmd["skip"] = (page - 1) * page_size
    elif endpoint_name == "employee_monthly":
        require_params(emp_code=emp_code, month=month)
        first, last, _, _ = parse_month(month)
        collection = "attendance_logs"
        cmd = {"aggregate": collection, "pipeline": build_monthly_pipeline(emp_code, first, last), "cursor": {}}
    elif endpoint_name == "department_summary":
        require_params(month=month)
        first, last, _, _ = parse_month(month)
        collection = "employees"
        cmd = {"aggregate": collection, "pipeline": build_summary_pipeline(first, last, department), "cursor": {}}
    elif endpoint_name == "late_leaderboard":
        require_params(month=month)
        first, last, _, _ = parse_month(month)
        collection = "attendance_logs"
        cmd = {"aggregate": collection,
               "pipeline": build_leaderboard_pipeline(first, last, limit, department), "cursor": {}}
    else:  # department_trend
        require_params(department=department, **{"from": frm, "to": to})
        d1, days = validate_trend_range(frm, to)
        collection = "employees"
        cmd = {"aggregate": collection, "pipeline": build_trend_pipeline(department, d1, days), "cursor": {}}

    result = db.command({"explain": cmd, "verbosity": "executionStats"})
    # BSON types (ObjectId, Timestamp...) -> plain JSON
    return {"endpoint": endpoint_name, "collection": collection, "explain": json.loads(json_util.dumps(result))}


# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
