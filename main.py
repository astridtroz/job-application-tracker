from __future__ import annotations

import csv
from html import escape
import io
import os
import sqlite3
from contextlib import closing
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ApplicationStatus(str, Enum):
    saved = "saved"
    applied = "applied"
    screening = "screening"
    interview = "interview"
    offer = "offer"
    rejected = "rejected"
    withdrawn = "withdrawn"


class ApplicationCreate(BaseModel):
    company: str = Field(min_length=1)
    role: str = Field(min_length=1)
    url: str | None = None
    source: str | None = None
    status: ApplicationStatus = ApplicationStatus.saved
    applied_at: date | None = None
    notes: str | None = None


class ApplicationPatch(BaseModel):
    company: str | None = Field(default=None, min_length=1)
    role: str | None = Field(default=None, min_length=1)
    url: str | None = None
    source: str | None = None
    status: ApplicationStatus | None = None
    applied_at: date | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def require_non_nullable_values(self) -> ApplicationPatch:
        for field in ("company", "role", "status"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class ApplicationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    company: str
    role: str
    url: str | None
    source: str | None
    status: ApplicationStatus
    applied_at: date | None
    notes: str | None
    created_at: datetime
    updated_at: datetime


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    return connection


def _initialize(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(_connect(db_path)) as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company TEXT NOT NULL,
                role TEXT NOT NULL,
                url TEXT,
                source TEXT,
                status TEXT NOT NULL DEFAULT 'saved',
                applied_at TEXT,
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        connection.commit()


def _filters(
    status: ApplicationStatus | None,
    company: str | None,
    applied_after: date | None,
    applied_before: date | None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    values: list[Any] = []
    if status is not None:
        clauses.append("status = ?")
        values.append(status.value)
    if company is not None:
        clauses.append("LOWER(company) LIKE ?")
        values.append(f"%{company.lower()}%")
    if applied_after is not None:
        clauses.append("applied_at >= ?")
        values.append(applied_after.isoformat())
    if applied_before is not None:
        clauses.append("applied_at <= ?")
        values.append(applied_before.isoformat())
    return (" WHERE " + " AND ".join(clauses) if clauses else "", values)


def create_app(db_path: str | Path | None = None) -> FastAPI:
    path = Path(db_path or os.environ.get("JOB_TRACKER_DB", "applications.db"))
    _initialize(path)
    app = FastAPI(title="Job Application Tracker API")

    def connection() -> sqlite3.Connection:
        return _connect(path)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/applications", response_model=ApplicationRead, status_code=201)
    async def create_application(payload: ApplicationCreate) -> dict[str, Any]:
        now = _timestamp()
        data = payload.model_dump(mode="json")
        with closing(connection()) as db:
            cursor = db.execute(
                """INSERT INTO applications
                (company, role, url, source, status, applied_at, notes, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (data["company"], data["role"], data["url"], data["source"],
                 data["status"], data["applied_at"], data["notes"], now, now),
            )
            db.commit()
            row = db.execute("SELECT * FROM applications WHERE id = ?", (cursor.lastrowid,)).fetchone()
            return dict(row)

    def query_rows(
        status: ApplicationStatus | None,
        company: str | None,
        applied_after: date | None,
        applied_before: date | None,
    ) -> list[dict[str, Any]]:
        where, values = _filters(status, company, applied_after, applied_before)
        with closing(connection()) as db:
            rows = db.execute(f"SELECT * FROM applications{where} ORDER BY id", values).fetchall()
            return [dict(row) for row in rows]

    def get_stats() -> dict[str, Any]:
        with closing(connection()) as db:
            total = db.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
            status_rows = db.execute(
                "SELECT status, COUNT(*) AS count FROM applications GROUP BY status ORDER BY status"
            ).fetchall()
            company_rows = db.execute(
                "SELECT company, COUNT(*) AS count FROM applications GROUP BY company "
                "ORDER BY count DESC, company COLLATE NOCASE ASC LIMIT 10"
            ).fetchall()
            oldest = db.execute(
                "SELECT id, company, role, applied_at FROM applications "
                "WHERE status IN ('applied', 'screening') AND applied_at IS NOT NULL "
                "ORDER BY applied_at ASC, id ASC LIMIT 1"
            ).fetchone()

        oldest_unanswered = None
        if oldest is not None:
            applied_date = date.fromisoformat(oldest["applied_at"])
            oldest_unanswered = {
                "id": oldest["id"],
                "company": oldest["company"],
                "role": oldest["role"],
                "applied_at": oldest["applied_at"],
                "days_waiting": (date.today() - applied_date).days,
            }

        return {
            "total": total,
            "by_status": {row["status"]: row["count"] for row in status_rows},
            "by_company": [{"company": row["company"], "count": row["count"]} for row in company_rows],
            "oldest_unanswered": oldest_unanswered,
        }

    @app.get("/stats")
    async def stats() -> dict[str, Any]:
        return get_stats()

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard() -> str:
        summary = get_stats()
        status_rows = "".join(
            f"<tr><td>{escape(status)}</td><td>{count}</td></tr>"
            for status, count in summary["by_status"].items()
        ) or '<tr><td colspan="2">No applications yet</td></tr>'
        company_rows = "".join(
            f"<tr><td>{escape(item['company'])}</td><td>{item['count']}</td></tr>"
            for item in summary["by_company"]
        ) or '<tr><td colspan="2">No applications yet</td></tr>'
        oldest = summary["oldest_unanswered"]
        oldest_card = (
            f"<p><strong>{escape(oldest['company'])}</strong> — {escape(oldest['role'])}</p>"
            f"<p>Applied {escape(oldest['applied_at'])}; waiting {oldest['days_waiting']} days.</p>"
            if oldest else "<p>No unanswered applications.</p>"
        )
        empty_message = '<p class="empty">No applications yet</p>' if summary["total"] == 0 else ""
        return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Job Application Dashboard</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; color: #202938; background: #f5f7fb; }}
h1, h2 {{ color: #172554; }} .total, .card {{ background: white; border-radius: 10px; padding: 1rem 1.25rem; box-shadow: 0 2px 10px #17255414; }}
.card {{ border-left: 5px solid #2563eb; margin: 1rem 0 2rem; }} .tables {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 1.5rem; }}
table {{ width: 100%; border-collapse: collapse; background: white; }} th, td {{ text-align: left; padding: .65rem; border-bottom: 1px solid #e5e7eb; }}
.empty {{ color: #526078; }}
</style></head><body>
<h1>Job Application Dashboard</h1><p class="total">Total applications: <strong>{summary['total']}</strong></p>{empty_message}
<section class="card"><h2>Oldest unanswered application</h2>{oldest_card}</section>
<div class="tables"><section><h2>Applications by status</h2><table><thead><tr><th>Status</th><th>Count</th></tr></thead><tbody>{status_rows}</tbody></table></section>
<section><h2>Applications by company</h2><table><thead><tr><th>Company</th><th>Count</th></tr></thead><tbody>{company_rows}</tbody></table></section></div>
</body></html>"""

    @app.get("/applications/export.csv")
    async def export_applications(
        status: ApplicationStatus | None = None,
        company: str | None = None,
        applied_after: date | None = None,
        applied_before: date | None = None,
    ) -> Response:
        rows = query_rows(status, company, applied_after, applied_before)
        output = io.StringIO(newline="")
        fields = ["id", "company", "role", "url", "source", "status", "applied_at", "notes", "created_at", "updated_at"]
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
        return Response(content=output.getvalue(), media_type="text/csv")

    @app.get("/applications", response_model=list[ApplicationRead])
    async def list_applications(
        status: ApplicationStatus | None = None,
        company: str | None = None,
        applied_after: date | None = None,
        applied_before: date | None = None,
    ) -> list[dict[str, Any]]:
        return query_rows(status, company, applied_after, applied_before)

    @app.get("/applications/{application_id}", response_model=ApplicationRead)
    async def get_application(application_id: int) -> dict[str, Any]:
        with closing(connection()) as db:
            row = db.execute("SELECT * FROM applications WHERE id = ?", (application_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Application not found")
        return dict(row)

    @app.patch("/applications/{application_id}", response_model=ApplicationRead)
    async def patch_application(application_id: int, payload: ApplicationPatch) -> dict[str, Any]:
        changes = payload.model_dump(exclude_unset=True, mode="json")
        if not changes:
            return await get_application(application_id)
        changes["updated_at"] = _timestamp()
        assignments = ", ".join(f"{key} = ?" for key in changes)
        values = list(changes.values()) + [application_id]
        with closing(connection()) as db:
            cursor = db.execute(f"UPDATE applications SET {assignments} WHERE id = ?", values)
            db.commit()
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Application not found")
            row = db.execute("SELECT * FROM applications WHERE id = ?", (application_id,)).fetchone()
            return dict(row)

    @app.delete("/applications/{application_id}", status_code=204)
    async def delete_application(application_id: int) -> Response:
        with closing(connection()) as db:
            cursor = db.execute("DELETE FROM applications WHERE id = ?", (application_id,))
            db.commit()
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Application not found")
        return Response(status_code=204)

    return app


app = create_app()
