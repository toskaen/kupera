"""
Bitmatrix-style constant product AMM with YieldBasis leverage tracking.

YIELDBASIS MECHANISM (from Ethereum/Curve implementation):
1. Users deposit BTC → receive ybBTC receipt tokens
2. Protocol borrows equal USD value against BTC (creates debt)
3. BTC + borrowed USD both go into AMM pool
4. Result: 2x BTC exposure (original BTC + borrowed USD buys more BTC exposure)
5. LEVAMM maintains 50% debt-to-value ratio via arbitrage
6. When BTC rises: debt ratio falls → arbitrageurs add more debt → restore 2x
7. When BTC falls: debt ratio rises → arbitrageurs remove debt → restore 2x
8. LP earns trading fees on 2x position, IL-free due to leverage math

This implementation replicates YB on Liquid with Elements covenants.
"""

from decimal import Decimal, getcontext
from dataclasses import dataclass
from typing import Dict, Optional, Tuple, List
from uuid import uuid4
import requests
from datetime import datetime

getcontext().prec = 28


def get_live_btc_price() -> Decimal:
    """Fetch current BTC price from CoinGecko API."""
    try:
        response = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "bitcoin", "vs_currencies": "usd"},
            timeout=5
        )
        response.raise_for_status()
        price = response.json()["bitcoin"]["usd"]
        print(f"📊 Live BTC Price: ${price:,.2f}")
        return Decimal(str(price))
    except Exception as e:
        print(f"⚠️  Failed to fetch live price: {e}")
        print("📊 Using fallback price: $97,000")
        return Decimal("97000")


@dataclass
class PoolState:
    """Complete pool state at any point in time."""
    lbtc_reserve: Decimal
    lusdt_reserve: Decimal
    debt_amount: Decimal  # Borrowed USDT for 2x leverage
    btc_price: Decimal    # Oracle price
    lp_supply: Decimal    # Total LP tokens issued
    yb_supply: Decimal    # ybBTC tokens (receipt tokens for YB positions)
    
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
    def rebalance_signal(self) -> Optional[str]:
        """Signal for rebalancing (YieldBasis mechanism)."""
        target = Decimal("0.50")
        deviation = abs(self.debt_ratio - target)
        
        if deviation > Decimal("0.05"):  # >5% deviation from target
            if self.debt_ratio > target:
                # Too much debt → need to REMOVE debt (repay)
                return f"REMOVE_DEBT: Ratio {self.debt_ratio:.4f} > {target:.4f}. Need to repay ${(self.debt_ratio - target) * self.pool_value_usd:.2f}"
            else:
                # Too little debt → need to ADD debt (borrow more)
                return f"ADD_DEBT: Ratio {self.debt_ratio:.4f} < {target:.4f}. Need to borrow ${(target - self.debt_ratio) * self.pool_value_usd:.2f}"
        return None
    
    def to_dict(self) -> Dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "lbtc_reserve": str(self.lbtc_reserve),
            "lusdt_reserve": str(self.lusdt_reserve),
            "debt_amount": str(self.debt_amount),
            "btc_price": str(self.btc_price),
            "lp_supply": str(self.lp_supply),
            "yb_supply": str(self.yb_supply),
            "pool_value_usd": str(self.pool_value_usd),
            "debt_ratio": str(self.debt_ratio),
            "leverage_multiplier": str(self.leverage_multiplier),
            "is_healthy": self.is_healthy,
            "rebalance_signal": self.rebalance_signal,
        }


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
    
    def to_payload(self) -> Dict:
        """Convert to JSON-serializable payload."""
        return {
            "input_asset": self.input_asset,
            "output_asset": self.output_asset,
            "amount_in": str(self.amount_in),
            "amount_out": str(self.amount_out),
            "fee_paid": str(self.fee_paid),
            "price_after": str(self.price_after),
        }


@dataclass
class FlashLoanTerms:
    """Terms for a flash loan (used for rebalancing)."""
    loan_id: str
    borrow_asset: str
    borrow_amount: Decimal
    repay_amount: Decimal
    repay_asset: str
    fee_amount: Decimal
    purpose: str = "rebalancing"  # YB uses flash loans for rebalancing
    
    def to_payload(self) -> Dict:
        """Convert to JSON-serializable payload."""
        return {
            "loan_id": self.loan_id,
            "borrow_asset": self.borrow_asset,
            "borrow_amount": str(self.borrow_amount),
            "repay_amount": str(self.repay_amount),
            "repay_asset": self.repay_asset,
            "fee_amount": str(self.fee_amount),
            "purpose": self.purpose,
        }


