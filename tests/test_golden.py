"""Golden-file tests for the calc engine (§12): hand-verified worked
scenarios. Expected money figures are rounded to cents since several
intermediate values (per-diem interest, escrow) are non-terminating
decimals; every scenario below was independently hand-checked against the
formulas in the spec before being encoded here."""

from decimal import Decimal as D

import pytest

from fha_calc.models import BuyerProfile, Credits, LoanLimitExceededError, PropertyInputs, round_money
from tests.helpers import run_pipeline


def test_35_percent_down_at_580():
    prop = PropertyInputs(
        purchase_price=D("300000"),
        annual_property_tax=D("3000"),
        annual_hoa=D("0"),
        annual_homeowners_insurance=D("1200"),
        county="Greenville, SC",
    )
    buyer = BuyerProfile(
        credit_score=580, down_payment_pct=D("0.035"), interest_rate=D("0.06"), closing_date_day_of_month=15
    )

    loan, mip, closing, prepaids, credits_applied, monthly, cash = run_pipeline(prop, buyer, days_in_month=30)

    assert loan.down_payment == D("10500.000")
    assert loan.base_loan_amount == D("289500.000")
    assert loan.ltv == D("0.965")
    assert loan.overlay_warning is True  # 580 is below the 620 lender-overlay threshold

    assert mip.ufmip_amount == D("5066.2500000")
    assert mip.total_loan_amount == D("294566.2500000")
    assert mip.annual_mip_rate == D("0.0055")
    assert mip.mip_duration_years is None  # <10% down -> life of loan

    assert closing.estimate.low == D("5790.00000")
    assert closing.estimate.likely == D("10132.500000")
    assert closing.estimate.high == D("14475.00000")

    assert round_money(prepaids.total) == D("2876.33")

    assert round_money(cash.total_cash_needed.low) == D("19166.33")
    assert round_money(cash.total_cash_needed.likely) == D("23508.83")
    assert round_money(cash.total_cash_needed.high) == D("27851.33")
    assert round_money(monthly.total) == D("2251.08")


def test_10_percent_down_at_550():
    prop = PropertyInputs(
        purchase_price=D("250000"),
        annual_property_tax=D("2500"),
        annual_hoa=D("600"),
        annual_homeowners_insurance=D("1100"),
        county="Spartanburg, SC",
    )
    buyer = BuyerProfile(
        credit_score=550, down_payment_pct=D("0.10"), interest_rate=D("0.07"), closing_date_day_of_month=1
    )

    loan, mip, closing, prepaids, credits_applied, monthly, cash = run_pipeline(prop, buyer, days_in_month=31)

    assert loan.down_payment == D("25000.00")
    assert loan.base_loan_amount == D("225000.00")
    assert loan.ltv == D("0.90")
    assert loan.overlay_warning is False  # below FHA's 580 tier entirely, not in the 580-619 overlay band

    assert mip.ufmip_amount == D("3937.500000")
    assert mip.total_loan_amount == D("228937.500000")
    assert mip.annual_mip_rate == D("0.0050")
    assert mip.mip_duration_years == 11  # >=10% down -> 11 years, not life of loan

    assert round_money(cash.total_cash_needed.low) == D("32725.51")
    assert round_money(cash.total_cash_needed.likely) == D("36100.51")
    assert round_money(cash.total_cash_needed.high) == D("39475.51")


def test_ufmip_financed_vs_cash():
    prop = PropertyInputs(
        purchase_price=D("285000"),
        annual_property_tax=D("2340"),
        annual_hoa=D("0"),
        annual_homeowners_insurance=D("1700"),
        county="Spartanburg, SC",
    )
    base_kwargs = dict(
        credit_score=700, down_payment_pct=D("0.035"), interest_rate=D("0.065"), closing_date_day_of_month=15
    )
    financed_buyer = BuyerProfile(**base_kwargs, finance_ufmip=True)
    cash_buyer = BuyerProfile(**base_kwargs, finance_ufmip=False)

    _, mip_f, _, _, _, _, cash_f = run_pipeline(prop, financed_buyer, days_in_month=30)
    _, mip_c, _, _, _, _, cash_c = run_pipeline(prop, cash_buyer, days_in_month=30)

    # Financed: UFMIP is rolled into the loan, contributes $0 to cash-to-close.
    assert mip_f.total_loan_amount == D("279837.9375000")
    assert cash_f.ufmip_cash_component.likely == D("0")

    # Paid in cash: UFMIP is NOT added to the loan, but its full value hits cash-to-close.
    assert mip_c.total_loan_amount == D("275025.000")
    assert cash_c.ufmip_cash_component.likely == mip_c.ufmip_amount

    assert round_money(cash_f.total_cash_needed.likely) == D("22916.72")
    assert round_money(cash_c.total_cash_needed.likely) == D("27716.80")

    # Paying cash costs more up front than the UFMIP alone would suggest,
    # because the financed loan (with a slightly larger balance) also
    # carries slightly more prepaid interest.
    diff = cash_c.total_cash_needed.likely - cash_f.total_cash_needed.likely
    assert diff < mip_c.ufmip_amount
    assert round_money(diff) == D("4800.08")


def test_dpa_and_seller_concessions():
    prop = PropertyInputs(
        purchase_price=D("320000"),
        annual_property_tax=D("3200"),
        annual_hoa=D("0"),
        annual_homeowners_insurance=D("1500"),
        county="Greenville, SC",
    )
    buyer = BuyerProfile(
        credit_score=660, down_payment_pct=D("0.035"), interest_rate=D("0.0625"), closing_date_day_of_month=20
    )
    credits_in = Credits(seller_concessions=D("8000"), dpa_program_key="palmetto-home-advantage-3")

    loan, mip, closing, prepaids, credits_applied, monthly, cash = run_pipeline(
        prop, buyer, credits_in, days_in_month=30
    )

    assert credits_applied.dpa_amount == D("9264.00000")  # 3% of $308,800 base loan
    assert credits_applied.dpa_program_label == "Palmetto Home Advantage (3% forgivable second)"
    assert credits_applied.seller_concession_cap == D("19200.00")  # 6% of $320,000
    assert credits_applied.seller_concession_exceeds_cap is False
    # $8,000 offered is fully usable since it's less than closing costs + prepaids
    # on every column.
    assert credits_applied.seller_concessions_applied.low == D("8000")
    assert credits_applied.seller_concessions_applied.likely == D("8000")
    assert credits_applied.seller_concessions_applied.high == D("8000")

    assert round_money(cash.total_cash_needed.likely) == D("7832.02")
    assert round_money(cash.cash_from_own_savings.likely) == D("7832.02")


def test_exceeds_county_loan_limit():
    prop = PropertyInputs(
        purchase_price=D("600000"),
        annual_property_tax=D("5000"),
        annual_hoa=D("0"),
        annual_homeowners_insurance=D("2000"),
        county="Greenville, SC",
    )
    buyer = BuyerProfile(credit_score=700, down_payment_pct=D("0.035"), interest_rate=D("0.065"))

    with pytest.raises(LoanLimitExceededError, match=r"exceeds the FHA loan limit"):
        run_pipeline(prop, buyer)
