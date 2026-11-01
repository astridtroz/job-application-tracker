from __future__ import annotations

import csv
import io
import os
import sqlite3
from contextlib import closing
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Response
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
