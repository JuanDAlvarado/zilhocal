"""argparse entry point, orchestration, output rendering."""

from __future__ import annotations

import argparse
import calendar
import re
import sys
from dataclasses import replace as dataclass_replace
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fha_calc.calc.closing import calculate_closing_costs
from fha_calc.calc.credits import apply_credits
from fha_calc.calc.loan import calculate_loan_amounts, tier_minimum_down_payment_pct
from fha_calc.calc.mip import calculate_mip
from fha_calc.calc.monthly import calculate_dti, calculate_monthly_payment
from fha_calc.calc.prepaids import calculate_prepaids
from fha_calc.calc.summary import assemble_cash_to_close
from fha_calc.config.loader import is_stale, load_config
from fha_calc.models import (
    BuyerProfile,
    CalculationResult,
    Credits,
    FhaCalcError,
    LoanConfig,
    PropertyInputs,
    PropertyResolutionNotes,
)
from fha_calc.report import render_report

_URL_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*://|www\.)")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fha-calc",
        description="Estimate cash-to-close and monthly payment for an FHA purchase.",
    )
    parser.add_argument("image", nargs="?", help="Path to a screenshot of the listing")
    parser.add_argument("--manual", action="store_true", help="skip OCR, prompt for the four numbers")

    parser.add_argument("--price", help="override/skip OCR for price")
    parser.add_argument("--credit-score", type=int, help="required")
    parser.add_argument("--down-pct", help="default: tier minimum for the credit score")
    parser.add_argument("--rate", help="annual interest rate, required")
    parser.add_argument("--term", type=int, default=30, help="loan term in years, default 30")

    ufmip = parser.add_mutually_exclusive_group()
    ufmip.add_argument("--finance-ufmip", dest="finance_ufmip", action="store_true", default=None)
    ufmip.add_argument("--pay-ufmip-cash", dest="finance_ufmip", action="store_false")

    parser.add_argument("--closing-day", type=int, default=15, help="day of month, default 15")
    parser.add_argument("--seller-concessions", default="0")
    parser.add_argument("--lender-credits", default="0")
    parser.add_argument("--dpa", help="PROGRAM_NAME|DECIMAL")
    parser.add_argument("--gift", default="0")
    parser.add_argument("--earnest", default="0")

    parser.add_argument("--itemized", action="store_true", help="use itemized closing costs")
    parser.add_argument("--owners-title-insurance", action="store_true")
    parser.add_argument("--radon-inspection", action="store_true")
    parser.add_argument("--septic-inspection", action="store_true")
    parser.add_argument("--well-inspection", action="store_true")
    parser.add_argument("--sewer-scope", action="store_true")

    parser.add_argument("--county", help="e.g. 'Spartanburg, SC' — used for the FHA loan-limit check")
    parser.add_argument(
        "--ocr-denoise", action="store_true", help="apply a mild median-blur denoise pass before OCR"
    )

    parser.add_argument("--monthly-income", help="optional, enables the informational DTI check")
    parser.add_argument("--monthly-debts", help="optional, enables the informational DTI check")

    parser.add_argument("--json", dest="json_path", metavar="PATH", help="export machine-readable result")
    parser.add_argument("--csv", dest="csv_path", metavar="PATH", help="export a CSV of every line item")
    parser.add_argument("--explain", action="store_true", help="print every intermediate value")
    parser.add_argument("--config", dest="config_path", help="path to a custom defaults.toml")

    return parser


def _parse_decimal(raw: str, flag_name: str) -> Decimal:
    try:
        return Decimal(raw.replace(",", "").replace("$", "").strip())
    except InvalidOperation:
        raise ValueError(f"Invalid value for {flag_name}: {raw!r}") from None


def _normalize_rate_or_pct(value: Decimal) -> Decimal:
    """Accept both '0.065' and '6.5' as meaning 6.5%."""
    return value / 100 if value > 1 else value


def _prompt_decimal(label: str, allow_blank: bool = False) -> Decimal | None:
    while True:
        raw = input(f"  {label}: ").strip()
        if not raw:
            if allow_blank:
                return None
            print("    A value is required.")
            continue
        try:
            return _parse_decimal(raw, label)
        except ValueError:
            print("    Couldn't parse that as a number, try again (e.g. 285000).")


