"""Local web UI: a thin JSON API in front of the same pure calc engine and
OCR/extract pipeline the CLI uses. No outbound network calls — this only
ever binds to localhost and serves the bundled static frontend; the browser
never leaves the machine either. All OCR-derived values are still treated
as proposals: /api/ocr only returns ranked candidates, nothing is
calculated until the user confirms/edits them in the browser and submits
/api/calculate.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import tempfile
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from fha_calc.config.loader import is_stale, load_config
from fha_calc.models import FhaCalcError
from fha_calc.report import json_safe

STATIC_DIR = Path(__file__).parent / "static"

app = Flask(__name__, static_folder=None)


def _config():
    # Loaded per-request rather than once at import time so `fha-calc-web
    # --config path/to/custom.toml` (see main()) and editing defaults.toml
    # without restarting are both picked up.
    return load_config(app.config.get("FHA_CONFIG_PATH"))


@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/<path:filename>")
def static_files(filename):
    return send_from_directory(STATIC_DIR, filename)


@app.get("/api/config")
def api_config():
    config = _config()
    return jsonify(
        {
            "counties": sorted(config.county_limits.keys()),
            "national_floor": str(config.national_floor),
            "national_ceiling": str(config.national_ceiling),
            "dpa_programs": [
                {
                    "key": p.key,
                    "label": p.label,
                    "min_credit_score": p.min_credit_score,
                    "notes": p.notes,
                }
                for p in config.dpa_programs
            ],
            "closing_cost_mode_default": config.closing_cost_mode,
            "last_verified": config.last_verified,
            "stale": is_stale(config),
            "verification_notes": config.verification_notes,
        }
    )


@app.post("/api/ocr")
def api_ocr():
    from fha_calc.extract import extract_fields
    from fha_calc.ocr import run_ocr

    body = request.get_json(silent=True) or {}
    data_url = body.get("image_base64")
    if not data_url:
        return jsonify({"error": "image_base64 is required"}), 400

    # Accept both a raw base64 string and a data: URL (what the browser's
    # clipboard/file APIs hand back most naturally).
    if "," in data_url and data_url.strip().startswith("data:"):
        data_url = data_url.split(",", 1)[1]

    try:
        image_bytes = base64.b64decode(data_url, validate=True)
    except (binascii.Error, ValueError):
        return jsonify({"error": "image_base64 could not be decoded"}), 400

    denoise = bool(body.get("denoise", False))

    with tempfile.NamedTemporaryFile(suffix=".png") as tmp:
        tmp.write(image_bytes)
        tmp.flush()
        try:
            ocr_result = run_ocr(tmp.name, denoise=denoise)
        except Exception as e:  # noqa: BLE001 - surfaced to the browser, not a crash
            return jsonify({"error": f"OCR failed: {e}"}), 400

    fields = extract_fields(ocr_result)
    return jsonify(json_safe(fields))


@app.post("/api/calculate")
def api_calculate():
    from fha_calc import orchestrate

    body = request.get_json(silent=True) or {}
    config = _config()
    notes: list[str] = []
    if is_stale(config):
        notes.append(
            f"Config was last verified {config.last_verified}, more than 12 months "
            f"ago — rates and limits may be stale. {config.verification_notes}"
        )

    try:
        price = orchestrate.parse_decimal(body.get("purchase_price", ""), "purchase_price")
        tax_raw = _optional_decimal(body.get("annual_property_tax"))
        hoa_raw = _optional_decimal(body.get("annual_hoa"))
        ins_raw = _optional_decimal(body.get("annual_homeowners_insurance"))

        property_inputs, property_notes = orchestrate.finalize_property(
            price,
            tax_raw,
            hoa_raw,
            ins_raw,
            body.get("county") or None,
            body.get("address") or None,
            config,
            notes,
        )

        buyer = orchestrate.build_buyer_profile(
            credit_score=_optional_int(body.get("credit_score")),
            rate=body.get("rate"),
            down_pct=body.get("down_pct") or None,
            finance_ufmip=body.get("finance_ufmip"),
            closing_day=int(body.get("closing_day") or 15),
            term=int(body.get("term") or 30),
            config=config,
        )
        credits_in = orchestrate.build_credits(
            seller_concessions=body.get("seller_concessions") or "0",
            lender_credits=body.get("lender_credits") or "0",
            gift=body.get("gift") or "0",
            earnest=body.get("earnest") or "0",
            dpa=body.get("dpa") or None,
            config=config,
        )

        item_overrides = {k: v for k, v in (body.get("item_overrides") or {}).items() if v}
        result = orchestrate.run_calculation(
            property_inputs,
            property_notes,
            buyer,
            credits_in,
            config,
            notes,
            closing_mode=body.get("closing_mode") or None,
            item_overrides=item_overrides,
            monthly_income=body.get("monthly_income") or None,
            monthly_debts=body.get("monthly_debts") or None,
        )
    except (FhaCalcError, ValueError) as e:
        return jsonify({"error": str(e)}), 400

    payload = json_safe(result)
    payload["notes"] = notes
    return jsonify(payload)


def _optional_decimal(raw):
    if raw is None or str(raw).strip() == "":
        return None
    from fha_calc import orchestrate

    return orchestrate.parse_decimal(raw, "value")


def _optional_int(raw):
    if raw is None or str(raw).strip() == "":
        return None
    return int(raw)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fha-calc-web", description="Run the local zilhocal web UI.")
    parser.add_argument("--host", default="127.0.0.1", help="default: 127.0.0.1 (localhost only)")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--config", dest="config_path", help="path to a custom defaults.toml")
    args = parser.parse_args(argv)

    app.config["FHA_CONFIG_PATH"] = args.config_path
    print(f"zilhocal web UI running at http://{args.host}:{args.port}  (Ctrl+C to stop)")
    app.run(host=args.host, port=args.port, debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
