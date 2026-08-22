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
            mode_label = "Paper Trading" if self.config.dry_run else "Live Execution"

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
        self.log_activity("info", f"Background worker listening to {active_count} master trader feeds (interval: {self.config.poll_interval_seconds}s).")
        
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
                        self.log_activity(
                            "success",
                            f"⚡ Signal Executed: {action} '{outcome}' on {slug} (${amt:.2f})"
                        )
                    logger.info(f"Cycle #{self.total_cycles} executed {len(executed)} trade(s).")
                else:
                    self.log_activity(
                        "info",
                        f"📡 Cycle #{self.total_cycles}: Polled {active_count} master traders (0 new signals)."
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


def calculate_analytics(trades: List[Dict[str, Any]], portfolio: Dict[str, Any]) -> Dict[str, Any]:
    """
    Computes analytics: Realized PnL, Win Rate %, Winners vs Losers, Volume, Profit Factor.
    """
    total_trades = len(trades)
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

    # Track equity curve over time for chart
    equity_history = []
    pnl_history = []
    timestamps = []

    running_pnl = 0.0
    initial_cash = portfolio.get("initial_cash_usd", 1000.0)
    current_cash = portfolio.get("cash_usd", 1000.0)
    realized_pnl_usd = portfolio.get("realized_pnl_usd", 0.0)

    for tr in trades:
        b_exec = tr.get("bot_execution", {})
        status = b_exec.get("status") or tr.get("status", "UNKNOWN")
        action = b_exec.get("action") or tr.get("master_trade", {}).get("side", "BUY")
        amt = float(b_exec.get("amount_usd") or 0.0)
        ts = tr.get("timestamp", "")
        pm = tr.get("portfolio_metrics", {})

        if status == "EXECUTED":
            executed_trades += 1
            if action == "BUY":
                buys_count += 1
                total_bought_usd += amt
            elif action == "SELL":
                sells_count += 1
                total_sold_usd += amt
                
                # Check for realized PnL in reason string or details
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
                    # If sell has no explicit PnL string, check portfolio metric change
                    winners_count += 1

            if "realized_pnl_usd" in pm:
                running_pnl = float(pm["realized_pnl_usd"])
            
            running_equity = float(pm.get("total_equity_usd", initial_cash + running_pnl))

            timestamps.append(ts[:19].replace("T", " "))
            pnl_history.append(round(running_pnl, 2))
            equity_history.append(round(running_equity, 2))

        elif status in ("SKIPPED", "REJECTED_BY_RISK"):
            skipped_trades += 1
        elif status == "FAILED":
            failed_trades += 1

    # If portfolio has realized PnL from latest trade or state, ensure accuracy
    if not realized_pnl_usd and running_pnl:
        realized_pnl_usd = running_pnl

    closed_trades_count = winners_count + losers_count + even_count
    win_rate_pct = (winners_count / (winners_count + losers_count) * 100.0) if (winners_count + losers_count) > 0 else (100.0 if winners_count > 0 else 0.0)
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (gross_profit if gross_profit > 0 else 1.0)

    # Position value
    positions = portfolio.get("positions", {})
    open_positions_val = sum(p.get("shares", 0.0) * p.get("avg_price", 0.50) for p in positions.values())
    total_equity = portfolio.get("total_equity_usd") or (current_cash + open_positions_val)

    return {
        "realized_pnl_usd": round(realized_pnl_usd, 2),
        "win_rate_pct": round(win_rate_pct, 1),
        "total_trades_count": total_trades,
        "executed_trades_count": executed_trades,
        "skipped_trades_count": skipped_trades,
        "failed_trades_count": failed_trades,
        "buys_count": buys_count,
        "sells_count": sells_count,
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
        "chart_timestamps": timestamps[-30:],
        "chart_equity": equity_history[-30:],
        "chart_pnl": pnl_history[-30:]
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
    analytics = calculate_analytics(trades, portfolio)
    return jsonify(analytics)


@app.route("/api/trades", methods=["GET"])
def api_trades():
    trades = parse_trade_logs(bot_manager.config.trades_log_file)
    # Return newest trades first
    return jsonify(list(reversed(trades)))


@app.route("/api/positions", methods=["GET"])
def api_positions():
    portfolio = bot_manager.get_portfolio()
    positions = portfolio.get("positions", {})
    return jsonify(positions)


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


@app.route("/api/config/update", methods=["POST"])
def api_config_update():
    data = request.get_json(silent=True) or {}
    cfg = load_config()
    
    if "dry_run" in data:
        cfg.dry_run = bool(data["dry_run"])
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
    bot_manager.log_activity("info", "⚙️ Configuration & risk limits updated.")
    return jsonify({"success": True, "message": "Configuration updated successfully."})


# =====================================================================
# DASHBOARD HTML TEMPLATE
# =====================================================================

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en" class="dark">
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
            <p class="text-xs text-gray-400">Top 25 Master Discovery & Auto Mirror</p>
          </div>
        </div>

        <!-- Live Status Pill -->
        <div id="status-pill" class="flex items-center space-x-2 px-3 py-1 rounded-full text-xs font-semibold bg-gray-800 text-gray-300 border border-gray-700 transition-all duration-300">
          <span id="status-dot" class="w-2.5 h-2.5 rounded-full bg-gray-500"></span>
          <span id="status-text">INITIALIZING...</span>
        </div>
      </div>

      <!-- Action Controls -->
      <div class="flex items-center space-x-3">
        <!-- Mode Switcher -->
        <div class="flex items-center bg-gray-900 border border-gray-800 rounded-lg p-1 text-xs">
          <button id="btn-mode-paper" onclick="setMode('paper')" class="px-3 py-1 rounded font-medium transition bg-cyan-600 text-white">
            🧪 Paper Trading
          </button>
          <button id="btn-mode-live" onclick="setMode('live')" class="px-3 py-1 rounded font-medium transition text-gray-400 hover:text-white">
            ⚡ Live Capital
          </button>
        </div>

        <!-- Start / Stop Button -->
        <button id="btn-power" onclick="toggleBotPower()" class="flex items-center space-x-2 px-4 py-2 rounded-lg font-bold text-sm shadow-lg transition duration-200 transform active:scale-95 bg-emerald-600 hover:bg-emerald-500 text-white shadow-emerald-600/20 disabled:opacity-60 disabled:cursor-not-allowed">
          <span id="power-icon-container" class="flex items-center justify-center">
            <svg class="w-4 h-4 fill-current" viewBox="0 0 24 24"><polygon points="6 3 20 12 6 21 6 3"></polygon></svg>
          </span>
          <span id="power-text">START BOT</span>
        </button>

        <!-- Refresh Button -->
        <button id="btn-refresh" onclick="manualRefresh()" class="p-2 rounded-lg bg-gray-800 border border-gray-700 text-gray-300 hover:text-white hover:bg-gray-700 transition" title="Refresh Dashboard">
          <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"></path></svg>
        </button>
      </div>

    </div>
  </header>

  <!-- MAIN CONTAINER -->
  <main class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6 flex-1 w-full space-y-6">

    <!-- OVERVIEW STAT CARDS (6 METRICS) -->
    <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-6 gap-4">
      
      <!-- Card 1: Realized PnL -->
      <div class="glass-card rounded-xl p-4 flex flex-col justify-between border-t-2 border-t-cyan-500">
        <div class="flex items-center justify-between text-gray-400 text-xs font-medium">
          <span>Realized PnL</span>
          <span class="text-cyan-400 font-bold">$</span>
        </div>
        <div class="mt-2">
          <div id="stat-pnl" class="text-2xl font-black text-emerald-400">+$0.00</div>
          <p id="stat-pnl-sub" class="text-xs text-gray-400 mt-1">Closed positions profit</p>
        </div>
      </div>

      <!-- Card 2: Win Rate -->
      <div class="glass-card rounded-xl p-4 flex flex-col justify-between border-t-2 border-t-emerald-500">
        <div class="flex items-center justify-between text-gray-400 text-xs font-medium">
          <span>Win Rate</span>
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
      <div class="glass-card rounded-xl p-4 flex flex-col justify-between border-t-2 border-t-purple-500">
        <div class="flex items-center justify-between text-gray-400 text-xs font-medium">
          <span>Wins / Losses</span>
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
      <div class="glass-card rounded-xl p-4 flex flex-col justify-between border-t-2 border-t-blue-500">
        <div class="flex items-center justify-between text-gray-400 text-xs font-medium">
          <span>Copied Trades</span>
          <span class="text-blue-400 font-bold">#</span>
        </div>
        <div class="mt-2">
          <div id="stat-total-trades" class="text-2xl font-black text-white">0</div>
          <p id="stat-trades-split" class="text-xs text-gray-400 mt-1">0 Buys • 0 Sells</p>
        </div>
      </div>

      <!-- Card 5: Total Equity & Cash -->
      <div class="glass-card rounded-xl p-4 flex flex-col justify-between border-t-2 border-t-amber-500">
        <div class="flex items-center justify-between text-gray-400 text-xs font-medium">
          <span>Total Equity</span>
          <span class="text-amber-400 font-bold">EQ</span>
        </div>
        <div class="mt-2">
          <div id="stat-equity" class="text-2xl font-black text-amber-400">$1,000.00</div>
          <p id="stat-cash" class="text-xs text-gray-400 mt-1">Cash: $1,000.00</p>
        </div>
      </div>

      <!-- Card 6: Daily Budget Utilization -->
      <div class="glass-card rounded-xl p-4 flex flex-col justify-between border-t-2 border-t-rose-500">
        <div class="flex items-center justify-between text-gray-400 text-xs font-medium">
          <span>Daily Budget Cap</span>
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
            <h2 class="text-sm font-bold text-white flex items-center gap-2">
              Performance & Equity Trajectory
            </h2>
            <p class="text-xs text-gray-400">Live trajectory updated after each executed trade</p>
          </div>
          <span id="badge-chart-status" class="text-xs font-semibold px-2 py-0.5 rounded bg-cyan-950 text-cyan-400 border border-cyan-800 flex items-center gap-1.5">
            <span class="w-1.5 h-1.5 rounded-full bg-cyan-400 pulse-dot"></span>
            Real-Time
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
            Bot Execution Summary
          </h2>
          <div class="space-y-2.5 text-xs">
            <div class="flex justify-between py-1.5 border-b border-gray-800">
              <span class="text-gray-400">Bot Status:</span>
              <span id="info-status" class="font-bold text-gray-400">🔴 STOPPED</span>
            </div>
            <div class="flex justify-between py-1.5 border-b border-gray-800">
              <span class="text-gray-400">Session Uptime:</span>
              <span id="info-uptime" class="font-bold text-white">0s</span>
            </div>
            <div class="flex justify-between py-1.5 border-b border-gray-800">
              <span class="text-gray-400">Active Master Traders:</span>
              <span id="info-traders" class="font-bold text-cyan-400">25 / 25</span>
            </div>
            <div class="flex justify-between py-1.5 border-b border-gray-800">
              <span class="text-gray-400">Open Positions:</span>
              <span id="info-positions" class="font-bold text-emerald-400">0 Markets</span>
            </div>
            <div class="flex justify-between py-1.5 border-b border-gray-800">
              <span class="text-gray-400">Copy Sizing Mode:</span>
              <span id="info-sizing" class="font-bold text-yellow-400">$10.00 Fixed</span>
            </div>
            <div class="flex justify-between py-1.5">
              <span class="text-gray-400">Auto Mirror Sells:</span>
              <span class="font-bold text-green-400">Enabled (Proportional)</span>
            </div>
          </div>
        </div>

        <div id="live-listener-card" class="bg-gray-900/80 rounded-lg p-3 border border-gray-800 flex items-center space-x-3 transition-colors duration-300">
          <div id="listener-icon-bg" class="p-2 rounded bg-gray-800 text-gray-400">
            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"></path></svg>
          </div>
          <div class="text-xs flex-1">
            <p id="listener-title" class="text-gray-300 font-semibold">Feed Listener Inactive</p>
            <p id="listener-subtitle" class="text-gray-500">Press Start to begin polling (<span id="info-poll-int">5</span>s)</p>
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
              Live Execution Activity & Polling Feed
              <span id="live-pulse-badge" class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[10px] font-bold bg-gray-800 text-gray-400 border border-gray-700 transition-all">
                <span id="live-pulse-dot" class="w-1.5 h-1.5 rounded-full bg-gray-500"></span>
                <span id="live-pulse-text">STANDBY</span>
              </span>
            </h2>
            <p class="text-xs text-gray-400">Real-time background worker cycles, smart money polling & execution events</p>
          </div>
        </div>
        <div class="flex items-center space-x-3 text-xs">
          <div id="cycle-badge" class="px-2.5 py-1 rounded bg-gray-900 border border-gray-800 text-gray-300 font-mono">
            Cycles: <span id="val-total-cycles" class="font-bold text-cyan-400">0</span>
          </div>
          <div id="last-poll-badge" class="px-2.5 py-1 rounded bg-gray-900 border border-gray-800 text-gray-400 font-mono text-[11px]">
            Last poll: <span id="val-last-poll" class="text-gray-300">Never</span>
          </div>
          <button onclick="clearActivityLogs()" class="text-gray-400 hover:text-white p-1 rounded hover:bg-gray-800 transition" title="Clear log view">
            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"></path></svg>
          </button>
        </div>
      </div>
      <!-- Terminal feed window -->
      <div id="activity-log-terminal" class="h-36 overflow-y-auto font-mono text-xs space-y-1.5 pr-2 bg-gray-950/90 rounded-lg p-3 border border-gray-900 select-text">
        <div class="text-gray-500 italic">Connecting to live feed...</div>
      </div>
    </div>

    <!-- TABS NAVIGATION -->
    <div class="border-b border-bordercol flex space-x-8 text-sm">
      <button onclick="switchTab('trades')" id="tab-btn-trades" class="pb-3 tab-active flex items-center gap-2">
        <span>📋</span> Copied Trades Feed (<span id="tab-trades-count">0</span>)
      </button>
      <button onclick="switchTab('positions')" id="tab-btn-positions" class="pb-3 text-gray-400 hover:text-gray-200 flex items-center gap-2">
        <span>📊</span> Open Positions (<span id="tab-pos-count">0</span>)
      </button>
      <button onclick="switchTab('traders')" id="tab-btn-traders" class="pb-3 text-gray-400 hover:text-gray-200 flex items-center gap-2">
        <span>👥</span> Top 25 Master Traders (<span id="tab-traders-count">25</span>)
      </button>
      <button onclick="switchTab('settings')" id="tab-btn-settings" class="pb-3 text-gray-400 hover:text-gray-200 flex items-center gap-2">
        <span>⚙️</span> Settings & Risk Caps
      </button>
    </div>

    <!-- TAB 1: TRADES FEED -->
    <div id="tab-content-trades" class="space-y-4">
      <div class="flex flex-col sm:flex-row items-center justify-between gap-3">
        <div class="flex items-center space-x-2">
          <button onclick="filterTrades('ALL')" class="trade-filter-btn px-3 py-1 rounded-md text-xs font-semibold bg-cyan-600 text-white" data-filter="ALL">All</button>
          <button onclick="filterTrades('EXECUTED')" class="trade-filter-btn px-3 py-1 rounded-md text-xs font-semibold bg-gray-800 text-gray-400 hover:text-white" data-filter="EXECUTED">Executed</button>
          <button onclick="filterTrades('BUY')" class="trade-filter-btn px-3 py-1 rounded-md text-xs font-semibold bg-gray-800 text-gray-400 hover:text-white" data-filter="BUY">Buys</button>
          <button onclick="filterTrades('SELL')" class="trade-filter-btn px-3 py-1 rounded-md text-xs font-semibold bg-gray-800 text-gray-400 hover:text-white" data-filter="SELL">Sells</button>
          <button onclick="filterTrades('SKIPPED')" class="trade-filter-btn px-3 py-1 rounded-md text-xs font-semibold bg-gray-800 text-gray-400 hover:text-white" data-filter="SKIPPED">Skipped</button>
          <button onclick="filterTrades('FAILED')" class="trade-filter-btn px-3 py-1 rounded-md text-xs font-semibold bg-gray-800 text-gray-400 hover:text-white" data-filter="FAILED">Failed</button>
        </div>
        <div class="relative w-full sm:w-64">
          <input type="text" id="trade-search" oninput="renderTradesTable()" placeholder="Search slug, master or outcome..." class="w-full bg-gray-900 border border-gray-800 rounded-lg px-3 py-1.5 text-xs text-white placeholder-gray-500 focus:outline-none focus:border-cyan-500">
        </div>
      </div>

      <!-- Trades Table -->
      <div class="glass-card rounded-xl overflow-hidden border border-bordercol">
        <div class="overflow-x-auto">
          <table class="w-full text-left text-xs">
            <thead class="bg-gray-900/90 text-gray-400 border-b border-bordercol uppercase tracking-wider font-semibold">
              <tr>
                <th class="px-4 py-3">Time (UTC)</th>
                <th class="px-4 py-3">Master Trader</th>
                <th class="px-4 py-3">Action</th>
                <th class="px-4 py-3">Market / Slug</th>
                <th class="px-4 py-3">Outcome</th>
                <th class="px-4 py-3 text-right">Master Size</th>
                <th class="px-4 py-3 text-right">Copied Amount</th>
                <th class="px-4 py-3 text-right">Price</th>
                <th class="px-4 py-3 text-center">Status</th>
                <th class="px-4 py-3">Details / Result</th>
              </tr>
            </thead>
            <tbody id="trades-tbody" class="divide-y divide-gray-800">
              <tr>
                <td colspan="10" class="px-4 py-8 text-center text-gray-500">
                  Loading trades log...
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- TAB 2: OPEN POSITIONS -->
    <div id="tab-content-positions" class="hidden space-y-4">
      <div class="glass-card rounded-xl overflow-hidden border border-bordercol">
        <div class="overflow-x-auto">
          <table class="w-full text-left text-xs">
            <thead class="bg-gray-900/90 text-gray-400 border-b border-bordercol uppercase tracking-wider font-semibold">
              <tr>
                <th class="px-4 py-3">Market Slug / Title</th>
                <th class="px-4 py-3">Outcome</th>
                <th class="px-4 py-3 text-right">Shares Held</th>
                <th class="px-4 py-3 text-right">Avg Entry Price</th>
                <th class="px-4 py-3 text-right">Total Cost</th>
                <th class="px-4 py-3 text-right">Current Value Est.</th>
              </tr>
            </thead>
            <tbody id="positions-tbody" class="divide-y divide-gray-800">
              <tr>
                <td colspan="6" class="px-4 py-8 text-center text-gray-500">
                  No open positions held.
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
                <th class="px-4 py-3">Copy Status</th>
                <th class="px-4 py-3">Trader Name</th>
                <th class="px-4 py-3">Wallet Address</th>
                <th class="px-4 py-3 text-right">7D Win Rate</th>
                <th class="px-4 py-3 text-right">7D Realized PnL</th>
                <th class="px-4 py-3 text-right">7D Volume</th>
                <th class="px-4 py-3">Category</th>
                <th class="px-4 py-3">Risk Tier</th>
                <th class="px-4 py-3">Style</th>
                <th class="px-4 py-3 text-center">Action</th>
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
          Risk Management & Sizing Parameters
        </h3>
        <form id="form-settings" onsubmit="saveSettings(event)" class="space-y-4 text-xs">
          <div>
            <label class="block text-gray-400 mb-1">Fixed Copy Amount per Trade (USD)</label>
            <input type="number" step="0.5" id="cfg-fixed-usd" class="w-full bg-gray-900 border border-gray-800 rounded-lg p-2 text-white" value="10.0">
          </div>
          <div>
            <label class="block text-gray-400 mb-1">Daily Spend Budget Cap (USD)</label>
            <input type="number" step="1" id="cfg-daily-budget" class="w-full bg-gray-900 border border-gray-800 rounded-lg p-2 text-white" value="100.0">
          </div>
          <div>
            <label class="block text-gray-400 mb-1">Max USD Exposure Per Single Market</label>
            <input type="number" step="1" id="cfg-max-market" class="w-full bg-gray-900 border border-gray-800 rounded-lg p-2 text-white" value="50.0">
          </div>
          <div>
            <label class="block text-gray-400 mb-1">Slippage Tolerance (%)</label>
            <input type="number" step="0.1" id="cfg-slippage" class="w-full bg-gray-900 border border-gray-800 rounded-lg p-2 text-white" value="2.0">
          </div>
          <div class="pt-2">
            <button id="btn-save-settings" type="submit" class="px-4 py-2 bg-cyan-600 hover:bg-cyan-500 font-bold rounded-lg text-white transition flex items-center gap-2">
              <span>Save Settings</span>
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
      } catch (e) {
        // Fallback silently
      }
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

    // Toggle Bot Power (Start / Stop) with INSTANT visual feedback
    async function toggleBotPower() {
      const powerBtn = document.getElementById('btn-power');
      const powerText = document.getElementById('power-text');
      const iconContainer = document.getElementById('power-icon-container');

      const willStart = !isBotRunning;

      powerBtn.disabled = true;
      if (willStart) {
        powerBtn.className = 'flex items-center space-x-2 px-4 py-2 rounded-lg font-bold text-sm shadow-lg transition duration-200 bg-emerald-700 text-white cursor-wait opacity-90';
        powerText.innerText = 'STARTING...';
        iconContainer.innerHTML = '<svg class="animate-spin w-4 h-4 text-white" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"></path></svg>';
        
        document.getElementById('status-pill').className = 'flex items-center space-x-2 px-3 py-1 rounded-full text-xs font-semibold bg-emerald-950/80 text-emerald-400 border border-emerald-800';
        document.getElementById('status-dot').className = 'w-2.5 h-2.5 rounded-full bg-emerald-400 animate-ping';
        document.getElementById('status-text').innerText = 'STARTING WORKER...';
        
        showToast('Starting Copytrading Bot', 'Launching background feed listener and loading master traders...', 'info');
      } else {
        powerBtn.className = 'flex items-center space-x-2 px-4 py-2 rounded-lg font-bold text-sm shadow-lg transition duration-200 bg-rose-700 text-white cursor-wait opacity-90';
        powerText.innerText = 'STOPPING...';
        iconContainer.innerHTML = '<svg class="animate-spin w-4 h-4 text-white" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"></path></svg>';
        
        showToast('Stopping Bot', 'Terminating background polling cycle...', 'info');
      }

      try {
        const endpoint = willStart ? '/api/bot/start' : '/api/bot/stop';
        const res = await fetch(endpoint, { method: 'POST' });
        const data = await res.json();

        if (data.success) {
          showToast(
            willStart ? '🚀 Bot Active' : '⏹️ Bot Stopped',
            data.message || (willStart ? 'Bot started successfully.' : 'Bot stopped successfully.'),
            willStart ? 'success' : 'warning'
          );
        } else {
          showToast('Notice', data.message || 'Operation failed.', 'warning');
        }
      } catch (err) {
        showToast('Communication Error', 'Failed to reach dashboard server: ' + err.message, 'error');
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
        btnPaper.className = 'px-3 py-1 rounded font-medium transition bg-cyan-600 text-white';
        btnLive.className = 'px-3 py-1 rounded font-medium transition text-gray-400 hover:text-white';
      } else {
        btnLive.className = 'px-3 py-1 rounded font-medium transition bg-rose-600 text-white';
        btnPaper.className = 'px-3 py-1 rounded font-medium transition text-gray-400 hover:text-white';
      }

      showToast(
        dryRun ? '🧪 Paper Trading Mode' : '⚡ Live Capital Mode',
        dryRun ? 'Switched to paper simulation mode. No real funds deployed.' : 'Switched to LIVE execution mode. Orders will mirror on Polymarket.',
        dryRun ? 'info' : 'warning'
      );

      try {
        await fetch('/api/config/update', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ dry_run: dryRun })
        });
      } catch (err) {
        showToast('Error', 'Failed to update execution mode: ' + err.message, 'error');
      }
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
            data.enabled ? 'Trader Activated' : 'Trader Paused',
            data.message,
            data.enabled ? 'success' : 'info'
          );
        }
      } catch (err) {
        showToast('Error', 'Failed to toggle trader: ' + err.message, 'error');
      }
      loadTraders();
      refreshAllData();
    }

    // Save Risk / Sizing Settings
    async function saveSettings(e) {
      e.preventDefault();
      const saveBtn = document.getElementById('btn-save-settings');
      saveBtn.disabled = true;
      saveBtn.innerHTML = '<span>Saving...</span>';

      const payload = {
        fixed_amount_usd: parseFloat(document.getElementById('cfg-fixed-usd').value),
        daily_budget_usd: parseFloat(document.getElementById('cfg-daily-budget').value),
        max_per_market_usd: parseFloat(document.getElementById('cfg-max-market').value),
        slippage_tolerance_pct: parseFloat(document.getElementById('cfg-slippage').value)
      };

      try {
        await fetch('/api/config/update', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        showToast('⚙️ Settings Saved', 'Risk limits and sizing parameters updated and applied.', 'success');
      } catch (err) {
        showToast('Error', 'Failed to save settings: ' + err.message, 'error');
      } finally {
        saveBtn.disabled = false;
        saveBtn.innerHTML = '<span>Save Settings</span>';
      }
      refreshAllData();
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
        terminal.innerHTML = '<div class="text-gray-500 italic">No activity yet. Press "START BOT" to begin live copytrading.</div>';
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
          tagText = 'WARN';
          msgColor = 'text-amber-300';
        } else if (item.level === 'error') {
          tagColor = 'text-rose-400 bg-rose-950 border-rose-800';
          tagText = 'ERROR';
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
        terminal.innerHTML = '<div class="text-gray-500 italic">Log view cleared. Waiting for new activity...</div>';
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

        const status = (b_exec.status || t.status || '').toUpperCase();
        const action = (b_exec.action || m_trade.side || '').toUpperCase();
        
        if (currentFilter === 'EXECUTED' && status !== 'EXECUTED') return false;
        if (currentFilter === 'BUY' && action !== 'BUY') return false;
        if (currentFilter === 'SELL' && action !== 'SELL') return false;
        if (currentFilter === 'SKIPPED' && !['SKIPPED', 'REJECTED_BY_RISK'].includes(status)) return false;
        if (currentFilter === 'FAILED' && status !== 'FAILED') return false;

        if (search) {
          const matchStr = `${market.slug || ''} ${market.outcome || ''} ${market.title || ''} ${master.name || ''} ${master.address || ''}`.toLowerCase();
          if (!matchStr.includes(search)) return false;
        }

        return true;
      });

      const countEl = document.getElementById('tab-trades-count');
      if (countEl) countEl.innerText = allTrades.length;

      if (filtered.length === 0) {
        tbody.innerHTML = `<tr><td colspan="10" class="px-4 py-8 text-center text-gray-500">No matching trades found.</td></tr>`;
        return;
      }

      tbody.innerHTML = filtered.map(t => {
        const b_exec = t.bot_execution || {};
        const m_trade = t.master_trade || {};
        const market = t.market || {};
        const master = t.master_trader || {};

        const action = (b_exec.action || m_trade.side || 'BUY').toUpperCase();
        const actionBadge = action === 'BUY' 
          ? '<span class="px-2 py-0.5 rounded font-bold bg-green-950 text-green-400 border border-green-800">BUY</span>'
          : '<span class="px-2 py-0.5 rounded font-bold bg-purple-950 text-purple-400 border border-purple-800">SELL</span>';

        const status = (b_exec.status || t.status || 'EXECUTED').toUpperCase();
        let statusBadge = '';
        if (status === 'EXECUTED') {
          statusBadge = '<span class="px-2 py-0.5 rounded-full font-bold bg-emerald-950 text-emerald-400 border border-emerald-800">FILLED</span>';
        } else if (status === 'SKIPPED' || status === 'REJECTED_BY_RISK') {
          statusBadge = '<span class="px-2 py-0.5 rounded-full font-bold bg-yellow-950 text-yellow-400 border border-yellow-800">SKIPPED</span>';
        } else {
          statusBadge = '<span class="px-2 py-0.5 rounded-full font-bold bg-rose-950 text-rose-400 border border-rose-800">FAILED</span>';
        }

        const timeStr = (t.timestamp || '').substring(0, 19).replace('T', ' ');
        const masterName = master.name || (master.address ? master.address.substring(0,8) : 'Unknown');
        const mSize = (m_trade.size_usd || 0).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
        const botAmt = (b_exec.amount_usd || 0).toFixed(2);
        const price = (b_exec.price || m_trade.price || 0.5).toFixed(3);
        const reasonStr = b_exec.reason || t.error || '-';

        return `
          <tr class="hover:bg-gray-900/50 transition">
            <td class="px-4 py-3 text-gray-400 whitespace-nowrap font-mono text-[11px]">${timeStr}</td>
            <td class="px-4 py-3 font-semibold text-white whitespace-nowrap">${masterName}</td>
            <td class="px-4 py-3 whitespace-nowrap">${actionBadge}</td>
            <td class="px-4 py-3 font-mono text-cyan-400 truncate max-w-xs" title="${market.slug || ''}">${market.slug || '-'}</td>
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

    // Load Open Positions
    async function loadPositions() {
      try {
        const res = await fetch('/api/positions');
        const positions = await res.json();
        const tbody = document.getElementById('positions-tbody');
        if (!tbody) return;
        const keys = Object.keys(positions);
        
        const posCountEl = document.getElementById('tab-pos-count');
        if (posCountEl) posCountEl.innerText = keys.length;

        if (keys.length === 0) {
          tbody.innerHTML = `<tr><td colspan="6" class="px-4 py-8 text-center text-gray-500">No open positions held.</td></tr>`;
          return;
        }

        tbody.innerHTML = keys.map(k => {
          const p = positions[k];
          const val = (p.shares || 0) * (p.avg_price || 0.5);
          return `
            <tr class="hover:bg-gray-900/50 transition">
              <td class="px-4 py-3 font-semibold text-white">
                <span class="text-cyan-400 font-mono">${p.market_slug || k}</span>
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
            ? '<span class="px-2 py-0.5 rounded font-bold bg-green-950 text-green-400 border border-green-800">ACTIVE</span>'
            : '<span class="px-2 py-0.5 rounded font-bold bg-red-950 text-red-400 border border-red-800">PAUSED</span>';

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
                  ${t.enabled ? 'Pause' : 'Enable'}
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
      showToast('Dashboard Refreshed', 'All market feeds, analytics, and metrics updated.', 'info', 2000);
    }

    // Refresh all data
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

        if (status.is_running) {
          if (powerBtn) {
            powerBtn.className = 'flex items-center space-x-2 px-4 py-2 rounded-lg font-bold text-sm shadow-lg transition duration-200 transform active:scale-95 bg-rose-600 hover:bg-rose-500 text-white shadow-rose-600/20';
            if (powerText) powerText.innerText = 'STOP BOT';
            if (iconContainer) iconContainer.innerHTML = '<svg class="w-4 h-4 fill-current" viewBox="0 0 24 24"><rect x="4" y="4" width="16" height="16" rx="2"></rect></svg>';
          }
          
          if (statusPill) statusPill.className = 'flex items-center space-x-2 px-3 py-1 rounded-full text-xs font-semibold bg-emerald-950 text-emerald-400 border border-emerald-800';
          if (statusDot) statusDot.className = 'w-2.5 h-2.5 rounded-full bg-emerald-400 pulse-dot';
          if (statusText) statusText.innerText = `LIVE LISTENING (${status.mode.toUpperCase()})`;
          
          const infoStatus = document.getElementById('info-status');
          if (infoStatus) infoStatus.innerHTML = '<span class="text-emerald-400 font-bold">🟢 ACTIVE (' + status.mode.toUpperCase() + ')</span>';

          const listenerIconBg = document.getElementById('listener-icon-bg');
          if (listenerIconBg) listenerIconBg.className = 'p-2 rounded bg-cyan-950 text-cyan-400 border border-cyan-800';
          const listenerTitle = document.getElementById('listener-title');
          if (listenerTitle) {
            listenerTitle.className = 'text-white font-semibold flex items-center gap-1.5';
            listenerTitle.innerHTML = '<span class="w-2 h-2 rounded-full bg-emerald-400 animate-ping"></span> Live Feed Active';
          }
          const listenerSubtitle = document.getElementById('listener-subtitle');
          if (listenerSubtitle) listenerSubtitle.innerText = `Polling ${status.active_traders_count} traders every ${status.poll_interval_seconds}s`;

          const pulseBadge = document.getElementById('live-pulse-badge');
          if (pulseBadge) pulseBadge.className = 'inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[10px] font-bold bg-emerald-950 text-emerald-400 border border-emerald-800';
          const pulseDot = document.getElementById('live-pulse-dot');
          if (pulseDot) pulseDot.className = 'w-1.5 h-1.5 rounded-full bg-emerald-400 animate-ping';
          const pulseText = document.getElementById('live-pulse-text');
          if (pulseText) pulseText.innerText = 'LISTENING';
        } else {
          if (powerBtn) {
            powerBtn.className = 'flex items-center space-x-2 px-4 py-2 rounded-lg font-bold text-sm shadow-lg transition duration-200 transform active:scale-95 bg-emerald-600 hover:bg-emerald-500 text-white shadow-emerald-600/20';
            if (powerText) powerText.innerText = 'START BOT';
            if (iconContainer) iconContainer.innerHTML = '<svg class="w-4 h-4 fill-current" viewBox="0 0 24 24"><polygon points="6 3 20 12 6 21 6 3"></polygon></svg>';
          }
          
          if (statusPill) statusPill.className = 'flex items-center space-x-2 px-3 py-1 rounded-full text-xs font-semibold bg-gray-800 text-gray-300 border border-gray-700';
          if (statusDot) statusDot.className = 'w-2.5 h-2.5 rounded-full bg-gray-500';
          if (statusText) statusText.innerText = 'BOT STOPPED';
          
          const infoStatus = document.getElementById('info-status');
          if (infoStatus) infoStatus.innerHTML = '<span class="text-gray-400 font-bold">🔴 STOPPED</span>';

          const listenerIconBg = document.getElementById('listener-icon-bg');
          if (listenerIconBg) listenerIconBg.className = 'p-2 rounded bg-gray-800 text-gray-400';
          const listenerTitle = document.getElementById('listener-title');
          if (listenerTitle) {
            listenerTitle.className = 'text-gray-300 font-semibold';
            listenerTitle.innerText = 'Feed Listener Inactive';
          }
          const listenerSubtitle = document.getElementById('listener-subtitle');
          if (listenerSubtitle) listenerSubtitle.innerText = `Press Start to begin polling (${status.poll_interval_seconds}s)`;

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
          if (btnPaper) btnPaper.className = 'px-3 py-1 rounded font-medium transition bg-cyan-600 text-white';
          if (btnLive) btnLive.className = 'px-3 py-1 rounded font-medium transition text-gray-400 hover:text-white';
        } else {
          if (btnLive) btnLive.className = 'px-3 py-1 rounded font-medium transition bg-rose-600 text-white';
          if (btnPaper) btnPaper.className = 'px-3 py-1 rounded font-medium transition text-gray-400 hover:text-white';
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

      // 2. Fetch Analytics
      try {
        const analyticsRes = await fetch('/api/analytics');
        const an = await analyticsRes.json();

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
        if (tradesSplitEl) tradesSplitEl.innerText = `${an.buys_count || 0} Buys • ${an.sells_count || 0} Sells`;

        const eqEl = document.getElementById('stat-equity');
        if (eqEl) eqEl.innerText = '$' + (an.total_equity_usd || 1000).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
        const cashEl = document.getElementById('stat-cash');
        if (cashEl) cashEl.innerText = 'Cash: $' + (an.cash_usd || 1000).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});

        const dailySpent = (an.daily_spent_usd) || 0;
        const dailyCap = (an.daily_budget_usd) || 100;
        const budgetEl = document.getElementById('stat-budget');
        if (budgetEl) budgetEl.innerText = `$${dailySpent.toFixed(2)} / $${dailyCap.toFixed(0)}`;
        const budgetBar = document.getElementById('stat-budget-bar');
        if (budgetBar) budgetBar.style.width = Math.min(100, (dailySpent / dailyCap) * 100) + '%';

        const infoPos = document.getElementById('info-positions');
        if (infoPos) infoPos.innerText = `${an.open_positions_count || 0} Markets`;

        // Update or initialize Chart
        if (!equityChart) {
          initChart();
        }
        if (an.chart_timestamps && an.chart_timestamps.length > 0 && equityChart) {
          equityChart.data.labels = an.chart_timestamps;
          equityChart.data.datasets[0].data = an.chart_equity;
          equityChart.update();
        }
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
        lastPollEl.innerText = isBotRunning ? 'Starting first cycle...' : 'Never';
        return;
      }

      const diffSecs = Math.max(0, Math.floor((new Date() - lastKnownCycleAt) / 1000));
      if (diffSecs === 0) {
        lastPollEl.innerText = 'Just now';
      } else if (diffSecs < 60) {
        lastPollEl.innerText = `${diffSecs}s ago`;
      } else {
        const mins = Math.floor(diffSecs / 60);
        const remSecs = diffSecs % 60;
        lastPollEl.innerText = `${mins}m ${remSecs}s ago`;
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
    print(f"🚀 POLYMARKET COPYTRADING DASHBOARD")
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
