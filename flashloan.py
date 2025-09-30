"""
Flash‑loan API for external arbitrageurs.

This Flask application exposes two endpoints:

* `POST /flashloan/request` – Given a desired asset and amount, returns
  a base64‑encoded PSET representing a partially signed transaction
  lending the requested tokens from the pool.  The caller must add
  their swap transaction and a repayment output and return the
  completed PSET.
* `POST /flashloan/submit` – Accepts a signed PSET from an arbitrageur
  and broadcasts it to the Liquid network via `liquid_utils.sign_and_send_pset`.

In this MVP, the logic for enforcing repayment and fee collection
resides in the off‑chain PSET construction; the covenant script (see
`amm_contract.py`) will ultimately verify that the borrowed funds are
returned with a fee in the same transaction.
"""

from __future__ import annotations

import logging
from flask import Flask, request, jsonify

from .config import CONFIG
from .liquid_utils import build_swap_pset, sign_and_send_pset

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@app.route("/flashloan/request", methods=["POST"])
def flashloan_request() -> Any:
    data = request.get_json(force=True)
    asset = data.get("asset")
    amount = data.get("amount")
    if asset not in (CONFIG.pool_asset_a, CONFIG.pool_asset_b):
        return jsonify({"error": "Unsupported asset"}), 400
    if amount is None or amount <= 0:
        return jsonify({"error": "Invalid amount"}), 400
    # Build a dummy PSET for demonstration.  The real implementation
    # would include the pool’s UTXO and enforce repayment.
    psbt = build_swap_pset([], 0, 0, 0)
    logger.info("Providing flashloan PSET for %s %s", amount, asset)
    return jsonify({"pset": psbt})


@app.route("/flashloan/submit", methods=["POST"])
def flashloan_submit() -> Any:
    data = request.get_json(force=True)
    pset = data.get("pset")
    if not pset:
        return jsonify({"error": "Missing PSET"}), 400
    try:
        txid = sign_and_send_pset(pset)
    except Exception as exc:
        logger.error("Error broadcasting PSET: %s", exc)
        return jsonify({"error": str(exc)}), 500
    return jsonify({"txid": txid})


if __name__ == "__main__":
    # Run the API on localhost for testing
    app.run(host="0.0.0.0", port=8000, debug=True)