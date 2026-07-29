# FHA Cash-to-Close Calculator — Engineering Specification

**Target implementer:** Claude Code (local, on developer laptop)
**Language:** Python 3.11+
**Runtime constraint:** Fully offline. Zero network calls at runtime.

---

## 1. Purpose

A local CLI tool that answers one question: *"How much cash do I need saved to close on this house with an FHA loan?"*

Workflow the user actually performs:

1. User browses to a listing in their own browser and takes a screenshot manually.
2. User runs `fha-calc path/to/screenshot.png`.
3. Tool OCRs the screenshot, extracts candidate property figures, and **shows them for confirmation/correction**.
4. Tool computes a full cash-to-close breakdown and a monthly-payment estimate.
5. Tool prints a report and optionally writes JSON/CSV.

---

## 2. Hard constraints

These are non-negotiable and should be enforced in code and tests.

| Constraint | Rationale |
|---|---|
| **No outbound network requests at runtime.** No `requests`, `httpx`, `urllib`, `selenium`, `playwright`, or headless browsers anywhere in the runtime path. | Offline requirement, and it keeps the tool clear of any automated-access issue with listing sites. |
| **The tool never fetches a URL.** It accepts an image file (or a manually pasted set of numbers). If a user passes a URL, exit with a message explaining to screenshot the page instead. | Same as above. The screenshot is produced by a human browsing normally; the tool only ever reads a local file. |
| **All OCR-derived values are treated as *proposals*, never as truth.** Every extracted number is surfaced to the user for confirmation before it enters a calculation. | OCR on a dense listing page is unreliable. See §5. |
| **All rates, limits, and fee assumptions live in an editable config file**, not hardcoded in logic. | These change annually. The user must be able to update without touching code. |

Add a test that asserts no networking module is imported by the package (walk the AST of all source files, fail on a denylist of module names).

---

## 3. Architecture

```
fha_calc/
├── cli.py              # argparse entry point, orchestration, output rendering
├── ocr.py              # image → raw text + word/box data
├── extract.py          # raw text → CandidateFields (regex/heuristic layer)
├── confirm.py          # interactive confirm/override loop
├── models.py           # dataclasses: PropertyInputs, LoanConfig, CashToClose, MonthlyPayment
├── calc/
│   ├── loan.py         # base loan amount, LTV, loan-limit validation
│   ├── mip.py          # UFMIP + annual MIP, duration rules
│   ├── closing.py      # closing costs, itemized or percentage mode
│   ├── prepaids.py     # per-diem interest, escrow setup
│   ├── credits.py      # seller concessions, lender credits, DPA, gifts
│   └── summary.py      # assembles the final cash-to-close figure
├── config/
│   ├── defaults.toml   # rates, fee ranges, county limits, DPA programs
│   └── loader.py
└── report.py           # text table output, JSON/CSV export
tests/
```

Keep `calc/` **pure**: functions take dataclasses in, return dataclasses out. No I/O, no printing, no config file reads inside them (config is loaded once and passed down). This is what makes the math testable and is the most important structural decision in the project.

---

## 4. Data model

```python
@dataclass(frozen=True)
class PropertyInputs:
    purchase_price: Decimal
    annual_property_tax: Decimal | None      # if None, estimate via config tax rate
    annual_hoa: Decimal = Decimal(0)
    annual_homeowners_insurance: Decimal | None = None
    county: str | None = None                # for loan-limit lookup
    address: str | None = None               # display only
    sqft: int | None = None                  # display only

@dataclass(frozen=True)
class BuyerProfile:
    credit_score: int
    down_payment_pct: Decimal                # user-chosen; validated against tier minimum
    finance_ufmip: bool = True
    closing_date_day_of_month: int = 15      # drives per-diem interest
    interest_rate: Decimal                   # annual nominal
    loan_term_years: int = 30

@dataclass(frozen=True)
class Credits:
    seller_concessions: Decimal = Decimal(0)
    lender_credits: Decimal = Decimal(0)
    dpa_amount: Decimal = Decimal(0)
    gift_funds: Decimal = Decimal(0)
    earnest_money_already_paid: Decimal = Decimal(0)
```

