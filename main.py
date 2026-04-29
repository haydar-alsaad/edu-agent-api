"""
Education Agent Data API - Consolidated v3
Deploy on Railway. Replaces vector store searches with fast exact lookups.
 
v3 changes:
- Loads 4 new tables: faculty, academic_calendar, degree_requirement_courses, course_offerings_summary
- /student/data now returns remaining required courses + eligible courses for planning
- /course/info uses course_offerings_summary for fast availability + returns structured prereqs
- /degree/requirements returns the actual course list per program
- /advisor/info enriches with faculty record (office hours, specialization)
- New /calendar endpoint for academic dates (registration, exams, deadlines)
- New /faculty endpoint for instructor lookups
"""
 
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional
import json
import os
 
app = FastAPI(title="Education Agent API", version="3.0")
 
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
 
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
 
def load(filename):
    path = os.path.join(DATA_DIR, filename)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return []
 
# Existing tables
students = []
scheduling_prefs = []
grades = []
class_schedules = []
exam_schedule = []
fee_records = []
holds = []
courses = []
course_sections = []
degree_programs = []
advisors = []
applicants = []
 
# New tables (v3)
faculty = []
academic_calendar = []
degree_requirement_courses = []
course_offerings_summary = []
 
 
@app.on_event("startup")
def load_all_data():
    global students, scheduling_prefs, grades, class_schedules
    global exam_schedule, fee_records, holds, courses, course_sections
    global degree_programs, advisors, applicants
    global faculty, academic_calendar, degree_requirement_courses, course_offerings_summary
 
    students = load("students.json")
    scheduling_prefs = load("scheduling_prefs.json")
    grades = load("grades.json")
    class_schedules = load("class_schedules.json")
    exam_schedule = load("exam_schedule.json")
    fee_records = load("fee_records.json")
    holds = load("holds.json")
    courses = load("courses.json")
    course_sections = load("course_sections.json")
    degree_programs = load("degree_programs.json")
    advisors = load("advisors.json")
    applicants = load("applicants.json")
    faculty = load("faculty.json")
    academic_calendar = load("academic_calendar.json")
    degree_requirement_courses = load("degree_requirement_courses.json")
    course_offerings_summary = load("course_offerings_summary.json")
 
    print(f"Loaded: {len(students)} students, {len(grades)} grades, "
          f"{len(courses)} courses, {len(course_sections)} sections, "
          f"{len(faculty)} faculty, {len(academic_calendar)} calendar events, "
          f"{len(degree_requirement_courses)} requirement entries, "
          f"{len(course_offerings_summary)} offering summaries")
 
 
def find(data, **kwargs):
    results = []
    for record in data:
        match = True
        for key, value in kwargs.items():
            rec_val = record.get(key, "")
            if isinstance(rec_val, str) and isinstance(value, str):
                if rec_val.lower() != value.lower():
                    match = False; break
            else:
                if rec_val != value:
                    match = False; break
        if match:
            results.append(record)
    return results
 
 
def calc_gpa(grade_list):
    grade_points = {"A": 4.0, "A-": 3.7, "B+": 3.3, "B": 3.0, "B-": 2.7,
                    "C+": 2.3, "C": 2.0, "C-": 1.75, "D+": 1.5, "D": 1.0, "F": 0.0}
    total_points = 0
    total_credits = 0
    for g in grade_list:
        if g.get("Status") == "Completed":
            credits = g.get("Credits", 3)
            grade = g.get("Grade", "")
            if grade in grade_points:
                total_points += grade_points[grade] * credits
                total_credits += credits
    return round(total_points / total_credits, 2) if total_credits > 0 else 0
 
 
def get_remaining_requirements(program_code, completed_codes, in_progress_codes):
    """Return required courses the student hasn't completed or isn't currently taking."""
    program_reqs = [r for r in degree_requirement_courses if r.get("Program Code") == program_code]
    done_or_doing = set(completed_codes) | set(in_progress_codes)
    remaining = []
    for r in program_reqs:
        if r.get("Required") and r.get("Course Code") not in done_or_doing:
            remaining.append(r)
    return remaining
 
 
