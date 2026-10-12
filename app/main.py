
"""uvicorn app.main:app --port 8000

Employee Attendance & Analytics API.
"""
import os
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
PRESENT_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def ensure_indexes() -> None:
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index("department")
    db.employees.create_index("joined_on")
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", 1)])
    db.attendance_logs.create_index([("late_minutes", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("status", 1), ("date", 1)])


ensure_indexes()


def round_half_up(value: Any, digits: int = 2):
    if value is None:
        return None
    q = Decimal("1").scaleb(-digits)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.astimezone(timezone.utc).timestamp() * 1000)


def from_epoch_ms(value: Optional[int]) -> Optional[datetime]:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


def dt_to_ist(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(IST)


def attendance_date_for_punch(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    punch_in_ist = dt_to_ist(punch_in)
    if shift_end <= shift_start:
        end_time = time.fromisoformat(shift_end)
        if punch_in_ist.time() < end_time:
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()
        return punch_in_ist.date().isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, shift_end: Optional[str] = None) -> int:
    punch_in_ist = dt_to_ist(punch_in)
    if shift_end is not None and shift_end <= shift_start and punch_in_ist.time() < time.fromisoformat(shift_end):
        start_day = punch_in_ist.date() - timedelta(days=1)
    else:
        start_day = punch_in_ist.date()
    start_dt = datetime.combine(start_day, time.fromisoformat(shift_start), tzinfo=IST)
    delta_seconds = (punch_in_ist - start_dt).total_seconds()
    if delta_seconds <= 600:
        return 0
    return int(delta_seconds // 60)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    if punch_out <= punch_in:
        raise ValueError("punch_out must be after punch_in")
    seconds = (punch_out - punch_in).total_seconds()
    hours = Decimal(str(seconds)) / Decimal("3600")
    return float(Decimal(str(hours)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_end: str, record_date: str, shift_start: str) -> int:
    record_day = date.fromisoformat(record_date)
    if shift_end <= shift_start:
        end_dt = datetime.combine(record_day + timedelta(days=1), time.fromisoformat(shift_end), tzinfo=IST)
    else:
        end_dt = datetime.combine(record_day, time.fromisoformat(shift_end), tzinfo=IST)
    diff_seconds = (dt_to_ist(punch_out) - end_dt).total_seconds()
    if diff_seconds <= 0:
        return 0
    minutes = int(diff_seconds // 60)
    return minutes if minutes >= 30 else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    if work_hours is None:
        return False
    return work_hours < 4.50


def month_bounds(month: str) -> tuple[date, date]:
    year_text, month_text = month.split("-")
    year = int(year_text)
    month_num = int(month_text)
    start = date(year, month_num, 1)
    if month_num == 12:
        end = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        end = date(year, month_num + 1, 1) - timedelta(days=1)
    return start, end


def convert_api_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return to_epoch_ms(value)
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    return value


def serialize_employee(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_epoch_ms(doc.get("created_at")),
    }


def serialize_history_entry(entry: dict[str, Any]) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    for field, payload in (entry.get("changes") or {}).items():
        if isinstance(payload, dict):
            changes[field] = {
                "from": convert_api_value(payload.get("from")),
                "to": convert_api_value(payload.get("to")),
            }
    return {
        "at": to_epoch_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes,
    }


def serialize_attendance_record(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_ms(doc.get("punch_in")),
        "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes", 0)),
        "overtime_minutes": int(doc.get("overtime_minutes", 0)),
        "half_day": bool(doc.get("half_day", False)),
        "history": [serialize_history_entry(entry) for entry in doc.get("history", [])],
    }


# Models
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: date

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, value: str) -> str:
        if not value:
            raise ValueError("emp_code is required")
        return value

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_times(cls, value: str) -> str:
        try:
            hour, minute = value.split(":")
            if len(hour) != 2 or len(minute) != 2:
                raise ValueError
            time.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("shift times must be in HH:MM format") from exc
        return value

    @model_validator(mode="after")
    def validate_shift_range(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: str = "PRESENT"

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in PRESENT_STATUSES:
            raise ValueError("status must be PRESENT, WFH or ON_DUTY")
        return value

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, value: Optional[int]) -> Optional[int]:
        if value is None:
            return value
        if value < 100000000000 or value > 4102444800000:
            raise ValueError("must be a valid epoch millisecond timestamp")
        return value


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emp_code: str
    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, value: Optional[int]) -> Optional[int]:
        if value is None:
            return value
        if value < 100000000000 or value > 4102444800000:
            raise ValueError("must be a valid epoch millisecond timestamp")
        return value


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Optional[str] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        if value not in ALL_STATUSES:
            raise ValueError("status must be one of PRESENT, ABSENT, LEAVE, WFH, ON_DUTY")
        return value

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_ts(cls, value: Optional[int]) -> Optional[int]:
        if value is None:
            return value
        if value < 100000000000 or value > 4102444800000:
            raise ValueError("time values must be valid epoch milliseconds")
        return value

    @model_validator(mode="after")
    def validate_absent_rules(self):
        if self.status in {"ABSENT", "LEAVE"} and (self.punch_in is not None or self.punch_out is not None):
            raise ValueError("ABSENT and LEAVE records must clear punch_in and punch_out")
        return self


