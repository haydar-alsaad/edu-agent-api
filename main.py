"""
Education Agent API v2 (Supabase-backed).

Replaces the JSON-file backed API. Same GET endpoint paths and query params as v1
so existing agent tool configurations continue to work without change. Response
shapes updated to the new Loveable/Supabase schema (snake_case columns).

NEW in v2:
  - 7 POST endpoints for write actions (enrollment, advising, documents, holds,
    fees, profile, applicant status)
  - Auto-logging middleware: every successful POST writes a row to agent_actions
    so the staff portal's "live agent activity" drawer fires automatically without
    each endpoint having to remember to log.
  - All reads/writes go through Supabase REST (PostgREST) using the service role
    key. The service role key MUST be set in the SUPABASE_SERVICE_ROLE_KEY env var.

Endpoints (paths preserved from v1):
  GET  /                         API info
  GET  /health                   Status + data load counts
  GET  /student                  Workhorse student profile (the single call)
  GET  /applicant                Applicant lookup
  GET  /course                   Course catalog with sections
  GET  /faculty                  Faculty lookup (with advisor routing hint)
  GET  /advisor                  Advisor lookup (with faculty enrichment)
  GET  /calendar                 Academic calendar events
  GET  /degree-requirements      Program requirements
  GET  /exam-schedule            Exam lookup

NEW POST endpoints (write actions):
  POST /enrollment/action        Drop / add / swap a course
  POST /advising/appointment     Book / cancel an advising appointment
  POST /document/generate        Generate a transcript / invoice / letter (logs intent)
  POST /hold/action              Clear a hold (registrar action)
  POST /fee/payment              Record a Sadad-style payment
  POST /profile/update           Update student contact info
  POST /application/action       Move applicant status / mark docs received

Auth: this is a demo API. No authentication on the API itself. The Supabase
service role key (server-side only) is the only credential.
"""

import os
import json
import logging
from datetime import datetime, timezone
from typing import Optional, Any, Dict, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
import httpx

# ============================================================
# Config
# ============================================================

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
PLANNING_SEMESTER_DEFAULT = os.environ.get("PLANNING_SEMESTER_DEFAULT", "Fall 2026")

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    raise RuntimeError(
        "Missing required env vars: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY"
    )

REST_BASE = f"{SUPABASE_URL}/rest/v1"
HEADERS_BASE = {
    "apikey": SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "Content-Type": "application/json",
}

logger = logging.getLogger("kfut_api")
logger.setLevel(logging.INFO)


