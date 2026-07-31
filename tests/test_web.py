"""Tests for the Flask web app (fha_calc/web/app.py), using Flask's test
client — no real sockets, no real network calls, keeps the no-network
AST-walk test meaningful (it only inspects fha_calc's own source, and
Flask itself is a normal third-party dependency, not something fha_calc
imports for outbound requests).

Skipped entirely if the optional `web` extra isn't installed, since the
base CLI install doesn't require Flask."""

from decimal import Decimal as D
from pathlib import Path

import pytest

flask = pytest.importorskip("flask")

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from fha_calc.web.app import app  # noqa: E402


@pytest.fixture
def client():
    app.config.update(TESTING=True, FHA_CONFIG_PATH=None)
    return app.test_client()


# --- static / index --------------------------------------------------------


def test_index_serves_html(client):
    res = client.get("/")
    assert res.status_code == 200
    assert b"zilhocal" in res.data


def test_static_assets_served(client):
    res = client.get("/style.css")
    assert res.status_code == 200
    res = client.get("/app.js")
    assert res.status_code == 200


def test_static_path_traversal_is_blocked(client):
    res = client.get("/../pyproject.toml")
    assert res.status_code in (400, 404)


# --- /api/config -------------------------------------------------------


def test_api_config_returns_counties_and_dpa_programs(client):
    res = client.get("/api/config")
    assert res.status_code == 200
    data = res.get_json()
    assert "Spartanburg, SC" in data["counties"]
    assert "Greenville, SC" in data["counties"]
    keys = {p["key"] for p in data["dpa_programs"]}
    assert "palmetto-heroes" in keys
    assert isinstance(data["stale"], bool)


# --- /api/calculate ------------------------------------------------------


def _base_payload(**overrides):
    payload = {
        "purchase_price": "285000",
        "annual_property_tax": "2340",
        "annual_hoa": "0",
        "annual_homeowners_insurance": "1700",
        "county": "Spartanburg, SC",
        "credit_score": 680,
        "rate": "6.5",
    }
    payload.update(overrides)
    return payload


def test_calculate_happy_path_matches_calc_engine(client):
    res = client.post("/api/calculate", json=_base_payload())
    assert res.status_code == 200
    data = res.get_json()

    # Cross-check against orchestrate.run_calculation directly — the same
    # function api_calculate() calls — rather than tests/helpers.run_pipeline,
    # which hardcodes a 30-day closing month; run_calculation resolves the
    # real "next occurrence" of the closing day from today's actual date, so
    # reproducing it any other way would drift depending on what day this
    # test happens to run.
    from fha_calc import orchestrate
    from fha_calc.config.loader import load_config
    from fha_calc.models import BuyerProfile, Credits, PropertyInputs, PropertyResolutionNotes

    config = load_config()
    prop = PropertyInputs(
        purchase_price=D("285000"),
        annual_property_tax=D("2340"),
        annual_hoa=D("0"),
        annual_homeowners_insurance=D("1700"),
        county="Spartanburg, SC",
    )
    buyer = BuyerProfile(credit_score=680, down_payment_pct=D("0.035"), interest_rate=D("0.065"))
    expected = orchestrate.run_calculation(
        prop, PropertyResolutionNotes(), buyer, Credits(), config, []
    )

    assert D(data["cash_to_close"]["total_cash_needed"]["likely"]) == expected.cash_to_close.total_cash_needed.likely
    assert D(data["monthly"]["total"]) > 0
    assert data["notes"] == []


def test_calculate_missing_purchase_price_returns_400(client):
    payload = _base_payload()
    del payload["purchase_price"]
    res = client.post("/api/calculate", json=payload)
    assert res.status_code == 400
    assert "error" in res.get_json()


def test_calculate_ineligible_credit_score_returns_400(client):
    res = client.post("/api/calculate", json=_base_payload(credit_score=450))
    assert res.status_code == 400
    assert "below FHA's minimum" in res.get_json()["error"]


