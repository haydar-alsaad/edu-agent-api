"""
Education Agent API v3.0.2 (Supabase-backed, multi-tenant).

CHANGES IN v3.0.2:
  - Enrollment actions now keep `class_schedules` and `grades` CONSISTENT.
    Previously drop/add/swap mutated only class_schedules, so a dropped course
    stayed "In Progress" in grades (surfacing in in_progress_grades) and an
    added course had no grades row at all. The agent reads both fields, so the
    data itself was contradictory and no SI rule could fix it.
  - Credits for a new in-progress grade resolve from the shared `courses`
    catalog rather than defaulting to a constant — academics.completed_credits
    sums this field, so a wrong default silently corrupts the credit total.
  - Dropping never deletes a Completed grade. If a Completed and an In Progress
    row share a course code (a retake), the delete targets the specific row id.
  - Adding an in-progress grade is idempotent — it reuses an existing orphan
    rather than creating a duplicate. This matters for tenants that already
    drifted before this fix.
  - `swap` now validates all preconditions BEFORE mutating anything. Rollback
    is a last resort rather than the expected path for a bad section code, and
    it restores both tables.

CHANGES IN v3.0.1:
  - /health now DERIVES `status` from its checks instead of hardcoding
    "healthy". The previous version reported healthy while checks.supabase
    held a 500, hiding a fully broken deployment. It also short-circuits with
    an explicit reason when DEFAULT_OWNER_ID is unset, which was the actual
    failure it was masking.
  - No other changes. Endpoints, tenancy scoping, and tool contracts identical.

CHANGES FROM v2.1 — MULTI-TENANCY:
  - Every endpoint accepts `caller_phone` (the WhatsApp sender's number from the
    agent's [User WhatsApp:] metadata). Railway resolves it to an `owner_id` via
    the `demo_users` table and scopes every per-tenant Supabase query by it.
  - Falls back to DEFAULT_OWNER_ID when caller_phone is missing or unresolvable,
    so agents that haven't been updated yet keep working against the shared
    default demo tenant. Zero-downtime rollout.
  - sb_get / sb_get_one / sb_insert / sb_update / sb_delete take an explicit
    `owner` argument. If the table is per-tenant and `owner` is None they raise —
    failing loudly beats silently returning another tenant's rows.
  - Phone normalization tolerates a missing "+" and strips spaces/dashes.

CARRIED FORWARD FROM v2.1:
  - HTTP/2 on the httpx client
  - In-process reference cache (60s TTL) for SHARED catalogs: advisors, faculty,
    degree_programs, degree_requirement_courses, courses. These have no owner_id,
    so the cache stays global and is NOT per-tenant.

TENANCY MODEL:
  Per-tenant tables (scoped by owner_id):
    students, grades, class_schedules, fee_records, holds,
    advising_appointments, documents_generated, applicants, agent_actions,
    course_sections, course_offerings_summary, exam_schedule
  Shared tables (no owner_id, one copy for everyone):
    advisors, faculty, courses, degree_programs, degree_requirement_courses,
    academic_calendar

  NOTE on course_sections: moved to per-tenant so two sales people demoing
  enrollment into the same section don't collide on seat counts.
  course_offerings_summary follows it — it summarizes section availability, so
  a shared summary next to per-tenant sections would contradict itself.

ENV VARS:
  SUPABASE_URL                 (required)
  SUPABASE_SERVICE_ROLE_KEY    (required)
  DEFAULT_OWNER_ID             (required) UUID of the fallback demo tenant
  PLANNING_SEMESTER_DEFAULT    (optional, default "Fall 2026")

Endpoints (paths preserved):
  GET  /                         API info
  GET  /health                   Status + cache stats
  GET  /student                  Workhorse student profile (the single call)
  GET  /applicant                Applicant lookup
  GET  /course                   Course catalog with sections
  GET  /faculty                  Faculty lookup (with advisor routing hint)
  GET  /advisor                  Advisor lookup (with faculty enrichment)
  GET  /calendar                 Academic calendar events
  GET  /degree-requirements      Program requirements
  GET  /exam-schedule            Exam lookup
  GET  /document/fetch           Fetch a pre-loaded document (transcript)
  POST /enrollment/action        Drop / add / swap a course
  POST /advising/appointment     Book / cancel an advising appointment
  POST /document/generate        Log a generated document
  POST /hold/action              Clear a hold
  POST /fee/payment              Record a Sadad-style payment
  POST /profile/update           Update student contact info
  POST /application/action       Move applicant status / mark docs received

Auth: this is a demo API. No authentication on the API itself. The Supabase
service role key (server-side only) is the only credential. Tenant isolation is
enforced in application code via owner_id scoping, not RLS (Railway uses the
service role, which bypasses RLS by design).
"""

import os
import json
import asyncio
import logging
import re
from datetime import datetime, timezone
from time import monotonic as _monotonic
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
DEFAULT_OWNER_ID = os.environ.get("DEFAULT_OWNER_ID", "")
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

if not DEFAULT_OWNER_ID:
    logger.warning(
        "DEFAULT_OWNER_ID not set. Requests without a resolvable caller_phone "
        "will fail. Set this to the UUID of the fallback demo tenant."
    )


# ============================================================
# Tenancy: which tables carry owner_id
# ============================================================

TENANT_TABLES = {
    "students",
    "grades",
    "class_schedules",
    "fee_records",
    "holds",
    "advising_appointments",
    "documents_generated",
    "applicants",
    "agent_actions",
    "course_sections",
    "course_offerings_summary",
    "exam_schedule",
}


