"""Flash-loan API exposing Bitmatrix-style flash loans with Bitfinex backing."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Dict

from flask import Flask, jsonify, request

from .amm_contract import SIMULATED_POOL
from .bfx_client import bfx_client
from .config import CONFIG
from .liquid_utils import build_flashloan_pset, decode_simulation_pset, sign_and_send_pset

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _decimal_from_request(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception as exc:  # pragma: no cover - defensive programming
        raise ValueError("Invalid numeric value") from exc


@app.route("/flashloan/request", methods=["POST"])
def flashloan_request() -> Any:
    """Allocate Bitfinex treasury capital and return a simulated PSET."""

    data = request.get_json(force=True)
    asset = data.get("asset", CONFIG.pool_asset_b)
    amount_raw = data.get("amount")
    if asset not in (CONFIG.pool_asset_a, CONFIG.pool_asset_b):
        return jsonify({"error": "Unsupported asset"}), 400
    try:
        amount = _decimal_from_request(amount_raw)
    except ValueError:
        return jsonify({"error": "Invalid amount"}), 400
    if amount <= 0:
        return jsonify({"error": "Amount must be positive"}), 400

    try:
        loan_terms = SIMULATED_POOL.prepare_flashloan(asset, amount)
    except ValueError as exc:
        logger.error("Flash loan preparation failed: %s", exc)
        return jsonify({"error": str(exc)}), 400

    try:
        bfx_client.reserve_flashloan_capital(
            loan_terms.loan_id, loan_terms.borrow_asset, loan_terms.borrow_amount
        )
    except ValueError as exc:
        SIMULATED_POOL.cancel_flashloan(loan_terms.loan_id)
        logger.error("Bitfinex treasury cannot fund flash loan: %s", exc)
        return jsonify({"error": str(exc)}), 400

    tolerance = Decimal(CONFIG.price_tolerance_bps) / Decimal(10_000)
    plan = SIMULATED_POOL.plan_flashloan_arbitrage(
        loan_terms,
        CONFIG.btc_usd_price,
        tolerance,
    )
    swaps = plan.get("swaps", [])
    expected_profit = plan.get("expected_profit")
    notes = plan.get("notes", {})
    psbt = build_flashloan_pset(
        loan_terms, swaps=swaps, expected_profit=expected_profit, notes=notes
    )

    response: Dict[str, Any] = {
        "pset": psbt,
        "loan_terms": loan_terms.to_payload(),
        "notes": notes,
        "expected_profit": str(expected_profit) if expected_profit is not None else None,
        "swap_plan": [swap.to_payload() for swap in swaps],
    }
    logger.info(
        "Issued flash loan template %s for %s %s",
        loan_terms.loan_id,
        loan_terms.borrow_amount,
        loan_terms.borrow_asset,
    )
    return jsonify(response)


@app.route("/flashloan/submit", methods=["POST"])
def flashloan_submit() -> Any:
    """Validate and settle a flash loan transaction."""

    data = request.get_json(force=True)
    pset = data.get("pset")
    if not pset:
        return jsonify({"error": "Missing PSET"}), 400

    decoded = decode_simulation_pset(pset)
    loan_meta = None
    if decoded and decoded.get("type") == "flashloan":
        loan_meta = decoded.get("flashloan")

    try:
        result = sign_and_send_pset(pset, decoded_pset=decoded)
    except Exception as exc:  # pragma: no cover - defensive path
        logger.error("Error broadcasting PSET: %s", exc)
        if loan_meta:
            loan_id = loan_meta.get("loan_id")
            SIMULATED_POOL.cancel_flashloan(loan_id)
            bfx_client.cancel_flashloan_reservation(loan_id)
        return jsonify({"error": str(exc)}), 500

    response: Dict[str, Any] = {"txid": result.txid}
    if result.details:
        response["details"] = result.details

    if loan_meta and result.details:
        repay_amount = result.details.get("repay_amount", loan_meta.get("repay_amount"))
        if repay_amount is not None:
            bfx_client.settle_flashloan(
                loan_meta.get("loan_id"),
                loan_meta.get("repay_asset"),
                Decimal(str(repay_amount)),
            )
            response["loan_id"] = loan_meta.get("loan_id")
    return jsonify(response)


if __name__ == "__main__":  # pragma: no cover - manual execution helper
    app.run(host="0.0.0.0", port=8000, debug=True)
