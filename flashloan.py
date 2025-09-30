"""
Enhanced flash loan API with covenant-aware validation and monitoring.

This API exposes flash loan functionality to external arbitrageurs
while enforcing covenant rules and tracking leverage impact.

Key improvements:
- Pre-validation of covenant constraints before issuing loans
- Leverage impact analysis in loan quotes
- Rate limiting and abuse prevention
- Detailed metrics for monitoring
- Graceful error handling with informative messages
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from decimal import Decimal
from typing import Any, Dict, Optional
from datetime import datetime, timedelta

from flask import Flask, jsonify, request, Response
from functools import wraps

from .amm_contract_enhanced import ENHANCED_POOL, ArbitrageOpportunity
from .bfx_client import bfx_client
from .config import CONFIG
from .liquid_utils import (
    build_flashloan_pset,
    decode_simulation_pset,
    sign_and_send_pset,
    fetch_pool_state,
)

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class RateLimiter:
    """Simple in-memory rate limiter for API endpoints."""
    
    def __init__(self, requests_per_minute: int = 10):
        self.requests_per_minute = requests_per_minute
        self.requests: Dict[str, list[float]] = defaultdict(list)
        
    def is_allowed(self, identifier: str) -> bool:
        """Check if request from identifier is within rate limit."""
        now = time.time()
        minute_ago = now - 60
        
        # Clean old requests
        self.requests[identifier] = [
            req_time for req_time in self.requests[identifier]
            if req_time > minute_ago
        ]
        
        # Check limit
        if len(self.requests[identifier]) >= self.requests_per_minute:
            return False
            
        self.requests[identifier].append(now)
        return True


class FlashLoanMetrics:
    """Track flash loan API metrics."""
    
    def __init__(self):
        self.total_requests = 0
        self.successful_loans = 0
        self.failed_loans = 0
        self.total_volume = Decimal(0)
        self.total_fees_collected = Decimal(0)
        self.active_loans: Dict[str, datetime] = {}
        self.avg_loan_duration: list[float] = []
        
    def record_request(self):
        """Record a loan request."""
        self.total_requests += 1
        
    def record_loan_issued(self, loan_id: str, amount: Decimal):
        """Record a loan being issued."""
        self.active_loans[loan_id] = datetime.now()
        self.total_volume += amount
        
    def record_loan_completed(self, loan_id: str, fee: Decimal):
        """Record successful loan completion."""
        if loan_id in self.active_loans:
            duration = (datetime.now() - self.active_loans[loan_id]).total_seconds()
            self.avg_loan_duration.append(duration)
            del self.active_loans[loan_id]
            
        self.successful_loans += 1
        self.total_fees_collected += fee
        
    def record_loan_failed(self, loan_id: str):
        """Record failed loan."""
        if loan_id in self.active_loans:
            del self.active_loans[loan_id]
        self.failed_loans += 1
        
    def summary(self) -> Dict[str, Any]:
        """Get metrics summary."""
        avg_duration = (
            sum(self.avg_loan_duration) / len(self.avg_loan_duration)
            if self.avg_loan_duration else 0
        )
        
        return {
            "total_requests": self.total_requests,
            "successful_loans": self.successful_loans,
            "failed_loans": self.failed_loans,
            "success_rate": (
                f"{100 * self.successful_loans / (self.successful_loans + self.failed_loans):.1f}%"
                if (self.successful_loans + self.failed_loans) > 0 else "N/A"
            ),
            "total_volume": f"${self.total_volume:,.2f}",
            "total_fees_collected": f"${self.total_fees_collected:,.2f}",
            "active_loans": len(self.active_loans),
            "avg_loan_duration": f"{avg_duration:.2f}s",
        }


# Global instances
rate_limiter = RateLimiter(requests_per_minute=10)
metrics = FlashLoanMetrics()


def rate_limit(f):
    """Decorator to enforce rate limiting on endpoints."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Use IP address as identifier (in production, use API key)
        identifier = request.remote_addr or "unknown"
        
        if not rate_limiter.is_allowed(identifier):
            logger.warning("Rate limit exceeded for %s", identifier)
            return jsonify({
                "error": "Rate limit exceeded. Maximum 10 requests per minute."
            }), 429
            
        return f(*args, **kwargs)
    return decorated_function


