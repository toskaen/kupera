"""
Rebalancing service for the YieldBasis‑on‑Liquid MVP.

This script monitors the state of the pool and Bitfinex balances to
maintain a 50/50 value split between L‑BTC and L‑USDT while keeping
the LP’s net position at 2× BTC exposure (no impermanent loss).  It
periodically checks the pool balances, compares the current BTC value
to the USDT side using a price feed, and initiates rebalances when the
ratio deviates beyond a configured threshold.  Rebalancing actions
include:

* Borrowing USDT from Bitfinex (if configured) and adding it to the
  pool when BTC appreciates relative to USD.
* Withdrawing excess USDT from the pool and repaying Bitfinex debt
  when BTC depreciates.
* Opening hedging positions on Bitfinex (e.g., selling/buying BTC) to
  neutralize the operator’s net BTC exposure as needed.

For simplicity, this MVP uses stub functions in `liquid_utils` and
`bfx_client` to represent on‑chain and off‑chain operations.  The
`rebalance` coroutine shows the high‑level steps that a complete
implementation would take.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

from .config import CONFIG
from .liquid_utils import fetch_pool_state, build_swap_pset, sign_and_send_pset
from .bfx_client import bfx_client

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


async def rebalance_loop() -> None:
    """
    Continuously monitor the pool and perform rebalancing when needed.

    In each iteration we:
    1. Fetch the current on‑chain pool state (LBTC and LUSDT balances).
    2. Compute the USD value of the BTC side using a price feed.
    3. Determine the deviation from the target ratio (e.g. 50/50).
    4. If deviation exceeds the threshold, perform an off‑chain trade on
       Bitfinex and adjust the on‑chain pool using a PSET swap.
    5. Sleep for a configured interval and repeat.
    """
    price_btc_usd = CONFIG.btc_usd_price
    threshold = CONFIG.rebalance_threshold
    ratio_target = CONFIG.target_ratio

    while True:
        state = fetch_pool_state()
        lb_tc = Decimal(state["lb_tc_balance"])
        l_usd = Decimal(state["lusdt_balance"])
        # Compute values in USD
        value_btc_usd = lb_tc * price_btc_usd
        value_usdt = l_usd  # USDT assumed pegged
        total_value = value_btc_usd + value_usdt
        current_ratio = value_btc_usd / total_value if total_value > 0 else Decimal(0)
        deviation = current_ratio - ratio_target
        logger.info(
            "Pool state: %.8f LBTC ($%.2f) and %.2f LUSDT, ratio %.4f",
            lb_tc, value_btc_usd, l_usd, current_ratio
        )
        if deviation > threshold:
            # BTC side too high – borrow USDT from Bitfinex and add to pool
            needed_value = (ratio_target - current_ratio) * total_value
            amount_usdt_needed = needed_value  # 1:1 USD
            logger.info(
                "BTC side over target by %.2f%%, adding %.2f USDT to pool",
                deviation * 100, amount_usdt_needed
            )
            # Borrow USDT or use treasury
            bfx_client.place_order("tBTCUSD", 0, 0, "sell")  # placeholder
            # Create and sign a PSET adding USDT to the pool (not implemented)
            pset = build_swap_pset([], 0, 0, 0)
            sign_and_send_pset(pset)
        elif deviation < -threshold:
            # BTC side too low – withdraw USDT from pool and repay Bitfinex
            needed_value = (ratio_target - current_ratio) * total_value
            amount_usdt_removed = -needed_value
            logger.info(
                "BTC side under target by %.2f%%, removing %.2f USDT from pool",
                -deviation * 100, amount_usdt_removed
            )
            # Sell BTC on Bitfinex to repay USDT debt
            bfx_client.place_order("tBTCUSD", 0, 0, "buy")  # placeholder
            pset = build_swap_pset([], 0, 0, 0)
            sign_and_send_pset(pset)
        else:
            logger.info("Pool ratio within threshold (%.4f), no rebalance needed", current_ratio)
        # Sleep before next check
        await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(rebalance_loop())