**Use `decimal.Decimal` for all money.** Never float. Set context precision explicitly and round only at presentation time, using `ROUND_HALF_UP` to cents.

---

## 5. OCR pipeline (`ocr.py`)

**Dependencies:** `pytesseract` + system `tesseract` (5.x), `Pillow`, `opencv-python-headless` for preprocessing.

Verified working combination: tesseract 5.3.4, pytesseract, Pillow.

### Preprocessing (materially improves accuracy on listing screenshots)

1. Load image, convert to grayscale.
2. Upscale 2× with `cv2.INTER_CUBIC` — listing pages render prices at small point sizes and tesseract does poorly below ~20px cap height.
3. Otsu binarization (`cv2.threshold` with `THRESH_BINARY | THRESH_OTSU`).
4. Optional mild denoise (`cv2.medianBlur`, kernel 3) — make this a config flag, since it can hurt on already-clean screenshots.

### Extraction call

Use `pytesseract.image_to_data(img, output_type=Output.DICT, config='--psm 6')` rather than `image_to_string`. You need the **bounding boxes and confidence scores**, not just a text blob, because:

- Spatial position is how you disambiguate the list price (large, top-left, standalone) from the Zestimate or the "est. payment" figure.
- Per-word confidence lets you flag low-confidence numbers for mandatory user review.

Return a `OcrResult` containing: full reconstructed text, a list of `Word(text, conf, x, y, w, h)`, and image dimensions.

### Known failure modes to handle gracefully

- Dollar signs OCR'd as `S`, `$`→`§`, or dropped entirely.
- Commas read as periods, which silently changes `$285,000` into `$285.000`.
- `0`/`O` and `1`/`l`/`I` confusion.
- Prices rendered inside images or with unusual webfonts producing garbage.

Normalize with a `clean_currency(token)` helper: strip non-digit/period/comma chars, collapse thousands separators, then sanity-check magnitude (see §6).

---

## 6. Field extraction (`extract.py`)

Return a `CandidateFields` object where **every field carries `(value, confidence, source_snippet)`**, so the confirmation UI can show provenance.

### Strategy per field

**Purchase price** — highest-value, hardest field.
- Regex candidates: `\$\s?([\d,]{5,12})` across all text.
- Score candidates by: (a) largest font height in the word boxes, (b) proximity to top of image, (c) *absence* of nearby disqualifying words within ~200px — specifically `Zestimate`, `est.`, `estimated`, `rent`, `/mo`, `per month`, `price/sqft`.
- Plausibility filter: reject anything outside a configurable band (default $30,000–$5,000,000).
- Present the **top 3 ranked candidates** to the user, not just the winner.

**Annual property tax**
- Look for `tax`, `taxes`, `annual tax`, `property tax` and grab the nearest currency token to the right or below.
- Listing pages often show a *monthly* tax figure inside a payment breakdown. Detect `/mo` or `monthly` in the same row and multiply by 12, flagging that you did so.
- Fallback: if absent, estimate from `config.default_property_tax_rate × purchase_price` and mark the field as `ESTIMATED`.

**HOA** — search for `HOA`, `association`, `dues`. Same monthly/annual detection. Default to 0 if absent, but *say so in the report* rather than silently assuming.

**Homeowners insurance** — rarely present on listings. Expect to fall back to config estimate (a % of price, or a flat regional default).

**County** — parse from address if present; otherwise prompt. Needed for the loan-limit check.

**Do not attempt to extract** beds, baths, year built, or comps. They don't feed the calculation and each additional extraction target is added surface area for bugs.

### Confidence tiers

- `HIGH` — regex matched, tesseract conf > 80, passed plausibility check.
- `MEDIUM` — matched but conf 50–80, or required unit conversion.
- `LOW` — inferred by position only, or conf < 50.
- `MISSING` — not found; will use config default or prompt.

---

## 7. Confirmation step (`confirm.py`)

Non-optional. Render a table:

```
  Field                  Value        Confidence   Source
  Purchase price         $285,000     HIGH         "$285,000" (top-left, 32px)
  Annual property tax    $2,340       MEDIUM       "$195/mo" → ×12
  HOA                    $0           MISSING      not found — assumed none
  County                 Spartanburg  HIGH         parsed from address

  [Enter] accept all   [1-4] edit field   [q] quit
```

