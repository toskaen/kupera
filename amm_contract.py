"""
Bitmatrix-style constant product AMM with YieldBasis leverage tracking.

CRITICAL CORRECTIONS:
1. Rebalancing is PERMISSIONLESS - done by external arbitrageurs for profit
2. Bitfinex seeds initial liquidity, then pool is open to all LPs
3. Pool exists perpetually once created (24/7 operation)
4. Leverage maintained by MARKET FORCES, not centralized service

This simulates the covenant logic that would run on-chain.
"""

from decimal import Decimal, getcontext
from dataclasses import dataclass
from typing import Dict, Optional, Tuple
from uuid import uuid4

getcontext().prec = 28


@dataclass
class PoolState:
    """Complete pool state at any point in time."""
    lbtc_reserve: Decimal
    lusdt_reserve: Decimal
    debt_amount: Decimal  # Borrowed USDT for leverage
    btc_price: Decimal    # Oracle price for leverage calculation
    lp_supply: Decimal    # Total LP tokens issued
    
    @property
    def pool_value_usd(self) -> Decimal:
        """Total pool value in USD."""
        return (self.lbtc_reserve * self.btc_price) + self.lusdt_reserve
    
    @property
    def debt_ratio(self) -> Decimal:
        """Current debt-to-value ratio (target: 0.50 for 2x leverage)."""
        if self.pool_value_usd == 0:
            return Decimal(0)
        return self.debt_amount / self.pool_value_usd
    
    @property
    def leverage_multiplier(self) -> Decimal:
        """Effective leverage (target: 2.0x)."""
        if self.debt_ratio >= Decimal("0.99"):
            return Decimal(999)
        return Decimal(1) / (Decimal(1) - self.debt_ratio)
    
    @property
    def is_healthy(self) -> bool:
        """Check if within covenant safety bands [6.25%, 53.125%]."""
        return Decimal("0.0625") <= self.debt_ratio <= Decimal("0.53125")
    
    @property
    def arbitrage_opportunity(self) -> Optional[str]:
        """Check if leverage ratio makes arbitrage profitable."""
        target = Decimal("0.50")
        deviation = abs(self.debt_ratio - target)
        
        if deviation > Decimal("0.05"):  # >5% deviation
            if self.debt_ratio > target:
                return f"ABOVE_TARGET: Ratio {self.debt_ratio:.4f} > {target:.4f}. Profitable to REPAY debt."
            else:
                return f"BELOW_TARGET: Ratio {self.debt_ratio:.4f} < {target:.4f}. Profitable to BORROW more."
        return None


@dataclass
class SwapQuote:
    """Quote for a swap operation."""
    input_asset: str
    output_asset: str
    amount_in: Decimal
    amount_out: Decimal
    fee_paid: Decimal
    price_after: Decimal
    new_state: PoolState


@dataclass
class FlashLoanTerms:
    """Terms for a flash loan (open to anyone)."""
    loan_id: str
    borrow_asset: str
    borrow_amount: Decimal
    repay_amount: Decimal
    fee_amount: Decimal