# ============================================================
# Lifespan + HTTP client (HTTP/2 enabled)
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create a shared httpx client for the lifetime of the app.

    HTTP/2 is enabled so all parallel Supabase requests multiplex over a single
    TCP connection instead of hitting HTTP/1.1's ~6-concurrent-request cap.
    Requires `httpx[http2]` (installs the `h2` package) in requirements.txt.
    """
    async with httpx.AsyncClient(
        base_url=REST_BASE,
        headers=HEADERS_BASE,
        http2=True,
        timeout=httpx.Timeout(15.0),
        limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
    ) as client:
        app.state.http = client
        logger.info("HTTP client initialized (HTTP/2 enabled); ready to serve.")
        yield
        logger.info("HTTP client shutting down.")


app = FastAPI(
    title="KFUT Student Support API",
    version="3.0.2",
    description="Supabase-backed, multi-tenant API for the KFUT WhatsApp student support agent.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# Phone normalization + tenant resolution
# ============================================================

def normalize_phone(raw: Optional[str]) -> Optional[str]:
    """Canonicalize a phone number to E.164 with a leading '+'.

    Tolerates: missing '+', spaces, dashes, parentheses, leading '00'.
    Returns None for empty/unusable input.

    This exists because URL query strings sometimes drop or mangle the '+'
    (it URL-encodes to a space), and agents occasionally strip it. Normalizing
    on both write and read sides means the lookup matches regardless.
    """
    if not raw:
        return None
    s = re.sub(r"[\s\-()]", "", str(raw).strip())
    if not s:
        return None
    if s.startswith("00"):
        s = "+" + s[2:]
    elif not s.startswith("+"):
        s = "+" + s
    if not re.fullmatch(r"\+\d{6,20}", s):
        return None
    return s


# Tenant resolution cache. Bindings change rarely (a sales person sets their
# demo number once), so a longer TTL than the reference cache is fine.
_TENANT_CACHE_TTL = 300.0  # 5 minutes
_tenant_cache: Dict[str, Dict[str, Any]] = {}  # normalized_phone -> {owner_id, ts}


async def resolve_owner(
    caller_phone: Optional[str],
    request: Optional[Request] = None,
) -> str:
    """Resolve a WhatsApp phone number to the owning demo tenant's owner_id.

    Falls back to DEFAULT_OWNER_ID when:
      - caller_phone is missing (agent not yet updated with the parameter)
      - caller_phone doesn't match any demo_users row (sales person hasn't
        registered their demo number yet)

    The fallback is deliberate: a demo that silently lands in the shared
    default tenant is recoverable; a hard 400 mid-demo in front of a prospect
    is not.
    """
    normalized = normalize_phone(caller_phone)
    if not normalized:
        return DEFAULT_OWNER_ID

    cached = _tenant_cache.get(normalized)
    if cached and (_monotonic() - cached["ts"]) <= _TENANT_CACHE_TTL:
        return cached["owner_id"]

    # demo_users is NOT a per-tenant table — it's the tenant registry itself.
    rows = await _sb_raw_get(
        "demo_users",
        {"whatsapp_number": f"eq.{normalized}", "select": "owner_id", "limit": "1"},
        request=request,
    )
    owner = rows[0]["owner_id"] if rows else DEFAULT_OWNER_ID

    _tenant_cache[normalized] = {"owner_id": owner, "ts": _monotonic()}
    return owner


def _tenant_cache_stats() -> Dict[str, Any]:
    """Diagnostic: how many phone→tenant bindings are currently cached."""
    now = _monotonic()
    return {
        "entries": len(_tenant_cache),
        "oldest_age_seconds": (
            round(now - min(v["ts"] for v in _tenant_cache.values()), 1)
            if _tenant_cache else None
        ),
    }


# ============================================================
# Supabase REST helpers (tenant-aware)
# ============================================================

async def _sb_raw_get(
    table: str,
    params: Optional[Dict[str, Any]] = None,
    request: Optional[Request] = None,
) -> List[Dict[str, Any]]:
    """Unscoped GET. ONLY for non-tenant tables like demo_users.
    Do not use for anything in TENANT_TABLES."""
    client: httpx.AsyncClient = request.app.state.http if request else app.state.http
    resp = await client.get(f"/{table}", params=params or {})
    if resp.status_code >= 400:
        logger.error(f"Supabase GET /{table} failed: {resp.status_code} {resp.text}")
        raise HTTPException(
            status_code=502,
            detail=f"Database read failed: {resp.text[:200]}",
        )
    return resp.json()


def _scope_params(
    table: str,
    params: Optional[Dict[str, Any]],
    owner: Optional[str],
) -> Dict[str, Any]:
    """Inject owner_id filter for per-tenant tables.

    Raises loudly if a per-tenant table is queried without an owner. Silent
    cross-tenant reads are the worst possible failure mode here — better to
    500 and see it in the logs than to serve another sales person's demo data.
    """
    p = dict(params or {})
    if table in TENANT_TABLES:
        if not owner:
            raise HTTPException(
                status_code=500,
                detail=f"Internal error: query on per-tenant table '{table}' missing owner scope",
            )
        p["owner_id"] = f"eq.{owner}"
    return p


async def sb_get(
    table: str,
    params: Optional[Dict[str, Any]] = None,
    request: Optional[Request] = None,
    owner: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """SELECT against a Supabase table, scoped to the tenant for per-tenant tables."""
    return await _sb_raw_get(table, _scope_params(table, params, owner), request=request)


async def sb_get_one(
    table: str,
    params: Optional[Dict[str, Any]] = None,
    request: Optional[Request] = None,
    owner: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Convenience — fetch first matching row or None."""
    rows = await sb_get(table, params=params, request=request, owner=owner)
    return rows[0] if rows else None


async def sb_insert(
    table: str,
    record: Any,
    request: Optional[Request] = None,
    owner: Optional[str] = None,
) -> Any:
    """INSERT a row (or list of rows), injecting owner_id for per-tenant tables."""
    if table in TENANT_TABLES:
        if not owner:
            raise HTTPException(
                status_code=500,
                detail=f"Internal error: insert into per-tenant table '{table}' missing owner scope",
            )
        if isinstance(record, list):
            record = [{**row, "owner_id": owner} for row in record]
        else:
            record = {**record, "owner_id": owner}

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
    owner: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """UPDATE matching rows, scoped to the tenant for per-tenant tables."""
    params = {k: f"eq.{v}" for k, v in match.items()}
    params = _scope_params(table, params, owner)

    client: httpx.AsyncClient = request.app.state.http if request else app.state.http
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
    owner: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """DELETE matching rows, scoped to the tenant for per-tenant tables."""
    params = {k: f"eq.{v}" for k, v in match.items()}
    params = _scope_params(table, params, owner)

    client: httpx.AsyncClient = request.app.state.http if request else app.state.http
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
    owner: Optional[str] = None,
) -> None:
    """Write a row to agent_actions. Scoped to the tenant so each sales person
    sees only their own activity feed in the staff portal drawer."""
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
            owner=owner,
        )
    except Exception as e:
        # Log but don't fail the parent request just because logging failed
        logger.warning(f"Failed to log agent_action: {e}")


# ============================================================
# In-process cache for SHARED reference tables (60s TTL)
# ============================================================
#
# These tables are SHARED across all tenants — one copy of the catalog, no
# owner_id. So this cache stays global; it is NOT per-tenant and does not need
# invalidating when a tenant resets their demo.
#
# What's NOT cached (hits Supabase every call, tenant-scoped):
#   students, class_schedules, grades, fee_records, holds,
#   advising_appointments, course_sections, exam_schedule — anything
#   transactional or per-student that changes as the agent takes actions.

