"""Seller concessions, lender credits, DPA, gifts (§8.6).

Pure: takes dataclasses in, returns a dataclass out. No I/O, no config reads.
"""

from __future__ import annotations

from decimal import Decimal

from fha_calc.models import (
    ClosingCosts,
    CostEstimate,
    Credits,
    CreditsApplied,
    DpaEligibilityError,
    LoanConfig,
    Prepaids,
)


def resolve_dpa_amount(
    credits: Credits, base_loan_amount: Decimal, buyer_credit_score: int, config: LoanConfig
) -> tuple[Decimal, str | None]:
    """Turns a Credits.dpa_amount / dpa_program_key pair into a concrete
    dollar figure. If dpa_program_key is set, the flat dpa_amount the caller
    supplied is ignored in favor of computing it from the program."""
    if credits.dpa_program_key is None:
        return credits.dpa_amount, None

    program = next((p for p in config.dpa_programs if p.key == credits.dpa_program_key), None)
    if program is None:
        raise ValueError(f"Unknown DPA program key: {credits.dpa_program_key!r}")

    if buyer_credit_score < program.min_credit_score:
        raise DpaEligibilityError(
            f"{program.label} requires a minimum credit score of "
            f"{program.min_credit_score}; buyer's score is {buyer_credit_score}."
        )

    if program.kind == "pct_of_loan":
        amount = program.value * base_loan_amount
    else:
        amount = program.value
    return amount, program.label


def apply_credits(
    credits: Credits,
    closing: ClosingCosts,
    prepaids: Prepaids,
    purchase_price: Decimal,
    base_loan_amount: Decimal,
    buyer_credit_score: int,
    config: LoanConfig,
) -> CreditsApplied:
    dpa_amount, dpa_label = resolve_dpa_amount(credits, base_loan_amount, buyer_credit_score, config)

    cap = config.max_seller_concession_pct * purchase_price
    exceeds_cap = credits.seller_concessions > cap
    usable_offer = min(credits.seller_concessions, cap)

    # Concessions can't be pocketed as cash — cap at actual costs they can
    # offset, independently per low/likely/high column.
    applied = CostEstimate(
        low=min(usable_offer, closing.estimate.low + prepaids.total),
        likely=min(usable_offer, closing.estimate.likely + prepaids.total),
        high=min(usable_offer, closing.estimate.high + prepaids.total),
    )

    return CreditsApplied(
        seller_concessions_offered=credits.seller_concessions,
        seller_concessions_applied=applied,
        seller_concession_cap=cap,
        seller_concession_exceeds_cap=exceeds_cap,
        lender_credits=credits.lender_credits,
        dpa_amount=dpa_amount,
        dpa_program_label=dpa_label,
        gift_funds=credits.gift_funds,
        earnest_money_already_paid=credits.earnest_money_already_paid,
    )
