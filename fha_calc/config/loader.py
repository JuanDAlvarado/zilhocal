"""Parses config/defaults.toml into a LoanConfig. This is the only place in
the package that reads the config file — calc/ receives a fully-built
LoanConfig and never touches disk itself."""

from __future__ import annotations

import tomllib
from datetime import date
from decimal import Decimal
from importlib import resources
from pathlib import Path

from fha_calc.models import ClosingCostItem, DpaProgram, LoanConfig, MipRule

_DEFAULT_CONFIG_RESOURCE = ("fha_calc.config", "defaults.toml")


def _dec(value: int | float | str) -> Decimal:
    """TOML floats come in as Python floats; route through str() so we get
    the digits the file actually spelled out rather than binary-float noise."""
    return Decimal(str(value))


def _load_toml(path: Path | None) -> dict:
    if path is not None:
        with open(path, "rb") as f:
            return tomllib.load(f)
    ref = resources.files(_DEFAULT_CONFIG_RESOURCE[0]).joinpath(_DEFAULT_CONFIG_RESOURCE[1])
    with resources.as_file(ref) as p, open(p, "rb") as f:
        return tomllib.load(f)


def load_config(path: str | Path | None = None) -> LoanConfig:
    """Load and parse a defaults.toml-shaped file into a LoanConfig.

    If `path` is None, loads the package's bundled fha_calc/config/defaults.toml.
    """
    data = _load_toml(Path(path) if path is not None else None)

    meta = data.get("meta", {})
    fha = data["fha"]
    limits = data["loan_limits"]
    county_limits = {
        name: _dec(v) for name, v in limits.get("counties", {}).items()
    }

    mip_rules = tuple(
        MipRule(
            term_years_max=int(r["term_years_max"]),
            ltv_max=_dec(r["ltv_max"]),
            loan_amount_max=_dec(r["loan_amount_max"]) if "loan_amount_max" in r else None,
            rate=_dec(r["rate"]),
        )
        for r in data.get("mip", {}).get("rules", [])
    )

    closing = data["closing_costs"]
    closing_items = tuple(
        ClosingCostItem(
            key=key,
            label=item["label"],
            basis=item["basis"],
            low=_dec(item["low"]),
            high=_dec(item["high"]),
            enabled=bool(item.get("enabled", True)),
            required=bool(item.get("required", False)),
            only_if_hoa=bool(item.get("only_if_hoa", False)),
            note=item.get("note", ""),
        )
        for key, item in closing.get("items", {}).items()
    )

    prepaids = data["prepaids"]

    dpa_programs = tuple(
        DpaProgram(
            key=p["key"],
            label=p["label"],
            kind=p["kind"],
            value=_dec(p["value"]),
            min_credit_score=int(p["min_credit_score"]),
            notes=p.get("notes", ""),
        )
        for p in data.get("dpa", {}).get("programs", [])
    )

    reserve = data["reserve"]
    dti = data["dti"]

    return LoanConfig(
        last_verified=meta.get("last_verified", ""),
        verification_notes=meta.get("notes", ""),
        ufmip_rate=_dec(fha["ufmip_rate"]),
        annual_mip_default=_dec(fha["annual_mip_default"]),
        min_down_580_plus=_dec(fha["min_down_580_plus"]),
        min_down_500_579=_dec(fha["min_down_500_579"]),
        fha_min_credit_score=int(fha["fha_min_credit_score"]),
        lender_overlay_score_threshold=int(fha["lender_overlay_score_threshold"]),
        max_seller_concession_pct=_dec(fha["max_seller_concession_pct"]),
        mip_11_year_down_pct_threshold=_dec(fha["mip_11_year_down_pct_threshold"]),
        national_floor=_dec(limits["national_floor"]),
        national_ceiling=_dec(limits["national_ceiling"]),
        county_limits=county_limits,
        mip_rules=mip_rules,
        closing_cost_mode=closing.get("mode", "percentage"),
        closing_pct_low=_dec(closing["pct_low"]),
        closing_pct_mid=_dec(closing["pct_mid"]),
        closing_pct_high=_dec(closing["pct_high"]),
        closing_items=closing_items,
        tax_escrow_months=int(prepaids["tax_escrow_months"]),
        ins_escrow_months=int(prepaids["ins_escrow_months"]),
        default_property_tax_rate=_dec(prepaids["default_property_tax_rate"]),
        default_hoi_rate=_dec(prepaids["default_hoi_rate"]),
        dpa_programs=dpa_programs,
        reserve_piti_months_default=int(reserve["piti_months_default"]),
        reserve_moving_costs_default=_dec(reserve["moving_costs_default"]),
        reserve_immediate_repairs_default=_dec(reserve["immediate_repairs_default"]),
        dti_target_back_end=_dec(dti["target_back_end"]),
    )


def is_stale(config: LoanConfig, as_of: date | None = None) -> bool:
    """True if config.last_verified ('YYYY-MM') is more than 12 months old."""
    if not config.last_verified:
        return False
    year, month = (int(part) for part in config.last_verified.split("-")[:2])
    verified = date(year, month, 1)
    today = as_of or date.today()
    months_elapsed = (today.year - verified.year) * 12 + (today.month - verified.month)
    return months_elapsed > 12