_REF_CACHE_TTL = 60.0  # seconds
_reference_cache: Dict[str, Dict[str, Any]] = {
    "advisors": {"data": None, "ts": 0.0},
    "faculty": {"data": None, "ts": 0.0},
    "degree_programs": {"data": None, "ts": 0.0},
    "degree_requirement_courses": {"data": None, "ts": 0.0},
    "courses": {"data": None, "ts": 0.0},
}


async def _get_ref_cached(
    table: str,
    request: Optional[Request] = None,
) -> List[Dict[str, Any]]:
    """Return a full SHARED reference table from cache or fetch+cache it. TTL 60s.

    Only for tables registered in _reference_cache above — all of which are
    shared catalogs with no owner_id. Callers filter in memory.
    """
    if table not in _reference_cache:
        raise ValueError(f"_get_ref_cached called for non-cached table: {table}")
    if table in TENANT_TABLES:
        raise ValueError(
            f"_get_ref_cached called for per-tenant table '{table}' — "
            "this cache is global and would leak data across tenants"
        )
    c = _reference_cache[table]
    if c["data"] is None or (_monotonic() - c["ts"]) > _REF_CACHE_TTL:
        c["data"] = await sb_get(table, params=None, request=request)
        c["ts"] = _monotonic()
    return c["data"]


def _cache_stats() -> Dict[str, Any]:
    """Diagnostic: current cache warmth per shared reference table."""
    now = _monotonic()
    return {
        table: {
            "warm": entry["data"] is not None,
            "row_count": len(entry["data"]) if entry["data"] is not None else 0,
            "age_seconds": round(now - entry["ts"], 1) if entry["ts"] else None,
        }
        for table, entry in _reference_cache.items()
    }


# ============================================================
# Root / health
# ============================================================

@app.get("/")
async def root():
    return {
        "name": "KFUT Student Support API",
        "version": "3.0.2",
        "multi_tenant": True,
        "docs": "/docs",
        "health": "/health",
        "backend": "Supabase",
        "default_owner_configured": bool(DEFAULT_OWNER_ID),
    }


