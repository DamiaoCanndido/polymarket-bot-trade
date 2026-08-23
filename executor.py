"""
Copytrade executor module for interacting with Polymarket via Bullpen CLI.
"""
import json
import subprocess
import logging
from typing import Dict, Any, List, Optional
from config import BotConfig, MasterTrader
from risk_manager import RiskManager

logger = logging.getLogger("PolymarketExecutor")


class CopyExecutor:
    def __init__(self, config: BotConfig, risk_manager: RiskManager):
        self.config = config
        self.risk_manager = risk_manager
        self.bullpen_path = config.bullpen_path

    def _run_cli(self, args: List[str]) -> Dict[str, Any]:
        cmd = [self.bullpen_path] + args + ["--output", "json"]
        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False
            )
            if result.returncode != 0:
                err_msg = result.stderr.strip() or f"CLI returned code {result.returncode}"
                return {"success": False, "error": err_msg, "code": result.returncode}

            try:
                parsed = json.loads(result.stdout)
                if isinstance(parsed, dict):
                    parsed["success"] = True
                    return parsed
                return {"success": True, "data": parsed}
            except json.JSONDecodeError:
                return {"success": True, "raw": result.stdout.strip()}
        except Exception as exc:
            return {"success": False, "error": str(exc), "code": -1}

    def get_portfolio_status(self) -> Dict[str, Any]:
        """
        Retrieves user Polymarket wallet status, balances, and open positions.
        """
        return self._run_cli(["polymarket", "positions"])

    def get_wallet_balance(self) -> Dict[str, Any]:
        """
        Queries Bullpen CLI for on-chain/CLOB balance and wallet address.
        """
        res = self._run_cli(["polymarket", "preflight"])
        if res.get("success"):
            wallet_addr = res.get("wallet_address") or ""
            bal_str = str(res.get("balance_usd") or res.get("wallet_balance_usd") or "$0.00")
            bal_clean = bal_str.replace("$", "").replace(",", "").strip()
            try:
                bal_float = float(bal_clean)
            except ValueError:
                try:
                    bal_float = float(res.get("balance") or 0.0)
                except ValueError:
                    bal_float = 0.0
            return {
                "success": True,
                "address": wallet_addr,
                "balance_usd": bal_float,
                "allowance_usd": res.get("allowance_usd", "Unlimited"),
                "approvals_ok": res.get("approvals_ok", True)
            }
        return {"success": False, "error": res.get("error", "Failed to query wallet balance"), "balance_usd": 0.0}

    def get_market_prices(self, market_slug: str) -> Dict[str, float]:
        """
        Retrieves live outcome prices for a given market slug.
        Returns a dict mapping outcome name -> price float (e.g. {"Team Spirit": 0.999, "TEAM VISION": 0.01}).
        """
        if not market_slug:
            return {}

        prices: Dict[str, float] = {}

        # 1. Primary: Bullpen CLI CLOB price check
        res = self._run_cli(["polymarket", "price", market_slug])
        if res.get("success"):
            outcomes = res.get("outcomes") or []
            if isinstance(outcomes, list):
                for item in outcomes:
                    name = item.get("outcome")
                    p = item.get("last_trade")
                    if p is None:
                        p = item.get("midpoint")
                    if p is None:
                        p = item.get("best_bid")
                    if name and p is not None:
                        try:
                            prices[name] = float(p)
                        except (ValueError, TypeError):
                            pass
            if prices:
                return prices

        # 2. Fallback: Gamma API
        try:
            import urllib.request
            url = f"https://gamma-api.polymarket.com/events?slug={market_slug}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode())
                if data and isinstance(data, list):
                    markets = data[0].get("markets", [])
                    for m in markets:
                        if m.get("slug") == market_slug or len(markets) == 1:
                            out_names = json.loads(m.get("outcomes", "[]")) if isinstance(m.get("outcomes"), str) else m.get("outcomes", [])
                            out_prices = json.loads(m.get("outcomePrices", "[]")) if isinstance(m.get("outcomePrices"), str) else m.get("outcomePrices", [])
                            for name, pr_str in zip(out_names, out_prices):
                                try:
                                    prices[name] = float(pr_str)
                                except (ValueError, TypeError):
                                    pass
        except Exception:
            pass

        return prices

    def list_copy_subscriptions(self) -> Dict[str, Any]:
        """
        Lists all active copy trading subscriptions from Bullpen tracker.
        """
        return self._run_cli(["tracker", "copy", "list"])

    def subscribe_to_trader(
        self,
        trader: MasterTrader,
        amount_usd: Optional[float] = None,
        dry_run: Optional[bool] = None
    ) -> Dict[str, Any]:
        """
        Creates or updates a copytrading subscription for a master trader.
        """
        is_dry_run = self.config.dry_run if dry_run is None else dry_run
        size = amount_usd or trader.copy_amount_usd or self.config.sizing.fixed_amount_usd

        args = [
            "tracker", "copy", "start", trader.address,
            "--amount", str(size),
            "--slippage", str(self.config.risk.slippage_tolerance_pct),
            "--max-per-market", str(self.config.risk.max_per_market_usd),
            "--daily-limit", str(self.config.risk.daily_budget_usd),
            "--max-trade-size", str(self.config.risk.max_trade_size_usd),
            "--min-trade-size", str(self.config.risk.min_trade_size_usd),
            "--price-range-min", str(self.config.risk.min_price),
            "--price-range-max", str(self.config.risk.max_price),
            "--min-time-to-resolution", str(self.config.risk.min_hours_to_resolution),
            "--exit-behavior", "mirror_sells" if self.config.risk.auto_exit_on_sell else "manual",
            "--nickname", trader.name or trader.address[:8],
            "--yes"
        ]

        if is_dry_run:
            args.append("--dry-run")

        return self._run_cli(args)

    def unsubscribe_trader(self, address: str) -> Dict[str, Any]:
        """
        Stops a copy trading subscription.
        """
        return self._run_cli(["tracker", "copy", "stop", address, "--yes"])

    def fetch_master_feed(self, address: Optional[str] = None, limit: int = 25) -> List[Dict[str, Any]]:
        """
        Fetches the recent trade feed for tracked master addresses.
        """
        args = ["tracker", "feed", "--limit", str(limit)]
        if address:
            args.extend(["--address", address])

        res = self._run_cli(args)
        if res.get("success"):
            data = res.get("trades") or res.get("data") or res.get("feed") or []
            if isinstance(data, list):
                return data
        return []

    def execute_buy(
        self,
        market_slug: str,
        outcome: str,
        amount_usd: float,
        max_price: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Executes a live Polymarket buy in USD.
        """
        args = [
            "polymarket", "buy",
            market_slug,
            outcome,
            f"{amount_usd:.2f}",
            "--yes"
        ]
        if max_price is not None:
            args.extend(["--max-price", f"{max_price:.4f}"])

        return self._run_cli(args)

    def execute_sell(
        self,
        market_slug: str,
        outcome: str,
        shares: Optional[float] = None,
        sell_all: bool = False,
        min_price: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Executes a live Polymarket sell in share quantity.
        """
        args = [
            "polymarket", "sell",
            market_slug,
            outcome
        ]
        if sell_all or shares is None:
            args.append("--max")
        else:
            args.append(f"{shares:.2f}")

        args.append("--yes")
        if min_price is not None:
            args.extend(["--min-price", f"{min_price:.4f}"])

        return self._run_cli(args)