@dataclass
class ArbitrageOpportunity:
    """Detected arbitrage opportunity for rebalancing."""
    action: str  # "add_debt" or "remove_debt"
    current_ratio: Decimal
    target_ratio: Decimal
    debt_adjustment: Decimal  # Amount of USDT to add/remove
    expected_profit: Decimal
    method: str  # "flash_loan" or "direct_deposit"
    
    def to_summary(self) -> Dict:
        """Convert to summary dict."""
        return {
            "action": self.action,
            "current_ratio": f"{self.current_ratio * 100:.2f}%",
            "target_ratio": f"{self.target_ratio * 100:.2f}%",
            "debt_adjustment": f"${self.debt_adjustment:,.2f}",
            "expected_profit": f"${self.expected_profit:,.2f}",
            "method": self.method,
        }


class YieldBasisAMM:
    """
    YieldBasis AMM on Liquid - Impermanent Loss Free BTC Yield.
    
    Core mechanism:
    1. Users deposit BTC → get ybBTC tokens (receipt tokens)
    2. Protocol borrows equal USD value (creates debt at 50% ratio)
    3. BTC + borrowed USD both enter AMM pool
    4. Result: 2x BTC exposure from single BTC deposit
    5. Leverage math: (√p)² becomes p, eliminating IL
    6. Permissionless arbitrage maintains 50% debt ratio:
       - BTC rises → debt ratio falls → add more debt
       - BTC falls → debt ratio rises → remove debt
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
        Initialize YieldBasis pool.
        
        Args:
            initial_lbtc: Initial BTC liquidity
            initial_lusdt: Initial USDT liquidity
            btc_price: Current BTC/USD price (from oracle)
            swap_fee_bps: Trading fee in basis points (0.30% = 30)
            flash_fee_bps: Flash loan fee in basis points (0.05% = 5)
        """
        self.swap_fee = Decimal(swap_fee_bps) / Decimal(10_000)
        self.flash_fee = Decimal(flash_fee_bps) / Decimal(10_000)
        
        # AMM reserves
        self.lbtc_reserve = Decimal(initial_lbtc)
        self.lusdt_reserve = Decimal(initial_lusdt)
        
        # Convenience dict
        self.reserves = {
            "LBTC": self.lbtc_reserve,
            "LUSDt": self.lusdt_reserve,
        }
        
        # YieldBasis debt tracking (borrowed USD for 2x leverage)
        initial_value = (self.lbtc_reserve * Decimal(btc_price)) + self.lusdt_reserve
        self.debt_amount = initial_value * Decimal("0.5")  # 50% ratio = 2x leverage
        
        # Price oracle
        self.btc_price = Decimal(btc_price)
        
        # Standard AMM LP tokens (for liquidity providers)
        initial_k = (self.lbtc_reserve * self.lusdt_reserve).sqrt()
        self.lp_supply = initial_k
        
        # ybBTC tokens (YieldBasis receipt tokens)
        # These represent leveraged BTC positions
        self.yb_supply = self.lbtc_reserve  # 1:1 with deposited BTC initially
        self.yb_holders: Dict[str, Decimal] = {}  # address -> ybBTC balance
        
        # Fee accumulation
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
            yb_supply=self.yb_supply,
        )
    
    def get_leverage_state(self) -> PoolState:
        """Alias for get_state() for API compatibility."""
        return self.get_state()
    
    def invariant(self) -> Decimal:
        """Constant product invariant k = x * y."""
        return self.lbtc_reserve * self.lusdt_reserve
    
    def price(self) -> Decimal:
        """Current pool price (USDT per BTC)."""
        if self.lbtc_reserve == 0:
            return Decimal(0)
        return self.lusdt_reserve / self.lbtc_reserve
    
    def snapshot(self) -> Dict[str, Decimal]:
        """Get current reserves snapshot."""
        return {
            "LBTC": self.lbtc_reserve,
            "LUSDt": self.lusdt_reserve,
        }
    
    # ========================================================================
    # YIELDBASIS CORE: ybBTC TOKEN OPERATIONS
    # ========================================================================
    
    def deposit_btc_for_yb(self, btc_amount: Decimal, depositor: str = "user") -> Decimal:
        """
        YieldBasis deposit: User deposits BTC, gets ybBTC tokens.
        
        Process:
        1. User deposits BTC
        2. Protocol borrows equal USD value
        3. Both BTC + borrowed USD go into pool
        4. User gets ybBTC tokens (receipt for 2x leveraged position)
        
        Returns: ybBTC tokens minted
        """
        btc_amount = Decimal(btc_amount)
        if btc_amount <= 0:
            raise ValueError("BTC amount must be positive")
        
        # Calculate USD value to borrow (equal to BTC deposited)
        usd_to_borrow = btc_amount * self.btc_price
        
        # Add BTC to reserves
        self.lbtc_reserve += btc_amount
        
        # Borrow USD (increases debt)
        self.debt_amount += usd_to_borrow
        
        # Add borrowed USD to reserves
        self.lusdt_reserve += usd_to_borrow
        
        # Mint ybBTC tokens (1:1 with deposited BTC)
        yb_tokens = btc_amount
        self.yb_supply += yb_tokens
        self.yb_holders[depositor] = self.yb_holders.get(depositor, Decimal(0)) + yb_tokens
        
        # Sync reserves dict
        self.reserves["LBTC"] = self.lbtc_reserve
        self.reserves["LUSDt"] = self.lusdt_reserve
        
        # Verify covenant health
        state = self.get_state()
        if not state.is_healthy:
            raise ValueError(f"Deposit violates covenant: ratio {state.debt_ratio:.4f}")
        
        return yb_tokens
    
    def withdraw_yb(self, yb_tokens: Decimal, holder: str = "user") -> Tuple[Decimal, Decimal]:
        """
        YieldBasis withdrawal: Burn ybBTC tokens, get BTC + proportional fees.
        
        Returns: (btc_amount, profit_usd)
        """
        yb_tokens = Decimal(yb_tokens)
        
        if yb_tokens <= 0:
            raise ValueError("ybBTC amount must be positive")
        
        if self.yb_holders.get(holder, Decimal(0)) < yb_tokens:
            raise ValueError("Insufficient ybBTC balance")
        
        # Calculate share of pool
        share = yb_tokens / self.yb_supply
        
        # Get proportional BTC
        btc_out = self.lbtc_reserve * share
        
        # Calculate profit (pool value increased due to fees)
        usd_value_out = self.lusdt_reserve * share
        initial_deposit_value = yb_tokens * self.btc_price
        profit = usd_value_out - initial_deposit_value
        
        # Burn ybBTC tokens
        self.yb_supply -= yb_tokens
        self.yb_holders[holder] -= yb_tokens
        
        # Remove from reserves
        self.lbtc_reserve -= btc_out
        self.lusdt_reserve -= usd_value_out
        
        # Repay proportional debt
        debt_repay = self.debt_amount * share
        self.debt_amount -= debt_repay
        
        # Sync reserves dict
        self.reserves["LBTC"] = self.lbtc_reserve
        self.reserves["LUSDt"] = self.lusdt_reserve
        
        return (btc_out, profit)
    
    # ========================================================================
    # SWAP OPERATIONS (Standard AMM)
    # ========================================================================
    
    def quote_swap(self, input_asset: str, amount_in: Decimal) -> SwapQuote:
        """Quote a swap without executing."""
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
        
        # Constant product: (x + Δx)(y - Δy) = xy
        amount_out = (reserve_out * amount_in_after_fee) / (reserve_in + amount_in_after_fee)
        
        if amount_out <= 0:
            raise ValueError("Output amount would be zero")
        
        # Calculate new state
        if input_asset == "LBTC":
            new_lbtc = reserve_in + amount_in
            new_lusdt = reserve_out - amount_out
        else:
            new_lbtc = reserve_out - amount_out
            new_lusdt = reserve_in + amount_in
        
        new_price = new_lusdt / new_lbtc if new_lbtc > 0 else Decimal(0)
        
        new_state = PoolState(
            lbtc_reserve=new_lbtc,
            lusdt_reserve=new_lusdt,
            debt_amount=self.debt_amount,  # Swaps don't change debt
            btc_price=self.btc_price,
            lp_supply=self.lp_supply,
            yb_supply=self.yb_supply,
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
        """Execute a swap (mutates pool state)."""
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
        
        # Sync reserves dict
        self.reserves["LBTC"] = self.lbtc_reserve
        self.reserves["LUSDt"] = self.lusdt_reserve
        
        return quote
    
    # ========================================================================
    # STANDARD LP OPERATIONS (for regular AMM LPs, not YB users)
    # ========================================================================
    
    def add_liquidity(self, lbtc_amount: Decimal, lusdt_amount: Decimal) -> Decimal:
        """Add liquidity to pool (standard AMM LP, not YB)."""
        lbtc_amount = Decimal(lbtc_amount)
        lusdt_amount = Decimal(lusdt_amount)
        
        if lbtc_amount <= 0 or lusdt_amount <= 0:
            raise ValueError("Amounts must be positive")
        
        if self.lp_supply == 0:
            lp_tokens = (lbtc_amount * lusdt_amount).sqrt()
        else:
            lbtc_ratio = lbtc_amount / self.lbtc_reserve
            lusdt_ratio = lusdt_amount / self.lusdt_reserve
            ratio = min(lbtc_ratio, lusdt_ratio)
            lp_tokens = self.lp_supply * ratio
        
        self.lbtc_reserve += lbtc_amount
        self.lusdt_reserve += lusdt_amount
        self.lp_supply += lp_tokens
        
        self.reserves["LBTC"] = self.lbtc_reserve
        self.reserves["LUSDt"] = self.lusdt_reserve
        
        return lp_tokens
    
    def remove_liquidity(self, lp_tokens: Decimal) -> Tuple[Decimal, Decimal]:
        """Remove liquidity from pool."""
        lp_tokens = Decimal(lp_tokens)
        
        if lp_tokens <= 0 or lp_tokens > self.lp_supply:
            raise ValueError("Invalid LP token amount")
        
        share = lp_tokens / self.lp_supply
        lbtc_out = self.lbtc_reserve * share
        lusdt_out = self.lusdt_reserve * share
        
        self.lbtc_reserve -= lbtc_out
        self.lusdt_reserve -= lusdt_out
        self.lp_supply -= lp_tokens
        
        self.reserves["LBTC"] = self.lbtc_reserve
        self.reserves["LUSDt"] = self.lusdt_reserve
        
        return (lbtc_out, lusdt_out)
    
    # ========================================================================
    # FLASH LOANS (for permissionless rebalancing)
    # ========================================================================
    
    def prepare_flashloan(self, asset: str, amount: Decimal, purpose: str = "rebalancing") -> FlashLoanTerms:
        """Prepare flash loan for rebalancing operations."""
        amount = Decimal(amount)
        
        if amount <= 0:
            raise ValueError("Amount must be positive")
        
        if asset not in ("LBTC", "LUSDt"):
            raise ValueError(f"Unsupported asset: {asset}")
        
        reserve = self.lbtc_reserve if asset == "LBTC" else self.lusdt_reserve
        max_loan = reserve * Decimal("0.3")
        
        if amount > max_loan:
            raise ValueError(f"Loan {amount} exceeds max {max_loan}")
        
        fee = amount * self.flash_fee
        repay = amount + fee
        
        loan_id = uuid4().hex
        terms = FlashLoanTerms(
            loan_id=loan_id,
            borrow_asset=asset,
            borrow_amount=amount,
            repay_amount=repay,
            repay_asset=asset,
            fee_amount=fee,
            purpose=purpose,
        )
        
        self.active_loans[loan_id] = terms
        return terms
    
    def complete_flashloan(self, loan_id: str, repaid: Decimal) -> Decimal:
        """Complete flash loan."""
        if loan_id not in self.active_loans:
            raise ValueError(f"Unknown loan: {loan_id}")
        
        terms = self.active_loans.pop(loan_id)
        repaid = Decimal(repaid)
        
        if repaid < terms.repay_amount:
            raise ValueError(f"Insufficient repayment: {repaid} < {terms.repay_amount}")
        
        asset = terms.borrow_asset
        self.accumulated_fees[asset] += terms.fee_amount
        
        return terms.fee_amount
    
    def cancel_flashloan(self, loan_id: str):
        """Cancel flash loan if not executed."""
        self.active_loans.pop(loan_id, None)
    
    # ========================================================================
    # YIELDBASIS REBALANCING (LEVAMM logic)
    # ========================================================================
    
    def detect_rebalance_opportunity(self) -> Optional[ArbitrageOpportunity]:
        """
        Detect if rebalancing is profitable.
        
        YieldBasis mechanism:
        - Target: 50% debt ratio (2x leverage)
        - When BTC rises: debt ratio falls → add more debt
        - When BTC falls: debt ratio rises → remove debt
        """
        state = self.get_state()
        target_ratio = Decimal("0.50")
        current_ratio = state.debt_ratio
        
        deviation = abs(current_ratio - target_ratio)
        
        # Need >5% deviation for profitable arbitrage
        if deviation <= Decimal("0.05"):
            return None
        
        # Calculate debt adjustment needed
        target_debt = state.pool_value_usd * target_ratio
        debt_adjustment = abs(target_debt - state.debt_amount)
        
        # Estimate profit (simplified: 0.5% of adjustment)
        expected_profit = debt_adjustment * Decimal("0.005")
        
        if current_ratio < target_ratio:
            # Need to ADD debt (borrow more USDT)
            return ArbitrageOpportunity(
                action="add_debt",
                current_ratio=current_ratio,
                target_ratio=target_ratio,
                debt_adjustment=debt_adjustment,
                expected_profit=expected_profit,
                method="flash_loan",
            )
        else:
            # Need to REMOVE debt (repay USDT)
            return ArbitrageOpportunity(
                action="remove_debt",
                current_ratio=current_ratio,
                target_ratio=target_ratio,
                debt_adjustment=debt_adjustment,
                expected_profit=expected_profit,
                method="flash_loan",
            )
    
    def rebalance_via_flashloan(self, adjustment: Decimal, action: str):
        """
        Execute rebalancing via flash loan.
        
        This simulates what an arbitrageur would do:
        1. Detect deviation from 50% ratio
        2. Borrow USD via flash loan
        3. Add/remove debt to restore ratio
        4. Profit from the rebalancing
        5. Repay flash loan + fee
        """
        adjustment = Decimal(adjustment)
        
        if action == "add_debt":
            # Borrow more USDT, add to pool
            self.debt_amount += adjustment
            self.lusdt_reserve += adjustment
        elif action == "remove_debt":
            # Remove USDT, repay debt
            if self.lusdt_reserve < adjustment:
                raise ValueError("Insufficient USDT to remove")
            self.debt_amount -= adjustment
            self.lusdt_reserve -= adjustment
        else:
            raise ValueError(f"Unknown action: {action}")
        
        # Sync reserves
        self.reserves["LUSDt"] = self.lusdt_reserve
        
        # Verify covenant
        state = self.get_state()
        if not state.is_healthy:
            raise ValueError(f"Rebalancing violates covenant: ratio {state.debt_ratio:.4f}")
    
    def arbitrage_opportunity(self, market_price: Decimal, tolerance: Decimal) -> Optional[ArbitrageOpportunity]:
        """Wrapper for detect_rebalance_opportunity for API compatibility."""
        return self.detect_rebalance_opportunity()
    
    def plan_flashloan_arbitrage(self, terms: FlashLoanTerms, market_price: Decimal, tolerance: Decimal) -> Dict:
        """Plan rebalancing arbitrage."""
        opportunity = self.detect_rebalance_opportunity()
        
        if opportunity is None:
            return {
                "swaps": [],
                "expected_profit": None,
                "notes": {"info": "No rebalancing needed"},
            }
        
        return {
            "swaps": [],  # Rebalancing is debt adjustment, not swaps
            "expected_profit": opportunity.expected_profit,
            "notes": {
                "action": opportunity.action,
                "adjustment": str(opportunity.debt_adjustment),
                "strategy": f"{opportunity.action} to restore 50% debt ratio",
            },
        }
    
    def update_price(self, new_price: Decimal):
        """Update BTC price from oracle."""
        old_state = self.get_state()
        self.btc_price = Decimal(new_price)
        new_state = self.get_state()
        
        return (old_state, new_state)
    
    def apply_simulated_pset(self, payload: Dict) -> Dict:
        """Apply simulated PSET transaction."""
        pset_type = payload.get("type")
        
        if pset_type == "swap":
            swap_data = payload["swap"]
            quote = self.execute_swap(
                swap_data["input_asset"],
                Decimal(swap_data["amount_in"])
            )
            return {
                "txid": f"swap_{uuid4().hex[:16]}",
                "amount_in": str(quote.amount_in),
                "amount_out": str(quote.amount_out),
                "fee_collected": str(quote.fee_paid),
            }
        
        elif pset_type == "flashloan":
            loan_data = payload["flashloan"]
            loan_id = loan_data["loan_id"]
            
            for swap_plan in payload.get("swaps", []):
                self.execute_swap(
                    swap_plan["input_asset"],
                    Decimal(swap_plan["amount_in"])
                )
            
            repay_amount = Decimal(loan_data["repay_amount"])
            fee = self.complete_flashloan(loan_id, repay_amount)
            
            return {
                "txid": f"flashloan_{uuid4().hex[:16]}",
                "loan_id": loan_id,
                "repay_amount": str(repay_amount),
                "fee_collected": str(fee),
            }
        
        else:
            raise ValueError(f"Unknown PSET type: {pset_type}")


# =============================================================================
# GLOBAL INSTANCES
# =============================================================================

# Get live BTC price
LIVE_BTC_PRICE = get_live_btc_price()

SIMULATED_POOL = YieldBasisAMM(
    initial_lbtc=Decimal("1.0"),
    initial_lusdt=LIVE_BTC_PRICE,  # Equal USD value for 50% ratio
    btc_price=LIVE_BTC_PRICE,
    swap_fee_bps=30,
    flash_fee_bps=5,
)

ENHANCED_POOL = SIMULATED_POOL  # Alias for flashloan.py


# =============================================================================
# EXAMPLE: YieldBasis mechanism with LIVE BTC price
# =============================================================================

def example_yieldbasis_mechanism():
    """Demonstrate YieldBasis IL-free yield with live BTC price."""
    print("\n" + "=" * 70)
    print("YIELDBASIS ON LIQUID - IL-FREE BTC YIELD DEMONSTRATION")
    print(f"Using LIVE BTC price: ${LIVE_BTC_PRICE:,.2f}")
    print("=" * 70)
    
    # Initialize pool
    pool = YieldBasisAMM(
        initial_lbtc=Decimal("10.0"),
        initial_lusdt=Decimal("10.0") * LIVE_BTC_PRICE,
        btc_price=LIVE_BTC_PRICE,
    )
    
    print("\n📝 STEP 1: Initial Pool State")
    state = pool.get_state()
    print(f"   BTC Reserve: {state.lbtc_reserve} BTC")
    print(f"   USDT Reserve: ${state.lusdt_reserve:,.2f}")
    print(f"   Debt: ${state.debt_amount:,.2f}")
    print(f"   Debt Ratio: {state.debt_ratio * 100:.2f}% (target: 50%)")
    print(f"   Leverage: {state.leverage_multiplier:.2f}x")
    print(f"   ybBTC Supply: {state.yb_supply} tokens")
    
    print("\n👤 STEP 2: User Deposits 1 BTC")
    yb_tokens = pool.deposit_btc_for_yb(Decimal("1.0"), "alice")
    print(f"   Alice receives: {yb_tokens} ybBTC tokens")
    print(f"   Protocol borrows: ${LIVE_BTC_PRICE:,.2f} USDT")
    print(f"   Total pool value: ${pool.get_state().pool_value_usd:,.2f}")
    
    print("\n📈 STEP 3: BTC Price Changes (simulate +10%)")
    new_price = LIVE_BTC_PRICE * Decimal("1.10")
    old_state, new_state = pool.update_price(new_price)
    print(f"   Old price: ${old_state.btc_price:,.2f}")
    print(f"   New price: ${new_state.btc_price:,.2f}")
    print(f"   Old ratio: {old_state.debt_ratio * 100:.2f}%")
    print(f"   New ratio: {new_state.debt_ratio * 100:.2f}%")
    print(f"   📊 {new_state.rebalance_signal}")
    
    print("\n⚡ STEP 4: Arbitrageur Rebalances")
    opportunity = pool.detect_rebalance_opportunity()
    if opportunity:
        print(f"   Action: {opportunity.action}")
        print(f"   Adjustment: {opportunity.to_summary()['debt_adjustment']}")
        print(f"   Expected profit: {opportunity.to_summary()['expected_profit']}")
        
        # Execute via flash loan
        print("\n   💰 Executing flash loan rebalance...")
        terms = pool.prepare_flashloan("LUSDt", opportunity.debt_adjustment)
        pool.rebalance_via_flashloan(opportunity.debt_adjustment, opportunity.action)
        fee = pool.complete_flashloan(terms.loan_id, terms.repay_amount)
        print(f"   ✅ Rebalanced! Fee collected: ${fee:,.2f}")
        
        final_state = pool.get_state()
        print(f"   Final ratio: {final_state.debt_ratio * 100:.2f}%")
        print(f"   Final leverage: {final_state.leverage_multiplier:.2f}x")
    
    print("\n" + "=" * 70)
    print("🎯 KEY INSIGHT: YieldBasis maintains 2x leverage automatically")
    print("   through permissionless arbitrage, achieving IL-free yield!")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    example_yieldbasis_mechanism()
