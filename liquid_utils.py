"""Utilities for the Liquid simulation and conceptual PSET handling."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, Iterable, Optional

import base64
import json
import logging
import requests

from .amm_contract import FlashLoanTerms, SIMULATED_POOL, SwapQuote
from .config import CONFIG

logger = logging.getLogger(__name__)


@dataclass
class SignResult:
    """Result from signing/broadcasting a PSET."""

    txid: str
    details: Optional[Dict[str, Any]] = None


class LiquidRPC:
    """Simple JSON-RPC client for Liquid."""

    def __init__(self, url: str, user: str, password: str) -> None:
        self.url = url
        self.auth = (user, password)

    def _call(self, method: str, *params: Any) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "id": 0,
            "method": method,
            "params": list(params),
        }
        response = requests.post(self.url, json=payload, auth=self.auth, timeout=10)
        response.raise_for_status()
        result = response.json()
        if result.get("error"):
            raise RuntimeError(f"RPC error: {result['error']}")
        return result["result"]

    def wallet_process_psbt(self, psbt: str) -> Dict[str, Any]:
        return self._call("walletprocesspsbt", psbt)

    def finalize_psbt(self, psbt: str) -> Dict[str, Any]:
        return self._call("finalizepsbt", psbt)

    def send_raw_transaction(self, tx_hex: str) -> str:
        return self._call("sendrawtransaction", tx_hex)


rpc_client = LiquidRPC(
    CONFIG.liquid_rpc_url, CONFIG.liquid_rpc_user, CONFIG.liquid_rpc_password
)


def _encode_payload(payload: Dict[str, Any]) -> str:
    return base64.b64encode(json.dumps(payload).encode("utf-8")).decode()


def _maybe_decimal(value: Decimal | float | int | str) -> str:
    return str(value)


def build_swap_pset(
    input_asset: str,
    amount_in: Decimal,
    min_output: Optional[Decimal] = None,
) -> str:
    """Construct a simulated swap PSET that will be executed by the AMM model."""

    quote = SIMULATED_POOL.quote_swap(input_asset, Decimal(amount_in))
    payload = {
        "simulation": True,
        "type": "swap",
        "swap": {
            **quote.to_payload(),
            "min_output": _maybe_decimal(min_output if min_output is not None else 0),
        },
    }
    return _encode_payload(payload)


def build_flashloan_pset(
    terms: FlashLoanTerms,
    swaps: Optional[Iterable[SwapQuote]] = None,
    expected_profit: Optional[Decimal] = None,
    notes: Optional[Dict[str, str]] = None,
) -> str:
    """Create a base64 payload representing a flash loan plus swap instructions."""

    payload: Dict[str, Any] = {
        "simulation": True,
        "type": "flashloan",
        "flashloan": terms.to_payload(),
        "swaps": [swap.to_payload() for swap in swaps] if swaps else [],
        "settlement": {"repay_amount": terms.to_payload()["repay_amount"]},
    }
    if expected_profit is not None:
        payload["expected_profit"] = _maybe_decimal(expected_profit)
    if notes:
        payload["notes"] = notes
    return _encode_payload(payload)


def decode_simulation_pset(pset_b64: str) -> Optional[Dict[str, Any]]:
    """Return the JSON payload for simulated PSETs, otherwise ``None``."""

    try:
        decoded = base64.b64decode(pset_b64.encode("utf-8"))
        payload = json.loads(decoded)
        if isinstance(payload, dict) and payload.get("simulation"):
            return payload
    except (ValueError, json.JSONDecodeError):
        return None
    return None


def sign_and_send_pset(
    pset_b64: str, decoded_pset: Optional[Dict[str, Any]] = None
) -> SignResult:
    """Process, finalise and broadcast a PSET or simulated payload."""

    payload = decoded_pset or decode_simulation_pset(pset_b64)
    if isinstance(payload, dict) and payload.get("simulation"):
        metadata = SIMULATED_POOL.apply_simulated_pset(payload)
        txid = str(metadata.get("txid"))
        details = {k: v for k, v in metadata.items() if k != "txid"}
        logger.info("Simulated PSET executed: %s", txid)
        return SignResult(txid=txid, details=details)

    # Fallback to RPC broadcast for non-simulated payloads
    processed = rpc_client.wallet_process_psbt(pset_b64)
    processed_pset = processed.get("psbt")
    final = rpc_client.finalize_psbt(processed_pset)
    tx_hex = final.get("hex")
    txid = rpc_client.send_raw_transaction(tx_hex)
    logger.info("Broadcast transaction %s", txid)
    return SignResult(txid=txid)


def fetch_pool_state() -> Dict[str, Decimal]:
    """Expose the current simulated pool reserves and price."""

    reserves = SIMULATED_POOL.snapshot()
    return {
        CONFIG.pool_asset_a: reserves[CONFIG.pool_asset_a],
        CONFIG.pool_asset_b: reserves[CONFIG.pool_asset_b],
        "price": SIMULATED_POOL.price(),
    }

