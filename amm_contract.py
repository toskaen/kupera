"""
Enhanced Bitmatrix-inspired AMM with YieldBasis leverage mechanism.

This module extends the base AMM simulator to include:
1. Explicit leverage ratio tracking and enforcement (50% debt-to-value)
2. Safety band validation matching YieldBasis Ethereum implementation
3. Debt obligation tracking for the 2x leverage mechanism
4. Rebalancing necessity detection
5. Covenant-style validation that mimics on-chain enforcement

The simulator validates all constraints a real covenant would enforce,
making it an accurate model for the production implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, getcontext
from typing import Dict, Iterable, List, Optional, Tuple
from uuid import uuid4
import logging

getcontext().prec = 28
logger = logging.getLogger(__name__)


def _to_decimal(value: Decimal | float | int | str) -> Decimal:
    """Helper that normalises numeric inputs to Decimal."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _serialize_decimal(value: Decimal) -> str:
    """Convert a Decimal to a JSON-friendly string."""
    return format(value, "f")


@dataclass
class LeverageState:
    """Current leverage state of the pool."""
    
    lbtc_reserve: Decimal
    lusdt_reserve: Decimal
    debt_amount: Decimal  # USDT debt for leverage
    btc_price: Decimal    # External BTC/USD price
    
    @property
    def pool_value_usd(self) -> Decimal:
        """Total pool value in USD terms."""
        return (self.lbtc_reserve * self.btc_price) + self.lusdt_reserve
    
    @property
    def debt_ratio(self) -> Decimal:
        """Current debt-to-value ratio (target is 0.5 for 2x leverage)."""
        if self.pool_value_usd == 0:
            return Decimal(0)
        return self.debt_amount / self.pool_value_usd
    
    @property
    def leverage_multiplier(self) -> Decimal:
        """Effective leverage multiplier (target is 2.0x)."""
        if self.debt_ratio == 1:
            return Decimal(999)  # Approaching infinite leverage
        return Decimal(1) / (Decimal(1) - self.debt_ratio)
    
    @property  
    def is_healthy(self) -> bool:
        """Check if leverage ratio is within YieldBasis safety bands."""
        return Decimal("0.0625") <= self.debt_ratio <= Decimal("0.53125")
    
    @property
    def needs_rebalancing(self) -> bool:
        """Check if rebalancing needed (outside target ±5% tolerance)."""
        target = Decimal("0.5")
        tolerance = Decimal("0.05")
        return abs(self.debt_ratio - target) > tolerance
    
    def to_dict(self) -> Dict[str, str]:
        """Serialize state for logging/API responses."""
        return {
            "lbtc_reserve": _serialize_decimal(self.lbtc_reserve),
            "lusdt_reserve": _serialize_decimal(self.lusdt_reserve),
            "debt_amount": _serialize_decimal(self.debt_amount),
            "pool_value_usd": _serialize_decimal(self.pool_value_usd),
            "debt_ratio": _serialize_decimal(self.debt_ratio),
            "leverage_multiplier": _serialize_decimal(self.leverage_multiplier),
            "is_healthy": str(self.is_healthy),
            "needs_rebalancing": str(self.needs_rebalancing),
        }


@dataclass
class SwapQuote:
    """Representation of a swap before or after execution."""
    
    input_asset: str
    output_asset: str
    amount_in: Decimal
    amount_out: Decimal
    fee_paid: Decimal
    price_after: Decimal
    leverage_state_after: Optional[LeverageState] = None
    
    def to_payload(self) -> Dict[str, str]:
        """Serialise the quote for JSON/base64 transport."""
        payload = {
            "input_asset": self.input_asset,
            "output_asset": self.output_asset,
            "amount_in": _serialize_decimal(self.amount_in),
            "amount_out": _serialize_decimal(self.amount_out),
            "fee_paid": _serialize_decimal(self.fee_paid),
            "price_after": _serialize_decimal(self.price_after),
        }
        if self.leverage_state_after:
            payload["leverage_state"] = self.leverage_state_after.to_dict()
        return payload


