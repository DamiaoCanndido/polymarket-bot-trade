"""
Trade Tracker and Feed Watcher for live copytrading.
Extracts trade details, executes buys and proportional sells, logs events to JSONL, and manages portfolio state.
"""
import os
import json
import time
import uuid
import logging
import traceback
from datetime import datetime
from typing import Dict, Any, List, Set, Optional

from config import BotConfig, MasterTrader
from risk_manager import RiskManager
from executor import CopyExecutor

logger = logging.getLogger("CopyTracker")


class CopyTracker:
    def __init__(self, config: BotConfig, executor: CopyExecutor, risk_manager: RiskManager):
        self.config = config
        self.executor = executor
        self.risk_manager = risk_manager

        self.trades_log_file = config.trades_log_file
        self.portfolio_state_file = config.portfolio_state_file
        self.processed_trade_ids: Set[str] = set()

        # Track master trader holdings for calculating sell proportions:
        # master_address -> { market_slug:outcome -> shares_held }
        self.master_positions: Dict[str, Dict[str, float]] = {}

        # Bot portfolio state (both for paper mode and tracking mirror execution)
        self.portfolio: Dict[str, Any] = self._load_or_init_portfolio()

        # Bot startup timestamp
        self.start_time = datetime.utcnow()
        self.start_timestamp_str = self.start_time.strftime("%Y-%m-%d %H:%M:%S UTC")
        self._seeded = False

    def _load_or_init_portfolio(self) -> Dict[str, Any]:
        """
        Loads existing portfolio state from file if available, or initializes new state.
        """
        if os.path.exists(self.portfolio_state_file):
            try:
                with open(self.portfolio_state_file, "r") as f:
                    state = json.load(f)
                    logger.info(f"Loaded existing portfolio state: ${state.get('cash_usd', 1000.0):.2f} cash, {len(state.get('positions', {}))} open positions")
                    return state
            except Exception as e:
                logger.warning(f"Could not load portfolio state file ({e}), initializing default.")

        return {
            "initial_cash_usd": 1000.0,
            "cash_usd": 1000.0,
            "realized_pnl_usd": 0.0,
            "total_trades_count": 0,
            "successful_trades": 0,
            "failed_trades": 0,
            "skipped_trades": 0,
            "positions": {},  # "market_slug:outcome" -> {"market_slug": ..., "outcome": ..., "shares": ..., "total_cost": ..., "avg_price": ...}
            "last_updated": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        }

    def _save_portfolio_state(self) -> None:
        """
        Persists current portfolio metrics and open positions to portfolio_state.json.
        """
        try:
            self.portfolio["last_updated"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            
            # Calculate open positions value estimate
            pos_val = 0.0
            for pos in self.portfolio.get("positions", {}).values():
                pos_val += pos.get("shares", 0.0) * pos.get("avg_price", 0.50)

            self.portfolio["positions_value_usd"] = pos_val
            self.portfolio["total_equity_usd"] = self.portfolio.get("cash_usd", 0.0) + pos_val
            self.portfolio["open_positions_count"] = len(self.portfolio.get("positions", {}))

            with open(self.portfolio_state_file, "w") as f:
                json.dump(self.portfolio, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save portfolio state to {self.portfolio_state_file}: {e}")

    def _log_trade_record(self, record: Dict[str, Any]) -> None:
        """
        Appends a structured trade event to trades_log.jsonl for performance tracking and dashboard streaming.
        """
        try:
            with open(self.trades_log_file, "a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as e:
            logger.error(f"Failed to write to trades log {self.trades_log_file}: {e}")

    def seed_historical_trades(self) -> None:
        """
        Seeds current feed trade IDs on bot startup so only subsequent new trades are executed.
        """
        logger.info("Seeding historical trades to ensure only new trades are mirrored...")
        seeded_count = 0
        for trader in self.config.traders:
            if not trader.enabled:
                continue
            try:
                trades = self.executor.fetch_master_feed(address=trader.address, limit=20)
                for tr in trades:
                    tid = tr.get("trade_id") or tr.get("id")
                    if tid:
                        self.processed_trade_ids.add(str(tid))
                        seeded_count += 1
            except Exception as e:
                logger.warning(f"Could not seed feed for trader {trader.name}: {e}")

        logger.info(f"Seeded {seeded_count} prior trades as already processed.")

    def extract_trade_details(self, trade: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extracts standardized trade fields: market slug, outcome, amount, price, side, etc.
        """
        market_slug = trade.get("market_slug") or trade.get("event_slug") or ""
        market_title = trade.get("market_title") or trade.get("event_name") or market_slug
        outcome = trade.get("outcome") or trade.get("asset") or "Yes"
        side = (trade.get("side") or "BUY").upper()
        
        try:
            price = float(trade.get("price") or trade.get("avg_price") or 0.50)
        except (ValueError, TypeError):
            price = 0.50

        try:
            size_usd = float(trade.get("size_usd") or trade.get("amount") or 0.0)
        except (ValueError, TypeError):
            size_usd = 0.0

        master_shares = size_usd / price if price > 0 else 0.0
        trade_id = trade.get("trade_id") or trade.get("id") or f"{market_slug}_{trade.get('timestamp')}_{side}"
        tx_hash = trade.get("transaction_hash") or ""
        trade_time = trade.get("timestamp") or datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

        return {
            "trade_id": str(trade_id),
            "transaction_hash": str(tx_hash),
            "market_slug": market_slug,
            "market_title": market_title,
            "outcome": outcome,
            "side": side,
            "price": price,
            "size_usd": size_usd,
            "master_shares": master_shares,
            "timestamp": trade_time
        }

    def process_incoming_trade(self, trade: Dict[str, Any], trader_info: MasterTrader) -> Dict[str, Any]:
        """
        Processes a single incoming trade event:
        1. Extracts market slug, outcome, amount
        2. If BUY -> Buys according to sizing & risk
        3. If SELL -> Sells proportionally IF we hold open position; otherwise skips safely
        4. Logs full details to trades_log.jsonl
        5. Updates portfolio state
        """
        extracted = self.extract_trade_details(trade)
        trade_id = extracted["trade_id"]

        if trade_id in self.processed_trade_ids:
            return {"status": "SKIPPED", "reason": "already processed"}

        self.processed_trade_ids.add(trade_id)

        market_slug = extracted["market_slug"]
        market_title = extracted["market_title"]
        outcome = extracted["outcome"]
        side = extracted["side"]
        price = extracted["price"]
        master_size_usd = extracted["size_usd"]
        master_shares = extracted["master_shares"]
        pos_key = f"{market_slug}:{outcome}"

        event_id = str(uuid.uuid4())
        timestamp_now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

        logger.info(
            f"⚡ New Signal: {side} {outcome} on '{market_slug}' by {trader_info.name} "
            f"(${master_size_usd:,.2f} @ ${price:.3f})"
        )

        # Base log structure for JSONL
        log_entry = {
            "event_id": event_id,
            "timestamp": timestamp_now,
            "trade_id": trade_id,
            "transaction_hash": extracted["transaction_hash"],
            "master_trader": {
                "address": trader_info.address,
                "name": trader_info.name,
                "category": trader_info.category,
                "risk_tier": trader_info.risk_tier,
                "style": trader_info.style
            },
            "market": {
                "slug": market_slug,
                "title": market_title,
                "outcome": outcome
            },
            "master_trade": {
                "side": side,
                "price": price,
                "size_usd": master_size_usd,
                "shares": master_shares,
                "timestamp": extracted["timestamp"]
            },
            "bot_execution": {
                "action": side,
                "amount_usd": 0.0,
                "shares": 0.0,
                "price": price,
                "proportional_fraction": 1.0,
                "mode": "paper" if self.config.dry_run else "live",
                "status": "PENDING",
                "reason": ""
            },
            "error": None,
            "portfolio_metrics": {}
        }

        # -------------------------------------------------------------
        # CASE 1: MASTER BOUGHT -> WE BUY
        # -------------------------------------------------------------
        if side == "BUY":
            # Track master's position accumulation
            if trader_info.address not in self.master_positions:
                self.master_positions[trader_info.address] = {}
            self.master_positions[trader_info.address][pos_key] = (
                self.master_positions[trader_info.address].get(pos_key, 0.0) + master_shares
            )

            # Determine target copy size
            if self.config.sizing.mode == "percentage":
                target_size_usd = master_size_usd * (self.config.sizing.mirror_percent_cap / 100.0)
            else:
                target_size_usd = trader_info.copy_amount_usd or self.config.sizing.fixed_amount_usd

            # Validate against Risk Manager
            is_valid, reason, approved_usd = self.risk_manager.validate_trade(
                market_slug=market_slug,
                price=price,
                intended_usd=target_size_usd,
                side="buy"
            )

            if not is_valid:
                logger.warning(f"Trade rejected by Risk Manager: {reason}")
                self.portfolio["skipped_trades"] = self.portfolio.get("skipped_trades", 0) + 1
                log_entry["bot_execution"]["status"] = "REJECTED_BY_RISK"
                log_entry["bot_execution"]["reason"] = reason
                log_entry["portfolio_metrics"] = self._get_portfolio_metrics()
                self._log_trade_record(log_entry)
                self._save_portfolio_state()
                return {"status": "REJECTED_BY_RISK", "reason": reason, "details": log_entry}

            # Execute Buy
            try:
                bot_shares = approved_usd / price if price > 0 else 0.0

                if self.config.dry_run:
                    # Paper execution
                    self.portfolio["cash_usd"] -= approved_usd
                    pos = self.portfolio["positions"].get(pos_key, {
                        "market_slug": market_slug,
                        "market_title": market_title,
                        "outcome": outcome,
                        "shares": 0.0,
                        "total_cost": 0.0,
                        "avg_price": price
                    })
                    pos["shares"] += bot_shares
                    pos["total_cost"] += approved_usd
                    pos["avg_price"] = pos["total_cost"] / pos["shares"] if pos["shares"] > 0 else price
                    self.portfolio["positions"][pos_key] = pos

                    self.risk_manager.record_trade_execution(market_slug, approved_usd, "buy")
                    self.portfolio["successful_trades"] = self.portfolio.get("successful_trades", 0) + 1
                    self.portfolio["total_trades_count"] = self.portfolio.get("total_trades_count", 0) + 1

                    log_entry["bot_execution"]["status"] = "EXECUTED"
                    log_entry["bot_execution"]["amount_usd"] = approved_usd
                    log_entry["bot_execution"]["shares"] = bot_shares
                    log_entry["bot_execution"]["reason"] = "Successfully mirrored master BUY (Paper)"
                else:
                    # Live execution via Bullpen CLI
                    max_allowed_price = price * (1.0 + (self.config.risk.slippage_tolerance_pct / 100.0))
                    res = self.executor.execute_buy(
                        market_slug=market_slug,
                        outcome=outcome,
                        amount_usd=approved_usd,
                        max_price=max_allowed_price
                    )
                    if not res.get("success"):
                        raise RuntimeError(f"Bullpen buy order failed: {res.get('error')}")

                    self.risk_manager.record_trade_execution(market_slug, approved_usd, "buy")
                    self.portfolio["successful_trades"] = self.portfolio.get("successful_trades", 0) + 1
                    self.portfolio["total_trades_count"] = self.portfolio.get("total_trades_count", 0) + 1

                    log_entry["bot_execution"]["status"] = "EXECUTED"
                    log_entry["bot_execution"]["amount_usd"] = approved_usd
                    log_entry["bot_execution"]["shares"] = bot_shares
                    log_entry["bot_execution"]["reason"] = "Successfully mirrored master BUY (Live)"

            except Exception as buy_err:
                err_msg = str(buy_err)
                logger.error(f"Failed to execute BUY for {market_slug}: {err_msg}")
                self.portfolio["failed_trades"] = self.portfolio.get("failed_trades", 0) + 1
                self.portfolio["total_trades_count"] = self.portfolio.get("total_trades_count", 0) + 1
                log_entry["bot_execution"]["status"] = "FAILED"
                log_entry["bot_execution"]["reason"] = f"Execution error: {err_msg}"
                log_entry["error"] = err_msg

        # -------------------------------------------------------------
        # CASE 2: MASTER SOLD -> WE SELL PROPORTIONALLY (IF HELD)
        # -------------------------------------------------------------
        elif side == "SELL":
            # Check if we have an open position in this market outcome
            held_position = self.portfolio.get("positions", {}).get(pos_key)
            if not held_position or held_position.get("shares", 0.0) <= 0.0001:
                reason = f"No open position held for {pos_key} (Skipping sell)"
                logger.info(f"⏭️ {reason}")
                self.portfolio["skipped_trades"] = self.portfolio.get("skipped_trades", 0) + 1
                log_entry["bot_execution"]["action"] = "SKIP"
                log_entry["bot_execution"]["status"] = "SKIPPED"
                log_entry["bot_execution"]["reason"] = reason
                log_entry["portfolio_metrics"] = self._get_portfolio_metrics()
                self._log_trade_record(log_entry)
                self._save_portfolio_state()
                return {"status": "SKIPPED", "reason": reason, "details": log_entry}

            # We hold this position! Calculate proportional sell fraction
            master_known_shares = self.master_positions.get(trader_info.address, {}).get(pos_key, 0.0)
            if master_known_shares > 0:
                proportional_fraction = min(1.0, master_shares / master_known_shares)
                self.master_positions[trader_info.address][pos_key] = max(0.0, master_known_shares - master_shares)
            else:
                # If we don't have prior tracked master shares, sell full holding
                proportional_fraction = 1.0

            our_shares = held_position.get("shares", 0.0)
            shares_to_sell = our_shares * proportional_fraction
            gross_usd_value = shares_to_sell * price

            try:
                if self.config.dry_run:
                    # Paper sell execution
                    cost_basis_sold = (shares_to_sell / our_shares) * held_position.get("total_cost", 0.0)
                    realized_pnl = gross_usd_value - cost_basis_sold

                    self.portfolio["cash_usd"] += gross_usd_value
                    self.portfolio["realized_pnl_usd"] = self.portfolio.get("realized_pnl_usd", 0.0) + realized_pnl
                    held_position["shares"] -= shares_to_sell
                    held_position["total_cost"] -= cost_basis_sold

                    if held_position["shares"] <= 0.001:
                        del self.portfolio["positions"][pos_key]
                    else:
                        self.portfolio["positions"][pos_key] = held_position

                    self.risk_manager.record_trade_execution(market_slug, gross_usd_value, "sell")
                    self.portfolio["successful_trades"] = self.portfolio.get("successful_trades", 0) + 1
                    self.portfolio["total_trades_count"] = self.portfolio.get("total_trades_count", 0) + 1

                    pnl_sign = "+" if realized_pnl >= 0 else ""
                    log_entry["bot_execution"]["status"] = "EXECUTED"
                    log_entry["bot_execution"]["amount_usd"] = gross_usd_value
                    log_entry["bot_execution"]["shares"] = shares_to_sell
                    log_entry["bot_execution"]["proportional_fraction"] = proportional_fraction
                    log_entry["bot_execution"]["reason"] = (
                        f"Sold {proportional_fraction * 100:.1f}% ({shares_to_sell:.2f} shares) | "
                        f"Realized PnL: {pnl_sign}${realized_pnl:.2f} (Paper)"
                    )
                else:
                    # Live sell execution via Bullpen CLI
                    min_allowed_price = price * (1.0 - (self.config.risk.slippage_tolerance_pct / 100.0))
                    res = self.executor.execute_sell(
                        market_slug=market_slug,
                        outcome=outcome,
                        shares=shares_to_sell,
                        sell_all=(proportional_fraction >= 0.99),
                        min_price=min_allowed_price
                    )
                    if not res.get("success"):
                        raise RuntimeError(f"Bullpen sell order failed: {res.get('error')}")

                    self.risk_manager.record_trade_execution(market_slug, gross_usd_value, "sell")
                    self.portfolio["successful_trades"] = self.portfolio.get("successful_trades", 0) + 1
                    self.portfolio["total_trades_count"] = self.portfolio.get("total_trades_count", 0) + 1

                    log_entry["bot_execution"]["status"] = "EXECUTED"
                    log_entry["bot_execution"]["amount_usd"] = gross_usd_value
                    log_entry["bot_execution"]["shares"] = shares_to_sell
                    log_entry["bot_execution"]["proportional_fraction"] = proportional_fraction
                    log_entry["bot_execution"]["reason"] = f"Sold {proportional_fraction * 100:.1f}% of position (Live)"

            except Exception as sell_err:
                err_msg = str(sell_err)
                logger.error(f"Failed to execute SELL for {market_slug}: {err_msg}")
                self.portfolio["failed_trades"] = self.portfolio.get("failed_trades", 0) + 1
                self.portfolio["total_trades_count"] = self.portfolio.get("total_trades_count", 0) + 1
                log_entry["bot_execution"]["status"] = "FAILED"
                log_entry["bot_execution"]["reason"] = f"Execution error: {err_msg}"
                log_entry["error"] = err_msg

        # Finalize and record entry
        log_entry["portfolio_metrics"] = self._get_portfolio_metrics()
        self._log_trade_record(log_entry)
        self._save_portfolio_state()

        return {
            "status": log_entry["bot_execution"]["status"],
            "reason": log_entry["bot_execution"]["reason"],
            "details": log_entry
        }

    def _get_portfolio_metrics(self) -> Dict[str, Any]:
        """
        Returns clean snapshot metrics for logging.
        """
        pos_val = sum(p.get("shares", 0.0) * p.get("avg_price", 0.50) for p in self.portfolio.get("positions", {}).values())
        cash = self.portfolio.get("cash_usd", 1000.0)
        return {
            "cash_usd": round(cash, 2),
            "positions_value_usd": round(pos_val, 2),
            "total_equity_usd": round(cash + pos_val, 2),
            "realized_pnl_usd": round(self.portfolio.get("realized_pnl_usd", 0.0), 2),
            "total_trades_count": self.portfolio.get("total_trades_count", 0),
            "successful_trades": self.portfolio.get("successful_trades", 0),
            "failed_trades": self.portfolio.get("failed_trades", 0),
            "open_positions_count": len(self.portfolio.get("positions", {}))
        }

    def poll_cycle(self) -> List[Dict[str, Any]]:
        """
        Runs a single poll cycle across all active master traders.
        Catches any feed or execution errors to ensure uninterrupted looping.
        """
        if not self._seeded:
            self.seed_historical_trades()
            self._seeded = True

        results = []
        for trader in self.config.traders:
            if not trader.enabled:
                continue

            try:
                trades = self.executor.fetch_master_feed(address=trader.address, limit=10)
                for trade in trades:
                    try:
                        res = self.process_incoming_trade(trade, trader)
                        if res.get("status") in ("EXECUTED", "FAILED"):
                            results.append(res)
                    except Exception as trade_err:
                        logger.error(f"Unhandled error processing trade from {trader.name}: {trade_err}")
                        # Fallback error logging to JSONL
                        self._log_trade_record({
                            "event_id": str(uuid.uuid4()),
                            "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "trade_id": str(trade.get("trade_id") or trade.get("id") or "unknown"),
                            "master_trader": {"address": trader.address, "name": trader.name},
                            "error": str(trade_err),
                            "traceback": traceback.format_exc(),
                            "status": "FAILED"
                        })
            except Exception as feed_err:
                logger.error(f"Error fetching trade feed for trader {trader.name} ({trader.address}): {feed_err}")

        return results
