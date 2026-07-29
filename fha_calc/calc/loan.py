"""Base loan amount, LTV, and loan-limit validation (§8.1, §8.2).

Pure: takes dataclasses in, returns a dataclass out. No I/O, no config reads.
"""

from __future__ import annotations

from decimal import Decimal

from fha_calc.models import (
    BuyerProfile,
    FhaEligibilityError,
    LoanAmounts,
    LoanConfig,
    LoanLimitExceededError,
    PropertyInputs,
)


def tier_minimum_down_payment_pct(credit_score: int, config: LoanConfig) -> Decimal:
    if credit_score < config.fha_min_credit_score:
        raise FhaEligibilityError(
            f"Credit score {credit_score} is below FHA's minimum of "
            f"{config.fha_min_credit_score}. FHA financing is not available."
        )
    if credit_score >= 580:
        return config.min_down_580_plus
    return config.min_down_500_579


def calculate_loan_amounts(
    property_inputs: PropertyInputs,
    buyer: BuyerProfile,
    config: LoanConfig,
) -> LoanAmounts:
    tier_minimum_pct = tier_minimum_down_payment_pct(buyer.credit_score, config)

    if buyer.down_payment_pct < tier_minimum_pct:
        raise FhaEligibilityError(
            f"Down payment of {buyer.down_payment_pct:.3%} is below the "
            f"{tier_minimum_pct:.3%} minimum required for a credit score of "
            f"{buyer.credit_score}."
        )

    overlay_warning = 580 <= buyer.credit_score < config.lender_overlay_score_threshold

    down_payment = property_inputs.purchase_price * buyer.down_payment_pct
    base_loan_amount = property_inputs.purchase_price - down_payment
    ltv = base_loan_amount / property_inputs.purchase_price

    applicable_loan_limit, county_label = _resolve_loan_limit(property_inputs.county, config)

    if base_loan_amount > applicable_loan_limit:
        gap = base_loan_amount - applicable_loan_limit
        raise LoanLimitExceededError(
            f"Base loan amount ${base_loan_amount:,.2f} exceeds the FHA loan "
            f"limit of ${applicable_loan_limit:,.2f} for {county_label} by "
            f"${gap:,.2f}. Increase the down payment by at least that amount "
            f"to bridge the gap, or use a different loan product."
        )

    return LoanAmounts(
        down_payment=down_payment,
        base_loan_amount=base_loan_amount,
        ltv=ltv,
        down_payment_pct=buyer.down_payment_pct,
        tier_minimum_pct=tier_minimum_pct,
        overlay_warning=overlay_warning,
        applicable_loan_limit=applicable_loan_limit,
        county_label=county_label,
    )


def _resolve_loan_limit(county: str | None, config: LoanConfig) -> tuple[Decimal, str]:
    if county is None:
        return config.national_floor, "unspecified county (using national floor)"
    limit = config.county_limits.get(county)
    if limit is None:
        return config.national_floor, f"{county} (not in county table, using national floor)"
    return limit, county
