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
    category: str = "Sports"
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
    slippage_tolerance_pct: float = 3.0  # Max allowed price slippage (e.g. 3%)
    min_price: float = 0.08  # Avoid extreme longshots (< 8c)
    max_price: float = 0.92  # Avoid extreme heavy favorites (> 92c)
    min_hours_to_resolution: float = 0.5  # Avoid markets expiring in less than 30 mins
    auto_exit_on_sell: bool = True  # Mirror sell orders from master traders in paper mode
    auto_take_profit: bool = True  # Automatically sell profitable positions in paper mode
    take_profit_price: float = 0.90  # Target price (0.0 - 1.0) to auto-trigger exit with profit
    take_profit_min_gain_pct: float = 20.0  # Minimum gain % to qualify for profit exit (e.g. 20%)
    auto_stop_loss: bool = True  # Automatically close losing or resolved positions in paper mode
    stop_loss_price: float = 0.05  # Trigger price (<= 0.05 or 5c) to close dead/losing markets
    stop_loss_max_loss_pct: float = 85.0  # Trigger exit if loss exceeds this percentage (e.g. 85%)


@dataclass
class SportsConfig:
    enabled: bool = True
    categories: List[str] = field(default_factory=lambda: ["Soccer", "Basketball", "Tennis", "MMA", "Esports", "Sports"])
    min_volume_24h_usd: float = 300.0  # Minimum 24h volume for sports event scan
    min_liquidity_usd: float = 500.0   # Minimum liquidity to ensure viable trade
    scan_interval_seconds: int = 15    # Interval to scan for sports market value opportunities
    min_odds: float = 0.12            # Min probability (12 cents)
    max_odds: float = 0.88            # Max probability (88 cents)
    only_sports_signals: bool = False  # If True, only sports signals are emitted


@dataclass
class SignalConfig:
    sound_alert: bool = True           # Audio alert in browser dashboard
    telegram_enabled: bool = False     # Send alerts to Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    discord_webhook_url: str = ""
    show_direct_links: bool = True     # Include direct clickable Polymarket URLs


TRADES_LOG_FILE = os.path.join(os.path.dirname(__file__), "trades_log.jsonl")
SIGNALS_LOG_FILE = os.path.join(os.path.dirname(__file__), "signals_log.jsonl")
PORTFOLIO_STATE_FILE = os.path.join(os.path.dirname(__file__), "portfolio_state.json")


