"""Closing costs, itemized or percentage mode (§8.4).

Pure: takes dataclasses in, returns a dataclass out. No I/O, no config reads.
"""

from __future__ import annotations

from decimal import Decimal

from fha_calc.models import ClosingCosts, CostEstimate, LineItem, LoanAmounts, LoanConfig, PropertyInputs


def calculate_percentage_closing_costs(loan: LoanAmounts, config: LoanConfig) -> ClosingCosts:
    estimate = CostEstimate(
        low=config.closing_pct_low * loan.base_loan_amount,
        likely=config.closing_pct_mid * loan.base_loan_amount,
        high=config.closing_pct_high * loan.base_loan_amount,
    )
    return ClosingCosts(estimate=estimate, mode="percentage")


def calculate_itemized_closing_costs(
    loan: LoanAmounts,
    property_inputs: PropertyInputs,
    config: LoanConfig,
    item_overrides: dict[str, bool] | None = None,
) -> ClosingCosts:
    """item_overrides maps a ClosingCostItem.key to True/False to flip its
    default enabled state (used for the owner's-title-insurance and optional
    inspection flags). `required` items ignore an attempt to disable them."""
    overrides = item_overrides or {}
    line_items: list[LineItem] = []
    total_low = Decimal(0)
    total_high = Decimal(0)

    for item in config.closing_items:
        if item.only_if_hoa and property_inputs.annual_hoa <= 0:
            continue

        included = overrides.get(item.key, item.enabled)
        if item.required:
            included = True
        if not included:
            continue

        if item.basis == "pct_of_loan":
            low = item.low * loan.base_loan_amount
            high = item.high * loan.base_loan_amount
        else:
            low, high = item.low, item.high

        line_items.append(LineItem(label=item.label, low=low, high=high))
        total_low += low
        total_high += high

    estimate = CostEstimate(low=total_low, likely=(total_low + total_high) / 2, high=total_high)
    return ClosingCosts(estimate=estimate, mode="itemized", line_items=tuple(line_items))


def calculate_closing_costs(
    mode: str,
    loan: LoanAmounts,
    property_inputs: PropertyInputs,
    config: LoanConfig,
    item_overrides: dict[str, bool] | None = None,
) -> ClosingCosts:
    if mode == "itemized":
        return calculate_itemized_closing_costs(loan, property_inputs, config, item_overrides)
    return calculate_percentage_closing_costs(loan, config)
