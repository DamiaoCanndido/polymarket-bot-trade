#!/usr/bin/env python3
"""
Polymarket Copytrading Bot CLI.
"""
import os
import sys
import json
import time
import logging
import argparse
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.layout import Layout

from config import load_config, save_config, BotConfig, MasterTrader
from scanner import LeaderboardScanner
from risk_manager import RiskManager
from executor import CopyExecutor
from tracker import CopyTracker

console = Console()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def print_banner(mode: str = "Paper Trading"):
    title = f"[bold cyan]POLYMARKET COPYTRADING BOT[/bold cyan] [yellow]({mode})[/yellow]"
    console.print(Panel.fit(title, border_style="cyan"))


def cmd_scan(args, config: BotConfig):
    console.print(f"[bold green]Scanning Polymarket Leaderboard ({args.period}) for top balanced master traders...[/bold green]")
    scanner = LeaderboardScanner(bullpen_path=config.bullpen_path)
    traders = scanner.fetch_top_traders(
        time_period=args.period,
        min_win_rate=args.min_win_rate,
        min_pnl=args.min_pnl,
        min_volume=args.min_volume,
        exclude_high_risk=not args.include_high_risk,
        limit=args.limit
    )

    if not traders:
        console.print("[yellow]No traders matched the specified criteria.[/yellow]")
        return

    table = Table(title=f"Top Master Traders ({args.period} Performance)", border_style="cyan")
    table.add_column("#", justify="right", style="cyan", no_wrap=True)
    table.add_column("Trader Name / Address", style="bold white")
    table.add_column("Win Rate", justify="right", style="green")
    table.add_column("Realized PnL", justify="right", style="magenta")
    table.add_column("7D Volume", justify="right", style="yellow")
    table.add_column("Category", style="blue")
    table.add_column("Risk Tier", style="bold")
    table.add_column("Style", style="white")

    for i, t in enumerate(traders[:args.top]):
        wr_str = f"{t.win_rate_7d * 100:.1f}%"
        pnl_str = f"+${t.pnl_7d:,.2f}" if t.pnl_7d >= 0 else f"-${abs(t.pnl_7d):,.2f}"
        vol_str = f"${t.volume_7d:,.2f}"
        risk_color = "green" if t.risk_tier == "low" else ("yellow" if t.risk_tier == "moderate" else "red")
        risk_str = f"[{risk_color}]{t.risk_tier.upper()}[/{risk_color}]"

        display_name = f"{t.name}\n[dim]{t.address}[/dim]" if t.name != t.address else t.address
        table.add_row(str(i + 1), display_name, wr_str, pnl_str, vol_str, t.category, risk_str, t.style)

    console.print(table)

    if args.save:
        config.traders = traders[:args.top]
        save_config(config)
        console.print(f"[bold green]Saved top {len(config.traders)} master traders to config.json![/bold green]")


