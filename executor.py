"""
Copytrade & Signal Executor module for Polymarket.
Supports automated paper trading (simulation) and real-time signals with direct URLs for manual execution.
Uses direct Polymarket Data API / Gamma API to completely eliminate Cloudflare blocking.
"""
import os
import sys
import time
import json
import urllib.request
import urllib.error
import subprocess
import logging
from typing import Dict, Any, List, Optional
from config import BotConfig, MasterTrader, SignalConfig
from risk_manager import RiskManager

logger = logging.getLogger("PolymarketExecutor")


class SignalDispatcher:
    """
    Formats and broadcasts sports trading signals to Dashboard, Telegram, Discord, and JSONL log.
    """
    def __init__(self, config: BotConfig):
        self.config = config
        self.signals_log_file = config.signals_log_file

    def format_signal_text(self, signal_data: Dict[str, Any]) -> str:
        """
        Creates a clean, human-readable signal notification text.
        """
        sport_label = signal_data.get("sport_label", "🏆 Esportes")
        event_title = signal_data.get("event_title") or signal_data.get("market_title") or "Evento Esportivo"
        outcome = signal_data.get("outcome", "Yes")
        price = signal_data.get("price", 0.50)
        odds_pct = signal_data.get("odds_pct") or f"{price * 100:.1f}%"
        amount = signal_data.get("suggested_amount_usd", 5.0)
        url = signal_data.get("event_url") or f"https://polymarket.com/event/{signal_data.get('event_slug', '')}"
        source = signal_data.get("source", "Scanner de Oportunidades")

        msg = (
            f"🚨 SINAL ESPORTIVO DETECTADO!\n"
            f"{sport_label} | {event_title}\n"
            f"🎯 Sugestão: {outcome} ({odds_pct})\n"
            f"💵 Preço / Cota: ${price:.3f}\n"
            f"📊 Valor Sugerido: ${amount:.2f} USD\n"
            f"🔍 Fonte: {source}\n"
            f"🔗 Apostar no site: {url}"
        )
        return msg

    def send_telegram(self, text: str) -> bool:
        """Sends signal text to configured Telegram chat."""
        token = self.config.signals.telegram_bot_token
        chat_id = self.config.signals.telegram_chat_id
        if not token or not chat_id:
            return False
        try:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            payload = json.dumps({"chat_id": chat_id, "text": text, "disable_web_page_preview": False}).encode("utf-8")
            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status == 200
        except Exception as e:
            logger.warning(f"Telegram notification error: {e}")
            return False

    def send_discord(self, signal_data: Dict[str, Any]) -> bool:
        """Sends rich embed to configured Discord Webhook."""
        webhook_url = self.config.signals.discord_webhook_url
        if not webhook_url:
            return False
        try:
            sport_label = signal_data.get("sport_label", "🏆 Esportes")
            event_title = signal_data.get("event_title", "Evento")
            outcome = signal_data.get("outcome", "Yes")
            price = signal_data.get("price", 0.50)
            url = signal_data.get("event_url", "https://polymarket.com")

            payload = {
                "content": f"🚨 **Novo Sinal: {sport_label}**",
                "embeds": [{
                    "title": event_title,
                    "url": url,
                    "color": 3447003,
                    "fields": [
                        {"name": "🎯 Seleção", "value": outcome, "inline": True},
                        {"name": "💵 Preço / Odd", "value": f"${price:.3f} ({price*100:.1f}%)", "inline": True},
                        {"name": "📊 Valor Sugerido", "value": f"${signal_data.get('suggested_amount_usd', 5.0):.2f}", "inline": True},
                    ],
                    "footer": {"text": "Polymarket Sports Signal Bot"}
                }]
            }
            req = urllib.request.Request(
                webhook_url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status in (200, 204)
        except Exception as e:
            logger.warning(f"Discord notification error: {e}")
            return False

    def dispatch_signal(self, signal_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Dispatches signal to log file, Telegram, and Discord.
        """
        text = self.format_signal_text(signal_data)
        logger.info(f"\n{text}\n")

        # 1. Log to signals_log.jsonl
        try:
            with open(self.signals_log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(signal_data) + "\n")
        except Exception as e:
            logger.error(f"Failed writing signal to log: {e}")

        # 2. Telegram
        if self.config.signals.telegram_enabled:
            self.send_telegram(text)

        # 3. Discord
        if self.config.signals.discord_webhook_url:
            self.send_discord(signal_data)

        return {"success": True, "signal": signal_data, "formatted_text": text}


class CopyExecutor:
    def __init__(self, config: BotConfig, risk_manager: RiskManager):
        self.config = config
        self.risk_manager = risk_manager
        self.bullpen_path = config.bullpen_path
        self.dispatcher = SignalDispatcher(config)
        self._last_reset_time: float = 0.0

    def _run_cli(self, args: List[str]) -> Dict[str, Any]:
        """Optional CLI fallback for users with Bullpen installed."""
        if not os.path.exists(self.bullpen_path) and not subprocess.run(["which", "bullpen"], stdout=subprocess.PIPE, stderr=subprocess.PIPE).returncode == 0:
            return {"success": False, "error": "Bullpen CLI não configurado (Modo Sinal / API direta ativo)"}

        cmd = [self.bullpen_path] + args + ["--output", "json"]
        env = os.environ.copy()
        for p in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
            if p in env and not str(env[p]).strip():
                env.pop(p, None)
        try:
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, check=False)
            stdout_clean = (result.stdout or "").strip()
            if stdout_clean:
                try:
                    loaded = json.loads(stdout_clean)
                    if isinstance(loaded, dict):
                        return loaded
                    elif isinstance(loaded, list):
                        return {"data": loaded, "success": True}
                except Exception:
                    pass
            return {"success": result.returncode == 0, "raw": stdout_clean}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    def fetch_master_feed(self, address: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
        """
        Fetches the recent trade activity for a master trader address directly from Polymarket Data API.
        Does NOT rely on local CLIs or suffer from Cloudflare blocking.
        """
        if not address:
            return []

        url = f"https://data-api.polymarket.com/activity?user={address}&limit={limit}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read().decode())
                if isinstance(data, list):
                    normalized = []
                    for item in data:
                        trade_id = item.get("transactionHash") or f"{address}_{item.get('timestamp')}_{item.get('slug')}"
                        event_slug = item.get("eventSlug") or item.get("slug") or ""
                        event_url = f"https://polymarket.com/event/{event_slug}" if event_slug else ""

                        normalized.append({
                            "id": trade_id,
                            "trade_id": trade_id,
                            "transaction_hash": item.get("transactionHash"),
                            "maker": item.get("proxyWallet") or address,
                            "slug": item.get("slug"),
                            "event_slug": event_slug,
                            "market_slug": item.get("slug"),
                            "market_url": event_url,
                            "title": item.get("title") or item.get("slug", ""),
                            "market_title": item.get("title") or item.get("slug", ""),
                            "outcome": item.get("outcome") or ("Yes" if item.get("outcomeIndex") == 0 else "No"),
                            "side": (item.get("side") or "BUY").upper(),
                            "price": float(item.get("price") or 0.50),
                            "size_usd": float(item.get("usdcSize") or (float(item.get("size") or 0.0) * float(item.get("price") or 0.50))),
                            "shares": float(item.get("size") or 0.0),
                            "timestamp": item.get("timestamp") or time.time()
                        })
                    return normalized
        except Exception as e:
            logger.debug(f"Direct Polymarket activity fetch failed for {address}: {e}")

        # Fallback to CLI if user configured it
        cli_res = self._run_cli(["tracker", "feed", "--address", address, "--limit", str(limit)])
        if cli_res.get("success"):
            data = cli_res.get("trades") or cli_res.get("data") or []
            if isinstance(data, list):
                return data

        return []

    def get_market_prices(self, market_slug: str) -> Dict[str, float]:
        """
        Retrieves live outcome prices for a given market slug via Gamma API.
        Returns a dict mapping outcome name -> price float (e.g. {"Yes": 0.55, "No": 0.45}).
        """
        if not market_slug:
            return {}

        prices: Dict[str, float] = {}

        # 1. Primary: Direct Gamma API
        try:
            url = f"https://gamma-api.polymarket.com/events?slug={market_slug}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            with urllib.request.urlopen(req, timeout=5) as resp:
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
            if prices:
                return prices
        except Exception as e:
            logger.debug(f"Gamma API price fetch failed for {market_slug}: {e}")

        return prices

    def get_wallet_balance(self) -> Dict[str, Any]:
        """
        Queries Polymarket balance for the configured user wallet address read-only,
        or falls back to Bullpen CLI if present.
        """
        addr = self.config.user_wallet_address.strip()
        if addr:
            try:
                url = f"https://data-api.polymarket.com/positions?user={addr}"
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    pos_data = json.loads(resp.read().decode())
                    if isinstance(pos_data, list):
                        total_pos_val = sum(float(p.get("curPrice", 0.0) or 0.0) * float(p.get("size", 0.0) or 0.0) for p in pos_data)
                        return {
                            "success": True,
                            "address": addr,
                            "balance_usd": round(self.config.live_initial_cash_usd, 2),
                            "positions_value_usd": round(total_pos_val, 2),
                            "open_positions_count": len(pos_data),
                            "mode": "read_only_live"
                        }
            except Exception as e:
                logger.debug(f"Direct wallet positions check failed: {e}")

        return {
            "success": True,
            "address": addr or "Manual (Navegador)",
            "balance_usd": self.config.live_initial_cash_usd,
            "positions_value_usd": 0.0,
            "open_positions_count": 0,
            "mode": "manual_browser"
        }

    def execute_buy(
        self,
        market_slug: str,
        outcome: str,
        amount_usd: float,
        max_price: Optional[float] = None,
        event_slug: Optional[str] = None,
        event_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        In Paper Trading, returns simulated success.
        In Live Trading, generates signal with direct link for manual execution.
        """
        target_slug = event_slug or market_slug
        url = event_url or (f"https://polymarket.com/event/{target_slug}" if target_slug else "")
        signal_data = {
            "type": "MANUAL_BUY_SIGNAL",
            "market_slug": market_slug,
            "event_slug": event_slug or market_slug,
            "outcome": outcome,
            "price": max_price or 0.50,
            "suggested_amount_usd": amount_usd,
            "event_url": url,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        }
        self.dispatcher.dispatch_signal(signal_data)
        return {"success": True, "mode": "signal", "signal": signal_data}

    def execute_sell(
        self,
        market_slug: str,
        outcome: str,
        shares: Optional[float] = None,
        sell_all: bool = False,
        min_price: Optional[float] = None,
        event_slug: Optional[str] = None,
        event_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        In Paper Trading, returns simulated success.
        In Live Trading, notifies user to close position in browser.
        """
        target_slug = event_slug or market_slug
        url = event_url or (f"https://polymarket.com/event/{target_slug}" if target_slug else "")
        signal_data = {
            "type": "MANUAL_SELL_SIGNAL",
            "market_slug": market_slug,
            "event_slug": event_slug or market_slug,
            "outcome": outcome,
            "shares": shares,
            "min_price": min_price,
            "event_url": url,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        }
        self.dispatcher.dispatch_signal(signal_data)
        return {"success": True, "mode": "signal", "signal": signal_data}
