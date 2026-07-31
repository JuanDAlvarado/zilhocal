"""Shared orchestration between the CLI and the web app: resolves raw
string/user input into typed dataclasses and chains the pure calc/
functions into a full CalculationResult. No argparse, no Flask — each
front end collects its inputs however it collects them (CLI flags, a web
form) and calls into this the same way.
"""

from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal, InvalidOperation

from fha_calc.calc.closing import calculate_closing_costs
from fha_calc.calc.credits import apply_credits
from fha_calc.calc.loan import calculate_loan_amounts, tier_minimum_down_payment_pct
from fha_calc.calc.mip import calculate_mip
from fha_calc.calc.monthly import calculate_dti, calculate_monthly_payment
from fha_calc.calc.prepaids import calculate_prepaids
from fha_calc.calc.summary import assemble_cash_to_close
from fha_calc.models import (
    BuyerProfile,
    CalculationResult,
    Credits,
    LoanConfig,
    PropertyInputs,
    PropertyResolutionNotes,
)


def parse_decimal(raw: str, field_name: str) -> Decimal:
    try:
        return Decimal(str(raw).replace(",", "").replace("$", "").strip())
    except InvalidOperation:
        raise ValueError(f"Invalid value for {field_name}: {raw!r}") from None


def normalize_rate_or_pct(value: Decimal) -> Decimal:
    """Accept both '0.065' and '6.5' as meaning 6.5%."""
    return value / 100 if value > 1 else value


def resolve_dpa_arg(raw: str, config: LoanConfig) -> tuple[str | None, Decimal]:
    try:
        return None, parse_decimal(raw, "dpa")
    except ValueError:
        pass
    key = raw.strip().lower().replace(" ", "-").replace("_", "-")
    if any(p.key == key for p in config.dpa_programs):
        return key, Decimal(0)  # resolved from the program table inside calc/credits.py
    valid = ", ".join(p.key for p in config.dpa_programs)
    raise ValueError(f"Unknown DPA value {raw!r}. Use a dollar amount or one of: {valid}")


def finalize_property(
    price: Decimal,
    tax_raw: Decimal | None,
    hoa_raw: Decimal | None,
    ins_raw: Decimal | None,
    county: str | None,
    address: str | None,
    config: LoanConfig,
    notes: list[str],
) -> tuple[PropertyInputs, PropertyResolutionNotes]:
    tax_estimated = tax_raw is None
    tax = tax_raw if tax_raw is not None else (config.default_property_tax_rate * price)
    if tax_estimated:
        notes.append(
            f"Annual property tax not provided — ESTIMATED at "
            f"{config.default_property_tax_rate:.2%} of purchase price (${tax:,.0f})."
        )

    hoa_assumed_zero = hoa_raw is None
    hoa = hoa_raw if hoa_raw is not None else Decimal(0)
    if hoa_assumed_zero:
        notes.append("HOA dues not provided — assumed $0.")

    insurance_estimated = ins_raw is None
    insurance = ins_raw if ins_raw is not None else (config.default_hoi_rate * price)
    if insurance_estimated:
        notes.append(
            f"Homeowners insurance not provided — ESTIMATED at "
            f"{config.default_hoi_rate:.2%} of purchase price (${insurance:,.0f})."
        )

    property_inputs = PropertyInputs(
        purchase_price=price,
        annual_property_tax=tax,
        annual_hoa=hoa,
        annual_homeowners_insurance=insurance,
        county=county,
        address=address,
    )
    property_notes = PropertyResolutionNotes(
        tax_estimated=tax_estimated,
        hoa_assumed_zero=hoa_assumed_zero,
        insurance_estimated=insurance_estimated,
    )
    return property_inputs, property_notes


def days_in_next_closing_month(day_of_month: int, today: date | None = None) -> int:
    """The closing date itself isn't tracked as a full calendar date — only
    the day-of-month is ever collected. This resolves it to the number of
    days in whichever month will next contain that day, so per-diem
    interest reflects real month length (28-31 days) without
    calc/prepaids.py itself touching the system clock."""
    today = today or date.today()
    year, month = today.year, today.month
    if today.day > day_of_month:
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return calendar.monthrange(year, month)[1]