def get_eligible_courses(remaining_reqs, completed_codes, in_progress_codes, semester):
    """Filter remaining requirements to only those whose prereqs are met AND are offered next semester."""
    done_or_doing = set(completed_codes) | set(in_progress_codes)
    course_by_code = {c.get("Course Code"): c for c in courses}
    offerings_by_code = {}
    for o in course_offerings_summary:
        if o.get("Semester", "").lower() == semester.lower():
            offerings_by_code[o.get("Course Code")] = o
 
    eligible = []
    for req in remaining_reqs:
        code = req.get("Course Code")
        course = course_by_code.get(code)
        if not course:
            continue
        prereqs = course.get("Prerequisites", [])
        # Skip if not offered this semester
        offering = offerings_by_code.get(code)
        if not offering:
            continue
        # Skip if prereqs not met (allow in-progress prereqs to count)
        if not all(p in done_or_doing for p in prereqs):
            continue
        eligible.append({
            "Course Code": code,
            "Course Name": course.get("Course Name (EN)"),
            "Credits": course.get("Credits"),
            "Department": course.get("Department"),
            "Course Type": course.get("Course Type"),
            "Typical Year": req.get("Typical Year"),
            "Prerequisites": prereqs,
            "Prerequisites Met": True,
            "Sections Available": offering.get("Total Sections"),
            "Seats Remaining": offering.get("Seats Remaining"),
            "Availability": offering.get("Availability"),
            "Patterns Offered": offering.get("Patterns Offered", [])
        })
    return eligible
 
 
@app.get("/student/data")
def get_student_data(
    student_id: Optional[str] = Query(None, description="Student ID, e.g. STU-2024001"),
    name: Optional[str] = Query(None, description="Student full name"),
    phone: Optional[str] = Query(None, description="Phone number"),
    planning_semester: Optional[str] = Query("Fall 2026", description="Semester for course planning eligibility check, e.g. Fall 2026")
):
    """
    Get ALL data for a student in one call: profile, scheduling preferences,
    grades, current schedule, upcoming exams, fee records, registration holds,
    remaining required courses, and courses eligible for the planning_semester.
 
    Use for identity verification, schedule planning, grade checks, fee inquiries,
    or any student-specific request. The remaining_requirements and eligible_courses
    fields make schedule planning a one-call operation.
    """
    if not student_id and not name and not phone:
        raise HTTPException(400, "Provide student_id, name, or phone")
    results = []
    if student_id:
        results = find(students, **{"Student ID": student_id})
    elif name:
        results = [s for s in students if name.lower() in s.get("Full Name (EN)", "").lower()
                   or name.lower() in s.get("Full Name (AR)", "").lower()]
    elif phone:
        results = find(students, **{"Phone": phone})
    if not results:
        return {"found": False, "message": "No student found matching the criteria."}
    student = results[0]
    sid = student.get("Student ID", "")
    program_code = student.get("Program Code", "")
 
    prefs = find(scheduling_prefs, **{"Student ID": sid})
    student_grades = find(grades, **{"Student ID": sid})
    completed = [g for g in student_grades if g.get("Status") == "Completed"]
    in_progress = [g for g in student_grades if g.get("Status") == "In Progress"]
    completed_codes = [g.get("Course Code") for g in completed]
    in_progress_codes = [g.get("Course Code") for g in in_progress]
    gpa = calc_gpa(student_grades)
    completed_credits = sum(g.get("Credits", 3) for g in completed)
 
    # Degree progress (NEW)
    program_info = next((p for p in degree_programs if p.get("Program Code") == program_code), None)
    total_required_credits = program_info.get("Total Credits", 0) if program_info else 0
    remaining_reqs = get_remaining_requirements(program_code, completed_codes, in_progress_codes)
    eligible_for_planning = get_eligible_courses(remaining_reqs, completed_codes, in_progress_codes, planning_semester)
 
    schedule = find(class_schedules, **{"Student ID": sid})
    enrolled_codes = [c.get("Course Code") for c in schedule]
    exams = [e for e in exam_schedule if e.get("Course Code") in enrolled_codes]
 
    student_fees = find(fee_records, **{"Student ID": sid})
    total_due = sum(r.get("Total Due (SAR)", 0) for r in student_fees)
    total_paid = sum(r.get("Paid (SAR)", 0) for r in student_fees)
    outstanding = total_due - total_paid
 
    student_holds = find(holds, **{"Student ID": sid})
    active_holds = [h for h in student_holds if h.get("Status", "").lower() == "active"]
    blocks_registration = any(h.get("Blocks Registration") for h in active_holds)
    blocks_transcript = any(h.get("Blocks Transcript") for h in active_holds)
 
    # Advisor enrichment with faculty record (NEW)
    advisor_id = student.get("Advisor", "")
    advisor = next((a for a in advisors if a.get("Advisor ID") == advisor_id), None)
 
    return {
        "found": True,
        "profile": student,
        "scheduling_preferences": prefs[0] if prefs else None,
        "advisor": advisor,
        "academics": {
            "calculated_gpa": gpa,
            "completed_courses": len(completed),
            "completed_credits": completed_credits,
            "total_program_credits": total_required_credits,
            "credits_remaining_estimate": max(0, total_required_credits - completed_credits),
            "in_progress_courses": len(in_progress),
            "completed_course_codes": completed_codes,
            "in_progress_course_codes": in_progress_codes,
            "completed_details": completed,
            "in_progress_details": in_progress
        },
        "degree_progress": {
            "program_code": program_code,
            "remaining_required_courses": remaining_reqs,
            "remaining_required_count": len(remaining_reqs),
            "planning_semester": planning_semester,
            "eligible_courses_for_planning_semester": eligible_for_planning,
            "eligible_courses_count": len(eligible_for_planning)
        },
        "current_schedule": schedule,
        "upcoming_exams": exams,
        "finances": {
            "records": student_fees,
            "total_due": total_due,
            "total_paid": total_paid,
            "outstanding": outstanding,
            "has_financial_hold": outstanding > 0
        },
        "holds": {
            "has_active_holds": len(active_holds) > 0,
            "blocks_registration": blocks_registration,
            "blocks_transcript": blocks_transcript,
            "active_holds": active_holds
        }
    }
 
 
