"""End-to-end: generate the fake dataset, import it through the HTTP API, and
check the dashboard hangs together. Numbers here are structural (shapes, sign,
planted patterns) because the exact figures live in the analytics tests."""
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.db import get_session
from app.main import app

BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def dataset(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("fake")
    subprocess.run(
        [sys.executable, str(BACKEND / "scripts" / "generate_fake_data.py"), "--out", str(out), "--end", "2026-09-11", "--days", "63"],
        check=True, cwd=BACKEND, capture_output=True,
    )
    return out


@pytest.fixture
def client(engine):
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def _override():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_session] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _upload(client, kind: str, path: Path) -> dict:
    with path.open("rb") as f:
        r = client.post(f"/api/import/{kind}", files={"file": (path.name, f, "text/csv")})
    assert r.status_code == 200, r.text
    return r.json()


def test_full_pipeline(client, dataset):
    for kind in ("stores", "products", "promotions", "sales", "sale_items", "inventory", "expenses", "external_events", "weather"):
        res = _upload(client, kind, dataset / f"{kind}.csv")
        assert res["errors"] == [], res
        assert res["inserted"] > 0

    assert client.post("/api/import/nope", files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")}).status_code == 404

    d = client.get("/api/dashboard").json()
    assert d["as_of"] == "2026-09-11"
    wk = d["weekly"]
    assert wk["current_week"]["period"]["start"] == "2026-09-07"
    assert float(wk["current_week"]["revenue"]) > 0
    assert 0.4 < float(wk["current_week"]["gross_margin"]) < 0.7
    assert len(d["trend"]) == 12
    assert d["categories"][0]["gross_profit"] >= d["categories"][-1]["gross_profit"]

    inv = d["inventory"]
    assert float(inv["inventory_value"]) > 0
    watch = {i["product"] for i in inv["watch"]}
    assert "Bath Soak 100mg" in watch  # planted dead SKU
    assert any(i["product"] in ("Blue Dream 3.5g", "Gummies 100mg") for i in inv["stockout_risk"])  # planted stockouts

    promos = {p["promotion"]: p for p in d["promotions"]}
    assert promos["Flower Friday 30% Off"]["verdict"] in ("revenue_up_profit_down", "unprofitable")
    assert promos["Flower Friday 30% Off"]["baseline"] is not None
    assert promos["Gummies BOGO 50"]["verdict"] == "profitable"

    ext = d["external"]
    assert ext["store"] == "MAIN"
    assert len(ext["forecast"]) == 7
    top = {f["event_id"]: f for f in ext["top_findings"]}
    assert "OUT-POWER-1" in top and float(top["OUT-POWER-1"]["variance"]) < 0
    assert float(ext["resilience"]["annualised_outage_loss"]) > 0


def test_metric_endpoints_accept_custom_periods(client, dataset):
    for kind in ("products", "sales", "sale_items"):
        _upload(client, kind, dataset / f"{kind}.csv")
    r = client.get("/api/metrics/summary", params={"start": "2026-09-01", "end": "2026-09-07"})
    assert r.status_code == 200 and r.json()["period"]["days"] == 7
    assert client.get("/api/metrics/summary", params={"start": "2026-09-07", "end": "2026-09-01"}).status_code == 400
    r = client.get("/api/metrics/products", params={"start": "2026-09-01", "end": "2026-09-07"})
    assert len(r.json()["products"]) > 30
    assert client.get("/api/external/events", params={"store": "NOPE"}).status_code == 404
