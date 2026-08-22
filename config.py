"""
Configuration management for the Polymarket Copytrading Bot.
"""
from dataclasses import dataclass, field, asdict
import json
import os
from typing import List, Optional

CONFIG_FILE_PATH = os.path.join(os.path.dirname(__file__), "config.json")


@dataclass
class MasterTrader:
    address: str
    name: str = ""
    win_rate_7d: float = 0.0
    pnl_7d: float = 0.0
    volume_7d: float = 0.0
    category: str = "Mixed"
    risk_tier: str = "low"
    style: str = "balanced"
    enabled: bool = True
    copy_amount_usd: float = 10.0  # Default fixed amount per trade to copy


@dataclass
class SizingConfig:
    mode: str = "fixed"  # "fixed" (USD amount) or "percentage" (proportional to balance/trader)
    fixed_amount_usd: float = 10.0
    balance_percentage: float = 5.0  # Max 5% of balance per trade
    mirror_percent_cap: float = 10.0  # Max percentage of master's trade size to copy


@dataclass
class RiskConfig:
    daily_budget_usd: float = 100.0  # Maximum total USD deployed per day
    max_per_market_usd: float = 50.0  # Maximum USD in a single market outcome
    min_trade_size_usd: float = 1.0  # Minimum trade size to mirror
    max_trade_size_usd: float = 25.0  # Hard cap per single trade
    slippage_tolerance_pct: float = 2.0  # Max allowed price slippage (e.g. 2%)
    min_price: float = 0.05  # Avoid extreme longshots (< 5c)
    max_price: float = 0.95  # Avoid extreme heavy favorites (> 95c)
    min_hours_to_resolution: float = 1.0  # Avoid markets expiring in minutes
    auto_exit_on_sell: bool = True  # Mirror sell orders from master traders


TRADES_LOG_FILE = os.path.join(os.path.dirname(__file__), "trades_log.jsonl")
PORTFOLIO_STATE_FILE = os.path.join(os.path.dirname(__file__), "portfolio_state.json")


@dataclass
class BotConfig:
    dry_run: bool = True  # True = Paper trade / Simulation mode; False = Live execution
    poll_interval_seconds: int = 5
    bullpen_path: str = "/home/nergal/.bullpen/bin/bullpen"
    trades_log_file: str = TRADES_LOG_FILE
    portfolio_state_file: str = PORTFOLIO_STATE_FILE
    sizing: SizingConfig = field(default_factory=SizingConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    traders: List[MasterTrader] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "BotConfig":
        sizing = SizingConfig(**data.get("sizing", {}))
        risk = RiskConfig(**data.get("risk", {}))
        traders = [MasterTrader(**t) for t in data.get("traders", [])]
        return cls(
            dry_run=data.get("dry_run", True),
            poll_interval_seconds=data.get("poll_interval_seconds", 5),
            bullpen_path=data.get("bullpen_path", "/home/nergal/.bullpen/bin/bullpen"),
            trades_log_file=data.get("trades_log_file", TRADES_LOG_FILE),
            portfolio_state_file=data.get("portfolio_state_file", PORTFOLIO_STATE_FILE),
            sizing=sizing,
            risk=risk,
            traders=traders,
        )


def load_dotenv(env_path: Optional[str] = None) -> None:
    """
    Lightweight .env loader that reads key=value pairs into os.environ.
    """
    if env_path is None:
        env_path = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(env_path):
        return
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = val.strip().strip("'\"")
                    if key and key not in os.environ:
                        os.environ[key] = val
    except Exception:
        pass


def load_config(path: str = CONFIG_FILE_PATH) -> BotConfig:
    load_dotenv()
    if os.path.exists(path):
        with open(path, "r") as f:
            data = json.load(f)
            cfg = BotConfig.from_dict(data)
    else:
        cfg = BotConfig()

    # Apply environment variable overrides if present
    if "DRY_RUN" in os.environ:
        cfg.dry_run = os.environ["DRY_RUN"].strip().lower() in ("true", "1", "yes")
    if "BULLPEN_PATH" in os.environ:
        cfg.bullpen_path = os.environ["BULLPEN_PATH"].strip()
    if "FIXED_AMOUNT_USD" in os.environ:
        try:
            cfg.sizing.fixed_amount_usd = float(os.environ["FIXED_AMOUNT_USD"])
        except ValueError:
            pass
    if "DAILY_BUDGET_USD" in os.environ:
        try:
            cfg.risk.daily_budget_usd = float(os.environ["DAILY_BUDGET_USD"])
        except ValueError:
            pass
    if "MAX_PER_MARKET_USD" in os.environ:
        try:
            cfg.risk.max_per_market_usd = float(os.environ["MAX_PER_MARKET_USD"])
        except ValueError:
            pass
    if "SLIPPAGE_TOLERANCE_PCT" in os.environ:
        try:
            cfg.risk.slippage_tolerance_pct = float(os.environ["SLIPPAGE_TOLERANCE_PCT"])
        except ValueError:
            pass
    if "POLL_INTERVAL_SECONDS" in os.environ:
        try:
            cfg.poll_interval_seconds = int(os.environ["POLL_INTERVAL_SECONDS"])
        except ValueError:
            pass

    return cfg


def save_config(config: BotConfig, path: str = CONFIG_FILE_PATH) -> None:
    with open(path, "w") as f:
        json.dump(config.to_dict(), f, indent=2)