@app.get("/course/info")
def get_course_info(
    course_code: Optional[str] = Query(None, description="Course code, e.g. CS450"),
    department: Optional[str] = Query(None, description="Department name, e.g. Computer Science"),
    semester: Optional[str] = Query(None, description="Filter sections by semester, e.g. Fall 2026"),
    status: Optional[str] = Query(None, description="Filter sections by status: Open, Nearly Full, or Full")
):
    """
    Get course details and available sections. Use for course information,
    prerequisite checks, section availability, and schedule planning.
    Pass course_code for a specific course, or department to list all courses.
    Add semester to filter sections (e.g., 'Fall 2026' for next semester).
    Returns structured Prerequisites array, course offerings summary, and full sections.
    """
    if not course_code and not department:
        raise HTTPException(400, "Provide course_code or department")
    if course_code:
        course_results = find(courses, **{"Course Code": course_code})
    else:
        course_results = [c for c in courses if department.lower() in c.get("Department", "").lower()]
    if not course_results:
        return {"found": False, "message": "No courses found."}
    matching_codes = [c.get("Course Code") for c in course_results]
 
    sections = [s for s in course_sections if s.get("Course Code") in matching_codes]
    if semester:
        sections = [s for s in sections if semester.lower() in s.get("Semester", "").lower()]
    if status:
        sections = [s for s in sections if s.get("Status", "").lower() == status.lower()]
 
    # Offerings summary (denormalized helper) for fast availability check
    summaries = [o for o in course_offerings_summary if o.get("Course Code") in matching_codes]
    if semester:
        summaries = [o for o in summaries if semester.lower() in o.get("Semester", "").lower()]
 
    return {
        "found": True,
        "courses": course_results,
        "offerings_summary": summaries,
        "available_sections": sections,
        "total_sections": len(sections)
    }
 
 
@app.get("/degree/requirements")
def get_degree_requirements(
    program_code: Optional[str] = Query(None, description="Program code, e.g. PROG-CS"),
    program_name: Optional[str] = Query(None, description="Program name, e.g. Computer Science"),
    requirement_type: Optional[str] = Query(None, description="Filter: Core, Elective Pool, or General Education")
):
    """
    Get degree program requirements: total credits, core/elective breakdown, duration,
    AND the actual list of required courses per program. Use when a student asks about
    graduation requirements, remaining credits, or what courses they need to take.
    """
    if program_code:
        prog_results = find(degree_programs, **{"Program Code": program_code})
    elif program_name:
        prog_results = [p for p in degree_programs if program_name.lower() in p.get("Program Name (EN)", "").lower()]
    else:
        raise HTTPException(400, "Provide program_code or program_name")
    if not prog_results:
        return {"found": False, "message": "Program not found."}
    program = prog_results[0]
    pc = program.get("Program Code")
 
    req_courses = [r for r in degree_requirement_courses if r.get("Program Code") == pc]
    if requirement_type:
        req_courses = [r for r in req_courses if r.get("Requirement Type", "").lower() == requirement_type.lower()]
 
    # Group by year for easier consumption
    by_year = {}
    for r in req_courses:
        y = r.get("Typical Year", 0)
        by_year.setdefault(y, []).append(r)
 
    return {
        "found": True,
        "program": program,
        "required_courses": req_courses,
        "required_courses_by_year": by_year,
        "total_required_courses": len(req_courses)
    }
 
 
