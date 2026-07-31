# zilhocal

A local, fully offline FHA cash-to-close calculator.

Answers one question: *"How much cash do I need saved to close on this house with an FHA loan?"*

Workflow:

1. Browse to a listing in your own browser and take a screenshot.
2. Run `fha-calc path/to/screenshot.png`.
3. The tool OCRs the screenshot, extracts candidate figures, and shows them for confirmation/correction.
4. It computes a full cash-to-close breakdown and a monthly-payment estimate.
5. It prints a report and optionally writes JSON/CSV.

No network calls at runtime — this tool never fetches a URL. If you pass one
by mistake, it tells you to screenshot the page instead. See
`fha-cash-to-close-spec.md` for the full engineering spec.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Requires system `tesseract` (5.x) with the English trained-data package
installed (`tesseract-data-eng` on Arch, `tesseract-ocr-eng` on Debian/Ubuntu)
for the OCR path. `--manual` mode doesn't need tesseract at all.

## Usage

```bash
# Manual entry — no OCR, prompts for the four property numbers
fha-calc --manual --credit-score 680 --rate 6.5 --county "Spartanburg, SC"

# From a screenshot — OCRs it, ranks candidate values, and asks you to
# confirm/correct each one before anything is calculated
fha-calc path/to/screenshot.png --credit-score 680 --rate 6.5

# Re-running against the exact same screenshot reuses the cached
# confirmation instead of asking again
```

Common flags (see `fha-calc --help` for the full list):

```
--price DECIMAL              override/skip OCR for price
--credit-score INT           required
--down-pct DECIMAL           default: tier minimum for the credit score
--rate DECIMAL                required (annual interest rate)
--term INT                   default 30
--finance-ufmip / --pay-ufmip-cash   default: finance
--closing-day INT            day of month, default 15
--seller-concessions / --lender-credits / --dpa / --gift / --earnest
--itemized                   itemized closing costs instead of percentage-range
--owners-title-insurance / --radon-inspection / --septic-inspection /
--well-inspection / --sewer-scope
--county STRING
--monthly-income / --monthly-debts   optional, enables an informational DTI check
--json PATH / --csv PATH     machine-readable export
--explain                    print every intermediate value
--ocr-denoise                mild denoise pass before OCR (can help on noisy screenshots)
```

## Tests

```bash
.venv/bin/pytest
```
