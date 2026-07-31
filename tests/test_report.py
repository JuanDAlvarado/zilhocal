"""Tests for report.py: text rendering, --explain, JSON export, CSV export."""

import csv
import json
from decimal import Decimal as D

from fha_calc.models import BuyerProfile, Credits, PropertyInputs
from fha_calc.report import DISCLAIMER, render_report, to_csv_rows, to_json_dict, write_csv, write_json
from tests.helpers import run_pipeline

PROP = PropertyInputs(
    purchase_price=D("285000"),
    annual_property_tax=D("2340"),
    annual_hoa=D("0"),
    annual_homeowners_insurance=D("1700"),
    county="Spartanburg, SC",
    address="109 Cumberland Dr, Moore SC",
)
BUYER = BuyerProfile(credit_score=680, down_payment_pct=D("0.035"), interest_rate=D("0.065"))


def _result():
    from fha_calc.models import CalculationResult, PropertyResolutionNotes

    loan, mip, closing, prepaids, credits_applied, monthly, cash = run_pipeline(PROP, BUYER, Credits())
    return CalculationResult(
        property_inputs=PROP,
        property_notes=PropertyResolutionNotes(),
        buyer=BUYER,
        credits_input=Credits(),
        loan=loan,
        mip=mip,
        closing=closing,
        prepaids=prepaids,
        credits=credits_applied,
        cash_to_close=cash,
        monthly=monthly,
        dti=None,
    )


def test_render_report_contains_all_three_sections():
    text = render_report(_result())
    assert "PROPERTY" in text
    assert "CASH TO CLOSE" in text
    assert "ESTIMATED MONTHLY PAYMENT" in text
    assert "TOTAL CASH NEEDED" in text
    assert "109 Cumberland Dr, Moore SC" in text


def test_render_report_always_includes_disclaimer():
    text = render_report(_result())
    # The disclaimer is wrapped, so check for a distinctive substring rather
    # than the full text verbatim.
    assert "planning aid, not a quote" in text
    assert DISCLAIMER.startswith("This is a planning aid")


def test_render_report_notes_are_prefixed_and_shown_first():
    text = render_report(_result(), notes=["Something worth flagging."])
    assert text.startswith("NOTE: Something worth flagging.")


def test_render_report_explain_includes_derivation_chain():
    text = render_report(_result(), explain=True)
    assert "EXPLAIN: full derivation chain" in text
    assert "[loan]" in text
    assert "[mip]" in text
    assert "[prepaids]" in text
    assert "[credits]" in text
    assert "[monthly]" in text


def test_render_report_ufmip_financed_shows_financed_not_dollars():
    text = render_report(_result())
    assert "financed" in text.lower()


# --- JSON export -----------------------------------------------------------


def test_json_export_includes_computed_total_properties():
    # Regression test: dataclasses.fields() doesn't see @property members,
    # so MonthlyPayment.total / Prepaids.total / ReserveEstimate.total were
    # silently missing from the JSON export.
    result = _result()
    data = to_json_dict(result)
    assert D(data["monthly"]["total"]) == result.monthly.total
    assert D(data["prepaids"]["total"]) == result.prepaids.total
    assert D(data["cash_to_close"]["reserve"]["total"]) == result.cash_to_close.reserve.total


def test_json_export_preserves_rate_precision_not_rounded_to_cents():
    # Regression test: to_json_dict used to round every Decimal to cents,
    # which corrupted rate/percentage fields (interest_rate=0.065 -> "0.07").
    data = to_json_dict(_result())
    assert data["buyer"]["interest_rate"] == "0.065"
    assert data["buyer"]["down_payment_pct"] == "0.035"


def test_json_export_money_fields_are_strings():
    data = to_json_dict(_result())
    assert data["property_inputs"]["purchase_price"] == "285000"
    assert isinstance(data["cash_to_close"]["total_cash_needed"]["likely"], str)


def test_write_json_round_trips_through_disk(tmp_path):
    path = tmp_path / "out.json"
    write_json(_result(), path)
    loaded = json.loads(path.read_text())
    assert loaded["buyer"]["credit_score"] == 680


# --- CSV export --------------------------------------------------------


def test_csv_rows_have_header_and_expected_sections():
    rows = to_csv_rows(_result())
    assert rows[0] == ["section", "label", "low", "likely", "high"]
    sections = {row[0] for row in rows[1:]}
    assert sections == {"property", "cash_to_close", "monthly"}


def test_csv_rows_total_cash_needed_matches_cash_to_close():
    result = _result()
    rows = to_csv_rows(result)
    total_row = next(r for r in rows if r[1] == "TOTAL CASH NEEDED")
    assert D(total_row[3]) == result.cash_to_close.total_cash_needed.likely.quantize(D("0.01"))


def test_write_csv_is_valid_csv(tmp_path):
    path = tmp_path / "out.csv"
    write_csv(_result(), path)
    with open(path, newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0] == ["section", "label", "low", "likely", "high"]
    assert len(rows) > 10
