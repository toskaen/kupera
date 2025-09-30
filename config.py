"""
Central configuration for the YieldBasis‑on‑Liquid MVP.

This module defines a dataclass with all runtime parameters needed by
the orchestrator, Bitfinex client, and flash‑loan service.  Using a
dataclass ensures a single source of truth for settings and allows
type‑checked access throughout the codebase.

Environment variables should be used to supply sensitive credentials
such as API keys.  Default values are provided for development
purposes but **must** be overridden for production.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal


@dataclass
class Config:
    """Central configuration for the MVP."""

    # Liquid RPC connection
    liquid_rpc_url: str = os.environ.get("LIQUID_RPC_URL", "http://localhost:18884")
    liquid_rpc_user: str = os.environ.get("LIQUID_RPC_USER", "user")
    liquid_rpc_password: str = os.environ.get("LIQUID_RPC_PASSWORD", "pass")

    # Bitfinex API credentials
    bitfinex_api_key: str = os.environ.get("BITFINEX_API_KEY", "")
    bitfinex_api_secret: str = os.environ.get("BITFINEX_API_SECRET", "")

    # Pool parameters
    pool_asset_a: str = os.environ.get("POOL_ASSET_A", "LBTC")  # primary asset (BTC)
    pool_asset_b: str = os.environ.get("POOL_ASSET_B", "LUSDt")  # secondary asset (stablecoin)
    fee_bps: int = int(os.environ.get("POOL_FEE_BPS", 30))  # fee in basis points (0.30%)
    flashloan_fee_bps: int = int(os.environ.get("FLASHLOAN_FEE_BPS", 5))  # 0.05%
    initial_lbtc_reserve: Decimal = Decimal(os.environ.get("INITIAL_LBTC_RESERVE", "1"))
    initial_lusdt_reserve: Decimal = Decimal(os.environ.get("INITIAL_LUSDT_RESERVE", "30000"))
    max_flashloan_ratio: Decimal = Decimal(os.environ.get("MAX_FLASHLOAN_RATIO", "0.3"))

    # Rebalancing parameters
    target_ratio: Decimal = Decimal(os.environ.get("TARGET_RATIO", "0.5"))
    rebalance_threshold: Decimal = Decimal(os.environ.get("REBALANCE_THRESHOLD", "0.05"))
    rebalance_poll_interval_seconds: int = int(os.environ.get("REBALANCE_POLL_INTERVAL", "30"))
    price_tolerance_bps: int = int(os.environ.get("PRICE_TOLERANCE_BPS", "10"))

    # Price feed (could be replaced with an oracle)
    btc_usd_price: Decimal = Decimal(os.environ.get("BTC_USD_PRICE", "30000"))

    # Bitfinex treasury used to bootstrap flash loans
    bitfinex_prep_capital_usdt: Decimal = Decimal(os.environ.get("BITFINEX_PRECAP_USDT", "100000"))
    bitfinex_prep_capital_lbtc: Decimal = Decimal(os.environ.get("BITFINEX_PRECAP_LBTC", "0"))


CONFIG = Config()