def cmd_status(args, config: BotConfig):
    mode_str = "[green]DRY-RUN / PAPER TRADING[/green]" if config.dry_run else "[red]LIVE EXECUTION[/red]"
    print_banner(mode="SIMULATION" if config.dry_run else "LIVE")

    risk_mgr = RiskManager(config.risk)
    executor = CopyExecutor(config, risk_mgr)
    tracker = CopyTracker(config, executor, risk_mgr)

    # Risk Config Summary
    summary_table = Table(show_header=False, box=None)
    summary_table.add_row("[bold]Execution Mode:[/bold]", mode_str)
    summary_table.add_row("[bold]Daily Budget Cap:[/bold]", f"${config.risk.daily_budget_usd:,.2f}")
    summary_table.add_row("[bold]Max USD Per Market:[/bold]", f"${config.risk.max_per_market_usd:,.2f}")
    summary_table.add_row("[bold]Max Trade Size:[/bold]", f"${config.risk.max_trade_size_usd:,.2f}")
    summary_table.add_row("[bold]Allowed Price Range:[/bold]", f"${config.risk.min_price:.2f} - ${config.risk.max_price:.2f}")
    summary_table.add_row("[bold]Slippage Tolerance:[/bold]", f"{config.risk.slippage_tolerance_pct}%")
    summary_table.add_row("[bold]Auto Mirror Sells:[/bold]", str(config.risk.auto_exit_on_sell))
    summary_table.add_row("[bold]Trade Log File (JSONL):[/bold]", f"[cyan]{config.trades_log_file}[/cyan]")
    summary_table.add_row("[bold]Portfolio State File:[/bold]", f"[cyan]{config.portfolio_state_file}[/cyan]")
    console.print(Panel(summary_table, title="[bold]Risk & Execution Configuration[/bold]", border_style="blue"))

    # Portfolio Summary
    metrics = tracker._get_portfolio_metrics()
    port_table = Table(show_header=False, box=None)
    port_table.add_row("[bold]Cash Balance:[/bold]", f"${metrics['cash_usd']:,.2f}")
    port_table.add_row("[bold]Open Positions Value:[/bold]", f"${metrics['positions_value_usd']:,.2f}")
    port_table.add_row("[bold]Total Equity:[/bold]", f"[bold yellow]${metrics['total_equity_usd']:,.2f}[/bold yellow]")
    pnl_color = "green" if metrics['realized_pnl_usd'] >= 0 else "red"
    port_table.add_row("[bold]Realized PnL:[/bold]", f"[{pnl_color}]${metrics['realized_pnl_usd']:+,.2f}[/{pnl_color}]")
    port_table.add_row("[bold]Total Trades Logged:[/bold]", f"{metrics['total_trades_count']} ({metrics['successful_trades']} filled, {metrics['failed_trades']} failed)")
    port_table.add_row("[bold]Open Positions Count:[/bold]", f"{metrics['open_positions_count']}")
    console.print(Panel(port_table, title="[bold]Portfolio & Performance Metrics[/bold]", border_style="green"))

    # Tracked Traders
    traders_table = Table(title=f"Configured Master Traders ({len(config.traders)})", border_style="cyan")
    table_cols = ["#", "Status", "Trader", "Address", "7D Win Rate", "7D PnL", "Category", "Copy Sizing"]
    for c in table_cols:
        traders_table.add_column(c)

    for i, t in enumerate(config.traders):
        status_str = "[green]ACTIVE[/green]" if t.enabled else "[red]PAUSED[/red]"
        wr_str = f"{t.win_rate_7d * 100:.1f}%"
        pnl_str = f"+${t.pnl_7d:,.2f}" if t.pnl_7d >= 0 else f"-${abs(t.pnl_7d):,.2f}"
        sizing_str = f"${t.copy_amount_usd:,.2f} fixed" if config.sizing.mode == "fixed" else f"{config.sizing.mirror_percent_cap}% mirror"
        traders_table.add_row(
            str(i + 1),
            status_str,
            t.name,
            f"{t.address[:6]}...{t.address[-4:]}",
            wr_str,
            pnl_str,
            t.category,
            sizing_str
        )
    console.print(traders_table)


def cmd_portfolio(args, config: BotConfig):
    risk_mgr = RiskManager(config.risk)
    executor = CopyExecutor(config, risk_mgr)
    tracker = CopyTracker(config, executor, risk_mgr)
    
    metrics = tracker._get_portfolio_metrics()
    console.print(Panel(
        f"[bold]Cash:[/bold] ${metrics['cash_usd']:,.2f} | "
        f"[bold]Positions Value:[/bold] ${metrics['positions_value_usd']:,.2f} | "
        f"[bold]Total Equity:[/bold] ${metrics['total_equity_usd']:,.2f} | "
        f"[bold]Realized PnL:[/bold] ${metrics['realized_pnl_usd']:+,.2f}",
        title="[bold green]Current Portfolio Overview[/bold green]",
        border_style="green"
    ))

    positions = tracker.portfolio.get("positions", {})
    if not positions:
        console.print("[yellow]No open positions currently held.[/yellow]")
        return

    table = Table(title="Open Positions", border_style="cyan")
    table.add_column("Market Slug", style="cyan")
    table.add_column("Outcome", style="bold white")
    table.add_column("Shares", justify="right", style="magenta")
    table.add_column("Avg Price", justify="right", style="yellow")
    table.add_column("Total Cost", justify="right", style="green")

    for key, pos in positions.items():
        table.add_row(
            pos.get("market_slug", key),
            pos.get("outcome", "Yes"),
            f"{pos.get('shares', 0):,.2f}",
            f"${pos.get('avg_price', 0):.3f}",
            f"${pos.get('total_cost', 0):,.2f}"
        )
    console.print(table)


