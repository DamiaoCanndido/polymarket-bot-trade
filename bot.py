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
from scanner import LeaderboardScanner, SportsMarketScanner
from risk_manager import RiskManager
from executor import CopyExecutor
from tracker import CopyTracker

console = Console()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def print_banner(mode: str = "Paper Trading"):
    title = f"[bold cyan]POLYMARKET SPORTS SIGNALS & TRADING BOT[/bold cyan] [yellow]({mode})[/yellow]"
    console.print(Panel.fit(title, border_style="cyan"))


def cmd_sports(args, config: BotConfig):
    console.print("[bold green]Scanning Polymarket for active sports events and odds...[/bold green]")
    scanner = SportsMarketScanner(config.sports)
    opps = scanner.scan_sports_opportunities(limit_per_sport=args.limit)

    if not opps:
        console.print("[yellow]No sports opportunities found matching criteria.[/yellow]")
        return

    table = Table(title="Live Sports Opportunities & Signals", border_style="green")
    table.add_column("Modalidade", style="bold cyan")
    table.add_column("Evento / Partida", style="bold white")
    table.add_column("Seleção", style="bold yellow")
    table.add_column("Odd / Preço", justify="right", style="magenta")
    table.add_column("Volume 24h", justify="right", style="green")
    table.add_column("Liquidez", justify="right", style="blue")
    table.add_column("Confiança", style="white")
    table.add_column("Link Direto", style="dim underline")

    for o in opps[:args.top]:
        table.add_row(
            o["sport_label"],
            o["event_title"][:40],
            o["outcome"],
            f"${o['price']:.2f} ({o['odds_pct']})",
            f"${o['volume_24h_usd']:,.0f}",
            f"${o['liquidity_usd']:,.0f}",
            o["confidence"],
            o["event_url"]
        )

    console.print(table)


