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
| `amm_contract.py` | High‑level description and placeholder for the Liquid covenant script.  In a real deployment this would be implemented in Liquid’s Miniscript or Elements’ `policy` language and compiled into a redeem script. |
| `liquid_utils.py` | Helper functions to interact with a Liquid node (via JSON‑RPC), build PSETs, sign them, and broadcast transactions.  It wraps common operations such as generating addresses, estimating fees, and fetching UTXO balances. |
| `bfx_client.py` | Simplified Bitfinex API client used by the rebalancer to borrow or repay USDT/BTC and to transfer funds between Bitfinex and Liquid.  This module does **not** implement full error handling and is meant for demonstration only. |
| `rebalance_service.py` | Core logic for monitoring the pool and maintaining a target 50/50 value ratio.  It queries the pool’s UTXO state, compares the current BTC value to USDT value (via price feed), and rebalances using Bitfinex and on‑chain swaps if necessary. |
| `flashloan.py` | Flask app exposing endpoints that generate PSETs for external arbitrageurs.  It uses `liquid_utils` to construct a transaction that lends L‑BTC/L‑USDT from the pool and expects repayment plus fee in the same PSET. |
| `config.py` | Central configuration dataclass for API keys, Liquid RPC connection, Bitfinex API credentials, pool parameters, and fee settings. |
| `README.md` | This file. |

## Running the MVP (conceptually)

1. **Install dependencies:** `pip install -r requirements.txt` (requires Python 3.10+).
2. **Start a Liquid node:** run `elementsd` configured for Liquid.  Ensure RPC is enabled and accessible.
3. **Fund the pool:** use `liquid_utils` to peg‑in BTC and USDT to Liquid and deposit equal value into the covenant UTXO.  You must also fund your Bitfinex account with USDT/BTC for rebalancing.
4. **Start the orchestrator:** run `python rebalance_service.py`.  The service will monitor the pool and Bitfinex and maintain the 2× BTC exposure by rebalancing.
5. **Start the flashloan API:** run `python flashloan.py` to allow external arbitrageurs to borrow capital and arbitrage the pool.  They will return borrowed tokens plus a fee in the same PSET, which accrues to your pool.

This MVP is a starting point.  To participate in the Lugano Plan ₿ pitch competition, focus on explaining how this design brings **impermanent‑loss‑free BTC yield** to Liquid.  The core innovation is marrying Liquid’s covenant‑based AMM with off‑chain leverage and PSET‑based flash loans so that anyone can arbitrage the pool and thus keep it efficient.  The next sections contain the code skeletons for each module.