@app.get("/health")
async def health(request: Request):
    """Health check including a Supabase reachability test and cache stats.

    IMPORTANT: `status` is DERIVED from the checks, never hardcoded. An earlier
    version always returned "healthy" while checks.supabase contained a 500,
    which hid a completely broken deployment behind a green status for the
    better part of an hour. If you add a check here, fold it into `healthy`.
    """
    if not DEFAULT_OWNER_ID:
        return {
            "status": "degraded",
            "version": "3.0.2",
            "multi_tenant": True,
            "default_owner_configured": False,
            "reason": "DEFAULT_OWNER_ID not set — unregistered callers will fail",
            "checks": {"api": "ok", "supabase": "not_checked"},
            "reference_cache": _cache_stats(),
            "tenant_cache": _tenant_cache_stats(),
        }

    checks = {"api": "ok"}
    try:
        rows = await sb_get(
            "students",
            params={"select": "student_id", "limit": "1"},
            request=request,
            owner=DEFAULT_OWNER_ID,
        )
        checks["supabase"] = "ok" if rows else "no_data"
    except Exception as e:
        checks["supabase"] = f"error: {str(e)[:200]}"

    healthy = checks["supabase"] in ("ok", "no_data")

    return {
        "status": "ok" if healthy else "degraded",
        "version": "3.0.2",
        "multi_tenant": True,
        "default_owner_configured": True,
        "checks": checks,
        "reference_cache": _cache_stats(),
        "tenant_cache": _tenant_cache_stats(),
    }


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
    caller_phone: Optional[str] = Query(None, description="Demo tenant routing — WhatsApp sender number"),
):
    """Workhorse endpoint — full student package in one call, scoped to the tenant.

    Returns: profile, advisor contact, completed + in-progress grades with GPA,
    current schedule, upcoming exams, fees, holds, remaining required courses,
    eligible courses for the planning semester (prereqs validated, sections
    available).
    """
    owner = await resolve_owner(caller_phone, request=request)

    if not any([student_id, name, phone]):
        raise HTTPException(400, "Provide student_id, name, or phone")

    # 1. Find the student (within this tenant)
    if student_id:
        student = await sb_get_one(
            "students", params={"student_id": f"eq.{student_id}"},
            request=request, owner=owner,
        )
    elif phone:
        # Normalize so a missing '+' still matches; fall back to the raw value
        # for legacy rows stored without canonical formatting.
        normalized = normalize_phone(phone)
        student = None
        if normalized:
            student = await sb_get_one(
                "students", params={"phone": f"eq.{normalized}"},
                request=request, owner=owner,
            )
        if not student:
            student = await sb_get_one(
                "students", params={"phone": f"eq.{phone}"},
                request=request, owner=owner,
            )
    else:
        student = await sb_get_one(
            "students",
            params={"full_name_en": f"ilike.*{name}*", "limit": "1"},
            request=request, owner=owner,
        )

    if not student:
        raise HTTPException(404, "Student not found")

    sid = student["student_id"]
    semester = planning_semester or PLANNING_SEMESTER_DEFAULT

    # 2. Prefetch SHARED reference tables (global cache) + tenant-scoped
    # transactional data in parallel. HTTP/2 multiplexes all of it.
    (
        advisors_all,
        faculty_all,
        programs_all,
        reqs_all,
        courses_all,
        grades,
        current_schedule,
        fees,
        holds,
    ) = await asyncio.gather(
        _get_ref_cached("advisors", request=request),
        _get_ref_cached("faculty", request=request),
        _get_ref_cached("degree_programs", request=request),
        _get_ref_cached("degree_requirement_courses", request=request),
        _get_ref_cached("courses", request=request),
        sb_get(
            "grades",
            params={"student_id": f"eq.{sid}", "order": "semester.desc"},
            request=request, owner=owner,
        ),
        sb_get(
            "class_schedules",
            params={"student_id": f"eq.{sid}"},
            request=request, owner=owner,
        ),
        sb_get(
            "fee_records",
            params={"student_id": f"eq.{sid}", "order": "due_date.desc"},
            request=request, owner=owner,
        ),
        sb_get(
            "holds",
            params={"student_id": f"eq.{sid}"},
            request=request, owner=owner,
        ),
    )

    # Build in-memory lookup dicts for shared reference data
    advisors_by_faculty_id = {a["faculty_id"]: a for a in advisors_all if a.get("faculty_id")}
    faculty_by_id = {f["faculty_id"]: f for f in faculty_all if f.get("faculty_id")}
    programs_by_code = {p["program_code"]: p for p in programs_all if p.get("program_code")}
    courses_by_code = {c["course_code"]: c for c in courses_all if c.get("course_code")}

    # 3. Resolve advisor + advisor's faculty record from the cached tables.
    # NOTE: student.advisor holds a FACULTY_ID, not an advisor_id.
    advisor_faculty_id = student.get("advisor")
    advisor_record = advisors_by_faculty_id.get(advisor_faculty_id) if advisor_faculty_id else None
    advisor_faculty = faculty_by_id.get(advisor_faculty_id) if advisor_faculty_id else None

    # 4. Compute completed credits from grades; use stored gpa from students record
    completed = [g for g in grades if g.get("status") == "Completed"]
    in_progress = [g for g in grades if g.get("status") != "Completed"]
    total_credits = sum(int(g.get("credits") or 0) for g in completed)
    computed_gpa = student.get("gpa")

    # 5. Hold flags — surface registration/transcript blockers up top
    active_holds = [h for h in holds if (h.get("status") or "").lower() == "active"]
    holds_summary = {
        "active_count": len(active_holds),
        "blocks_registration": any(h.get("blocks_registration") for h in active_holds),
        "blocks_transcript": any(h.get("blocks_transcript") for h in active_holds),
        "active_holds": active_holds,
        "all_holds": holds,
    }

    # 6. Finances summary
    outstanding_total = sum(float(f.get("outstanding_sar") or 0) for f in fees)
    finances = {
        "outstanding_total_sar": round(outstanding_total, 2),
        "has_outstanding": outstanding_total > 0,
        "records": fees,
    }

    # 7. Upcoming exams — match the student's current course codes.
    # exam_schedule is per-tenant (cloned per demo), so scope it.
    upcoming_exams: List[Dict[str, Any]] = []
    enrolled_codes = list({s["course_code"] for s in current_schedule if s.get("course_code")})
    if enrolled_codes:
        exam_results = await asyncio.gather(*[
            sb_get(
                "exam_schedule",
                params={"course_code": f"eq.{code}"},
                request=request, owner=owner,
            )
            for code in enrolled_codes
        ])
        for ex in exam_results:
            upcoming_exams.extend(ex)

    # 8. Degree progress — what's left for the program
    remaining_required: List[Dict[str, Any]] = []
    eligible_for_planning: List[Dict[str, Any]] = []
    program_total_credits = 0

    if student.get("program_code"):
        program = programs_by_code.get(student["program_code"])
        all_reqs = [r for r in reqs_all if r.get("program_code") == student["program_code"]]

        program_total_credits = (program or {}).get("total_credits", 0) or 0

        completed_codes = {g["course_code"] for g in completed if g.get("course_code")}
        in_progress_codes = {g["course_code"] for g in in_progress if g.get("course_code")}
        scheduled_codes = {s["course_code"] for s in current_schedule if s.get("course_code")}

        for r in all_reqs:
            code = r.get("course_code")
            if code and code not in completed_codes:
                remaining_required.append(r)

        # Eligible for planning semester: remaining courses with at least one
        # Open section in the requested semester, not already enrolled.
        # Course metadata comes from the shared cached `courses` dict.
        # Sections are per-tenant and change with enrollments — fetched live.
        candidates = [
            r for r in remaining_required
            if r.get("course_code")
            and r["course_code"] not in scheduled_codes
            and r["course_code"] not in in_progress_codes
        ]

        if candidates:
            section_results = await asyncio.gather(*[
                sb_get(
                    "course_sections",
                    params={
                        "course_code": f"eq.{r['course_code']}",
                        "semester": f"eq.{semester}",
                    },
                    request=request, owner=owner,
                )
                for r in candidates
            ])

            for r, sections in zip(candidates, section_results):
                code = r["course_code"]
                open_sections = [
                    s for s in sections
                    if (s.get("status") or "").lower() in ("open", "nearly full")
                ]
                if open_sections:
                    course_meta = courses_by_code.get(code)
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
    caller_phone: Optional[str] = Query(None, description="Demo tenant routing — WhatsApp sender number"),
):
    """Admissions application status for prospective students."""
    owner = await resolve_owner(caller_phone, request=request)

    if not application_id and not national_id:
        raise HTTPException(400, "Provide application_id or national_id")

    params = (
        {"application_id": f"eq.{application_id}"}
        if application_id
        else {"national_id": f"eq.{national_id}"}
    )
    row = await sb_get_one("applicants", params=params, request=request, owner=owner)
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
    caller_phone: Optional[str] = Query(None, description="Demo tenant routing — WhatsApp sender number"),
):
    """Course details (shared catalog) + sections and offerings summary (per-tenant)."""
    owner = await resolve_owner(caller_phone, request=request)

    if not course_code and not department:
        raise HTTPException(400, "Provide course_code or department")

    # Course meta — SHARED catalog, no tenant scoping
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

    # Sections + offerings summary are PER-TENANT (seat counts change per demo).
    # Fetch them in parallel across all matched courses instead of sequentially.
    async def _sections_and_summary(code: str):
        section_params = {"course_code": f"eq.{code}"}
        offering_params = {"course_code": f"eq.{code}"}
        if semester:
            section_params["semester"] = f"eq.{semester}"
            offering_params["semester"] = f"eq.{semester}"
        return await asyncio.gather(
            sb_get("course_sections", params=section_params, request=request, owner=owner),
            sb_get("course_offerings_summary", params=offering_params, request=request, owner=owner),
        )

    fetched = await asyncio.gather(*[_sections_and_summary(c["course_code"]) for c in courses])

    results = []
    for c, (sections, summary) in zip(courses, fetched):
        if status_filter:
            sections = [
                s for s in sections
                if (s.get("status") or "").lower() == status_filter.lower()
            ]
        results.append({
            "course": c,
            "sections": sections,
            "offerings_summary": summary,
        })

    if course_code:
        return results[0]
    return {"courses": results, "count": len(results)}


# ============================================================
# GET /faculty (shared catalog, with advisor routing hint)
# ============================================================