def cmd_logs(args, config: BotConfig):
    if not os.path.exists(config.trades_log_file):
        console.print(f"[yellow]No trade log file found at {config.trades_log_file}[/yellow]")
        return

    lines = []
    with open(config.trades_log_file, "r") as f:
        for line in f:
            if line.strip():
                try:
                    lines.append(json.loads(line.strip()))
                except Exception:
                    pass

    if not lines:
        console.print("[yellow]Trade log is currently empty.[/yellow]")
        return

    table = Table(title=f"Recent Trade Logs (Showing last {min(len(lines), args.limit)} of {len(lines)})", border_style="cyan")
    table.add_column("Time (UTC)", style="dim")
    table.add_column("Master", style="bold white")
    table.add_column("Action", style="bold")
    table.add_column("Market Slug", style="cyan")
    table.add_column("Outcome", style="yellow")
    table.add_column("Master Size", justify="right")
    table.add_column("Copied Amount", justify="right", style="green")
    table.add_column("Status", style="bold")

    for rec in lines[-args.limit:]:
        m_trade = rec.get("master_trade", {})
        b_exec = rec.get("bot_execution", {})
        market = rec.get("market", {})
        master = rec.get("master_trader", {})

        action = b_exec.get("action") or m_trade.get("side", "BUY")
        action_color = "green" if action == "BUY" else "red"
        action_str = f"[{action_color}]{action}[/{action_color}]"

        status = b_exec.get("status", rec.get("status", "UNKNOWN"))
        status_color = "green" if status == "EXECUTED" else ("yellow" if status in ("SKIPPED", "REJECTED_BY_RISK") else "red")
        status_str = f"[{status_color}]{status}[/{status_color}]"

        table.add_row(
            rec.get("timestamp", "")[:19].replace("T", " "),
            master.get("name") or master.get("address", "")[:8],
            action_str,
            market.get("slug", "")[:28],
            market.get("outcome", ""),
            f"${m_trade.get('size_usd', 0):,.2f}",
            f"${b_exec.get('amount_usd', 0):,.2f}",
            status_str
        )
    console.print(table)


def cmd_start(args, config: BotConfig):
    if args.live:
        config.dry_run = False
    elif args.dry_run:
        config.dry_run = True

    mode_label = "DRY RUN (SIMULATION)" if config.dry_run else "LIVE EXECUTION"
    print_banner(mode=mode_label)

    risk_mgr = RiskManager(config.risk)
    executor = CopyExecutor(config, risk_mgr)
    tracker = CopyTracker(config, executor, risk_mgr)

    active_traders = [t for t in config.traders if t.enabled]
    if not active_traders:
        console.print("[red]Error: No active master traders configured! Run `python3 bot.py scan --save` first.[/red]")
        return

    console.print(f"[bold green]Starting copytrading bot listening to {len(active_traders)} master traders...[/bold green]")
    console.print(f"[dim]Trade Log: {config.trades_log_file} | State: {config.portfolio_state_file}[/dim]")
    console.print(f"[dim]Polling interval: {config.poll_interval_seconds}s. Press Ctrl+C to stop.[/dim]\n")

    try:
        iteration = 0
        while True:
            iteration += 1
            executed = tracker.poll_cycle()
            if executed:
                for event in executed:
                    details = event.get("details", {})
                    market = details.get("market", {})
                    m_trade = details.get("master_trade", {})
                    b_exec = details.get("bot_execution", {})
                    master = details.get("master_trader", {})

                    status = b_exec.get("status", "EXECUTED")
                    if status == "EXECUTED":
                        action = b_exec.get("action", "BUY")
                        action_color = "green" if action == "BUY" else "magenta"
                        console.print(
                            f"[{action_color}]✓ {action} EXECUTED:[/{action_color}] "
                            f"Outcome: [bold yellow]{market.get('outcome')}[/bold yellow] on [cyan]{market.get('slug')}[/cyan] | "
                            f"Copied: [green]${b_exec.get('amount_usd', 0):.2f}[/green] ({b_exec.get('shares', 0):.2f} shs @ ${b_exec.get('price', 0):.3f}) | "
                            f"Master: [bold white]{master.get('name')}[/bold white] (${m_trade.get('size_usd', 0):,.2f})"
                        )
                    elif status == "FAILED":
                        console.print(
                            f"[red]✗ TRADE FAILED:[/red] {market.get('slug')} ({market.get('outcome')}) | "
                            f"Error: {b_exec.get('reason')} (Continuing loop...)"
                        )

            if iteration % 6 == 0:
                risk_summary = risk_mgr.get_risk_summary()
                metrics = tracker._get_portfolio_metrics()
                pnl_color = "green" if metrics['realized_pnl_usd'] >= 0 else "red"
                console.print(
                    f"[dim][{time.strftime('%H:%M:%S')}] Monitoring... "
                    f"Cash: ${metrics['cash_usd']:,.2f} | "
                    f"Equity: ${metrics['total_equity_usd']:,.2f} | "
                    f"Realized PnL: [{pnl_color}]${metrics['realized_pnl_usd']:+,.2f}[/{pnl_color}] | "
                    f"Daily Spend: ${risk_summary['daily_spent_usd']:.2f} / ${risk_summary['daily_budget_usd']:.2f} | "
                    f"Active Positions: {metrics['open_positions_count']}[/dim]"
                )

            time.sleep(config.poll_interval_seconds)
    except KeyboardInterrupt:
        console.print("\n[bold yellow]Copytrading bot stopped by user.[/bold yellow]")