class YieldBasisAMM:
    """
    Constant product AMM with YieldBasis 2x leverage mechanism.
    
    Key principles:
    - Pool created ONCE, runs perpetually
    - Anyone can be an LP (Bitfinex just seeds initial capital)
    - Leverage maintained by PERMISSIONLESS arbitrage
    - Flash loans enable arbitrageurs to profit from rebalancing
    - Covenants enforce all rules on-chain
    """
    
    def __init__(
        self,
        initial_lbtc: Decimal,
        initial_lusdt: Decimal,
        btc_price: Decimal,
        swap_fee_bps: int = 30,
        flash_fee_bps: int = 5,
    ):
        """
        Initialize pool with seed liquidity.
        
        Args:
            initial_lbtc: Initial BTC liquidity (e.g., from Bitfinex)
            initial_lusdt: Initial USDT liquidity
            btc_price: Current BTC/USD price (from oracle)
            swap_fee_bps: Trading fee in basis points (0.30% = 30)
            flash_fee_bps: Flash loan fee in basis points (0.05% = 5)
        """
        self.swap_fee = Decimal(swap_fee_bps) / Decimal(10_000)
        self.flash_fee = Decimal(flash_fee_bps) / Decimal(10_000)
        
        # Initialize reserves
        self.lbtc_reserve = Decimal(initial_lbtc)
        self.lusdt_reserve = Decimal(initial_lusdt)
        
        # Calculate initial debt for 50% ratio (2x leverage)
        initial_value = (self.lbtc_reserve * Decimal(btc_price)) + self.lusdt_reserve
        self.debt_amount = initial_value * Decimal("0.5")
        
        # Price tracking
        self.btc_price = Decimal(btc_price)
        
        # LP tracking
        initial_k = (self.lbtc_reserve * self.lusdt_reserve).sqrt()
        self.lp_supply = initial_k
        
        # Fee accumulation (goes to LPs)
        self.accumulated_fees = {
            "LBTC": Decimal(0),
            "LUSDt": Decimal(0),
        }
        
        # Active flash loans
        self.active_loans: Dict[str, FlashLoanTerms] = {}
        
    def get_state(self) -> PoolState:
        """Get current pool state."""
        return PoolState(
            lbtc_reserve=self.lbtc_reserve,
            lusdt_reserve=self.lusdt_reserve,
            debt_amount=self.debt_amount,
            btc_price=self.btc_price,
            lp_supply=self.lp_supply,
        )
    
    def invariant(self) -> Decimal:
        """Constant product invariant k = x * y."""
        return self.lbtc_reserve * self.lusdt_reserve
    
    def price(self) -> Decimal:
        """Current pool price (USDT per BTC)."""
        if self.lbtc_reserve == 0:
            return Decimal(0)
        return self.lusdt_reserve / self.lbtc_reserve
    
    # ========================================================================
    # SWAP OPERATIONS (Standard AMM)
    # ========================================================================
    
    def quote_swap(self, input_asset: str, amount_in: Decimal) -> SwapQuote:
        """
        Quote a swap without executing.
        
        Standard constant product formula with fees.
        """
        amount_in = Decimal(amount_in)
        if amount_in <= 0:
            raise ValueError("Amount must be positive")
        
        if input_asset == "LBTC":
            reserve_in = self.lbtc_reserve
            reserve_out = self.lusdt_reserve
            output_asset = "LUSDt"
        elif input_asset == "LUSDt":
            reserve_in = self.lusdt_reserve
            reserve_out = self.lbtc_reserve
            output_asset = "LBTC"
        else:
            raise ValueError(f"Unknown asset: {input_asset}")
        
        if reserve_in <= 0 or reserve_out <= 0:
            raise ValueError("Pool has zero reserves")
        
        # Apply swap fee
        amount_in_after_fee = amount_in * (Decimal(1) - self.swap_fee)
        fee_paid = amount_in * self.swap_fee
        
        # Constant product formula: (x + Δx)(y - Δy) = xy
        # Solving for Δy: Δy = (y * Δx) / (x + Δx)
        amount_out = (reserve_out * amount_in_after_fee) / (reserve_in + amount_in_after_fee)
        
        if amount_out <= 0:
            raise ValueError("Output amount would be zero")
        
        # Calculate new reserves
        if input_asset == "LBTC":
            new_lbtc = reserve_in + amount_in
            new_lusdt = reserve_out - amount_out
        else:
            new_lbtc = reserve_out - amount_out
            new_lusdt = reserve_in + amount_in
        
        new_price = new_lusdt / new_lbtc if new_lbtc > 0 else Decimal(0)
        
        # Calculate new pool state (debt unchanged by swaps)
        new_state = PoolState(
            lbtc_reserve=new_lbtc,
            lusdt_reserve=new_lusdt,
            debt_amount=self.debt_amount,  # Unchanged
            btc_price=self.btc_price,
            lp_supply=self.lp_supply,
        )
        
        return SwapQuote(
            input_asset=input_asset,
            output_asset=output_asset,
            amount_in=amount_in,
            amount_out=amount_out,
            fee_paid=fee_paid,
            price_after=new_price,
            new_state=new_state,
        )
    
    def execute_swap(self, input_asset: str, amount_in: Decimal) -> SwapQuote:
        """
        Execute a swap (mutates pool state).
        
        This would be enforced by AMM covenant on-chain.
        """
        quote = self.quote_swap(input_asset, amount_in)
        
        # Update reserves
        if input_asset == "LBTC":
            self.lbtc_reserve += quote.amount_in
            self.lusdt_reserve -= quote.amount_out
            self.accumulated_fees["LBTC"] += quote.fee_paid
        else:
            self.lusdt_reserve += quote.amount_in
            self.lbtc_reserve -= quote.amount_out
            self.accumulated_fees["LUSDt"] += quote.fee_paid
        
        # Verify covenant: new_k >= old_k (due to fees)
        new_k = self.invariant()
        # In production, covenant would reject if this fails
        
        return quote
    
    # ========================================================================
    # LIQUIDITY OPERATIONS (Anyone can be an LP)
    # ========================================================================
    
    def add_liquidity(
        self, 
        lbtc_amount: Decimal, 
        lusdt_amount: Decimal
    ) -> Decimal:
        """
        Add liquidity to pool (open to anyone, not just Bitfinex).
        
        Returns: LP tokens minted
        """
        lbtc_amount = Decimal(lbtc_amount)
        lusdt_amount = Decimal(lusdt_amount)
        
        if lbtc_amount <= 0 or lusdt_amount <= 0:
            raise ValueError("Amounts must be positive")
        
        # For first LP, use geometric mean
        if self.lp_supply == 0:
            lp_tokens = (lbtc_amount * lusdt_amount).sqrt()
        else:
            # Proportional to existing pool
            lbtc_ratio = lbtc_amount / self.lbtc_reserve
            lusdt_ratio = lusdt_amount / self.lusdt_reserve
            
            # Use minimum ratio to prevent manipulation
            ratio = min(lbtc_ratio, lusdt_ratio)
            lp_tokens = self.lp_supply * ratio
        
        # Update pool
        self.lbtc_reserve += lbtc_amount
        self.lusdt_reserve += lusdt_amount
        self.lp_supply += lp_tokens
        
        return lp_tokens
    
    def remove_liquidity(self, lp_tokens: Decimal) -> Tuple[Decimal, Decimal]:
        """
        Remove liquidity from pool.
        
        Returns: (lbtc_amount, lusdt_amount)
        """
        lp_tokens = Decimal(lp_tokens)
        
        if lp_tokens <= 0 or lp_tokens > self.lp_supply:
            raise ValueError("Invalid LP token amount")
        
        # Pro-rata share
        share = lp_tokens / self.lp_supply
        lbtc_out = self.lbtc_reserve * share
        lusdt_out = self.lusdt_reserve * share
        
        # Update pool
        self.lbtc_reserve -= lbtc_out
        self.lusdt_reserve -= lusdt_out
        self.lp_supply -= lp_tokens
        
        return (lbtc_out, lusdt_out)
    
    # ========================================================================
    # FLASH LOAN OPERATIONS (Enables permissionless rebalancing)
    # ========================================================================
    
    def prepare_flashloan(self, asset: str, amount: Decimal) -> FlashLoanTerms:
        """
        Prepare flash loan terms (anyone can request).
        
        Flash loans enable arbitrageurs to:
        1. Borrow USDT when leverage below target
        2. Add USDT to pool (adjusts debt)
        3. Profit from price arbitrage
        4. Repay loan + fee
        
        All atomic in single transaction via PSET.
        """
        amount = Decimal(amount)
        
        if amount <= 0:
            raise ValueError("Amount must be positive")
        
        if asset not in ("LBTC", "LUSDt"):
            raise ValueError(f"Unsupported asset: {asset}")
        
        # Check pool has liquidity
        reserve = self.lbtc_reserve if asset == "LBTC" else self.lusdt_reserve
        max_loan = reserve * Decimal("0.3")  # Max 30% of reserves
        
        if amount > max_loan:
            raise ValueError(f"Loan {amount} exceeds max {max_loan}")
        
        # Calculate repayment
        fee = amount * self.flash_fee
        repay = amount + fee
        
        loan_id = uuid4().hex
        terms = FlashLoanTerms(
            loan_id=loan_id,
            borrow_asset=asset,
            borrow_amount=amount,
            repay_amount=repay,
            fee_amount=fee,
        )
        
        self.active_loans[loan_id] = terms
        return terms
    
    def complete_flashloan(self, loan_id: str, repaid: Decimal) -> Decimal:
        """
        Complete flash loan (covenant enforces repayment).
        
        Returns: Fee collected (goes to LPs)
        """
        if loan_id not in self.active_loans:
            raise ValueError(f"Unknown loan: {loan_id}")
        
        terms = self.active_loans.pop(loan_id)
        repaid = Decimal(repaid)
        
        if repaid < terms.repay_amount:
            raise ValueError(
                f"Insufficient repayment: {repaid} < {terms.repay_amount}"
            )
        
        # Fee goes to pool (benefits LPs)
        asset = terms.borrow_asset
        self.accumulated_fees[asset] += terms.fee_amount
        
        return terms.fee_amount
    
    def cancel_flashloan(self, loan_id: str):
        """Cancel flash loan if not executed."""
        self.active_loans.pop(loan_id, None)
    
    # ========================================================================
    # LEVERAGE ADJUSTMENT (Via flash loan arbitrage)
    # ========================================================================
    
    def adjust_debt(self, adjustment: Decimal, direction: str):
        """
        Adjust debt amount (simulates arbitrageur action).
        
        In production:
        - Arbitrageur borrows via flash loan
        - Adds/removes USDT to adjust ratio
        - Profits from bringing ratio back to target
        - Repays flash loan
        
        Args:
            adjustment: Amount of USDT to adjust
            direction: "borrow" (increase debt) or "repay" (decrease debt)
        """
        adjustment = Decimal(adjustment)
        
        if direction == "borrow":
            # Arbitrageur borrows more USDT, adds to pool
            self.debt_amount += adjustment
            self.lusdt_reserve += adjustment
        elif direction == "repay":
            # Arbitrageur removes USDT, repays debt
            if self.lusdt_reserve < adjustment:
                raise ValueError("Insufficient USDT to repay")
            self.debt_amount -= adjustment
            self.lusdt_reserve -= adjustment
        else:
            raise ValueError(f"Unknown direction: {direction}")
        
        # Verify covenant after adjustment
        state = self.get_state()
        if not state.is_healthy:
            raise ValueError(
                f"Adjustment violates covenant: ratio {state.debt_ratio:.4f}"
            )
    
    def update_price(self, new_price: Decimal):
        """
        Update BTC price from oracle.
        
        Price changes create arbitrage opportunities:
        - Price UP → debt ratio falls → profit from borrowing more
        - Price DOWN → debt ratio rises → profit from repaying debt
        """
        old_state = self.get_state()
        self.btc_price = Decimal(new_price)
        new_state = self.get_state()
        
        return (old_state, new_state)