def _decimal_from_request(value: Any) -> Decimal:
    """Safely convert request parameter to Decimal."""
    try:
        return Decimal(str(value))
    except Exception as exc:
        raise ValueError("Invalid numeric value") from exc


def _validate_pool_health() -> tuple[bool, Optional[str]]:
    """Validate pool is healthy enough to issue flash loans."""
    state = ENHANCED_POOL.get_leverage_state()
    
    if not state.is_healthy:
        return (False, 
                f"Pool unhealthy: leverage ratio {state.debt_ratio:.4f} "
                f"outside safety bands [0.0625, 0.53125]")
                
    # Check if reserves sufficient
    min_reserve_btc = Decimal("0.01")
    min_reserve_usdt = Decimal("1000")
    
    if state.lbtc_reserve < min_reserve_btc:
        return (False, f"Insufficient L-BTC reserves: {state.lbtc_reserve} BTC")
        
    if state.lusdt_reserve < min_reserve_usdt:
        return (False, f"Insufficient L-USDT reserves: ${state.lusdt_reserve}")
        
    return (True, None)


@app.route("/health", methods=["GET"])
def health_check() -> Response:
    """Health check endpoint for monitoring."""
    state = ENHANCED_POOL.get_leverage_state()
    pool_healthy, error = _validate_pool_health()
    
    health_data = {
        "status": "healthy" if pool_healthy else "unhealthy",
        "pool_state": state.to_dict(),
        "bitfinex_available": bfx_client.available_flashloan("LUSDt") > Decimal("1000"),
        "metrics": metrics.summary(),
        "error": error if not pool_healthy else None,
    }
    
    status_code = 200 if pool_healthy else 503
    return jsonify(health_data), status_code


@app.route("/pool/state", methods=["GET"])
def get_pool_state() -> Response:
    """Get current pool state including leverage metrics."""
    state = ENHANCED_POOL.get_leverage_state()
    pool_state = fetch_pool_state()
    
    return jsonify({
        "reserves": {
            "lbtc": str(pool_state[CONFIG.pool_asset_a]),
            "lusdt": str(pool_state[CONFIG.pool_asset_b]),
        },
        "price": str(pool_state["price"]),
        "leverage": state.to_dict(),
        "fees_accumulated": {
            "lbtc": str(ENHANCED_POOL.accumulated_fees[CONFIG.pool_asset_a]),
            "lusdt": str(ENHANCED_POOL.accumulated_fees[CONFIG.pool_asset_b]),
        },
    })


@app.route("/flashloan/opportunities", methods=["GET"])
def get_arbitrage_opportunities() -> Response:
    """
    Get current arbitrage opportunities.
    
    Useful for arbitrage bots to check if profitable trades exist.
    """
    market_price = ENHANCED_POOL.btc_price
    tolerance = Decimal(CONFIG.price_tolerance_bps) / Decimal(10_000)
    
    opportunity = ENHANCED_POOL.arbitrage_opportunity(market_price, tolerance)
    
    if opportunity is None:
        return jsonify({
            "arbitrage_available": False,
            "message": "Pool price within tolerance of market price",
            "pool_price": str(ENHANCED_POOL.price()),
            "market_price": str(market_price),
        })
        
    return jsonify({
        "arbitrage_available": True,
        "opportunity": opportunity.to_summary(),
        "pool_price": str(ENHANCED_POOL.price()),
        "market_price": str(market_price),
        "leverage_warning": opportunity.leverage_impact,
    })


