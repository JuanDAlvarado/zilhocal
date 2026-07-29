"""Core data model shared by the calc engine, CLI, and report layers.

All money fields use Decimal. Context precision is set explicitly here since
this module is imported everywhere before any Decimal arithmetic happens.
Rounding to cents happens only at presentation time (see round_money), never
inside the calc engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, getcontext

getcontext().prec = 28

CENTS = Decimal("0.01")


def round_money(value: Decimal) -> Decimal:
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Domain errors
# ---------------------------------------------------------------------------


class FhaCalcError(ValueError):
    """Base class for errors that should be reported to the user, not crash."""


class FhaEligibilityError(FhaCalcError):
    """Buyer does not meet FHA's minimum eligibility bar (credit score < 500,
    or down payment below the tier minimum for their score)."""


class LoanLimitExceededError(FhaCalcError):
    """Base loan amount exceeds the applicable county/national FHA limit."""


class DpaEligibilityError(FhaCalcError):
    """Buyer does not meet a requested down-payment-assistance program's
    stated minimum credit score."""


# ---------------------------------------------------------------------------
# User-supplied inputs (§4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PropertyInputs:
    purchase_price: Decimal
    annual_property_tax: Decimal | None  # if None, estimate via config tax rate
    annual_hoa: Decimal = Decimal(0)
    annual_homeowners_insurance: Decimal | None = None
    county: str | None = None  # for loan-limit lookup
    address: str | None = None  # display only
    sqft: int | None = None  # display only


@dataclass(frozen=True)
class BuyerProfile:
    # Reordered vs. the spec prose so required fields precede defaulted ones
    # (Python dataclasses reject non-default fields after default fields).
    credit_score: int
    down_payment_pct: Decimal  # user-chosen; validated against tier minimum
    interest_rate: Decimal  # annual nominal
    finance_ufmip: bool = True
    closing_date_day_of_month: int = 15  # drives per-diem interest
    loan_term_years: int = 30


@dataclass(frozen=True)
class Credits:
    seller_concessions: Decimal = Decimal(0)
    lender_credits: Decimal = Decimal(0)
    dpa_amount: Decimal = Decimal(0)
    gift_funds: Decimal = Decimal(0)
    earnest_money_already_paid: Decimal = Decimal(0)
    dpa_program_key: str | None = None  # set when --dpa named a config program


@dataclass(frozen=True)
class PropertyResolutionNotes:
    """Flags for values the CLI filled in rather than the user providing,
    so the report can say so instead of presenting them as given facts."""

    tax_estimated: bool = False
    hoa_assumed_zero: bool = False
    insurance_estimated: bool = False


# ---------------------------------------------------------------------------
# Config (loaded once by config/loader.py, passed down — never read from
# inside calc/) (§9)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MipRule:
    term_years_max: int  # rule applies when loan_term_years <= this
    ltv_max: Decimal  # rule applies when ltv <= this
    loan_amount_max: Decimal | None  # None = unbounded
    rate: Decimal


@dataclass(frozen=True)
class ClosingCostItem:
    key: str
    label: str
    basis: str  # "pct_of_loan" | "flat"
    low: Decimal
    high: Decimal
    enabled: bool
    required: bool = False  # cannot be disabled (e.g. SC attorney fee)
    only_if_hoa: bool = False
    note: str = ""


@dataclass(frozen=True)
class DpaProgram:
    key: str
    label: str
    kind: str  # "pct_of_loan" | "flat"
    value: Decimal  # pct (e.g. 0.03) if kind == pct_of_loan, else flat dollars
    min_credit_score: int
    notes: str = ""


@dataclass(frozen=True)
class LoanConfig:
    last_verified: str
    verification_notes: str

    ufmip_rate: Decimal
    annual_mip_default: Decimal
    min_down_580_plus: Decimal
    min_down_500_579: Decimal
    fha_min_credit_score: int
    lender_overlay_score_threshold: int
    max_seller_concession_pct: Decimal
    mip_11_year_down_pct_threshold: Decimal

    national_floor: Decimal
    national_ceiling: Decimal
    county_limits: dict[str, Decimal]

    mip_rules: tuple[MipRule, ...]

    closing_cost_mode: str  # "percentage" | "itemized"
    closing_pct_low: Decimal
    closing_pct_mid: Decimal
    closing_pct_high: Decimal
    closing_items: tuple[ClosingCostItem, ...]

    tax_escrow_months: int
    ins_escrow_months: int
    default_property_tax_rate: Decimal
    default_hoi_rate: Decimal

    dpa_programs: tuple[DpaProgram, ...]

    reserve_piti_months_default: int
    reserve_moving_costs_default: Decimal
    reserve_immediate_repairs_default: Decimal

    dti_target_back_end: Decimal


# ---------------------------------------------------------------------------
# Calc engine outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CostEstimate:
    """A low/likely/high range. Closing costs are the only figure that
    legitimately varies this way; everything else is carried as a flat
    value replicated across all three columns so the report can treat the
    whole cash column uniformly (§11)."""

    low: Decimal
    likely: Decimal
    high: Decimal

    @classmethod
    def flat(cls, value: Decimal) -> "CostEstimate":
        return cls(value, value, value)

    def __add__(self, other: "CostEstimate") -> "CostEstimate":
        return CostEstimate(
            self.low + other.low,
            self.likely + other.likely,
            self.high + other.high,
        )

    def __sub__(self, other: "CostEstimate") -> "CostEstimate":
        return CostEstimate(
            self.low - other.low,
            self.likely - other.likely,
            self.high - other.high,
        )

    def sub_flat(self, amount: Decimal) -> "CostEstimate":
        return CostEstimate(self.low - amount, self.likely - amount, self.high - amount)

    def floor_zero(self) -> "CostEstimate":
        zero = Decimal(0)
        return CostEstimate(max(self.low, zero), max(self.likely, zero), max(self.high, zero))


@dataclass(frozen=True)
class LoanAmounts:
    down_payment: Decimal
    base_loan_amount: Decimal
    ltv: Decimal
    down_payment_pct: Decimal
    tier_minimum_pct: Decimal
    overlay_warning: bool  # score in [580, lender_overlay_score_threshold)
    applicable_loan_limit: Decimal
    county_label: str


@dataclass(frozen=True)
class MipResult:
    ufmip_amount: Decimal
    ufmip_financed: bool
    total_loan_amount: Decimal  # base + ufmip if financed, else base
    annual_mip_rate: Decimal
    monthly_mip: Decimal
    mip_duration_years: int | None  # None means life of loan


@dataclass(frozen=True)
class LineItem:
    label: str
    low: Decimal
    high: Decimal


@dataclass(frozen=True)
class ClosingCosts:
    estimate: CostEstimate
    mode: str  # "percentage" | "itemized"
    line_items: tuple[LineItem, ...] = ()


@dataclass(frozen=True)
class Prepaids:
    prepaid_interest: Decimal
    days_of_prepaid_interest: int
    prepaid_insurance: Decimal
    tax_escrow: Decimal
    ins_escrow: Decimal

    @property
    def total(self) -> Decimal:
        return self.prepaid_interest + self.prepaid_insurance + self.tax_escrow + self.ins_escrow


@dataclass(frozen=True)
class CreditsApplied:
    seller_concessions_offered: Decimal
    seller_concessions_applied: CostEstimate
    seller_concession_cap: Decimal
    seller_concession_exceeds_cap: bool
    lender_credits: Decimal
    dpa_amount: Decimal
    dpa_program_label: str | None
    gift_funds: Decimal
    earnest_money_already_paid: Decimal


@dataclass(frozen=True)
class ReserveEstimate:
    months_piti: int
    piti_reserve: Decimal
    moving_costs: Decimal
    immediate_repairs: Decimal

    @property
    def total(self) -> Decimal:
        return self.piti_reserve + self.moving_costs + self.immediate_repairs


@dataclass(frozen=True)
class CashToClose:
    down_payment: CostEstimate
    closing_costs: CostEstimate
    prepaids_and_escrow: CostEstimate
    ufmip_cash_component: CostEstimate  # flat 0s if financed
    seller_concessions_applied: CostEstimate
    lender_credits: Decimal
    dpa_amount: Decimal
    total_cash_needed: CostEstimate
    cash_from_own_savings: CostEstimate
    gift_funds: Decimal
    earnest_money_already_paid: Decimal
    reserve: ReserveEstimate


@dataclass(frozen=True)
class MonthlyPayment:
    principal_and_interest: Decimal
    mip: Decimal
    property_tax: Decimal
    homeowners_insurance: Decimal
    hoa: Decimal

    @property
    def total(self) -> Decimal:
        return (
            self.principal_and_interest
            + self.mip
            + self.property_tax
            + self.homeowners_insurance
            + self.hoa
        )


@dataclass(frozen=True)
class DtiResult:
    monthly_gross_income: Decimal
    existing_monthly_debts: Decimal
    housing_payment: Decimal
    back_end_dti: Decimal
    target_max: Decimal
    within_target: bool


@dataclass(frozen=True)
class CalculationResult:
    """Top-level bundle handed from cli.py to report.py."""

    property_inputs: PropertyInputs
    property_notes: PropertyResolutionNotes
    buyer: BuyerProfile
    credits_input: Credits
    loan: LoanAmounts
    mip: MipResult
    closing: ClosingCosts
    prepaids: Prepaids
    credits: CreditsApplied
    cash_to_close: CashToClose
    monthly: MonthlyPayment
    dti: DtiResult | None
    mip_duration_years: int | None = field(default=None)