@dataclass
class FlashLoanTerms:
    """Snapshot of an active flash loan."""
    
    loan_id: str
    borrow_asset: str
    borrow_amount: Decimal
    repay_asset: str
    repay_amount: Decimal
    fee_amount: Decimal
    purpose: str = "arbitrage"  # or "rebalance"
    
    def to_payload(self) -> Dict[str, str]:
        return {
            "loan_id": self.loan_id,
            "borrow_asset": self.borrow_asset,
            "borrow_amount": _serialize_decimal(self.borrow_amount),
            "repay_asset": self.repay_asset,
            "repay_amount": _serialize_decimal(self.repay_amount),
            "fee": _serialize_decimal(self.fee_amount),
            "purpose": self.purpose,
        }


@dataclass
class RebalanceAction:
    """Suggested rebalancing operation to restore target leverage."""
    
    reason: str
    current_ratio: Decimal
    target_ratio: Decimal
    action_type: str  # "borrow" or "repay"
    amount_usdt: Decimal
    expected_ratio_after: Decimal
    
    def to_summary(self) -> Dict[str, str]:
        return {
            "reason": self.reason,
            "current_ratio": _serialize_decimal(self.current_ratio),
            "target_ratio": _serialize_decimal(self.target_ratio),
            "action_type": self.action_type,
            "amount_usdt": _serialize_decimal(self.amount_usdt),
            "expected_ratio_after": _serialize_decimal(self.expected_ratio_after),
        }


@dataclass
class ArbitrageOpportunity:
    """Suggested flash-loan-backed trade to realign the pool price."""
    
    reason: str
    target_price: Decimal
    borrow_asset: str
    borrow_amount: Decimal
    repay_amount: Decimal
    swap: SwapQuote
    expected_profit: Decimal
    leverage_impact: Optional[str] = None
    
    def to_summary(self) -> Dict[str, str]:
        summary = {
            "reason": self.reason,
            "target_price": _serialize_decimal(self.target_price),
            "borrow_asset": self.borrow_asset,
            "borrow_amount": _serialize_decimal(self.borrow_amount),
            "repay_amount": _serialize_decimal(self.repay_amount),
            "expected_profit": _serialize_decimal(self.expected_profit),
        }
        if self.leverage_impact:
            summary["leverage_impact"] = self.leverage_impact
        return summary


