"""argparse entry point, orchestration, output rendering."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import replace as dataclass_replace
from pathlib import Path

from fha_calc import orchestrate
from fha_calc.config.loader import is_stale, load_config
from fha_calc.models import FhaCalcError, LoanConfig, PropertyInputs, PropertyResolutionNotes
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


def _prompt_decimal(label: str, allow_blank: bool = False):
    while True:
        raw = input(f"  {label}: ").strip()
        if not raw:
            if allow_blank:
                return None
            print("    A value is required.")
            continue
        try:
            return orchestrate.parse_decimal(raw, label)
        except ValueError:
            print("    Couldn't parse that as a number, try again (e.g. 285000).")


def _looks_like_url(value: str) -> bool:
    return bool(_URL_RE.match(value.strip()))


def resolve_property_manual(
    args: argparse.Namespace, config: LoanConfig, notes: list[str]
) -> tuple[PropertyInputs, PropertyResolutionNotes]:
    print("Manual entry — enter the property figures below (blank to skip where noted).\n")

    if args.price is not None:
        price = orchestrate.parse_decimal(args.price, "--price")
    else:
        price = _prompt_decimal("Purchase price")

    tax_raw = _prompt_decimal("Annual property tax (blank to estimate from price)", allow_blank=True)
    hoa_raw = _prompt_decimal("Annual HOA dues (blank if none)", allow_blank=True)
    ins_raw = _prompt_decimal(
        "Annual homeowners insurance (blank to estimate from price)", allow_blank=True
    )

    return orchestrate.finalize_property(price, tax_raw, hoa_raw, ins_raw, args.county, None, config, notes)


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
            override_price = orchestrate.parse_decimal(args.price, "--price")
            fields = dataclass_replace(
                fields,
                purchase_price_candidates=(FieldCandidate(override_price, Confidence.HIGH, "--price flag"),)
                + fields.purchase_price_candidates,
            )
        if args.county is not None:
            fields = dataclass_replace(fields, county=FieldCandidate(args.county, Confidence.HIGH, "--county flag"))

        confirmed = confirm_fields(fields)
        save_confirmation_to_cache(image_hash, confirmed)

    return orchestrate.finalize_property(
        confirmed.purchase_price,
        confirmed.annual_property_tax,
        confirmed.annual_hoa,
        confirmed.annual_homeowners_insurance,
        confirmed.county,
        None,
        config,
        notes,
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

        buyer = orchestrate.build_buyer_profile(
            credit_score=args.credit_score,
            rate=args.rate,
            down_pct=args.down_pct,
            finance_ufmip=args.finance_ufmip,
            closing_day=args.closing_day,
            term=args.term,
            config=config,
        )
        credits_in = orchestrate.build_credits(
            seller_concessions=args.seller_concessions,
            lender_credits=args.lender_credits,
            gift=args.gift,
            earnest=args.earnest,
            dpa=args.dpa,
            config=config,
        )

        item_overrides = {
            "owners_title": args.owners_title_insurance,
            "radon_inspection": args.radon_inspection,
            "septic_inspection": args.septic_inspection,
            "well_inspection": args.well_inspection,
            "sewer_scope": args.sewer_scope,
        }
        result = orchestrate.run_calculation(
            property_inputs,
            property_notes,
            buyer,
            credits_in,
            config,
            notes,
            closing_mode="itemized" if args.itemized else None,
            item_overrides={k: v for k, v in item_overrides.items() if v},
            monthly_income=args.monthly_income,
            monthly_debts=args.monthly_debts,
        )
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