def test_calculate_exceeds_loan_limit_returns_400(client):
    res = client.post("/api/calculate", json=_base_payload(purchase_price="900000", down_pct="3.5"))
    assert res.status_code == 400
    assert "exceeds the FHA loan limit" in res.get_json()["error"]


def test_calculate_blank_property_fields_are_estimated(client):
    payload = _base_payload()
    payload["annual_property_tax"] = None
    payload["annual_homeowners_insurance"] = None
    res = client.post("/api/calculate", json=payload)
    assert res.status_code == 200
    data = res.get_json()
    assert any("ESTIMATED" in n for n in data["notes"])


def test_calculate_with_dpa_program_and_seller_concessions(client):
    res = client.post(
        "/api/calculate",
        json=_base_payload(
            purchase_price="320000",
            seller_concessions="8000",
            dpa="palmetto-home-advantage-3",
            credit_score=660,
        ),
    )
    assert res.status_code == 200
    data = res.get_json()
    assert data["credits"]["dpa_program_label"] == "Palmetto Home Advantage (3% forgivable second)"
    assert D(data["credits"]["dpa_amount"]) == D("308800") * D("0.03")


def test_calculate_dpa_ineligible_credit_score_returns_400(client):
    res = client.post(
        "/api/calculate",
        json=_base_payload(dpa="palmetto-home-advantage-3", credit_score=600),
    )
    assert res.status_code == 400
    assert "minimum credit score" in res.get_json()["error"]


def test_calculate_itemized_mode(client):
    res = client.post("/api/calculate", json=_base_payload(closing_mode="itemized"))
    assert res.status_code == 200
    data = res.get_json()
    assert data["closing"]["mode"] == "itemized"
    assert len(data["closing"]["line_items"]) > 0


def test_calculate_dti_included_when_income_and_debts_given(client):
    res = client.post(
        "/api/calculate", json=_base_payload(monthly_income="8000", monthly_debts="500")
    )
    assert res.status_code == 200
    data = res.get_json()
    assert data["dti"] is not None
    assert 0 < float(data["dti"]["back_end_dti"]) < 1


def test_calculate_dti_omitted_by_default(client):
    res = client.post("/api/calculate", json=_base_payload())
    assert res.status_code == 200
    assert res.get_json()["dti"] is None


def test_calculate_ufmip_pay_cash(client):
    res = client.post("/api/calculate", json=_base_payload(finance_ufmip=False))
    assert res.status_code == 200
    data = res.get_json()
    assert data["mip"]["ufmip_financed"] is False
    assert D(data["cash_to_close"]["ufmip_cash_component"]["likely"]) > 0


# --- /api/ocr ------------------------------------------------------------


def test_ocr_requires_image(client):
    res = client.post("/api/ocr", json={})
    assert res.status_code == 400


def test_ocr_rejects_bad_base64(client):
    res = client.post("/api/ocr", json={"image_base64": "not-valid-base64!!!"})
    assert res.status_code == 400


def _font_or_skip(size):
    candidates = [
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/noto/NotoSans-Bold.ttf",
        "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
    ]
    for path in candidates:
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    pytest.skip("No usable TrueType font found for synthetic OCR image generation")


def test_ocr_extracts_price_from_uploaded_image(client, tmp_path):
    import base64

    font = _font_or_skip(32)
    img = Image.new("RGB", (400, 100), "white")
    draw = ImageDraw.Draw(img)
    draw.text((10, 10), "$285,000", fill="black", font=font)
    path = tmp_path / "listing.png"
    img.save(path)

    b64 = base64.b64encode(path.read_bytes()).decode()
    res = client.post("/api/ocr", json={"image_base64": f"data:image/png;base64,{b64}"})
    assert res.status_code == 200
    data = res.get_json()
    assert data["best_purchase_price"]["value"] == "285000"
    assert data["best_purchase_price"]["confidence"] == "HIGH"
