"""Text table output (§11), --explain derivation dump, and the accuracy
disclaimer (§15). JSON export lives here too (added in a later stage)."""

from __future__ import annotations

import dataclasses
import json
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from fha_calc.models import CalculationResult, CostEstimate

LABEL_WIDTH = 42
COL_WIDTH = 12

DISCLAIMER = (
    "This is a planning aid, not a quote. Figures are estimates based on "
    "user-supplied and config-supplied assumptions. Actual costs come from "
    "your lender's Loan Estimate and Closing Disclosure. Rates, FHA limits, "
    "and assistance-program terms change — verify current figures before "
    "relying on this for a real transaction."
)


def _dollars(value: Decimal) -> str:
    whole = value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return f"${whole:,.0f}"


def _pct(value: Decimal, places: int = 1) -> str:
    return f"{value * 100:.{places}f}%"


def _row(label: str, low: str, likely: str, high: str) -> str:
    return f"  {label:<{LABEL_WIDTH}}{low:>{COL_WIDTH}}{likely:>{COL_WIDTH}}{high:>{COL_WIDTH}}"


def _flat_row(label: str, value: str) -> str:
    return f"  {label:<{LABEL_WIDTH}}{value:>{COL_WIDTH}}"


def _cost_row(label: str, est: CostEstimate) -> str:
    return _row(label, _dollars(est.low), _dollars(est.likely), _dollars(est.high))


def _tier_label(credit_score: int) -> str:
    return "580+ tier" if credit_score >= 580 else "500-579 tier"


def render_report(result: CalculationResult, notes: list[str] | None = None, explain: bool = False) -> str:
    lines: list[str] = []

    if notes:
        for note in notes:
            lines.append(f"NOTE: {note}")
        lines.append("")

    lines.append("PROPERTY")
    addr = result.property_inputs.address or "(no address)"
    lines.append(_flat_row(addr, _dollars(result.property_inputs.purchase_price)))
    lines.append("")

    lines.append(f"CASH TO CLOSE{'':>{LABEL_WIDTH - 13}}{'LOW':>{COL_WIDTH}}{'LIKELY':>{COL_WIDTH}}{'HIGH':>{COL_WIDTH}}")
    down_label = f"Down payment ({_pct(result.loan.down_payment_pct)}, {_tier_label(result.buyer.credit_score)})"
    lines.append(_row(down_label, *[_dollars(result.loan.down_payment)] * 3))

    if result.closing.mode == "percentage":
        low_pct = result.closing.estimate.low / result.loan.base_loan_amount * 100
        high_pct = result.closing.estimate.high / result.loan.base_loan_amount * 100
        closing_label = f"Closing costs ({low_pct:.1f}–{high_pct:.1f}% of loan)"
    else:
        closing_label = f"Closing costs (itemized, {len(result.closing.line_items)} items)"
    lines.append(_cost_row(closing_label, result.closing.estimate))

    lines.append(_cost_row("Prepaids & escrow setup", result.cash_to_close.prepaids_and_escrow))

    ufmip_rate_display = result.mip.ufmip_amount / result.loan.base_loan_amount
    ufmip_label = f"Upfront MIP ({_pct(ufmip_rate_display, 2)})"
    if result.mip.ufmip_financed:
        lines.append(_row(ufmip_label, "financed", "financed", "financed"))
    else:
        lines.append(_cost_row(ufmip_label, result.cash_to_close.ufmip_cash_component))

    lines.append(_cost_row("Less: seller concessions", result.cash_to_close.seller_concessions_applied))
    if result.credits.lender_credits > 0:
        lines.append(_row("Less: lender credits", *[_dollars(result.credits.lender_credits)] * 3))
    if result.credits.dpa_amount > 0:
        dpa_label = "Less: DPA"
        if result.credits.dpa_program_label:
            dpa_label = f"Less: DPA ({result.credits.dpa_program_label})"
        lines.append(_row(dpa_label, *[_dollars(result.credits.dpa_amount)] * 3))

    lines.append("  " + "─" * (LABEL_WIDTH + 3 * COL_WIDTH - 2))
    lines.append(_cost_row("TOTAL CASH NEEDED", result.cash_to_close.total_cash_needed))
    lines.append(_cost_row("  of which from your own savings", result.cash_to_close.cash_from_own_savings))
    lines.append("")

    if result.credits.earnest_money_already_paid > 0:
        lines.append(
            _flat_row("Earnest money (due at contract)", _dollars(result.credits.earnest_money_already_paid))
            + "   ← needed earlier, credited back"
        )
    reserve = result.cash_to_close.reserve
    lines.append(
        _flat_row(
            f"Suggested reserve ({reserve.months_piti} mo PITI + moving)", _dollars(reserve.total)
        )
        + "   ← not required"
    )
    lines.append("")

    lines.append("ESTIMATED MONTHLY PAYMENT")
    duration = "life of loan" if result.mip.mip_duration_years is None else f"{result.mip.mip_duration_years} years"
    lines.append(_flat_row("Principal & interest", _dollars(result.monthly.principal_and_interest)))
    lines.append(_flat_row(f"MIP ({_pct(result.mip.annual_mip_rate, 2)}/yr, {duration})", _dollars(result.monthly.mip)))
    lines.append(_flat_row("Property tax", _dollars(result.monthly.property_tax)))
    lines.append(_flat_row("Homeowners insurance", _dollars(result.monthly.homeowners_insurance)))
    lines.append(_flat_row("HOA", _dollars(result.monthly.hoa)))
    lines.append("  " + "─" * (LABEL_WIDTH + COL_WIDTH - 2))
    lines.append(_flat_row("TOTAL", _dollars(result.monthly.total)))

    if result.dti is not None:
        lines.append("")
        lines.append("DEBT-TO-INCOME (informational only — not an approval prediction)")
        lines.append(
            _flat_row("Back-end DTI", f"{_pct(result.dti.back_end_dti)}"
                       f" (target ≤ {_pct(result.dti.target_max)})")
        )

    if explain:
        lines.append("")
        lines.extend(_render_explain(result))

    lines.append("")
    lines.append(_wrap(DISCLAIMER, LABEL_WIDTH + 3 * COL_WIDTH))

    return "\n".join(lines)


