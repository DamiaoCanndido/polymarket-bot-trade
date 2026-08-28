import os
import sys
import time
import json
import subprocess
import logging
import urllib.request
from typing import List, Dict, Any, Optional
from config import MasterTrader

logger = logging.getLogger("LeaderboardScanner")


class LeaderboardScanner:
    def __init__(self, bullpen_path: str = "/home/nergal/.bullpen/bin/bullpen"):
        self.bullpen_path = bullpen_path

    def _run_cli(self, args: List[str]) -> Optional[Dict[str, Any]]:
        cmd = [self.bullpen_path] + args + ["--output", "json"]
        env = os.environ.copy()
        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                check=False
            )
            stdout_clean = (result.stdout or "").strip()
            stderr_clean = (result.stderr or "").strip()

            if stdout_clean:
                try:
                    data = json.loads(stdout_clean)
                    if isinstance(data, dict):
                        if result.returncode != 0 or data.get("status") == "error":
                            err_msg = data.get("error") or data.get("message") or stderr_clean
                            logger.warning(f"Bullpen CLI returned non-zero ({result.returncode}): {err_msg}")
                            return None
                        return data
                except Exception:
                    pass

            if result.returncode != 0:
                logger.warning(f"Bullpen CLI error ({result.returncode}): {stderr_clean or stdout_clean}")
                return None

            return None
        except Exception as exc:
            logger.warning(f"Bullpen CLI execution failed: {exc}")
            return None

    def _fetch_direct_polymarket_leaderboard(self, period: str = "week", sort_by: str = "PNL", limit: int = 50) -> List[Dict[str, Any]]:
        """
        Direct fallback to Polymarket Data API if Bullpen CLI leaderboard is slow or unavailable.
        """
        period_map = {"1d": "day", "day": "day", "7d": "week", "week": "week", "30d": "month", "month": "month", "all": "all"}
        p_clean = period_map.get(period, "week")
        url = f"https://data-api.polymarket.com/v1/leaderboard?timePeriod={p_clean}&orderBy={sort_by}&limit={limit}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read().decode())
                if isinstance(data, list):
                    return data
        except Exception as e:
            logger.warning(f"Direct Polymarket leaderboard fetch failed ({sort_by}): {e}")
        return []

    def fetch_top_traders(
        self,
        time_period: str = "7d",
        min_win_rate: float = 0.65,
        min_pnl: float = 5000.0,
        min_volume: float = 5000.0,
        exclude_high_risk: bool = True,
        limit: int = 100
    ) -> List[MasterTrader]:
        """
        Fetches and curates top traders from Polymarket data leaderboards.
        Tries Bullpen CLI first, then falls back seamlessly to Polymarket Data API.
        """
        period_alias_map = {"1d": "day", "7d": "week", "30d": "month", "all": "all"}
        clean_period = period_alias_map.get(time_period, time_period)

        all_entries: Dict[str, Dict[str, Any]] = {}

        # 1. Try Bullpen CLI with standard --period
        res_cli = self._run_cli(["polymarket", "data", "leaderboard", "--period", clean_period, "--limit", str(limit)])
        if res_cli and isinstance(res_cli.get("leaderboard"), list):
            for entry in res_cli["leaderboard"]:
                addr = entry.get("address")
                if addr:
                    all_entries[addr.lower()] = {
                        "address": addr,
                        "name": entry.get("username") or entry.get("display_name") or f"{addr[:6]}...{addr[-4:]}",
                        "pnl": float(entry.get("pnl") or 0.0),
                        "volume": float(entry.get("volume") or 0.0),
                        "win_rate": float(entry.get("win_rate") or 0.75),
                        "category": "Sports" if "sports" in (entry.get("username") or "").lower() else "Mixed",
                        "risk_tier": "low",
                        "style": "balanced"
                    }

        # 2. Fallback or enrich with Direct Polymarket Data API
        if len(all_entries) < limit:
            for sort_by in ("PNL", "VOL"):
                direct_items = self._fetch_direct_polymarket_leaderboard(period=clean_period, sort_by=sort_by, limit=limit)
                for item in direct_items:
                    addr = item.get("proxyWallet") or item.get("address")
                    if addr and addr.lower() not in all_entries:
                        pnl = float(item.get("pnl") or 0.0)
                        vol = float(item.get("vol") or item.get("volume") or 0.0)
                        uname = item.get("userName") or item.get("name") or f"{addr[:6]}...{addr[-4:]}"
                        all_entries[addr.lower()] = {
                            "address": addr,
                            "name": uname,
                            "pnl": pnl,
                            "volume": vol,
                            "win_rate": 0.75 if pnl > 0 else 0.50,
                            "category": "Crypto" if any(c in uname.lower() for c in ("eth", "btc", "sol", "crypto")) else "Mixed",
                            "risk_tier": "low",
                            "style": "scalper" if vol > 1000000 else "balanced"
                        }

        curated: List[MasterTrader] = []
        for addr_lower, t in all_entries.items():
            wr = t.get("win_rate", 0.75)
            pnl = t.get("pnl", 0.0)
            vol = t.get("volume", 0.0)
            risk = t.get("risk_tier", "low")

            if exclude_high_risk and risk in ("high", "degen"):
                continue

            if wr >= min_win_rate and pnl >= min_pnl and vol >= min_volume:
                curated.append(
                    MasterTrader(
                        address=t["address"],
                        name=t["name"],
                        win_rate_7d=float(wr),
                        pnl_7d=float(pnl),
                        volume_7d=float(vol),
                        category=t.get("category", "Mixed"),
                        risk_tier=risk,
                        style=t.get("style", "balanced"),
                        enabled=True,
                        copy_amount_usd=1.0
                    )
                )

        # If filtering is too aggressive due to win_rate or volume, ensure we still return high-PnL traders
        if not curated and all_entries:
            for addr_lower, t in all_entries.items():
                if t.get("pnl", 0.0) >= min_pnl:
                    curated.append(
                        MasterTrader(
                            address=t["address"],
                            name=t["name"],
                            win_rate_7d=float(t.get("win_rate", 0.75)),
                            pnl_7d=float(t.get("pnl", 0.0)),
                            volume_7d=float(t.get("volume", 0.0)),
                            category=t.get("category", "Mixed"),
                            risk_tier="low",
                            style="balanced",
                            enabled=True,
                            copy_amount_usd=1.0
                        )
                    )

        # Sort by PnL descending
        curated.sort(key=lambda x: x.pnl_7d, reverse=True)
        return curated