Anything below `HIGH` should be visually flagged. Let the user edit any field inline. Persist the confirmed values to a small local JSON cache keyed by a hash of the image, so re-runs skip re-confirmation.

Also support a `--manual` flag that skips OCR entirely and just prompts for the four numbers. **Build this path first** — it lets the entire calculation engine be developed and tested before OCR exists, and it's the fallback whenever OCR fails.

---

## 8. Calculation engine

This is the substance of the tool. All figures below are **defaults in config**, not hardcoded.

### 8.1 Down payment tier validation (`calc/loan.py`)

| Credit score | Minimum down payment |
|---|---|
| 580+ | 3.5% |
| 500–579 | 10% |
| below 500 | Not FHA-eligible — hard error |

Validate `down_payment_pct >= tier_minimum`; error clearly if not. Note in config comments that many lenders impose their own overlays around 620–640 even though FHA's floor is 580 — worth surfacing as a warning when score is 580–619.

```
down_payment      = purchase_price × down_payment_pct
base_loan_amount  = purchase_price − down_payment
ltv               = base_loan_amount / purchase_price
```

### 8.2 Loan limit check

Compare `base_loan_amount` against the county limit from config. If it exceeds, emit a hard error explaining the buyer must either increase the down payment to bridge the gap or use a different loan product.

Config ships with the 2026 national **floor of $541,287** and **ceiling of $1,249,125**, plus a `county_limits` table. Greenville County, SC = $541,287 (at the floor). The user should be told in comments to verify their county against HUD's official lookup, since limits reset annually.

### 8.3 Mortgage insurance (`calc/mip.py`)

**Upfront MIP (UFMIP):** `1.75% × base_loan_amount`.

Two branches, driven by `finance_ufmip`:
- **Financed (typical):** UFMIP is added to the loan → `total_loan_amount = base_loan_amount + ufmip`. Contributes **$0** to cash-to-close but increases the monthly payment.
- **Paid in cash:** contributes its full value to cash-to-close; `total_loan_amount = base_loan_amount`.

This toggle swings the cash requirement by thousands of dollars and is the single most commonly misunderstood line in an FHA quote. Make it prominent in output.

**Annual MIP:** rate ranges 0.15%–0.75% depending on term, LTV, and loan size; **0.55% is the common case for a 30-year loan above 95% LTV under the floor limit**. Implement as a lookup table in config keyed by `(term_bucket, ltv_bucket, loan_amount_bucket)`, with 0.55% as the fallback. Instruct the user in config comments to verify their tier against the current HUD mortgagee letter.

Note the annual MIP is technically assessed on the *average outstanding balance for the year*. A first-year approximation using the initial loan amount is fine for this tool — but label it as an approximation in the report rather than presenting it as exact.

**MIP duration** (affects total-cost display, not cash-to-close):
- Down payment < 10% → MIP for the **life of the loan**.
- Down payment ≥ 10% → **11 years**.

### 8.4 Closing costs (`calc/closing.py`)

Two modes:

**Percentage mode (default):** `closing_costs = pct × base_loan_amount`, with configurable low/mid/high at **2% / 3.5% / 5%**. Output should show a range, not a single point estimate — a single number here projects false precision.

**Itemized mode:** sum of individually configurable line items, each with a default range:

| Item | Typical range | Notes |
|---|---|---|
| Loan origination / underwriting / processing | 0.5–1% of loan | |
| Appraisal | $350–$1,000 | FHA appraisal required |
| Credit report | $30–$100 | |
| Lender's title insurance | $300–$1,500+ | |
| Owner's title insurance | varies | Optional — make it a flag |
| Title search | $200–$800 | |
| Settlement / escrow fee | $350–$1,000+ | |
| Recording fees | $20–$250 | |
| **Closing attorney fee** | $500–$1,200 | **South Carolina is an attorney-closing state — this is mandatory, not optional.** Default it on. |
| Home inspection | $300–$500 | |
| Termite / WDO inspection | $75–$150 | Common and often required in SC |
| Optional inspections | varies | radon, septic, well, sewer scope — individually toggleable |
| Transfer tax / deed stamps | varies by state | In SC customarily seller-paid — default to 0 for buyer with a config note |
| HOA transfer / estoppel fee | $0–$500 | Only if HOA > 0 |

