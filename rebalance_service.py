"""
Rebalance monitoring service - EXAMPLE arbitrage bot.

This is NOT required infrastructure. It's an EXAMPLE showing how
arbitrageurs can profit from maintaining the 50% debt ratio.

In production, any external party can run their own bot or manually
execute rebalancing trades via the flash loan API.
"""

import asyncio
import logging
from decimal import Decimal

# FIXED: Use absolute imports
import amm_contract
import bfx_client
import config
import liquid_utils

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# Global references
SIMULATED_POOL = amm_contract.SIMULATED_POOL
bfx_client_instance = bfx_client.bfx_client
CONFIG = config.CONFIG


async def rebalance_loop() -> None:
    """
    Example rebalancing bot that monitors pool and executes arbitrage.
    
    This demonstrates how arbitrageurs profit from YieldBasis rebalancing:
    1. Detect deviation from 50% debt ratio
    2. Borrow via flash loan
    3. Add/remove debt to restore ratio
    4. Profit from the rebalancing
    5. Repay flash loan + fee
    """
    
    poll_interval = CONFIG.rebalance_poll_interval_seconds
    
    logger.info("🤖 Starting example rebalancing bot...")
    logger.info("   Current BTC price: $%s", SIMULATED_POOL.btc_price)
    logger.info("   Poll interval: %s seconds", poll_interval)
    
    while True:
        state = liquid_utils.fetch_pool_state()
        lbtc = Decimal(state[CONFIG.pool_asset_a])
        lusdt = Decimal(state[CONFIG.pool_asset_b])
        pool_price = Decimal(state["price"])
        
        leverage_state = SIMULATED_POOL.get_leverage_state()
        
        logger.info(
            "📊 Pool: %s BTC / $%s USDT | Price: $%s | Ratio: %.2f%%",
            lbtc,
            lusdt,
            pool_price,
            leverage_state.debt_ratio * 100,
        )
        
        # Check for rebalancing opportunity
        opportunity = SIMULATED_POOL.detect_rebalance_opportunity()
        
        if opportunity:
            logger.info("💰 ARBITRAGE DETECTED: %s", opportunity.to_summary())
            
            # Check Bitfinex treasury has capital
            available = bfx_client_instance.available_flashloan(CONFIG.pool_asset_b)
            borrow_amount = min(opportunity.debt_adjustment, available)
            
            if borrow_amount <= Decimal("100"):
                logger.warning("⚠️  Insufficient Bitfinex treasury: $%s", available)
            else:
                try:
                    # Prepare flash loan
                    logger.info("   Preparing flash loan for $%s...", borrow_amount)
                    terms = SIMULATED_POOL.prepare_flashloan(
                        CONFIG.pool_asset_b,
                        borrow_amount,
                        purpose="rebalancing"
                    )
                    
                    # Reserve Bitfinex capital
                    bfx_client_instance.reserve_flashloan_capital(
                        terms.loan_id,
                        terms.borrow_asset,
                        terms.borrow_amount
                    )
                    
                    # Plan arbitrage
                    tolerance = Decimal(CONFIG.price_tolerance_bps) / Decimal(10_000)
                    plan = SIMULATED_POOL.plan_flashloan_arbitrage(
                        terms,
                        SIMULATED_POOL.btc_price,
                        tolerance
                    )
                    
                    # Build and execute PSET
                    pset = liquid_utils.build_flashloan_pset(
                        terms,
                        swaps=plan.get("swaps", []),
                        expected_profit=plan.get("expected_profit"),
                        notes={**plan.get("notes", {}), "initiator": "example_bot"}
                    )
                    
                    decoded = liquid_utils.decode_simulation_pset(pset)
                    
                    # Execute rebalancing
                    logger.info("   Executing rebalancing: %s", opportunity.action)
                    SIMULATED_POOL.rebalance_via_flashloan(
                        borrow_amount,
                        opportunity.action
                    )
                    
                    # Broadcast transaction
                    result = liquid_utils.sign_and_send_pset(pset, decoded_pset=decoded)
                    
                    # Settle with Bitfinex
                    details = result.details or {}
                    repay_amount = Decimal(details.get("repay_amount", terms.repay_amount))
                    bfx_client_instance.settle_flashloan(
                        terms.loan_id,
                        terms.repay_asset,
                        repay_amount
                    )
                    
                    new_state = SIMULATED_POOL.get_leverage_state()
                    logger.info(
                        "   ✅ Rebalanced! TX: %s | New ratio: %.2f%% | Profit: $%s",
                        result.txid,
                        new_state.debt_ratio * 100,
                        plan.get("expected_profit", 0)
                    )
                    
                except ValueError as exc:
                    logger.error("   ❌ Rebalancing failed: %s", exc)
                    if terms.loan_id in SIMULATED_POOL.active_loans:
                        SIMULATED_POOL.cancel_flashloan(terms.loan_id)
                        bfx_client_instance.cancel_flashloan_reservation(terms.loan_id)
                        
                except Exception as exc:
                    logger.error("   ❌ Unexpected error: %s", exc, exc_info=True)
                    if terms.loan_id in SIMULATED_POOL.active_loans:
                        SIMULATED_POOL.cancel_flashloan(terms.loan_id)
                        bfx_client_instance.cancel_flashloan_reservation(terms.loan_id)
        else:
            logger.info("✅ Pool balanced (ratio within 5% of target)")
        
        logger.info("")
        await asyncio.sleep(poll_interval)


if __name__ == "__main__":
    logger.info("=" * 70)
    logger.info("YIELDBASIS EXAMPLE REBALANCING BOT")
    logger.info("=" * 70)
    logger.info("")
    logger.info("This is an EXAMPLE bot showing how arbitrageurs profit")
    logger.info("from maintaining the 50%% debt ratio in YieldBasis.")
    logger.info("")
    logger.info("In production:")
    logger.info("  - Any external party can run rebalancing bots")
    logger.info("  - No centralized operator needed")
    logger.info("  - Market forces maintain leverage automatically")
    logger.info("")
    logger.info("=" * 70)
    logger.info("")
    
    try:
        asyncio.run(rebalance_loop())
    except KeyboardInterrupt:
        logger.info("\n🛑 Bot stopped by user")
