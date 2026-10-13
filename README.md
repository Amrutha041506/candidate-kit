# Employee Attendance & Analytics API | FastAPI, MongoDB

Designed and developed a backend attendance management and analytics system using Python, FastAPI, and MongoDB, transforming employee attendance records into actionable HR insights.

- Engineered RESTful APIs for employee lifecycle management, attendance tracking, punch-in/punch-out workflows, and attendance regularization with audit history.
- Implemented complex attendance business logic covering IST time-zone handling, overnight shifts, late-arrival grace periods, overtime calculations, work-hour rounding, and half-day eligibility.
- Built MongoDB aggregation pipelines for monthly employee analytics, cross-collection department summaries, tie-aware late-comer rankings, and department-level attendance trends with seven-day moving averages.
- Optimized database access through strategic indexing, paginated queries, and execution-plan analysis to support efficient retrieval across large attendance datasets.
- Addressed concurrent attendance operations using database-level consistency mechanisms to prevent duplicate records and preserve correction history.
- Implemented input validation and structured HTTP error handling to improve API reliability and maintain predictable responses.
- Documented code review findings, technical trade-offs, database design decisions, and application setup instructions.

**Tech Stack:** Python, FastAPI, MongoDB, PyMongo, Uvicorn, REST APIs, Aggregation Pipelines, Git, GitHub.

**Key Engineering Focus:** Backend API design, data integrity, concurrency control, database performance, business-rule validation, and analytics-driven reporting.

# Candidate kit

Read in this order:

1. `PROBLEM_STATEMENT.docx`: what you are asked to do, how it is evaluated.
2. `openapi.yaml`: the API contract and the business rules (R1 - R10).
3. `DATA_MODEL.md`: what is stored in MongoDB, with nine sample documents.
4. `app/main.py`: the starter. Put **all** your code in this one file. It must start with `uvicorn app.main:app --port 8000`.
5. `REVIEW.md`, `DECISIONS.md`: templates to fill in.

`sample_data/` holds the nine sample documents as MongoDB Extended JSON; `sample_seed.py` loads them. The grader's dataset is
much larger and contains cases they do not show.

## Local setup
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # set MONGO_URI / MONGO_DB (local docker: docker run -p 27017:27017 mongo:7)
python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```
MongoDB **6.0 or newer** is required (the grader uses 7.0). MongoDB Atlas free tier (M0) works.

## Submit
A public Git repository with `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md` and a short `README.md`.
No `.env`, no secrets, no Dockerfile.
