"""
Polymarket Copytrading Bot - Local Web Dashboard & Control Center
Provides real-time analytics, PnL tracking, win rate metrics, trade feed, and bot on/off controls.
"""
import os
import sys
import json
import time
import uuid
import logging
import threading
from datetime import datetime
from typing import Dict, Any, List, Optional
from flask import Flask, jsonify, request, render_template_string

from config import load_config, save_config, BotConfig, MasterTrader
from risk_manager import RiskManager
from executor import CopyExecutor
from tracker import CopyTracker

logger = logging.getLogger("Dashboard")

app = Flask(__name__)
app.config["SECRET_KEY"] = "polymarket-copytrade-dashboard-secret"

# Global Bot Runner Manager
class BotRunnerManager:
    def __init__(self):
        self.config: BotConfig = load_config()
        self.risk_manager: RiskManager = RiskManager(self.config.risk)
        self.executor: CopyExecutor = CopyExecutor(self.config, self.risk_manager)
        self.tracker: CopyTracker = CopyTracker(self.config, self.executor, self.risk_manager)
        
        self.is_running: bool = False
        self.worker_thread: Optional[threading.Thread] = None
        self.stop_event: threading.Event = threading.Event()
        self.started_at: Optional[datetime] = None
        self.last_cycle_at: Optional[datetime] = None
        self.last_cycle_status: str = "IDLE"
        self.total_cycles: int = 0
        self.activity_logs: List[Dict[str, Any]] = []
        self.lock = threading.RLock()

        # Initial startup log
        self.log_activity("info", "Dashboard initialized. Ready to launch copytrading bot.")

    def log_activity(self, level: str, message: str, details: Optional[Dict[str, Any]] = None):
        """Records an activity event into the in-memory ring buffer (capped at 100)."""
        now = datetime.utcnow()
        entry = {
            "id": str(uuid.uuid4())[:8],
            "timestamp": now.strftime("%H:%M:%S"),
            "iso_time": now.isoformat() + "Z",
            "level": level,  # 'info', 'success', 'warning', 'error'
            "message": message,
            "details": details or {}
        }
        with self.lock:
            self.activity_logs.insert(0, entry)
            if len(self.activity_logs) > 100:
                self.activity_logs.pop()

    def get_portfolio(self) -> Dict[str, Any]:
        """Returns the freshest portfolio state, reloading from disk if not actively running."""
        with self.lock:
            if not self.is_running and os.path.exists(self.config.portfolio_state_file):
                try:
                    with open(self.config.portfolio_state_file, "r") as f:
                        disk_portfolio = json.load(f)
                        if isinstance(disk_portfolio, dict) and "paper" in disk_portfolio and "live" in disk_portfolio:
                            self.tracker.portfolio = disk_portfolio
                            return disk_portfolio
                except Exception:
                    pass
            return self.tracker.portfolio

    def reload_config(self):
        with self.lock:
            self.config = load_config()
            self.risk_manager = RiskManager(self.config.risk)
            self.executor = CopyExecutor(self.config, self.risk_manager)
            self.tracker = CopyTracker(self.config, self.executor, self.risk_manager)

    def get_wallet_info(self) -> Dict[str, Any]:
        return self.executor.get_wallet_balance()

    def sync_live_wallet_balance(self) -> Dict[str, Any]:
        with self.lock:
            info = self.executor.get_wallet_balance()
            if info.get("success"):
                bal = float(info.get("balance_usd", 0.0))
                wallet_addr = info.get("address", "")
                self.config.live_initial_cash_usd = bal
                save_config(self.config)
                if "live" in self.tracker.portfolio:
                    self.tracker.portfolio["live"]["cash_usd"] = bal
                    self.tracker.portfolio["live"]["initial_cash_usd"] = bal
                    self.tracker._save_portfolio_state()
                display_addr = f"{wallet_addr[:6]}...{wallet_addr[-4:]}" if len(wallet_addr) >= 10 else wallet_addr
                self.log_activity("success", f"💰 Saldo da carteira real ({display_addr}) sincronizado: ${bal:,.2f}")
                return {
                    "success": True,
                    "balance_usd": bal,
                    "address": wallet_addr,
                    "message": f"Saldo sincronizado com sucesso: ${bal:,.2f}"
                }
            return {"success": False, "error": info.get("error", "Falha ao consultar carteira"), "balance_usd": 0.0}

    def start(self, mode: Optional[str] = None) -> Dict[str, Any]:
        with self.lock:
            if self.is_running:
                return {"success": False, "message": "Bot is already running."}

            if mode in ("paper", "dry_run"):
                self.config.dry_run = True
            elif mode == "live":
                self.config.dry_run = False

            save_config(self.config)
            self.reload_config()

            active_traders = [t for t in self.config.traders if t.enabled]
            mode_label = "Paper Trading (Fake)" if self.config.dry_run else "Live Execution (Real)"

            self.stop_event.clear()
            self.is_running = True
            self.started_at = datetime.utcnow()
            self.last_cycle_status = "STARTING"
            self.worker_thread = threading.Thread(target=self._run_loop, daemon=True)
            self.worker_thread.start()

            msg = f"Bot started successfully in {mode_label} mode ({len(active_traders)} active master traders)."
            self.log_activity("success", f"🚀 {msg}")
            logger.info(f"Bot started in {mode_label.upper()} mode.")
            return {
                "success": True,
                "message": msg,
                "mode": "paper" if self.config.dry_run else "live",
                "active_traders": len(active_traders)
            }

    def stop(self) -> Dict[str, Any]:
        with self.lock:
            if not self.is_running:
                return {"success": False, "message": "Bot is not running."}

            self.stop_event.set()
            self.is_running = False
            self.last_cycle_status = "STOPPED"
            self.log_activity("warning", "⏹️ Bot stopped by operator. Background worker halted.")
            logger.info("Bot stop requested.")
            return {"success": True, "message": "Bot stopped successfully."}

    def _run_loop(self):
        logger.info("Background bot worker loop active.")
        active_count = len([t for t in self.config.traders if t.enabled])
        mode_tag = "PAPER (FAKE)" if self.config.dry_run else "LIVE (REAL)"
        self.log_activity("info", f"Background worker active in [{mode_tag}] listening to {active_count} master trader feeds ({self.config.poll_interval_seconds}s interval).")
        
        while not self.stop_event.is_set():
            try:
                self.total_cycles += 1
                self.last_cycle_at = datetime.utcnow()
                self.last_cycle_status = "POLLING"
                
                executed = self.tracker.poll_cycle()
                self.last_cycle_status = "LISTENING"

                active_count = len([t for t in self.config.traders if t.enabled])
                if executed:
                    for ev in executed:
                        b_exec = ev.get("details", {}).get("bot_execution", {})
                        mkt = ev.get("details", {}).get("market", {})
                        action = b_exec.get("action", "TRADE")
                        outcome = mkt.get("outcome", "Unknown")
                        slug = mkt.get("slug", "")
                        amt = b_exec.get("amount_usd", 0.0)
                        exec_mode = b_exec.get("mode", "paper").upper()
                        self.log_activity(
                            "success",
                            f"⚡ Signal Executed [{exec_mode}]: {action} '{outcome}' on {slug} (${amt:.2f})"
                        )
                    logger.info(f"Cycle #{self.total_cycles} executed {len(executed)} trade(s).")
                else:
                    self.log_activity(
                        "info",
                        f"📡 Cycle #{self.total_cycles} [{mode_tag}]: Polled {active_count} master traders (0 new signals)."
                    )
            except Exception as e:
                self.last_cycle_status = "ERROR"
                self.log_activity("error", f"Error in background bot loop: {e}")
                logger.error(f"Error in background bot loop: {e}", exc_info=True)

            # Sleep in 0.5s increments to respond quickly to stop event
            poll_interval = max(1, self.config.poll_interval_seconds)
            for _ in range(int(poll_interval * 2)):
                if self.stop_event.is_set():
                    break
                time.sleep(0.5)

        self.last_cycle_status = "STOPPED"
        logger.info("Background bot worker loop terminated.")

    def get_status(self) -> Dict[str, Any]:
        uptime_str = "0s"
        uptime_seconds = 0
        if self.is_running and self.started_at:
            delta = datetime.utcnow() - self.started_at
            uptime_seconds = int(delta.total_seconds())
            hours, rem = divmod(uptime_seconds, 3600)
            mins, secs = divmod(rem, 60)
            if hours > 0:
                uptime_str = f"{hours}h {mins}m {secs}s"
            elif mins > 0:
                uptime_str = f"{mins}m {secs}s"
            else:
                uptime_str = f"{secs}s"

        risk_summary = self.risk_manager.get_risk_summary()

        return {
            "is_running": self.is_running,
            "mode": "paper" if self.config.dry_run else "live",
            "started_at": self.started_at.isoformat() + "Z" if self.started_at else None,
            "uptime": uptime_str,
            "uptime_seconds": uptime_seconds,
            "total_cycles": self.total_cycles,
            "last_cycle_at": self.last_cycle_at.isoformat() + "Z" if self.last_cycle_at else None,
            "last_cycle_status": self.last_cycle_status,
            "poll_interval_seconds": self.config.poll_interval_seconds,
            "active_traders_count": len([t for t in self.config.traders if t.enabled]),
            "total_traders_count": len(self.config.traders),
            "risk_summary": risk_summary,
            "activity_logs": self.activity_logs[:25]
        }


bot_manager = BotRunnerManager()


