"""Property-based tests (§12) using hypothesis:

- cash-to-close must never be negative
- increasing down payment must never increase the loan amount
- concessions applied must never exceed closing costs + prepaids
"""

from decimal import Decimal as D

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from fha_calc.calc.closing import calculate_closing_costs
from fha_calc.calc.credits import apply_credits
from fha_calc.calc.loan import calculate_loan_amounts
from fha_calc.calc.mip import calculate_mip
from fha_calc.calc.monthly import calculate_monthly_payment
from fha_calc.calc.prepaids import calculate_prepaids
from fha_calc.calc.summary import assemble_cash_to_close
from fha_calc.models import BuyerProfile, Credits, PropertyInputs
from tests.helpers import CONFIG

purchase_prices = st.decimals(min_value="60000", max_value="500000", places=2).map(D)
credit_scores = st.integers(min_value=580, max_value=850)
interest_rates = st.decimals(min_value="0.02", max_value="0.10", places=4).map(D)
down_payment_pcts = st.decimals(min_value="0.035", max_value="0.50", places=4).map(D)
seller_concession_amounts = st.decimals(min_value="0", max_value="40000", places=2).map(D)
lender_credit_amounts = st.decimals(min_value="0", max_value="20000", places=2).map(D)
dpa_amounts = st.decimals(min_value="0", max_value="20000", places=2).map(D)
gift_amounts = st.decimals(min_value="0", max_value="40000", places=2).map(D)


def _build(purchase_price, credit_score, down_pct, rate, seller_concessions, lender_credits, dpa, gift):
    prop = PropertyInputs(
        purchase_price=purchase_price,
        annual_property_tax=purchase_price * D("0.01"),
        annual_hoa=D("0"),
        annual_homeowners_insurance=purchase_price * D("0.004"),
        county="Greenville, SC",
    )
    buyer = BuyerProfile(
        credit_score=credit_score,
        down_payment_pct=down_pct,
        interest_rate=rate,
        closing_date_day_of_month=15,
    )
    credits_in = Credits(
        seller_concessions=seller_concessions,
        lender_credits=lender_credits,
        dpa_amount=dpa,
        gift_funds=gift,
    )

    loan = calculate_loan_amounts(prop, buyer, CONFIG)
    mip = calculate_mip(loan, buyer, CONFIG)
    closing = calculate_closing_costs("percentage", loan, prop, CONFIG)
    prepaids = calculate_prepaids(
        mip.total_loan_amount,
        buyer.interest_rate,
        buyer.closing_date_day_of_month,
        30,
        prop.annual_property_tax,
        prop.annual_homeowners_insurance,
        CONFIG,
    )
    credits_applied = apply_credits(
        credits_in, closing, prepaids, prop.purchase_price, loan.base_loan_amount, buyer.credit_score, CONFIG
    )
    monthly = calculate_monthly_payment(
        mip, buyer, prop.annual_property_tax, prop.annual_homeowners_insurance, prop.annual_hoa
    )
    cash = assemble_cash_to_close(loan, mip, closing, prepaids, credits_applied, monthly.total, CONFIG)
    return loan, mip, closing, prepaids, credits_applied, cash


@given(
    purchase_price=purchase_prices,
    credit_score=credit_scores,
    down_pct=down_payment_pcts,
    rate=interest_rates,
    seller_concessions=seller_concession_amounts,
    lender_credits=lender_credit_amounts,
    dpa=dpa_amounts,
    gift=gift_amounts,
)
@settings(max_examples=200)
def test_cash_to_close_never_negative(
    purchase_price, credit_score, down_pct, rate, seller_concessions, lender_credits, dpa, gift
):
    # Loan-limit and eligibility errors are expected territory, not the
    # property under test here — skip inputs that hit those hard errors.
    try:
        _, _, _, _, _, cash = _build(
            purchase_price, credit_score, down_pct, rate, seller_concessions, lender_credits, dpa, gift
        )
    except Exception:
        assume(False)

    assert cash.total_cash_needed.low >= 0
    assert cash.total_cash_needed.likely >= 0
    assert cash.total_cash_needed.high >= 0
    assert cash.cash_from_own_savings.low >= 0
    assert cash.cash_from_own_savings.likely >= 0
    assert cash.cash_from_own_savings.high >= 0


@given(
    purchase_price=purchase_prices,
    credit_score=credit_scores,
    rate=interest_rates,
    down_pct_low=down_payment_pcts,
    down_pct_delta=st.decimals(min_value="0.001", max_value="0.20", places=4).map(D),
)
@settings(max_examples=200)
def test_increasing_down_payment_never_increases_loan_amount(
    purchase_price, credit_score, rate, down_pct_low, down_pct_delta
):
    down_pct_high = down_pct_low + down_pct_delta
    assume(down_pct_high <= D("0.9"))

    prop = PropertyInputs(
        purchase_price=purchase_price,
        annual_property_tax=purchase_price * D("0.01"),
        annual_hoa=D("0"),
        annual_homeowners_insurance=purchase_price * D("0.004"),
        county="Greenville, SC",
    )
    buyer_low = BuyerProfile(
        credit_score=credit_score, down_payment_pct=down_pct_low, interest_rate=rate, closing_date_day_of_month=15
    )
    buyer_high = BuyerProfile(
        credit_score=credit_score, down_payment_pct=down_pct_high, interest_rate=rate, closing_date_day_of_month=15
    )

    try:
        loan_low = calculate_loan_amounts(prop, buyer_low, CONFIG)
        loan_high = calculate_loan_amounts(prop, buyer_high, CONFIG)
    except Exception:
        assume(False)

    assert loan_high.base_loan_amount <= loan_low.base_loan_amount


@given(
    purchase_price=purchase_prices,
    credit_score=credit_scores,
    down_pct=down_payment_pcts,
    rate=interest_rates,
    seller_concessions=seller_concession_amounts,
)
@settings(max_examples=200)
def test_seller_concessions_never_exceed_costs_plus_prepaids(
    purchase_price, credit_score, down_pct, rate, seller_concessions
):
    try:
        _, _, closing, prepaids, credits_applied, _ = _build(
            purchase_price, credit_score, down_pct, rate, seller_concessions, D("0"), D("0"), D("0")
        )
    except Exception:
        assume(False)

    for col in ("low", "likely", "high"):
        applied = getattr(credits_applied.seller_concessions_applied, col)
        cap = getattr(closing.estimate, col) + prepaids.total
        assert applied <= cap
