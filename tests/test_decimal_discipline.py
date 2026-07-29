"""Decimal discipline test (§12): no `float` may appear in any money-carrying
dataclass field. Walks every dataclass in fha_calc.models and checks that
every field typed to hold a monetary/rate value is annotated Decimal (or
Optional[Decimal]), never float."""

import dataclasses
import typing

from fha_calc import models

FLOAT_FORBIDDEN_TYPES = (float,)


def _iter_dataclasses():
    for name in dir(models):
        obj = getattr(models, name)
        if dataclasses.is_dataclass(obj) and isinstance(obj, type):
            yield obj


def _type_mentions_float(annotation) -> bool:
    origin = typing.get_origin(annotation)
    if origin is not None:
        return any(_type_mentions_float(a) for a in typing.get_args(annotation))
    return annotation is float


def test_no_float_in_dataclass_annotations():
    offenders = []
    for cls in _iter_dataclasses():
        hints = typing.get_type_hints(cls)
        for field in dataclasses.fields(cls):
            annotation = hints.get(field.name, field.type)
            if _type_mentions_float(annotation):
                offenders.append(f"{cls.__name__}.{field.name}")

    assert not offenders, f"float found in money-carrying dataclass fields: {offenders}"


def test_no_float_instances_in_calc_output():
    """Belt-and-suspenders: run a real calculation and check every Decimal
    dataclass field actually holds a Decimal instance at runtime, not just
    that the annotation says so."""
    from decimal import Decimal as D

    from fha_calc.models import BuyerProfile, Credits, PropertyInputs
    from tests.helpers import run_pipeline

    prop = PropertyInputs(
        purchase_price=D("285000"),
        annual_property_tax=D("2340"),
        annual_hoa=D("0"),
        annual_homeowners_insurance=D("1700"),
        county="Spartanburg, SC",
    )
    buyer = BuyerProfile(credit_score=680, down_payment_pct=D("0.035"), interest_rate=D("0.065"))
    results = run_pipeline(prop, buyer, Credits())

    offenders = []

    def _walk(obj, path):
        if isinstance(obj, float):
            offenders.append(path)
        elif dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            for f in dataclasses.fields(obj):
                _walk(getattr(obj, f.name), f"{path}.{f.name}")
        elif isinstance(obj, (list, tuple)):
            for i, v in enumerate(obj):
                _walk(v, f"{path}[{i}]")

    for result in results:
        _walk(result, type(result).__name__)

    assert not offenders, f"float instances found at runtime: {offenders}"