@app.get("/faculty")
async def get_faculty_info(
    request: Request,
    faculty_id: Optional[str] = Query(None),
    name: Optional[str] = Query(None),
    department: Optional[str] = Query(None),
    caller_phone: Optional[str] = Query(None, description="Accepted for consistency; faculty is a shared catalog"),
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

    # advisors is also a shared catalog — pull it from cache instead of one
    # query per advisor-flagged faculty member.
    advisors_all = await _get_ref_cached("advisors", request=request)
    advisors_by_faculty_id = {a["faculty_id"]: a for a in advisors_all if a.get("faculty_id")}

    enriched = []
    for f in rows:
        record = dict(f)
        if f.get("is_advisor"):
            adv = advisors_by_faculty_id.get(f["faculty_id"])
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
# GET /advisor (shared catalog)
# ============================================================

@app.get("/advisor")
async def get_advisor_info(
    request: Request,
    advisor_id: Optional[str] = Query(None),
    faculty_id: Optional[str] = Query(None),
    department: Optional[str] = Query(None),
    caller_phone: Optional[str] = Query(None, description="Accepted for consistency; advisors is a shared catalog"),
):
    """Advisor lookup, enriched with the underlying faculty record (office
    hours, specialization, languages). Use for academic advising appointments —
    available_days/available_hours are advising-specific.

    IMPORTANT: student.advisor from /student is a FACULTY_ID. Call this with
    faculty_id=<student.advisor> to get the actual advisor_id needed for booking.
    """
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

    # faculty is a shared catalog — resolve enrichment from cache
    faculty_all = await _get_ref_cached("faculty", request=request)
    faculty_by_id = {f["faculty_id"]: f for f in faculty_all if f.get("faculty_id")}

    enriched = []
    for adv in rows:
        fac = faculty_by_id.get(adv.get("faculty_id")) if adv.get("faculty_id") else None
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
# GET /calendar (shared catalog)
# ============================================================

@app.get("/calendar")
async def get_academic_calendar(
    request: Request,
    semester: Optional[str] = Query(None),
    event_type: Optional[str] = Query(None),
    upcoming_only: bool = Query(False),
    caller_phone: Optional[str] = Query(None, description="Accepted for consistency; calendar is a shared catalog"),
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
# GET /degree-requirements (shared catalog)
# ============================================================

@app.get("/degree-requirements")
async def get_degree_requirements(
    request: Request,
    program_code: Optional[str] = Query(None),
    program_name: Optional[str] = Query(None),
    requirement_type: Optional[str] = Query(None),
    caller_phone: Optional[str] = Query(None, description="Accepted for consistency; programs is a shared catalog"),
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
# GET /exam-schedule (per-tenant)
# ============================================================

@app.get("/exam-schedule")
async def get_exam_schedule(
    request: Request,
    course_code: Optional[str] = Query(None),
    semester: Optional[str] = Query(None),
    exam_type: Optional[str] = Query(None),
    caller_phone: Optional[str] = Query(None, description="Demo tenant routing — WhatsApp sender number"),
):
    """Exam schedule lookup. Use for 'when is my X exam?' questions.
    NOTE: does not accept student_id. Student-specific exams come from
    /student.upcoming_exams (already resolved server-side)."""
    owner = await resolve_owner(caller_phone, request=request)

    params: Dict[str, Any] = {"order": "exam_date.asc"}
    if course_code:
        params["course_code"] = f"eq.{course_code}"
    if semester:
        params["semester"] = f"eq.{semester}"
    if exam_type:
        params["exam_type"] = f"eq.{exam_type}"

    exams = await sb_get("exam_schedule", params=params, request=request, owner=owner)
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

    # For swap: the course to drop + the section to add
    drop_course_code: Optional[str] = None
    drop_section: Optional[str] = None
    add_course_code: Optional[str] = None
    add_section: Optional[str] = None

    caller_phone: Optional[str] = Field(None, description="Demo tenant routing")


class AdvisingAppointmentRequest(BaseModel):
    student_id: str
    advisor_id: str
    action: str = Field("book", description="book | cancel")
    scheduled_for: Optional[str] = Field(None, description="ISO timestamp")
    duration_minutes: int = 30
    notes: Optional[str] = None
    appointment_id: Optional[str] = Field(None, description="Required for cancel")

    caller_phone: Optional[str] = Field(None, description="Demo tenant routing")


class DocumentGenerateRequest(BaseModel):
    student_id: str
    document_type: str = Field(..., description="fee_statement | enrollment_letter | schedule_summary")
    download_url: Optional[str] = Field(None, description="Optional — defaults to # placeholder")

    caller_phone: Optional[str] = Field(None, description="Demo tenant routing")


class HoldActionRequest(BaseModel):
    student_id: str
    hold_id: int
    action: str = Field("clear", description="clear")
    resolution_note: Optional[str] = None

    caller_phone: Optional[str] = Field(None, description="Demo tenant routing")


class FeePaymentRequest(BaseModel):
    student_id: str
    fee_record_id: int
    amount_sar: float
    method: str = Field("Sadad", description="Payment method")
    sadad_reference: Optional[str] = None

    caller_phone: Optional[str] = Field(None, description="Demo tenant routing")


class ProfileUpdateRequest(BaseModel):
    student_id: str
    updates: Dict[str, Any] = Field(..., description="Fields to update — phone, email, city")

    caller_phone: Optional[str] = Field(None, description="Demo tenant routing")


class ApplicationActionRequest(BaseModel):
    application_id: str
    action: str = Field(..., description="submit_documents | accept | reject | waitlist | request_documents")
    next_step: Optional[str] = None
    notes: Optional[str] = None

    caller_phone: Optional[str] = Field(None, description="Demo tenant routing")


# ============================================================
# Enrollment <-> grades consistency helpers
# ============================================================
# A student's enrolled courses surface through TWO tables:
#   current_schedule     <- class_schedules  (the class slot)
#   in_progress_grades   <- grades WHERE status != "Completed"
#
# Before v3.0.2 the enrollment actions mutated only class_schedules, so the
# two drifted: a dropped course vanished from the schedule but stayed
# "In Progress" in grades, and an added course appeared in the schedule with
# no grades row. The agent reads both fields and had no way to reconcile them.
#
# Every enrollment mutation now updates BOTH tables. The helpers below are the
# single implementation, used by drop, add, and swap.


async def _resolve_credits(
    course_code: str,
    sec: Optional[Dict[str, Any]],
    request: Optional[Request] = None,
) -> int:
    """Best available credit value for a course.

    course_sections does not reliably carry `credits`, and academics.
    completed_credits sums this field — so defaulting to a constant would
    quietly corrupt the credit total. Resolution order:
      1. the section row, if it happens to have it
      2. the shared `courses` catalog (cached, free on warm calls)
      3. 3, the KFUT standard, as a last resort
    """
    if sec and sec.get("credits"):
        try:
            return int(sec["credits"])
        except (TypeError, ValueError):
            pass

    try:
        courses_all = await _get_ref_cached("courses", request=request)
        for row in courses_all:
            if row.get("course_code") == course_code and row.get("credits"):
                return int(row["credits"])
    except Exception as e:
        logger.warning(f"credit lookup failed for {course_code}: {e}")

    return 3


async def _drop_in_progress_grade(
    student_id: str,
    course_code: str,
    request: Optional[Request] = None,
    owner: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Delete the non-Completed grades row for a dropped course.

    Returns the deleted rows so a caller can restore them on rollback.

    GUARD: only deletes when NO Completed grade exists for this course code.
    A Completed grade is the student's permanent academic record — dropping a
    current course must never erase a past grade for a same-coded course
    (retakes make this a real case, not a hypothetical).
    """
    grade_rows = await sb_get(
        "grades",
        params={
            "student_id": f"eq.{student_id}",
            "course_code": f"eq.{course_code}",
        },
        request=request, owner=owner,
    ) or []

    has_completed = any((g.get("status") or "") == "Completed" for g in grade_rows)
    in_progress = [g for g in grade_rows if (g.get("status") or "") != "Completed"]

    if not in_progress:
        return []

    if has_completed:
        # Both a Completed and an In Progress row exist for this code (retake).
        # A blanket delete would destroy the permanent record, so delete by row
        # id instead of by course_code.
        deleted: List[Dict[str, Any]] = []
        for g in in_progress:
            if g.get("id") is None:
                logger.warning(
                    f"cannot safely delete in-progress grade for {student_id}/{course_code}: "
                    "row has no id and a Completed grade exists — skipping"
                )
                continue
            rows = await sb_delete(
                "grades", match={"id": str(g["id"])}, request=request, owner=owner
            )
            deleted.extend(rows or [])
        return deleted

    return await sb_delete(
        "grades",
        match={"student_id": student_id, "course_code": course_code},
        request=request, owner=owner,
    ) or []


async def _add_in_progress_grade(
    student_id: str,
    sec: Dict[str, Any],
    request: Optional[Request] = None,
    owner: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Create the "In Progress" grades row for a newly added course.

    Idempotent: if a non-Completed grades row already exists for this course
    (e.g. an orphan left by a pre-v3.0.2 drop), it is reused rather than
    duplicated.
    """
    course_code = sec.get("course_code")

    existing = await sb_get(
        "grades",
        params={
            "student_id": f"eq.{student_id}",
            "course_code": f"eq.{course_code}",
        },
        request=request, owner=owner,
    ) or []
    if any((g.get("status") or "") != "Completed" for g in existing):
        logger.info(
            f"in-progress grade already present for {student_id}/{course_code} — not duplicating"
        )
        return None

    credits = await _resolve_credits(course_code, sec, request=request)

    return await sb_insert(
        "grades",
        {
            "student_id": student_id,
            "semester": sec.get("semester"),
            "course_code": course_code,
            "course_name": sec.get("course_name"),
            "credits": credits,
            "grade": None,
            "grade_points": None,
            "status": "In Progress",
        },
        request=request, owner=owner,
    )


async def _restore_grade_rows(
    rows: List[Dict[str, Any]],
    request: Optional[Request] = None,
    owner: Optional[str] = None,
) -> None:
    """Re-insert grades rows removed during an operation that later failed."""
    for row in rows or []:
        r = dict(row)
        r.pop("id", None)
        r.pop("owner_id", None)  # re-injected by sb_insert
        try:
            await sb_insert("grades", r, request=request, owner=owner)
        except Exception as e:
            logger.error(f"failed to restore grades row on rollback: {e}")


# ============================================================
# POST /enrollment/action  — drop / add / swap a course
# ============================================================

@app.post("/enrollment/action")
async def enrollment_action(req: EnrollmentActionRequest, request: Request):
    """Drop, add, or swap a course for a student, within the caller's tenant.

    Side effects:
      - drop/add: mutates class_schedules
      - swap: drops one course and adds another (best-effort atomic, with rollback)
      - always: logs to agent_actions for staff portal real-time updates
    """
    owner = await resolve_owner(req.caller_phone, request=request)

    student = await sb_get_one(
        "students", params={"student_id": f"eq.{req.student_id}"},
        request=request, owner=owner,
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
            request=request, owner=owner,
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
            request=request, owner=owner,
        )

        # Keep grades in sync — a dropped course must leave BOTH tables,
        # otherwise it lingers in in_progress_grades and the student appears
        # enrolled in a course that's gone from their schedule.
        dropped_grades = await _drop_in_progress_grade(
            req.student_id, req.course_code, request=request, owner=owner
        )

        await log_agent_action(
            action_type="drop_course",
            description=f"Dropped {req.course_code} from {student_name}'s schedule",
            student_id=req.student_id,
            payload={
                "course_code": req.course_code,
                "removed_rows": deleted,
                "removed_grade_rows": dropped_grades,
            },
            request=request, owner=owner,
        )

        return {
            "ok": True,
            "action": "drop",
            "student_id": req.student_id,
            "course_code": req.course_code,
            "removed": deleted,
            "removed_grades": dropped_grades,
        }

    if action == "add":
        if not req.course_code or not req.section or not req.semester:
            raise HTTPException(
                400, "course_code, section, and semester required for add"
            )

        # Verify section exists and is open (per-tenant section grid)
        sec = await sb_get_one(
            "course_sections",
            params={
                "course_code": f"eq.{req.course_code}",
                "section": f"eq.{req.section}",
                "semester": f"eq.{req.semester}",
            },
            request=request, owner=owner,
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
            request=request, owner=owner,
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
        inserted = await sb_insert("class_schedules", new_row, request=request, owner=owner)

        # Keep grades in sync — an added course must appear in BOTH tables.
        added_grade = await _add_in_progress_grade(
            req.student_id, sec, request=request, owner=owner
        )

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
                "added_grade_row": added_grade,
            },
            request=request, owner=owner,
        )

        return {
            "ok": True,
            "action": "add",
            "student_id": req.student_id,
            "added": inserted,
            "added_grade": added_grade,
        }

    if action == "swap":
        if not all([req.drop_course_code, req.add_course_code, req.add_section, req.semester]):
            raise HTTPException(
                400,
                "drop_course_code, add_course_code, add_section, and semester required for swap",
            )

        # VALIDATE BEFORE MUTATING.
        # The previous version dropped first and rolled back if the replacement
        # section turned out not to exist. Now that a swap touches two tables,
        # that rollback has twice the surface to get wrong — so check every
        # precondition first and only mutate once the swap is guaranteed to
        # complete. The rollback below is now a genuine last resort rather
        # than the expected path for a bad section code.

        # 1. Replacement section must exist and have room
        sec = await sb_get_one(
            "course_sections",
            params={
                "course_code": f"eq.{req.add_course_code}",
                "section": f"eq.{req.add_section}",
                "semester": f"eq.{req.semester}",
            },
            request=request, owner=owner,
        )
        if not sec:
            raise HTTPException(
                404,
                f"Replacement section {req.add_course_code}-{req.add_section} not found "
                f"in {req.semester} — nothing was changed",
            )
        if (sec.get("status") or "").lower() == "full":
            raise HTTPException(
                409,
                f"Section {req.add_course_code}-{req.add_section} is full — nothing was changed",
            )

        # 2. Student must actually be enrolled in the course being dropped
        currently_enrolled = await sb_get(
            "class_schedules",
            params={
                "student_id": f"eq.{req.student_id}",
                "course_code": f"eq.{req.drop_course_code}",
            },
            request=request, owner=owner,
        )
        if not currently_enrolled:
            raise HTTPException(
                404,
                f"Student is not currently enrolled in {req.drop_course_code} — nothing was changed",
            )

        # 3. Student must not already be enrolled in the incoming course
        already_has_target = await sb_get(
            "class_schedules",
            params={
                "student_id": f"eq.{req.student_id}",
                "course_code": f"eq.{req.add_course_code}",
            },
            request=request, owner=owner,
        )
        if already_has_target and req.add_course_code != req.drop_course_code:
            raise HTTPException(
                409,
                f"Student is already enrolled in {req.add_course_code} — nothing was changed",
            )

        # --- All checks passed. Mutate both tables. ---

        deleted = await sb_delete(
            "class_schedules",
            match={
                "student_id": req.student_id,
                "course_code": req.drop_course_code,
            },
            request=request, owner=owner,
        )
        dropped_grades = await _drop_in_progress_grade(
            req.student_id, req.drop_course_code, request=request, owner=owner
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

        try:
            inserted = await sb_insert("class_schedules", new_row, request=request, owner=owner)
            added_grade = await _add_in_progress_grade(
                req.student_id, sec, request=request, owner=owner
            )
        except Exception:
            # Last-resort rollback: restore BOTH tables so the student is left
            # exactly as they started rather than short one course.
            for row in deleted:
                r = dict(row)
                r.pop("id", None)
                r.pop("owner_id", None)  # re-injected by sb_insert
                try:
                    await sb_insert("class_schedules", r, request=request, owner=owner)
                except Exception as e:
                    logger.error(f"failed to restore class_schedules row on rollback: {e}")
            await _restore_grade_rows(dropped_grades, request=request, owner=owner)
            raise HTTPException(
                502,
                f"Swap failed while adding {req.add_course_code}-{req.add_section} — "
                f"{req.drop_course_code} has been restored, nothing was changed",
            )

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
                "removed_grade_rows": dropped_grades,
                "added_grade_row": added_grade,
            },
            request=request, owner=owner,
        )

        return {
            "ok": True,
            "action": "swap",
            "student_id": req.student_id,
            "dropped": deleted,
            "added": inserted,
            "removed_grades": dropped_grades,
            "added_grade": added_grade,
        }

    raise HTTPException(400, f"Unknown action '{req.action}'. Use add, drop, or swap.")


# ============================================================
# POST /advising/appointment
# ============================================================

@app.post("/advising/appointment")
async def advising_appointment(req: AdvisingAppointmentRequest, request: Request):
    """Book or cancel an advising appointment with an academic advisor."""
    owner = await resolve_owner(req.caller_phone, request=request)

    student = await sb_get_one(
        "students", params={"student_id": f"eq.{req.student_id}"},
        request=request, owner=owner,
    )
    if not student:
        raise HTTPException(404, f"Student {req.student_id} not found")
    student_name = student.get("full_name_en", req.student_id)

    action = req.action.lower()

    if action == "book":
        if not req.scheduled_for:
            raise HTTPException(400, "scheduled_for required for booking")

        # advisors is a SHARED catalog — no tenant scoping
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
            request=request, owner=owner,
        )

        await log_agent_action(
            action_type="book_advising",
            description=(
                f"Booked advising appointment for {student_name} with "
                f"{advisor.get('name', req.advisor_id)} at {req.scheduled_for}"
            ),
            student_id=req.student_id,
            payload={"appointment": appt},
            request=request, owner=owner,
        )

        return {"ok": True, "action": "book", "appointment": appt}

    if action == "cancel":
        if not req.appointment_id:
            raise HTTPException(400, "appointment_id required for cancel")

        updated = await sb_update(
            "advising_appointments",
            match={"id": req.appointment_id},
            updates={"status": "cancelled"},
            request=request, owner=owner,
        )
        if not updated:
            raise HTTPException(404, f"Appointment {req.appointment_id} not found")

        await log_agent_action(
            action_type="cancel_advising",
            description=f"Cancelled advising appointment {req.appointment_id} for {student_name}",
            student_id=req.student_id,
            payload={"appointment": updated[0]},
            request=request, owner=owner,
        )

        return {"ok": True, "action": "cancel", "appointment": updated[0]}

    raise HTTPException(400, f"Unknown action '{req.action}'. Use book or cancel.")


# ============================================================
# POST /document/generate
# ============================================================

@app.post("/document/generate")
async def document_generate(req: DocumentGenerateRequest, request: Request):
    """Log a generated document.

    The actual PDF is generated agent-side (python_repl) and uploaded to
    Nebelus' artifact storage; this endpoint records the metadata so the staff
    portal shows 'document generated' in the activity drawer.

    Transcripts are static, pre-loaded PDFs served by /document/fetch. This
    endpoint rejects document_type='transcript' with a 400 so duplicate rows
    can't accumulate in documents_generated.
    """
    owner = await resolve_owner(req.caller_phone, request=request)

    student = await sb_get_one(
        "students", params={"student_id": f"eq.{req.student_id}"},
        request=request, owner=owner,
    )
    if not student:
        raise HTTPException(404, f"Student {req.student_id} not found")
    student_name = student.get("full_name_en", req.student_id)

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
        request=request, owner=owner,
    )

    pretty_type = req.document_type.replace("_", " ").title()
    await log_agent_action(
        action_type="generate_document",
        description=f"Generated {pretty_type} for {student_name}",
        student_id=req.student_id,
        payload={"document": doc},
        request=request, owner=owner,
    )

    return {"ok": True, "document": doc}


