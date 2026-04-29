# KFUT Education Demo Data — Expanded v2

Generated: 2026-04-29

This is an expansion of the original 12-file dataset for the T2 / Nebelus university WhatsApp agent demo. All files preserve KSA university conventions (Sun-Tue-Thu / Mon-Wed scheduling patterns, SAR currency, Arabic + English bilingual fields, Saudi National IDs).

## Files

| File | Records | Status |
|---|---|---|
| students.json | 10 | unchanged from v1 |
| scheduling_prefs.json | 10 | unchanged from v1 |
| grades.json | 178 | **expanded** — full transcripts for all 10 students |
| fee_records.json | 51 | **expanded** — multi-semester history per student |
| holds.json | 6 | **expanded** — added library, medical examples + new fields |
| class_schedules.json | 14 | rebuilt from current Spring 2026 in-progress courses |
| exam_schedule.json | 282 | **expanded** — covers Spring 2026 + Fall 2026 |
| courses.json | 75 | **expanded** — all 5 departments, full 100/200/300/400 progression + electives |
| course_sections.json | 177 | **expanded** — 3 semesters: Spring 2026 + Summer 2026 + Fall 2026 |
| course_offerings_summary.json | 151 | **NEW** — denormalized helper, one row per (course, semester) |
| degree_programs.json | 5 | extended with tuition + accreditation |
| degree_requirement_courses.json | 133 | **NEW** — actual required course list per program |
| advisors.json | 6 | extended; references faculty IDs |
| applicants.json | 6 | extended with more diverse cases |
| academic_calendar.json | 28 | **NEW** — registration/exam/deadline dates as structured records |
| faculty.json | 22 | **NEW** — instructors as first-class records |

## Schema changes from v1

### Structured arrays (was: comma-separated strings)

In `courses.json`:
- `Prerequisites`: was `"CS201, MATH201"`, now `["CS201", "MATH201"]`
- `Semester Offered`: was `"Fall, Spring"`, now `["Fall", "Spring"]`
- `Prerequisites Display` (new): the original string format, kept for human-readable display

### New fields added to existing files

`courses.json`:
- `Course Level` (100/200/300/400)
- `Course Type` (Core/Elective/General)
- `Lab Required` (boolean)

`course_sections.json`:
- `Semester` is now mandatory on every record (was missing in some original entries)
- `Status` field always present (Open / Nearly Full / Full)

`degree_programs.json`:
- `Tuition Per Semester (SAR)`
- `Accreditation`

`holds.json`:
- `Blocks Registration` (boolean) — critical for the agent to know whether a hold prevents course registration
- `Blocks Transcript` (boolean)
- `Contact` — email address for resolution

`fee_records.json`:
- `Late Fee (SAR)` — applied automatically on unpaid balances

`applicants.json`:
- `Program Code`, `STEP Score`, `Email`, `Phone`, `Next Step`

## New tables — design rationale

### `degree_requirement_courses.json`
This is the table that fixes the "what courses do I still need?" failure mode. It lists every required course for every program — so the agent can do a simple set-difference between this and the student's completed courses.

Each record has:
- `Program Code`
- `Course Code` + `Course Name` + `Credits`
- `Requirement Type`: "Core" / "Elective Pool" / "General Education"
- `Required` (boolean): true for must-take, false for "pick from pool"
- `Typical Year`: when this course is normally taken (1–4)

### `course_offerings_summary.json`
Denormalized helper: one row per `(course_code, semester)`. Lets the agent answer "is CS401 available in Fall 2026?" without filtering 177 section rows.

Each record has:
- `Course Code`, `Course Name`, `Department`, `Credits`
- `Semester`
- `Total Sections`, `Total Seats`, `Seats Taken`, `Seats Remaining`
- `Availability`: "Open" / "Nearly Full" / "Full"
- `Patterns Offered`: array of "Sun-Tue-Thu" and/or "Mon-Wed"
- `Number of Instructors`
- `Prerequisites` + `Prerequisites Display`

### `faculty.json`
Instructors as records (was: name strings inside section data). Lets the agent answer "what does Dr. Hassan teach?" or "who are the CS faculty?" with a direct lookup.

Each record has:
- `Faculty ID`
- `Name (EN)`, `Name (AR)`, `Title`, `Department`
- `Email`, `Phone`, `Office`
- `Office Hours`, `Specialization`
- `Years at KFUT`, `Is Advisor` (boolean), `Languages`

### `academic_calendar.json`
Structured calendar (was: free-text in vector store). The agent now answers "when is Fall 2026 registration?" with a fast structured lookup instead of a slow RAG call.

Each record has:
- `Event ID`
- `Semester` (or "All" for university-wide)
- `Event Type`: "Registration" / "Classes" / "Exams" / "Deadline" / "Holiday" / "Tuition"
- `Event Name`, `Start Date`, `End Date`, `Description`
- `Status`: "Completed" / "Upcoming"

## Test scenarios this dataset supports

| Scenario | Test student | Expected agent behavior |
|---|---|---|
| Schedule planning, Y3 student, mid-progress | STU-2024001 (Abdullah) | Cross-ref grades + requirements + Fall 2026 offerings → propose 5–6 eligible courses |
| Near graduation, Y4 student, all core done | STU-2024003 (Faisal) | Recognize core+gen-ed complete → suggest electives + senior project |
| Probation, must repeat failed courses | STU-2024005 (Mohammed) | Recommend MATH101 + PHYS101 retakes + lighter load |
| Multiple holds blocking registration | STU-2024005, STU-2024009 | Surface holds first; explain registration is blocked until resolved |
| Brand new Y1 student, no transcript yet | STU-2024010 (Lama) | Default to first-year curriculum |
| Library/transcript hold (non-registration) | STU-2024003 | Inform student but don't block scheduling |
| Medical hold blocking registration | STU-2024008 | Surface hold + clear resolution path |
| Section already full | CS402 Section B (Spring 2026) | Suggest alternative section |
| Course offered in only one semester | CS407 (Fall only), CS406 (Spring only) | Plan around availability |
| Summer term lighter catalog | Various | Accept reduced options for Summer 2026 |
| Faculty lookup | Dr. Hassan | Return office hours, courses taught, contact |
| Calendar lookup | "When is registration?" | Return Fall 2026 dates from calendar |

## Integrity guarantees

The validator (`07_validate.py`) confirms:
- Every grade's course code exists in the catalog
- Every section's course code exists in the catalog
- Every prerequisite in courses.json resolves to a real course
- Every requirement entry's course exists
- Every advisor referenced in students.json exists
- Every program code in students.json exists
- Every hold's student ID exists
- Every section's instructor exists in faculty.json
- **No instructor is double-booked** (same time, same day, same semester)
- **No room is double-booked** (same time, same day, same semester)
- All 10 students' calculated GPAs are within 0.20 of their target GPA