def cmd_scan(args, config: BotConfig):
    console.print(f"[bold green]Scanning Polymarket Leaderboard ({args.period}) for top sports / master traders...[/bold green]")
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
    mode_str = "[cyan]DRY-RUN / PAPER TRADING (FAKE)[/cyan]" if config.dry_run else "[bold red]LIVE EXECUTION (REAL)[/bold red]"
    print_banner(mode="SIMULAÇÃO (FAKE)" if config.dry_run else "DINHEIRO REAL (LIVE)")

    risk_mgr = RiskManager(config.risk)
    executor = CopyExecutor(config, risk_mgr)
    tracker = CopyTracker(config, executor, risk_mgr)

    # Risk Config Summary
    summary_table = Table(show_header=False, box=None)
    summary_table.add_row("[bold]Modo Atual do Robô:[/bold]", mode_str)
    summary_table.add_row("[bold]Limite Diário (Daily Budget Cap):[/bold]", f"${config.risk.daily_budget_usd:,.2f}")
    summary_table.add_row("[bold]Exposição Máx por Mercado:[/bold]", f"${config.risk.max_per_market_usd:,.2f}")
    summary_table.add_row("[bold]Tamanho Máximo por Trade:[/bold]", f"${config.risk.max_trade_size_usd:,.2f}")
    summary_table.add_row("[bold]Faixa de Preço Permitida:[/bold]", f"${config.risk.min_price:.2f} - ${config.risk.max_price:.2f}")
    summary_table.add_row("[bold]Tolerância de Slippage:[/bold]", f"{config.risk.slippage_tolerance_pct}%")
    summary_table.add_row("[bold]Venda Espelho Automática:[/bold]", str(config.risk.auto_exit_on_sell))
    tp_str = f"[green]ATIVO[/green] (Preço >= ${config.risk.take_profit_price:.2f} ou Lucro >= +{config.risk.take_profit_min_gain_pct:.0f}%)" if getattr(config.risk, "auto_take_profit", True) else "[red]DESATIVADO[/red]"
    summary_table.add_row("[bold]Auto Take-Profit (Lucro):[/bold]", tp_str)
    summary_table.add_row("[bold]Arquivo de Trades (JSONL):[/bold]", f"[cyan]{config.trades_log_file}[/cyan]")
    summary_table.add_row("[bold]Arquivo de Estado (JSON):[/bold]", f"[cyan]{config.portfolio_state_file}[/cyan]")
    console.print(Panel(summary_table, title="[bold]⚙️ Configurações de Risco & Parâmetros[/bold]", border_style="blue"))

    # Separated Portfolios: Paper vs Live
    paper_metrics = tracker._get_portfolio_metrics(mode="paper")
    live_metrics = tracker._get_portfolio_metrics(mode="live")

    # Paper Table
    paper_table = Table(show_header=False, box=None)
    paper_table.add_row("[bold]Saldo Inicial:[/bold]", f"${paper_metrics['initial_cash_usd']:,.2f}")
    paper_table.add_row("[bold]Saldo em Caixa:[/bold]", f"${paper_metrics['cash_usd']:,.2f}")
    paper_table.add_row("[bold]Valor em Posições:[/bold]", f"${paper_metrics['positions_value_usd']:,.2f}")
    paper_table.add_row("[bold]Patrimônio Total (Equity):[/bold]", f"[bold cyan]${paper_metrics['total_equity_usd']:,.2f}[/bold cyan]")
    paper_pnl_color = "green" if paper_metrics['realized_pnl_usd'] >= 0 else "red"
    paper_table.add_row("[bold]Lucro Realizado (PnL):[/bold]", f"[{paper_pnl_color}]${paper_metrics['realized_pnl_usd']:+,.2f}[/{paper_pnl_color}]")
    paper_table.add_row("[bold]Trades Totais:[/bold]", f"{paper_metrics['total_trades_count']} ({paper_metrics['successful_trades']} executados, {paper_metrics['failed_trades']} falhas)")
    paper_table.add_row("[bold]Posições Abertas:[/bold]", f"{paper_metrics['open_positions_count']}")
    console.print(Panel(paper_table, title="[bold cyan]🧪 Estatísticas: Dinheiro Fake (Simulação / Paper)[/bold cyan]", border_style="cyan"))

    # Live Table
    live_table = Table(show_header=False, box=None)
    live_table.add_row("[bold]Saldo Inicial:[/bold]", f"${live_metrics['initial_cash_usd']:,.2f}")
    live_table.add_row("[bold]Saldo em Caixa:[/bold]", f"${live_metrics['cash_usd']:,.2f}")
    live_table.add_row("[bold]Valor em Posições:[/bold]", f"${live_metrics['positions_value_usd']:,.2f}")
    live_table.add_row("[bold]Patrimônio Total (Equity):[/bold]", f"[bold emerald]${live_metrics['total_equity_usd']:,.2f}[/bold emerald]")
    live_pnl_color = "green" if live_metrics['realized_pnl_usd'] >= 0 else "red"
    live_table.add_row("[bold]Lucro Realizado (PnL):[/bold]", f"[{live_pnl_color}]${live_metrics['realized_pnl_usd']:+,.2f}[/{live_pnl_color}]")
    live_table.add_row("[bold]Trades Totais:[/bold]", f"{live_metrics['total_trades_count']} ({live_metrics['successful_trades']} executados, {live_metrics['failed_trades']} falhas)")
    live_table.add_row("[bold]Posições Abertas:[/bold]", f"{live_metrics['open_positions_count']}")
    console.print(Panel(live_table, title="[bold green]⚡ Estatísticas: Dinheiro Real (Live Capital)[/bold green]", border_style="green"))

    # Tracked Traders
    traders_table = Table(title=f"Master Traders Configurados ({len(config.traders)})", border_style="cyan")
    table_cols = ["#", "Status", "Trader", "Endereço", "Win Rate 7D", "Lucro 7D", "Categoria", "Sizing"]
    for c in table_cols:
        traders_table.add_column(c)

    for i, t in enumerate(config.traders):
        status_str = "[green]ATIVO[/green]" if t.enabled else "[red]PAUSADO[/red]"
        wr_str = f"{t.win_rate_7d * 100:.1f}%"
        pnl_str = f"+${t.pnl_7d:,.2f}" if t.pnl_7d >= 0 else f"-${abs(t.pnl_7d):,.2f}"
        sizing_str = f"${t.copy_amount_usd:,.2f} fixo" if config.sizing.mode == "fixed" else f"{config.sizing.mirror_percent_cap}% espelho"
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
    
    mode = getattr(args, "mode", "all")
    modes_to_show = ["paper", "live"] if mode == "all" else [mode]

    for m in modes_to_show:
        metrics = tracker._get_portfolio_metrics(mode=m)
        m_title = "🧪 DINHEIRO FAKE (SIMULAÇÃO)" if m == "paper" else "⚡ DINHEIRO REAL (LIVE)"
        m_color = "cyan" if m == "paper" else "green"
        pnl_color = "green" if metrics['realized_pnl_usd'] >= 0 else "red"

        console.print(Panel(
            f"[bold]Caixa:[/bold] ${metrics['cash_usd']:,.2f} | "
            f"[bold]Posições:[/bold] ${metrics['positions_value_usd']:,.2f} | "
            f"[bold]Equity:[/bold] ${metrics['total_equity_usd']:,.2f} | "
            f"[bold]PnL Realizado:[/bold] [{pnl_color}]${metrics['realized_pnl_usd']:+,.2f}[/{pnl_color}] | "
            f"[bold]Trades:[/bold] {metrics['total_trades_count']} ({metrics['successful_trades']} ok, {metrics['failed_trades']} falhas)",
            title=f"[bold {m_color}]Visão Geral: {m_title}[/bold {m_color}]",
            border_style=m_color
        ))

        positions = tracker.portfolio.get(m, {}).get("positions", {})
        if not positions:
            console.print(f"[yellow]Nenhuma posição aberta no modo {m.upper()}.[/yellow]\n")
            continue

        table = Table(title=f"Posições Abertas ({m.upper()})", border_style=m_color)
        table.add_column("URL Polymarket", style="cyan")
        table.add_column("Desfecho", style="bold yellow")
        table.add_column("Cotas", justify="right", style="magenta")
        table.add_column("Preço Médio", justify="right", style="yellow")
        table.add_column("Custo Total", justify="right", style="green")

        for key, pos in positions.items():
            p_slug = pos.get("event_slug") or pos.get("market_slug", key.split(":")[0])
            p_url = pos.get("market_url") or (f"https://polymarket.com/event/{p_slug}" if p_slug else "-")
            table.add_row(
                p_url,
                pos.get("outcome", "Yes"),
                f"{pos.get('shares', 0):,.2f}",
                f"${pos.get('avg_price', 0):.3f}",
                f"${pos.get('total_cost', 0):,.2f}"
            )
        console.print(table)
        console.print("")


