"""Bitmatrix-inspired AMM simulation with flash-loan aware arbitrage helpers.

This module does not attempt to reproduce the covenant script that would run on
Liquid.  Instead it provides an **executable model** of the Bitmatrix
constant-product AMM extended with flash loan accounting.  The model mirrors the
behaviour that a Miniscript covenant would enforce:

* reserves follow the constant-product invariant with a configurable fee;
* swaps collect a fee that accrues to LPs;
* flash loans can be opened against the pool up to a configurable percentage of
  reserves and must be repaid in the same transaction with an additional fee;
* helper methods generate arbitrage plans that align the pool price with an
  external price source (Bitfinex in our MVP).

By shipping a live Python implementation we can run end-to-end demos: the
`flashloan.py` service uses this module to quote flash loans and verify that an
arbitrageur’s transaction would return the borrowed funds plus fee, while
`rebalance_service.py` leverages the same logic to simulate Bitfinex-powered
rebalancing.  Replacing this simulator with a real covenant is an engineering
task, not a conceptual leap, which makes the MVP pitch-ready.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, getcontext
from typing import Dict, Iterable, List, Optional
from uuid import uuid4

from .config import CONFIG

getcontext().prec = 28


def _to_decimal(value: Decimal | float | int | str) -> Decimal:
    """Helper that normalises numeric inputs to :class:`~decimal.Decimal`."""

    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _serialize_decimal(value: Decimal) -> str:
    """Convert a :class:`~decimal.Decimal` to a JSON-friendly string."""

    return format(value, "f")


@dataclass
class SwapQuote:
    """Representation of a swap before or after execution."""

    input_asset: str
    output_asset: str
    amount_in: Decimal
    amount_out: Decimal
    fee_paid: Decimal
    price_after: Decimal

    def to_payload(self) -> Dict[str, str]:
        """Serialise the quote for JSON/base64 transport."""

        return {
            "input_asset": self.input_asset,
            "output_asset": self.output_asset,
            "amount_in": _serialize_decimal(self.amount_in),
            "amount_out": _serialize_decimal(self.amount_out),
            "fee_paid": _serialize_decimal(self.fee_paid),
            "price_after": _serialize_decimal(self.price_after),
        }


@dataclass
class FlashLoanTerms:
    """Snapshot of an active flash loan."""

    loan_id: str
    borrow_asset: str
    borrow_amount: Decimal
    repay_asset: str
    repay_amount: Decimal
    fee_amount: Decimal

    def to_payload(self) -> Dict[str, str]:
        return {
            "loan_id": self.loan_id,
            "borrow_asset": self.borrow_asset,
            "borrow_amount": _serialize_decimal(self.borrow_amount),
            "repay_asset": self.repay_asset,
            "repay_amount": _serialize_decimal(self.repay_amount),
            "fee": _serialize_decimal(self.fee_amount),
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

    def to_summary(self) -> Dict[str, str]:
        return {
            "reason": self.reason,
            "target_price": _serialize_decimal(self.target_price),
            "borrow_asset": self.borrow_asset,
            "borrow_amount": _serialize_decimal(self.borrow_amount),
            "repay_amount": _serialize_decimal(self.repay_amount),
            "expected_profit": _serialize_decimal(self.expected_profit),
        }


class BitmatrixAMMSim:
    """Bitmatrix-style constant product AMM with flash loan tracking."""

    def __init__(
        self,
        asset_a: str,
        asset_b: str,
        fee_bps: int,
        flash_fee_bps: int,
        initial_reserves: Dict[str, Decimal],
        max_flashloan_ratio: Decimal,
    ) -> None:
        self.asset_a = asset_a
        self.asset_b = asset_b
        self.swap_fee = _to_decimal(fee_bps) / Decimal(10_000)
        self.flash_fee = _to_decimal(flash_fee_bps) / Decimal(10_000)
        self.max_flashloan_ratio = max_flashloan_ratio
        self.reserves: Dict[str, Decimal] = {
            asset_a: _to_decimal(initial_reserves.get(asset_a, Decimal(0))),
            asset_b: _to_decimal(initial_reserves.get(asset_b, Decimal(0))),
        }
        self.accumulated_fees: Dict[str, Decimal] = {
            asset_a: Decimal(0),
            asset_b: Decimal(0),
        }
        self.active_loans: Dict[str, FlashLoanTerms] = {}

    # ------------------------------------------------------------------
    # Reserve helpers
    # ------------------------------------------------------------------
    def invariant(self) -> Decimal:
        return self.reserves[self.asset_a] * self.reserves[self.asset_b]

    def price(self) -> Decimal:
        """Return the marginal price (asset_b per asset_a)."""

        if self.reserves[self.asset_a] == 0:
            return Decimal(0)
        return self.reserves[self.asset_b] / self.reserves[self.asset_a]

    def snapshot(self) -> Dict[str, Decimal]:
        return dict(self.reserves)

    # ------------------------------------------------------------------
    # Swap logic
    # ------------------------------------------------------------------
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

        amount_in_with_fee = amount_in * (Decimal(1) - self.swap_fee)
        numerator = amount_in_with_fee * reserve_out
        denominator = reserve_in + amount_in_with_fee
        amount_out = numerator / denominator
        if amount_out <= 0:
            raise ValueError("Swap would produce zero output")

        fee_paid = amount_in - amount_in_with_fee
        # Post-swap reserves
        new_reserves = {
            input_asset: reserve_in + amount_in,
            output_asset: reserve_out - amount_out,
        }
        price_after = new_reserves[self.asset_b] / new_reserves[self.asset_a]
        return SwapQuote(
            input_asset=input_asset,
            output_asset=output_asset,
            amount_in=amount_in,
            amount_out=amount_out,
            fee_paid=fee_paid,
            price_after=price_after,
        )

    def execute_swap(self, input_asset: str, amount_in: Decimal) -> SwapQuote:
        """Mutate reserves according to the swap and return the realised quote."""

        quote = self.quote_swap(input_asset, amount_in)
        self.reserves[quote.input_asset] += quote.amount_in
        self.reserves[quote.output_asset] -= quote.amount_out
        self.accumulated_fees[quote.input_asset] += quote.fee_paid
        return quote

    # ------------------------------------------------------------------
    # Flash loan logic
    # ------------------------------------------------------------------
    def prepare_flashloan(self, asset: str, amount: Decimal) -> FlashLoanTerms:
        amount = _to_decimal(amount)
        if amount <= 0:
            raise ValueError("Flash loan amount must be positive")
        if asset not in (self.asset_a, self.asset_b):
            raise ValueError("Unsupported flash loan asset")

        reserve = self.reserves[asset]
        max_amount = reserve * self.max_flashloan_ratio
        if amount > max_amount:
            raise ValueError(
                f"Requested flash loan {amount} exceeds maximum {max_amount} for {asset}"
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
        )
        self.active_loans[loan_id] = terms
        return terms

    def cancel_flashloan(self, loan_id: str) -> None:
        self.active_loans.pop(loan_id, None)

    def complete_flashloan(self, loan_id: str, repay_amount: Decimal) -> Decimal:
        if loan_id not in self.active_loans:
            raise ValueError("Unknown flash loan identifier")
        terms = self.active_loans.pop(loan_id)
        repay_amount = _to_decimal(repay_amount)
        required = terms.repay_amount
        if repay_amount < required:
            raise ValueError(
                f"Flash loan repayment {repay_amount} lower than required {required}"
            )
        fee_collected = repay_amount - terms.borrow_amount
        self.reserves[terms.borrow_asset] += fee_collected
        self.accumulated_fees[terms.borrow_asset] += fee_collected
        return fee_collected

    # ------------------------------------------------------------------
    # Arbitrage helpers
    # ------------------------------------------------------------------
    def _solve_quadratic_positive(self, a: Decimal, b: Decimal, c: Decimal) -> Optional[Decimal]:
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
        if not candidates:
            return None
        return max(candidates)

    def _solve_input_asset_a_for_price(self, target_price: Decimal) -> Optional[Decimal]:
        x = self.reserves[self.asset_a]
        y = self.reserves[self.asset_b]
        gamma = Decimal(1) - self.swap_fee
        a = target_price * gamma
        b = target_price * x * (Decimal(1) + gamma)
        c = target_price * (x * x) - x * y
        return self._solve_quadratic_positive(a, b, c)

    def _solve_input_asset_b_for_price(self, target_price: Decimal) -> Optional[Decimal]:
        x = self.reserves[self.asset_a]
        y = self.reserves[self.asset_b]
        gamma = Decimal(1) - self.swap_fee
        a = gamma
        b = y * (Decimal(1) + gamma)
        c = y * y - target_price * x * y
        return self._solve_quadratic_positive(a, b, c)

    def arbitrage_opportunity(
        self, market_price: Decimal, tolerance: Decimal
    ) -> Optional[ArbitrageOpportunity]:
        """Return the trade that would move the pool back inside tolerance."""

        market_price = _to_decimal(market_price)
        tolerance = _to_decimal(tolerance)
        current_price = self.price()
        upper = market_price * (Decimal(1) + tolerance)
        lower = market_price * (Decimal(1) - tolerance)

        if lower <= current_price <= upper:
            return None

        if current_price > upper:
            amount_in = self._solve_input_asset_a_for_price(market_price)
            if amount_in is None:
                return None
            swap_quote = self.quote_swap(self.asset_a, amount_in)
            borrow_amount = swap_quote.amount_in * market_price
            repay_amount = borrow_amount * (Decimal(1) + self.flash_fee)
            expected_profit = swap_quote.amount_out - repay_amount
            reason = "Pool LBTC price above Bitfinex – sell LBTC into the pool."
        else:
            amount_in = self._solve_input_asset_b_for_price(market_price)
            if amount_in is None:
                return None
            swap_quote = self.quote_swap(self.asset_b, amount_in)
            borrow_amount = swap_quote.amount_in
            repay_amount = borrow_amount * (Decimal(1) + self.flash_fee)
            external_sale = swap_quote.amount_out * market_price
            expected_profit = external_sale - repay_amount
            reason = "Pool LBTC price below Bitfinex – buy LBTC from the pool."

        return ArbitrageOpportunity(
            reason=reason,
            target_price=market_price,
            borrow_asset=self.asset_b,
            borrow_amount=borrow_amount,
            repay_amount=repay_amount,
            swap=swap_quote,
            expected_profit=expected_profit,
        )

    def plan_flashloan_arbitrage(
        self, terms: FlashLoanTerms, market_price: Decimal, tolerance: Decimal
    ) -> Dict[str, object]:
        """Given specific flash loan terms, compute the swap instructions."""

        market_price = _to_decimal(market_price)
        tolerance = _to_decimal(tolerance)
        current_price = self.price()
        upper = market_price * (Decimal(1) + tolerance)
        lower = market_price * (Decimal(1) - tolerance)
        notes: Dict[str, str] = {
            "current_price": _serialize_decimal(current_price),
        }

        if terms.borrow_asset != self.asset_b:
            notes[
                "warning"
            ] = "Bitfinex flash loans are denominated in USDT for automated arbitrage."
            return {"swaps": [], "expected_profit": Decimal(0), "notes": notes}

        if lower <= current_price <= upper:
            notes["status"] = "Pool already within tolerance"
            return {"swaps": [], "expected_profit": Decimal(0), "notes": notes}

        if current_price > upper:
            amount_in = terms.borrow_amount / market_price
            swap_quote = self.quote_swap(self.asset_a, amount_in)
            expected_profit = swap_quote.amount_out - terms.repay_amount
            notes["strategy"] = "Buy LBTC on Bitfinex with borrowed USDT and sell to pool."
        else:
            amount_in = terms.borrow_amount
            swap_quote = self.quote_swap(self.asset_b, amount_in)
            external_sale = swap_quote.amount_out * market_price
            expected_profit = external_sale - terms.repay_amount
            notes["strategy"] = "Borrow USDT, buy LBTC from pool, sell on Bitfinex."

        notes["price_after"] = _serialize_decimal(swap_quote.price_after)
        return {
            "swaps": [swap_quote],
            "expected_profit": expected_profit,
            "notes": notes,
        }

    # ------------------------------------------------------------------
    # Simulation interface used by liquid_utils
    # ------------------------------------------------------------------
    def apply_simulated_pset(self, data: Dict[str, object]) -> Dict[str, object]:
        """Execute a simulated swap or flash loan payload."""

        action_type = data.get("type")
        if action_type == "swap":
            swap_data = data.get("swap", {})
            quote = self.execute_swap(
                str(swap_data["input_asset"]),
                _to_decimal(swap_data["amount_in"]),
            )
            min_output = swap_data.get("min_output")
            if min_output is not None:
                min_output_dec = _to_decimal(min_output)
                if min_output_dec > 0 and quote.amount_out < min_output_dec:
                    raise ValueError("Swap output below minimum requirement")
            return {
                "txid": f"swap-sim-{uuid4().hex}",
                "amount_out": _serialize_decimal(quote.amount_out),
                "price_after": _serialize_decimal(quote.price_after),
                "pool_reserves": {
                    asset: _serialize_decimal(amount) for asset, amount in self.reserves.items()
                },
            }

        if action_type == "flashloan":
            loan = data.get("flashloan", {})
            loan_id = str(loan["loan_id"])
            swaps_payload: Iterable[Dict[str, object]] = data.get("swaps", [])  # type: ignore[arg-type]
            realised_swaps: List[Dict[str, str]] = []
            for swap_payload in swaps_payload:
                quote = self.execute_swap(
                    str(swap_payload["input_asset"]),
                    _to_decimal(swap_payload["amount_in"]),
                )
                min_output = swap_payload.get("min_output")
                if min_output is not None:
                    min_output_dec = _to_decimal(min_output)
                    if min_output_dec > 0 and quote.amount_out < min_output_dec:
                        raise ValueError("Flash loan swap output below minimum requirement")
                realised_swaps.append({
                    "input_asset": quote.input_asset,
                    "output_asset": quote.output_asset,
                    "amount_in": _serialize_decimal(quote.amount_in),
                    "amount_out": _serialize_decimal(quote.amount_out),
                    "price_after": _serialize_decimal(quote.price_after),
                })
            settlement = data.get("settlement", {})
            repay_amount = _to_decimal(
                settlement.get("repay_amount", loan.get("repay_amount"))
            )
            fee_collected = self.complete_flashloan(loan_id, repay_amount)
            return {
                "txid": f"flashloan-sim-{loan_id}",
                "loan_id": loan_id,
                "repay_amount": _serialize_decimal(repay_amount),
                "fee_collected": _serialize_decimal(fee_collected),
                "swaps": realised_swaps,
                "price_after": _serialize_decimal(self.price()),
                "pool_reserves": {
                    asset: _serialize_decimal(amount) for asset, amount in self.reserves.items()
                },
            }

        raise ValueError(f"Unsupported simulated action: {action_type}")


SIMULATED_POOL = BitmatrixAMMSim(
    asset_a=CONFIG.pool_asset_a,
    asset_b=CONFIG.pool_asset_b,
    fee_bps=CONFIG.fee_bps,
    flash_fee_bps=CONFIG.flashloan_fee_bps,
    initial_reserves={
        CONFIG.pool_asset_a: CONFIG.initial_lbtc_reserve,
        CONFIG.pool_asset_b: CONFIG.initial_lusdt_reserve,
    },
    max_flashloan_ratio=CONFIG.max_flashloan_ratio,
)

