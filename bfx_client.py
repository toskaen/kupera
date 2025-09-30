"""
Simplified Bitfinex API client for rebalancing and flash loan funding.

This client wraps Bitfinex REST API for treasury management.
In production, use official Bitfinex SDK and handle errors properly.
"""

import hmac
import hashlib
import json
import logging
import time
from decimal import Decimal
from typing import Any, Dict

import requests

# FIXED: Use absolute import
import config

logger = logging.getLogger(__name__)

# Global config reference
CONFIG = config.CONFIG


class BitfinexClient:
    BASE_URL = "https://api.bitfinex.com"

    def __init__(self, api_key: str, api_secret: str) -> None:
        self.api_key = api_key
        self.api_secret = api_secret.encode("utf-8")
        
        # Simulated treasury (for MVP testing)
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
        """Return simplified mapping of currency codes to balances."""
        logger.debug("Fetching Bitfinex balances")
        return {asset: float(balance) for asset, balance in self.treasury.items()}

    def place_order(self, symbol: str, amount: float, price: float, side: str) -> Dict[str, Any]:
        """Place a limit order on Bitfinex."""
        logger.debug("Placing order %s %s @ %s for %s", side, amount, price, symbol)
        return {"id": 123, "symbol": symbol, "amount": amount, "price": price, "side": side}

    def withdraw(self, currency: str, amount: float, address: str) -> Dict[str, Any]:
        """Withdraw funds to external address."""
        logger.info("Withdrawing %s %s to %s", amount, currency, address)
        return {"status": "success", "txid": "dummy"}

    def deposit_address(self, currency: str, network: str = "Liquid") -> str:
        """Retrieve deposit address for given currency and network."""
        logger.debug("Fetching deposit address for %s on %s", currency, network)
        return "VJLdummydepositaddress"

    # ------------------------------------------------------------------
    # Simulation helpers for flash loans
    # ------------------------------------------------------------------

    def available_flashloan(self, asset: str) -> Decimal:
        """Get available treasury balance for flash loans."""
        return self.treasury.get(asset, Decimal(0))

    def reserve_flashloan_capital(self, loan_id: str, asset: str, amount: Decimal) -> None:
        """Reserve capital from treasury for flash loan."""
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
        logger.info("Reserved %s %s from treasury for loan %s", amount, asset, loan_id)

    def settle_flashloan(self, loan_id: str, asset: str, repay_amount: Decimal) -> None:
        """Settle flash loan by returning capital to treasury."""
        repay_amount = Decimal(repay_amount)
        outstanding = self.outstanding_flashloans.pop(loan_id, None)
        
        if outstanding is None:
            logger.warning("Attempted to settle unknown flash loan %s", loan_id)
            self.treasury[asset] = self.treasury.get(asset, Decimal(0)) + repay_amount
            return
        
        expected_asset = outstanding["asset"]
        if expected_asset != asset:
            logger.warning(
                "Flash loan asset mismatch for %s: %s vs %s", 
                loan_id, expected_asset, asset
            )
        
        self.treasury[asset] = self.treasury.get(asset, Decimal(0)) + repay_amount
        logger.info(
            "Settled flash loan %s: returned %s %s. Treasury now: %s",
            loan_id,
            repay_amount,
            asset,
            self.treasury[asset],
        )

    def provide_liquidity(self, asset: str, amount: Decimal) -> None:
        """Allocate treasury capital to pool."""
        amount = Decimal(amount)
        if amount <= 0:
            return
        
        available = self.available_flashloan(asset)
        if amount > available:
            raise ValueError("Insufficient treasury to provide liquidity")
        
        self.treasury[asset] = available - amount
        logger.info("Allocated %s %s from treasury to pool", amount, asset)

    def reclaim_liquidity(self, asset: str, amount: Decimal) -> None:
        """Return liquidity from pool to treasury."""
        amount = Decimal(amount)
        if amount <= 0:
            return
        
        self.treasury[asset] = self.treasury.get(asset, Decimal(0)) + amount
        logger.info("Returned %s %s to treasury", amount, asset)

    def cancel_flashloan_reservation(self, loan_id: str) -> None:
        """Cancel flash loan reservation and return capital to treasury."""
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


# Global instance
bfx_client = BitfinexClient(CONFIG.bitfinex_api_key, CONFIG.bitfinex_api_secret)
