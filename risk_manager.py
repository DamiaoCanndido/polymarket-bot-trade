"""
Risk and position safety management for copy trading.
"""
from datetime import datetime, date
from typing import Dict, Any, Tuple, Optional
from config import RiskConfig


class RiskManager:
    def __init__(self, risk_config: RiskConfig):
        self.config = risk_config
        self.last_reset_date: date = datetime.utcnow().date()
        self.daily_spend_usd: Dict[str, float] = {"paper": 0.0, "live": 0.0}
        self.market_exposure_usd: Dict[str, Dict[str, float]] = {"paper": {}, "live": {}}

    def _check_and_reset_daily(self) -> None:
        today = datetime.utcnow().date()
        if today != self.last_reset_date:
            self.daily_spend_usd = {"paper": 0.0, "live": 0.0}
            self.market_exposure_usd = {"paper": {}, "live": {}}
            self.last_reset_date = today

    def validate_trade(
        self,
        market_slug: str,
        price: float,
        intended_usd: float,
        side: str = "buy",
        mode: str = "paper"
    ) -> Tuple[bool, str, float]:
        """
        Validates if a trade conforms to risk rules and returns:
        (is_valid: bool, reason: str, adjusted_size_usd: float)
        """
        self._check_and_reset_daily()
        mode_key = "live" if mode == "live" else "paper"
        spend = self.daily_spend_usd[mode_key]
        exposure = self.market_exposure_usd[mode_key]

        # Check Price Bounds (only for buy orders)
        if side.lower() == "buy":
            if price < self.config.min_price:
                return False, f"Price {price:.3f} is below minimum allowed ({self.config.min_price:.2f})", 0.0
            if price > self.config.max_price:
                return False, f"Price {price:.3f} is above maximum allowed ({self.config.max_price:.2f})", 0.0

        # Adjust trade size within min and max boundaries
        adjusted_size = min(intended_usd, self.config.max_trade_size_usd)
        if adjusted_size < self.config.min_trade_size_usd:
            return False, f"Intended size ${adjusted_size:.2f} is below minimum ${self.config.min_trade_size_usd:.2f}", 0.0

        # Check Daily Budget Cap
        if side.lower() == "buy":
            if spend + adjusted_size > self.config.daily_budget_usd:
                remaining_budget = max(0.0, self.config.daily_budget_usd - spend)
                if remaining_budget < self.config.min_trade_size_usd:
                    return False, f"Daily budget reached ({mode_key.upper()}: ${spend:.2f} / ${self.config.daily_budget_usd:.2f})", 0.0
            # Check Per-Market Exposure Cap
            current_market_exp = exposure.get(market_slug, 0.0)
            if current_market_exp + adjusted_size > self.config.max_per_market_usd:
                remaining_market = max(0.0, self.config.max_per_market_usd - current_market_exp)
                if remaining_market < self.config.min_trade_size_usd:
                    return False, f"Max market exposure reached for {market_slug} ({mode_key.upper()}: ${current_market_exp:.2f} / ${self.config.max_per_market_usd:.2f})", 0.0
                adjusted_size = remaining_market

        # Check minimum shares for Polymarket CLOB compatibility (minimum 5 shares on live CLOB)
        if side.lower() == "buy" and mode_key == "live" and price > 0:
            est_shares = adjusted_size / price
            if est_shares < 5.0:
                needed_usd = round(5.0 * price, 2)
                # Auto-adjust if within budget and maximum single trade limit
                if needed_usd <= self.config.max_trade_size_usd and (spend + needed_usd) <= self.config.daily_budget_usd:
                    adjusted_size = needed_usd
                else:
                    return False, f"Tamanho da ordem ({est_shares:.2f} cotas a ${price:.2f}) abaixo do mínimo da Polymarket (mínimo 5 cotas = ${needed_usd:.2f})", 0.0

        return True, "Approved", adjusted_size

    def record_trade_execution(
        self,
        market_slug: str,
        actual_usd: float,
        side: str = "buy",
        mode: str = "paper"
    ) -> None:
        """
        Updates internal risk tracking after a trade executes.
        """
        self._check_and_reset_daily()
        mode_key = "live" if mode == "live" else "paper"
        if side.lower() == "buy":
            self.daily_spend_usd[mode_key] += actual_usd
            self.market_exposure_usd[mode_key][market_slug] = (
                self.market_exposure_usd[mode_key].get(market_slug, 0.0) + actual_usd
            )
        elif side.lower() == "sell":
            # Reduce market exposure
            current = self.market_exposure_usd[mode_key].get(market_slug, 0.0)
            self.market_exposure_usd[mode_key][market_slug] = max(0.0, current - actual_usd)

    def get_risk_summary(self, mode: Optional[str] = None) -> Dict[str, Any]:
        self._check_and_reset_daily()
        if mode in ("paper", "live"):
            mode_key = mode
            spend = self.daily_spend_usd[mode_key]
            exp_dict = self.market_exposure_usd[mode_key]
            return {
                "mode": mode_key,
                "daily_spent_usd": spend,
                "daily_budget_usd": self.config.daily_budget_usd,
                "budget_utilization_pct": (spend / self.config.daily_budget_usd * 100) if self.config.daily_budget_usd > 0 else 0,
                "active_markets_count": len([m for m, exp in exp_dict.items() if exp > 0]),
                "total_open_exposure_usd": sum(exp_dict.values())
            }

        paper_sum = self.get_risk_summary(mode="paper")
        live_sum = self.get_risk_summary(mode="live")
        return {
            "paper": paper_sum,
            "live": live_sum,
            "daily_spent_usd": paper_sum["daily_spent_usd"],
            "daily_budget_usd": self.config.daily_budget_usd,
            "budget_utilization_pct": paper_sum["budget_utilization_pct"],
            "active_markets_count": paper_sum["active_markets_count"],
            "total_open_exposure_usd": paper_sum["total_open_exposure_usd"]
        }