def _wrap(text: str, width: int) -> str:
    import textwrap

    return "\n".join(textwrap.wrap(text, width=width))


def _render_explain(result: CalculationResult) -> list[str]:
    lines = ["--- EXPLAIN: full derivation chain ---", ""]

    lines.append("[loan]")
    lines.append(f"  purchase_price          = {result.property_inputs.purchase_price}")
    lines.append(f"  down_payment_pct        = {result.loan.down_payment_pct} (tier minimum {result.loan.tier_minimum_pct})")
    lines.append(f"  down_payment            = {result.loan.down_payment}")
    lines.append(f"  base_loan_amount        = {result.loan.base_loan_amount}")
    lines.append(f"  ltv                     = {result.loan.ltv}")
    lines.append(f"  applicable_loan_limit   = {result.loan.applicable_loan_limit} ({result.loan.county_label})")
    if result.loan.overlay_warning:
        lines.append("  overlay_warning         = True (many lenders require 620-640 despite FHA's 580 floor)")
    lines.append("")

    lines.append("[mip]")
    lines.append(f"  ufmip_rate x base_loan  = {result.mip.ufmip_amount}")
    lines.append(f"  ufmip_financed          = {result.mip.ufmip_financed}")
    lines.append(f"  total_loan_amount       = {result.mip.total_loan_amount}")
    lines.append(f"  annual_mip_rate         = {result.mip.annual_mip_rate}")
    lines.append(f"  monthly_mip             = {result.mip.monthly_mip}")
    lines.append(f"  mip_duration_years      = {result.mip.mip_duration_years}")
    lines.append("")

    lines.append("[closing_costs]")
    lines.append(f"  mode                    = {result.closing.mode}")
    lines.append(f"  low/likely/high         = {result.closing.estimate.low} / {result.closing.estimate.likely} / {result.closing.estimate.high}")
    for item in result.closing.line_items:
        lines.append(f"    - {item.label}: {item.low} - {item.high}")
    lines.append("")

    lines.append("[prepaids]")
    lines.append(f"  days_of_prepaid_interest = {result.prepaids.days_of_prepaid_interest}")
    lines.append(f"  prepaid_interest         = {result.prepaids.prepaid_interest}")
    lines.append(f"  prepaid_insurance        = {result.prepaids.prepaid_insurance}")
    lines.append(f"  tax_escrow               = {result.prepaids.tax_escrow}")
    lines.append(f"  ins_escrow               = {result.prepaids.ins_escrow}")
    lines.append(f"  total                    = {result.prepaids.total}")
    lines.append("")

    lines.append("[credits]")
    lines.append(f"  seller_concessions_offered = {result.credits.seller_concessions_offered} (cap {result.credits.seller_concession_cap}, exceeds_cap={result.credits.seller_concession_exceeds_cap})")
    lines.append(f"  seller_concessions_applied = {result.credits.seller_concessions_applied}")
    lines.append(f"  lender_credits             = {result.credits.lender_credits}")
    lines.append(f"  dpa_amount                 = {result.credits.dpa_amount} ({result.credits.dpa_program_label})")
    lines.append(f"  gift_funds                 = {result.credits.gift_funds}")
    lines.append("")

    lines.append("[cash_to_close]")
    lines.append(f"  total_cash_needed       = {result.cash_to_close.total_cash_needed}")
    lines.append(f"  cash_from_own_savings   = {result.cash_to_close.cash_from_own_savings}")
    lines.append("")

    lines.append("[monthly]")
    lines.append(f"  principal_and_interest  = {result.monthly.principal_and_interest}")
    lines.append(f"  mip                     = {result.monthly.mip}")
    lines.append(f"  property_tax            = {result.monthly.property_tax}")
    lines.append(f"  homeowners_insurance    = {result.monthly.homeowners_insurance}")
    lines.append(f"  hoa                     = {result.monthly.hoa}")
    lines.append(f"  total                   = {result.monthly.total}")

    return lines


def _json_safe(value):
    if isinstance(value, Decimal):
        # Exact string, not rounded: this field could be a dollar amount or
        # a rate/percentage (e.g. interest_rate=0.065), and cent-rounding
        # every Decimal indiscriminately would corrupt the latter.
        return str(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _json_safe(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    return value


def to_json_dict(result: CalculationResult) -> dict:
    """Money and rate fields are exported as exact Decimal strings (e.g.
    "9975.000") rather than JSON numbers, since JSON has no decimal type and
    round-tripping through float would reintroduce the precision loss
    Decimal exists to avoid. Round for display at the consumer's discretion."""
    return _json_safe(result)


def write_json(result: CalculationResult, path: str | Path) -> None:
    with open(path, "w") as f:
        json.dump(to_json_dict(result), f, indent=2)
        f.write("\n")