def _looks_like_url(value: str) -> bool:
    return bool(_URL_RE.match(value.strip()))


def _resolve_dpa_arg(raw: str, config: LoanConfig) -> tuple[str | None, Decimal]:
    try:
        return None, _parse_decimal(raw, "--dpa")
    except ValueError:
        pass
    key = raw.strip().lower().replace(" ", "-").replace("_", "-")
    if any(p.key == key for p in config.dpa_programs):
        return key, Decimal(0)  # resolved from the program table inside calc/credits.py
    valid = ", ".join(p.key for p in config.dpa_programs)
    raise ValueError(f"Unknown --dpa value {raw!r}. Use a dollar amount or one of: {valid}")


def _finalize_property(
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


def resolve_property_manual(
    args: argparse.Namespace, config: LoanConfig, notes: list[str]
) -> tuple[PropertyInputs, PropertyResolutionNotes]:
    print("Manual entry — enter the property figures below (blank to skip where noted).\n")

    if args.price is not None:
        price = _parse_decimal(args.price, "--price")
    else:
        price = _prompt_decimal("Purchase price")

    tax_raw = _prompt_decimal("Annual property tax (blank to estimate from price)", allow_blank=True)
    hoa_raw = _prompt_decimal("Annual HOA dues (blank if none)", allow_blank=True)
    ins_raw = _prompt_decimal(
        "Annual homeowners insurance (blank to estimate from price)", allow_blank=True
    )

    return _finalize_property(price, tax_raw, hoa_raw, ins_raw, args.county, None, config, notes)


def resolve_property_via_image(
    args: argparse.Namespace, config: LoanConfig, notes: list[str]
) -> tuple[PropertyInputs, PropertyResolutionNotes]:
    from fha_calc.confirm import (
        confirm_fields,
        hash_image,
        load_cached_confirmation,
        save_confirmation_to_cache,
    )
    from fha_calc.extract import Confidence, FieldCandidate, extract_fields
    from fha_calc.ocr import run_ocr

    image_path = Path(args.image)
    image_hash = hash_image(image_path)
    cached = load_cached_confirmation(image_hash)

    if cached is not None:
        notes.append("Using a cached confirmation from a previous run against this exact image.")
        confirmed = cached
    else:
        print(f"Running OCR on {image_path}...\n")
        ocr_result = run_ocr(image_path, denoise=args.ocr_denoise)
        fields = extract_fields(ocr_result)

        # --price/--county explicitly override/skip OCR for that field (§10)
        # — surface the override as an already-high-confidence candidate
        # rather than bypassing confirmation entirely, so it still shows up
        # for the user to see (and, if they want, change).
        if args.price is not None:
            override_price = _parse_decimal(args.price, "--price")
            fields = dataclass_replace(
                fields,
                purchase_price_candidates=(FieldCandidate(override_price, Confidence.HIGH, "--price flag"),)
                + fields.purchase_price_candidates,
            )
        if args.county is not None:
            fields = dataclass_replace(fields, county=FieldCandidate(args.county, Confidence.HIGH, "--county flag"))

        confirmed = confirm_fields(fields)
        save_confirmation_to_cache(image_hash, confirmed)

    return _finalize_property(
        confirmed.purchase_price,
        confirmed.annual_property_tax,
        confirmed.annual_hoa,
        confirmed.annual_homeowners_insurance,
        confirmed.county,
        None,
        config,
        notes,
    )


def _days_in_next_closing_month(day_of_month: int, today: date | None = None) -> int:
    """The closing date itself isn't tracked as a full calendar date — only
    the day-of-month is exposed as a CLI flag (§8.5). This resolves it to
    the number of days in whichever month will next contain that day, so
    per-diem interest reflects real month length (28-31 days) without
    calc/prepaids.py itself touching the system clock."""
    today = today or date.today()
    year, month = today.year, today.month
    if today.day > day_of_month:
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return calendar.monthrange(year, month)[1]


def build_buyer_profile(args: argparse.Namespace, config: LoanConfig) -> BuyerProfile:
    if args.credit_score is None:
        raise ValueError("--credit-score is required")
    if args.rate is None:
        raise ValueError("--rate is required")

    interest_rate = _normalize_rate_or_pct(_parse_decimal(args.rate, "--rate"))

    if args.down_pct is not None:
        down_pct = _normalize_rate_or_pct(_parse_decimal(args.down_pct, "--down-pct"))
    else:
        down_pct = tier_minimum_down_payment_pct(args.credit_score, config)

    finance_ufmip = True if args.finance_ufmip is None else args.finance_ufmip

    return BuyerProfile(
        credit_score=args.credit_score,
        down_payment_pct=down_pct,
        interest_rate=interest_rate,
        finance_ufmip=finance_ufmip,
        closing_date_day_of_month=args.closing_day,
        loan_term_years=args.term,
    )


def build_credits(args: argparse.Namespace, config: LoanConfig) -> Credits:
    seller = _parse_decimal(args.seller_concessions, "--seller-concessions")
    lender = _parse_decimal(args.lender_credits, "--lender-credits")
    gift = _parse_decimal(args.gift, "--gift")
    earnest = _parse_decimal(args.earnest, "--earnest")

    dpa_program_key, dpa_amount = (None, Decimal(0))
    if args.dpa:
        dpa_program_key, dpa_amount = _resolve_dpa_arg(args.dpa, config)

    return Credits(
        seller_concessions=seller,
        lender_credits=lender,
        dpa_amount=dpa_amount,
        dpa_program_key=dpa_program_key,
        gift_funds=gift,
        earnest_money_already_paid=earnest,
    )


def run_calculation(
    property_inputs: PropertyInputs,
    property_notes: PropertyResolutionNotes,
    buyer: BuyerProfile,
    credits_in: Credits,
    args: argparse.Namespace,
    config: LoanConfig,
    notes: list[str],
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

    item_overrides: dict[str, bool] = {}
    if args.owners_title_insurance:
        item_overrides["owners_title"] = True
    if args.radon_inspection:
        item_overrides["radon_inspection"] = True
    if args.septic_inspection:
        item_overrides["septic_inspection"] = True
    if args.well_inspection:
        item_overrides["well_inspection"] = True
    if args.sewer_scope:
        item_overrides["sewer_scope"] = True

    mode = "itemized" if args.itemized else config.closing_cost_mode
    closing = calculate_closing_costs(mode, loan, property_inputs, config, item_overrides)

    days_in_month = _days_in_next_closing_month(buyer.closing_date_day_of_month, today)
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
    if args.monthly_income is not None and args.monthly_debts is not None:
        income = _parse_decimal(args.monthly_income, "--monthly-income")
        debts = _parse_decimal(args.monthly_debts, "--monthly-debts")
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


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.image and _looks_like_url(args.image):
        print(
            "This tool doesn't fetch URLs. Take a screenshot of the listing page "
            "and run:\n\n  fha-calc path/to/screenshot.png\n\ninstead of pointing "
            "it at a URL.",
            file=sys.stderr,
        )
        return 1

    try:
        config = load_config(args.config_path)
    except Exception as e:  # noqa: BLE001 - surfaced to the user, not a crash
        print(f"Error loading config: {e}", file=sys.stderr)
        return 1

    notes: list[str] = []
    if is_stale(config):
        notes.append(
            f"Config was last verified {config.last_verified}, more than 12 months "
            f"ago — rates and limits may be stale. {config.verification_notes}"
        )

    try:
        if args.image and not args.manual:
            property_inputs, property_notes = resolve_property_via_image(args, config, notes)
        else:
            property_inputs, property_notes = resolve_property_manual(args, config, notes)

        buyer = build_buyer_profile(args, config)
        credits_in = build_credits(args, config)
        result = run_calculation(property_inputs, property_notes, buyer, credits_in, args, config, notes)
    except FhaCalcError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except (ValueError, FileNotFoundError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130

    print(render_report(result, notes=notes, explain=args.explain))

    if args.json_path:
        from fha_calc.report import write_json

        write_json(result, args.json_path)
        print(f"\nWrote JSON export to {args.json_path}")

    if args.csv_path:
        from fha_calc.report import write_csv

        write_csv(result, args.csv_path)
        print(f"\nWrote CSV export to {args.csv_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
