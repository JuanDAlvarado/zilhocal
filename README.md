# zilhocal

A local, fully offline FHA cash-to-close calculator.

Answers one question: *"How much cash do I need saved to close on this house with an FHA loan?"*

Workflow:

1. Browse to a listing in your own browser and take a screenshot.
2. Run `fha-calc path/to/screenshot.png`.
3. The tool OCRs the screenshot, extracts candidate figures, and shows them for confirmation/correction.
4. It computes a full cash-to-close breakdown and a monthly-payment estimate.
5. It prints a report and optionally writes JSON.

No network calls at runtime. See `fha-cash-to-close-spec.md` for the full engineering spec.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

## Usage

```bash
fha-calc --manual
fha-calc path/to/screenshot.png
```

## Tests

```bash
.venv/bin/pytest
```
