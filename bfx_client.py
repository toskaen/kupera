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
from decimal import Decimal
from typing import Any, Dict

import requests

from .config import CONFIG

logger = logging.getLogger(__name__)


class BitfinexClient:
    BASE_URL = "https://api.bitfinex.com"

    def __init__(self, api_key: str, api_secret: str) -> None:
        self.api_key = api_key
        self.api_secret = api_secret.encode("utf-8")
        self.treasury: Dict[str, Decimal] = {
            CONFIG.pool_asset_a: CONFIG.bitfinex_prep_capital_lbtc,
            CONFIG.pool_asset_b: CONFIG.bitfinex_prep_capital_usdt,
        }
        self.outstanding_flashloans: Dict[str, Dict[str, Decimal]] = {}

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
        # Placeholder stub returning the simulated treasury values
        return {asset: float(balance) for asset, balance in self.treasury.items()}

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

    # ------------------------------------------------------------------
    # Simulation helpers for flash loans / treasury movements
    # ------------------------------------------------------------------

    def available_flashloan(self, asset: str) -> Decimal:
        return self.treasury.get(asset, Decimal(0))

    def reserve_flashloan_capital(self, loan_id: str, asset: str, amount: Decimal) -> None:
        amount = Decimal(amount)
        if amount <= 0:
            raise ValueError("Flash loan reservation must be positive")
        available = self.available_flashloan(asset)
        if amount > available:
            raise ValueError(
                f"Insufficient Bitfinex treasury for {asset}: requested {amount}, have {available}"
            )
        self.treasury[asset] = available - amount
        self.outstanding_flashloans[loan_id] = {"asset": asset, "amount": amount}

    def settle_flashloan(self, loan_id: str, asset: str, repay_amount: Decimal) -> None:
        repay_amount = Decimal(repay_amount)
        outstanding = self.outstanding_flashloans.pop(loan_id, None)
        if outstanding is None:
            logger.warning("Attempted to settle unknown flash loan %s", loan_id)
            self.treasury[asset] = self.treasury.get(asset, Decimal(0)) + repay_amount
            return
        expected_asset = outstanding["asset"]
        if expected_asset != asset:
            logger.warning("Flash loan asset mismatch for %s: %s vs %s", loan_id, expected_asset, asset)
        self.treasury[asset] = self.treasury.get(asset, Decimal(0)) + repay_amount
        logger.info(
            "Settled Bitfinex flash loan %s on %s. Treasury now %s",
            loan_id,
            asset,
            self.treasury[asset],
        )

    def provide_liquidity(self, asset: str, amount: Decimal) -> None:
        amount = Decimal(amount)
        if amount <= 0:
            return
        available = self.available_flashloan(asset)
        if amount > available:
            raise ValueError("Insufficient treasury to provide liquidity")
        self.treasury[asset] = available - amount
        logger.info("Allocated %s %s from Bitfinex treasury to the pool", amount, asset)

    def reclaim_liquidity(self, asset: str, amount: Decimal) -> None:
        amount = Decimal(amount)
        if amount <= 0:
            return
        self.treasury[asset] = self.treasury.get(asset, Decimal(0)) + amount
        logger.info("Returned %s %s to Bitfinex treasury", amount, asset)

    def cancel_flashloan_reservation(self, loan_id: str) -> None:
        reservation = self.outstanding_flashloans.pop(loan_id, None)
        if not reservation:
            return
        asset = reservation["asset"]
        amount = reservation["amount"]
        self.treasury[asset] = self.treasury.get(asset, Decimal(0)) + amount
        logger.info(
            "Cancelled flash loan %s reservation, returning %s %s to treasury",
            loan_id,
            amount,
            asset,
        )


bfx_client = BitfinexClient(CONFIG.bitfinex_api_key, CONFIG.bitfinex_api_secret)