def cmd_logs(args, config: BotConfig):
    if not os.path.exists(config.trades_log_file):
        console.print(f"[yellow]Arquivo de log de trades não encontrado em {config.trades_log_file}[/yellow]")
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
        console.print("[yellow]O log de trades está vazio.[/yellow]")
        return

    filter_mode = getattr(args, "mode", "all")
    if filter_mode in ("paper", "live"):
        lines = [r for r in lines if (r.get("bot_execution", {}).get("mode") or "paper").lower() == filter_mode.lower()]

    table = Table(title=f"Log de Trades Recentes (Exibindo últimos {min(len(lines), args.limit)} de {len(lines)})", border_style="cyan")
    table.add_column("Horário (UTC)", style="dim")
    table.add_column("Modo", style="bold")
    table.add_column("Master", style="bold white")
    table.add_column("Ação", style="bold")
    table.add_column("URL Polymarket", style="cyan")
    table.add_column("Desfecho", style="yellow")
    table.add_column("Tamanho Mestre", justify="right")
    table.add_column("Copiado", justify="right", style="green")
    table.add_column("Status", style="bold")

    for rec in lines[-args.limit:]:
        m_trade = rec.get("master_trade", {})
        b_exec = rec.get("bot_execution", {})
        market = rec.get("market", {})
        master = rec.get("master_trader", {})

        trade_mode = (b_exec.get("mode") or "paper").upper()
        mode_str = "[cyan]🧪 FAKE[/cyan]" if trade_mode == "PAPER" else "[green]⚡ REAL[/green]"

        action = b_exec.get("action") or m_trade.get("side", "BUY")
        action_color = "green" if action == "BUY" else "red"
        action_str = f"[{action_color}]{action}[/{action_color}]"

        status = b_exec.get("status", rec.get("status", "UNKNOWN"))
        status_color = "green" if status == "EXECUTED" else ("yellow" if status in ("SKIPPED", "REJECTED_BY_RISK") else "red")
        status_str = f"[{status_color}]{status}[/{status_color}]"

        m_slug = market.get("event_slug") or market.get("slug", "")
        m_url = market.get("url") or (f"https://polymarket.com/event/{m_slug}" if m_slug else "-")

        table.add_row(
            rec.get("timestamp", "")[:19].replace("T", " "),
            mode_str,
            master.get("name") or (master.get("address", "")[:8] if master.get("address") else "Unknown"),
            action_str,
            m_url,
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

    if not config.dry_run:
        console.print("[dim]Syncing on-chain live wallet balance...[/dim]")
        info = executor.get_wallet_balance()
        if info.get("success"):
            bal = float(info.get("balance_usd", 0.0))
            config.live_initial_cash_usd = bal
            save_config(config)
            if "live" in tracker.portfolio:
                tracker.portfolio["live"]["cash_usd"] = bal
                tracker.portfolio["live"]["initial_cash_usd"] = bal
                tracker._save_portfolio_state()
            console.print(f"[bold green]✓ Live Wallet Balance ({info.get('address', '')[:8]}...): ${bal:,.2f} USDC (Polygon)[/bold green]")
        else:
            console.print(f"[yellow]Warning: Could not sync live wallet balance: {info.get('error')}[/yellow]")

        console.print("[dim]Checking Polymarket CLOB API key authentication...[/dim]")
        if executor.check_and_ensure_clob_auth():
            console.print("[bold green]✓ Polymarket CLOB API Session: Authenticated & Active[/bold green]")
        else:
            console.print("[yellow]Warning: CLOB API key check returned warning; will auto-recover on order placement.[/yellow]")

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

                    m_slug = market.get("event_slug") or market.get("slug", "")
                    m_url = market.get("url") or (f"https://polymarket.com/event/{m_slug}" if m_slug else "")

                    status = b_exec.get("status", "EXECUTED")
                    if status == "EXECUTED":
                        action = b_exec.get("action", "BUY")
                        action_color = "green" if action == "BUY" else "magenta"
                        console.print(
                            f"[{action_color}]✓ {action} EXECUTED:[/{action_color}] "
                            f"Outcome: [bold yellow]{market.get('outcome')}[/bold yellow] on [cyan]{m_url or m_slug}[/cyan] | "
                            f"Copied: [green]${b_exec.get('amount_usd', 0):.2f}[/green] ({b_exec.get('shares', 0):.2f} shs @ ${b_exec.get('price', 0):.3f}) | "
                            f"Master: [bold white]{master.get('name')}[/bold white] (${m_trade.get('size_usd', 0):,.2f})"
                        )
                    elif status == "FAILED":
                        console.print(
                            f"[red]✗ TRADE FAILED:[/red] {m_url or m_slug} ({market.get('outcome')}) | "
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


def cmd_sync_wallet(args, config: BotConfig):
    console.print("[bold cyan]Consultando saldo on-chain da carteira real via Bullpen...[/bold cyan]")
    risk_mgr = RiskManager(config.risk)
    executor = CopyExecutor(config, risk_mgr)
    tracker = CopyTracker(config, executor, risk_mgr)
    info = executor.get_wallet_balance()
    if info.get("success"):
        bal = float(info.get("balance_usd", 0.0))
        addr = info.get("address", "")
        config.live_initial_cash_usd = bal
        save_config(config)
        if "live" in tracker.portfolio:
            tracker.portfolio["live"]["cash_usd"] = bal
            tracker.portfolio["live"]["initial_cash_usd"] = bal
            tracker._save_portfolio_state()
        console.print(f"[bold green]✓ Saldo da Carteira ({addr}) sincronizado com sucesso: ${bal:,.2f} USDC (Polygon)[/bold green]")
    else:
        console.print(f"[bold red]✗ Erro ao consultar carteira: {info.get('error')}[/bold red]")


def cmd_close_position(args, config: BotConfig):
    risk_mgr = RiskManager(config.risk)
    executor = CopyExecutor(config, risk_mgr)
    tracker = CopyTracker(config, executor, risk_mgr)
    res = tracker.manual_close_position(args.position_key, mode=args.mode)
    if res.get("success"):
        console.print(f"[bold green]✓ {res.get('message')}[/bold green]")
    else:
        console.print(f"[bold red]✗ {res.get('error')}[/bold red]")


def cmd_reset_stats(args, config: BotConfig):
    risk_mgr = RiskManager(config.risk)
    executor = CopyExecutor(config, risk_mgr)
    tracker = CopyTracker(config, executor, risk_mgr)
    target_mode = None if args.mode == "all" else args.mode
    res = tracker.reset_statistics(mode=target_mode)
    if res.get("success"):
        console.print(f"[bold green]✓ {res.get('message')}[/bold green]")
    else:
        console.print(f"[bold red]✗ {res.get('error')}[/bold red]")


def main():
    parser = argparse.ArgumentParser(description="Polymarket Copytrading Bot")
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # sports
    p_sports = subparsers.add_parser("sports", help="Scan active sports events, matches, and odds on Polymarket")
    p_sports.add_argument("--limit", type=int, default=10, help="Limit per sport category")
    p_sports.add_argument("--top", type=int, default=20, help="Top sports signals to show")

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

    # sync-wallet
    p_sync = subparsers.add_parser("sync-wallet", help="Sync on-chain wallet balance from Polygon into bot live state")

    # close-position
    p_close = subparsers.add_parser("close-position", help="Manually close and liquidate an open position")
    p_close.add_argument("position_key", help="Position key to close (e.g. market_slug:Outcome)")
    p_close.add_argument("--mode", default=None, choices=["paper", "live"], help="Portfolio mode (paper or live)")

    # reset-stats
    p_reset = subparsers.add_parser("reset-stats", help="Reset portfolio statistics and clear trades log")
    p_reset.add_argument("--mode", default="all", choices=["paper", "live", "all"], help="Mode to reset (paper, live, or all)")

    # portfolio
    p_port = subparsers.add_parser("portfolio", help="Show current open positions and equity")
    p_port.add_argument("--mode", default="all", choices=["paper", "live", "all"], help="Filter portfolio by mode (paper, live, or all)")

    # logs
    p_logs = subparsers.add_parser("logs", help="Display recent trades from the JSONL log")
    p_logs.add_argument("--limit", type=int, default=20, help="Number of recent records to display")
    p_logs.add_argument("--mode", default="all", choices=["paper", "live", "all"], help="Filter logs by mode (paper, live, or all)")

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

    if args.command == "sports":
        cmd_sports(args, config)
    elif args.command == "scan":
        cmd_scan(args, config)
    elif args.command == "status":
        cmd_status(args, config)
    elif args.command == "sync-wallet":
        cmd_sync_wallet(args, config)
    elif args.command == "close-position":
        cmd_close_position(args, config)
    elif args.command == "reset-stats":
        cmd_reset_stats(args, config)
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
