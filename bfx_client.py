"""
Simplified Bitfinex API client used for rebalancing and flash‑loan funding.

This client wraps a subset of the Bitfinex REST API.  It only
implements the endpoints necessary for this MVP: obtaining balances,
placing trades, and transferring funds to and from the Liquid wallet.

**WARNING**: This module is for demonstration only.  In practice you
must handle API errors, rate limits, WebSocket updates, and secure
management of your API keys.  Consult Bitfinex’s official
documentation for full details.
"""

from __future__ import annotations

import hmac
import hashlib
import json
import logging
import time
from typing import Any, Dict

import requests

from .config import CONFIG

logger = logging.getLogger(__name__)


class BitfinexClient:
    BASE_URL = "https://api.bitfinex.com"

    def __init__(self, api_key: str, api_secret: str) -> None:
        self.api_key = api_key
        self.api_secret = api_secret.encode("utf-8")

    def _nonce(self) -> str:
        return str(int(time.time() * 1000))

    def _headers(self, path: str, body: Dict[str, Any]) -> Dict[str, str]:
        payload_json = json.dumps(body)
        payload = f"/api/{path}{payload_json}{self._nonce()}".encode()
        signature = hmac.new(self.api_secret, payload, hashlib.sha384).hexdigest()
        return {
            "bfx-nonce": self._nonce(),
            "bfx-apikey": self.api_key,
            "bfx-signature": signature,
            "Content-Type": "application/json",
        }

    def get_balances(self) -> Dict[str, float]:
        """Return a simplified mapping of currency codes to balances."""
        # In production use the v2 /auth/r/wallets endpoint
        logger.debug("Fetching Bitfinex balances")
        # Placeholder stub returning static values
        return {"BTC": 1.0, "USDT": 30000.0}

    def place_order(self, symbol: str, amount: float, price: float, side: str) -> Dict[str, Any]:
        """Place a limit order on Bitfinex.

        :param symbol: Trading pair symbol (e.g. "tBTCUSD").
        :param amount: Amount to buy/sell.
        :param price: Limit price.
        :param side: "buy" or "sell".
        :return: Order response.
        """
        logger.debug("Placing order %s %s @ %s for %s", side, amount, price, symbol)
        # For demonstration we pretend the order fills immediately
        return {"id": 123, "symbol": symbol, "amount": amount, "price": price, "side": side}

    def withdraw(self, currency: str, amount: float, address: str) -> Dict[str, Any]:
        """
        Withdraw funds to an external address.

        Bitfinex supports specifying the network transport (e.g. Liquid) when
        withdrawing tokens like USDT or BTC.  In a real implementation you
        would call the `/v4/auth/w/withdrawal` endpoint.  Here we just log the
        action.
        """
        logger.info("Withdrawing %s %s to %s", amount, currency, address)
        return {"status": "success", "txid": "dummy"}

    def deposit_address(self, currency: str, network: str = "Liquid") -> str:
        """Retrieve the deposit address for the given currency and network."""
        logger.debug("Fetching deposit address for %s on %s", currency, network)
        return "VJLdummydepositaddress"


bfx_client = BitfinexClient(CONFIG.bitfinex_api_key, CONFIG.bitfinex_api_secret)