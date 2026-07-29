"""Assembles the final cash-to-close figure (§8.7).

Pure: takes dataclasses in, returns a dataclass out. No I/O, no config reads.
"""

from __future__ import annotations

from decimal import Decimal

from fha_calc.models import (
    CashToClose,
    ClosingCosts,
    CostEstimate,
    CreditsApplied,
    LoanAmounts,
    LoanConfig,
    MipResult,
    Prepaids,
    ReserveEstimate,
)


def assemble_cash_to_close(
    loan: LoanAmounts,
    mip: MipResult,
    closing: ClosingCosts,
    prepaids: Prepaids,
    credits: CreditsApplied,
    monthly_total_piti: Decimal,
    config: LoanConfig,
    reserve_piti_months: int | None = None,
    reserve_moving_costs: Decimal | None = None,
    reserve_immediate_repairs: Decimal | None = None,
) -> CashToClose:
    down_payment_est = CostEstimate.flat(loan.down_payment)
    prepaids_est = CostEstimate.flat(prepaids.total)
    ufmip_cash_est = (
        CostEstimate.flat(Decimal(0)) if mip.ufmip_financed else CostEstimate.flat(mip.ufmip_amount)
    )

    total = down_payment_est + closing.estimate + prepaids_est + ufmip_cash_est
    total = total - credits.seller_concessions_applied
    total = total.sub_flat(credits.lender_credits + credits.dpa_amount)
    total = total.floor_zero()

    cash_from_own_savings = total.sub_flat(credits.gift_funds).floor_zero()

    reserve_months = (
        reserve_piti_months if reserve_piti_months is not None else config.reserve_piti_months_default
    )
    moving_costs = (
        reserve_moving_costs if reserve_moving_costs is not None else config.reserve_moving_costs_default
    )
    immediate_repairs = (
        reserve_immediate_repairs
        if reserve_immediate_repairs is not None
        else config.reserve_immediate_repairs_default
    )
    reserve = ReserveEstimate(
        months_piti=reserve_months,
        piti_reserve=monthly_total_piti * reserve_months,
        moving_costs=moving_costs,
        immediate_repairs=immediate_repairs,
    )

    return CashToClose(
        down_payment=down_payment_est,
        closing_costs=closing.estimate,
        prepaids_and_escrow=prepaids_est,
        ufmip_cash_component=ufmip_cash_est,
        seller_concessions_applied=credits.seller_concessions_applied,
        lender_credits=credits.lender_credits,
        dpa_amount=credits.dpa_amount,
        total_cash_needed=total,
        cash_from_own_savings=cash_from_own_savings,
        gift_funds=credits.gift_funds,
        earnest_money_already_paid=credits.earnest_money_already_paid,
        reserve=reserve,
    )