# ============================================================
# GET /document/fetch — pre-loaded documents (fast path)
# ============================================================
# Pre-loaded transcripts live in Supabase Storage under the `student-documents`
# bucket. Convention: <document_type>s/<student_id>.pdf
#
# NOTE on multi-tenancy: the storage bucket is SHARED — every tenant's baseline
# has the same student IDs, so transcripts/STU-2024001.pdf serves all tenants.
# That's correct: the baseline transcript content is identical across demos.
# Only the documents_generated registry row is tenant-scoped.

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
    caller_phone: Optional[str] = Query(None, description="Demo tenant routing — WhatsApp sender number"),
):
    """Fetch a pre-loaded document for a student.

    Returns a short-lived signed URL pointing at the PDF in Supabase Storage.
    The agent passes this URL directly to send_whatsapp_media — no python_repl
    / PDF generation needed. ~2-3 seconds end-to-end.
    """
    owner = await resolve_owner(caller_phone, request=request)

    student = await sb_get_one(
        "students", params={"student_id": f"eq.{student_id}"},
        request=request, owner=owner,
    )
    if not student:
        raise HTTPException(404, f"Student {student_id} not found")
    student_name = student.get("full_name_en", student_id)

    valid_preloaded = {"transcript"}
    if document_type not in valid_preloaded:
        raise HTTPException(
            404,
            (
                f"No pre-loaded {document_type} available. "
                f"Use generate_document to create one on the fly."
            ),
        )

    storage_path = f"{document_type}s/{student_id}.pdf"

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
        raise HTTPException(502, f"Storage sign failed: {resp.text[:200]}")

    body = resp.json()
    signed_path = body.get("signedURL") or body.get("signedUrl")
    if not signed_path:
        raise HTTPException(502, f"Unexpected sign response: {body}")

    full_url = f"{SUPABASE_URL}/storage/v1{signed_path}"

    pretty_type = document_type.replace("_", " ").title()
    await log_agent_action(
        action_type="fetch_document",
        description=f"Shared {pretty_type} with {student_name}",
        student_id=student_id,
        payload={"document_type": document_type, "storage_path": storage_path},
        request=request, owner=owner,
    )

    # Update this tenant's documents_generated row if one exists — so the
    # Documents tab shows one transcript per student, not a growing list of
    # delivery events. agent_actions above is the audit log; documents_generated
    # is the registry of what's available.
    existing_doc = await sb_get_one(
        "documents_generated",
        params={
            "student_id": f"eq.{student_id}",
            "document_type": f"eq.{document_type}",
        },
        request=request, owner=owner,
    )
    now_iso = datetime.now(timezone.utc).isoformat()
    if existing_doc:
        await sb_update(
            "documents_generated",
            match={"id": str(existing_doc["id"])},
            updates={"generated_at": now_iso},
            request=request, owner=owner,
        )
    else:
        await sb_insert(
            "documents_generated",
            {
                "student_id": student_id,
                "document_type": document_type,
                "download_url": full_url,
            },
            request=request, owner=owner,
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

    Registrar action in the real world. In the demo the agent can call it —
    e.g. after the student pays an outstanding fee that was the basis of a
    financial hold.
    """
    owner = await resolve_owner(req.caller_phone, request=request)

    student = await sb_get_one(
        "students", params={"student_id": f"eq.{req.student_id}"},
        request=request, owner=owner,
    )
    if not student:
        raise HTTPException(404, f"Student {req.student_id} not found")
    student_name = student.get("full_name_en", req.student_id)

    if req.action.lower() != "clear":
        raise HTTPException(400, "Only 'clear' action supported for holds")

    hold = await sb_get_one(
        "holds", params={"id": f"eq.{req.hold_id}"},
        request=request, owner=owner,
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
        request=request, owner=owner,
    )

    await log_agent_action(
        action_type="clear_hold",
        description=f"Cleared {hold.get('hold_type')} hold for {student_name}",
        student_id=req.student_id,
        payload={"hold": updated[0] if updated else None},
        request=request, owner=owner,
    )

    return {"ok": True, "hold": updated[0] if updated else None}


# ============================================================
# POST /fee/payment
# ============================================================

@app.post("/fee/payment")
async def fee_payment(req: FeePaymentRequest, request: Request):
    """Record a Sadad-style payment against a fee record."""
    owner = await resolve_owner(req.caller_phone, request=request)

    student = await sb_get_one(
        "students", params={"student_id": f"eq.{req.student_id}"},
        request=request, owner=owner,
    )
    if not student:
        raise HTTPException(404, f"Student {req.student_id} not found")
    student_name = student.get("full_name_en", req.student_id)

    fee = await sb_get_one(
        "fee_records", params={"id": f"eq.{req.fee_record_id}"},
        request=request, owner=owner,
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
        request=request, owner=owner,
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
        request=request, owner=owner,
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
    owner = await resolve_owner(req.caller_phone, request=request)

    student = await sb_get_one(
        "students", params={"student_id": f"eq.{req.student_id}"},
        request=request, owner=owner,
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

    # Canonicalize phone so later get_student_data(phone=...) lookups match
    if "phone" in safe_updates:
        normalized = normalize_phone(safe_updates["phone"])
        if normalized:
            safe_updates["phone"] = normalized

    updated = await sb_update(
        "students",
        match={"student_id": req.student_id},
        updates=safe_updates,
        request=request, owner=owner,
    )

    changed_fields = ", ".join(safe_updates.keys())
    await log_agent_action(
        action_type="update_profile",
        description=f"Updated {student_name}'s contact info ({changed_fields})",
        student_id=req.student_id,
        payload={"updates": safe_updates},
        request=request, owner=owner,
    )

    return {"ok": True, "student": updated[0] if updated else None}


# ============================================================
# POST /application/action
# ============================================================

@app.post("/application/action")
async def application_action(req: ApplicationActionRequest, request: Request):
    """Move an applicant through the admissions pipeline."""
    owner = await resolve_owner(req.caller_phone, request=request)

    applicant = await sb_get_one(
        "applicants",
        params={"application_id": f"eq.{req.application_id}"},
        request=request, owner=owner,
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
        request=request, owner=owner,
    )

    await log_agent_action(
        action_type="applicant_action",
        description=f"Application {req.application_id} ({name}) moved to {new_status}",
        student_id=None,  # applicants are not students yet
        payload={"applicant": updated[0] if updated else None, "action": req.action},
        request=request, owner=owner,
    )

    return {"ok": True, "applicant": updated[0] if updated else None}