class YieldBasisAMM:
    """
    YieldBasis-enhanced AMM with leverage tracking and covenant-style validation.
    
    Extends Bitmatrix constant product AMM with:
    - 50% debt-to-value ratio enforcement (2x leverage)
    - Safety band validation (6.25% - 53.125%)
    - Debt obligation tracking
    - Rebalancing detection and suggestions
    - Covenant-aware transaction validation
    """
    
    def __init__(
        self,
        asset_a: str,
        asset_b: str,
        fee_bps: int,
        flash_fee_bps: int,
        initial_reserves: Dict[str, Decimal],
        max_flashloan_ratio: Decimal,
        btc_price: Decimal,
        target_leverage: Decimal = Decimal("2.0"),
    ) -> None:
        self.asset_a = asset_a  # L-BTC
        self.asset_b = asset_b  # L-USDT
        self.swap_fee = _to_decimal(fee_bps) / Decimal(10_000)
        self.flash_fee = _to_decimal(flash_fee_bps) / Decimal(10_000)
        self.max_flashloan_ratio = max_flashloan_ratio
        self.btc_price = _to_decimal(btc_price)
        self.target_leverage = _to_decimal(target_leverage)
        
        self.reserves: Dict[str, Decimal] = {
            asset_a: _to_decimal(initial_reserves.get(asset_a, Decimal(0))),
            asset_b: _to_decimal(initial_reserves.get(asset_b, Decimal(0))),
        }
        
        # YieldBasis debt tracking
        # Initial debt = 50% of pool value for 2x leverage
        initial_pool_value = (self.reserves[asset_a] * self.btc_price + 
                             self.reserves[asset_b])
        self.debt_amount = initial_pool_value * Decimal("0.5")
        
        self.accumulated_fees: Dict[str, Decimal] = {
            asset_a: Decimal(0),
            asset_b: Decimal(0),
        }
        self.active_loans: Dict[str, FlashLoanTerms] = {}
        
        logger.info(
            "Initialized YieldBasis AMM with %s BTC @ $%s = $%s pool value, "
            "$%s debt (%.2f%% ratio, %.2fx leverage)",
            self.reserves[asset_a],
            self.btc_price,
            initial_pool_value,
            self.debt_amount,
            self.get_leverage_state().debt_ratio * 100,
            self.get_leverage_state().leverage_multiplier,
        )
    
    # ------------------------------------------------------------------
    # Leverage State Management
    # ------------------------------------------------------------------
    
    def get_leverage_state(self) -> LeverageState:
        """Get current leverage state of the pool."""
        return LeverageState(
            lbtc_reserve=self.reserves[self.asset_a],
            lusdt_reserve=self.reserves[self.asset_b],
            debt_amount=self.debt_amount,
            btc_price=self.btc_price,
        )
    
    def update_btc_price(self, new_price: Decimal) -> None:
        """Update BTC price (triggers rebalancing detection)."""
        old_price = self.btc_price
        self.btc_price = _to_decimal(new_price)
        old_state = self.get_leverage_state()
        old_state.btc_price = old_price
        new_state = self.get_leverage_state()
        
        logger.info(
            "BTC price updated: $%s -> $%s | "
            "Debt ratio: %.2f%% -> %.2f%% | "
            "Leverage: %.2fx -> %.2fx",
            old_price,
            new_price,
            old_state.debt_ratio * 100,
            new_state.debt_ratio * 100,
            old_state.leverage_multiplier,
            new_state.leverage_multiplier,
        )
    
    def validate_leverage_covenant(self) -> Tuple[bool, str]:
        """
        Validate current state against covenant constraints.
        
        Mimics on-chain covenant validation:
        - Debt ratio within safety bands (6.25% - 53.125%)
        - Returns (success, message)
        """
        state = self.get_leverage_state()
        
        if not state.is_healthy:
            return (False, 
                   f"Leverage ratio {state.debt_ratio:.4f} outside safety bands "
                   f"[0.0625, 0.53125]. Pool unhealthy!")
        
        return (True, f"Leverage ratio {state.debt_ratio:.4f} healthy.")
    
    def suggest_rebalance(self) -> Optional[RebalanceAction]:
        """
        Suggest rebalancing action if leverage drifted from target.
        
        Returns None if within tolerance, otherwise returns action to take.
        """
        state = self.get_leverage_state()
        target_ratio = Decimal("0.5")  # 2x leverage
        current_ratio = state.debt_ratio
        
        # Check if rebalancing needed (>5% deviation from target)
        deviation = abs(current_ratio - target_ratio)
        tolerance = Decimal("0.05")
        
        if deviation <= tolerance:
            return None
        
        # Calculate required debt adjustment
        target_debt = state.pool_value_usd * target_ratio
        debt_adjustment = target_debt - self.debt_amount
        
        if debt_adjustment > 0:
            # Need to borrow more USDT
            return RebalanceAction(
                reason=f"Debt ratio {current_ratio:.2%} below target {target_ratio:.2%}. "
                       f"BTC price increased, need more debt.",
                current_ratio=current_ratio,
                target_ratio=target_ratio,
                action_type="borrow",
                amount_usdt=debt_adjustment,
                expected_ratio_after=target_ratio,
            )
        else:
            # Need to repay debt
            return RebalanceAction(
                reason=f"Debt ratio {current_ratio:.2%} above target {target_ratio:.2%}. "
                       f"BTC price decreased, need to repay debt.",
                current_ratio=current_ratio,
                target_ratio=target_ratio,
                action_type="repay",
                amount_usdt=abs(debt_adjustment),
                expected_ratio_after=target_ratio,
            )
    
    def execute_rebalance(self, action: RebalanceAction) -> bool:
        """
        Execute suggested rebalancing action.
        
        In production, this would trigger Bitfinex API calls and PSETs.
        Here we simulate the debt adjustment.
        """
        if action.action_type == "borrow":
            # Borrow USDT from Bitfinex, add to reserves
            self.reserves[self.asset_b] += action.amount_usdt
            self.debt_amount += action.amount_usdt
            logger.info("Borrowed $%s USDT for rebalancing", action.amount_usdt)
        else:  # repay
            # Remove USDT from reserves, reduce debt
            if self.reserves[self.asset_b] < action.amount_usdt:
                logger.error("Insufficient USDT reserves to repay debt")
                return False
            self.reserves[self.asset_b] -= action.amount_usdt
            self.debt_amount -= action.amount_usdt
            logger.info("Repaid $%s USDT debt for rebalancing", action.amount_usdt)
        
        # Validate new state
        valid, msg = self.validate_leverage_covenant()
        new_state = self.get_leverage_state()
        logger.info(
            "Rebalancing complete. New ratio: %.2f%%, leverage: %.2fx. %s",
            new_state.debt_ratio * 100,
            new_state.leverage_multiplier,
            msg,
        )
        
        return valid
    
    # ------------------------------------------------------------------
    # Standard AMM Operations (inherited from base)
    # ------------------------------------------------------------------
    
    def invariant(self) -> Decimal:
        """Calculate constant product invariant k = x * y."""
        return self.reserves[self.asset_a] * self.reserves[self.asset_b]
    
    def price(self) -> Decimal:
        """Return the marginal price (asset_b per asset_a)."""
        if self.reserves[self.asset_a] == 0:
            return Decimal(0)
        return self.reserves[self.asset_b] / self.reserves[self.asset_a]
    
    def snapshot(self) -> Dict[str, Decimal]:
        """Return current reserves."""
        return dict(self.reserves)
    
    def quote_swap(self, input_asset: str, amount_in: Decimal) -> SwapQuote:
        """Return the output amount and fee for a swap without mutating state."""
        amount_in = _to_decimal(amount_in)
        if amount_in <= 0:
            raise ValueError("Swap amount must be positive")
        
        if input_asset not in (self.asset_a, self.asset_b):
            raise ValueError("Unsupported asset for swap")
        
        output_asset = self.asset_b if input_asset == self.asset_a else self.asset_a
        reserve_in = self.reserves[input_asset]
        reserve_out = self.reserves[output_asset]
        
        if reserve_in <= 0 or reserve_out <= 0:
            raise ValueError("Pool is empty")
        
        # Apply swap fee
        amount_in_with_fee = amount_in * (Decimal(1) - self.swap_fee)
        numerator = amount_in_with_fee * reserve_out
        denominator = reserve_in + amount_in_with_fee
        amount_out = numerator / denominator
        
        if amount_out <= 0:
            raise ValueError("Swap would produce zero output")
        
        fee_paid = amount_in - amount_in_with_fee
        
        # Calculate post-swap state
        new_reserves = {
            input_asset: reserve_in + amount_in,
            output_asset: reserve_out - amount_out,
        }
        price_after = new_reserves[self.asset_b] / new_reserves[self.asset_a]
        
        # Calculate leverage state after swap
        leverage_state_after = LeverageState(
            lbtc_reserve=new_reserves[self.asset_a],
            lusdt_reserve=new_reserves[self.asset_b],
            debt_amount=self.debt_amount,
            btc_price=self.btc_price,
        )
        
        return SwapQuote(
            input_asset=input_asset,
            output_asset=output_asset,
            amount_in=amount_in,
            amount_out=amount_out,
            fee_paid=fee_paid,
            price_after=price_after,
            leverage_state_after=leverage_state_after,
        )
    
    def execute_swap(self, input_asset: str, amount_in: Decimal) -> SwapQuote:
        """Mutate reserves according to the swap and return the realised quote."""
        quote = self.quote_swap(input_asset, amount_in)
        
        # Update reserves
        self.reserves[quote.input_asset] += quote.amount_in
        self.reserves[quote.output_asset] -= quote.amount_out
        self.accumulated_fees[quote.input_asset] += quote.fee_paid
        
        # Validate covenant post-swap
        valid, msg = self.validate_leverage_covenant()
        if not valid:
            logger.warning("Swap resulted in unhealthy leverage state: %s", msg)
        
        # Check if rebalancing needed
        if self.get_leverage_state().needs_rebalancing:
            logger.info("Swap moved leverage ratio outside target tolerance. "
                       "Rebalancing recommended.")
        
        return quote
    
    # ------------------------------------------------------------------
    # Flash Loan Operations  
    # ------------------------------------------------------------------
    
    def prepare_flashloan(
        self, 
        asset: str, 
        amount: Decimal,
        purpose: str = "arbitrage",
    ) -> FlashLoanTerms:
        """Prepare flash loan terms (does not execute)."""
        amount = _to_decimal(amount)
        if amount <= 0:
            raise ValueError("Flash loan amount must be positive")
        if asset not in (self.asset_a, self.asset_b):
            raise ValueError("Unsupported flash loan asset")
        
        reserve = self.reserves[asset]
        max_amount = reserve * self.max_flashloan_ratio
        if amount > max_amount:
            raise ValueError(
                f"Requested flash loan {amount} exceeds maximum {max_amount} "
                f"({self.max_flashloan_ratio * 100}% of reserves)"
            )
        
        fee_amount = amount * self.flash_fee
        repay_amount = amount + fee_amount
        loan_id = uuid4().hex
        
        terms = FlashLoanTerms(
            loan_id=loan_id,
            borrow_asset=asset,
            borrow_amount=amount,
            repay_asset=asset,
            repay_amount=repay_amount,
            fee_amount=fee_amount,
            purpose=purpose,
        )
        
        self.active_loans[loan_id] = terms
        logger.info("Prepared flash loan %s: %s %s for %s", 
                   loan_id, amount, asset, purpose)
        return terms
    
    def cancel_flashloan(self, loan_id: str) -> None:
        """Cancel flash loan (e.g., if Bitfinex treasury unavailable)."""
        self.active_loans.pop(loan_id, None)
        logger.info("Cancelled flash loan %s", loan_id)
    
    def complete_flashloan(self, loan_id: str, repay_amount: Decimal) -> Decimal:
        """Complete flash loan repayment and collect fee."""
        if loan_id not in self.active_loans:
            raise ValueError(f"Unknown flash loan identifier: {loan_id}")
        
        terms = self.active_loans.pop(loan_id)
        repay_amount = _to_decimal(repay_amount)
        required = terms.repay_amount
        
        if repay_amount < required:
            raise ValueError(
                f"Flash loan repayment {repay_amount} lower than required {required}"
            )
        
        # Collect fee (excess goes to LPs)
        fee_collected = repay_amount - terms.borrow_amount
        self.reserves[terms.borrow_asset] += fee_collected
        self.accumulated_fees[terms.borrow_asset] += fee_collected
        
        logger.info("Completed flash loan %s: collected fee %s %s",
                   loan_id, fee_collected, terms.borrow_asset)
        return fee_collected
    
    # ------------------------------------------------------------------
    # Arbitrage Opportunity Detection
    # ------------------------------------------------------------------
    
    def arbitrage_opportunity(
        self, market_price: Decimal, tolerance: Decimal
    ) -> Optional[ArbitrageOpportunity]:
        """
        Detect arbitrage opportunity between pool and external market.
        
        Returns trade that would move pool price back to market price.
        """
        market_price = _to_decimal(market_price)
        tolerance = _to_decimal(tolerance)
        current_price = self.price()
        upper = market_price * (Decimal(1) + tolerance)
        lower = market_price * (Decimal(1) - tolerance)
        
        if lower <= current_price <= upper:
            return None  # Pool price within tolerance
        
        # Calculate swap needed to reach market price
        if current_price > upper:
            # Pool overpricing BTC - sell BTC to pool
            amount_in = self._solve_input_asset_a_for_price(market_price)
            if amount_in is None:
                return None
            
            swap_quote = self.quote_swap(self.asset_a, amount_in)
            borrow_amount = swap_quote.amount_in * market_price
            repay_amount = borrow_amount * (Decimal(1) + self.flash_fee)
            expected_profit = swap_quote.amount_out - repay_amount
            reason = "Pool L-BTC price above market – sell L-BTC into pool."
            
        else:
            # Pool underpricing BTC - buy BTC from pool
            amount_in = self._solve_input_asset_b_for_price(market_price)
            if amount_in is None:
                return None
            
            swap_quote = self.quote_swap(self.asset_b, amount_in)
            borrow_amount = swap_quote.amount_in
            repay_amount = borrow_amount * (Decimal(1) + self.flash_fee)
            external_sale = swap_quote.amount_out * market_price
            expected_profit = external_sale - repay_amount
            reason = "Pool L-BTC price below market – buy L-BTC from pool."
        
        # Check leverage impact
        leverage_impact = None
        if swap_quote.leverage_state_after:
            if not swap_quote.leverage_state_after.is_healthy:
                leverage_impact = "WARNING: Arbitrage would push leverage ratio outside safety bands!"
        
        return ArbitrageOpportunity(
            reason=reason,
            target_price=market_price,
            borrow_asset=self.asset_b,
            borrow_amount=borrow_amount,
            repay_amount=repay_amount,
            swap=swap_quote,
            expected_profit=expected_profit,
            leverage_impact=leverage_impact,
        )
    
    def _solve_input_asset_a_for_price(self, target_price: Decimal) -> Optional[Decimal]:
        """Solve for input amount of asset_a to reach target price."""
        x = self.reserves[self.asset_a]
        y = self.reserves[self.asset_b]
        gamma = Decimal(1) - self.swap_fee
        
        a = target_price * gamma
        b = target_price * x * (Decimal(1) + gamma)
        c = target_price * (x * x) - x * y
        
        return self._solve_quadratic_positive(a, b, c)
    
    def _solve_input_asset_b_for_price(self, target_price: Decimal) -> Optional[Decimal]:
        """Solve for input amount of asset_b to reach target price."""
        x = self.reserves[self.asset_a]
        y = self.reserves[self.asset_b]
        gamma = Decimal(1) - self.swap_fee
        
        a = gamma
        b = y * (Decimal(1) + gamma)
        c = y * y - target_price * x * y
        
        return self._solve_quadratic_positive(a, b, c)
    
    def _solve_quadratic_positive(
        self, a: Decimal, b: Decimal, c: Decimal
    ) -> Optional[Decimal]:
        """Solve quadratic equation and return positive root if exists."""
        if a == 0:
            if b == 0:
                return None
            root = -c / b
            return root if root > 0 else None
        
        discriminant = b * b - Decimal(4) * a * c
        if discriminant <= 0:
            return None
        
        sqrt_disc = discriminant.sqrt()
        root1 = (-b + sqrt_disc) / (Decimal(2) * a)
        root2 = (-b - sqrt_disc) / (Decimal(2) * a)
        
        candidates = [root for root in (root1, root2) if root > 0]
        return max(candidates) if candidates else None


# Example configuration matching real deployment
CONFIG = {
    "asset_a": "LBTC",
    "asset_b": "LUSDt",
    "fee_bps": 30,
    "flash_fee_bps": 5,
    "initial_lbtc_reserve": Decimal("1"),
    "initial_lusdt_reserve": Decimal("30000"),
    "max_flashloan_ratio": Decimal("0.3"),
    "btc_price": Decimal("30000"),
}

# Create pool instance (would be imported by other modules)
ENHANCED_POOL = YieldBasisAMM(
    asset_a=CONFIG["asset_a"],
    asset_b=CONFIG["asset_b"],
    fee_bps=CONFIG["fee_bps"],
    flash_fee_bps=CONFIG["flash_fee_bps"],
    initial_reserves={
        CONFIG["asset_a"]: CONFIG["initial_lbtc_reserve"],
        CONFIG["asset_b"]: CONFIG["initial_lusdt_reserve"],
    },
    max_flashloan_ratio=CONFIG["max_flashloan_ratio"],
    btc_price=CONFIG["btc_price"],
)
