# YieldBasis on Liquid — MVP

This repository contains a **minimal proof‑of‑concept** for replicating the principles of YieldBasis (YB) on the Liquid Bitcoin network.  YieldBasis was originally introduced on Ethereum/Curve to provide leveraged liquidity with no impermanent loss.  It accomplishes this by borrowing stablecoins against deposited BTC to maintain a constant two‑times BTC exposure, while an AMM generates swap fees for liquidity providers.  This MVP adapts that idea to Liquid by combining a covenant‑based AMM pool (inspired by the Bitmatrix design) with off‑chain hedging and rebalancing via Bitfinex.

Key points from our research:

* **Liquid AMM capabilities** – Bitmatrix demonstrated that Automated Market Makers can be built on Liquid using covenant scripts.  The AMM holds Liquid Bitcoin (L‑BTC) and Liquid‑issued stablecoins (e.g. USDt) and enforces swap rules via script; users can create pools, add liquidity, and perform swaps trustlessly【839791027663161†screenshot】.
* **Partially Signed Elements Transactions (PSET)** – Liquid swaps are implemented as PSETs.  A swap is a single transaction where each party adds their inputs and outputs and signs only what they own; the PSET travels between parties until fully signed【761627512528675†screenshot】.  Our design uses PSETs to allow external arbitrageurs to borrow tokens from the pool and return them within the same transaction, similar to a “flash loan” in EVM ecosystems.

## Architecture

The MVP is deliberately simple.  It focuses on demonstrating the core flow of a YB‑style pool rather than deploying a production‑ready DeFi protocol.  There are three main components:

1. **Covenant script (contract)** – A simple **constant‑product AMM** covenant holds L‑BTC and L‑USDT and enforces swap rules.  The script ensures that anyone who wishes to take funds from the pool must supply the correct amount of the other token to satisfy `x · y = k` and must add a small fee.  This contract lives on Liquid and governs all swaps and liquidity withdrawals.
2. **Off‑chain orchestrator** –  A Python service (see `rebalance_service.py`) monitors the pool balance via RPC, interacts with Bitfinex to borrow or lend L‑USDT/L‑BTC, and rebalances the pool back to a target 50/50 value ratio.  This service implements the YieldBasis logic: if the BTC side of the pool grows relative to the USDT side, it borrows USDT from Bitfinex (or uses treasury reserves) to top up the pool; if the BTC side shrinks, it sells BTC on Bitfinex and repays USDT debt.  All rebalancing happens off‑chain, but the orchestrator publishes and signs PSETs to update the covenant’s UTXO.
3. **Flash‑loan API and arbitrage helper** – A simple Flask API (see `flashloan.py`) exposes an endpoint for external arbitrageurs.  Arbitrageurs can request a quote and receive a partially signed PSET that lends them L‑BTC or L‑USDT from the pool.  They must return the borrowed asset plus fee in the same transaction; otherwise the covenant will reject the transaction.  This allows anyone to arbitrage price discrepancies between our pool and other venues, paying a small fee that accrues to liquidity providers.  All transactions are constructed and signed using the `liquid_utils` module.

The repository does **not** include any governance token or incentive mechanism.  It assumes the operator (you) provide the initial liquidity and manage the Bitfinex account.  Future enhancements could introduce a token to distribute fees or decentralize control.

## Files

| File | Description |
| --- | --- |
| `amm_contract.py` | Executable Bitmatrix-style AMM simulator that enforces constant-product swaps, tracks flash-loan terms, and suggests arbitrage trades using Bitfinex pricing.  Replacing this with a covenant script is a deployment detail. |
| `liquid_utils.py` | Convenience layer that converts AMM quotes into base64 “PSET-like” payloads, decodes simulated transactions, and falls back to Elements RPC if you connect a real node. |
| `bfx_client.py` | Simulated Bitfinex treasury with reservation/settlement logic so flash loans draw from an operator-provided USDT buffer rather than P2P credit. |
| `rebalance_service.py` | Async loop that continuously checks the AMM price versus Bitfinex, automatically spins up a flash loan, and closes it to demonstrate how the pool stays balanced. |
| `flashloan.py` | Flask API that any external party can call to reserve Bitfinex capital, receive an arbitrage PSET template, and submit the completed trade for settlement. |
| `config.py` | Central configuration dataclass for API keys, Liquid RPC connection, Bitfinex API credentials, pool parameters, and fee settings. |
| `README.md` | This file. |

## Running the MVP (conceptually)

1. **Install dependencies:** `pip install -r requirements.txt` (requires Python 3.10+).
2. **Start a Liquid node:** run `elementsd` configured for Liquid.  Ensure RPC is enabled and accessible.
3. **Fund the pool:** use `liquid_utils` to peg‑in BTC and USDT to Liquid and deposit equal value into the covenant UTXO.  You must also fund your Bitfinex account with USDT/BTC for rebalancing.
4. **Start the orchestrator:** run `python rebalance_service.py`.  The service now spins simulated flash loans backed by Bitfinex treasury capital whenever the AMM price drifts beyond the configured tolerance and settles them within the same loop.
5. **Start the flashloan API:** run `python flashloan.py` to allow external arbitrageurs to borrow capital and arbitrage the pool.  Each request reserves Bitfinex USDT, returns a base64 payload describing the swap/repay steps, and releases the reservation when the signed transaction is submitted.
6. **Test an arbitrage round-trip:** `curl -X POST localhost:8000/flashloan/request -d '{"asset":"LUSDt","amount":"1000"}'` to obtain a template, add your swap details, then `POST` the completed payload back to `/flashloan/submit`.  The simulator will verify repayment, accrue fees to LPs, and top the Bitfinex treasury back up.

This MVP is a starting point.  To participate in the Lugano Plan ₿ pitch competition, focus on explaining how this design brings **impermanent‑loss‑free BTC yield** to Liquid.  The core innovation is marrying Liquid’s covenant‑based AMM with off‑chain leverage and PSET‑based flash loans so that anyone can arbitrage the pool and thus keep it efficient.  The next sections contain the code skeletons for each module.
