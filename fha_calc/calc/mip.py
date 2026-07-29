"""Upfront MIP + annual MIP, duration rules (§8.3).

Pure: takes dataclasses in, returns a dataclass out. No I/O, no config reads.
"""

from __future__ import annotations

from decimal import Decimal

from fha_calc.models import BuyerProfile, LoanAmounts, LoanConfig, MipResult

_INF = Decimal("Infinity")


def _select_annual_mip_rate(
    loan_term_years: int, ltv: Decimal, total_loan_amount: Decimal, config: LoanConfig
) -> Decimal:
    candidates = [
        rule
        for rule in config.mip_rules
        if loan_term_years <= rule.term_years_max
        and ltv <= rule.ltv_max
        and (rule.loan_amount_max is None or total_loan_amount <= rule.loan_amount_max)
    ]
    if not candidates:
        return config.annual_mip_default

    def sort_key(rule):
        return (
            rule.term_years_max,
            rule.ltv_max,
            rule.loan_amount_max if rule.loan_amount_max is not None else _INF,
        )

    return min(candidates, key=sort_key).rate


def calculate_mip(loan: LoanAmounts, buyer: BuyerProfile, config: LoanConfig) -> MipResult:
    ufmip_amount = loan.base_loan_amount * config.ufmip_rate

    if buyer.finance_ufmip:
        total_loan_amount = loan.base_loan_amount + ufmip_amount
    else:
        total_loan_amount = loan.base_loan_amount

    annual_mip_rate = _select_annual_mip_rate(
        buyer.loan_term_years, loan.ltv, total_loan_amount, config
    )
    monthly_mip = (annual_mip_rate * total_loan_amount) / 12

    mip_duration_years = None if buyer.down_payment_pct < config.mip_11_year_down_pct_threshold else 11

    return MipResult(
        ufmip_amount=ufmip_amount,
        ufmip_financed=buyer.finance_ufmip,
        total_loan_amount=total_loan_amount,
        annual_mip_rate=annual_mip_rate,
        monthly_mip=monthly_mip,
        mip_duration_years=mip_duration_years,
    )
