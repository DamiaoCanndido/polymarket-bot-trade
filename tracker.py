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
import concurrent.futures
from typing import Dict, Any, List, Set, Optional

from config import BotConfig, MasterTrader
from risk_manager import RiskManager
from executor import CopyExecutor
from scanner import SportsMarketScanner

logger = logging.getLogger("CopyTracker")


class CopyTracker:
    def __init__(self, config: BotConfig, executor: CopyExecutor, risk_manager: RiskManager):
        self.config = config
        self.executor = executor
        self.risk_manager = risk_manager
        self.sports_scanner = SportsMarketScanner(getattr(config, "sports", None))

        self.trades_log_file = config.trades_log_file
        self.signals_log_file = getattr(config, "signals_log_file", os.path.join(os.path.dirname(__file__), "signals_log.jsonl"))
        self.portfolio_state_file = config.portfolio_state_file
        self.processed_trade_ids: Set[str] = set()
        self.dispatched_signal_ids: Set[str] = set()

        # Track master trader holdings separately for paper and live modes:
        # mode -> master_address -> { market_slug:outcome -> shares_held }
        self.master_positions: Dict[str, Dict[str, Dict[str, float]]] = {
            "paper": {},
            "live": {}
        }

        # Bot portfolio state separated into "paper" and "live"
        self.portfolio: Dict[str, Any] = self._load_or_init_portfolio()

        # Bot startup timestamp
        self.start_time = datetime.utcnow()
        self.start_timestamp_str = self.start_time.strftime("%Y-%m-%d %H:%M:%S UTC")
        self._seeded = False

    def _default_mode_state(self, initial_cash: float = 1000.0) -> Dict[str, Any]:
        return {
            "initial_cash_usd": float(initial_cash),
            "cash_usd": float(initial_cash),
            "realized_pnl_usd": 0.0,
            "total_trades_count": 0,
            "successful_trades": 0,
            "failed_trades": 0,
            "skipped_trades": 0,
            "positions": {},  # "market_slug:outcome" -> {"market_slug": ..., "outcome": ..., "shares": ..., "total_cost": ..., "avg_price": ...}
            "positions_value_usd": 0.0,
            "total_equity_usd": float(initial_cash),
            "open_positions_count": 0,
            "last_updated": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        }

    def _load_or_init_portfolio(self) -> Dict[str, Any]:
        """
        Loads existing portfolio state from file if available, or initializes new state.
        Migrates legacy flat portfolio files into separated "paper" and "live" states.
        """
        if os.path.exists(self.portfolio_state_file):
            try:
                with open(self.portfolio_state_file, "r") as f:
                    state = json.load(f)

                    # Check if already in separated format
                    if isinstance(state, dict) and "paper" in state and "live" in state:
                        logger.info(
                            f"Loaded separated portfolio state | "
                            f"Paper: ${state['paper'].get('cash_usd', 1000.0):.2f} ({len(state['paper'].get('positions', {}))} pos) | "
                            f"Live: ${state['live'].get('cash_usd', 0.0):.2f} ({len(state['live'].get('positions', {}))} pos)"
                        )
                        return state

                    # Legacy format detected: migrate flat dict into paper state
                    logger.info("Migrating legacy portfolio state to separated paper/live schema...")
                    paper_state = {
                        "initial_cash_usd": float(state.get("initial_cash_usd", self.config.paper_initial_cash_usd)),
                        "cash_usd": float(state.get("cash_usd", self.config.paper_initial_cash_usd)),
                        "realized_pnl_usd": float(state.get("realized_pnl_usd", 0.0)),
                        "total_trades_count": int(state.get("successful_trades", 0)),
                        "successful_trades": int(state.get("successful_trades", 0)),
                        "failed_trades": 0,
                        "skipped_trades": int(state.get("skipped_trades", 0)),
                        "positions": state.get("positions", {}),
                        "positions_value_usd": float(state.get("positions_value_usd", 0.0)),
                        "total_equity_usd": float(state.get("total_equity_usd", 1000.0)),
                        "open_positions_count": len(state.get("positions", {})),
                        "last_updated": state.get("last_updated", datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"))
                    }

                    # Live state initialized clean
                    live_state = self._default_mode_state(self.config.live_initial_cash_usd)

                    migrated = {
                        "paper": paper_state,
                        "live": live_state,
                        "last_updated": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
                    }

                    # Persist migrated state immediately
                    with open(self.portfolio_state_file, "w") as out_f:
                        json.dump(migrated, out_f, indent=2)

                    return migrated
            except Exception as e:
                logger.warning(f"Could not load portfolio state file ({e}), initializing default.")

        return {
            "paper": self._default_mode_state(self.config.paper_initial_cash_usd),
            "live": self._default_mode_state(self.config.live_initial_cash_usd),
            "last_updated": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        }

    def _save_portfolio_state(self) -> None:
        """
        Persists current portfolio metrics and open positions for both paper and live modes to portfolio_state.json.
        """
        try:
            now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            self.portfolio["last_updated"] = now_str

            for mode in ("paper", "live"):
                if mode not in self.portfolio:
                    self.portfolio[mode] = self._default_mode_state(
                        self.config.paper_initial_cash_usd if mode == "paper" else self.config.live_initial_cash_usd
                    )
                mode_port = self.portfolio[mode]
                mode_port["last_updated"] = now_str

                # Calculate open positions value estimate
                pos_val = 0.0
                for pos in mode_port.get("positions", {}).values():
                    pos_val += float(pos.get("shares", 0.0)) * float(pos.get("avg_price", 0.50))

                mode_port["positions_value_usd"] = round(pos_val, 2)
                mode_port["total_equity_usd"] = round(float(mode_port.get("cash_usd", 0.0)) + pos_val, 2)
                mode_port["open_positions_count"] = len(mode_port.get("positions", {}))

            with open(self.portfolio_state_file, "w") as f:
                json.dump(self.portfolio, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save portfolio state to {self.portfolio_state_file}: {e}")

    def reset_statistics(self, mode: Optional[str] = None) -> Dict[str, Any]:
        """
        Resets portfolio statistics and clears trades log.
        If mode is 'paper', resets only paper state.
        If mode is 'live', resets only live state.
        If mode is None or 'all', resets both modes and clears the trades log file completely.
        """
        if mode == "paper":
            self.portfolio["paper"] = self._default_mode_state(self.config.paper_initial_cash_usd)
            self.master_positions["paper"] = {}
        elif mode == "live":
            self.portfolio["live"] = self._default_mode_state(self.config.live_initial_cash_usd)
            self.master_positions["live"] = {}
        else:
            self.portfolio = {
                "paper": self._default_mode_state(self.config.paper_initial_cash_usd),
                "live": self._default_mode_state(self.config.live_initial_cash_usd),
                "last_updated": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            }
            self.master_positions = {"paper": {}, "live": {}}
            self.processed_trade_ids.clear()
            # Clear trades log file
            try:
                with open(self.trades_log_file, "w") as f:
                    pass
            except Exception as e:
                logger.error(f"Error clearing trades log {self.trades_log_file}: {e}")

        self._save_portfolio_state()
        return {"success": True, "message": f"Estatísticas resetadas com sucesso ({mode or 'todas'})."}

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
        Seeds current feed trade IDs on bot startup in parallel so only subsequent new trades are executed.
        """
        logger.info("Seeding historical trades in parallel...")
        seeded_count = 0
        enabled_traders = [t for t in self.config.traders if t.enabled]

        def fetch_for_trader(t):
            try:
                return self.executor.fetch_master_feed(address=t.address, limit=15)
            except Exception:
                return []

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(12, len(enabled_traders) or 1)) as pool:
            future_to_trader = {pool.submit(fetch_for_trader, t): t for t in enabled_traders}
            for fut in concurrent.futures.as_completed(future_to_trader):
                try:
                    trades = fut.result()
                    for tr in trades:
                        tid = tr.get("trade_id") or tr.get("id")
                        if tid:
                            self.processed_trade_ids.add(str(tid))
                            seeded_count += 1
                except Exception:
                    pass

        logger.info(f"Seeded {seeded_count} prior trades as already processed.")

    def extract_trade_details(self, trade: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extracts standardized trade fields: market slug, outcome, amount, price, side, etc.
        """
        event_slug = trade.get("event_slug") or trade.get("eventSlug") or ""
        market_slug = trade.get("market_slug") or trade.get("slug") or event_slug or ""
        market_title = trade.get("market_title") or trade.get("title") or trade.get("event_name") or market_slug
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

        target_slug = event_slug or market_slug
        market_url = trade.get("market_url") or trade.get("event_url") or (f"https://polymarket.com/event/{target_slug}" if target_slug else "")

        return {
            "trade_id": str(trade_id),
            "transaction_hash": str(tx_hash),
            "event_slug": event_slug,
            "market_slug": market_slug,
            "market_title": market_title,
            "market_url": market_url,
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
        2. Determines execution mode (Paper vs Live)
        3. If BUY -> Buys according to sizing & risk in mode's portfolio
        4. If SELL -> Sells proportionally IF we hold open position in mode; otherwise skips safely
        5. Logs full details to trades_log.jsonl with mode-isolated metrics
        6. Updates portfolio state
        """
        extracted = self.extract_trade_details(trade)
        trade_id = extracted["trade_id"]

        if trade_id in self.processed_trade_ids:
            return {"status": "SKIPPED", "reason": "already processed"}

        self.processed_trade_ids.add(trade_id)

        mode = "paper" if self.config.dry_run else "live"
        mode_port = self.portfolio[mode]
        master_pos_map = self.master_positions[mode]

        event_slug = extracted.get("event_slug") or market_slug
        market_title = extracted["market_title"]
        market_url = extracted["market_url"]
        outcome = extracted["outcome"]
        side = extracted["side"]
        price = extracted["price"]
        master_size_usd = extracted["size_usd"]
        master_shares = extracted["master_shares"]
        pos_key = f"{market_slug}:{outcome}"

        event_id = str(uuid.uuid4())
        timestamp_now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

        logger.info(
            f"⚡ New Signal [{mode.upper()}]: {side} {outcome} on '{market_url or market_slug}' by {trader_info.name} "
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
                "event_slug": event_slug,
                "title": market_title,
                "url": market_url,
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
                "mode": mode,
                "status": "PENDING",
                "reason": ""
            },
            "error": None,
            "portfolio_metrics": {}
        }

        # Check if market slug or outcome is valid
        if not market_slug or not market_slug.strip() or not outcome or not outcome.strip():
            reason = "Mercado não identificado no feed do Polymarket (slug ou outcome vazio)"
            logger.warning(f"⏭️ {reason} (Trade ID: {trade_id})")
            mode_port["skipped_trades"] = mode_port.get("skipped_trades", 0) + 1
            log_entry["bot_execution"]["status"] = "SKIPPED"
            log_entry["bot_execution"]["reason"] = reason
            log_entry["portfolio_metrics"] = self._get_portfolio_metrics(mode)
            self._log_trade_record(log_entry)
            self._save_portfolio_state()
            return {"status": "SKIPPED", "reason": reason, "details": log_entry}

        # -------------------------------------------------------------
        # CASE 1: MASTER BOUGHT -> WE BUY
        # -------------------------------------------------------------
        if side == "BUY":
            # Track master's position accumulation for this mode
            if trader_info.address not in master_pos_map:
                master_pos_map[trader_info.address] = {}
            master_pos_map[trader_info.address][pos_key] = (
                master_pos_map[trader_info.address].get(pos_key, 0.0) + master_shares
            )

            # Determine target copy size
            if self.config.sizing.mode == "percentage":
                target_size_usd = master_size_usd * (self.config.sizing.mirror_percent_cap / 100.0)
            else:
                target_size_usd = getattr(trader_info, "copy_amount_usd", None) or self.config.sizing.fixed_amount_usd

            # Validate against Risk Manager for current mode
            is_valid, reason, approved_usd = self.risk_manager.validate_trade(
                market_slug=market_slug,
                price=price,
                intended_usd=target_size_usd,
                side="buy",
                mode=mode
            )

            if not is_valid:
                logger.warning(f"Trade rejected by Risk Manager [{mode.upper()}]: {reason}")
                mode_port["skipped_trades"] = mode_port.get("skipped_trades", 0) + 1
                log_entry["bot_execution"]["status"] = "REJECTED_BY_RISK"
                log_entry["bot_execution"]["reason"] = reason
                log_entry["portfolio_metrics"] = self._get_portfolio_metrics(mode)
                self._log_trade_record(log_entry)
                self._save_portfolio_state()
                return {"status": "REJECTED_BY_RISK", "reason": reason, "details": log_entry}

            # Execute Buy
            try:
                bot_shares = approved_usd / price if price > 0 else 0.0

                if mode == "paper":
                    # Paper execution
                    mode_port["cash_usd"] -= approved_usd
                    pos = mode_port["positions"].get(pos_key, {
                        "market_slug": market_slug,
                        "event_slug": event_slug,
                        "market_title": market_title,
                        "market_url": market_url,
                        "outcome": outcome,
                        "shares": 0.0,
                        "total_cost": 0.0,
                        "avg_price": price
                    })
                    pos["market_url"] = market_url
                    pos["event_slug"] = event_slug
                    pos["shares"] += bot_shares
                    pos["total_cost"] += approved_usd
                    pos["avg_price"] = pos["total_cost"] / pos["shares"] if pos["shares"] > 0 else price
                    mode_port["positions"][pos_key] = pos

                    self.risk_manager.record_trade_execution(market_slug, approved_usd, "buy", mode="paper")
                    mode_port["successful_trades"] = mode_port.get("successful_trades", 0) + 1
                    mode_port["total_trades_count"] = mode_port.get("total_trades_count", 0) + 1

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
                        max_price=max_allowed_price,
                        event_slug=event_slug,
                        event_url=market_url
                    )
                    if not res.get("success"):
                        raise RuntimeError(f"Bullpen buy order failed: {res.get('error')}")

                    # Update live position in state
                    mode_port["cash_usd"] -= approved_usd
                    pos = mode_port["positions"].get(pos_key, {
                        "market_slug": market_slug,
                        "event_slug": event_slug,
                        "market_title": market_title,
                        "market_url": market_url,
                        "outcome": outcome,
                        "shares": 0.0,
                        "total_cost": 0.0,
                        "avg_price": price
                    })
                    pos["market_url"] = market_url
                    pos["event_slug"] = event_slug
                    pos["shares"] += bot_shares
                    pos["total_cost"] += approved_usd
                    pos["avg_price"] = pos["total_cost"] / pos["shares"] if pos["shares"] > 0 else price
                    mode_port["positions"][pos_key] = pos

                    self.risk_manager.record_trade_execution(market_slug, approved_usd, "buy", mode="live")
                    mode_port["successful_trades"] = mode_port.get("successful_trades", 0) + 1
                    mode_port["total_trades_count"] = mode_port.get("total_trades_count", 0) + 1

                    log_entry["bot_execution"]["status"] = "EXECUTED"
                    log_entry["bot_execution"]["amount_usd"] = approved_usd
                    log_entry["bot_execution"]["shares"] = bot_shares
                    log_entry["bot_execution"]["reason"] = "Successfully mirrored master BUY (Live)"

            except Exception as buy_err:
                err_msg = str(buy_err)
                logger.error(f"Failed to execute BUY for {market_slug} [{mode.upper()}]: {err_msg}")
                mode_port["failed_trades"] = mode_port.get("failed_trades", 0) + 1
                mode_port["total_trades_count"] = mode_port.get("total_trades_count", 0) + 1
                log_entry["bot_execution"]["status"] = "FAILED"
                log_entry["bot_execution"]["reason"] = f"Execution error: {err_msg}"
                log_entry["error"] = err_msg

        # -------------------------------------------------------------
        # CASE 2: MASTER SOLD -> WE SELL PROPORTIONALLY (IF HELD IN MODE)
        # -------------------------------------------------------------
        elif side == "SELL":
            # Check if we have an open position in this mode's portfolio
            held_position = mode_port.get("positions", {}).get(pos_key)
            if not held_position or held_position.get("shares", 0.0) <= 0.0001:
                reason = f"No open position held in {mode.upper()} portfolio for {pos_key} (Skipping sell)"
                logger.info(f"⏭️ {reason}")
                mode_port["skipped_trades"] = mode_port.get("skipped_trades", 0) + 1
                log_entry["bot_execution"]["action"] = "SKIP"
                log_entry["bot_execution"]["status"] = "SKIPPED"
                log_entry["bot_execution"]["reason"] = reason
                log_entry["portfolio_metrics"] = self._get_portfolio_metrics(mode)
                self._log_trade_record(log_entry)
                self._save_portfolio_state()
                return {"status": "SKIPPED", "reason": reason, "details": log_entry}

            # We hold this position! Calculate proportional sell fraction
            master_known_shares = master_pos_map.get(trader_info.address, {}).get(pos_key, 0.0)
            if master_known_shares > 0:
                proportional_fraction = min(1.0, master_shares / master_known_shares)
                master_pos_map[trader_info.address][pos_key] = max(0.0, master_known_shares - master_shares)
            else:
                # If we don't have prior tracked master shares, sell full holding
                proportional_fraction = 1.0

            our_shares = held_position.get("shares", 0.0)
            shares_to_sell = our_shares * proportional_fraction
            gross_usd_value = shares_to_sell * price
            cost_basis_sold = (shares_to_sell / our_shares) * held_position.get("total_cost", 0.0)
            realized_pnl = gross_usd_value - cost_basis_sold

            try:
                if mode == "paper":
                    # Paper sell execution
                    mode_port["cash_usd"] += gross_usd_value
                    mode_port["realized_pnl_usd"] = mode_port.get("realized_pnl_usd", 0.0) + realized_pnl
                    held_position["shares"] -= shares_to_sell
                    held_position["total_cost"] -= cost_basis_sold

                    if held_position["shares"] <= 0.001:
                        del mode_port["positions"][pos_key]
                    else:
                        mode_port["positions"][pos_key] = held_position

                    self.risk_manager.record_trade_execution(market_slug, gross_usd_value, "sell", mode="paper")
                    mode_port["successful_trades"] = mode_port.get("successful_trades", 0) + 1
                    mode_port["total_trades_count"] = mode_port.get("total_trades_count", 0) + 1

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
                        min_price=min_allowed_price,
                        event_slug=event_slug,
                        event_url=market_url
                    )
                    if not res.get("success"):
                        raise RuntimeError(f"Bullpen sell order failed: {res.get('error')}")

                    mode_port["cash_usd"] += gross_usd_value
                    mode_port["realized_pnl_usd"] = mode_port.get("realized_pnl_usd", 0.0) + realized_pnl
                    held_position["shares"] -= shares_to_sell
                    held_position["total_cost"] -= cost_basis_sold

                    if held_position["shares"] <= 0.001:
                        del mode_port["positions"][pos_key]
                    else:
                        mode_port["positions"][pos_key] = held_position

                    self.risk_manager.record_trade_execution(market_slug, gross_usd_value, "sell", mode="live")
                    mode_port["successful_trades"] = mode_port.get("successful_trades", 0) + 1
                    mode_port["total_trades_count"] = mode_port.get("total_trades_count", 0) + 1

                    pnl_sign = "+" if realized_pnl >= 0 else ""
                    log_entry["bot_execution"]["status"] = "EXECUTED"
                    log_entry["bot_execution"]["amount_usd"] = gross_usd_value
                    log_entry["bot_execution"]["shares"] = shares_to_sell
                    log_entry["bot_execution"]["proportional_fraction"] = proportional_fraction
                    log_entry["bot_execution"]["reason"] = (
                        f"Sold {proportional_fraction * 100:.1f}% ({shares_to_sell:.2f} shares) | "
                        f"Realized PnL: {pnl_sign}${realized_pnl:.2f} (Live)"
                    )

            except Exception as sell_err:
                err_msg = str(sell_err)
                logger.error(f"Failed to execute SELL for {market_slug} [{mode.upper()}]: {err_msg}")
                mode_port["failed_trades"] = mode_port.get("failed_trades", 0) + 1
                mode_port["total_trades_count"] = mode_port.get("total_trades_count", 0) + 1
                log_entry["bot_execution"]["status"] = "FAILED"
                log_entry["bot_execution"]["reason"] = f"Execution error: {err_msg}"
                log_entry["error"] = err_msg

        # Finalize and record entry
        log_entry["portfolio_metrics"] = self._get_portfolio_metrics(mode)
        self._log_trade_record(log_entry)
        self._save_portfolio_state()

        return {
            "status": log_entry["bot_execution"]["status"],
            "reason": log_entry["bot_execution"]["reason"],
            "details": log_entry
        }

    def _get_portfolio_metrics(self, mode: Optional[str] = None) -> Dict[str, Any]:
        """
        Returns clean snapshot metrics for logging.
        """
        if mode in ("paper", "live"):
            port = self.portfolio.get(mode, {})
            init_cash = port.get("initial_cash_usd", self.config.paper_initial_cash_usd if mode == "paper" else self.config.live_initial_cash_usd)
            pos_val = sum(p.get("shares", 0.0) * p.get("avg_price", 0.50) for p in port.get("positions", {}).values())
            cash = port.get("cash_usd", init_cash)
            return {
                "mode": mode,
                "initial_cash_usd": round(init_cash, 2),
                "cash_usd": round(cash, 2),
                "positions_value_usd": round(pos_val, 2),
                "total_equity_usd": round(cash + pos_val, 2),
                "realized_pnl_usd": round(port.get("realized_pnl_usd", 0.0), 2),
                "total_trades_count": port.get("total_trades_count", 0),
                "successful_trades": port.get("successful_trades", 0),
                "failed_trades": port.get("failed_trades", 0),
                "skipped_trades": port.get("skipped_trades", 0),
                "open_positions_count": len(port.get("positions", {}))
            }

        return {
            "paper": self._get_portfolio_metrics("paper"),
            "live": self._get_portfolio_metrics("live"),
            "current_mode": "paper" if self.config.dry_run else "live"
        }

    def check_auto_take_profit(self) -> List[Dict[str, Any]]:
        """
        Scans all open positions in the active mode portfolio.
        If a position meets the Auto Take-Profit criteria (price target or min gain %),
        triggers an automatic SELL to lock in profits without waiting for the master trader.
        """
        if not getattr(self.config.risk, "auto_take_profit", True):
            return []

        mode = "paper" if self.config.dry_run else "live"
        mode_port = self.portfolio.get(mode, {})
        positions = mode_port.get("positions", {})
        if not positions:
            return []

        tp_price_target = float(getattr(self.config.risk, "take_profit_price", 0.90))
        tp_min_gain_pct = float(getattr(self.config.risk, "take_profit_min_gain_pct", 20.0))

        executed_tps = []
        keys_to_close = []

        for pos_key, pos in list(positions.items()):
            market_slug = pos.get("market_slug")
            outcome = pos.get("outcome")
            shares = float(pos.get("shares", 0.0))
            avg_price = float(pos.get("avg_price", 0.50))
            total_cost = float(pos.get("total_cost", 0.0))
            market_title = pos.get("market_title", market_slug)
            market_url = pos.get("market_url", "")

            if shares <= 0.0001 or not market_slug or not outcome:
                continue

            try:
                live_prices = self.executor.get_market_prices(market_slug)
                current_price = live_prices.get(outcome)

                if current_price is None or current_price <= 0:
                    continue

                gain_pct = ((current_price - avg_price) / avg_price * 100.0) if avg_price > 0 else 0.0
                
                # Check criteria:
                # 1. Price is at or above target (e.g. >= 0.90) and profitable
                # 2. Or Gain % is above min gain threshold and current price > avg price
                is_target_price = (current_price >= tp_price_target and current_price > avg_price)
                is_target_gain = (tp_min_gain_pct > 0 and gain_pct >= tp_min_gain_pct and current_price > avg_price)

                if is_target_price or is_target_gain:
                    gross_usd_value = shares * current_price
                    realized_pnl = gross_usd_value - total_cost

                    logger.info(
                        f"🎯 AUTO TAKE-PROFIT Triggered [{mode.upper()}]: {outcome} on '{market_slug}' | "
                        f"Current: ${current_price:.3f} (Entry: ${avg_price:.3f} | +{gain_pct:.1f}%) | Realized PnL: +${realized_pnl:.2f}"
                    )

                    event_id = str(uuid.uuid4())
                    timestamp_now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

                    log_entry = {
                        "event_id": event_id,
                        "timestamp": timestamp_now,
                        "trade_id": f"tp_{event_id[:8]}",
                        "transaction_hash": None,
                        "master_trader": {
                            "address": "bot_auto_take_profit",
                            "name": "Auto Take-Profit",
                            "category": "System",
                            "risk_tier": "low",
                            "style": "profit_taker"
                        },
                        "market": {
                            "slug": market_slug,
                            "title": market_title,
                            "url": market_url,
                            "outcome": outcome
                        },
                        "master_trade": {
                            "side": "SELL",
                            "price": current_price,
                            "size_usd": gross_usd_value,
                            "shares": shares,
                            "timestamp": timestamp_now
                        },
                        "bot_execution": {
                            "action": "SELL",
                            "amount_usd": gross_usd_value,
                            "shares": shares,
                            "price": current_price,
                            "proportional_fraction": 1.0,
                            "mode": mode,
                            "status": "EXECUTED",
                            "reason": f"🎯 Auto Take-Profit: Realizou lucro de +${realized_pnl:.2f} (+{gain_pct:.1f}%) a ${current_price:.3f} sem esperar o mestre."
                        },
                        "error": None,
                        "portfolio_metrics": {}
                    }

                    if mode == "paper":
                        mode_port["cash_usd"] += gross_usd_value
                        mode_port["realized_pnl_usd"] = mode_port.get("realized_pnl_usd", 0.0) + realized_pnl
                        mode_port["successful_trades"] = mode_port.get("successful_trades", 0) + 1
                        mode_port["total_trades_count"] = mode_port.get("total_trades_count", 0) + 1
                        self.risk_manager.record_trade_execution(market_slug, gross_usd_value, "sell", mode="paper")
                        keys_to_close.append(pos_key)
                    else:
                        # Live sell execution via Bullpen CLI
                        min_allowed_price = current_price * (1.0 - (self.config.risk.slippage_tolerance_pct / 100.0))
                        res = self.executor.execute_sell(
                            market_slug=market_slug,
                            outcome=outcome,
                            shares=shares,
                            sell_all=True,
                            min_price=min_allowed_price
                        )
                        if not res.get("success"):
                            logger.error(f"Failed to execute live Auto Take-Profit sell: {res.get('error')}")
                            log_entry["bot_execution"]["status"] = "FAILED"
                            log_entry["bot_execution"]["reason"] = f"Live sell failed: {res.get('error')}"
                            log_entry["error"] = str(res.get("error"))
                            mode_port["failed_trades"] = mode_port.get("failed_trades", 0) + 1
                            mode_port["total_trades_count"] = mode_port.get("total_trades_count", 0) + 1
                        else:
                            mode_port["cash_usd"] += gross_usd_value
                            mode_port["realized_pnl_usd"] = mode_port.get("realized_pnl_usd", 0.0) + realized_pnl
                            mode_port["successful_trades"] = mode_port.get("successful_trades", 0) + 1
                            mode_port["total_trades_count"] = mode_port.get("total_trades_count", 0) + 1
                            self.risk_manager.record_trade_execution(market_slug, gross_usd_value, "sell", mode="live")
                            keys_to_close.append(pos_key)

                    log_entry["portfolio_metrics"] = self._get_portfolio_metrics(mode)
                    self._log_trade_record(log_entry)
                    executed_tps.append(log_entry)

            except Exception as e:
                logger.error(f"Error evaluating Auto Take-Profit for {pos_key}: {e}")

        for k in keys_to_close:
            if k in mode_port.get("positions", {}):
                del mode_port["positions"][k]

        if executed_tps:
            self._save_portfolio_state()

        return executed_tps

    def check_auto_stop_loss(self) -> List[Dict[str, Any]]:
        """
        Scans all open positions in the active mode portfolio.
        If a position meets the Stop-Loss / Market Resolution criteria (e.g. price <= 0.05 or loss >= 85%),
        automatically closes and liquidates the position to clear dead/resolved markets.
        """
        if not getattr(self.config.risk, "auto_stop_loss", True):
            return []

        mode = "paper" if self.config.dry_run else "live"
        mode_port = self.portfolio.get(mode, {})
        positions = mode_port.get("positions", {})
        if not positions:
            return []

        sl_price_trigger = float(getattr(self.config.risk, "stop_loss_price", 0.05))
        sl_max_loss_pct = float(getattr(self.config.risk, "stop_loss_max_loss_pct", 85.0))

        executed_sls = []
        keys_to_close = []

        for pos_key, pos in list(positions.items()):
            market_slug = pos.get("market_slug")
            outcome = pos.get("outcome")
            shares = float(pos.get("shares", 0.0))
            avg_price = float(pos.get("avg_price", 0.50))
            total_cost = float(pos.get("total_cost", 0.0))
            market_title = pos.get("market_title", market_slug)
            market_url = pos.get("market_url", "")

            if shares <= 0.0001 or not market_slug or not outcome:
                continue

            try:
                live_prices = self.executor.get_market_prices(market_slug)
                current_price = live_prices.get(outcome)

                if current_price is None:
                    continue

                current_price = max(0.0, float(current_price))
                loss_pct = ((avg_price - current_price) / avg_price * 100.0) if avg_price > 0 else 0.0

                # Check Stop-Loss / Resolution criteria:
                # 1. Price is at or below trigger (e.g. <= 0.05 or 5 cents) and in loss
                # 2. Or Loss % exceeds threshold (e.g. >= 85%) and price < avg_price
                is_sl_price = (current_price <= sl_price_trigger and current_price < avg_price)
                is_sl_loss = (sl_max_loss_pct > 0 and loss_pct >= sl_max_loss_pct and current_price < avg_price)

                if is_sl_price or is_sl_loss:
                    gross_usd_value = shares * current_price
                    realized_pnl = gross_usd_value - total_cost

                    logger.info(
                        f"🛑 AUTO STOP-LOSS / RESOLUTION Triggered [{mode.upper()}]: {outcome} on '{market_slug}' | "
                        f"Current: ${current_price:.3f} (Entry: ${avg_price:.3f} | -{loss_pct:.1f}%) | Realized PnL: ${realized_pnl:.2f}"
                    )

                    event_id = str(uuid.uuid4())
                    timestamp_now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

                    log_entry = {
                        "event_id": event_id,
                        "timestamp": timestamp_now,
                        "trade_id": f"sl_{event_id[:8]}",
                        "transaction_hash": None,
                        "master_trader": {
                            "address": "bot_auto_stop_loss",
                            "name": "Auto Stop-Loss",
                            "category": "System",
                            "risk_tier": "low",
                            "style": "risk_manager"
                        },
                        "market": {
                            "slug": market_slug,
                            "title": market_title,
                            "url": market_url,
                            "outcome": outcome
                        },
                        "master_trade": {
                            "side": "SELL",
                            "price": current_price,
                            "size_usd": gross_usd_value,
                            "shares": shares,
                            "timestamp": timestamp_now
                        },
                        "bot_execution": {
                            "action": "SELL",
                            "amount_usd": gross_usd_value,
                            "shares": shares,
                            "price": current_price,
                            "proportional_fraction": 1.0,
                            "mode": mode,
                            "status": "EXECUTED",
                            "reason": f"🛑 Auto Stop-Loss: Encerrou posição resolvida/perdedora ({outcome}) com Realized PnL: ${realized_pnl:.2f} (-{loss_pct:.1f}%) a ${current_price:.3f}."
                        },
                        "error": None,
                        "portfolio_metrics": {}
                    }

                    if mode == "paper":
                        mode_port["cash_usd"] += gross_usd_value
                        mode_port["realized_pnl_usd"] = mode_port.get("realized_pnl_usd", 0.0) + realized_pnl
                        mode_port["successful_trades"] = mode_port.get("successful_trades", 0) + 1
                        mode_port["total_trades_count"] = mode_port.get("total_trades_count", 0) + 1
                        self.risk_manager.record_trade_execution(market_slug, gross_usd_value, "sell", mode="paper")
                        keys_to_close.append(pos_key)
                    else:
                        # Live mode: sell if value > 0.005, else write off
                        if current_price > 0.005:
                            min_allowed_price = max(0.001, current_price * (1.0 - (self.config.risk.slippage_tolerance_pct / 100.0)))
                            res = self.executor.execute_sell(
                                market_slug=market_slug,
                                outcome=outcome,
                                shares=shares,
                                sell_all=True,
                                min_price=min_allowed_price
                            )
                            if res.get("success"):
                                mode_port["cash_usd"] += gross_usd_value
                                mode_port["realized_pnl_usd"] = mode_port.get("realized_pnl_usd", 0.0) + realized_pnl
                                mode_port["successful_trades"] = mode_port.get("successful_trades", 0) + 1
                                mode_port["total_trades_count"] = mode_port.get("total_trades_count", 0) + 1
                                self.risk_manager.record_trade_execution(market_slug, gross_usd_value, "sell", mode="live")
                                keys_to_close.append(pos_key)
                            else:
                                mode_port["realized_pnl_usd"] = mode_port.get("realized_pnl_usd", 0.0) + realized_pnl
                                mode_port["successful_trades"] = mode_port.get("successful_trades", 0) + 1
                                mode_port["total_trades_count"] = mode_port.get("total_trades_count", 0) + 1
                                keys_to_close.append(pos_key)
                        else:
                            mode_port["realized_pnl_usd"] = mode_port.get("realized_pnl_usd", 0.0) + realized_pnl
                            mode_port["successful_trades"] = mode_port.get("successful_trades", 0) + 1
                            mode_port["total_trades_count"] = mode_port.get("total_trades_count", 0) + 1
                            keys_to_close.append(pos_key)

                    log_entry["portfolio_metrics"] = self._get_portfolio_metrics(mode)
                    self._log_trade_record(log_entry)
                    executed_sls.append(log_entry)

            except Exception as e:
                logger.error(f"Error evaluating Auto Stop-Loss for {pos_key}: {e}")

        for k in keys_to_close:
            if k in mode_port.get("positions", {}):
                del mode_port["positions"][k]

        if executed_sls:
            self._save_portfolio_state()

        return executed_sls

    def manual_close_position(self, pos_key: str, mode: Optional[str] = None) -> Dict[str, Any]:
        """Manually closes and liquidates an open position in the specified or current mode."""
        exec_mode = mode or ("paper" if self.config.dry_run else "live")
        mode_port = self.portfolio.get(exec_mode, {})
        positions = mode_port.get("positions", {})

        if pos_key not in positions:
            return {"success": False, "error": f"Posição '{pos_key}' não encontrada no portfólio {exec_mode.upper()}."}

        pos = positions[pos_key]
        market_slug = pos.get("market_slug")
        outcome = pos.get("outcome")
        shares = float(pos.get("shares", 0.0))
        avg_price = float(pos.get("avg_price", 0.50))
        total_cost = float(pos.get("total_cost", 0.0))
        market_title = pos.get("market_title", market_slug)
        market_url = pos.get("market_url", "")

        current_price = 0.0
        try:
            live_prices = self.executor.get_market_prices(market_slug)
            current_price = float(live_prices.get(outcome, 0.0) or 0.0)
        except Exception:
            current_price = 0.0

        gross_usd_value = shares * current_price
        realized_pnl = gross_usd_value - total_cost

        event_id = str(uuid.uuid4())
        timestamp_now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

        log_entry = {
            "event_id": event_id,
            "timestamp": timestamp_now,
            "trade_id": f"manual_{event_id[:8]}",
            "transaction_hash": None,
            "master_trader": {
                "address": "user_manual_close",
                "name": "Manual Liquidation",
                "category": "User",
                "risk_tier": "low",
                "style": "manual"
            },
            "market": {
                "slug": market_slug,
                "title": market_title,
                "url": market_url,
                "outcome": outcome
            },
            "master_trade": {
                "side": "SELL",
                "price": current_price,
                "size_usd": gross_usd_value,
                "shares": shares,
                "timestamp": timestamp_now
            },
            "bot_execution": {
                "action": "SELL",
                "amount_usd": gross_usd_value,
                "shares": shares,
                "price": current_price,
                "proportional_fraction": 1.0,
                "mode": exec_mode,
                "status": "EXECUTED",
                "reason": f"👋 Liquidação Manual: Encerrou posição ({outcome}) com Realized PnL: ${realized_pnl:.2f} a ${current_price:.3f}."
            },
            "error": None,
            "portfolio_metrics": {}
        }

        if exec_mode == "paper":
            mode_port["cash_usd"] += gross_usd_value
            mode_port["realized_pnl_usd"] = mode_port.get("realized_pnl_usd", 0.0) + realized_pnl
            mode_port["successful_trades"] = mode_port.get("successful_trades", 0) + 1
            mode_port["total_trades_count"] = mode_port.get("total_trades_count", 0) + 1
            del mode_port["positions"][pos_key]
        else:
            if current_price > 0.005:
                res = self.executor.execute_sell(market_slug=market_slug, outcome=outcome, shares=shares, sell_all=True)
                if not res.get("success"):
                    logger.warning(f"Live manual sell CLI returned warning/error: {res.get('error')}")
            mode_port["cash_usd"] += gross_usd_value
            mode_port["realized_pnl_usd"] = mode_port.get("realized_pnl_usd", 0.0) + realized_pnl
            mode_port["successful_trades"] = mode_port.get("successful_trades", 0) + 1
            mode_port["total_trades_count"] = mode_port.get("total_trades_count", 0) + 1
            del mode_port["positions"][pos_key]

        log_entry["portfolio_metrics"] = self._get_portfolio_metrics(exec_mode)
        self._log_trade_record(log_entry)
        self._save_portfolio_state()

        return {
            "success": True,
            "message": f"Posição '{pos_key}' liquidada com sucesso (PnL: ${realized_pnl:.2f}).",
            "realized_pnl": realized_pnl,
            "gross_usd_value": gross_usd_value
        }

    def record_manual_trade(
        self,
        market_slug: str,
        outcome: str,
        price: float,
        amount_usd: float,
        market_title: str = "",
        event_url: str = "",
        mode: str = "live"
    ) -> Dict[str, Any]:
        """
        Records a manual trade (either real live bet or simulated paper bet).
        Updates the respective portfolio state and logs the trade record.
        """
        if amount_usd <= 0 or price <= 0:
            return {"success": False, "error": "Valor ou preço inválido."}

        exec_mode = "paper" if mode == "paper" else "live"
        mode_port = self.portfolio.get(
            exec_mode,
            self._default_mode_state(
                self.config.paper_initial_cash_usd if exec_mode == "paper" else self.config.live_initial_cash_usd
            )
        )
        shares = round(amount_usd / price, 2)
        pos_key = f"{market_slug}:{outcome}"

        # Deduct or adjust cash
        if exec_mode == "paper":
            mode_port["cash_usd"] = mode_port.get("cash_usd", 1000.0) - amount_usd
        else:
            mode_port["cash_usd"] = max(0.0, mode_port.get("cash_usd", 0.0) - amount_usd)

        mode_port["total_trades_count"] = mode_port.get("total_trades_count", 0) + 1
        mode_port["successful_trades"] = mode_port.get("successful_trades", 0) + 1

        # Add / update position
        positions = mode_port.setdefault("positions", {})
        if pos_key in positions:
            existing = positions[pos_key]
            old_shares = float(existing.get("shares", 0.0))
            old_cost = float(existing.get("total_cost", 0.0))
            new_shares = old_shares + shares
            new_cost = old_cost + amount_usd
            new_avg_price = new_cost / new_shares if new_shares > 0 else price
            positions[pos_key] = {
                "market_slug": market_slug,
                "market_title": market_title or existing.get("market_title", market_slug),
                "market_url": event_url or existing.get("market_url", f"https://polymarket.com/event/{market_slug}"),
                "outcome": outcome,
                "shares": round(new_shares, 2),
                "total_cost": round(new_cost, 2),
                "avg_price": round(new_avg_price, 4),
                "last_price": round(price, 4)
            }
        else:
            positions[pos_key] = {
                "market_slug": market_slug,
                "market_title": market_title or market_slug,
                "market_url": event_url or f"https://polymarket.com/event/{market_slug}",
                "outcome": outcome,
                "shares": shares,
                "total_cost": round(amount_usd, 2),
                "avg_price": round(price, 4),
                "last_price": round(price, 4)
            }

        event_id = str(uuid.uuid4())
        timestamp_now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        mode_label = "Live" if exec_mode == "live" else "Paper"
        trader_name = "Manual (Polymarket Web)" if exec_mode == "live" else "Simulação Manual (Paper)"

        log_entry = {
            "event_id": event_id,
            "timestamp": timestamp_now,
            "trade_id": f"manual_buy_{event_id[:8]}",
            "transaction_hash": None,
            "master_trader": {
                "address": f"user_manual_{exec_mode}",
                "name": trader_name,
                "category": "Sports",
                "risk_tier": "low",
                "style": "manual"
            },
            "market": {
                "slug": market_slug,
                "title": market_title or market_slug,
                "url": event_url or f"https://polymarket.com/event/{market_slug}",
                "outcome": outcome
            },
            "master_trade": {
                "side": "BUY",
                "price": price,
                "size_usd": amount_usd,
                "shares": shares,
                "timestamp": timestamp_now
            },
            "bot_execution": {
                "action": "BUY",
                "amount_usd": amount_usd,
                "shares": shares,
                "price": price,
                "proportional_fraction": 1.0,
                "mode": exec_mode,
                "status": "EXECUTED",
                "reason": f"👤 Aposta {mode_label.lower()} manual ({'registrada na Polymarket' if exec_mode == 'live' else 'simulada'}) (${amount_usd:.2f} @ ${price:.3f})."
            },
            "error": None,
            "portfolio_metrics": self._get_portfolio_metrics(exec_mode)
        }

        self._log_trade_record(log_entry)
        self._save_portfolio_state()

        return {
            "success": True,
            "message": f"Aposta ({mode_label}) de ${amount_usd:.2f} em {outcome} registrada com sucesso!",
            "details": log_entry
        }

    def record_manual_real_trade(
        self,
        market_slug: str,
        outcome: str,
        price: float,
        amount_usd: float,
        market_title: str = "",
        event_url: str = ""
    ) -> Dict[str, Any]:
        """Backward compatibility wrapper for record_manual_trade with mode='live'."""
        return self.record_manual_trade(
            market_slug=market_slug,
            outcome=outcome,
            price=price,
            amount_usd=amount_usd,
            market_title=market_title,
            event_url=event_url,
            mode="live"
        )

    def poll_cycle(self) -> List[Dict[str, Any]]:
        """
        Runs a single poll cycle:
        1. Scans sports markets for value signals.
        2. Monitors master trader feeds.
        3. Checks auto take-profit and stop-loss on open positions.
        """
        if not self._seeded:
            self.seed_historical_trades()
            self._seeded = True

        results = []

        # 1. Scan Sports Opportunities & Dispatch Signals
        if getattr(self.config, "sports", None) and getattr(self.config.sports, "enabled", True):
            try:
                sports_opps = self.sports_scanner.scan_sports_opportunities(limit_per_sport=5)
                for opp in sports_opps[:8]:
                    opp_id = opp["id"]
                    if opp_id not in self.dispatched_signal_ids:
                        self.dispatched_signal_ids.add(opp_id)
                        # Dispatch formatted notification
                        self.executor.dispatcher.dispatch_signal(opp)

                        # If Paper Trading is active, enter simulated paper trade
                        if self.config.dry_run:
                            paper_port = self.portfolio.get("paper", {})
                            market_slug = opp["market_slug"]
                            outcome = opp["outcome"]
                            price = opp["price"]
                            amt = min(self.config.sizing.fixed_amount_usd, 10.0)

                            if paper_port.get("cash_usd", 0.0) >= amt:
                                pos_key = f"{market_slug}:{outcome}"
                                if pos_key not in paper_port.get("positions", {}):
                                    shares = round(amt / price, 2)
                                    paper_port["cash_usd"] -= amt
                                    paper_port["total_trades_count"] = paper_port.get("total_trades_count", 0) + 1
                                    paper_port.setdefault("positions", {})[pos_key] = {
                                        "market_slug": market_slug,
                                        "market_title": opp["event_title"],
                                        "market_url": opp["event_url"],
                                        "outcome": outcome,
                                        "shares": shares,
                                        "total_cost": round(amt, 2),
                                        "avg_price": price,
                                        "last_price": price
                                    }
                                    self._save_portfolio_state()
            except Exception as sports_err:
                logger.error(f"Error scanning sports opportunities: {sports_err}")

        # 2. Process Master Trader Feeds in Parallel
        enabled_traders = [t for t in self.config.traders if t.enabled]

        def fetch_and_process(trader):
            trade_results = []
            try:
                trades = self.executor.fetch_master_feed(address=trader.address, limit=8)
                for trade in trades:
                    try:
                        res = self.process_incoming_trade(trade, trader)
                        if res.get("status") in ("EXECUTED", "FAILED"):
                            trade_results.append(res)
                    except Exception as trade_err:
                        logger.error(f"Unhandled error processing trade from {trader.name}: {trade_err}")
            except Exception as feed_err:
                logger.error(f"Error fetching trade feed for trader {trader.name} ({trader.address}): {feed_err}")
            return trade_results

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(10, len(enabled_traders) or 1)) as pool:
            future_to_trader = {pool.submit(fetch_and_process, t): t for t in enabled_traders}
            for fut in concurrent.futures.as_completed(future_to_trader):
                try:
                    res_list = fut.result()
                    results.extend(res_list)
                except Exception:
                    pass

        # 3. Check and Execute Auto Take-Profit on Profitable Open Positions
        try:
            tp_results = self.check_auto_take_profit()
            for tp in tp_results:
                results.append({"status": "EXECUTED", "reason": tp["bot_execution"]["reason"], "details": tp})
        except Exception as tp_err:
            logger.error(f"Error checking Auto Take-Profit: {tp_err}")

        # 4. Check and Execute Auto Stop-Loss / Cleanup on Losing/Resolved Positions
        try:
            sl_results = self.check_auto_stop_loss()
            for sl in sl_results:
                results.append({"status": "EXECUTED", "reason": sl["bot_execution"]["reason"], "details": sl})
        except Exception as sl_err:
            logger.error(f"Error checking Auto Stop-Loss: {sl_err}")

        return results
