import asyncio

import pytest
import httpx

from main import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "test.sqlite3")

    class Client:
        def request(self, method, path, **kwargs):
            async def send():
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                    return await client.request(method, path, **kwargs)

            return asyncio.run(send())

        def get(self, path, **kwargs):
            return self.request("GET", path, **kwargs)

        def post(self, path, **kwargs):
            return self.request("POST", path, **kwargs)

        def patch(self, path, **kwargs):
            return self.request("PATCH", path, **kwargs)

        def delete(self, path, **kwargs):
            return self.request("DELETE", path, **kwargs)

    return Client()


def create(client, **overrides):
    payload = {"company": "Acme", "role": "Engineer", **overrides}
    return client.post("/applications", json=payload)


def test_create_returns_defaults(client):
    response = create(client)
    assert response.status_code == 201
    body = response.json()
    assert body["id"]
    assert body["status"] == "saved"
    assert body["company"] == "Acme"
    assert body["applied_at"] is None
    assert body["created_at"] and body["updated_at"]


def test_list_returns_created_record(client):
    created = create(client).json()
    response = client.get("/applications")
    assert response.status_code == 200
    assert response.json() == [created]


def test_filter_by_status(client):
    create(client, status="applied")
    create(client)
    response = client.get("/applications", params={"status": "applied"})
    assert [item["status"] for item in response.json()] == ["applied"]


def test_filter_by_company_substring_case_insensitive(client):
    create(client, company="Northwind Systems")
    create(client, company="Other")
    response = client.get("/applications", params={"company": "WIND"})
    assert [item["company"] for item in response.json()] == ["Northwind Systems"]


def test_applied_date_range_filters(client):
    create(client, applied_at="2025-01-01")
    create(client, applied_at="2025-02-15")
    create(client, applied_at="2025-03-01")
    response = client.get(
        "/applications", params={"applied_after": "2025-02-01", "applied_before": "2025-02-28"}
    )
    assert [item["applied_at"] for item in response.json()] == ["2025-02-15"]


def test_get_missing_id_returns_404(client):
    response = client.get("/applications/999")
    assert response.status_code == 404
    assert response.json() == {"detail": "Application not found"}


def test_patch_updates_fields_and_timestamp(client):
    created = create(client).json()
    response = client.patch(f"/applications/{created['id']}", json={"role": "Senior Engineer", "status": "interview"})
    assert response.status_code == 200
    body = response.json()
    assert body["role"] == "Senior Engineer"
    assert body["status"] == "interview"
    assert body["updated_at"] >= created["updated_at"]


def test_patch_invalid_status_returns_422(client):
    created = create(client).json()
    response = client.patch(f"/applications/{created['id']}", json={"status": "in_progress"})
    assert response.status_code == 422
    assert client.get("/applications", params={"applied_after": "yesterday"}).status_code == 422


def test_delete_then_get_returns_404(client):
    created = create(client).json()
    assert client.delete(f"/applications/{created['id']}").status_code == 204
    assert client.get(f"/applications/{created['id']}").status_code == 404


def test_csv_export_has_header_rows_and_status_filter(client):
    create(client, status="applied")
    create(client)
    response = client.get("/applications/export.csv", params={"status": "applied"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    lines = response.text.splitlines()
    assert lines[0].startswith("id,company,role,")
    assert len(lines) == 2
    assert ",applied," in lines[1]


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