### 8.5 Prepaids & escrow (`calc/prepaids.py`)

Frequently omitted from naive calculators; often roughly half of what a buyer actually pays at the table.

```
daily_interest    = (total_loan_amount × interest_rate) / 365
days_remaining    = days_in_closing_month − closing_date_day_of_month
prepaid_interest  = daily_interest × days_remaining

prepaid_insurance = annual_homeowners_insurance × 1        # first year upfront
tax_escrow        = (annual_property_tax / 12) × config.tax_escrow_months        # default 3
ins_escrow        = (annual_homeowners_insurance / 12) × config.ins_escrow_months # default 2
```

Expose `closing_date_day_of_month` as a CLI flag — closing late in the month meaningfully reduces prepaid interest, and showing that is genuinely useful.

### 8.6 Credits (`calc/credits.py`)

- **Seller concessions:** FHA permits up to **6% of purchase price** toward closing costs. Validate against that cap and warn if exceeded. Also cap the applied amount at actual closing costs + prepaids — concessions can't be pocketed as cash.
- **Lender credits:** free-form input.
- **DPA / grants:** config-driven program table. Ship with SC entries: **Palmetto Home Advantage** (forgivable second, 0% / 3% / 4% of loan amount, 640 minimum credit score) and **Palmetto Heroes** ($10,000 forgivable, for teachers, nurses, law enforcement, corrections, firefighters, EMTs, paramedics, veterans, active duty, National Guard; first-come first-served with annual funding cycles). Note in config that program terms and funding reset annually and must be verified with SC Housing.
- **Gift funds:** reduces personal savings needed; track separately so the report can show both "total cash to close" and "cash from your own savings."
- **Earnest money:** credited at closing, so it does *not* add to the total — but it must be liquid **earlier**. Report it as a separate timeline line, not as an addition.

### 8.7 Final assembly (`calc/summary.py`)

```
cash_to_close = down_payment
              + closing_costs
              + prepaids_and_escrow
              + (ufmip if not financed else 0)
              − seller_concessions
              − lender_credits
              − dpa_amount

cash_from_own_savings = cash_to_close − gift_funds
```

Also compute an optional **recommended reserve** (moving costs + immediate repairs + N months of PITI, N configurable, default 3), reported separately from the required figure. Not FHA-required; clearly labeled as a cushion.

### 8.8 Monthly payment module

```
P&I           = standard amortization on total_loan_amount
monthly_MIP   = (annual_mip_rate × total_loan_amount) / 12
monthly_tax   = annual_property_tax / 12
monthly_ins   = annual_homeowners_insurance / 12
monthly_hoa   = annual_hoa / 12
total_monthly = sum of the above
```

Add an optional DTI check: accept monthly gross income and existing monthly debts, compute back-end DTI. FHA generally targets **≤43%**, with higher ratios possible given compensating factors. Report it as informational — the tool should not claim to predict approval.

---

## 9. Config file (`config/defaults.toml`)

Every number in §8 lives here. Structure it with clear section comments including *when each value was last verified and where it came from*, since these expire annually:

```toml
[meta]
last_verified = "2026-07"
notes = "Verify FHA limits at HUD's lookup tool and MIP rates against the current mortgagee letter before relying on results."

[fha]
ufmip_rate = 0.0175
annual_mip_default = 0.0055
min_down_580_plus = 0.035
min_down_500_579 = 0.10
max_seller_concession_pct = 0.06

[loan_limits]
national_floor = 541287
national_ceiling = 1249125

[loan_limits.counties]
"Greenville, SC" = 541287
"Spartanburg, SC" = 541287

[closing_costs]
mode = "percentage"          # or "itemized"
pct_low = 0.02
pct_mid = 0.035
pct_high = 0.05
```

On startup, if `meta.last_verified` is more than 12 months old, print a warning that rates may be stale.

---

## 10. CLI surface

