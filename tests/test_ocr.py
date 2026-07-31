"""OCR tests (§12): synthetic listing-like images at known values, asserting
extraction recovers them. Real screenshot fixtures aren't included here —
see tests/fixtures/README for how to add them locally."""

from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from fha_calc.ocr import preprocess_image, run_ocr

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


def _make_listing_image(path: Path) -> None:
    font_big = _load_font(_FONT_CANDIDATES_BOLD, 32)
    font_small = _load_font(_FONT_CANDIDATES_REGULAR, 16)

    img = Image.new("RGB", (600, 300), "white")
    draw = ImageDraw.Draw(img)
    draw.text((20, 20), "$285,000", fill="black", font=font_big)
    draw.text((20, 80), "3 bd | 2 ba | 1,800 sqft", fill="black", font=font_small)
    draw.text((20, 110), "Zestimate: $291,400", fill="gray", font=font_small)
    draw.text((20, 140), "Est. payment: $1,950/mo", fill="gray", font=font_small)
    draw.text((20, 170), "HOA: $45/mo", fill="black", font=font_small)
    draw.text((20, 200), "Tax: $195/mo", fill="black", font=font_small)
    img.save(path)


@pytest.fixture
def listing_image(tmp_path) -> Path:
    path = tmp_path / "synthetic_listing.png"
    _make_listing_image(path)
    return path


def test_ocr_recovers_known_values(listing_image):
    result = run_ocr(listing_image)

    assert "285,000" in result.text
    assert "291,400" in result.text
    assert "1,950" in result.text
    assert "45" in result.text
    assert "195" in result.text


def test_ocr_words_carry_position_and_confidence(listing_image):
    result = run_ocr(listing_image)

    price_words = [w for w in result.words if "285" in w.text]
    assert price_words, "expected to find a word containing the list price"
    price_word = price_words[0]
    assert price_word.conf > 0
    assert price_word.x >= 0 and price_word.y >= 0
    assert price_word.w > 0 and price_word.h > 0

    # The list price is rendered above (smaller y) the Zestimate line.
    zestimate_words = [w for w in result.words if "291" in w.text]
    assert zestimate_words
    assert price_word.y < zestimate_words[0].y


def test_ocr_image_dimensions_are_2x_upscaled(listing_image):
    result = run_ocr(listing_image)
    with Image.open(listing_image) as original:
        orig_w, orig_h = original.size
    assert result.image_width == orig_w * 2
    assert result.image_height == orig_h * 2


def test_ocr_rejects_url_input():
    with pytest.raises(ValueError, match="never fetches URLs"):
        run_ocr("https://example.com/listing.png")


def test_ocr_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        run_ocr(tmp_path / "does-not-exist.png")


def test_preprocess_image_upscales_and_binarizes(listing_image):
    import numpy as np

    from fha_calc.ocr import load_grayscale

    gray = load_grayscale(listing_image)
    processed = preprocess_image(gray, denoise=False)

    assert processed.shape == (gray.shape[0] * 2, gray.shape[1] * 2)
    unique_values = set(np.unique(processed).tolist())
    assert unique_values <= {0, 255}  # Otsu binarization -> strictly black/white


def test_preprocess_image_denoise_flag_runs(listing_image):
    from fha_calc.ocr import load_grayscale

    gray = load_grayscale(listing_image)
    denoised = preprocess_image(gray, denoise=True)
    plain = preprocess_image(gray, denoise=False)
    assert denoised.shape == plain.shape