def cmd_dashboard(args, config: BotConfig):
    from dashboard import run_dashboard
    run_dashboard(host=args.host, port=args.port)


def main():
    parser = argparse.ArgumentParser(description="Polymarket Copytrading Bot")
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # scan
    p_scan = subparsers.add_parser("scan", help="Scan leaderboard for top master traders")
    p_scan.add_argument("--period", default="7d", choices=["1d", "7d", "30d", "all"], help="Time period")
    p_scan.add_argument("--min-win-rate", type=float, default=0.65, help="Minimum win rate (0.0 - 1.0)")
    p_scan.add_argument("--min-pnl", type=float, default=5000.0, help="Minimum realized PnL in USD")
    p_scan.add_argument("--min-volume", type=float, default=5000.0, help="Minimum volume in USD")
    p_scan.add_argument("--limit", type=int, default=100, help="Number of records to inspect")
    p_scan.add_argument("--top", type=int, default=25, help="Top N traders to display/save")
    p_scan.add_argument("--include-high-risk", action="store_true", help="Include high risk and degen accounts")
    p_scan.add_argument("--save", action="store_true", help="Save discovered traders into config.json")

    # status
    p_status = subparsers.add_parser("status", help="Show current bot status and master trader roster")

    # portfolio
    p_port = subparsers.add_parser("portfolio", help="Show current open positions and equity")

    # logs
    p_logs = subparsers.add_parser("logs", help="Display recent trades from the JSONL log")
    p_logs.add_argument("--limit", type=int, default=20, help="Number of recent records to display")

    # start
    p_start = subparsers.add_parser("start", help="Start the copytrading bot")
    p_start.add_argument("--dry-run", action="store_true", help="Run in simulation / paper trading mode")
    p_start.add_argument("--live", action="store_true", help="Run in live money execution mode")

    # dashboard
    p_dash = subparsers.add_parser("dashboard", help="Launch the local web dashboard")
    p_dash.add_argument("--port", type=int, default=5000, help="Port to bind dashboard server")
    p_dash.add_argument("--host", default="0.0.0.0", help="Host interface to bind")

    args = parser.parse_args()
    config = load_config()

    if args.command == "scan":
        cmd_scan(args, config)
    elif args.command == "status":
        cmd_status(args, config)
    elif args.command == "portfolio":
        cmd_portfolio(args, config)
    elif args.command == "logs":
        cmd_logs(args, config)
    elif args.command == "start":
        cmd_start(args, config)
    elif args.command == "dashboard":
        cmd_dashboard(args, config)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
