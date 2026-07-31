"""Tests for extract.py (§6): clean_currency normalization and field
extraction against synthetic listing images with known values."""

from decimal import Decimal as D
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from fha_calc.extract import Confidence, clean_currency, extract_fields
from fha_calc.ocr import run_ocr

_FONT_CANDIDATES_BOLD = [
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
]
_FONT_CANDIDATES_REGULAR = [
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Regular.ttf",
]


def _load_font(candidates: list[str], size: int) -> ImageFont.FreeTypeFont:
    for path in candidates:
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    pytest.skip("No usable TrueType font found for synthetic OCR image generation")


@pytest.fixture
def listing_image(tmp_path) -> Path:
    font_big = _load_font(_FONT_CANDIDATES_BOLD, 32)
    font_small = _load_font(_FONT_CANDIDATES_REGULAR, 16)

    img = Image.new("RGB", (600, 320), "white")
    draw = ImageDraw.Draw(img)
    draw.text((20, 20), "285 Cumberland Dr, Moore, SC", fill="black", font=font_small)
    draw.text((20, 45), "$285,000", fill="black", font=font_big)
    draw.text((20, 105), "3 bd | 2 ba | 1,800 sqft", fill="black", font=font_small)
    draw.text((20, 135), "Zestimate: $291,400", fill="gray", font=font_small)
    draw.text((20, 165), "Est. payment: $1,950/mo", fill="gray", font=font_small)
    draw.text((20, 195), "HOA: $45/mo", fill="black", font=font_small)
    draw.text((20, 225), "Tax: $195/mo", fill="black", font=font_small)

    path = tmp_path / "listing.png"
    img.save(path)
    return path


# --- clean_currency -----------------------------------------------------


@pytest.mark.parametrize(
    "token,expected",
    [
        ("$285,000", D("285000")),
        ("S285,000", D("285000")),  # dollar sign misread as S
        ("§285,000", D("285000")),  # dollar sign misread as section symbol
        ("285.000", D("285000")),  # comma misread as period (thousands separator)
        ("1234.56", D("1234.56")),  # genuine decimal, not a misread comma
        ("$195/mo", D("195")),  # unit suffix must not corrupt the digits
        ("$45/mo", D("45")),
        ("l2,34O", D("12340")),  # l/I -> 1, O -> 0 digit confusion
        ("1O0", D("100")),
        ("", None),
        ("no digits here", None),
    ],
)
def test_clean_currency(token, expected):
    assert clean_currency(token) == expected


def test_clean_currency_unit_suffix_does_not_inject_phantom_digit():
    # Regression test: translating 'o' -> '0' before stripping "/mo" turned
    # "$195/mo" into "1950" (a phantom trailing zero from "mo" -> "m0").
    assert clean_currency("$195/mo") == D("195")


# --- extract_fields against a synthetic image ----------------------------


def test_extract_purchase_price_ranks_list_price_above_zestimate(listing_image):
    ocr = run_ocr(listing_image)
    fields = extract_fields(ocr)

    assert fields.purchase_price_candidates, "expected at least one price candidate"
    best = fields.best_purchase_price
    assert best.value == D("285000")
    assert best.confidence == Confidence.HIGH

    # Zestimate should still appear as a ranked alternate (top 3), just not first.
    values = [c.value for c in fields.purchase_price_candidates]
    assert D("285000") in values


def test_extract_annual_property_tax_converts_monthly(listing_image):
    ocr = run_ocr(listing_image)
    fields = extract_fields(ocr)

    assert fields.annual_property_tax.value == D("2340")  # $195/mo * 12
    assert fields.annual_property_tax.confidence == Confidence.MEDIUM  # unit-converted


def test_extract_hoa_converts_monthly(listing_image):
    ocr = run_ocr(listing_image)
    fields = extract_fields(ocr)

    assert fields.annual_hoa.value == D("540")  # $45/mo * 12
    assert fields.annual_hoa.confidence == Confidence.MEDIUM


def test_extract_hoa_defaults_to_zero_when_absent():
    from fha_calc.ocr import OcrResult, Word

    ocr = OcrResult(
        text="$285,000\nno HOA mentioned anywhere",
        words=(Word("$285,000", 92.0, 10, 10, 100, 30),),
        image_width=600,
        image_height=200,
    )
    fields = extract_fields(ocr)
    assert fields.annual_hoa.value == D("0")
    assert fields.annual_hoa.confidence == Confidence.MISSING


def test_extract_insurance_missing_when_not_present(listing_image):
    ocr = run_ocr(listing_image)
    fields = extract_fields(ocr)
    assert fields.annual_homeowners_insurance.value is None
    assert fields.annual_homeowners_insurance.confidence == Confidence.MISSING


def test_extract_county_guesses_city_state_from_address(listing_image):
    ocr = run_ocr(listing_image)
    fields = extract_fields(ocr)
    assert fields.county.value == "Moore, SC"
    assert fields.county.confidence == Confidence.LOW


def test_extract_purchase_price_rejects_implausible_values():
    from fha_calc.ocr import OcrResult, Word

    ocr = OcrResult(
        text="$5 special financing available",
        words=(Word("$5", 90.0, 10, 10, 20, 20),),
        image_width=600,
        image_height=200,
    )
    fields = extract_fields(ocr)
    assert fields.purchase_price_candidates == ()