@app.get("/advisor/info")
def get_advisor_info(
    advisor_id: Optional[str] = Query(None, description="Advisor ID, e.g. ADV-001"),
    department: Optional[str] = Query(None, description="Filter by department")
):
    """
    Get academic advisor details and availability, enriched with their faculty record
    (office hours, specialization, languages spoken). Use when a student needs to
    contact their advisor or book an advising appointment.
    """
    if advisor_id:
        results = find(advisors, **{"Advisor ID": advisor_id})
    elif department:
        results = [a for a in advisors if department.lower() in a.get("Department", "").lower()]
    else:
        results = advisors
 
    # Enrich with faculty data
    enriched = []
    for adv in results:
        fac_id = adv.get("Faculty ID")
        fac_record = next((f for f in faculty if f.get("Faculty ID") == fac_id), None)
        enriched.append({
            **adv,
            "faculty_record": fac_record
        })
 
    return {"advisors": enriched, "count": len(enriched)}
 
 
@app.get("/applicant/status")
def get_applicant_status(
    application_id: Optional[str] = Query(None, description="Application ID, e.g. ADM-2026003"),
    national_id: Optional[str] = Query(None, description="National ID")
):
    """
    Get admissions application status. Use for prospective students checking
    their application progress or required documents.
    """
    if application_id:
        results = find(applicants, **{"Application ID": application_id})
    elif national_id:
        results = find(applicants, **{"National ID": national_id})
    else:
        raise HTTPException(400, "Provide application_id or national_id")
    if not results:
        return {"found": False, "message": "Application not found."}
    return {"found": True, "application": results[0]}
 
 
@app.get("/calendar")
def get_calendar(
    semester: Optional[str] = Query(None, description="Filter by semester, e.g. Fall 2026"),
    event_type: Optional[str] = Query(None, description="Filter by type: Registration, Classes, Exams, Deadline, Holiday, Tuition"),
    upcoming_only: bool = Query(False, description="Only return events with status Upcoming")
):
    """
    Get academic calendar events: registration windows, exam periods, holidays,
    deadlines, tuition due dates. Use this for any 'when is X?' question about
    semester dates. Replaces vector store calendar lookups for fast structured access.
    """
    results = list(academic_calendar)
    if semester:
        results = [e for e in results if semester.lower() in e.get("Semester", "").lower() or e.get("Semester") == "All"]
    if event_type:
        results = [e for e in results if e.get("Event Type", "").lower() == event_type.lower()]
    if upcoming_only:
        results = [e for e in results if e.get("Status", "").lower() == "upcoming"]
    return {"events": results, "count": len(results)}
 
 
@app.get("/faculty")
def get_faculty(
    faculty_id: Optional[str] = Query(None, description="Faculty ID, e.g. FAC-001"),
    name: Optional[str] = Query(None, description="Faculty name (partial match)"),
    department: Optional[str] = Query(None, description="Filter by department"),
    is_advisor: Optional[bool] = Query(None, description="Only return faculty who are advisors")
):
    """
    Get instructor/faculty details: title, department, office, office hours,
    specialization, languages. Use for 'who teaches X?' or 'what are Dr. Hassan's
    office hours?' questions.
    """
    if faculty_id:
        results = find(faculty, **{"Faculty ID": faculty_id})
    elif name:
        results = [f for f in faculty if name.lower() in f.get("Name (EN)", "").lower()
                   or name.lower() in f.get("Name (AR)", "").lower()]
    elif department:
        results = [f for f in faculty if department.lower() in f.get("Department", "").lower()]
    else:
        results = list(faculty)
    if is_advisor is not None:
        results = [f for f in results if f.get("Is Advisor") == is_advisor]
 
    # Enrich with sections taught (current + next semester)
    enriched = []
    for f in results:
        name_en = f.get("Name (EN)")
        sections_taught = [s for s in course_sections if s.get("Instructor") == name_en]
        # Group by semester
        by_sem = {}
        for s in sections_taught:
            by_sem.setdefault(s.get("Semester"), []).append({
                "Course Code": s.get("Course Code"),
                "Course Name": s.get("Course Name"),
                "Section": s.get("Section"),
                "Schedule Pattern": s.get("Schedule Pattern"),
                "Time": s.get("Time")
            })
        enriched.append({**f, "sections_taught_by_semester": by_sem})
 
    return {"faculty": enriched, "count": len(enriched)}
 
 
@app.get("/health")
def health_check():
    return {
        "status": "healthy",
        "version": "3.0",
        "data_loaded": {
            "students": len(students),
            "grades": len(grades),
            "courses": len(courses),
            "sections": len(course_sections),
            "offerings_summary": len(course_offerings_summary),
            "degree_programs": len(degree_programs),
            "degree_requirement_courses": len(degree_requirement_courses),
            "advisors": len(advisors),
            "faculty": len(faculty),
            "academic_calendar": len(academic_calendar),
            "applicants": len(applicants)
        }
    }
 
 
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
