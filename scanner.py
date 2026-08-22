"""
Leaderboard Scanner for finding top-performing Polymarket traders.
"""
import json
import subprocess
from typing import List, Dict, Any, Optional
from config import MasterTrader


class LeaderboardScanner:
    def __init__(self, bullpen_path: str = "/home/nergal/.bullpen/bin/bullpen"):
        self.bullpen_path = bullpen_path

    def _run_cli(self, args: List[str]) -> Dict[str, Any]:
        cmd = [self.bullpen_path] + args + ["--output", "json"]
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False
        )
        if result.returncode != 0:
            raise RuntimeError(f"Bullpen CLI error ({result.returncode}): {result.stderr.strip()}")
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Failed to parse JSON from CLI output: {e}\nRaw output: {result.stdout}")

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
        """
        # Fetch by Win Rate
        wr_raw = self._run_cli([
            "polymarket", "data", "leaderboard",
            "--time-period", time_period,
            "--sort", "win-rate",
            "--hide-bots",
            "--hide-farmers",
            "--limit", str(limit)
        ])

        # Fetch by PnL
        pnl_raw = self._run_cli([
            "polymarket", "data", "leaderboard",
            "--time-period", time_period,
            "--sort", "pnl",
            "--hide-bots",
            "--hide-farmers",
            "--limit", str(limit)
        ])

        all_entries = {}
        for entry in wr_raw.get("leaderboard", []) + pnl_raw.get("leaderboard", []):
            addr = entry.get("address")
            if addr and addr not in all_entries:
                all_entries[addr] = entry

        curated: List[MasterTrader] = []
        for addr, t in all_entries.items():
            wr = t.get(f"win_rate_{time_period}") if t.get(f"win_rate_{time_period}") is not None else t.get("win_rate", 0)
            pnl = t.get(f"realized_pnl_{time_period}") or 0.0
            vol = t.get(f"volume_{time_period}") or 0.0
            total_vol = t.get("volume_all") or t.get("lifetime_volume") or 0.0
            risk = (t.get("risk_tier") or "low").lower()

            if exclude_high_risk and (risk in ("high", "degen") or (t.get("max_drawdown") or 0) > 0.40):
                continue

            if wr >= min_win_rate and pnl >= min_pnl and (vol >= min_volume or total_vol >= min_volume):
                name = t.get("display_name") or f"{addr[:6]}...{addr[-4:]}"
                curated.append(
                    MasterTrader(
                        address=addr,
                        name=name,
                        win_rate_7d=float(wr),
                        pnl_7d=float(pnl),
                        volume_7d=float(vol),
                        category=t.get("primary_category") or "Mixed",
                        risk_tier=risk,
                        style=t.get("trading_style") or "balanced",
                        enabled=True,
                        copy_amount_usd=10.0
                    )
                )

        # Sort by PnL descending
        curated.sort(key=lambda x: x.pnl_7d, reverse=True)
        return curated
