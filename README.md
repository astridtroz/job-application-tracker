# Job Application Tracker

A small API for recording job applications and following their progress. It tracks her real job hunt, with filters and CSV export for reviewing applications.

## Quickstart

Install the dependencies:

```bash
python3 -m pip install fastapi uvicorn pytest httpx
```

Run the API:

```bash
uvicorn main:app --reload
```

Run the test suite:

```bash
python3 -m pytest -q
```

By default, records are stored in `applications.db` in the current directory. Set `JOB_TRACKER_DB` to use a different SQLite file.

## API

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/applications` | Create an application; returns `201` |
| `GET` | `/applications` | List applications; accepts `status`, `company`, `applied_after`, and `applied_before` filters |
| `GET` | `/applications/{id}` | Get one application |
| `PATCH` | `/applications/{id}` | Partially update an application |
| `DELETE` | `/applications/{id}` | Delete an application; returns `204` |
| `GET` | `/applications/export.csv` | Export matching applications as CSV; accepts the list filters |
| `GET` | `/health` | Health check |

Statuses are `saved`, `applied`, `screening`, `interview`, `offer`, `rejected`, and `withdrawn`. Missing application IDs return `404`; invalid status values and dates return `422`.

## Built with OpenAI Codex
