"""Monthly payment (P&I, MIP, tax, insurance, HOA) and optional DTI (§8.8).

Pure: takes dataclasses/Decimals in, returns a dataclass out. No I/O, no
config reads. Informational only — this module estimates a payment, it does
not predict loan approval.
"""

from __future__ import annotations

from decimal import Decimal

from fha_calc.models import BuyerProfile, DtiResult, LoanConfig, MipResult, MonthlyPayment


def calculate_principal_and_interest(
    total_loan_amount: Decimal, annual_rate: Decimal, term_years: int
) -> Decimal:
    monthly_rate = annual_rate / 12
    n_payments = term_years * 12
    if monthly_rate == 0:
        return total_loan_amount / n_payments
    factor = (1 + monthly_rate) ** n_payments
    return total_loan_amount * (monthly_rate * factor) / (factor - 1)


def calculate_monthly_payment(
    mip: MipResult,
    buyer: BuyerProfile,
    annual_property_tax: Decimal,
    annual_homeowners_insurance: Decimal,
    annual_hoa: Decimal,
) -> MonthlyPayment:
    principal_and_interest = calculate_principal_and_interest(
        mip.total_loan_amount, buyer.interest_rate, buyer.loan_term_years
    )
    return MonthlyPayment(
        principal_and_interest=principal_and_interest,
        mip=mip.monthly_mip,
        property_tax=annual_property_tax / 12,
        homeowners_insurance=annual_homeowners_insurance / 12,
        hoa=annual_hoa / 12,
    )


def calculate_dti(
    monthly_gross_income: Decimal,
    existing_monthly_debts: Decimal,
    housing_payment: Decimal,
    config: LoanConfig,
) -> DtiResult:
    if monthly_gross_income <= 0:
        raise ValueError("monthly_gross_income must be positive to compute DTI")

    back_end_dti = (existing_monthly_debts + housing_payment) / monthly_gross_income

    return DtiResult(
        monthly_gross_income=monthly_gross_income,
        existing_monthly_debts=existing_monthly_debts,
        housing_payment=housing_payment,
        back_end_dti=back_end_dti,
        target_max=config.dti_target_back_end,
        within_target=back_end_dti <= config.dti_target_back_end,
    )