@dataclass
class BotConfig:
    dry_run: bool = True  # True = Paper trade (fake money); False = Live execution / manual real
    poll_interval_seconds: int = 5
    bullpen_path: str = "/home/nergal/.bullpen/bin/bullpen"
    trades_log_file: str = TRADES_LOG_FILE
    signals_log_file: str = SIGNALS_LOG_FILE
    portfolio_state_file: str = PORTFOLIO_STATE_FILE
    paper_initial_cash_usd: float = 1000.0  # Initial fake/simulated cash balance
    live_initial_cash_usd: float = 0.0     # Initial live/real cash balance
    user_wallet_address: str = ""          # Optional public wallet address to track real holdings read-only
    sizing: SizingConfig = field(default_factory=SizingConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    sports: SportsConfig = field(default_factory=SportsConfig)
    signals: SignalConfig = field(default_factory=SignalConfig)
    traders: List[MasterTrader] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "BotConfig":
        sizing = SizingConfig(**data.get("sizing", {}))
        risk = RiskConfig(**data.get("risk", {}))
        sports = SportsConfig(**data.get("sports", {}))
        signals = SignalConfig(**data.get("signals", {}))
        traders = [MasterTrader(**t) for t in data.get("traders", [])]
        return cls(
            dry_run=data.get("dry_run", True),
            poll_interval_seconds=data.get("poll_interval_seconds", 5),
            bullpen_path=data.get("bullpen_path", "/home/nergal/.bullpen/bin/bullpen"),
            trades_log_file=data.get("trades_log_file", TRADES_LOG_FILE),
            signals_log_file=data.get("signals_log_file", SIGNALS_LOG_FILE),
            portfolio_state_file=data.get("portfolio_state_file", PORTFOLIO_STATE_FILE),
            paper_initial_cash_usd=float(data.get("paper_initial_cash_usd", 1000.0)),
            live_initial_cash_usd=float(data.get("live_initial_cash_usd", 0.0)),
            user_wallet_address=data.get("user_wallet_address", ""),
            sizing=sizing,
            risk=risk,
            sports=sports,
            signals=signals,
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
                    if key:
                        if val:
                            os.environ[key] = val
                            # Automatically mirror proxy variables to lowercase for cross-tool compatibility
                            if key.upper() in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
                                os.environ[key.lower()] = val
                                os.environ[key.upper()] = val
                        else:
                            os.environ.pop(key, None)
                            if key.upper() in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
                                os.environ.pop(key.lower(), None)
                                os.environ.pop(key.upper(), None)
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
    if "PAPER_INITIAL_CASH_USD" in os.environ:
        try:
            cfg.paper_initial_cash_usd = float(os.environ["PAPER_INITIAL_CASH_USD"])
        except ValueError:
            pass
    if "LIVE_INITIAL_CASH_USD" in os.environ:
        try:
            cfg.live_initial_cash_usd = float(os.environ["LIVE_INITIAL_CASH_USD"])
        except ValueError:
            pass
    if "BULLPEN_PATH" in os.environ:
        cfg.bullpen_path = os.environ["BULLPEN_PATH"].strip()
    if "FIXED_AMOUNT_USD" in os.environ:
        try:
            val = float(os.environ["FIXED_AMOUNT_USD"])
            cfg.sizing.fixed_amount_usd = val
            for t in cfg.traders:
                t.copy_amount_usd = val
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
    if "AUTO_TAKE_PROFIT" in os.environ:
        cfg.risk.auto_take_profit = os.environ["AUTO_TAKE_PROFIT"].strip().lower() in ("true", "1", "yes")
    if "TAKE_PROFIT_PRICE" in os.environ:
        try:
            cfg.risk.take_profit_price = float(os.environ["TAKE_PROFIT_PRICE"])
        except ValueError:
            pass
    if "TAKE_PROFIT_MIN_GAIN_PCT" in os.environ:
        try:
            cfg.risk.take_profit_min_gain_pct = float(os.environ["TAKE_PROFIT_MIN_GAIN_PCT"])
        except ValueError:
            pass
    if "AUTO_STOP_LOSS" in os.environ:
        cfg.risk.auto_stop_loss = os.environ["AUTO_STOP_LOSS"].strip().lower() in ("true", "1", "yes")
    if "STOP_LOSS_PRICE" in os.environ:
        try:
            cfg.risk.stop_loss_price = float(os.environ["STOP_LOSS_PRICE"])
        except ValueError:
            pass
    if "STOP_LOSS_MAX_LOSS_PCT" in os.environ:
        try:
            cfg.risk.stop_loss_max_loss_pct = float(os.environ["STOP_LOSS_MAX_LOSS_PCT"])
        except ValueError:
            pass
    if "USER_WALLET_ADDRESS" in os.environ:
        cfg.user_wallet_address = os.environ["USER_WALLET_ADDRESS"].strip()
    if "TELEGRAM_BOT_TOKEN" in os.environ:
        cfg.signals.telegram_bot_token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
        cfg.signals.telegram_enabled = bool(cfg.signals.telegram_bot_token)
    if "TELEGRAM_CHAT_ID" in os.environ:
        cfg.signals.telegram_chat_id = os.environ["TELEGRAM_CHAT_ID"].strip()
    if "DISCORD_WEBHOOK_URL" in os.environ:
        cfg.signals.discord_webhook_url = os.environ["DISCORD_WEBHOOK_URL"].strip()
    if "SPORTS_MIN_VOLUME_USD" in os.environ:
        try:
            cfg.sports.min_volume_24h_usd = float(os.environ["SPORTS_MIN_VOLUME_USD"])
        except ValueError:
            pass
    if "SPORTS_MIN_LIQUIDITY_USD" in os.environ:
        try:
            cfg.sports.min_liquidity_usd = float(os.environ["SPORTS_MIN_LIQUIDITY_USD"])
        except ValueError:
            pass

    return cfg


def save_config(config: BotConfig, path: str = CONFIG_FILE_PATH) -> None:
    with open(path, "w") as f:
        json.dump(config.to_dict(), f, indent=2)