def parse_trade_logs(log_file: str) -> List[Dict[str, Any]]:
    """Parses JSONL trades log file and returns list of trade records."""
    if not os.path.exists(log_file):
        return []
    records = []
    try:
        with open(log_file, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except Exception:
                        pass
    except Exception as e:
        logger.error(f"Error reading trade log {log_file}: {e}")
    return records


def _compute_single_mode_analytics(
    trades: List[Dict[str, Any]],
    mode_portfolio: Dict[str, Any],
    mode: str = "paper",
    initial_cash: float = 1000.0,
    risk_summary: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Calculates metrics specifically for a single mode (paper or live).
    """
    mode_trades = [
        t for t in trades
        if (t.get("bot_execution", {}).get("mode") or "paper").lower() == mode.lower()
    ]
    total_trades = len(mode_trades)
    executed_trades = 0
    skipped_trades = 0
    failed_trades = 0

    buys_count = 0
    sells_count = 0
    total_bought_usd = 0.0
    total_sold_usd = 0.0

    winners_count = 0
    losers_count = 0
    even_count = 0
    gross_profit = 0.0
    gross_loss = 0.0

    equity_history = []
    pnl_history = []
    timestamps = []

    running_pnl = 0.0
    init_cash = float(mode_portfolio.get("initial_cash_usd", initial_cash))
    current_cash = float(mode_portfolio.get("cash_usd", init_cash))
    realized_pnl_usd = float(mode_portfolio.get("realized_pnl_usd", 0.0))

    if mode_trades:
        timestamps.append("Start")
        pnl_history.append(0.0)
        equity_history.append(round(init_cash, 2))

    for tr in mode_trades:
        b_exec = tr.get("bot_execution", {})
        status = b_exec.get("status") or tr.get("status", "UNKNOWN")
        action = b_exec.get("action") or tr.get("master_trade", {}).get("side", "BUY")
        amt = float(b_exec.get("amount_usd") or 0.0)
        ts = tr.get("timestamp", "")
        pm = tr.get("portfolio_metrics", {})
        if isinstance(pm, dict) and mode in pm:
            pm = pm[mode]

        if status == "EXECUTED":
            executed_trades += 1
            if action == "BUY":
                buys_count += 1
                total_bought_usd += amt
            elif action == "SELL":
                sells_count += 1
                total_sold_usd += amt
                reason = b_exec.get("reason", "")
                if "Realized PnL:" in reason:
                    try:
                        pnl_part = reason.split("Realized PnL:")[1].split()[0].replace("$", "").replace("+", "").replace(",", "")
                        pnl_val = float(pnl_part)
                        if pnl_val > 0.001:
                            winners_count += 1
                            gross_profit += pnl_val
                        elif pnl_val < -0.001:
                            losers_count += 1
                            gross_loss += abs(pnl_val)
                        else:
                            even_count += 1
                    except Exception:
                        pass
                else:
                    winners_count += 1

            if isinstance(pm, dict) and "realized_pnl_usd" in pm:
                running_pnl = float(pm["realized_pnl_usd"])

            running_equity = float(pm.get("total_equity_usd", init_cash + running_pnl)) if isinstance(pm, dict) else (init_cash + running_pnl)

            timestamps.append(ts[:19].replace("T", " "))
            pnl_history.append(round(running_pnl, 2))
            equity_history.append(round(running_equity, 2))

        elif status in ("SKIPPED", "REJECTED_BY_RISK"):
            skipped_trades += 1
        elif status == "FAILED":
            failed_trades += 1

    if not realized_pnl_usd and running_pnl:
        realized_pnl_usd = running_pnl

    closed_trades_count = winners_count + losers_count + even_count
    win_rate_pct = (winners_count / (winners_count + losers_count) * 100.0) if (winners_count + losers_count) > 0 else (100.0 if winners_count > 0 else 0.0)
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (gross_profit if gross_profit > 0 else 1.0)

    positions = mode_portfolio.get("positions", {})
    open_positions_val = sum(float(p.get("shares", 0.0)) * float(p.get("avg_price", 0.50)) for p in positions.values())
    total_equity = float(mode_portfolio.get("total_equity_usd", current_cash + open_positions_val))

    daily_spent = risk_summary.get("daily_spent_usd", 0.0) if risk_summary else 0.0
    daily_cap = risk_summary.get("daily_budget_usd", 100.0) if risk_summary else 100.0

    return {
        "mode": mode,
        "initial_cash_usd": round(init_cash, 2),
        "realized_pnl_usd": round(realized_pnl_usd, 2),
        "win_rate_pct": round(win_rate_pct, 1),
        "total_trades_count": total_trades if total_trades > 0 else mode_portfolio.get("total_trades_count", 0),
        "executed_trades_count": executed_trades if executed_trades > 0 else mode_portfolio.get("successful_trades", 0),
        "skipped_trades_count": skipped_trades if skipped_trades > 0 else mode_portfolio.get("skipped_trades", 0),
        "failed_trades_count": failed_trades if failed_trades > 0 else mode_portfolio.get("failed_trades", 0),
        "buys_count": buys_count,
        "sells_count": sells_count,
        "total_bought_usd": round(total_bought_usd, 2),
        "total_sold_usd": round(total_sold_usd, 2),
        "winners_count": winners_count,
        "losers_count": losers_count,
        "even_count": even_count,
        "closed_trades_count": closed_trades_count,
        "gross_profit_usd": round(gross_profit, 2),
        "gross_loss_usd": round(gross_loss, 2),
        "profit_factor": round(profit_factor, 2),
        "cash_usd": round(current_cash, 2),
        "open_positions_value_usd": round(open_positions_val, 2),
        "total_equity_usd": round(total_equity, 2),
        "open_positions_count": len(positions),
        "daily_spent_usd": round(daily_spent, 2),
        "daily_budget_usd": round(daily_cap, 2),
        "chart_timestamps": timestamps[-30:] if timestamps else ["Start"],
        "chart_equity": equity_history[-30:] if equity_history else [round(init_cash, 2)],
        "chart_pnl": pnl_history[-30:] if pnl_history else [0.0]
    }


def calculate_analytics(trades: List[Dict[str, Any]], portfolio: Dict[str, Any], risk_manager: Optional[RiskManager] = None) -> Dict[str, Any]:
    """
    Computes separate analytics for Paper and Live trading modes.
    """
    paper_port = portfolio.get("paper", {})
    live_port = portfolio.get("live", {})

    paper_risk = risk_manager.get_risk_summary(mode="paper") if risk_manager else None
    live_risk = risk_manager.get_risk_summary(mode="live") if risk_manager else None

    paper_an = _compute_single_mode_analytics(trades, paper_port, mode="paper", initial_cash=bot_manager.config.paper_initial_cash_usd, risk_summary=paper_risk)
    live_an = _compute_single_mode_analytics(trades, live_port, mode="live", initial_cash=bot_manager.config.live_initial_cash_usd, risk_summary=live_risk)

    current_mode = "paper" if bot_manager.config.dry_run else "live"
    active_an = paper_an if current_mode == "paper" else live_an

    return {
        "paper": paper_an,
        "live": live_an,
        "current_mode": current_mode,
        **active_an
    }


# =====================================================================
# API ENDPOINTS
# =====================================================================

@app.route("/api/status", methods=["GET"])
def api_status():
    return jsonify(bot_manager.get_status())


@app.route("/api/bot/start", methods=["POST"])
def api_bot_start():
    data = request.get_json(silent=True) or {}
    mode = data.get("mode")
    result = bot_manager.start(mode=mode)
    return jsonify(result)


@app.route("/api/bot/stop", methods=["POST"])
def api_bot_stop():
    result = bot_manager.stop()
    return jsonify(result)


@app.route("/api/activity", methods=["GET"])
def api_activity():
    limit = int(request.args.get("limit", 50))
    return jsonify(bot_manager.activity_logs[:limit])


@app.route("/api/analytics", methods=["GET"])
def api_analytics():
    trades = parse_trade_logs(bot_manager.config.trades_log_file)
    portfolio = bot_manager.get_portfolio()
    mode = request.args.get("mode")
    analytics = calculate_analytics(trades, portfolio, bot_manager.risk_manager)
    if mode in ("paper", "live"):
        return jsonify(analytics[mode])
    return jsonify(analytics)


@app.route("/api/trades", methods=["GET"])
def api_trades():
    trades = parse_trade_logs(bot_manager.config.trades_log_file)
    mode = request.args.get("mode")
    if mode in ("paper", "live"):
        trades = [t for t in trades if (t.get("bot_execution", {}).get("mode") or "paper").lower() == mode.lower()]
    # Return newest trades first
    return jsonify(list(reversed(trades)))


@app.route("/api/positions", methods=["GET"])
def api_positions():
    portfolio = bot_manager.get_portfolio()
    mode = request.args.get("mode")
    if mode == "paper":
        return jsonify(portfolio.get("paper", {}).get("positions", {}))
    elif mode == "live":
        return jsonify(portfolio.get("live", {}).get("positions", {}))
    return jsonify({
        "paper": portfolio.get("paper", {}).get("positions", {}),
        "live": portfolio.get("live", {}).get("positions", {})
    })


@app.route("/api/portfolio", methods=["GET"])
def api_portfolio():
    return jsonify(bot_manager.get_portfolio())


@app.route("/api/traders", methods=["GET"])
def api_traders():
    cfg = load_config()
    traders_list = [t.__dict__ for t in cfg.traders]
    return jsonify(traders_list)


@app.route("/api/traders/toggle", methods=["POST"])
def api_traders_toggle():
    data = request.get_json(silent=True) or {}
    address = data.get("address")
    if not address:
        return jsonify({"success": False, "message": "Address required"}), 400

    cfg = load_config()
    found = False
    new_state = False
    trader_name = address
    for t in cfg.traders:
        if t.address.lower() == address.lower():
            t.enabled = not t.enabled
            new_state = t.enabled
            trader_name = t.name or address[:8]
            found = True
            break

    if found:
        save_config(cfg)
        bot_manager.reload_config()
        state_str = 'enabled' if new_state else 'paused'
        bot_manager.log_activity("info", f"👤 Master trader {trader_name} {state_str}.")
        return jsonify({"success": True, "enabled": new_state, "message": f"Trader {trader_name} {state_str}."})
    return jsonify({"success": False, "message": "Trader not found"}), 404


@app.route("/api/traders/scan", methods=["POST"])
def api_traders_scan():
    data = request.get_json(silent=True) or {}
    period = data.get("period", "7d")
    top_n = int(data.get("top", 25))
    cfg = load_config()
    try:
        from scanner import LeaderboardScanner
        scanner = LeaderboardScanner(bullpen_path=cfg.bullpen_path)
        traders = scanner.fetch_top_traders(time_period=period, limit=100)
        if traders:
            cfg.traders = traders[:top_n]
            save_config(cfg)
            bot_manager.reload_config()
            bot_manager.log_activity("info", f"🎯 Escaneou e atualizou os top {len(cfg.traders)} master traders ({period}).")
            return jsonify({
                "success": True,
                "count": len(cfg.traders),
                "message": f"Atualizado com sucesso com os top {len(cfg.traders)} traders ({period})!",
                "traders": [t.__dict__ for t in cfg.traders]
            })
        return jsonify({"success": False, "message": "Nenhum trader encontrado com os filtros atuais."}), 404
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/wallet/info", methods=["GET"])
def api_wallet_info():
    info = bot_manager.get_wallet_info()
    return jsonify(info)


@app.route("/api/wallet/sync", methods=["POST"])
def api_wallet_sync():
    res = bot_manager.sync_live_wallet_balance()
    return jsonify(res)


@app.route("/api/config/update", methods=["POST"])
def api_config_update():
    data = request.get_json(silent=True) or {}
    cfg = load_config()
    
    if "dry_run" in data:
        cfg.dry_run = bool(data["dry_run"])
    if "paper_initial_cash_usd" in data:
        cfg.paper_initial_cash_usd = float(data["paper_initial_cash_usd"])
    if "live_initial_cash_usd" in data:
        new_live_cash = float(data["live_initial_cash_usd"])
        cfg.live_initial_cash_usd = new_live_cash
        if hasattr(bot_manager, "tracker") and "live" in bot_manager.tracker.portfolio:
            bot_manager.tracker.portfolio["live"]["initial_cash_usd"] = new_live_cash
            if bot_manager.tracker.portfolio["live"].get("cash_usd", 0.0) == 0.0 or bot_manager.tracker.portfolio["live"].get("total_trades_count", 0) == 0:
                bot_manager.tracker.portfolio["live"]["cash_usd"] = new_live_cash
            bot_manager.tracker._save_portfolio_state()
    if "fixed_amount_usd" in data:
        cfg.sizing.fixed_amount_usd = float(data["fixed_amount_usd"])
    if "daily_budget_usd" in data:
        cfg.risk.daily_budget_usd = float(data["daily_budget_usd"])
    if "max_per_market_usd" in data:
        cfg.risk.max_per_market_usd = float(data["max_per_market_usd"])
    if "slippage_tolerance_pct" in data:
        cfg.risk.slippage_tolerance_pct = float(data["slippage_tolerance_pct"])

    save_config(cfg)
    bot_manager.reload_config()
    bot_manager.log_activity("info", "⚙️ Configurações e limites de risco atualizados.")
    return jsonify({"success": True, "message": "Configurações salvas com sucesso."})


# =====================================================================
# DASHBOARD HTML TEMPLATE
# =====================================================================

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="pt-BR" class="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Polymarket Copytrading Dashboard</title>
  <!-- Tailwind CSS -->
  <script src="https://cdn.tailwindcss.com"></script>
  <script>
    tailwind.config = {
      darkMode: 'class',
      theme: {
        extend: {
          colors: {
            brand: {
              50: '#ecfeff',
              100: '#cffafe',
              400: '#22d3ee',
              500: '#06b6d4',
              600: '#0891b2',
              900: '#164e63',
            },
            darkbg: '#090d16',
            cardbg: '#111827',
            bordercol: '#1f293d',
          }
        }
      }
    }
  </script>
  <!-- Chart.js & Lucide Icons -->
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <script src="https://unpkg.com/lucide@latest"></script>
  <style>
    body {
      background-color: #090d16;
      color: #f3f4f6;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    }
    .glass-card {
      background: rgba(17, 24, 39, 0.85);
      backdrop-filter: blur(12px);
      border: 1px solid #1f293d;
    }
    .pulse-dot {
      animation: pulse-glow 2s cubic-bezier(0.4, 0, 0.6, 1) infinite;
    }
    @keyframes pulse-glow {
      0%, 100% { opacity: 1; transform: scale(1); }
      50% { opacity: .4; transform: scale(1.15); }
    }
    .tab-active {
      border-bottom: 2px solid #06b6d4;
      color: #22d3ee;
      font-weight: 700;
    }
    /* Toast animations */
    @keyframes slideInRight {
      from { transform: translateX(120%); opacity: 0; }
      to { transform: translateX(0); opacity: 1; }
    }
    @keyframes slideOutRight {
      from { transform: translateX(0); opacity: 1; }
      to { transform: translateX(120%); opacity: 0; }
    }
    .toast-animate-in {
      animation: slideInRight 0.28s cubic-bezier(0.16, 1, 0.3, 1) forwards;
    }
    .toast-animate-out {
      animation: slideOutRight 0.22s cubic-bezier(0.7, 0, 0.84, 0) forwards;
    }
  </style>
</head>
<body class="min-h-screen flex flex-col antialiased">

  <!-- TOAST NOTIFICATION CONTAINER -->
  <div id="toast-container" class="fixed top-5 right-5 z-50 flex flex-col space-y-2.5 max-w-sm w-full pointer-events-none"></div>

  <!-- TOP NAVIGATION BAR -->
  <header class="border-b border-bordercol glass-card sticky top-0 z-40">
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center justify-between">
      
      <!-- Brand & Status -->
      <div class="flex items-center space-x-4">
        <div class="flex items-center space-x-2">
          <div class="w-9 h-9 rounded-lg bg-gradient-to-tr from-cyan-600 to-blue-500 flex items-center justify-center shadow-lg shadow-cyan-500/20">
            <svg class="w-5 h-5 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 2a2 2 0 012 2v2a2 2 0 01-2 2 2 2 0 01-2-2V4a2 2 0 012-2zM4 11a2 2 0 012-2h12a2 2 0 012 2v7a2 2 0 01-2 2H6a2 2 0 01-2-2v-7zM9 16h6M9 12h.01M15 12h.01"></path></svg>
          </div>
          <div>
            <h1 class="text-base font-bold text-white tracking-wide flex items-center gap-2">
              POLYMARKET <span class="text-cyan-400">COPYTRADER</span>
            </h1>
            <p class="text-xs text-gray-400">Painel de Gestão & Métricas Separadas</p>
          </div>
        </div>

        <!-- Live Status Pill -->
        <div id="status-pill" class="flex items-center space-x-2 px-3 py-1 rounded-full text-xs font-semibold bg-gray-800 text-gray-300 border border-gray-700 transition-all duration-300">
          <span id="status-dot" class="w-2.5 h-2.5 rounded-full bg-gray-500"></span>
          <span id="status-text">CARREGANDO...</span>
        </div>
      </div>

      <!-- Action Controls -->
      <div class="flex items-center space-x-3">
        <!-- Mode Switcher -->
        <div class="flex items-center bg-gray-900 border border-gray-800 rounded-lg p-1 text-xs">
          <button id="btn-mode-paper" onclick="setMode('paper')" class="px-3 py-1 rounded font-medium transition bg-cyan-600 text-white flex items-center gap-1.5">
            <span>🧪</span> Dinheiro Fake
          </button>
          <button id="btn-mode-live" onclick="setMode('live')" class="px-3 py-1 rounded font-medium transition text-gray-400 hover:text-white flex items-center gap-1.5">
            <span>⚡</span> Dinheiro Real
          </button>
        </div>

        <!-- Start / Stop Button -->
        <button id="btn-power" onclick="toggleBotPower()" class="flex items-center space-x-2 px-4 py-2 rounded-lg font-bold text-sm shadow-lg transition duration-200 transform active:scale-95 bg-emerald-600 hover:bg-emerald-500 text-white shadow-emerald-600/20 disabled:opacity-60 disabled:cursor-not-allowed">
          <span id="power-icon-container" class="flex items-center justify-center">
            <svg class="w-4 h-4 fill-current" viewBox="0 0 24 24"><polygon points="6 3 20 12 6 21 6 3"></polygon></svg>
          </span>
          <span id="power-text">INICIAR BOT</span>
        </button>

        <!-- Refresh Button -->
        <button id="btn-refresh" onclick="manualRefresh()" class="p-2 rounded-lg bg-gray-800 border border-gray-700 text-gray-300 hover:text-white hover:bg-gray-700 transition" title="Atualizar Dados">
          <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"></path></svg>
        </button>
      </div>

    </div>
  </header>

  <!-- MAIN CONTAINER -->
  <main class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6 flex-1 w-full space-y-6">

    <!-- COMPARATIVE SUMMARY BAR (FAKE VS REAL SELECTOR) -->
    <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
      
      <!-- Card: Dinheiro Fake (Paper) -->
      <div id="summary-card-paper" onclick="setViewMode('paper')" class="glass-card rounded-xl p-4 border-2 border-cyan-500 bg-cyan-950/25 cursor-pointer hover:border-cyan-400 transition-all shadow-lg shadow-cyan-950/30">
        <div class="flex items-center justify-between">
          <div class="flex items-center space-x-2.5">
            <span class="p-2 rounded-lg bg-cyan-900/60 text-cyan-400 text-base">🧪</span>
            <div>
              <div class="flex items-center gap-2">
                <h3 class="text-sm font-bold text-white">Dinheiro Fake (Simulação)</h3>
                <span id="pill-active-paper" class="px-2 py-0.5 rounded-full text-[10px] font-bold bg-cyan-500 text-gray-950">ATIVO NA TELA</span>
              </div>
              <p class="text-xs text-gray-400">Saldo virtual de teste & PnL simulado</p>
            </div>
          </div>
          <div class="text-right">
            <div id="mini-equity-paper" class="text-base font-black text-cyan-400">$1,000.00</div>
            <div id="mini-pnl-paper" class="text-xs font-bold text-emerald-400">+$0.00 PnL</div>
          </div>
        </div>
        <div class="mt-3 pt-2.5 border-t border-gray-800 flex items-center justify-between text-xs text-gray-400 font-mono">
          <span>Trades: <strong id="mini-trades-paper" class="text-white">0</strong></span>
          <span>Taxa de Acerto: <strong id="mini-wr-paper" class="text-emerald-400">0.0%</strong></span>
          <span>Caixa: <strong id="mini-cash-paper" class="text-gray-300">$1,000.00</strong></span>
        </div>
      </div>

      <!-- Card: Dinheiro Real (Live) -->
      <div id="summary-card-live" onclick="setViewMode('live')" class="glass-card rounded-xl p-4 border border-gray-800 hover:border-emerald-500/60 cursor-pointer transition-all">
        <div class="flex items-center justify-between">
          <div class="flex items-center space-x-2.5">
            <span class="p-2 rounded-lg bg-emerald-900/40 text-emerald-400 text-base">⚡</span>
            <div>
              <div class="flex items-center gap-2">
                <h3 class="text-sm font-bold text-white">Dinheiro Real (Live Trading)</h3>
                <span id="pill-active-live" class="hidden px-2 py-0.5 rounded-full text-[10px] font-bold bg-emerald-500 text-gray-950">ATIVO NA TELA</span>
              </div>
              <p class="text-xs text-gray-400">Carteira: <span class="font-mono text-emerald-400" title="0xcc78a24c4856c0f195ad26354d549255b5f2ab18">0xcc78...ab18</span> (Polygon)</p>
            </div>
          </div>
          <div class="text-right">
            <div id="mini-equity-live" class="text-base font-black text-emerald-400">$0.00</div>
            <div id="mini-pnl-live" class="text-xs font-bold text-gray-400">$0.00 PnL</div>
          </div>
        </div>
        <div class="mt-3 pt-2.5 border-t border-gray-800 flex items-center justify-between text-xs text-gray-400 font-mono">
          <span>Trades: <strong id="mini-trades-live" class="text-white">0</strong></span>
          <span>Taxa de Acerto: <strong id="mini-wr-live" class="text-emerald-400">0.0%</strong></span>
          <div class="flex items-center gap-2">
            <span>Caixa: <strong id="mini-cash-live" class="text-gray-300">$0.00</strong></span>
            <button onclick="event.stopPropagation(); syncWalletBalance();" class="px-2 py-0.5 rounded bg-emerald-950 hover:bg-emerald-900 border border-emerald-700 text-emerald-300 font-sans text-[11px] flex items-center gap-1 font-semibold transition" title="Sincronizar saldo da carteira on-chain (Polygon)">
              <span>🔄</span> Sincronizar
            </button>
          </div>
        </div>
      </div>

    </div>

    <!-- VIEW MODE INDICATOR HEADER -->
    <div id="view-banner" class="p-3 rounded-xl bg-cyan-950/30 border border-cyan-800/80 flex items-center justify-between text-xs transition-colors">
      <div class="flex items-center space-x-2">
        <span id="view-banner-icon" class="text-base">🧪</span>
        <div>
          <span class="text-gray-400 font-medium">Exibindo Estatísticas de:</span>
          <strong id="view-banner-title" class="text-cyan-400 font-bold ml-1 text-sm">DINHEIRO FAKE (MODO SIMULAÇÃO)</strong>
        </div>
      </div>
      <div class="flex items-center space-x-2 text-gray-400">
        <span>Modo de Execução Atual do Robô:</span>
        <strong id="bot-engine-badge" class="px-2 py-0.5 rounded font-bold bg-cyan-900 text-cyan-300 border border-cyan-700 uppercase">SIMULAÇÃO</strong>
      </div>
    </div>

    <!-- OVERVIEW STAT CARDS (6 METRICS FOR SELECTED VIEW MODE) -->
    <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-6 gap-4">
      
      <!-- Card 1: Realized PnL -->
      <div id="card-stat-pnl" class="glass-card rounded-xl p-4 flex flex-col justify-between border-t-2 border-t-cyan-500">
        <div class="flex items-center justify-between text-gray-400 text-xs font-medium">
          <span id="label-pnl">Realized PnL (Fake)</span>
          <span class="text-cyan-400 font-bold">$</span>
        </div>
        <div class="mt-2">
          <div id="stat-pnl" class="text-2xl font-black text-emerald-400">+$0.00</div>
          <p id="stat-pnl-sub" class="text-xs text-gray-400 mt-1">Lucro líquido realizado</p>
        </div>
      </div>

      <!-- Card 2: Win Rate -->
      <div id="card-stat-winrate" class="glass-card rounded-xl p-4 flex flex-col justify-between border-t-2 border-t-emerald-500">
        <div class="flex items-center justify-between text-gray-400 text-xs font-medium">
          <span id="label-winrate">Win Rate (Fake)</span>
          <span class="text-emerald-400 font-bold">%</span>
        </div>
        <div class="mt-2">
          <div id="stat-winrate" class="text-2xl font-black text-emerald-400">0.0%</div>
          <div class="w-full bg-gray-800 h-1.5 rounded-full mt-2 overflow-hidden">
            <div id="stat-winrate-bar" class="bg-emerald-500 h-full rounded-full transition-all duration-500" style="width: 0%"></div>
          </div>
        </div>
      </div>

      <!-- Card 3: Winners vs Losers -->
      <div id="card-stat-wl" class="glass-card rounded-xl p-4 flex flex-col justify-between border-t-2 border-t-purple-500">
        <div class="flex items-center justify-between text-gray-400 text-xs font-medium">
          <span id="label-wl">Wins / Losses (Fake)</span>
          <span class="text-purple-400 font-bold">W/L</span>
        </div>
        <div class="mt-2">
          <div class="flex items-baseline space-x-2">
            <span id="stat-wins" class="text-2xl font-black text-emerald-400">0W</span>
            <span class="text-gray-500">/</span>
            <span id="stat-losses" class="text-2xl font-black text-rose-400">0L</span>
          </div>
          <p id="stat-profit-factor" class="text-xs text-gray-400 mt-1">Profit Factor: 1.0x</p>
        </div>
      </div>

      <!-- Card 4: Total Trades -->
      <div id="card-stat-trades" class="glass-card rounded-xl p-4 flex flex-col justify-between border-t-2 border-t-blue-500">
        <div class="flex items-center justify-between text-gray-400 text-xs font-medium">
          <span id="label-trades">Copied Trades (Fake)</span>
          <span class="text-blue-400 font-bold">#</span>
        </div>
        <div class="mt-2">
          <div id="stat-total-trades" class="text-2xl font-black text-white">0</div>
          <p id="stat-trades-split" class="text-xs text-gray-400 mt-1">0 Executados • 0 Falhas</p>
        </div>
      </div>

      <!-- Card 5: Total Equity & Cash -->
      <div id="card-stat-equity" class="glass-card rounded-xl p-4 flex flex-col justify-between border-t-2 border-t-amber-500">
        <div class="flex items-center justify-between text-gray-400 text-xs font-medium">
          <span id="label-equity">Total Equity (Fake)</span>
          <span class="text-amber-400 font-bold">EQ</span>
        </div>
        <div class="mt-2">
          <div id="stat-equity" class="text-2xl font-black text-amber-400">$1,000.00</div>
          <p id="stat-cash" class="text-xs text-gray-400 mt-1">Caixa: $1,000.00</p>
        </div>
      </div>

      <!-- Card 6: Daily Budget Utilization -->
      <div id="card-stat-budget" class="glass-card rounded-xl p-4 flex flex-col justify-between border-t-2 border-t-rose-500">
        <div class="flex items-center justify-between text-gray-400 text-xs font-medium">
          <span id="label-budget">Daily Budget Cap (Fake)</span>
          <span class="text-rose-400 font-bold">CAP</span>
        </div>
        <div class="mt-2">
          <div id="stat-budget" class="text-xl font-black text-white">$0.00 / $100</div>
          <div class="w-full bg-gray-800 h-1.5 rounded-full mt-2 overflow-hidden">
            <div id="stat-budget-bar" class="bg-rose-500 h-full rounded-full transition-all duration-500" style="width: 0%"></div>
          </div>
        </div>
      </div>

    </div>

    <!-- CHARTS & LIVE STATUS SECTION -->
    <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
      <!-- Chart 1: Equity & Cumulative PnL Curve -->
      <div class="glass-card rounded-xl p-5 lg:col-span-2 flex flex-col justify-between">
        <div class="flex items-center justify-between mb-4">
          <div>
            <h2 id="chart-title" class="text-sm font-bold text-white flex items-center gap-2">
              Evolução Patrimonial & Curva de PnL (Dinheiro Fake)
            </h2>
            <p id="chart-subtitle" class="text-xs text-gray-400">Trajetória histórica atualizada a cada trade de simulação</p>
          </div>
          <span id="badge-chart-status" class="text-xs font-semibold px-2 py-0.5 rounded bg-cyan-950 text-cyan-400 border border-cyan-800 flex items-center gap-1.5">
            <span class="w-1.5 h-1.5 rounded-full bg-cyan-400 pulse-dot"></span>
            Tempo Real
          </span>
        </div>
        <div class="h-64 w-full relative">
          <canvas id="equityChart"></canvas>
        </div>
      </div>

      <!-- Quick Info / Status Box -->
      <div class="glass-card rounded-xl p-5 flex flex-col justify-between space-y-4">
        <div>
          <h2 class="text-sm font-bold text-white flex items-center gap-2 mb-3">
            Resumo de Execução do Robô
          </h2>
          <div class="space-y-2.5 text-xs">
            <div class="flex justify-between py-1.5 border-b border-gray-800">
              <span class="text-gray-400">Status do Bot:</span>
              <span id="info-status" class="font-bold text-gray-400">🔴 PARADO</span>
            </div>
            <div class="flex justify-between py-1.5 border-b border-gray-800">
              <span class="text-gray-400">Tempo de Execução:</span>
              <span id="info-uptime" class="font-bold text-white">0s</span>
            </div>
            <div class="flex justify-between py-1.5 border-b border-gray-800">
              <span class="text-gray-400">Modo em Operação:</span>
              <span id="info-exec-mode" class="font-bold text-cyan-400">🧪 SIMULAÇÃO (FAKE)</span>
            </div>
            <div class="flex justify-between py-1.5 border-b border-gray-800">
              <span class="text-gray-400">Master Traders Ativos:</span>
              <span id="info-traders" class="font-bold text-cyan-400">25 / 25</span>
            </div>
            <div class="flex justify-between py-1.5 border-b border-gray-800">
              <span class="text-gray-400">Posições Abertas (Modo Atual):</span>
              <span id="info-positions" class="font-bold text-emerald-400">0 Mercados</span>
            </div>
            <div class="flex justify-between py-1.5 border-b border-gray-800">
              <span class="text-gray-400">Valor por Trade Copiado:</span>
              <span id="info-sizing" class="font-bold text-yellow-400">$10.00 Fixo</span>
            </div>
            <div class="flex justify-between py-1.5">
              <span class="text-gray-400">Venda Espelho Proporcional:</span>
              <span class="font-bold text-green-400">Ativada</span>
            </div>
          </div>
        </div>

        <div id="live-listener-card" class="bg-gray-900/80 rounded-lg p-3 border border-gray-800 flex items-center space-x-3 transition-colors duration-300">
          <div id="listener-icon-bg" class="p-2 rounded bg-gray-800 text-gray-400">
            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"></path></svg>
          </div>
          <div class="text-xs flex-1">
            <p id="listener-title" class="text-gray-300 font-semibold">Monitor de Feed Inativo</p>
            <p id="listener-subtitle" class="text-gray-500">Clique em Iniciar Bot para monitorar (<span id="info-poll-int">5</span>s)</p>
          </div>
        </div>
      </div>
    </div>

    <!-- LIVE ACTIVITY & EVENT STREAM TERMINAL -->
    <div class="glass-card rounded-xl p-4 border border-bordercol space-y-3">
      <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2 border-b border-gray-800 pb-3">
        <div class="flex items-center space-x-3">
          <div class="p-1.5 rounded-lg bg-cyan-950 text-cyan-400 border border-cyan-800 flex items-center justify-center">
            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><polyline points="4 17 10 11 4 5"></polyline><line x1="12" y1="19" x2="20" y2="19"></line></svg>
          </div>
          <div>
            <h2 class="text-sm font-bold text-white flex items-center gap-2">
              Terminal de Atividade & Ciclos de Polling em Tempo Real
              <span id="live-pulse-badge" class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[10px] font-bold bg-gray-800 text-gray-400 border border-gray-700 transition-all">
                <span id="live-pulse-dot" class="w-1.5 h-1.5 rounded-full bg-gray-500"></span>
                <span id="live-pulse-text">STANDBY</span>
              </span>
            </h2>
            <p class="text-xs text-gray-400">Monitoramento dos sinais dos master traders, validações de risco e execuções</p>
          </div>
        </div>
        <div class="flex items-center space-x-3 text-xs">
          <div id="cycle-badge" class="px-2.5 py-1 rounded bg-gray-900 border border-gray-800 text-gray-300 font-mono">
            Ciclos: <span id="val-total-cycles" class="font-bold text-cyan-400">0</span>
          </div>
          <div id="last-poll-badge" class="px-2.5 py-1 rounded bg-gray-900 border border-gray-800 text-gray-400 font-mono text-[11px]">
            Último poll: <span id="val-last-poll" class="text-gray-300">Nunca</span>
          </div>
          <button onclick="clearActivityLogs()" class="text-gray-400 hover:text-white p-1 rounded hover:bg-gray-800 transition" title="Limpar log">
            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"></path></svg>
          </button>
        </div>
      </div>
      <!-- Terminal feed window -->
      <div id="activity-log-terminal" class="h-36 overflow-y-auto font-mono text-xs space-y-1.5 pr-2 bg-gray-950/90 rounded-lg p-3 border border-gray-900 select-text">
        <div class="text-gray-500 italic">Conectando ao feed de atividades...</div>
      </div>
    </div>

    <!-- TABS NAVIGATION -->
    <div class="border-b border-bordercol flex space-x-8 text-sm">
      <button onclick="switchTab('trades')" id="tab-btn-trades" class="pb-3 tab-active flex items-center gap-2">
        <span>📋</span> Feed de Operações (<span id="tab-trades-count">0</span>)
      </button>
      <button onclick="switchTab('positions')" id="tab-btn-positions" class="pb-3 text-gray-400 hover:text-gray-200 flex items-center gap-2">
        <span>📊</span> Posições Abertas (<span id="tab-pos-count">0</span>)
      </button>
      <button onclick="switchTab('traders')" id="tab-btn-traders" class="pb-3 text-gray-400 hover:text-gray-200 flex items-center gap-2">
        <span>👥</span> Top 25 Master Traders (<span id="tab-traders-count">25</span>)
      </button>
      <button onclick="switchTab('settings')" id="tab-btn-settings" class="pb-3 text-gray-400 hover:text-gray-200 flex items-center gap-2">
        <span>⚙️</span> Configurações & Gestão de Risco
      </button>
    </div>

    <!-- TAB 1: TRADES FEED -->
    <div id="tab-content-trades" class="space-y-4">
      <div class="flex flex-col sm:flex-row items-center justify-between gap-3">
        <div class="flex items-center space-x-2 overflow-x-auto max-w-full pb-1 sm:pb-0">
          <button onclick="filterTrades('ALL')" class="trade-filter-btn px-3 py-1 rounded-md text-xs font-semibold bg-cyan-600 text-white" data-filter="ALL">Todos</button>
          <button onclick="filterTrades('PAPER_ONLY')" class="trade-filter-btn px-3 py-1 rounded-md text-xs font-semibold bg-gray-800 text-gray-400 hover:text-white" data-filter="PAPER_ONLY">🧪 Somente Fake</button>
          <button onclick="filterTrades('LIVE_ONLY')" class="trade-filter-btn px-3 py-1 rounded-md text-xs font-semibold bg-gray-800 text-gray-400 hover:text-white" data-filter="LIVE_ONLY">⚡ Somente Real</button>
          <button onclick="filterTrades('EXECUTED')" class="trade-filter-btn px-3 py-1 rounded-md text-xs font-semibold bg-gray-800 text-gray-400 hover:text-white" data-filter="EXECUTED">Executados</button>
          <button onclick="filterTrades('BUY')" class="trade-filter-btn px-3 py-1 rounded-md text-xs font-semibold bg-gray-800 text-gray-400 hover:text-white" data-filter="BUY">Compras</button>
          <button onclick="filterTrades('SELL')" class="trade-filter-btn px-3 py-1 rounded-md text-xs font-semibold bg-gray-800 text-gray-400 hover:text-white" data-filter="SELL">Vendas</button>
          <button onclick="filterTrades('SKIPPED')" class="trade-filter-btn px-3 py-1 rounded-md text-xs font-semibold bg-gray-800 text-gray-400 hover:text-white" data-filter="SKIPPED">Ignorados</button>
          <button onclick="filterTrades('FAILED')" class="trade-filter-btn px-3 py-1 rounded-md text-xs font-semibold bg-gray-800 text-gray-400 hover:text-white" data-filter="FAILED">Falhas</button>
        </div>
        <div class="relative w-full sm:w-64">
          <input type="text" id="trade-search" oninput="renderTradesTable()" placeholder="Filtrar mercado, trader ou desfecho..." class="w-full bg-gray-900 border border-gray-800 rounded-lg px-3 py-1.5 text-xs text-white placeholder-gray-500 focus:outline-none focus:border-cyan-500">
        </div>
      </div>

      <!-- Trades Table -->
      <div class="glass-card rounded-xl overflow-hidden border border-bordercol">
        <div class="overflow-x-auto">
          <table class="w-full text-left text-xs">
            <thead class="bg-gray-900/90 text-gray-400 border-b border-bordercol uppercase tracking-wider font-semibold">
              <tr>
                <th class="px-4 py-3">Horário (UTC)</th>
                <th class="px-4 py-3 text-center">Modo</th>
                <th class="px-4 py-3">Master Trader</th>
                <th class="px-4 py-3">Ação</th>
                <th class="px-4 py-3">Mercado / URL Polymarket</th>
                <th class="px-4 py-3">Desfecho</th>
                <th class="px-4 py-3 text-right">Tamanho Mestre</th>
                <th class="px-4 py-3 text-right">Copiado</th>
                <th class="px-4 py-3 text-right">Preço</th>
                <th class="px-4 py-3 text-center">Status</th>
                <th class="px-4 py-3">Detalhes / Resultado</th>
              </tr>
            </thead>
            <tbody id="trades-tbody" class="divide-y divide-gray-800">
              <tr>
                <td colspan="11" class="px-4 py-8 text-center text-gray-500">
                  Carregando histórico de operações...
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- TAB 2: OPEN POSITIONS -->
    <div id="tab-content-positions" class="hidden space-y-4">
      <div class="flex items-center justify-between bg-gray-900/60 p-3 rounded-xl border border-bordercol text-xs">
        <div class="flex items-center space-x-2">
          <span class="text-gray-400">Exibindo posições abertas de:</span>
          <strong id="positions-mode-label" class="text-cyan-400 font-bold uppercase">🧪 Dinheiro Fake (Simulação)</strong>
        </div>
        <div class="flex items-center gap-2">
          <button onclick="setViewMode('paper')" class="px-2.5 py-1 rounded bg-cyan-900 text-cyan-300 font-semibold border border-cyan-700">Ver Fake</button>
          <button onclick="setViewMode('live')" class="px-2.5 py-1 rounded bg-emerald-900 text-emerald-300 font-semibold border border-emerald-700">Ver Real</button>
        </div>
      </div>

      <div class="glass-card rounded-xl overflow-hidden border border-bordercol">
        <div class="overflow-x-auto">
          <table class="w-full text-left text-xs">
            <thead class="bg-gray-900/90 text-gray-400 border-b border-bordercol uppercase tracking-wider font-semibold">
              <tr>
                <th class="px-4 py-3">Mercado / URL Polymarket</th>
                <th class="px-4 py-3">Desfecho</th>
                <th class="px-4 py-3 text-right">Cotas Detidas</th>
                <th class="px-4 py-3 text-right">Preço Médio</th>
                <th class="px-4 py-3 text-right">Custo Total</th>
                <th class="px-4 py-3 text-right">Valor Estimado</th>
              </tr>
            </thead>
            <tbody id="positions-tbody" class="divide-y divide-gray-800">
              <tr>
                <td colspan="6" class="px-4 py-8 text-center text-gray-500">
                  Nenhuma posição aberta no momento.
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- TAB 3: TOP 25 MASTER TRADERS -->
    <div id="tab-content-traders" class="hidden space-y-4">
      <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 bg-gray-900/60 p-4 rounded-xl border border-bordercol">
        <div>
          <h3 class="text-sm font-bold text-white flex items-center gap-2">
            <span>👥</span> Gerenciamento de Master Traders
          </h3>
          <p class="text-xs text-gray-400 mt-0.5">
            Traders ativos salvos no <span class="font-mono text-cyan-400">config.json</span>. Você pode ativar/pausar individualmente ou escanear o ranking da Polymarket.
          </p>
        </div>
        <div class="flex items-center gap-2">
          <select id="scan-period-select" class="bg-gray-800 border border-gray-700 text-gray-200 text-xs rounded-lg px-3 py-1.5 focus:outline-none focus:border-cyan-500">
            <option value="7d">Últimos 7 Dias (7D)</option>
            <option value="30d">Últimos 30 Dias (30D)</option>
            <option value="all">Todo o Período (All-Time)</option>
          </select>
          <button onclick="scanTradersFromDashboard()" id="btn-scan-traders" class="px-3 py-1.5 rounded-lg text-xs font-bold bg-cyan-600 hover:bg-cyan-500 text-white flex items-center gap-1.5 shadow-lg shadow-cyan-900/30 transition">
            <span id="scan-spinner" class="hidden">⏳</span>
            <span>🔄 Escanear & Atualizar Top 25</span>
          </button>
        </div>
      </div>

      <div class="glass-card rounded-xl overflow-hidden border border-bordercol">
        <div class="overflow-x-auto">
          <table class="w-full text-left text-xs">
            <thead class="bg-gray-900/90 text-gray-400 border-b border-bordercol uppercase tracking-wider font-semibold">
              <tr>
                <th class="px-4 py-3 text-center">#</th>
                <th class="px-4 py-3">Status Espelho</th>
                <th class="px-4 py-3">Nome do Trader</th>
                <th class="px-4 py-3">Endereço Carteira</th>
                <th class="px-4 py-3 text-right">Win Rate 7D</th>
                <th class="px-4 py-3 text-right">Lucro 7D</th>
                <th class="px-4 py-3 text-right">Volume 7D</th>
                <th class="px-4 py-3">Categoria</th>
                <th class="px-4 py-3">Risco</th>
                <th class="px-4 py-3">Estilo</th>
                <th class="px-4 py-3 text-center">Ação</th>
              </tr>
            </thead>
            <tbody id="traders-tbody" class="divide-y divide-gray-800">
              <!-- Loaded via JS -->
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- TAB 4: SETTINGS & RISK -->
    <div id="tab-content-settings" class="hidden space-y-4">
      <div class="glass-card rounded-xl p-6 max-w-2xl border border-bordercol">
        <h3 class="text-sm font-bold text-white mb-4 flex items-center gap-2">
          Configurações de Risco, Dimensionamento e Saldos
        </h3>
        <form id="form-settings" onsubmit="saveSettings(event)" class="space-y-4 text-xs">
          <div>
            <label class="block text-gray-400 mb-1">Valor Fixo por Trade Copiado (USD)</label>
            <input type="number" step="0.5" id="cfg-fixed-usd" class="w-full bg-gray-900 border border-gray-800 rounded-lg p-2 text-white" value="10.0">
          </div>
          <div>
            <label class="block text-gray-400 mb-1">Limite Máximo de Gasto Diário (USD)</label>
            <input type="number" step="1" id="cfg-daily-budget" class="w-full bg-gray-900 border border-gray-800 rounded-lg p-2 text-white" value="100.0">
          </div>
          <div>
            <label class="block text-gray-400 mb-1">Exposição Máxima por Mercado Único (USD)</label>
            <input type="number" step="1" id="cfg-max-market" class="w-full bg-gray-900 border border-gray-800 rounded-lg p-2 text-white" value="50.0">
          </div>
          <div>
            <label class="block text-gray-400 mb-1">Tolerância de Slippage (%)</label>
            <input type="number" step="0.1" id="cfg-slippage" class="w-full bg-gray-900 border border-gray-800 rounded-lg p-2 text-white" value="2.0">
          </div>
          <div class="grid grid-cols-2 gap-4">
            <div>
              <label class="block text-gray-400 mb-1">Saldo Inicial Fake / Simulação (USD)</label>
              <input type="number" step="10" id="cfg-paper-cash" class="w-full bg-gray-900 border border-gray-800 rounded-lg p-2 text-white" value="1000.0">
            </div>
            <div>
              <div class="flex items-center justify-between mb-1">
                <label class="block text-gray-400">Saldo Inicial Real (USD)</label>
                <button type="button" onclick="syncWalletBalance()" class="text-[11px] text-emerald-400 hover:text-emerald-300 flex items-center gap-1 font-semibold">
                  <span>🔄</span> Sincronizar On-chain
                </button>
              </div>
              <input type="number" step="1" id="cfg-live-cash" class="w-full bg-gray-900 border border-gray-800 rounded-lg p-2 text-white" value="0.0">
            </div>
          </div>
          <div class="pt-2">
            <button id="btn-save-settings" type="submit" class="px-4 py-2 bg-cyan-600 hover:bg-cyan-500 font-bold rounded-lg text-white transition flex items-center gap-2">
              <span>Salvar Configurações</span>
            </button>
          </div>
        </form>
      </div>
    </div>

  </main>

  <!-- JAVASCRIPT LOGIC -->
  <script>
    let currentFilter = 'ALL';
    let allTrades = [];
    let currentAnalytics = null;
    let viewMode = 'paper'; // 'paper' or 'live'
    let equityChart = null;
    let isBotRunning = false;
    let lastKnownCycleAt = null;

    // Toast notification helper
    function showToast(title, message, type = 'info', duration = 4000) {
      const container = document.getElementById('toast-container');
      if (!container) return;

      const toastId = 'toast-' + Math.random().toString(36).substr(2, 9);
      const toast = document.createElement('div');
      
      let borderCol = 'border-cyan-500/80 bg-gray-900/95 text-cyan-200';
      let iconSvg = '<svg class="w-4 h-4 text-cyan-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>';
      
      if (type === 'success') {
        borderCol = 'border-emerald-500 bg-gray-900/95 text-emerald-200 shadow-lg shadow-emerald-950/50';
        iconSvg = '<svg class="w-4 h-4 text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"></path></svg>';
      } else if (type === 'warning') {
        borderCol = 'border-amber-500 bg-gray-900/95 text-amber-200 shadow-lg shadow-amber-950/50';
        iconSvg = '<svg class="w-4 h-4 text-amber-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"></path></svg>';
      } else if (type === 'error') {
        borderCol = 'border-rose-500 bg-gray-900/95 text-rose-200 shadow-lg shadow-rose-950/50';
        iconSvg = '<svg class="w-4 h-4 text-rose-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path></svg>';
      }

      toast.id = toastId;
      toast.className = `pointer-events-auto p-3.5 rounded-xl border ${borderCol} backdrop-blur-md shadow-xl flex items-start space-x-3 text-xs toast-animate-in transition-all`;
      toast.innerHTML = `
        <div class="flex-shrink-0 mt-0.5">${iconSvg}</div>
        <div class="flex-1 min-w-0">
          <p class="font-bold text-white">${title}</p>
          <p class="text-gray-300 mt-0.5 leading-relaxed">${message}</p>
        </div>
        <button onclick="dismissToast('${toastId}')" class="text-gray-400 hover:text-white flex-shrink-0">
          <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path></svg>
        </button>
      `;

      container.appendChild(toast);

      setTimeout(() => {
        dismissToast(toastId);
      }, duration);
    }

    function dismissToast(toastId) {
      const toast = document.getElementById(toastId);
      if (toast) {
        toast.className = toast.className.replace('toast-animate-in', 'toast-animate-out');
        setTimeout(() => {
          if (toast && toast.parentNode) toast.parentNode.removeChild(toast);
        }, 200);
      }
    }

    // Safe Lucide icon renderer
    function safeCreateIcons() {
      try {
        if (typeof lucide !== 'undefined' && lucide && typeof lucide.createIcons === 'function') {
          lucide.createIcons();
        }
      } catch (e) {}
    }

    // Initialize Chart.js safely
    function initChart() {
      try {
        if (typeof Chart === 'undefined') return;
        const canvas = document.getElementById('equityChart');
        if (!canvas) return;
        const ctx = canvas.getContext('2d');
        equityChart = new Chart(ctx, {
          type: 'line',
          data: {
            labels: ['Start'],
            datasets: [{
              label: 'Total Equity ($)',
              data: [1000.0],
              borderColor: '#06b6d4',
              backgroundColor: 'rgba(6, 182, 212, 0.1)',
              borderWidth: 2,
              tension: 0.3,
              fill: true,
              pointRadius: 3,
              pointBackgroundColor: '#22d3ee'
            }]
          },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
              legend: { display: false },
              tooltip: {
                callbacks: {
                  label: function(ctx) { return '$' + (ctx.parsed.y || 0).toFixed(2); }
                }
              }
            },
            scales: {
              x: {
                grid: { color: '#1f2937' },
                ticks: { color: '#6b7280', font: { size: 10 } }
              },
              y: {
                grid: { color: '#1f2937' },
                ticks: {
                  color: '#6b7280',
                  font: { size: 10 },
                  callback: function(val) { return '$' + val; }
                }
              }
            }
          }
        });
      } catch (e) {
        console.warn('Chart init error:', e);
      }
    }

    // Tab switcher
    function switchTab(tabId) {
      ['trades', 'positions', 'traders', 'settings'].forEach(t => {
        const content = document.getElementById(`tab-content-${t}`);
        const btn = document.getElementById(`tab-btn-${t}`);
        if (t === tabId) {
          content.classList.remove('hidden');
          btn.className = 'pb-3 tab-active flex items-center gap-2';
        } else {
          content.classList.add('hidden');
          btn.className = 'pb-3 text-gray-400 hover:text-gray-200 flex items-center gap-2';
        }
      });
      safeCreateIcons();
    }

    // Switch between Viewing Paper vs Live Stats
    function setViewMode(mode) {
      viewMode = mode;
      
      const cardPaper = document.getElementById('summary-card-paper');
      const cardLive = document.getElementById('summary-card-live');
      const pillPaper = document.getElementById('pill-active-paper');
      const pillLive = document.getElementById('pill-active-live');
      
      const viewBanner = document.getElementById('view-banner');
      const viewBannerTitle = document.getElementById('view-banner-title');
      const viewBannerIcon = document.getElementById('view-banner-icon');
      const chartTitle = document.getElementById('chart-title');
      const chartSubtitle = document.getElementById('chart-subtitle');
      const posLabel = document.getElementById('positions-mode-label');

      if (mode === 'paper') {
        cardPaper.className = 'glass-card rounded-xl p-4 border-2 border-cyan-500 bg-cyan-950/25 cursor-pointer transition-all shadow-lg shadow-cyan-950/40';
        cardLive.className = 'glass-card rounded-xl p-4 border border-gray-800 hover:border-emerald-500/60 cursor-pointer transition-all';
        pillPaper.classList.remove('hidden');
        pillLive.classList.add('hidden');

        viewBanner.className = 'p-3 rounded-xl bg-cyan-950/30 border border-cyan-800/80 flex items-center justify-between text-xs transition-colors';
        viewBannerTitle.innerText = 'DINHEIRO FAKE (MODO SIMULAÇÃO)';
        viewBannerTitle.className = 'text-cyan-400 font-bold ml-1 text-sm';
        viewBannerIcon.innerText = '🧪';

        chartTitle.innerText = 'Evolução Patrimonial & Curva de PnL (Dinheiro Fake)';
        chartSubtitle.innerText = 'Trajetória histórica atualizada a cada trade de simulação';
        if (posLabel) posLabel.innerText = '🧪 Dinheiro Fake (Simulação)';

        document.getElementById('label-pnl').innerText = 'Realized PnL (Fake)';
        document.getElementById('label-winrate').innerText = 'Win Rate (Fake)';
        document.getElementById('label-wl').innerText = 'Wins / Losses (Fake)';
        document.getElementById('label-trades').innerText = 'Copied Trades (Fake)';
        document.getElementById('label-equity').innerText = 'Total Equity (Fake)';
        document.getElementById('label-budget').innerText = 'Daily Budget Cap (Fake)';
      } else {
        cardLive.className = 'glass-card rounded-xl p-4 border-2 border-emerald-500 bg-emerald-950/25 cursor-pointer transition-all shadow-lg shadow-emerald-950/40';
        cardPaper.className = 'glass-card rounded-xl p-4 border border-gray-800 hover:border-cyan-500/60 cursor-pointer transition-all';
        pillLive.classList.remove('hidden');
        pillPaper.classList.add('hidden');

        viewBanner.className = 'p-3 rounded-xl bg-emerald-950/30 border border-emerald-800/80 flex items-center justify-between text-xs transition-colors';
        viewBannerTitle.innerText = 'DINHEIRO REAL (LIVE CAPITAL)';
        viewBannerTitle.className = 'text-emerald-400 font-bold ml-1 text-sm';
        viewBannerIcon.innerText = '⚡';

        chartTitle.innerText = 'Evolução Patrimonial & Curva de PnL (Dinheiro Real)';
        chartSubtitle.innerText = 'Trajetória histórica de operações reais na Polymarket';
        if (posLabel) posLabel.innerText = '⚡ Dinheiro Real (Live Capital)';

        document.getElementById('label-pnl').innerText = 'Realized PnL (Real)';
        document.getElementById('label-winrate').innerText = 'Win Rate (Real)';
        document.getElementById('label-wl').innerText = 'Wins / Losses (Real)';
        document.getElementById('label-trades').innerText = 'Copied Trades (Real)';
        document.getElementById('label-equity').innerText = 'Total Equity (Real)';
        document.getElementById('label-budget').innerText = 'Daily Budget Cap (Real)';
      }

      if (currentAnalytics) {
        renderAnalytics(currentAnalytics);
      }
      loadPositions();
    }

    // Toggle Bot Power (Start / Stop) with INSTANT visual feedback
    async function toggleBotPower() {
      const powerBtn = document.getElementById('btn-power');
      const powerText = document.getElementById('power-text');
      const iconContainer = document.getElementById('power-icon-container');

      const willStart = !isBotRunning;

      powerBtn.disabled = true;
      if (willStart) {
        powerBtn.className = 'flex items-center space-x-2 px-4 py-2 rounded-lg font-bold text-sm shadow-lg transition duration-200 bg-emerald-700 text-white cursor-wait opacity-90';
        powerText.innerText = 'INICIANDO...';
        iconContainer.innerHTML = '<svg class="animate-spin w-4 h-4 text-white" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"></path></svg>';
        
        document.getElementById('status-pill').className = 'flex items-center space-x-2 px-3 py-1 rounded-full text-xs font-semibold bg-emerald-950/80 text-emerald-400 border border-emerald-800';
        document.getElementById('status-dot').className = 'w-2.5 h-2.5 rounded-full bg-emerald-400 animate-ping';
        document.getElementById('status-text').innerText = 'INICIANDO WORKER...';
        
        showToast('Iniciando Robô', 'Conectando ao feed dos master traders...', 'info');
      } else {
        powerBtn.className = 'flex items-center space-x-2 px-4 py-2 rounded-lg font-bold text-sm shadow-lg transition duration-200 bg-rose-700 text-white cursor-wait opacity-90';
        powerText.innerText = 'PARANDO...';
        iconContainer.innerHTML = '<svg class="animate-spin w-4 h-4 text-white" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"></path></svg>';
        
        showToast('Parando Robô', 'Interrompendo ciclo de monitoramento...', 'info');
      }

      try {
        const endpoint = willStart ? '/api/bot/start' : '/api/bot/stop';
        const res = await fetch(endpoint, { method: 'POST' });
        const data = await res.json();

        if (data.success) {
          showToast(
            willStart ? '🚀 Robô Ativo' : '⏹️ Robô Parado',
            data.message || (willStart ? 'Robô iniciado com sucesso.' : 'Robô parado com sucesso.'),
            willStart ? 'success' : 'warning'
          );
        } else {
          showToast('Aviso', data.message || 'Operação não pôde ser concluída.', 'warning');
        }
      } catch (err) {
        showToast('Erro de Comunicação', 'Falha ao conectar com o servidor: ' + err.message, 'error');
      } finally {
        powerBtn.disabled = false;
        await refreshAllData();
      }
    }

    // Set Execution Mode (Paper / Live)
    async function setMode(mode) {
      const dryRun = (mode === 'paper');
      const btnPaper = document.getElementById('btn-mode-paper');
      const btnLive = document.getElementById('btn-mode-live');

      if (dryRun) {
        btnPaper.className = 'px-3 py-1 rounded font-medium transition bg-cyan-600 text-white flex items-center gap-1.5';
        btnLive.className = 'px-3 py-1 rounded font-medium transition text-gray-400 hover:text-white flex items-center gap-1.5';
      } else {
        btnLive.className = 'px-3 py-1 rounded font-medium transition bg-rose-600 text-white flex items-center gap-1.5';
        btnPaper.className = 'px-3 py-1 rounded font-medium transition text-gray-400 hover:text-white flex items-center gap-1.5';
      }

      showToast(
        dryRun ? '🧪 Modo Dinheiro Fake' : '⚡ Modo Dinheiro Real',
        dryRun ? 'Modo de simulação ativado. Nenhum saldo real será movimentado.' : 'Modo REAL ativado! As operações serão espelhadas na Polymarket com dinheiro real.',
        dryRun ? 'info' : 'warning'
      );

      try {
        await fetch('/api/config/update', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ dry_run: dryRun })
        });
      } catch (err) {
        showToast('Erro', 'Falha ao atualizar modo: ' + err.message, 'error');
      }
      
      setViewMode(mode);
      refreshAllData();
    }

    // Toggle Individual Trader
    async function toggleTrader(address) {
      try {
        const res = await fetch('/api/traders/toggle', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ address: address })
        });
        const data = await res.json();
        if (data.success) {
          showToast(
            data.enabled ? 'Trader Ativado' : 'Trader Pausado',
            data.message,
            data.enabled ? 'success' : 'info'
          );
        }
      } catch (err) {
        showToast('Erro', 'Falha ao alterar status do trader: ' + err.message, 'error');
      }
      loadTraders();
      refreshAllData();
    }

    // Save Risk / Sizing Settings
    async function saveSettings(e) {
      e.preventDefault();
      const saveBtn = document.getElementById('btn-save-settings');
      saveBtn.disabled = true;
      saveBtn.innerHTML = '<span>Salvando...</span>';

      const payload = {
        fixed_amount_usd: parseFloat(document.getElementById('cfg-fixed-usd').value),
        daily_budget_usd: parseFloat(document.getElementById('cfg-daily-budget').value),
        max_per_market_usd: parseFloat(document.getElementById('cfg-max-market').value),
        slippage_tolerance_pct: parseFloat(document.getElementById('cfg-slippage').value),
        paper_initial_cash_usd: parseFloat(document.getElementById('cfg-paper-cash').value),
        live_initial_cash_usd: parseFloat(document.getElementById('cfg-live-cash').value)
      };

      try {
        await fetch('/api/config/update', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        showToast('⚙️ Configurações Salvas', 'Limites de risco e saldos atualizados com sucesso.', 'success');
      } catch (err) {
        showToast('Erro', 'Falha ao salvar configurações: ' + err.message, 'error');
      } finally {
        saveBtn.disabled = false;
        saveBtn.innerHTML = '<span>Salvar Configurações</span>';
      }
      refreshAllData();
    }

    // Sincronizar Saldo On-chain da Carteira Real via Bullpen
    async function syncWalletBalance() {
      showToast('Carteira', 'Consultando saldo on-chain na rede Polygon...', 'info');
      try {
        const res = await fetch('/api/wallet/sync', { method: 'POST' });
        const data = await res.json();
        if (data.success) {
          showToast('Saldo Atualizado', `Saldo da carteira real: $${data.balance_usd.toFixed(2)} (Polygon)`, 'success');
          const cfgLive = document.getElementById('cfg-live-cash');
          if (cfgLive) cfgLive.value = data.balance_usd;
          refreshAllData();
        } else {
          showToast('Aviso', data.error || 'Não foi possível ler o saldo da carteira', 'warning');
        }
      } catch (err) {
        showToast('Erro', 'Falha ao sincronizar carteira: ' + err.message, 'error');
      }
    }

    // Filter trades
    function filterTrades(filter) {
      currentFilter = filter;
      document.querySelectorAll('.trade-filter-btn').forEach(b => {
        if (b.dataset.filter === filter) {
          b.className = 'trade-filter-btn px-3 py-1 rounded-md text-xs font-semibold bg-cyan-600 text-white';
        } else {
          b.className = 'trade-filter-btn px-3 py-1 rounded-md text-xs font-semibold bg-gray-800 text-gray-400 hover:text-white';
        }
      });
      renderTradesTable();
    }

    // Render Activity Logs Terminal
    function renderActivityLogs(logs) {
      const terminal = document.getElementById('activity-log-terminal');
      if (!terminal) return;
      if (!logs || logs.length === 0) {
        terminal.innerHTML = '<div class="text-gray-500 italic">Nenhuma atividade registrada ainda. Clique em "INICIAR BOT" para começar.</div>';
        return;
      }

      terminal.innerHTML = logs.map(item => {
        let tagColor = 'text-cyan-400 bg-cyan-950 border-cyan-800';
        let tagText = 'INFO';
        let msgColor = 'text-gray-300';

        if (item.level === 'success') {
          tagColor = 'text-emerald-400 bg-emerald-950 border-emerald-800';
          tagText = 'TRADE';
          msgColor = 'text-emerald-300 font-semibold';
        } else if (item.level === 'warning') {
          tagColor = 'text-amber-400 bg-amber-950 border-amber-800';
          tagText = 'AVISO';
          msgColor = 'text-amber-300';
        } else if (item.level === 'error') {
          tagColor = 'text-rose-400 bg-rose-950 border-rose-800';
          tagText = 'ERRO';
          msgColor = 'text-rose-300 font-bold';
        }

        return `
          <div class="flex items-start space-x-2 py-0.5 hover:bg-gray-900/60 rounded px-1 transition">
            <span class="text-gray-500 select-none">[${item.timestamp}]</span>
            <span class="px-1 py-0.2 rounded border text-[10px] uppercase font-bold tracking-wider ${tagColor}">${tagText}</span>
            <span class="flex-1 ${msgColor} truncate" title="${item.message}">${item.message}</span>
          </div>
        `;
      }).join('');
    }

    function clearActivityLogs() {
      const terminal = document.getElementById('activity-log-terminal');
      if (terminal) {
        terminal.innerHTML = '<div class="text-gray-500 italic">Visualização limpa. Aguardando novas atividades...</div>';
      }
    }

    // Render Trades Table
    function renderTradesTable() {
      const tbody = document.getElementById('trades-tbody');
      if (!tbody) return;
      const search = (document.getElementById('trade-search')?.value || '').toLowerCase();

      let filtered = allTrades.filter(t => {
        const b_exec = t.bot_execution || {};
        const m_trade = t.master_trade || {};
        const market = t.market || {};
        const master = t.master_trader || {};

        const mode = (b_exec.mode || 'paper').toUpperCase();
        const status = (b_exec.status || t.status || '').toUpperCase();
        const action = (b_exec.action || m_trade.side || '').toUpperCase();
        
        if (currentFilter === 'PAPER_ONLY' && mode !== 'PAPER') return false;
        if (currentFilter === 'LIVE_ONLY' && mode !== 'LIVE') return false;
        if (currentFilter === 'EXECUTED' && status !== 'EXECUTED') return false;
        if (currentFilter === 'BUY' && action !== 'BUY') return false;
        if (currentFilter === 'SELL' && action !== 'SELL') return false;
        if (currentFilter === 'SKIPPED' && !['SKIPPED', 'REJECTED_BY_RISK'].includes(status)) return false;
        if (currentFilter === 'FAILED' && status !== 'FAILED') return false;

        if (search) {
          const matchStr = `${market.slug || ''} ${market.outcome || ''} ${market.title || ''} ${master.name || ''} ${master.address || ''} ${mode}`.toLowerCase();
          if (!matchStr.includes(search)) return false;
        }

        return true;
      });

      const countEl = document.getElementById('tab-trades-count');
      if (countEl) countEl.innerText = allTrades.length;

      if (filtered.length === 0) {
        tbody.innerHTML = `<tr><td colspan="11" class="px-4 py-8 text-center text-gray-500">Nenhuma operação encontrada com os filtros atuais.</td></tr>`;
        return;
      }

      tbody.innerHTML = filtered.map(t => {
        const b_exec = t.bot_execution || {};
        const m_trade = t.master_trade || {};
        const market = t.market || {};
        const master = t.master_trader || {};

        const tradeMode = (b_exec.mode || 'paper').toUpperCase();
        const modeBadge = tradeMode === 'PAPER'
          ? '<span class="px-1.5 py-0.5 rounded text-[10px] font-bold bg-cyan-950 text-cyan-400 border border-cyan-800">🧪 FAKE</span>'
          : '<span class="px-1.5 py-0.5 rounded text-[10px] font-bold bg-emerald-950 text-emerald-400 border border-emerald-800">⚡ REAL</span>';

        const action = (b_exec.action || m_trade.side || 'BUY').toUpperCase();
        const actionBadge = action === 'BUY' 
          ? '<span class="px-2 py-0.5 rounded font-bold bg-green-950 text-green-400 border border-green-800">COMPRA</span>'
          : '<span class="px-2 py-0.5 rounded font-bold bg-purple-950 text-purple-400 border border-purple-800">VENDA</span>';

        const status = (b_exec.status || t.status || 'EXECUTED').toUpperCase();
        let statusBadge = '';
        if (status === 'EXECUTED') {
          statusBadge = '<span class="px-2 py-0.5 rounded-full font-bold bg-emerald-950 text-emerald-400 border border-emerald-800">FILLED</span>';
        } else if (status === 'SKIPPED' || status === 'REJECTED_BY_RISK') {
          statusBadge = '<span class="px-2 py-0.5 rounded-full font-bold bg-yellow-950 text-yellow-400 border border-yellow-800">SKIPPED</span>';
        } else {
          statusBadge = '<span class="px-2 py-0.5 rounded-full font-bold bg-rose-950 text-rose-400 border border-rose-800">FALHOU</span>';
        }

        const timeStr = (t.timestamp || '').substring(0, 19).replace('T', ' ');
        const masterName = master.name || (master.address ? master.address.substring(0,8) : 'Unknown');
        const mSize = (m_trade.size_usd || 0).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
        const botAmt = (b_exec.amount_usd || 0).toFixed(2);
        const price = (b_exec.price || m_trade.price || 0.5).toFixed(3);
        const reasonStr = b_exec.reason || t.error || '-';
        const marketSlug = market.slug || '';
        const marketUrl = market.url || (marketSlug ? `https://polymarket.com/event/${marketSlug}` : '');

        return `
          <tr class="hover:bg-gray-900/50 transition">
            <td class="px-4 py-3 text-gray-400 whitespace-nowrap font-mono text-[11px]">${timeStr}</td>
            <td class="px-4 py-3 text-center whitespace-nowrap">${modeBadge}</td>
            <td class="px-4 py-3 font-semibold text-white whitespace-nowrap">${masterName}</td>
            <td class="px-4 py-3 whitespace-nowrap">${actionBadge}</td>
            <td class="px-4 py-3 font-mono">
              ${marketUrl ? `
                <a href="${marketUrl}" target="_blank" rel="noopener noreferrer" class="text-cyan-400 hover:text-cyan-300 underline font-mono inline-flex items-center gap-1 max-w-[280px] truncate group" title="Abrir: ${marketUrl}">
                  <span class="truncate">${marketUrl}</span>
                  <svg class="w-3 h-3 flex-shrink-0 text-cyan-500 group-hover:text-cyan-300" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14"></path></svg>
                </a>
              ` : '<span class="text-gray-500">-</span>'}
              ${market.title ? `<span class="text-gray-400 text-[11px] block truncate max-w-[280px] mt-0.5">${market.title}</span>` : ''}
            </td>
            <td class="px-4 py-3 font-bold text-yellow-400 whitespace-nowrap">${market.outcome || '-'}</td>
            <td class="px-4 py-3 text-right text-gray-300 font-mono">$${mSize}</td>
            <td class="px-4 py-3 text-right font-bold text-green-400 font-mono">$${botAmt}</td>
            <td class="px-4 py-3 text-right text-gray-300 font-mono">$${price}</td>
            <td class="px-4 py-3 text-center whitespace-nowrap">${statusBadge}</td>
            <td class="px-4 py-3 text-gray-400 text-xs truncate max-w-sm" title="${reasonStr}">${reasonStr}</td>
          </tr>
        `;
      }).join('');
    }

    // Load Open Positions for Selected Mode
    async function loadPositions() {
      try {
        const res = await fetch('/api/positions?mode=' + viewMode);
        const positions = await res.json();
        const tbody = document.getElementById('positions-tbody');
        if (!tbody) return;
        const keys = Object.keys(positions);
        
        const posCountEl = document.getElementById('tab-pos-count');
        if (posCountEl) posCountEl.innerText = keys.length;

        if (keys.length === 0) {
          tbody.innerHTML = `<tr><td colspan="6" class="px-4 py-8 text-center text-gray-500">Nenhuma posição aberta no modo ${viewMode.toUpperCase()}.</td></tr>`;
          return;
        }

        tbody.innerHTML = keys.map(k => {
          const p = positions[k];
          const val = (p.shares || 0) * (p.avg_price || 0.5);
          const pSlug = p.market_slug || k.split(':')[0] || '';
          const pUrl = p.market_url || (pSlug ? `https://polymarket.com/event/${pSlug}` : '');

          return `
            <tr class="hover:bg-gray-900/50 transition">
              <td class="px-4 py-3 font-semibold text-white">
                ${pUrl ? `
                  <a href="${pUrl}" target="_blank" rel="noopener noreferrer" class="text-cyan-400 hover:text-cyan-300 underline font-mono inline-flex items-center gap-1 max-w-[320px] truncate group" title="Abrir: ${pUrl}">
                    <span class="truncate">${pUrl}</span>
                    <svg class="w-3 h-3 flex-shrink-0 text-cyan-500 group-hover:text-cyan-300" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14"></path></svg>
                  </a>
                ` : `<span class="text-cyan-400 font-mono">${pSlug || k}</span>`}
                ${p.market_title ? `<br><span class="text-gray-400 text-[11px]">${p.market_title}</span>` : ''}
              </td>
              <td class="px-4 py-3 font-bold text-yellow-400">${p.outcome || '-'}</td>
              <td class="px-4 py-3 text-right font-mono text-cyan-400 font-bold">${(p.shares || 0).toFixed(2)}</td>
              <td class="px-4 py-3 text-right font-mono text-gray-300">$${(p.avg_price || 0).toFixed(3)}</td>
              <td class="px-4 py-3 text-right font-mono text-gray-300 font-bold">$${(p.total_cost || 0).toFixed(2)}</td>
              <td class="px-4 py-3 text-right font-mono text-emerald-400 font-bold">$${val.toFixed(2)}</td>
            </tr>
          `;
        }).join('');
      } catch (e) {
        console.error('Error loading positions:', e);
      }
    }

    // Load Master Traders List
    async function loadTraders() {
      try {
        const res = await fetch('/api/traders');
        const traders = await res.json();
        const tbody = document.getElementById('traders-tbody');
        if (!tbody) return;

        const countEl = document.getElementById('tab-traders-count');
        if (countEl) countEl.innerText = traders.length;

        tbody.innerHTML = traders.map((t, idx) => {
          const statusBadge = t.enabled
            ? '<span class="px-2 py-0.5 rounded font-bold bg-green-950 text-green-400 border border-green-800">ATIVO</span>'
            : '<span class="px-2 py-0.5 rounded font-bold bg-red-950 text-red-400 border border-red-800">PAUSADO</span>';

          const wrStr = ((t.win_rate_7d || 0) * 100).toFixed(1) + '%';
          const pnlVal = t.pnl_7d || 0;
          const pnlStr = (pnlVal >= 0 ? '+' : '') + '$' + pnlVal.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
          const volStr = '$' + (t.volume_7d || 0).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
          
          const riskColor = t.risk_tier === 'low' ? 'text-green-400' : (t.risk_tier === 'moderate' ? 'text-yellow-400' : 'text-red-400');

          return `
            <tr class="hover:bg-gray-900/50 transition">
              <td class="px-4 py-3 text-center text-gray-500 font-mono">${idx + 1}</td>
              <td class="px-4 py-3 whitespace-nowrap">${statusBadge}</td>
              <td class="px-4 py-3 font-semibold text-white whitespace-nowrap">${t.name}</td>
              <td class="px-4 py-3 font-mono text-gray-400 text-[11px]">${t.address.substring(0, 8)}...${t.address.substring(t.address.length - 6)}</td>
              <td class="px-4 py-3 text-right font-bold text-emerald-400 font-mono">${wrStr}</td>
              <td class="px-4 py-3 text-right font-bold text-cyan-400 font-mono">${pnlStr}</td>
              <td class="px-4 py-3 text-right text-gray-300 font-mono">${volStr}</td>
              <td class="px-4 py-3 text-cyan-400">${t.category}</td>
              <td class="px-4 py-3 font-bold uppercase ${riskColor}">${t.risk_tier}</td>
              <td class="px-4 py-3 text-gray-400 capitalize">${t.style}</td>
              <td class="px-4 py-3 text-center">
                <button onclick="toggleTrader('${t.address}')" class="px-2.5 py-1 rounded text-xs font-semibold ${t.enabled ? 'bg-rose-950 text-rose-300 border border-rose-800 hover:bg-rose-900' : 'bg-emerald-950 text-emerald-300 border border-emerald-800 hover:bg-emerald-900'} transition">
                  ${t.enabled ? 'Pausar' : 'Ativar'}
                </button>
              </td>
            </tr>
          `;
        }).join('');
      } catch (e) {
        console.error('Error loading traders:', e);
      }
    }

    // Scan & Update Master Traders directly from Dashboard
    async function scanTradersFromDashboard() {
      const btn = document.getElementById('btn-scan-traders');
      const spinner = document.getElementById('scan-spinner');
      const periodSelect = document.getElementById('scan-period-select');
      const period = periodSelect ? periodSelect.value : '7d';

      if (btn) btn.disabled = true;
      if (spinner) spinner.classList.remove('hidden');

      showToast('Escaneando Polymarket', `Buscando e filtrando os melhores traders (${period})...`, 'info', 4000);

      try {
        const res = await fetch('/api/traders/scan', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ period: period, top: 25 })
        });
        const data = await res.json();
        if (data.success) {
          showToast('Sucesso', data.message || 'Lista de traders atualizada!', 'success', 3500);
          await loadTraders();
          await refreshAllData();
        } else {
          showToast('Erro ao escanear', data.error || data.message || 'Falha ao buscar traders', 'error', 4000);
        }
      } catch (err) {
        showToast('Erro de Conexão', err.message, 'error', 4000);
      } finally {
        if (btn) btn.disabled = false;
        if (spinner) spinner.classList.add('hidden');
      }
    }

    // Manual refresh action
    async function manualRefresh() {
      const btn = document.getElementById('btn-refresh');
      if (btn) btn.classList.add('animate-spin');
      await refreshAllData();
      if (btn) setTimeout(() => btn.classList.remove('animate-spin'), 500);
      showToast('Painel Atualizado', 'Todas as métricas e feeds foram sincronizados.', 'info', 2000);
    }

    // Render Analytics based on selected view mode (Paper vs Live)
    function renderAnalytics(data) {
      if (!data) return;
      currentAnalytics = data;

      const paper = data.paper || {};
      const live = data.live || {};
      const an = viewMode === 'paper' ? paper : live;

      // Update Mini Comparative Summary Cards
      const miniEqPaper = document.getElementById('mini-equity-paper');
      if (miniEqPaper) miniEqPaper.innerText = '$' + (paper.total_equity_usd || 1000).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
      const miniPnlPaper = document.getElementById('mini-pnl-paper');
      if (miniPnlPaper) {
        const pVal = paper.realized_pnl_usd || 0;
        miniPnlPaper.innerText = (pVal >= 0 ? '+' : '') + '$' + pVal.toFixed(2) + ' PnL';
        miniPnlPaper.className = 'text-xs font-bold ' + (pVal >= 0 ? 'text-emerald-400' : 'text-rose-400');
      }
      const miniTrPaper = document.getElementById('mini-trades-paper');
      if (miniTrPaper) miniTrPaper.innerText = `${paper.executed_trades_count || 0} filled (${paper.total_trades_count || 0} tot)`;
      const miniWrPaper = document.getElementById('mini-wr-paper');
      if (miniWrPaper) miniWrPaper.innerText = (paper.win_rate_pct || 0).toFixed(1) + '%';
      const miniCashPaper = document.getElementById('mini-cash-paper');
      if (miniCashPaper) miniCashPaper.innerText = '$' + (paper.cash_usd || 1000).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});

      const miniEqLive = document.getElementById('mini-equity-live');
      if (miniEqLive) miniEqLive.innerText = '$' + (live.total_equity_usd || 0).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
      const miniPnlLive = document.getElementById('mini-pnl-live');
      if (miniPnlLive) {
        const pVal = live.realized_pnl_usd || 0;
        miniPnlLive.innerText = (pVal >= 0 ? '+' : '') + '$' + pVal.toFixed(2) + ' PnL';
        miniPnlLive.className = 'text-xs font-bold ' + (pVal >= 0 ? 'text-emerald-400' : 'text-rose-400');
      }
      const miniTrLive = document.getElementById('mini-trades-live');
      if (miniTrLive) miniTrLive.innerText = `${live.executed_trades_count || 0} filled (${live.failed_trades_count || 0} falhas)`;
      const miniWrLive = document.getElementById('mini-wr-live');
      if (miniWrLive) miniWrLive.innerText = (live.win_rate_pct || 0).toFixed(1) + '%';
      const miniCashLive = document.getElementById('mini-cash-live');
      if (miniCashLive) miniCashLive.innerText = '$' + (live.cash_usd || 0).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});

      // Update 6 Main Stat Cards for Active View Mode
      const pnlEl = document.getElementById('stat-pnl');
      if (pnlEl) {
        const pnlVal = an.realized_pnl_usd || 0;
        pnlEl.innerText = (pnlVal >= 0 ? '+$' : '-$') + Math.abs(pnlVal).toFixed(2);
        pnlEl.className = 'text-2xl font-black ' + (pnlVal >= 0 ? 'text-emerald-400' : 'text-rose-400');
      }

      const wrEl = document.getElementById('stat-winrate');
      if (wrEl) wrEl.innerText = (an.win_rate_pct || 0).toFixed(1) + '%';
      const wrBar = document.getElementById('stat-winrate-bar');
      if (wrBar) wrBar.style.width = (an.win_rate_pct || 0) + '%';

      const winsEl = document.getElementById('stat-wins');
      if (winsEl) winsEl.innerText = (an.winners_count || 0) + 'W';
      const lossEl = document.getElementById('stat-losses');
      if (lossEl) lossEl.innerText = (an.losers_count || 0) + 'L';
      const pfEl = document.getElementById('stat-profit-factor');
      if (pfEl) pfEl.innerText = `Profit Factor: ${(an.profit_factor || 1.0).toFixed(2)}x`;

      const totalTradesEl = document.getElementById('stat-total-trades');
      if (totalTradesEl) totalTradesEl.innerText = an.total_trades_count || 0;
      const tradesSplitEl = document.getElementById('stat-trades-split');
      if (tradesSplitEl) tradesSplitEl.innerText = `${an.executed_trades_count || 0} Executados • ${an.failed_trades_count || 0} Falhas`;

      const eqEl = document.getElementById('stat-equity');
      if (eqEl) eqEl.innerText = '$' + (an.total_equity_usd || (viewMode === 'paper' ? 1000 : 0)).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
      const cashEl = document.getElementById('stat-cash');
      if (cashEl) cashEl.innerText = 'Caixa: $' + (an.cash_usd || (viewMode === 'paper' ? 1000 : 0)).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});

      const dailySpent = an.daily_spent_usd || 0;
      const dailyCap = an.daily_budget_usd || 100;
      const budgetEl = document.getElementById('stat-budget');
      if (budgetEl) budgetEl.innerText = `$${dailySpent.toFixed(2)} / $${dailyCap.toFixed(0)}`;
      const budgetBar = document.getElementById('stat-budget-bar');
      if (budgetBar) budgetBar.style.width = Math.min(100, (dailySpent / dailyCap) * 100) + '%';

      const infoPos = document.getElementById('info-positions');
      if (infoPos) infoPos.innerText = `${an.open_positions_count || 0} Mercados`;

      // Update Chart for Active View Mode
      if (!equityChart) {
        initChart();
      }
      if (equityChart && an.chart_timestamps && an.chart_timestamps.length > 0) {
        equityChart.data.labels = an.chart_timestamps;
        equityChart.data.datasets[0].data = an.chart_equity;
        if (viewMode === 'paper') {
          equityChart.data.datasets[0].borderColor = '#06b6d4';
          equityChart.data.datasets[0].backgroundColor = 'rgba(6, 182, 212, 0.1)';
          equityChart.data.datasets[0].pointBackgroundColor = '#22d3ee';
        } else {
          equityChart.data.datasets[0].borderColor = '#10b981';
          equityChart.data.datasets[0].backgroundColor = 'rgba(16, 185, 129, 0.1)';
          equityChart.data.datasets[0].pointBackgroundColor = '#34d399';
        }
        equityChart.update();
      }
    }

    // Refresh all data from APIs
    async function refreshAllData() {
      // 1. Fetch Status
      try {
        const statusRes = await fetch('/api/status');
        const status = await statusRes.json();
        isBotRunning = status.is_running;
        lastKnownCycleAt = status.last_cycle_at ? new Date(status.last_cycle_at) : null;

        const powerBtn = document.getElementById('btn-power');
        const powerText = document.getElementById('power-text');
        const iconContainer = document.getElementById('power-icon-container');
        const statusPill = document.getElementById('status-pill');
        const statusDot = document.getElementById('status-dot');
        const statusText = document.getElementById('status-text');

        const engineBadge = document.getElementById('bot-engine-badge');
        if (engineBadge) {
          engineBadge.innerText = status.mode === 'paper' ? '🧪 SIMULAÇÃO (FAKE)' : '⚡ REAL (LIVE)';
          engineBadge.className = status.mode === 'paper'
            ? 'px-2 py-0.5 rounded font-bold bg-cyan-900 text-cyan-300 border border-cyan-700 uppercase'
            : 'px-2 py-0.5 rounded font-bold bg-emerald-900 text-emerald-300 border border-emerald-700 uppercase';
        }

        const infoExecMode = document.getElementById('info-exec-mode');
        if (infoExecMode) {
          infoExecMode.innerText = status.mode === 'paper' ? '🧪 SIMULAÇÃO (FAKE)' : '⚡ REAL (LIVE)';
          infoExecMode.className = status.mode === 'paper' ? 'font-bold text-cyan-400' : 'font-bold text-emerald-400';
        }

        if (status.is_running) {
          if (powerBtn) {
            powerBtn.className = 'flex items-center space-x-2 px-4 py-2 rounded-lg font-bold text-sm shadow-lg transition duration-200 transform active:scale-95 bg-rose-600 hover:bg-rose-500 text-white shadow-rose-600/20';
            if (powerText) powerText.innerText = 'PARAR BOT';
            if (iconContainer) iconContainer.innerHTML = '<svg class="w-4 h-4 fill-current" viewBox="0 0 24 24"><rect x="4" y="4" width="16" height="16" rx="2"></rect></svg>';
          }
          
          if (statusPill) statusPill.className = 'flex items-center space-x-2 px-3 py-1 rounded-full text-xs font-semibold bg-emerald-950 text-emerald-400 border border-emerald-800';
          if (statusDot) statusDot.className = 'w-2.5 h-2.5 rounded-full bg-emerald-400 pulse-dot';
          if (statusText) statusText.innerText = `ROBÔ ATIVO (${status.mode.toUpperCase()})`;
          
          const infoStatus = document.getElementById('info-status');
          if (infoStatus) infoStatus.innerHTML = '<span class="text-emerald-400 font-bold">🟢 ATIVO (' + status.mode.toUpperCase() + ')</span>';

          const listenerIconBg = document.getElementById('listener-icon-bg');
          if (listenerIconBg) listenerIconBg.className = 'p-2 rounded bg-cyan-950 text-cyan-400 border border-cyan-800';
          const listenerTitle = document.getElementById('listener-title');
          if (listenerTitle) {
            listenerTitle.className = 'text-white font-semibold flex items-center gap-1.5';
            listenerTitle.innerHTML = '<span class="w-2 h-2 rounded-full bg-emerald-400 animate-ping"></span> Monitor de Feed Ativo';
          }
          const listenerSubtitle = document.getElementById('listener-subtitle');
          if (listenerSubtitle) listenerSubtitle.innerText = `Monitorando ${status.active_traders_count} traders a cada ${status.poll_interval_seconds}s`;

          const pulseBadge = document.getElementById('live-pulse-badge');
          if (pulseBadge) pulseBadge.className = 'inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[10px] font-bold bg-emerald-950 text-emerald-400 border border-emerald-800';
          const pulseDot = document.getElementById('live-pulse-dot');
          if (pulseDot) pulseDot.className = 'w-1.5 h-1.5 rounded-full bg-emerald-400 animate-ping';
          const pulseText = document.getElementById('live-pulse-text');
          if (pulseText) pulseText.innerText = 'MONITORANDO';
        } else {
          if (powerBtn) {
            powerBtn.className = 'flex items-center space-x-2 px-4 py-2 rounded-lg font-bold text-sm shadow-lg transition duration-200 transform active:scale-95 bg-emerald-600 hover:bg-emerald-500 text-white shadow-emerald-600/20';
            if (powerText) powerText.innerText = 'INICIAR BOT';
            if (iconContainer) iconContainer.innerHTML = '<svg class="w-4 h-4 fill-current" viewBox="0 0 24 24"><polygon points="6 3 20 12 6 21 6 3"></polygon></svg>';
          }
          
          if (statusPill) statusPill.className = 'flex items-center space-x-2 px-3 py-1 rounded-full text-xs font-semibold bg-gray-800 text-gray-300 border border-gray-700';
          if (statusDot) statusDot.className = 'w-2.5 h-2.5 rounded-full bg-gray-500';
          if (statusText) statusText.innerText = 'ROBÔ PARADO';
          
          const infoStatus = document.getElementById('info-status');
          if (infoStatus) infoStatus.innerHTML = '<span class="text-gray-400 font-bold">🔴 PARADO</span>';

          const listenerIconBg = document.getElementById('listener-icon-bg');
          if (listenerIconBg) listenerIconBg.className = 'p-2 rounded bg-gray-800 text-gray-400';
          const listenerTitle = document.getElementById('listener-title');
          if (listenerTitle) {
            listenerTitle.className = 'text-gray-300 font-semibold';
            listenerTitle.innerText = 'Monitor de Feed Inativo';
          }
          const listenerSubtitle = document.getElementById('listener-subtitle');
          if (listenerSubtitle) listenerSubtitle.innerText = `Clique em Iniciar Bot para monitorar (${status.poll_interval_seconds}s)`;

          const pulseBadge = document.getElementById('live-pulse-badge');
          if (pulseBadge) pulseBadge.className = 'inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[10px] font-bold bg-gray-800 text-gray-400 border border-gray-700';
          const pulseDot = document.getElementById('live-pulse-dot');
          if (pulseDot) pulseDot.className = 'w-1.5 h-1.5 rounded-full bg-gray-500';
          const pulseText = document.getElementById('live-pulse-text');
          if (pulseText) pulseText.innerText = 'STANDBY';
        }

        const btnPaper = document.getElementById('btn-mode-paper');
        const btnLive = document.getElementById('btn-mode-live');
        if (status.mode === 'paper') {
          if (btnPaper) btnPaper.className = 'px-3 py-1 rounded font-medium transition bg-cyan-600 text-white flex items-center gap-1.5';
          if (btnLive) btnLive.className = 'px-3 py-1 rounded font-medium transition text-gray-400 hover:text-white flex items-center gap-1.5';
        } else {
          if (btnLive) btnLive.className = 'px-3 py-1 rounded font-medium transition bg-rose-600 text-white flex items-center gap-1.5';
          if (btnPaper) btnPaper.className = 'px-3 py-1 rounded font-medium transition text-gray-400 hover:text-white flex items-center gap-1.5';
        }

        const infoUptime = document.getElementById('info-uptime');
        if (infoUptime) infoUptime.innerText = status.uptime;
        const infoTraders = document.getElementById('info-traders');
        if (infoTraders) infoTraders.innerText = `${status.active_traders_count} / ${status.total_traders_count}`;
        const infoPoll = document.getElementById('info-poll-int');
        if (infoPoll) infoPoll.innerText = status.poll_interval_seconds;
        const valCycles = document.getElementById('val-total-cycles');
        if (valCycles) valCycles.innerText = status.total_cycles;

        renderActivityLogs(status.activity_logs);
      } catch (err) {
        console.error('Error fetching status:', err);
      }

      // 2. Fetch Separated Analytics
      try {
        const analyticsRes = await fetch('/api/analytics');
        const analyticsData = await analyticsRes.json();
        renderAnalytics(analyticsData);
      } catch (err) {
        console.error('Error fetching analytics:', err);
      }

      // 3. Fetch Trades
      try {
        const tradesRes = await fetch('/api/trades');
        allTrades = await tradesRes.json();
        renderTradesTable();
      } catch (err) {
        console.error('Error fetching trades:', err);
      }

      // 4. Fetch Positions
      try {
        await loadPositions();
      } catch (err) {
        console.error('Error loading positions:', err);
      }

      safeCreateIcons();
    }

    // Relative timestamp updater (runs every 1 second)
    function updateRelativeTimers() {
      const lastPollEl = document.getElementById('val-last-poll');
      if (!lastPollEl) return;

      if (!lastKnownCycleAt) {
        lastPollEl.innerText = isBotRunning ? 'Iniciando primeiro ciclo...' : 'Nunca';
        return;
      }

      const diffSecs = Math.max(0, Math.floor((new Date() - lastKnownCycleAt) / 1000));
      if (diffSecs === 0) {
        lastPollEl.innerText = 'Agora mesmo';
      } else if (diffSecs < 60) {
        lastPollEl.innerText = `${diffSecs}s atrás`;
      } else {
        const mins = Math.floor(diffSecs / 60);
        const remSecs = diffSecs % 60;
        lastPollEl.innerText = `${mins}m ${remSecs}s atrás`;
      }
    }

    // Initial Load
    window.addEventListener('DOMContentLoaded', () => {
      initChart();
      loadTraders();
      refreshAllData();
      safeCreateIcons();
      // Auto refresh data every 2.0 seconds
      setInterval(refreshAllData, 2000);
      // Continuous 1s ticker for relative timers
      setInterval(updateRelativeTimers, 1000);
    });
  </script>
</body>
</html>
"""


@app.route("/", methods=["GET"])
def index():
    return render_template_string(DASHBOARD_HTML)


def run_dashboard(host: str = "0.0.0.0", port: int = 5000, debug: bool = False):
    print(f"\n=======================================================")
    print(f"🚀 POLYMARKET COPYTRADING DASHBOARD (SEPARATED STATS)")
    print(f"📡 Local Web UI URL: http://localhost:{port}")
    print(f"=======================================================\n")
    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    port = 5000
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass
    run_dashboard(port=port)