@app.route("/flashloan/request", methods=["POST"])
@rate_limit
def flashloan_request() -> Response:
    """
    Request a flash loan with covenant validation.
    
    Request body:
    {
        "asset": "LUSDt",  # or "LBTC"
        "amount": "5000"
    }
    
    Returns PSET template with loan terms and arbitrage suggestions.
    """
    metrics.record_request()
    
    # Validate pool health first
    pool_healthy, error = _validate_pool_health()
    if not pool_healthy:
        logger.warning("Flash loan request denied: %s", error)
        return jsonify({"error": error}), 503
        
    # Parse request
    data = request.get_json(force=True)
    asset = data.get("asset", CONFIG.pool_asset_b)
    amount_raw = data.get("amount")
    
    # Validate inputs
    if asset not in (CONFIG.pool_asset_a, CONFIG.pool_asset_b):
        return jsonify({"error": f"Unsupported asset: {asset}"}), 400
        
    try:
        amount = _decimal_from_request(amount_raw)
    except ValueError:
        return jsonify({"error": "Invalid amount format"}), 400
        
    if amount <= 0:
        return jsonify({"error": "Amount must be positive"}), 400
        
    # Check max loan amount
    max_loan = ENHANCED_POOL.reserves[asset] * CONFIG.max_flashloan_ratio
    if amount > max_loan:
        return jsonify({
            "error": f"Requested amount ${amount} exceeds maximum ${max_loan:.2f} "
                    f"({CONFIG.max_flashloan_ratio * 100}% of reserves)"
        }), 400
        
    # Prepare flash loan
    try:
        loan_terms = ENHANCED_POOL.prepare_flashloan(
            asset=asset,
            amount=amount,
            purpose="arbitrage",
        )
    except ValueError as exc:
        logger.error("Flash loan preparation failed: %s", exc)
        return jsonify({"error": str(exc)}), 400
        
    # Reserve Bitfinex capital
    try:
        bfx_client.reserve_flashloan_capital(
            loan_terms.loan_id,
            loan_terms.borrow_asset,
            loan_terms.borrow_amount,
        )
    except ValueError as exc:
        ENHANCED_POOL.cancel_flashloan(loan_terms.loan_id)
        logger.error("Bitfinex treasury cannot fund flash loan: %s", exc)
        return jsonify({
            "error": f"Bitfinex liquidity unavailable: {exc}"
        }), 503
        
    # Calculate arbitrage plan
    tolerance = Decimal(CONFIG.price_tolerance_bps) / Decimal(10_000)
    plan = ENHANCED_POOL.plan_flashloan_arbitrage(
        loan_terms,
        ENHANCED_POOL.btc_price,
        tolerance,
    )
    
    swaps = plan.get("swaps", [])
    expected_profit = plan.get("expected_profit")
    notes = plan.get("notes", {})
    
    # Add leverage impact warning if applicable
    if swaps:
        test_quote = swaps[0]
        if hasattr(test_quote, 'leverage_state_after'):
            if not test_quote.leverage_state_after.is_healthy:
                notes["leverage_warning"] = (
                    "WARNING: This arbitrage would push leverage ratio "
                    "outside safety bands. Transaction will be rejected by covenant."
                )
                logger.warning(
                    "Flash loan %s would violate leverage constraints",
                    loan_terms.loan_id
                )
                
    # Build PSET template
    pset = build_flashloan_pset(
        loan_terms,
        swaps=swaps,
        expected_profit=expected_profit,
        notes=notes,
    )
    
    # Record metrics
    metrics.record_loan_issued(loan_terms.loan_id, loan_terms.borrow_amount)
    
    response: Dict[str, Any] = {
        "pset": pset,
        "loan_terms": loan_terms.to_payload(),
        "notes": notes,
        "expected_profit": str(expected_profit) if expected_profit is not None else None,
        "swap_plan": [swap.to_payload() for swap in swaps],
        "instructions": (
            "1. Review the swap plan and expected profit\n"
            "2. Optionally modify the PSET with your own arbitrage strategy\n"
            "3. Ensure repayment output returns loan_terms.repay_amount to pool\n"
            "4. Sign all your inputs\n"
            "5. POST the signed PSET to /flashloan/submit"
        ),
    }
    
    logger.info(
        "Issued flash loan template %s for %s %s (profit: %s)",
        loan_terms.loan_id,
        loan_terms.borrow_amount,
        loan_terms.borrow_asset,
        expected_profit,
    )
    
    return jsonify(response)


