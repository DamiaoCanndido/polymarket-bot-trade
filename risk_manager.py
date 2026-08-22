"""
Risk and position safety management for copy trading.
"""
from datetime import datetime, date
from typing import Dict, Any, Tuple
from config import RiskConfig


class RiskManager:
    def __init__(self, risk_config: RiskConfig):
        self.config = risk_config
        self.daily_spend_usd: float = 0.0
        self.last_reset_date: date = datetime.utcnow().date()
        self.market_exposure_usd: Dict[str, float] = {}  # market_slug -> USD

    def _check_and_reset_daily(self) -> None:
        today = datetime.utcnow().date()
        if today != self.last_reset_date:
            self.daily_spend_usd = 0.0
            self.last_reset_date = today

    def validate_trade(
        self,
        market_slug: str,
        price: float,
        intended_usd: float,
        side: str = "buy"
    ) -> Tuple[bool, str, float]:
        """
        Validates if a trade conforms to risk rules and returns:
        (is_valid: bool, reason: str, adjusted_size_usd: float)
        """
        self._check_and_reset_daily()

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
            if self.daily_spend_usd + adjusted_size > self.config.daily_budget_usd:
                remaining_budget = max(0.0, self.config.daily_budget_usd - self.daily_spend_usd)
                if remaining_budget < self.config.min_trade_size_usd:
                    return False, f"Daily budget reached (${self.daily_spend_usd:.2f} / ${self.config.daily_budget_usd:.2f})", 0.0
                adjusted_size = remaining_budget

            # Check Per-Market Exposure Cap
            current_market_exp = self.market_exposure_usd.get(market_slug, 0.0)
            if current_market_exp + adjusted_size > self.config.max_per_market_usd:
                remaining_market = max(0.0, self.config.max_per_market_usd - current_market_exp)
                if remaining_market < self.config.min_trade_size_usd:
                    return False, f"Max market exposure reached for {market_slug} (${current_market_exp:.2f} / ${self.config.max_per_market_usd:.2f})", 0.0
                adjusted_size = remaining_market

        return True, "Approved", adjusted_size

    def record_trade_execution(self, market_slug: str, actual_usd: float, side: str = "buy") -> None:
        """
        Updates internal risk tracking after a trade executes.
        """
        self._check_and_reset_daily()
        if side.lower() == "buy":
            self.daily_spend_usd += actual_usd
            self.market_exposure_usd[market_slug] = self.market_exposure_usd.get(market_slug, 0.0) + actual_usd
        elif side.lower() == "sell":
            # Reduce market exposure
            current = self.market_exposure_usd.get(market_slug, 0.0)
            self.market_exposure_usd[market_slug] = max(0.0, current - actual_usd)

    def get_risk_summary(self) -> Dict[str, Any]:
        self._check_and_reset_daily()
        return {
            "daily_spent_usd": self.daily_spend_usd,
            "daily_budget_usd": self.config.daily_budget_usd,
            "budget_utilization_pct": (self.daily_spend_usd / self.config.daily_budget_usd * 100) if self.config.daily_budget_usd > 0 else 0,
            "active_markets_count": len([m for m, exp in self.market_exposure_usd.items() if exp > 0]),
            "total_open_exposure_usd": sum(self.market_exposure_usd.values())
        }