```
fha-calc SCREENSHOT.png [options]
fha-calc --manual

  --price DECIMAL              override/skip OCR for price
  --credit-score INT           required
  --down-pct DECIMAL           default: tier minimum
  --rate DECIMAL               annual interest rate, required
  --term INT                   default 30
  --finance-ufmip / --pay-ufmip-cash   default: finance
  --closing-day INT            day of month, default 15
  --seller-concessions DECIMAL
  --dpa PROGRAM_NAME|DECIMAL
  --gift DECIMAL
  --earnest DECIMAL
  --itemized                   use itemized closing costs
  --county STRING
  --json PATH                  export machine-readable result
  --explain                    print every intermediate value
```

`--explain` is worth building early. When a number looks wrong, being able to see the full derivation chain is the difference between a five-minute fix and an hour of print-statement debugging.

---

## 11. Output format

Three sections. Do not collapse them — the whole point is that a buyer sees where the money goes.

```
PROPERTY
  109 Cumberland Dr, Moore SC          $285,000

CASH TO CLOSE                                  LOW        LIKELY       HIGH
  Down payment (3.5%, 580+ tier)           $9,975      $9,975      $9,975
  Closing costs (2.0–5.0% of loan)         $5,501      $9,626     $13,751
  Prepaids & escrow setup                  $2,890      $2,890      $2,890
  Upfront MIP (1.75%)                    financed    financed    financed
  Less: seller concessions                      $0          $0          $0
  ─────────────────────────────────────────────────────────────────────
  TOTAL CASH NEEDED                       $18,366     $22,491     $26,616
    of which from your own savings        $18,366     $22,491     $26,616

  Earnest money (due at contract)          $2,850   ← needed earlier, credited back
  Suggested reserve (3 mo PITI + moving)   $9,200   ← not required

ESTIMATED MONTHLY PAYMENT
  Principal & interest                     $1,742
  MIP (0.55%/yr, life of loan)               $126
  Property tax                               $195
  Homeowners insurance                       $142
  HOA                                          $0
  ─────────────────────────────────────
  TOTAL                                    $2,205
```

Carry the low/likely/high range through the whole cash column. A single figure is misleading given that closing costs legitimately span a 2–5% range.

---

## 12. Testing

- **Golden-file tests** for the calc engine: a handful of fully worked scenarios with hand-verified expected outputs. Include at least: 3.5%-down at 580, 10%-down at 550, UFMIP financed vs. cash, a case with DPA + seller concessions, and a case that exceeds the county loan limit.
- **Property-based tests** (`hypothesis`): cash-to-close must never be negative; increasing down payment must never increase the loan amount; concessions applied must never exceed closing costs + prepaids.
- **Decimal discipline test:** assert no `float` appears in any money-carrying dataclass field.
- **OCR tests:** generate synthetic listing-like images with PIL at known values, assert extraction recovers them. Also keep 2–3 real screenshots as fixtures (they're your own screenshots of pages you visited, so no redistribution concern in a private repo).
- **No-network test:** AST walk over the package asserting nothing imports `requests`, `urllib`, `httpx`, `socket`, `selenium`, or `playwright`.

---

## 13. Build order

Do these in sequence — each stage is independently useful and testable.

1. **Models + config loader + `--manual` mode + calc engine + report.** This is a complete, working calculator with zero OCR. Get the math right first, verified against golden tests.
2. **`--explain` output.** Cheap to add, pays for itself immediately during stage 1 verification.
3. **OCR pipeline**, returning raw text and boxes. Test against synthetic images.
4. **Extraction heuristics** with candidate ranking and confidence tiers.
5. **Confirmation UI** and the image-hash result cache.
6. **JSON/CSV export**, itemized closing-cost mode, DTI module.

Stage 1 alone delivers most of the practical value. Everything after it is convenience.

---

## 14. Explicitly out of scope

- Any automated retrieval from listing sites — no URL fetching, no headless browser, no scraper. Screenshots are produced manually by the user.
- Zestimate or comps analysis.
- Amortization schedules beyond a simple monthly total (easy to add later; not needed for the core question).
- Any claim of approval prediction. The tool estimates cash requirements; it does not underwrite.

---

## 15. Accuracy disclaimer to print in the report footer

The tool should state plainly that figures are estimates based on user-supplied and config-supplied assumptions, that actual costs come from the lender's Loan Estimate and Closing Disclosure, and that rates, FHA limits, and assistance-program terms change. This is a planning aid for how much to save, not a quote.
