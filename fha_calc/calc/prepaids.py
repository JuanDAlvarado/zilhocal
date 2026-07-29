"""Per-diem interest and escrow setup (§8.5).

Pure: takes dataclasses/Decimals in, returns a dataclass out. No I/O, no
config reads, no reading of the system clock — the caller (cli.py) resolves
"today" and the closing month into `days_in_closing_month` before calling in,
so this stays a deterministic, unit-testable function of its inputs.
"""

from __future__ import annotations

from decimal import Decimal

from fha_calc.models import LoanConfig, Prepaids


def calculate_prepaids(
    total_loan_amount: Decimal,
    interest_rate: Decimal,
    closing_date_day_of_month: int,
    days_in_closing_month: int,
    annual_property_tax: Decimal,
    annual_homeowners_insurance: Decimal,
    config: LoanConfig,
) -> Prepaids:
    daily_interest = (total_loan_amount * interest_rate) / 365
    days_remaining = max(0, days_in_closing_month - closing_date_day_of_month)
    prepaid_interest = daily_interest * days_remaining

    prepaid_insurance = annual_homeowners_insurance  # first year, paid upfront

    tax_escrow = (annual_property_tax / 12) * config.tax_escrow_months
    ins_escrow = (annual_homeowners_insurance / 12) * config.ins_escrow_months

    return Prepaids(
        prepaid_interest=prepaid_interest,
        days_of_prepaid_interest=days_remaining,
        prepaid_insurance=prepaid_insurance,
        tax_escrow=tax_escrow,
        ins_escrow=ins_escrow,
    )
