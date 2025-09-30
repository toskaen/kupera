"""Asynchronous rebalancer that demonstrates Bitfinex-backed arbitrage."""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

from .amm_contract import SIMULATED_POOL
from .bfx_client import bfx_client
from .config import CONFIG
from .liquid_utils import (
    build_flashloan_pset,
    decode_simulation_pset,
    fetch_pool_state,
    sign_and_send_pset,
)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


async def rebalance_loop() -> None:
    """Continuously monitor the pool and execute simulated arbitrage trades."""

    target_price = CONFIG.btc_usd_price
    tolerance = Decimal(CONFIG.price_tolerance_bps) / Decimal(10_000)
    poll_interval = CONFIG.rebalance_poll_interval_seconds

    while True:
        state = fetch_pool_state()
        lb_tc = Decimal(state[CONFIG.pool_asset_a])
        l_usd = Decimal(state[CONFIG.pool_asset_b])
        pool_price = Decimal(state["price"])
        value_btc_usd = lb_tc * target_price
        total_value = value_btc_usd + l_usd
        ratio = value_btc_usd / total_value if total_value > 0 else Decimal(0)
        logger.info(
            "Pool reserves: %s %s / %s %s | price %s | ratio %.4f",
            _serialize(lb_tc),
            CONFIG.pool_asset_a,
            _serialize(l_usd),
            CONFIG.pool_asset_b,
            _serialize(pool_price),
            ratio,
        )

        opportunity = SIMULATED_POOL.arbitrage_opportunity(target_price, tolerance)
        if opportunity:
            logger.info("Detected arbitrage opportunity: %s", opportunity.to_summary())
            available = bfx_client.available_flashloan(opportunity.borrow_asset)
            borrow_amount = min(opportunity.borrow_amount, available)
            if borrow_amount <= 0:
                logger.warning("Bitfinex treasury exhausted for %s", opportunity.borrow_asset)
            else:
                try:
                    terms = SIMULATED_POOL.prepare_flashloan(
                        opportunity.borrow_asset, borrow_amount
                    )
                except ValueError as exc:
                    logger.warning("Unable to prepare flash loan: %s", exc)
                else:
                    try:
                        bfx_client.reserve_flashloan_capital(
                            terms.loan_id, terms.borrow_asset, terms.borrow_amount
                        )
                    except ValueError as exc:
                        SIMULATED_POOL.cancel_flashloan(terms.loan_id)
                        logger.warning("Bitfinex treasury reservation failed: %s", exc)
                    else:
                        plan = SIMULATED_POOL.plan_flashloan_arbitrage(
                            terms, target_price, tolerance
                        )
                        swaps = plan.get("swaps", [])
                        notes = {**plan.get("notes", {}), "initiator": "rebalancer"}
                        pset = build_flashloan_pset(
                            terms,
                            swaps=swaps,
                            expected_profit=plan.get("expected_profit"),
                            notes=notes,
                        )
                        decoded = decode_simulation_pset(pset)
                        try:
                            result = sign_and_send_pset(pset, decoded_pset=decoded)
                        except Exception as exc:  # pragma: no cover - defensive path
                            logger.error("Rebalance flash loan failed: %s", exc)
                            SIMULATED_POOL.cancel_flashloan(terms.loan_id)
                            bfx_client.cancel_flashloan_reservation(terms.loan_id)
                        else:
                            details = result.details or {}
                            repay_amount = Decimal(
                                details.get("repay_amount", terms.repay_amount)
                            )
                            bfx_client.settle_flashloan(
                                terms.loan_id, terms.repay_asset, repay_amount
                            )
                            logger.info(
                                "Rebalance transaction %s executed, pool price now %s",
                                result.txid,
                                _serialize(SIMULATED_POOL.price()),
                            )
        else:
            logger.info(
                "Pool price %.2f within tolerance of Bitfinex %.2f",
                pool_price,
                target_price,
            )

        await asyncio.sleep(poll_interval)


def _serialize(value: Decimal) -> str:
    return format(value, "f")


if __name__ == "__main__":  # pragma: no cover - manual execution helper
    asyncio.run(rebalance_loop())
