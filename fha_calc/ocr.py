"""Image -> raw text + word/box data (§5).

Verified working combination: tesseract 5.3.4+, pytesseract, Pillow. Only
ever reads a local file — never a URL (cli.py rejects URL-shaped arguments
before this module is reached, and this module re-checks defensively so it
stays safe to call directly, e.g. from tests).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import pytesseract
from PIL import Image
from pytesseract import Output

_URL_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*://|www\.)")


@dataclass(frozen=True)
class Word:
    text: str
    conf: float
    x: int
    y: int
    w: int
    h: int


@dataclass(frozen=True)
class OcrResult:
    text: str
    words: tuple[Word, ...]
    image_width: int
    image_height: int


def load_grayscale(path: Path) -> np.ndarray:
    with Image.open(path) as img:
        return np.array(img.convert("L"))


def preprocess_image(gray: np.ndarray, denoise: bool = False) -> np.ndarray:
    """Grayscale in, ready-for-tesseract binary image out (§5 preprocessing).

    Upscaling 2x materially helps: listing pages render prices at small
    point sizes and tesseract does poorly below ~20px cap height. Denoise is
    a caller-controlled flag, not a fixed default, since it can hurt on
    already-clean screenshots.
    """
    upscaled = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    _, binarized = cv2.threshold(upscaled, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    if denoise:
        binarized = cv2.medianBlur(binarized, 3)
    return binarized


def _reconstruct_text(data: dict) -> str:
    lines: dict[tuple[int, int, int], list[str]] = {}
    for i in range(len(data["text"])):
        text = data["text"][i].strip()
        if not text:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append(text)
    return "\n".join(" ".join(words) for words in lines.values())


def run_ocr(image_path: str | Path, denoise: bool = False, psm: int = 6) -> OcrResult:
    raw = str(image_path)
    if _URL_RE.match(raw.strip()):
        raise ValueError(
            "This tool never fetches URLs. Take a screenshot of the listing page "
            "and pass that image file instead."
        )

    path = Path(image_path)
    if not path.is_file():
        raise FileNotFoundError(f"No such image file: {path}")

    gray = load_grayscale(path)
    processed = preprocess_image(gray, denoise=denoise)
    height, width = processed.shape[:2]

    data = pytesseract.image_to_data(processed, output_type=Output.DICT, config=f"--psm {psm}")

    words = []
    for i in range(len(data["text"])):
        text = data["text"][i].strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][i])
        except (ValueError, TypeError):
            conf = -1.0
        words.append(
            Word(
                text=text,
                conf=conf,
                x=int(data["left"][i]),
                y=int(data["top"][i]),
                w=int(data["width"][i]),
                h=int(data["height"][i]),
            )
        )

    return OcrResult(
        text=_reconstruct_text(data),
        words=tuple(words),
        image_width=width,
        image_height=height,
    )
