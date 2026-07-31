"""Interactive confirm/override loop (§7) and the image-hash result cache.

Every OCR-derived value is a *proposal*, never truth (§2) — this module is
the mandatory gate between extract.py's guesses and anything entering a
calculation. Confirmed values are cached by image hash so re-runs against
the same screenshot skip re-confirmation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

from fha_calc.extract import CandidateFields, Confidence, FieldCandidate, clean_currency

DEFAULT_CACHE_DIR = Path(".fha_calc_cache")


@dataclass(frozen=True)
class ConfirmedFields:
    purchase_price: Decimal
    annual_property_tax: Decimal | None  # None -> caller estimates from config
    annual_hoa: Decimal
    annual_homeowners_insurance: Decimal | None  # None -> caller estimates from config
    county: str | None


@dataclass
class _Row:
    kind: str  # "price" | "tax" | "hoa" | "insurance" | "county"
    label: str
    value: Decimal | str | None
    confidence: Confidence
    source: str


def _display_value(row: _Row) -> str:
    if row.value is None:
        return "(none)"
    if row.kind == "county":
        return str(row.value)
    return f"${row.value:,.0f}"


def _build_rows(fields: CandidateFields) -> list[_Row]:
    price = fields.best_purchase_price
    return [
        _Row("price", "Purchase price", price.value, price.confidence, price.source_snippet),
        _Row(
            "tax",
            "Annual property tax",
            fields.annual_property_tax.value,
            fields.annual_property_tax.confidence,
            fields.annual_property_tax.source_snippet,
        ),
        _Row("hoa", "HOA", fields.annual_hoa.value, fields.annual_hoa.confidence, fields.annual_hoa.source_snippet),
        _Row(
            "insurance",
            "Homeowners insurance",
            fields.annual_homeowners_insurance.value,
            fields.annual_homeowners_insurance.confidence,
            fields.annual_homeowners_insurance.source_snippet,
        ),
        _Row("county", "County", fields.county.value, fields.county.confidence, fields.county.source_snippet),
    ]


def render_confirmation_table(rows: list[_Row]) -> str:
    lines = [f"  {'Field':<24}{'Value':<14}{'Confidence':<13}Source"]
    for i, row in enumerate(rows, start=1):
        conf_text = row.confidence.value if row.confidence == Confidence.HIGH else f"! {row.confidence.value}"
        lines.append(f"  {i}. {row.label:<21}{_display_value(row):<14}{conf_text:<13}{row.source}")
    lines.append("")
    lines.append(f"  [Enter] accept all   [1-{len(rows)}] edit field   [q] quit")
    return "\n".join(lines)


def _prompt_money(label: str) -> Decimal | None:
    raw = input(f"    New value for {label} ($, blank to clear/estimate): ").strip()
    if not raw:
        return None
    value = clean_currency(raw)
    if value is None:
        print("    Couldn't parse that as a dollar amount — leaving unchanged.")
        return None
    return value


def _edit_row(row: _Row, price_candidates: tuple[FieldCandidate, ...]) -> _Row:
    if row.kind == "county":
        raw = input("    New county (e.g. 'Greenville, SC', blank to clear): ").strip()
        return replace(row, value=raw or None, confidence=Confidence.HIGH, source="manually entered")

    if row.kind == "price" and len(price_candidates) > 1:
        print("    Candidates:")
        for i, c in enumerate(price_candidates, start=1):
            print(f"      {i}. ${c.value:,.0f}  ({c.confidence.value}) — {c.source_snippet}")
        print(f"      {len(price_candidates) + 1}. enter a custom value")
        choice = input("    Pick a number: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(price_candidates):
            picked = price_candidates[int(choice) - 1]
            return replace(row, value=picked.value, confidence=picked.confidence, source=picked.source_snippet)
        # falls through to custom entry for anything else, including the
        # explicit "enter a custom value" option

    value = _prompt_money(row.label)
    if value is None:
        return replace(row, value=None, confidence=Confidence.MISSING, source="manually cleared")
    return replace(row, value=value, confidence=Confidence.HIGH, source="manually entered")


def confirm_fields(fields: CandidateFields) -> ConfirmedFields:
    rows = _build_rows(fields)
    while True:
        print(render_confirmation_table(rows))
        choice = input("> ").strip().lower()
        if choice == "":
            break
        if choice == "q":
            raise KeyboardInterrupt("User cancelled at confirmation prompt")
        if choice.isdigit() and 1 <= int(choice) <= len(rows):
            idx = int(choice) - 1
            rows[idx] = _edit_row(rows[idx], fields.purchase_price_candidates)
            continue
        print("  Unrecognized input.\n")

    by_kind = {row.kind: row for row in rows}
    price = by_kind["price"].value
    if price is None:
        raise ValueError("Purchase price is required and can't be left blank.")

    return ConfirmedFields(
        purchase_price=price,
        annual_property_tax=by_kind["tax"].value,
        annual_hoa=by_kind["hoa"].value if by_kind["hoa"].value is not None else Decimal(0),
        annual_homeowners_insurance=by_kind["insurance"].value,
        county=by_kind["county"].value,
    )


# ---------------------------------------------------------------------------
# Image-hash result cache
# ---------------------------------------------------------------------------


def hash_image(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]


def _cache_path(image_hash: str, cache_dir: Path) -> Path:
    return cache_dir / f"{image_hash}.json"


def load_cached_confirmation(image_hash: str, cache_dir: Path = DEFAULT_CACHE_DIR) -> ConfirmedFields | None:
    path = _cache_path(image_hash, cache_dir)
    if not path.is_file():
        return None
    data = json.loads(path.read_text())
    return ConfirmedFields(
        purchase_price=Decimal(data["purchase_price"]),
        annual_property_tax=Decimal(data["annual_property_tax"]) if data["annual_property_tax"] is not None else None,
        annual_hoa=Decimal(data["annual_hoa"]),
        annual_homeowners_insurance=(
            Decimal(data["annual_homeowners_insurance"]) if data["annual_homeowners_insurance"] is not None else None
        ),
        county=data["county"],
    )


def save_confirmation_to_cache(
    image_hash: str, confirmed: ConfirmedFields, cache_dir: Path = DEFAULT_CACHE_DIR
) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "purchase_price": str(confirmed.purchase_price),
        "annual_property_tax": str(confirmed.annual_property_tax) if confirmed.annual_property_tax is not None else None,
        "annual_hoa": str(confirmed.annual_hoa),
        "annual_homeowners_insurance": (
            str(confirmed.annual_homeowners_insurance) if confirmed.annual_homeowners_insurance is not None else None
        ),
        "county": confirmed.county,
    }
    _cache_path(image_hash, cache_dir).write_text(json.dumps(data, indent=2) + "\n")