# Endpoints
@app.get("/health")
def health() -> dict[str, str]:
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as exc:  # pragma: no cover
        raise HTTPException(status_code=503, detail="MongoDB unavailable") from exc


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    doc = body.model_dump(mode="python")
    doc["joined_on"] = body.joined_on.isoformat()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "emp_code already exists") from exc
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: dict[str, Any] = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    docs = list(db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size))
    return {"items": [serialize_employee(doc) for doc in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")

    ts = datetime.now(timezone.utc) if body.punched_at is None else from_epoch_ms(int(body.punched_at))
    ts = ts.replace(microsecond=0)
    record_date = attendance_date_for_punch(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": record_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], emp["shift_end"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        result = db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "already punched in for this date") from exc
    created = db.attendance_logs.find_one({"_id": result.inserted_id})
    return serialize_attendance_record(created)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")

    ts = datetime.now(timezone.utc) if body.punched_at is None else from_epoch_ms(int(body.punched_at))
    ts = ts.replace(microsecond=0)
    record = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_out": None, "punch_in": {"$lte": ts}}, sort=[("punch_in", -1)])
    if record is None:
        raise HTTPException(404, "no punch-in found")
    if ts <= record["punch_in"]:
        raise HTTPException(422, "punch_out must be after punch_in")
    if (ts - record["punch_in"]).total_seconds() > 24 * 3600:
        raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    work_hours = compute_work_hours(record["punch_in"], ts)
    overtime_minutes = compute_overtime(ts, emp["shift_end"], record["date"], emp["shift_start"])
    update = {
        "punch_out": ts,
        "work_hours": work_hours,
        "overtime_minutes": overtime_minutes,
        "half_day": compute_half_day(work_hours),
    }
    updated = db.attendance_logs.find_one_and_update({"_id": record["_id"], "punch_out": None}, {"$set": update}, return_document=True)
    if updated is None:
        raise HTTPException(409, "that record is already punched out")
    return serialize_attendance_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        if date_from and date_to and date.fromisoformat(date_from) > date.fromisoformat(date_to):
            raise HTTPException(422, "date_from cannot be after date_to")
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    docs = list(db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip((page - 1) * page_size).limit(page_size))
    return {"items": [serialize_attendance_record(doc) for doc in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(404, "attendance record not found")

    final_status = body.status if body.status is not None else record["status"]
    final_punch_in = record.get("punch_in") if body.punch_in is None else from_epoch_ms(int(body.punch_in))
    final_punch_out = record.get("punch_out") if body.punch_out is None else from_epoch_ms(int(body.punch_out))

    if final_status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT and LEAVE records must clear punch_in and punch_out")
        final_punch_in = None
        final_punch_out = None
    else:
        if final_punch_in is None:
            raise HTTPException(422, "presence status requires punch_in")
        if attendance_date_for_punch(final_punch_in, employee["shift_start"], employee["shift_end"]) != date:
            raise HTTPException(422, "punch_in must stay on the attendance date")
        if final_punch_out is not None and final_punch_out <= final_punch_in:
            raise HTTPException(422, "punch_out must be after punch_in")
        if final_punch_out is not None and (final_punch_out - final_punch_in).total_seconds() > 24 * 3600:
            raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    old_status = record.get("status")
    old_punch_in = record.get("punch_in")
    old_punch_out = record.get("punch_out")
    old_work_hours = record.get("work_hours")
    old_late_minutes = int(record.get("late_minutes", 0))
    old_overtime_minutes = int(record.get("overtime_minutes", 0))
    old_half_day = bool(record.get("half_day", False))

    if final_status in {"ABSENT", "LEAVE"}:
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = 0 if final_punch_in is None else compute_late_minutes(final_punch_in, employee["shift_start"], employee["shift_end"])
        if final_punch_in is not None and final_punch_out is not None:
            new_work_hours = compute_work_hours(final_punch_in, final_punch_out)
            new_overtime_minutes = compute_overtime(final_punch_out, employee["shift_end"], date, employee["shift_start"])
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    if (
        final_status == old_status
        and final_punch_in == old_punch_in
        and final_punch_out == old_punch_out
        and new_work_hours == old_work_hours
        and new_late_minutes == old_late_minutes
        and new_overtime_minutes == old_overtime_minutes
        and new_half_day == old_half_day
    ):
        raise HTTPException(422, "request changes nothing")

    changes: dict[str, Any] = {}
    for field, before, after in [
        ("status", old_status, final_status),
        ("punch_in", old_punch_in, final_punch_in),
        ("punch_out", old_punch_out, final_punch_out),
        ("work_hours", old_work_hours, new_work_hours),
        ("late_minutes", old_late_minutes, new_late_minutes),
        ("overtime_minutes", old_overtime_minutes, new_overtime_minutes),
        ("half_day", old_half_day, new_half_day),
    ]:
        if before != after:
            changes[field] = {"from": convert_api_value(before), "to": convert_api_value(after)}

    history_entry = {
        "at": datetime.now(timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": {key: {"from": value["from"], "to": value["to"]} for key, value in changes.items()},
    }

    update = {
        "status": final_status,
        "punch_in": final_punch_in,
        "punch_out": final_punch_out,
        "work_hours": new_work_hours,
        "late_minutes": new_late_minutes,
        "overtime_minutes": new_overtime_minutes,
        "half_day": new_half_day,
    }
    updated = db.attendance_logs.find_one_and_update({"_id": record["_id"]}, {"$set": update, "$push": {"history": history_entry}}, return_document=True)
    if updated is None:
        raise HTTPException(409, "concurrent modification detected")
    return serialize_attendance_record(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    month_start, month_end = month_bounds(month)
    docs = list(db.attendance_logs.find({"emp_code": emp_code, "date": {"$gte": month_start.isoformat(), "$lte": month_end.isoformat()}}))

    working_days = 0
    joined = date.fromisoformat(employee["joined_on"])
    current = month_start
    while current <= month_end:
        if current.weekday() < 5 and current >= joined:
            working_days += 1
        current += timedelta(days=1)

    present_days = Decimal("0")
    leave_days = 0
    late_count = 0
    total_late_minutes = 0
    total_overtime_minutes = 0
    for doc in docs:
        if doc.get("status") == "LEAVE":
            leave_days += 1
        if doc.get("status") in PRESENT_STATUSES and date.fromisoformat(doc["date"]).weekday() < 5:
            present_days += Decimal("0.5") if doc.get("half_day") else Decimal("1")
        if int(doc.get("late_minutes", 0)) > 0:
            late_count += 1
            total_late_minutes += int(doc.get("late_minutes", 0))
        total_overtime_minutes += int(doc.get("overtime_minutes", 0))

    attendance_pct = None if working_days == 0 else round_half_up((present_days / Decimal(working_days)) * Decimal("100"), 4)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": float(present_days),
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    month_start, month_end = month_bounds(month)
    filter_emp: dict[str, Any] = {"joined_on": {"$lte": month_end.isoformat()}}
    if department is not None:
        filter_emp["department"] = department
    employees = list(db.employees.find(filter_emp, {"_id": 0, "emp_code": 1, "department": 1, "joined_on": 1}))
    if not employees:
        return {"month": month, "items": []}

    by_department: dict[str, dict[str, Any]] = {}
    for employee in employees:
        dept = employee["department"]
        by_department.setdefault(dept, {
            "headcount": 0,
            "present_days": Decimal("0"),
            "late_count": 0,
            "total_late_minutes": 0,
            "avg_work_hours_total": Decimal("0"),
            "avg_work_hours_count": 0,
            "leave_count": 0,
            "on_duty_count": 0,
        })
        by_department[dept]["headcount"] += 1

    for doc in db.attendance_logs.find({"date": {"$gte": month_start.isoformat(), "$lte": month_end.isoformat()}}):
        employee = next((item for item in employees if item["emp_code"] == doc.get("emp_code")), None)
        if employee is None:
            continue
        dept = employee["department"]
        stats = by_department[dept]
        if doc.get("status") in PRESENT_STATUSES and date.fromisoformat(doc["date"]).weekday() < 5:
            stats["present_days"] += Decimal("0.5") if doc.get("half_day") else Decimal("1")
        if doc.get("status") == "LEAVE":
            stats["leave_count"] += 1
        if doc.get("status") == "ON_DUTY":
            stats["on_duty_count"] += 1
        if int(doc.get("late_minutes", 0)) > 0:
            stats["late_count"] += 1
            stats["total_late_minutes"] += int(doc.get("late_minutes", 0))
        if doc.get("status") in PRESENT_STATUSES and doc.get("work_hours") is not None:
            stats["avg_work_hours_total"] += Decimal(str(doc.get("work_hours")))
            stats["avg_work_hours_count"] += 1

    items = []
    for dept in sorted(by_department):
        stats = by_department[dept]
        avg = None if stats["avg_work_hours_count"] == 0 else round_half_up(stats["avg_work_hours_total"] / Decimal(stats["avg_work_hours_count"]), 2)
        items.append({
            "department": dept,
            "headcount": stats["headcount"],
            "present_days": float(stats["present_days"]),
            "avg_work_hours": avg,
            "late_count": stats["late_count"],
            "total_late_minutes": stats["total_late_minutes"],
            "leave_count": stats["leave_count"],
            "on_duty_count": stats["on_duty_count"],
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    month_start, month_end = month_bounds(month)
    match = {"date": {"$gte": month_start.isoformat(), "$lte": month_end.isoformat()}, "late_minutes": {"$gt": 0}}
    pipeline: list[dict[str, Any]] = [
        {"$match": match},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$group": {"_id": "$emp_code", "name": {"$first": "$employee.name"}, "department": {"$first": "$employee.department"}, "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
    ])
    rows = list(db.attendance_logs.aggregate(pipeline))
    final = []
    rank = 0
    prev_total = None
    for idx, row in enumerate(rows, start=1):
        if prev_total is None or row["total_late_minutes"] != prev_total:
            rank = idx
            prev_total = row["total_late_minutes"]
        if rank <= limit:
            final.append({
                "rank": rank,
                "emp_code": row["_id"],
                "name": row["name"],
                "department": row["department"],
                "total_late_minutes": row["total_late_minutes"],
                "late_count": row["late_count"],
            })
    return {"month": month, "items": final}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    employees = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not employees:
        raise HTTPException(404, "unknown department")

    start = date.fromisoformat(from_date)
    end = date.fromisoformat(to_date)
    if end < start:
        raise HTTPException(422, "to must be on or after from")
    if (end - start).days + 1 > 92:
        raise HTTPException(422, "date range exceeds 92 days")

    emp_codes = [employee["emp_code"] for employee in employees]
    logs_by_day: dict[str, list[dict[str, Any]]] = {}
    for doc in db.attendance_logs.find({"emp_code": {"$in": emp_codes}, "date": {"$gte": from_date, "$lte": to_date}}):
        logs_by_day.setdefault(doc["date"], []).append(doc)

    rows = []
    current = start
    while current <= end:
        day_str = current.isoformat()
        headcount = sum(1 for employee in employees if date.fromisoformat(employee["joined_on"]) <= current)
        day_logs = logs_by_day.get(day_str, [])
        present_count = Decimal("0")
        late_count = 0
        for doc in day_logs:
            if doc.get("status") in PRESENT_STATUSES:
                present_count += Decimal("0.5") if doc.get("half_day") else Decimal("1")
            if int(doc.get("late_minutes", 0)) > 0:
                late_count += 1
        attendance_rate = None
        if current.weekday() < 5:
            if headcount > 0:
                attendance_rate = round_half_up(present_count / Decimal(headcount), 4)
            else:
                attendance_rate = 0.0
        rows.append({
            "date": day_str,
            "is_working_day": current.weekday() < 5,
            "headcount": headcount,
            "present_count": float(present_count),
            "late_count": late_count,
            "attendance_rate": attendance_rate,
            "moving_avg_7d": None,
        })
        current += timedelta(days=1)

    for index, row in enumerate(rows):
        window = [item["attendance_rate"] for item in rows[max(0, index - 6): index + 1] if item["attendance_rate"] is not None]
        if not window:
            row["moving_avg_7d"] = None
        else:
            row["moving_avg_7d"] = round_half_up(sum(Decimal(str(value)) for value in window) / Decimal(len(window)), 4)
    return {"department": department, "items": rows}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: Optional[int] = Query(None, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    allowed = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in allowed:
        raise HTTPException(404, "unknown endpoint")

    if endpoint == "attendance_list":
        q: dict[str, Any] = {}
        if emp_code is not None:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status is not None:
            q["status"] = status
        explain_doc = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip((page - 1) * page_size).limit(page_size).explain("executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_doc}

    if endpoint == "employee_monthly":
        if emp_code is None or month is None:
            raise HTTPException(422, "emp_code and month are required")
        start, end = month_bounds(month)
        explain_doc = db.attendance_logs.find({"emp_code": emp_code, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}).sort("date", 1).explain("executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_doc}

    if endpoint == "department_summary":
        if month is None:
            raise HTTPException(422, "month is required")
        start, end = month_bounds(month)
        explain_doc = db.attendance_logs.find({"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}).sort("date", 1).explain("executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_doc}

    if endpoint == "late_leaderboard":
        if month is None:
            raise HTTPException(422, "month is required")
        start, end = month_bounds(month)
        q: dict[str, Any] = {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}, "late_minutes": {"$gt": 0}}
        if department is not None:
            q["emp_code"] = {"$in": [item["emp_code"] for item in db.employees.find({"department": department}, {"_id": 0, "emp_code": 1})]}
        explain_doc = db.attendance_logs.find(q).sort([("late_minutes", -1), ("emp_code", 1)]).explain("executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_doc}

    if endpoint == "department_trend":
        if department is None or from_date is None or to_date is None:
            raise HTTPException(422, "department, from and to are required")
        emp_codes = [item["emp_code"] for item in db.employees.find({"department": department}, {"_id": 0, "emp_code": 1})]
        explain_doc = db.attendance_logs.find({"emp_code": {"$in": emp_codes}, "date": {"$gte": from_date, "$lte": to_date}}).sort("date", 1).explain("executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_doc}

    raise HTTPException(404, "unknown endpoint")