def build_buyer_profile(
    *,
    credit_score: int | None,
    rate: str | None,
    down_pct: str | None,
    finance_ufmip: bool | None,
    closing_day: int,
    term: int,
    config: LoanConfig,
) -> BuyerProfile:
    if credit_score is None:
        raise ValueError("credit_score is required")
    if rate is None:
        raise ValueError("rate is required")

    interest_rate = normalize_rate_or_pct(parse_decimal(rate, "rate"))

    if down_pct is not None and str(down_pct).strip() != "":
        down_payment_pct = normalize_rate_or_pct(parse_decimal(down_pct, "down_pct"))
    else:
        down_payment_pct = tier_minimum_down_payment_pct(credit_score, config)

    return BuyerProfile(
        credit_score=credit_score,
        down_payment_pct=down_payment_pct,
        interest_rate=interest_rate,
        finance_ufmip=True if finance_ufmip is None else finance_ufmip,
        closing_date_day_of_month=closing_day,
        loan_term_years=term,
    )


def build_credits(
    *,
    seller_concessions: str,
    lender_credits: str,
    gift: str,
    earnest: str,
    dpa: str | None,
    config: LoanConfig,
) -> Credits:
    dpa_program_key, dpa_amount = (None, Decimal(0))
    if dpa:
        dpa_program_key, dpa_amount = resolve_dpa_arg(dpa, config)

    return Credits(
        seller_concessions=parse_decimal(seller_concessions, "seller_concessions"),
        lender_credits=parse_decimal(lender_credits, "lender_credits"),
        dpa_amount=dpa_amount,
        dpa_program_key=dpa_program_key,
        gift_funds=parse_decimal(gift, "gift"),
        earnest_money_already_paid=parse_decimal(earnest, "earnest"),
    )


def run_calculation(
    property_inputs: PropertyInputs,
    property_notes: PropertyResolutionNotes,
    buyer: BuyerProfile,
    credits_in: Credits,
    config: LoanConfig,
    notes: list[str],
    *,
    closing_mode: str | None = None,
    item_overrides: dict[str, bool] | None = None,
    monthly_income: str | None = None,
    monthly_debts: str | None = None,
    today: date | None = None,
) -> CalculationResult:
    loan = calculate_loan_amounts(property_inputs, buyer, config)
    if loan.overlay_warning:
        notes.append(
            f"Credit score {buyer.credit_score} clears FHA's 580 floor for the 3.5% down "
            f"tier, but many lenders overlay a higher minimum (often 620-640) — confirm "
            f"with your specific lender."
        )

    mip = calculate_mip(loan, buyer, config)

    mode = closing_mode or config.closing_cost_mode
    closing = calculate_closing_costs(mode, loan, property_inputs, config, item_overrides or {})

    days_in_month = days_in_next_closing_month(buyer.closing_date_day_of_month, today)
    prepaids = calculate_prepaids(
        mip.total_loan_amount,
        buyer.interest_rate,
        buyer.closing_date_day_of_month,
        days_in_month,
        property_inputs.annual_property_tax,
        property_inputs.annual_homeowners_insurance,
        config,
    )

    credits_applied = apply_credits(
        credits_in,
        closing,
        prepaids,
        property_inputs.purchase_price,
        loan.base_loan_amount,
        buyer.credit_score,
        config,
    )
    if credits_applied.seller_concession_exceeds_cap:
        notes.append(
            f"Seller concessions offered (${credits_applied.seller_concessions_offered:,.0f}) "
            f"exceed FHA's {config.max_seller_concession_pct:.0%} cap of "
            f"${credits_applied.seller_concession_cap:,.0f} for this purchase price — only "
            f"the capped amount is usable."
        )

    monthly = calculate_monthly_payment(
        mip,
        buyer,
        property_inputs.annual_property_tax,
        property_inputs.annual_homeowners_insurance,
        property_inputs.annual_hoa,
    )

    cash_to_close = assemble_cash_to_close(loan, mip, closing, prepaids, credits_applied, monthly.total, config)

    dti = None
    if monthly_income is not None and monthly_debts is not None:
        income = parse_decimal(monthly_income, "monthly_income")
        debts = parse_decimal(monthly_debts, "monthly_debts")
        dti = calculate_dti(income, debts, monthly.total, config)

    return CalculationResult(
        property_inputs=property_inputs,
        property_notes=property_notes,
        buyer=buyer,
        credits_input=credits_in,
        loan=loan,
        mip=mip,
        closing=closing,
        prepaids=prepaids,
        credits=credits_applied,
        cash_to_close=cash_to_close,
        monthly=monthly,
        dti=dti,
    )