@app.route("/flashloan/submit", methods=["POST"])
@rate_limit
def flashloan_submit() -> Response:
    """
    Submit signed flash loan PSET for execution.
    
    Request body:
    {
        "pset": "<base64_encoded_pset>"
    }
    
    Returns transaction ID and settlement details if successful.
    """
    data = request.get_json(force=True)
    pset = data.get("pset")
    
    if not pset:
        return jsonify({"error": "Missing PSET"}), 400
        
    # Decode PSET
    decoded = decode_simulation_pset(pset)
    loan_meta = None
    
    if decoded and decoded.get("type") == "flashloan":
        loan_meta = decoded.get("flashloan")
    else:
        return jsonify({
            "error": "Invalid PSET: not a flash loan transaction"
        }), 400
        
    loan_id = loan_meta.get("loan_id")
    
    # Validate loan exists
    if loan_id not in ENHANCED_POOL.active_loans:
        return jsonify({
            "error": f"Unknown or expired loan ID: {loan_id}"
        }), 404
        
    # Validate covenant constraints before broadcasting
    # In production, this would verify the actual PSET structure
    # For demo, we validate using the simulator
    try:
        result = sign_and_send_pset(pset, decoded_pset=decoded)
    except Exception as exc:
        logger.error("Error broadcasting PSET for loan %s: %s", loan_id, exc)
        
        # Cleanup on failure
        ENHANCED_POOL.cancel_flashloan(loan_id)
        bfx_client.cancel_flashloan_reservation(loan_id)
        metrics.record_loan_failed(loan_id)
        
        return jsonify({
            "error": f"Transaction validation failed: {exc}"
        }), 400
        
    # Settlement
    response: Dict[str, Any] = {"txid": result.txid}
    
    if result.details:
        response["details"] = result.details
        
        # Settle with Bitfinex
        repay_amount = result.details.get(
            "repay_amount",
            loan_meta.get("repay_amount")
        )
        
        if repay_amount is not None:
            fee_collected = Decimal(str(result.details.get("fee_collected", 0)))
            
            bfx_client.settle_flashloan(
                loan_id,
                loan_meta.get("repay_asset"),
                Decimal(str(repay_amount)),
            )
            
            metrics.record_loan_completed(loan_id, fee_collected)
            response["loan_id"] = loan_id
            response["fee_collected"] = str(fee_collected)
            
            logger.info(
                "Flash loan %s completed successfully. Fee collected: %s %s",
                loan_id,
                fee_collected,
                loan_meta.get("repay_asset"),
            )
    else:
        # No details means failure
        metrics.record_loan_failed(loan_id)
        
    return jsonify(response)


@app.route("/flashloan/cancel/<loan_id>", methods=["POST"])
def flashloan_cancel(loan_id: str) -> Response:
    """
    Cancel a flash loan request (before submission).
    
    Useful if arbitrageur decides not to execute the loan.
    """
    if loan_id not in ENHANCED_POOL.active_loans:
        return jsonify({
            "error": f"Unknown loan ID: {loan_id}"
        }), 404
        
    ENHANCED_POOL.cancel_flashloan(loan_id)
    bfx_client.cancel_flashloan_reservation(loan_id)
    metrics.record_loan_failed(loan_id)
    
    logger.info("Flash loan %s cancelled by user", loan_id)
    
    return jsonify({
        "message": f"Flash loan {loan_id} cancelled successfully",
        "loan_id": loan_id,
    })


@app.route("/metrics", methods=["GET"])
def get_metrics() -> Response:
    """Get API metrics for monitoring."""
    return jsonify({
        "flash_loans": metrics.summary(),
        "pool": ENHANCED_POOL.get_leverage_state().to_dict(),
        "bitfinex_treasury": {
            "lbtc_available": str(bfx_client.available_flashloan("LBTC")),
            "lusdt_available": str(bfx_client.available_flashloan("LUSDt")),
        },
    })


@app.errorhandler(404)
def not_found(error) -> Response:
    """Handle 404 errors."""
    return jsonify({"error": "Endpoint not found"}), 404


@app.errorhandler(500)
def internal_error(error) -> Response:
    """Handle 500 errors."""
    logger.error("Internal server error: %s", error, exc_info=True)
    return jsonify({
        "error": "Internal server error. Please try again later."
    }), 500


if __name__ == "__main__":
    logger.info("Starting Kupera Flash Loan API...")
    logger.info("Pool configuration:")
    logger.info("  - Asset A: %s", CONFIG.pool_asset_a)
    logger.info("  - Asset B: %s", CONFIG.pool_asset_b)
    logger.info("  - Swap fee: %.2f%%", CONFIG.fee_bps / 100)
    logger.info("  - Flash loan fee: %.2f%%", CONFIG.flashloan_fee_bps / 100)
    logger.info("  - Max flash loan: %.0f%% of reserves", 
                CONFIG.max_flashloan_ratio * 100)
    
    # Start Flask app
    app.run(
        host="0.0.0.0",
        port=8000,
        debug=False,  # Set to True for development only
        threaded=True,
    )
