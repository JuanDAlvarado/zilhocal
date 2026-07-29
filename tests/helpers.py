"""Shared helper for chaining the calc engine in tests, mirroring the
orchestration cli.py does but without any argparse/CLI concerns."""

from __future__ import annotations

from decimal import Decimal

from fha_calc.calc.closing import calculate_closing_costs
from fha_calc.calc.credits import apply_credits
from fha_calc.calc.loan import calculate_loan_amounts
from fha_calc.calc.mip import calculate_mip
from fha_calc.calc.monthly import calculate_monthly_payment
from fha_calc.calc.prepaids import calculate_prepaids
from fha_calc.calc.summary import assemble_cash_to_close
from fha_calc.config.loader import load_config
from fha_calc.models import BuyerProfile, Credits, LoanConfig, PropertyInputs

CONFIG: LoanConfig = load_config()


def run_pipeline(
    prop: PropertyInputs,
    buyer: BuyerProfile,
    credits_in: Credits = Credits(),
    config: LoanConfig = CONFIG,
    mode: str = "percentage",
    days_in_month: int = 30,
    item_overrides: dict[str, bool] | None = None,
):
    loan = calculate_loan_amounts(prop, buyer, config)
    mip = calculate_mip(loan, buyer, config)
    closing = calculate_closing_costs(mode, loan, prop, config, item_overrides)
    prepaids = calculate_prepaids(
        mip.total_loan_amount,
        buyer.interest_rate,
        buyer.closing_date_day_of_month,
        days_in_month,
        prop.annual_property_tax,
        prop.annual_homeowners_insurance,
        config,
    )
    credits_applied = apply_credits(
        credits_in, closing, prepaids, prop.purchase_price, loan.base_loan_amount, buyer.credit_score, config
    )
    monthly = calculate_monthly_payment(
        mip, buyer, prop.annual_property_tax, prop.annual_homeowners_insurance, prop.annual_hoa
    )
    cash = assemble_cash_to_close(loan, mip, closing, prepaids, credits_applied, monthly.total, config)
    return loan, mip, closing, prepaids, credits_applied, monthly, cash
