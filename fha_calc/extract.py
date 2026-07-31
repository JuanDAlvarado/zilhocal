"""Raw OCR text/words -> CandidateFields (§6). A regex/heuristic layer only
— no config reads, no purchase-price-driven estimation. Fields this can't
find come back MISSING with value=None (HOA is the one exception: it
defaults to $0, matching the confirmation-table example in §7). Resolving
MISSING fields into config-based ESTIMATED figures is the caller's job
(cli.py / confirm.py), the same code path --manual already uses.

Does not attempt beds, baths, year built, or comps — they don't feed the
calculation and are extra surface area for bugs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum

from fha_calc.ocr import OcrResult, Word

DEFAULT_PRICE_MIN = Decimal("30000")
DEFAULT_PRICE_MAX = Decimal("5000000")

_DISQUALIFYING_PHRASES = (
    "zestimate",
    "est.",
    "estimated",
    "rent",
    "/mo",
    "per month",
    "price/sqft",
    "$/sqft",
)
_PROXIMITY_PX = 200

_TAX_LABEL_KEYWORDS = ("tax",)
_HOA_LABEL_KEYWORDS = ("hoa", "association", "dues")
_MONTHLY_MARKERS = ("/mo", "per month", "/month", "monthly")

_DOLLAR_SIGN_RE = re.compile(r"[\$§]")
_ADDRESS_CITY_STATE_RE = re.compile(r"([A-Za-z][A-Za-z\s]{1,25}),\s*(SC)\b")
_UNIT_SUFFIX_RE = re.compile(r"(?i)/?(mo|month|months|yr|year|years)\.?$")


class Confidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    MISSING = "MISSING"


@dataclass(frozen=True)
class FieldCandidate:
    value: Decimal | str | None
    confidence: Confidence
    source_snippet: str


@dataclass(frozen=True)
class CandidateFields:
    purchase_price_candidates: tuple[FieldCandidate, ...]  # ranked best-first, up to 3
    annual_property_tax: FieldCandidate
    annual_hoa: FieldCandidate
    annual_homeowners_insurance: FieldCandidate
    county: FieldCandidate

    @property
    def best_purchase_price(self) -> FieldCandidate:
        if self.purchase_price_candidates:
            return self.purchase_price_candidates[0]
        return FieldCandidate(None, Confidence.MISSING, "no price-like text found")


def clean_currency(token: str) -> Decimal | None:
    """Normalizes an OCR'd currency-like token into a Decimal, handling the
    known failure modes from §5: dollar signs read as S/§ or dropped,
    commas misread as periods (thousands separator confusion), and 0/O,
    1/l/I digit confusion."""
    if not token:
        return None

    t = token.strip()
    t = re.sub(r"^[S§$]+", "", t)
    # Drop a real unit suffix ("/mo", "/month", "yr") before digit-confusion
    # cleanup — translating its letters first would corrupt "mo" into "m0"
    # and leave a phantom trailing zero. A targeted suffix match (rather
    # than stripping any trailing letters) also avoids swallowing a
    # legitimate trailing digit-confused "O" that should become "0".
    t = _UNIT_SUFFIX_RE.sub("", t)
    if not re.search(r"\d", t):
        return None  # nothing numeric here even before digit-confusion rescue
    t = t.translate(str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1", "|": "1"}))
    t = re.sub(r"[^0-9,.]", "", t)
    if not t:
        return None

    has_comma, has_period = "," in t, "." in t
    if has_comma and has_period:
        t = t.replace(",", "")
    elif has_period and not has_comma:
        integer_part, _, frac_part = t.rpartition(".")
        if len(frac_part) == 3:  # e.g. "285.000" — almost certainly a misread comma
            t = integer_part + frac_part
    elif has_comma and not has_period:
        t = t.replace(",", "")

    try:
        return Decimal(t)
    except InvalidOperation:
        return None


def _looks_currency_like(text: str) -> bool:
    digits = re.sub(r"[^0-9]", "", text)
    return len(digits) >= 2


def _confidence_tier(ocr_conf: float, unit_converted: bool = False, position_only: bool = False) -> Confidence:
    if position_only or ocr_conf < 50:
        return Confidence.LOW
    if unit_converted or ocr_conf <= 80:
        return Confidence.MEDIUM
    return Confidence.HIGH


def _group_lines(words: tuple[Word, ...]) -> list[list[Word]]:
    lines: dict[tuple[int, int, int], list[Word]] = {}
    order: list[tuple[int, int, int]] = []
    for w in words:
        if w.line_id not in lines:
            lines[w.line_id] = []
            order.append(w.line_id)
        lines[w.line_id].append(w)
    return [sorted(lines[key], key=lambda w: w.x) for key in order]


def _line_text(line: list[Word]) -> str:
    return " ".join(w.text for w in line)


def _is_monthly(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in _MONTHLY_MARKERS)


# ---------------------------------------------------------------------------
# Purchase price
# ---------------------------------------------------------------------------


def _price_candidates(ocr: OcrResult, price_min: Decimal, price_max: Decimal) -> list[FieldCandidate]:
    raw: list[tuple[Word, Decimal]] = []
    for w in ocr.words:
        if not _looks_currency_like(w.text):
            continue
        value = clean_currency(w.text)
        if value is None:
            continue
        raw.append((w, value))

    if not raw:
        return []

    max_height = max(w.h for w, _ in raw)
    scored: list[tuple[float, Word, Decimal]] = []
    for w, value in raw:
        if not (price_min <= value <= price_max):
            continue

        height_norm = w.h / max_height if max_height else 0
        y_norm = w.y / ocr.image_height if ocr.image_height else 0
        has_dollar_sign = bool(_DOLLAR_SIGN_RE.search(w.text))

        nearby_text = " ".join(
            other.text
            for other in ocr.words
            if other is not w and _chebyshev_distance(w, other) <= _PROXIMITY_PX
        ).lower()
        disqualified = any(phrase in nearby_text for phrase in _DISQUALIFYING_PHRASES)

        score = height_norm * 0.5 + (1 - y_norm) * 0.3 + (0.1 if has_dollar_sign else 0)
        if disqualified:
            score -= 1.0

        scored.append((score, w, value))

    scored.sort(key=lambda item: item[0], reverse=True)

    candidates = []
    for score, w, value in scored[:3]:
        confidence = _confidence_tier(w.conf)
        candidates.append(FieldCandidate(value, confidence, f"{w.text!r} (x={w.x}, y={w.y}, h={w.h}px)"))
    return candidates


def _chebyshev_distance(a: Word, b: Word) -> float:
    ax, ay = a.x + a.w / 2, a.y + a.h / 2
    bx, by = b.x + b.w / 2, b.y + b.h / 2
    return max(abs(ax - bx), abs(ay - by))


def extract_purchase_price(
    ocr: OcrResult, price_min: Decimal = DEFAULT_PRICE_MIN, price_max: Decimal = DEFAULT_PRICE_MAX
) -> tuple[FieldCandidate, ...]:
    return tuple(_price_candidates(ocr, price_min, price_max))


# ---------------------------------------------------------------------------
# Labeled currency fields (tax, HOA)
# ---------------------------------------------------------------------------


def _label_match_end_x(line: list[Word], keywords: tuple[str, ...]) -> int | None:
    end_x = None
    for w in line:
        low = w.text.lower()
        if any(kw in low for kw in keywords):
            end_x = w.x + w.w
    return end_x


def _find_labeled_value(lines: list[list[Word]], keywords: tuple[str, ...]):
    for i, line in enumerate(lines):
        text_low = _line_text(line).lower()
        if not any(kw in text_low for kw in keywords):
            continue

        label_end_x = _label_match_end_x(line, keywords)
        if label_end_x is not None:
            same_line = [w for w in line if w.x >= label_end_x and _looks_currency_like(w.text)]
            if same_line:
                value_word = same_line[0]
                value = clean_currency(value_word.text)
                if value is not None:
                    return value_word, value, _is_monthly(text_low), _line_text(line)

        for j in range(i + 1, min(i + 3, len(lines))):
            below = lines[j]
            below_text = _line_text(below)
            below_candidates = [w for w in below if _looks_currency_like(w.text)]
            if below_candidates:
                value_word = below_candidates[0]
                value = clean_currency(value_word.text)
                if value is not None:
                    monthly = _is_monthly(text_low) or _is_monthly(below_text)
                    return value_word, value, monthly, f"{_line_text(line)} -> {below_text}"
    return None


def extract_annual_property_tax(ocr: OcrResult) -> FieldCandidate:
    lines = _group_lines(ocr.words)
    found = _find_labeled_value(lines, _TAX_LABEL_KEYWORDS)
    if found is None:
        return FieldCandidate(None, Confidence.MISSING, "not found")

    value_word, value, monthly, snippet = found
    annual_value = value * 12 if monthly else value
    confidence = _confidence_tier(value_word.conf, unit_converted=monthly)
    display_snippet = f"{snippet!r} -> ×12" if monthly else repr(snippet)
    return FieldCandidate(annual_value, confidence, display_snippet)


def extract_hoa(ocr: OcrResult) -> FieldCandidate:
    lines = _group_lines(ocr.words)
    found = _find_labeled_value(lines, _HOA_LABEL_KEYWORDS)
    if found is None:
        return FieldCandidate(Decimal(0), Confidence.MISSING, "not found — assumed none")

    value_word, value, monthly, snippet = found
    annual_value = value * 12 if monthly else value
    confidence = _confidence_tier(value_word.conf, unit_converted=monthly)
    display_snippet = f"{snippet!r} -> ×12" if monthly else repr(snippet)
    return FieldCandidate(annual_value, confidence, display_snippet)


def extract_homeowners_insurance(ocr: OcrResult) -> FieldCandidate:
    # Rarely present on listing pages at all; if a label happens to show up
    # (some listings include an "est. payment" breakdown with an insurance
    # line), we'll still try, but expect MISSING most of the time.
    lines = _group_lines(ocr.words)
    found = _find_labeled_value(lines, ("insurance", "hoi"))
    if found is None:
        return FieldCandidate(None, Confidence.MISSING, "not found")

    value_word, value, monthly, snippet = found
    annual_value = value * 12 if monthly else value
    confidence = _confidence_tier(value_word.conf, unit_converted=monthly)
    display_snippet = f"{snippet!r} -> ×12" if monthly else repr(snippet)
    return FieldCandidate(annual_value, confidence, display_snippet)


# ---------------------------------------------------------------------------
# County
# ---------------------------------------------------------------------------


def extract_county(ocr: OcrResult) -> FieldCandidate:
    """Best-effort only: without a network geocoding lookup there's no way
    to map a street address to its county, so this looks for a "City, SC"
    pattern near the top of the image and surfaces it as a LOW-confidence
    guess (it's a city, not a verified county) for the user to confirm or
    replace."""
    match = _ADDRESS_CITY_STATE_RE.search(ocr.text)
    if not match:
        return FieldCandidate(None, Confidence.MISSING, "not found — no address text detected")

    guess = f"{match.group(1).strip()}, {match.group(2)}"
    return FieldCandidate(
        guess, Confidence.LOW, f"parsed from address text {match.group(0)!r} — city/state, not a verified county"
    )


def extract_fields(
    ocr: OcrResult, price_min: Decimal = DEFAULT_PRICE_MIN, price_max: Decimal = DEFAULT_PRICE_MAX
) -> CandidateFields:
    return CandidateFields(
        purchase_price_candidates=extract_purchase_price(ocr, price_min, price_max),
        annual_property_tax=extract_annual_property_tax(ocr),
        annual_hoa=extract_hoa(ocr),
        annual_homeowners_insurance=extract_homeowners_insurance(ocr),
        county=extract_county(ocr),
    )