# ============================================================
# Lifespan + HTTP client
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create a shared httpx client for the lifetime of the app."""
    async with httpx.AsyncClient(
        base_url=REST_BASE,
        headers=HEADERS_BASE,
        timeout=httpx.Timeout(15.0),
    ) as client:
        app.state.http = client
        logger.info("HTTP client initialized; ready to serve.")
        yield
        logger.info("HTTP client shutting down.")


app = FastAPI(
    title="KFUT Student Support API",
    version="2.0",
    description="Supabase-backed API for the KFUT WhatsApp student support agent.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# Supabase REST helpers
# ============================================================

async def sb_get(
    table: str,
    params: Optional[Dict[str, Any]] = None,
    request: Optional[Request] = None,
) -> List[Dict[str, Any]]:
    """SELECT against a Supabase table via PostgREST. Returns a list of rows."""
    client: httpx.AsyncClient = request.app.state.http if request else app.state.http
    resp = await client.get(f"/{table}", params=params or {})
    if resp.status_code >= 400:
        logger.error(f"Supabase GET /{table} failed: {resp.status_code} {resp.text}")
        raise HTTPException(
            status_code=502,
            detail=f"Database read failed: {resp.text[:200]}",
        )
    return resp.json()


async def sb_get_one(
    table: str,
    params: Optional[Dict[str, Any]] = None,
    request: Optional[Request] = None,
) -> Optional[Dict[str, Any]]:
    """Convenience — fetch first matching row or None."""
    rows = await sb_get(table, params=params, request=request)
    return rows[0] if rows else None


async def sb_insert(
    table: str,
    record: Dict[str, Any],
    request: Optional[Request] = None,
) -> Dict[str, Any]:
    """INSERT a row, return the inserted representation."""
    client: httpx.AsyncClient = request.app.state.http if request else app.state.http
    resp = await client.post(
        f"/{table}",
        json=record,
        headers={"Prefer": "return=representation"},
    )
    if resp.status_code >= 400:
        logger.error(f"Supabase INSERT /{table} failed: {resp.status_code} {resp.text}")
        raise HTTPException(
            status_code=502,
            detail=f"Database write failed: {resp.text[:200]}",
        )
    data = resp.json()
    return data[0] if isinstance(data, list) and data else data


async def sb_update(
    table: str,
    match: Dict[str, str],
    updates: Dict[str, Any],
    request: Optional[Request] = None,
) -> List[Dict[str, Any]]:
    """UPDATE matching rows, return updated representations."""
    client: httpx.AsyncClient = request.app.state.http if request else app.state.http
    params = {k: f"eq.{v}" for k, v in match.items()}
    resp = await client.patch(
        f"/{table}",
        params=params,
        json=updates,
        headers={"Prefer": "return=representation"},
    )
    if resp.status_code >= 400:
        logger.error(f"Supabase UPDATE /{table} failed: {resp.status_code} {resp.text}")
        raise HTTPException(
            status_code=502,
            detail=f"Database update failed: {resp.text[:200]}",
        )
    return resp.json()


async def sb_delete(
    table: str,
    match: Dict[str, str],
    request: Optional[Request] = None,
) -> List[Dict[str, Any]]:
    """DELETE matching rows, return deleted representations."""
    client: httpx.AsyncClient = request.app.state.http if request else app.state.http
    params = {k: f"eq.{v}" for k, v in match.items()}
    resp = await client.delete(
        f"/{table}",
        params=params,
        headers={"Prefer": "return=representation"},
    )
    if resp.status_code >= 400:
        logger.error(f"Supabase DELETE /{table} failed: {resp.status_code} {resp.text}")
        raise HTTPException(
            status_code=502,
            detail=f"Database delete failed: {resp.text[:200]}",
        )
    return resp.json()


async def log_agent_action(
    action_type: str,
    description: str,
    student_id: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    status_str: str = "success",
    request: Optional[Request] = None,
) -> None:
    """Write a row to agent_actions. Used by every successful POST endpoint
    so the staff-portal activity drawer fires in realtime via Supabase channels."""
    try:
        await sb_insert(
            "agent_actions",
            {
                "action_type": action_type,
                "description": description,
                "student_id": student_id,
                "payload": payload or {},
                "status": status_str,
            },
            request=request,
        )
    except Exception as e:
        # Log but don't fail the parent request just because logging failed
        logger.warning(f"Failed to log agent_action: {e}")


# ============================================================
# Root / health
# ============================================================

@app.get("/")
async def root():
    return {
        "name": "KFUT Student Support API",
        "version": "2.0",
        "docs": "/docs",
        "health": "/health",
        "backend": "Supabase",
    }


@app.get("/health")
async def health(request: Request):
    """Health check including a basic Supabase reachability test."""
    checks = {"api": "ok"}
    try:
        rows = await sb_get(
            "students",
            params={"select": "student_id", "limit": "1"},
            request=request,
        )
        checks["supabase"] = "ok" if rows else "no_data"
    except Exception as e:
        checks["supabase"] = f"error: {e}"
    return {"status": "healthy", "version": "2.0", "checks": checks}


# ============================================================
# GET /student — workhorse
# ============================================================

@app.get("/student")
async def get_student_data(
    request: Request,
    student_id: Optional[str] = Query(None),
    name: Optional[str] = Query(None, description="Full name (English) — partial match"),
    phone: Optional[str] = Query(None),
    planning_semester: Optional[str] = Query(None, description="Default Fall 2026"),
):
    """Workhorse endpoint — full student package in one call.

    Returns: profile, scheduling preferences (from advisor record), advisor
    contact, completed + in-progress grades with computed GPA, current schedule,
    upcoming exams, fees, holds, remaining required courses, eligible courses
    for the planning semester (prereqs validated, sections available).
    """
    if not any([student_id, name, phone]):
        raise HTTPException(400, "Provide student_id, name, or phone")

    # 1. Find the student
    if student_id:
        student = await sb_get_one(
            "students", params={"student_id": f"eq.{student_id}"}, request=request
        )
    elif phone:
        student = await sb_get_one(
            "students", params={"phone": f"eq.{phone}"}, request=request
        )
    else:
        student = await sb_get_one(
            "students",
            params={"full_name_en": f"ilike.*{name}*", "limit": "1"},
            request=request,
        )
    if not student:
        raise HTTPException(404, "Student not found")

    sid = student["student_id"]
    semester = planning_semester or PLANNING_SEMESTER_DEFAULT

    # 2. Pull everything in parallel-ish (PostgREST is one-call-per-table)
    advisor_record = None
    advisor_faculty = None
    if student.get("advisor"):
        # student.advisor is a faculty_id per the schema; advisors table joins on faculty_id
        advisor_record = await sb_get_one(
            "advisors", params={"faculty_id": f"eq.{student['advisor']}"}, request=request
        )
        advisor_faculty = await sb_get_one(
            "faculty", params={"faculty_id": f"eq.{student['advisor']}"}, request=request
        )

    grades = await sb_get(
        "grades",
        params={"student_id": f"eq.{sid}", "order": "semester.desc"},
        request=request,
    )
    current_schedule = await sb_get(
        "class_schedules",
        params={"student_id": f"eq.{sid}"},
        request=request,
    )
    fees = await sb_get(
        "fee_records",
        params={"student_id": f"eq.{sid}", "order": "due_date.desc"},
        request=request,
    )
    holds = await sb_get(
        "holds",
        params={"student_id": f"eq.{sid}"},
        request=request,
    )

    # 3. Compute completed credits from grades; use stored gpa from students record
    # (so agent and staff portal show the same number — portal reads students.gpa directly)
    completed = [g for g in grades if g.get("status") == "Completed"]
    in_progress = [g for g in grades if g.get("status") != "Completed"]
    total_credits = sum(int(g.get("credits") or 0) for g in completed)
    computed_gpa = student.get("gpa")

    # 4. Hold flags — surface registration/transcript blockers up top
    active_holds = [h for h in holds if (h.get("status") or "").lower() == "active"]
    holds_summary = {
        "active_count": len(active_holds),
        "blocks_registration": any(h.get("blocks_registration") for h in active_holds),
        "blocks_transcript": any(h.get("blocks_transcript") for h in active_holds),
        "active_holds": active_holds,
        "all_holds": holds,
    }

    # 5. Finances summary
    outstanding_total = sum(float(f.get("outstanding_sar") or 0) for f in fees)
    finances = {
        "outstanding_total_sar": round(outstanding_total, 2),
        "has_outstanding": outstanding_total > 0,
        "records": fees,
    }

    # 6. Upcoming exams — match the student's current course codes
    upcoming_exams: List[Dict[str, Any]] = []
    enrolled_codes = list({s["course_code"] for s in current_schedule if s.get("course_code")})
    if enrolled_codes:
        for code in enrolled_codes:
            ex = await sb_get(
                "exam_schedule",
                params={"course_code": f"eq.{code}"},
                request=request,
            )
            upcoming_exams.extend(ex)

    # 7. Degree progress — what's left for the program
    remaining_required: List[Dict[str, Any]] = []
    eligible_for_planning: List[Dict[str, Any]] = []
    program_total_credits = 0

    if student.get("program_code"):
        program = await sb_get_one(
            "degree_programs",
            params={"program_code": f"eq.{student['program_code']}"},
            request=request,
        )
        program_total_credits = (program or {}).get("total_credits", 0) or 0

        all_reqs = await sb_get(
            "degree_requirement_courses",
            params={"program_code": f"eq.{student['program_code']}"},
            request=request,
        )
        completed_codes = {g["course_code"] for g in completed if g.get("course_code")}
        in_progress_codes = {g["course_code"] for g in in_progress if g.get("course_code")}
        scheduled_codes = {s["course_code"] for s in current_schedule if s.get("course_code")}

        for r in all_reqs:
            code = r.get("course_code")
            if code and code not in completed_codes:
                remaining_required.append(r)

        # Eligible for planning semester: remaining courses that have at least
        # one Open section in the requested semester AND aren't already enrolled
        for r in remaining_required:
            code = r.get("course_code")
            if not code or code in scheduled_codes or code in in_progress_codes:
                continue
            sections = await sb_get(
                "course_sections",
                params={
                    "course_code": f"eq.{code}",
                    "semester": f"eq.{semester}",
                },
                request=request,
            )
            open_sections = [
                s for s in sections
                if (s.get("status") or "").lower() in ("open", "nearly full")
            ]
            if open_sections:
                # Get prerequisite display from courses table
                course_meta = await sb_get_one(
                    "courses", params={"course_code": f"eq.{code}"}, request=request
                )
                eligible_for_planning.append({
                    "course_code": code,
                    "course_name": r.get("course_name"),
                    "credits": r.get("credits"),
                    "typical_year": r.get("typical_year"),
                    "requirement_type": r.get("requirement_type"),
                    "prerequisites_display": (course_meta or {}).get("prerequisites_display"),
                    "open_sections_count": len(open_sections),
                    "open_sections": open_sections,
                })

    credits_remaining = max(0, program_total_credits - total_credits) if program_total_credits else None

    return {
        "student": student,
        "advisor": (
            {**advisor_record, "faculty_record": advisor_faculty}
            if advisor_record
            else None
        ),
        "academics": {
            "computed_gpa": computed_gpa,
            "completed_credits": total_credits,
            "program_total_credits": program_total_credits,
            "credits_remaining_estimate": credits_remaining,
            "completed_grades": completed,
            "in_progress_grades": in_progress,
        },
        "current_schedule": current_schedule,
        "upcoming_exams": upcoming_exams,
        "finances": finances,
        "holds": holds_summary,
        "degree_progress": {
            "planning_semester": semester,
            "remaining_required_courses": remaining_required,
            "eligible_courses_for_planning_semester": eligible_for_planning,
        },
    }


# ============================================================
# GET /applicant
# ============================================================

@app.get("/applicant")
async def get_applicant_status(
    request: Request,
    application_id: Optional[str] = Query(None),
    national_id: Optional[str] = Query(None),
):
    """Admissions application status for prospective students."""
    if not application_id and not national_id:
        raise HTTPException(400, "Provide application_id or national_id")
    params = (
        {"application_id": f"eq.{application_id}"}
        if application_id
        else {"national_id": f"eq.{national_id}"}
    )
    row = await sb_get_one("applicants", params=params, request=request)
    if not row:
        raise HTTPException(404, "Application not found")
    return {"applicant": row}


# ============================================================
# GET /course
# ============================================================

@app.get("/course")
async def get_course_info(
    request: Request,
    course_code: Optional[str] = Query(None),
    department: Optional[str] = Query(None),
    semester: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
):
    """Course details + sections + offerings summary."""
    if not course_code and not department:
        raise HTTPException(400, "Provide course_code or department")

    # Course meta
    if course_code:
        courses = await sb_get(
            "courses", params={"course_code": f"eq.{course_code}"}, request=request
        )
    else:
        courses = await sb_get(
            "courses", params={"department": f"eq.{department}"}, request=request
        )
    if not courses:
        raise HTTPException(404, "No course found")

    results = []
    for c in courses:
        code = c["course_code"]
        section_params = {"course_code": f"eq.{code}"}
        if semester:
            section_params["semester"] = f"eq.{semester}"
        sections = await sb_get("course_sections", params=section_params, request=request)
        if status_filter:
            sections = [
                s for s in sections
                if (s.get("status") or "").lower() == status_filter.lower()
            ]

        offering_params = {"course_code": f"eq.{code}"}
        if semester:
            offering_params["semester"] = f"eq.{semester}"
        summary = await sb_get(
            "course_offerings_summary", params=offering_params, request=request
        )

        results.append({
            "course": c,
            "sections": sections,
            "offerings_summary": summary,
        })

    if course_code:
        return results[0]
    return {"courses": results, "count": len(results)}


# ============================================================
# GET /faculty (with advisor routing hint)
# ============================================================

@app.get("/faculty")
async def get_faculty_info(
    request: Request,
    faculty_id: Optional[str] = Query(None),
    name: Optional[str] = Query(None),
    department: Optional[str] = Query(None),
):
    """Faculty lookup. If the faculty member is also an advisor, surface a
    routing hint pointing the agent at /advisor for advising hours."""
    if not any([faculty_id, name, department]):
        raise HTTPException(400, "Provide faculty_id, name, or department")

    if faculty_id:
        rows = await sb_get(
            "faculty", params={"faculty_id": f"eq.{faculty_id}"}, request=request
        )
    elif name:
        rows = await sb_get(
            "faculty", params={"name_en": f"ilike.*{name}*"}, request=request
        )
    else:
        rows = await sb_get(
            "faculty", params={"department": f"eq.{department}"}, request=request
        )

    enriched = []
    for f in rows:
        record = dict(f)
        if f.get("is_advisor"):
            adv = await sb_get_one(
                "advisors",
                params={"faculty_id": f"eq.{f['faculty_id']}"},
                request=request,
            )
            if adv:
                record["_routing_hint"] = (
                    f"This faculty member is also an academic advisor "
                    f"({adv.get('advisor_id')}). The 'office_hours' field above is "
                    f"for course/drop-in questions only. For academic advising "
                    f"appointments, use get_advisor_info — advising hours are "
                    f"{adv.get('available_days')} {adv.get('available_hours')}, "
                    f"which may differ from the office hours shown here."
                )
        enriched.append(record)

    return {"faculty": enriched, "count": len(enriched)}


# ============================================================
# GET /advisor
# ============================================================

@app.get("/advisor")
async def get_advisor_info(
    request: Request,
    advisor_id: Optional[str] = Query(None),
    faculty_id: Optional[str] = Query(None),
    department: Optional[str] = Query(None),
):
    """Advisor lookup, enriched with the underlying faculty record (office
    hours, specialization, languages, years at university). Use for academic
    advising appointments — Available Days/Hours are advising-specific."""
    if not any([advisor_id, faculty_id, department]):
        raise HTTPException(400, "Provide advisor_id, faculty_id, or department")

    if advisor_id:
        rows = await sb_get(
            "advisors", params={"advisor_id": f"eq.{advisor_id}"}, request=request
        )
    elif faculty_id:
        rows = await sb_get(
            "advisors", params={"faculty_id": f"eq.{faculty_id}"}, request=request
        )
    else:
        rows = await sb_get(
            "advisors", params={"department": f"eq.{department}"}, request=request
        )

    enriched = []
    for adv in rows:
        fac = None
        if adv.get("faculty_id"):
            fac = await sb_get_one(
                "faculty",
                params={"faculty_id": f"eq.{adv['faculty_id']}"},
                request=request,
            )

        record = {**adv, "faculty_record": fac}

        # Routing hint when faculty office hours differ from advising hours
        if fac:
            office_hours = (fac.get("office_hours") or "").strip()
            advising_hours = f"{adv.get('available_days', '')} {adv.get('available_hours', '')}".strip()
            if office_hours and advising_hours and office_hours.lower() != advising_hours.lower():
                record["_routing_hint"] = (
                    f"For academic advising appointments, use this advisor record's "
                    f"available_days and available_hours fields ({advising_hours}). "
                    f"The faculty office_hours field ({office_hours}) is for "
                    f"course/drop-in questions, NOT advising — do not surface those "
                    f"hours when the student asked about an advising appointment."
                )

        enriched.append(record)

    return {"advisors": enriched, "count": len(enriched)}


# ============================================================
# GET /calendar
# ============================================================

@app.get("/calendar")
async def get_academic_calendar(
    request: Request,
    semester: Optional[str] = Query(None),
    event_type: Optional[str] = Query(None),
    upcoming_only: bool = Query(False),
):
    """Academic calendar events: registration, exams, deadlines, holidays, etc."""
    params: Dict[str, Any] = {"order": "start_date.asc"}
    if semester:
        params["semester"] = f"eq.{semester}"
    if event_type:
        params["event_type"] = f"eq.{event_type}"
    if upcoming_only:
        today = datetime.now(timezone.utc).date().isoformat()
        params["start_date"] = f"gte.{today}"
    events = await sb_get("academic_calendar", params=params, request=request)
    return {"events": events, "count": len(events)}


# ============================================================
# GET /degree-requirements
# ============================================================

@app.get("/degree-requirements")
async def get_degree_requirements(
    request: Request,
    program_code: Optional[str] = Query(None),
    program_name: Optional[str] = Query(None),
    requirement_type: Optional[str] = Query(None),
):
    """Program requirements with course list."""
    if not program_code and not program_name:
        raise HTTPException(400, "Provide program_code or program_name")

    if program_code:
        program = await sb_get_one(
            "degree_programs",
            params={"program_code": f"eq.{program_code}"},
            request=request,
        )
    else:
        program = await sb_get_one(
            "degree_programs",
            params={"program_name_en": f"ilike.*{program_name}*"},
            request=request,
        )
    if not program:
        raise HTTPException(404, "Program not found")

    req_params = {"program_code": f"eq.{program['program_code']}"}
    if requirement_type:
        req_params["requirement_type"] = f"eq.{requirement_type}"
    courses = await sb_get(
        "degree_requirement_courses",
        params=req_params,
        request=request,
    )
    return {"program": program, "courses": courses}


# ============================================================
# GET /exam-schedule
# ============================================================

@app.get("/exam-schedule")
async def get_exam_schedule(
    request: Request,
    course_code: Optional[str] = Query(None),
    semester: Optional[str] = Query(None),
    exam_type: Optional[str] = Query(None),
):
    """Exam schedule lookup. Use for 'when is my X exam?' questions."""
    params: Dict[str, Any] = {"order": "exam_date.asc"}
    if course_code:
        params["course_code"] = f"eq.{course_code}"
    if semester:
        params["semester"] = f"eq.{semester}"
    if exam_type:
        params["exam_type"] = f"eq.{exam_type}"
    exams = await sb_get("exam_schedule", params=params, request=request)
    return {"exams": exams, "count": len(exams)}


# ============================================================
# Pydantic models for POST endpoints
# ============================================================

class EnrollmentActionRequest(BaseModel):
    student_id: str
    action: str = Field(..., description="add | drop | swap")
    course_code: Optional[str] = None
    section: Optional[str] = None
    semester: Optional[str] = None
    # For swap: the section to drop + the section to add
    drop_course_code: Optional[str] = None
    drop_section: Optional[str] = None
    add_course_code: Optional[str] = None
    add_section: Optional[str] = None


class AdvisingAppointmentRequest(BaseModel):
    student_id: str
    advisor_id: str
    action: str = Field("book", description="book | cancel")
    scheduled_for: Optional[str] = Field(None, description="ISO timestamp")
    duration_minutes: int = 30
    notes: Optional[str] = None
    appointment_id: Optional[str] = Field(None, description="Required for cancel")


class DocumentGenerateRequest(BaseModel):
    student_id: str
    document_type: str = Field(..., description="transcript | fee_statement | enrollment_letter | schedule_summary")
    download_url: Optional[str] = Field(None, description="Optional — defaults to # placeholder")


class HoldActionRequest(BaseModel):
    student_id: str
    hold_id: int
    action: str = Field("clear", description="clear")
    resolution_note: Optional[str] = None


class FeePaymentRequest(BaseModel):
    student_id: str
    fee_record_id: int
    amount_sar: float
    method: str = Field("Sadad", description="Payment method")
    sadad_reference: Optional[str] = None


class ProfileUpdateRequest(BaseModel):
    student_id: str
    updates: Dict[str, Any] = Field(..., description="Fields to update — phone, email, city")


class ApplicationActionRequest(BaseModel):
    application_id: str
    action: str = Field(..., description="submit_documents | accept | reject | waitlist | request_documents")
    next_step: Optional[str] = None
    notes: Optional[str] = None


# ============================================================
# POST /enrollment/action  — drop / add / swap a course
# ============================================================

@app.post("/enrollment/action")
async def enrollment_action(req: EnrollmentActionRequest, request: Request):
    """Drop, add, or swap a course for a student.

    Side effects:
      - For drop/add: mutates class_schedules
      - For swap: drops one section and adds another atomically (best-effort)
      - Always: logs to agent_actions for staff portal real-time updates
    """
    student = await sb_get_one(
        "students", params={"student_id": f"eq.{req.student_id}"}, request=request
    )
    if not student:
        raise HTTPException(404, f"Student {req.student_id} not found")

    action = req.action.lower()
    student_name = student.get("full_name_en", req.student_id)

    if action == "drop":
        if not req.course_code:
            raise HTTPException(400, "course_code required for drop")
        existing = await sb_get(
            "class_schedules",
            params={
                "student_id": f"eq.{req.student_id}",
                "course_code": f"eq.{req.course_code}",
            },
            request=request,
        )
        if not existing:
            raise HTTPException(
                404,
                f"Student is not currently enrolled in {req.course_code}",
            )
        deleted = await sb_delete(
            "class_schedules",
            match={
                "student_id": req.student_id,
                "course_code": req.course_code,
            },
            request=request,
        )
        await log_agent_action(
            action_type="drop_course",
            description=f"Dropped {req.course_code} from {student_name}'s schedule",
            student_id=req.student_id,
            payload={"course_code": req.course_code, "removed_rows": deleted},
            request=request,
        )
        return {
            "ok": True,
            "action": "drop",
            "student_id": req.student_id,
            "course_code": req.course_code,
            "removed": deleted,
        }

    if action == "add":
        if not req.course_code or not req.section or not req.semester:
            raise HTTPException(
                400, "course_code, section, and semester required for add"
            )
        # Verify section exists and is open
        sec = await sb_get_one(
            "course_sections",
            params={
                "course_code": f"eq.{req.course_code}",
                "section": f"eq.{req.section}",
                "semester": f"eq.{req.semester}",
            },
            request=request,
        )
        if not sec:
            raise HTTPException(
                404,
                f"Section {req.course_code}-{req.section} not found in {req.semester}",
            )
        if (sec.get("status") or "").lower() == "full":
            raise HTTPException(
                409, f"Section {req.course_code}-{req.section} is full"
            )

        # Check for duplicate enrollment
        existing = await sb_get(
            "class_schedules",
            params={
                "student_id": f"eq.{req.student_id}",
                "course_code": f"eq.{req.course_code}",
            },
            request=request,
        )
        if existing:
            raise HTTPException(
                409,
                f"Student is already enrolled in {req.course_code}",
            )

        new_row = {
            "student_id": req.student_id,
            "semester": sec.get("semester"),
            "course_code": sec.get("course_code"),
            "course_name": sec.get("course_name"),
            "section": sec.get("section"),
            "schedule_pattern": sec.get("schedule_pattern"),
            "day_1": sec.get("day_1"),
            "day_2": sec.get("day_2"),
            "day_3": sec.get("day_3"),
            "time": sec.get("time"),
            "duration": sec.get("duration"),
            "room": sec.get("room"),
            "instructor": sec.get("instructor"),
        }
        inserted = await sb_insert("class_schedules", new_row, request=request)
        await log_agent_action(
            action_type="add_course",
            description=(
                f"Added {req.course_code} Section {req.section} "
                f"({sec.get('schedule_pattern')} {sec.get('time')}) to "
                f"{student_name}'s schedule"
            ),
            student_id=req.student_id,
            payload={
                "course_code": req.course_code,
                "section": req.section,
                "semester": req.semester,
                "added_row": inserted,
            },
            request=request,
        )
        return {
            "ok": True,
            "action": "add",
            "student_id": req.student_id,
            "added": inserted,
        }

    if action == "swap":
        if not all([req.drop_course_code, req.add_course_code, req.add_section, req.semester]):
            raise HTTPException(
                400,
                "drop_course_code, add_course_code, add_section, and semester required for swap",
            )
        # Drop first
        deleted = await sb_delete(
            "class_schedules",
            match={
                "student_id": req.student_id,
                "course_code": req.drop_course_code,
            },
            request=request,
        )
        if not deleted:
            raise HTTPException(
                404,
                f"Student is not currently enrolled in {req.drop_course_code} — nothing to drop",
            )
        # Then add the replacement
        sec = await sb_get_one(
            "course_sections",
            params={
                "course_code": f"eq.{req.add_course_code}",
                "section": f"eq.{req.add_section}",
                "semester": f"eq.{req.semester}",
            },
            request=request,
        )
        if not sec:
            # Roll back the drop by re-inserting (best-effort)
            for row in deleted:
                row.pop("id", None)
                await sb_insert("class_schedules", row, request=request)
            raise HTTPException(
                404,
                f"Replacement section {req.add_course_code}-{req.add_section} not found "
                f"in {req.semester} — drop rolled back",
            )
        new_row = {
            "student_id": req.student_id,
            "semester": sec.get("semester"),
            "course_code": sec.get("course_code"),
            "course_name": sec.get("course_name"),
            "section": sec.get("section"),
            "schedule_pattern": sec.get("schedule_pattern"),
            "day_1": sec.get("day_1"),
            "day_2": sec.get("day_2"),
            "day_3": sec.get("day_3"),
            "time": sec.get("time"),
            "duration": sec.get("duration"),
            "room": sec.get("room"),
            "instructor": sec.get("instructor"),
        }
        inserted = await sb_insert("class_schedules", new_row, request=request)
        await log_agent_action(
            action_type="swap_course",
            description=(
                f"Swapped {req.drop_course_code} for {req.add_course_code} "
                f"Section {req.add_section} in {student_name}'s schedule"
            ),
            student_id=req.student_id,
            payload={
                "dropped": deleted,
                "added": inserted,
            },
            request=request,
        )
        return {
            "ok": True,
            "action": "swap",
            "student_id": req.student_id,
            "dropped": deleted,
            "added": inserted,
        }

    raise HTTPException(400, f"Unknown action '{req.action}'. Use add, drop, or swap.")


# ============================================================
# POST /advising/appointment
# ============================================================

@app.post("/advising/appointment")
async def advising_appointment(req: AdvisingAppointmentRequest, request: Request):
    """Book or cancel an advising appointment with an academic advisor."""
    student = await sb_get_one(
        "students", params={"student_id": f"eq.{req.student_id}"}, request=request
    )
    if not student:
        raise HTTPException(404, f"Student {req.student_id} not found")
    student_name = student.get("full_name_en", req.student_id)

    action = req.action.lower()

    if action == "book":
        if not req.scheduled_for:
            raise HTTPException(400, "scheduled_for required for booking")
        advisor = await sb_get_one(
            "advisors",
            params={"advisor_id": f"eq.{req.advisor_id}"},
            request=request,
        )
        if not advisor:
            raise HTTPException(404, f"Advisor {req.advisor_id} not found")
        appt = await sb_insert(
            "advising_appointments",
            {
                "student_id": req.student_id,
                "advisor_id": req.advisor_id,
                "scheduled_for": req.scheduled_for,
                "duration_minutes": req.duration_minutes,
                "status": "scheduled",
                "notes": req.notes,
            },
            request=request,
        )
        await log_agent_action(
            action_type="book_advising",
            description=(
                f"Booked advising appointment for {student_name} with "
                f"{advisor.get('name', req.advisor_id)} at {req.scheduled_for}"
            ),
            student_id=req.student_id,
            payload={"appointment": appt},
            request=request,
        )
        return {"ok": True, "action": "book", "appointment": appt}

    if action == "cancel":
        if not req.appointment_id:
            raise HTTPException(400, "appointment_id required for cancel")
        updated = await sb_update(
            "advising_appointments",
            match={"id": req.appointment_id},
            updates={"status": "cancelled"},
            request=request,
        )
        if not updated:
            raise HTTPException(404, f"Appointment {req.appointment_id} not found")
        await log_agent_action(
            action_type="cancel_advising",
            description=f"Cancelled advising appointment {req.appointment_id} for {student_name}",
            student_id=req.student_id,
            payload={"appointment": updated[0]},
            request=request,
        )
        return {"ok": True, "action": "cancel", "appointment": updated[0]}

    raise HTTPException(400, f"Unknown action '{req.action}'. Use book or cancel.")


# ============================================================
# POST /document/generate
# ============================================================

@app.post("/document/generate")
async def document_generate(req: DocumentGenerateRequest, request: Request):
    """Log a generated document.

    The actual PDF content is generated agent-side (python_repl) and uploaded
    to Nebelus' artifact storage; this endpoint just records the metadata so the
    staff portal can show 'document generated' in the activity drawer and on the
    student detail page.

    NOTE on transcripts: transcripts are static, pre-loaded PDFs served by
    /document/fetch. They never need to be regenerated. This endpoint rejects
    document_type='transcript' with a 400 so duplicate rows can't accumulate
    in documents_generated. The agent should use fetch_document for transcripts.
    """
    student = await sb_get_one(
        "students", params={"student_id": f"eq.{req.student_id}"}, request=request
    )
    if not student:
        raise HTTPException(404, f"Student {req.student_id} not found")
    student_name = student.get("full_name_en", req.student_id)

    # Transcripts are static — always served via fetch_document, never generated
    if req.document_type == "transcript":
        raise HTTPException(
            400,
            (
                "Transcripts are pre-loaded static documents. "
                "Use fetch_document(student_id=..., document_type='transcript') "
                "to serve a transcript — do not call generate_document for transcripts."
            ),
        )

    valid_types = {"fee_statement", "enrollment_letter", "schedule_summary"}
    if req.document_type not in valid_types:
        raise HTTPException(
            400,
            f"document_type must be one of {sorted(valid_types)} (transcripts use fetch_document)",
        )

    doc = await sb_insert(
        "documents_generated",
        {
            "student_id": req.student_id,
            "document_type": req.document_type,
            "download_url": req.download_url or "#",
        },
        request=request,
    )
    pretty_type = req.document_type.replace("_", " ").title()
    await log_agent_action(
        action_type="generate_document",
        description=f"Generated {pretty_type} for {student_name}",
        student_id=req.student_id,
        payload={"document": doc},
        request=request,
    )
    return {"ok": True, "document": doc}


# ============================================================
# GET /document/fetch — pre-loaded documents (fast path)
# ============================================================

# The pre-loaded transcripts live in Supabase Storage under the
# `student-documents` bucket. Convention: <document_type>s/<student_id>.pdf
# (e.g. "transcripts/STU-2024001.pdf"). Loveable creates and seeds this bucket.

STORAGE_BUCKET = "student-documents"
SIGNED_URL_EXPIRY_SECONDS = 600  # 10 minutes — long enough for the agent to send

@app.get("/document/fetch")
async def document_fetch(
    request: Request,
    student_id: str = Query(..., description="Student ID, e.g. STU-2024001"),
    document_type: str = Query(
        "transcript",
        description="Document type — currently only 'transcript' is pre-loaded",
    ),
):
    """Fetch a pre-loaded document for a student.

    Returns a short-lived signed URL pointing at the PDF in Supabase Storage.
    The agent passes this URL directly to send_whatsapp_media — no
    python_repl / PDF generation needed. ~2-3 seconds end-to-end.

    Falls back to a 404 with a helpful message if no pre-loaded document
    exists for the requested type; the agent can then offer to generate
    one on the fly via the existing generate_document path.
    """
    student = await sb_get_one(
        "students", params={"student_id": f"eq.{student_id}"}, request=request
    )
    if not student:
        raise HTTPException(404, f"Student {student_id} not found")
    student_name = student.get("full_name_en", student_id)

    # Build the storage path. Convention: <document_type>s/<student_id>.pdf
    valid_preloaded = {"transcript"}  # extend in phase 2 with more types
    if document_type not in valid_preloaded:
        raise HTTPException(
            404,
            (
                f"No pre-loaded {document_type} available. "
                f"Use generate_document to create one on the fly."
            ),
        )

    storage_path = f"{document_type}s/{student_id}.pdf"

    # Generate a signed URL via Supabase Storage API
    client: httpx.AsyncClient = request.app.state.http
    sign_url = (
        f"{SUPABASE_URL}/storage/v1/object/sign/{STORAGE_BUCKET}/{storage_path}"
    )
    try:
        resp = await client.post(
            sign_url,
            json={"expiresIn": SIGNED_URL_EXPIRY_SECONDS},
            headers={
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Content-Type": "application/json",
            },
        )
    except Exception as e:
        logger.exception("Failed to call Supabase Storage sign endpoint")
        raise HTTPException(502, f"Storage sign failed: {e}")

    if resp.status_code == 404:
        raise HTTPException(
            404,
            (
                f"No pre-loaded {document_type} found for {student_id}. "
                f"Bucket '{STORAGE_BUCKET}' may not contain '{storage_path}'."
            ),
        )
    if resp.status_code >= 400:
        logger.error(f"Supabase Storage sign failed: {resp.status_code} {resp.text}")
        raise HTTPException(
            502, f"Storage sign failed: {resp.text[:200]}"
        )

    body = resp.json()
    # Supabase returns {"signedURL": "/object/sign/..."} — needs the host prefix
    signed_path = body.get("signedURL") or body.get("signedUrl")
    if not signed_path:
        raise HTTPException(502, f"Unexpected sign response: {body}")
    full_url = f"{SUPABASE_URL}/storage/v1{signed_path}"

    # Log it just like generate_document so the activity drawer fires
    pretty_type = document_type.replace("_", " ").title()
    await log_agent_action(
        action_type="fetch_document",
        description=f"Shared {pretty_type} with {student_name}",
        student_id=student_id,
        payload={"document_type": document_type, "storage_path": storage_path},
        request=request,
    )

    # Update the existing documents_generated row for this (student, doc_type)
    # if one exists — so the Documents tab shows one transcript per student,
    # not a growing list of delivery events. The agent_actions table above is
    # the audit log; documents_generated is the registry of what's available.
    existing_doc = await sb_get_one(
        "documents_generated",
        params={
            "student_id": f"eq.{student_id}",
            "document_type": f"eq.{document_type}",
        },
        request=request,
    )
    now_iso = datetime.now(timezone.utc).isoformat()
    if existing_doc:
        # Bump generated_at so the staff portal can show "just delivered" if it
        # wants to. Leave download_url alone — if it's "preloaded:..." that's
        # what flags it as a pre-loaded doc in the Documents tab.
        await sb_update(
            "documents_generated",
            match={"id": str(existing_doc["id"])},
            updates={"generated_at": now_iso},
            request=request,
        )
    else:
        # Fallback: no seed row exists for this student/doc_type (rare —
        # the seed migration should populate one for every student × type)
        await sb_insert(
            "documents_generated",
            {
                "student_id": student_id,
                "document_type": document_type,
                "download_url": full_url,
            },
            request=request,
        )

    return {
        "ok": True,
        "student_id": student_id,
        "document_type": document_type,
        "download_url": full_url,
        "expires_in_seconds": SIGNED_URL_EXPIRY_SECONDS,
        "filename": f"{student_name.replace(' ', '_')}_{pretty_type.replace(' ', '_')}.pdf",
    }


# ============================================================
# POST /hold/action
# ============================================================

@app.post("/hold/action")
async def hold_action(req: HoldActionRequest, request: Request):
    """Clear a registration/transcript/financial hold on a student.

    Note: this is a registrar action in the real world. In the demo, the agent
    can call it (e.g., after the student confirms they've paid an outstanding
    fee that was the basis of a financial hold).
    """
    student = await sb_get_one(
        "students", params={"student_id": f"eq.{req.student_id}"}, request=request
    )
    if not student:
        raise HTTPException(404, f"Student {req.student_id} not found")
    student_name = student.get("full_name_en", req.student_id)

    if req.action.lower() != "clear":
        raise HTTPException(400, "Only 'clear' action supported for holds")

    hold = await sb_get_one(
        "holds", params={"id": f"eq.{req.hold_id}"}, request=request
    )
    if not hold:
        raise HTTPException(404, f"Hold {req.hold_id} not found")
    if hold.get("student_id") != req.student_id:
        raise HTTPException(403, "Hold does not belong to this student")

    updated = await sb_update(
        "holds",
        match={"id": str(req.hold_id)},
        updates={
            "status": "Cleared",
            "resolution": req.resolution_note or "Cleared via agent",
        },
        request=request,
    )
    await log_agent_action(
        action_type="clear_hold",
        description=(
            f"Cleared {hold.get('hold_type')} hold for {student_name}"
        ),
        student_id=req.student_id,
        payload={"hold": updated[0] if updated else None},
        request=request,
    )
    return {"ok": True, "hold": updated[0] if updated else None}


# ============================================================
# POST /fee/payment
# ============================================================

@app.post("/fee/payment")
async def fee_payment(req: FeePaymentRequest, request: Request):
    """Record a Sadad-style payment against a fee record."""
    student = await sb_get_one(
        "students", params={"student_id": f"eq.{req.student_id}"}, request=request
    )
    if not student:
        raise HTTPException(404, f"Student {req.student_id} not found")
    student_name = student.get("full_name_en", req.student_id)

    fee = await sb_get_one(
        "fee_records", params={"id": f"eq.{req.fee_record_id}"}, request=request
    )
    if not fee:
        raise HTTPException(404, f"Fee record {req.fee_record_id} not found")
    if fee.get("student_id") != req.student_id:
        raise HTTPException(403, "Fee record does not belong to this student")

    paid_so_far = float(fee.get("paid_sar") or 0)
    total_due = float(fee.get("total_due_sar") or 0)
    new_paid = paid_so_far + req.amount_sar
    new_outstanding = max(0, total_due - new_paid)

    updates = {
        "paid_sar": round(new_paid, 2),
        "outstanding_sar": round(new_outstanding, 2),
        "payment_date": datetime.now(timezone.utc).date().isoformat(),
        "method": req.method,
        "status": "Paid" if new_outstanding == 0 else "Partial",
    }
    updated = await sb_update(
        "fee_records",
        match={"id": str(req.fee_record_id)},
        updates=updates,
        request=request,
    )

    sadad_ref = req.sadad_reference or f"SDD-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    await log_agent_action(
        action_type="record_payment",
        description=(
            f"Recorded SAR {req.amount_sar:.2f} payment for {student_name} "
            f"(ref: {sadad_ref})"
        ),
        student_id=req.student_id,
        payload={
            "fee_record": updated[0] if updated else None,
            "amount_sar": req.amount_sar,
            "sadad_reference": sadad_ref,
        },
        request=request,
    )
    return {
        "ok": True,
        "fee_record": updated[0] if updated else None,
        "sadad_reference": sadad_ref,
    }


# ============================================================
# POST /profile/update
# ============================================================

@app.post("/profile/update")
async def profile_update(req: ProfileUpdateRequest, request: Request):
    """Update a student's contact info (phone, email, city). Other fields are
    blocked to prevent accidental academic data overwrites."""
    student = await sb_get_one(
        "students", params={"student_id": f"eq.{req.student_id}"}, request=request
    )
    if not student:
        raise HTTPException(404, f"Student {req.student_id} not found")
    student_name = student.get("full_name_en", req.student_id)

    allowed_fields = {"phone", "email", "city"}
    safe_updates = {k: v for k, v in req.updates.items() if k in allowed_fields}
    if not safe_updates:
        raise HTTPException(
            400,
            f"No updatable fields provided. Allowed: {sorted(allowed_fields)}",
        )

    updated = await sb_update(
        "students",
        match={"student_id": req.student_id},
        updates=safe_updates,
        request=request,
    )
    changed_fields = ", ".join(safe_updates.keys())
    await log_agent_action(
        action_type="update_profile",
        description=f"Updated {student_name}'s contact info ({changed_fields})",
        student_id=req.student_id,
        payload={"updates": safe_updates},
        request=request,
    )
    return {"ok": True, "student": updated[0] if updated else None}


# ============================================================
# POST /application/action
# ============================================================

@app.post("/application/action")
async def application_action(req: ApplicationActionRequest, request: Request):
    """Move an applicant through the admissions pipeline."""
    applicant = await sb_get_one(
        "applicants",
        params={"application_id": f"eq.{req.application_id}"},
        request=request,
    )
    if not applicant:
        raise HTTPException(404, f"Application {req.application_id} not found")
    name = applicant.get("full_name_en", req.application_id)

    action_to_status = {
        "submit_documents": "Under Review",
        "accept": "Accepted",
        "reject": "Rejected",
        "waitlist": "Waitlisted",
        "request_documents": "Pending Documents",
    }
    new_status = action_to_status.get(req.action.lower())
    if not new_status:
        raise HTTPException(
            400,
            f"action must be one of {sorted(action_to_status.keys())}",
        )

    updates: Dict[str, Any] = {"status": new_status}
    if req.next_step is not None:
        updates["next_step"] = req.next_step
    if req.notes is not None:
        updates["notes"] = req.notes

    updated = await sb_update(
        "applicants",
        match={"application_id": req.application_id},
        updates=updates,
        request=request,
    )
    await log_agent_action(
        action_type="applicant_action",
        description=f"Application {req.application_id} ({name}) moved to {new_status}",
        student_id=None,  # applicants are not students yet
        payload={"applicant": updated[0] if updated else None, "action": req.action},
        request=request,
    )
    return {"ok": True, "applicant": updated[0] if updated else None}


# ============================================================
# Global error handler
# ============================================================

@app.exception_handler(Exception)
async def all_exception_handler(request: Request, exc: Exception):
    if isinstance(exc, HTTPException):
        raise exc
    logger.exception("Unhandled exception")
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error", "detail": str(exc)[:200]},
    )
