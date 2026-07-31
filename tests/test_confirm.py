"""Tests for confirm.py (§7): the confirmation loop and image-hash cache."""

from decimal import Decimal as D

import pytest

from fha_calc.confirm import (
    ConfirmedFields,
    confirm_fields,
    hash_image,
    load_cached_confirmation,
    save_confirmation_to_cache,
)
from fha_calc.extract import CandidateFields, Confidence, FieldCandidate


def _fields(**overrides) -> CandidateFields:
    defaults = dict(
        purchase_price_candidates=(
            FieldCandidate(D("285000"), Confidence.HIGH, "'$285,000' top-left"),
            FieldCandidate(D("291400"), Confidence.HIGH, "'$291,400' Zestimate"),
        ),
        annual_property_tax=FieldCandidate(D("2340"), Confidence.MEDIUM, "'Tax: $195/mo' -> x12"),
        annual_hoa=FieldCandidate(D("0"), Confidence.MISSING, "not found — assumed none"),
        annual_homeowners_insurance=FieldCandidate(None, Confidence.MISSING, "not found"),
        county=FieldCandidate("Moore, SC", Confidence.LOW, "parsed from address"),
    )
    defaults.update(overrides)
    return CandidateFields(**defaults)


def _feed_input(monkeypatch, answers: list[str]):
    it = iter(answers)
    monkeypatch.setattr("builtins.input", lambda *_args: next(it))


# --- confirm_fields loop --------------------------------------------------


def test_confirm_fields_accept_all(monkeypatch):
    _feed_input(monkeypatch, [""])
    result = confirm_fields(_fields())

    assert result == ConfirmedFields(
        purchase_price=D("285000"),
        annual_property_tax=D("2340"),
        annual_hoa=D("0"),
        annual_homeowners_insurance=None,
        county="Moore, SC",
    )


def test_confirm_fields_edit_price_pick_alternate(monkeypatch):
    _feed_input(monkeypatch, ["1", "2", ""])
    result = confirm_fields(_fields())
    assert result.purchase_price == D("291400")


def test_confirm_fields_edit_price_custom_value(monkeypatch):
    _feed_input(monkeypatch, ["1", "3", "310000", ""])
    result = confirm_fields(_fields())
    assert result.purchase_price == D("310000")


def test_confirm_fields_edit_money_field(monkeypatch):
    _feed_input(monkeypatch, ["2", "3000", ""])
    result = confirm_fields(_fields())
    assert result.annual_property_tax == D("3000")


def test_confirm_fields_edit_county(monkeypatch):
    _feed_input(monkeypatch, ["5", "Spartanburg, SC", ""])
    result = confirm_fields(_fields())
    assert result.county == "Spartanburg, SC"


def test_confirm_fields_quit_raises(monkeypatch):
    _feed_input(monkeypatch, ["q"])
    with pytest.raises(KeyboardInterrupt):
        confirm_fields(_fields())


def test_confirm_fields_rejects_blank_price(monkeypatch):
    # Edit price -> "enter a custom value" option -> leave it blank -> accept.
    _feed_input(monkeypatch, ["1", "3", "", ""])
    with pytest.raises(ValueError, match="Purchase price is required"):
        confirm_fields(_fields())


def test_confirm_fields_unrecognized_input_reprompts(monkeypatch):
    _feed_input(monkeypatch, ["bogus", ""])
    result = confirm_fields(_fields())
    assert result.purchase_price == D("285000")


# --- image hash cache ------------------------------------------------------


def test_hash_image_is_deterministic_and_content_sensitive(tmp_path):
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    a.write_bytes(b"same-bytes")
    b.write_bytes(b"same-bytes")
    assert hash_image(a) == hash_image(b)

    c = tmp_path / "c.png"
    c.write_bytes(b"different-bytes")
    assert hash_image(a) != hash_image(c)


def test_cache_round_trip(tmp_path):
    cache_dir = tmp_path / "cache"
    confirmed = ConfirmedFields(
        purchase_price=D("285000"),
        annual_property_tax=D("2340"),
        annual_hoa=D("0"),
        annual_homeowners_insurance=None,
        county="Moore, SC",
    )

    assert load_cached_confirmation("deadbeef", cache_dir=cache_dir) is None

    save_confirmation_to_cache("deadbeef", confirmed, cache_dir=cache_dir)
    loaded = load_cached_confirmation("deadbeef", cache_dir=cache_dir)
    assert loaded == confirmed
