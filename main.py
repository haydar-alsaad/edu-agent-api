"""
Education Agent Data API - Consolidated (5 endpoints)
Deploy on Railway. Replaces vector store searches with fast exact lookups.
"""

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional
import json
import os

app = FastAPI(title="Education Agent API", version="2.0")

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

@app.on_event("startup")
def load_all_data():
    global students, scheduling_prefs, grades, class_schedules
    global exam_schedule, fee_records, holds, courses, course_sections
    global degree_programs, advisors, applicants
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
    print(f"Loaded: {len(students)} students, {len(grades)} grades, "
          f"{len(courses)} courses, {len(course_sections)} sections")

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
                    "C+": 2.3, "C": 2.0, "C-": 1.7, "D+": 1.3, "D": 1.0, "F": 0.0}
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


@app.get("/student/data")
def get_student_data(
    student_id: Optional[str] = Query(None, description="Student ID, e.g. STU-2024001"),
    name: Optional[str] = Query(None, description="Student full name"),
    phone: Optional[str] = Query(None, description="Phone number")
):
    """
    Get ALL data for a student in one call: profile, scheduling preferences,
    grades, current schedule, upcoming exams, fee records, and registration holds.
    Use for identity verification, schedule planning, grade checks, fee inquiries,
    or any student-specific request.
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
    prefs = find(scheduling_prefs, **{"Student ID": sid})
    student_grades = find(grades, **{"Student ID": sid})
    completed = [g for g in student_grades if g.get("Status") == "Completed"]
    in_progress = [g for g in student_grades if g.get("Status") == "In Progress"]
    completed_codes = [g.get("Course Code") for g in completed]
    gpa = calc_gpa(student_grades)
    completed_credits = sum(g.get("Credits", 3) for g in completed)
    schedule = find(class_schedules, **{"Student ID": sid})
    enrolled_codes = [c.get("Course Code") for c in schedule]
    exams = [e for e in exam_schedule if e.get("Course Code") in enrolled_codes]
    student_fees = find(fee_records, **{"Student ID": sid})
    total_due = sum(r.get("Total Due (SAR)", 0) for r in student_fees)
    total_paid = sum(r.get("Paid (SAR)", 0) for r in student_fees)
    outstanding = total_due - total_paid
    student_holds = find(holds, **{"Student ID": sid})
    active_holds = [h for h in student_holds if h.get("Status", "").lower() == "active"]
    return {
        "found": True,
        "profile": student,
        "scheduling_preferences": prefs[0] if prefs else None,
        "academics": {
            "calculated_gpa": gpa,
            "completed_courses": len(completed),
            "completed_credits": completed_credits,
            "in_progress_courses": len(in_progress),
            "completed_course_codes": completed_codes,
            "completed_details": completed,
            "in_progress_details": in_progress
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
            "active_holds": active_holds
        }
    }


@app.get("/course/info")
def get_course_info(
    course_code: Optional[str] = Query(None, description="Course code, e.g. CS450"),
    department: Optional[str] = Query(None, description="Department name, e.g. Computer Science"),
    semester: Optional[str] = Query(None, description="Filter sections by semester, e.g. Fall 2026"),
    status: Optional[str] = Query(None, description="Filter sections by status: Open or Full")
):
    """
    Get course details and available sections. Use for course information,
    prerequisite checks, section availability, and schedule planning.
    Pass course_code for a specific course, or department to list all courses.
    Add semester to filter sections (e.g., 'Fall 2026' for next semester).
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
    return {
        "found": True,
        "courses": course_results,
        "available_sections": sections,
        "total_sections": len(sections)
    }


@app.get("/degree/requirements")
def get_degree_requirements(
    program_code: Optional[str] = Query(None, description="Program code, e.g. PROG-CS"),
    program_name: Optional[str] = Query(None, description="Program name, e.g. Computer Science")
):
    """
    Get degree program requirements including total credits, core/elective breakdown,
    and duration. Use when a student asks about graduation requirements or remaining credits.
    """
    if program_code:
        results = find(degree_programs, **{"Program Code": program_code})
    elif program_name:
        results = [p for p in degree_programs if program_name.lower() in p.get("Program Name (EN)", "").lower()]
    else:
        raise HTTPException(400, "Provide program_code or program_name")
    if not results:
        return {"found": False, "message": "Program not found."}
    return {"found": True, "program": results[0]}


@app.get("/advisor/info")
def get_advisor_info(
    advisor_id: Optional[str] = Query(None, description="Advisor ID, e.g. ADV-001"),
    department: Optional[str] = Query(None, description="Filter by department")
):
    """
    Get academic advisor details and availability. Use when a student needs
    to contact their advisor or book an advising appointment.
    """
    if advisor_id:
        results = find(advisors, **{"Advisor ID": advisor_id})
    elif department:
        results = [a for a in advisors if department.lower() in a.get("Department", "").lower()]
    else:
        results = advisors
    return {"advisors": results}


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


@app.get("/health")
def health_check():
    return {
        "status": "healthy",
        "version": "2.0 (consolidated)",
        "data_loaded": {
            "students": len(students),
            "grades": len(grades),
            "courses": len(courses),
            "sections": len(course_sections),
            "degree_programs": len(degree_programs),
            "advisors": len(advisors),
            "applicants": len(applicants)
        }
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