# =============================================================================
# EXAMPLE: How permissionless rebalancing works
# =============================================================================

def example_arbitrage_rebalancing():
    """
    Example showing how MARKET maintains leverage, not centralized service.
    """
    print("=" * 70)
    print("EXAMPLE: Permissionless Leverage Maintenance via Arbitrage")
    print("=" * 70)
    
    # 1. Pool initialized with seed liquidity (e.g., Bitfinex provides initial)
    pool = YieldBasisAMM(
        initial_lbtc=Decimal("1.0"),
        initial_lusdt=Decimal("30000"),
        btc_price=Decimal("30000"),
    )
    
    print("\n1. INITIAL STATE:")
    state = pool.get_state()
    print(f"   Reserves: {state.lbtc_reserve} BTC, ${state.lusdt_reserve} USDT")
    print(f"   Debt: ${state.debt_amount}")
    print(f"   Ratio: {state.debt_ratio * 100:.2f}% (target: 50.00%)")
    print(f"   Leverage: {state.leverage_multiplier:.2f}x (target: 2.00x)")
    
    # 2. BTC price increases → debt ratio falls
    print("\n2. BTC PRICE INCREASES TO $35,000:")
    old_state, new_state = pool.update_price(Decimal("35000"))
    print(f"   Old ratio: {old_state.debt_ratio * 100:.2f}%")
    print(f"   New ratio: {new_state.debt_ratio * 100:.2f}%")
    print(f"   Arbitrage opportunity: {new_state.arbitrage_opportunity}")
    
    # 3. Arbitrageur profits by restoring target ratio
    print("\n3. ARBITRAGEUR REBALANCES (via flash loan):")
    print("   Step 1: Borrow $5000 USDT via flash loan")
    terms = pool.prepare_flashloan("LUSDt", Decimal("5000"))
    print(f"   Step 2: Add USDT to pool, adjust debt")
    pool.adjust_debt(Decimal("5000"), "borrow")
    print(f"   Step 3: Repay flash loan + {pool.flash_fee * 100}% fee")
    fee = pool.complete_flashloan(terms.loan_id, terms.repay_amount)
    print(f"   Step 4: Arbitrageur profits, LPs earn ${fee} fee")
    
    final_state = pool.get_state()
    print(f"\n   Final ratio: {final_state.debt_ratio * 100:.2f}%")
    print(f"   Final leverage: {final_state.leverage_multiplier:.2f}x")
    
    print("\n" + "=" * 70)
    print("KEY INSIGHT: No centralized rebalancer needed!")
    print("Market participants profit from maintaining correct leverage ratio.")
    print("=" * 70)


if __name__ == "__main__":
    example_arbitrage_rebalancing()
