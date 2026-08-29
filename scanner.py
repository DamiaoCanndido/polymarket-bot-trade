import os
import sys
import time
import json
import subprocess
import logging
import urllib.request
from typing import List, Dict, Any, Optional
from config import MasterTrader, SportsConfig

logger = logging.getLogger("PolymarketScanner")

SPORT_TAG_MAP = {
    "soccer": ("Soccer", "⚽ Futebol"),
    "football": ("Soccer", "⚽ Futebol"),
    "premier-league": ("Soccer", "⚽ Premier League"),
    "la-liga": ("Soccer", "⚽ La Liga"),
    "champions-league": ("Soccer", "⚽ Champions League"),
    "basketball": ("Basketball", "🏀 Basquete / NBA"),
    "nba": ("Basketball", "🏀 NBA"),
    "wnba": ("Basketball", "🏀 WNBA"),
    "tennis": ("Tennis", "🎾 Tênis"),
    "atp": ("Tennis", "🎾 Tênis ATP"),
    "wta": ("Tennis", "🎾 Tênis WTA"),
    "mma": ("MMA", "🥊 MMA / UFC"),
    "ufc": ("MMA", "🥊 UFC"),
    "boxing": ("MMA", "🥊 Boxe"),
    "esports": ("Esports", "🎮 Esports"),
    "league-of-legends": ("Esports", "🎮 League of Legends"),
    "counter-strike": ("Esports", "🎮 CS2"),
    "baseball": ("Baseball", "⚾ Beisebol / MLB"),
    "mlb": ("Baseball", "⚾ MLB"),
    "formula-1": ("Motorsport", "🏎️ F1"),
    "sports": ("Sports", "🏆 Esportes")
}


class SportsMarketScanner:
    """
    Scans Polymarket Gamma API for active sports events, matches, and odds.
    Generates actionable signals with direct URLs and value metrics.
    """
    def __init__(self, config: Optional[SportsConfig] = None):
        self.config = config or SportsConfig()
        self._last_scan_time: float = 0.0
        self._cached_opportunities: List[Dict[str, Any]] = []

    def _fetch_events_for_tag(self, tag_slug: str, limit: int = 15) -> List[Dict[str, Any]]:
        url = f"https://gamma-api.polymarket.com/events?closed=false&limit={limit}&tag_slug={tag_slug}&order=volume24hr&ascending=false"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read().decode())
                if isinstance(data, list):
                    return data
        except Exception as e:
            logger.debug(f"Failed fetching events for tag '{tag_slug}': {e}")
        return []

    def scan_sports_opportunities(self, limit_per_sport: int = 10) -> List[Dict[str, Any]]:
        """
        Scans all configured sport tags and analyzes markets for betting value / signals.
        """
        now = time.time()
        # Throttled cache: only re-fetch every 10 seconds max
        if (now - self._last_scan_time) < 10.0 and self._cached_opportunities:
            return self._cached_opportunities

        opportunities: List[Dict[str, Any]] = []
        seen_events: set = set()

        tags_to_scan = [
            "soccer", "basketball", "nba", "tennis", "mma", "ufc",
            "premier-league", "la-liga", "champions-league", "esports", "sports"
        ]

        for tag in tags_to_scan:
            events = self._fetch_events_for_tag(tag, limit=limit_per_sport)
            category_info = SPORT_TAG_MAP.get(tag, ("Sports", "🏆 Esportes"))

            for ev in events:
                event_id = ev.get("id") or ev.get("slug")
                if not event_id or event_id in seen_events:
                    continue

                seen_events.add(event_id)
                event_slug = ev.get("slug", "")
                event_title = ev.get("title", "")
                volume_24h = float(ev.get("volume24hr") or ev.get("volume") or 0.0)
                liquidity = float(ev.get("liquidity") or 0.0)
                event_url = f"https://polymarket.com/event/{event_slug}" if event_slug else ""
                icon_url = ev.get("icon") or ev.get("image") or ""

                # Filter by volume / liquidity threshold
                if volume_24h < self.config.min_volume_24h_usd and liquidity < self.config.min_liquidity_usd:
                    continue

                markets = ev.get("markets", [])
                if not markets:
                    continue

                for m in markets:
                    market_slug = m.get("slug", event_slug)
                    market_question = m.get("question", event_title)
                    outcomes_raw = m.get("outcomes", [])
                    prices_raw = m.get("outcomePrices", [])

                    if isinstance(outcomes_raw, str):
                        try:
                            outcomes = json.loads(outcomes_raw)
                        except Exception:
                            outcomes = ["Yes", "No"]
                    else:
                        outcomes = outcomes_raw or ["Yes", "No"]

                    if isinstance(prices_raw, str):
                        try:
                            prices = [float(p) for p in json.loads(prices_raw)]
                        except Exception:
                            prices = []
                    else:
                        prices = [float(p) for p in (prices_raw or [])]

                    if len(outcomes) < 2 or len(prices) < 2:
                        continue

                    # Analyze outcome value
                    for outcome_name, price in zip(outcomes, prices):
                        if not (self.config.min_odds <= price <= self.config.max_odds):
                            continue

                        # Determine if this is a high-value signal
                        confidence = "Alta" if (0.35 <= price <= 0.65) else ("Moderada" if price > 0.65 else "Estratégica")
                        badge_color = "green" if price <= 0.50 else "blue"

                        opp = {
                            "id": f"sport_{event_slug}_{outcome_name}_{int(now)}",
                            "type": "SPORTS_MARKET_SIGNAL",
                            "sport_category": category_info[0],
                            "sport_label": category_info[1],
                            "event_title": event_title,
                            "event_slug": event_slug,
                            "market_slug": market_slug,
                            "market_question": market_question,
                            "outcome": outcome_name,
                            "price": round(price, 3),
                            "odds_pct": f"{price * 100:.1f}%",
                            "volume_24h_usd": round(volume_24h, 2),
                            "liquidity_usd": round(liquidity, 2),
                            "event_url": event_url,
                            "icon_url": icon_url,
                            "confidence": confidence,
                            "badge_color": badge_color,
                            "suggested_action": "BUY",
                            "suggested_amount_usd": 5.0,
                            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
                        }
                        opportunities.append(opp)

        # Sort by 24h volume descending
        opportunities.sort(key=lambda x: x["volume_24h_usd"], reverse=True)
        self._cached_opportunities = opportunities
        self._last_scan_time = now
        return opportunities


class LeaderboardScanner:
    def __init__(self, bullpen_path: str = "/home/nergal/.bullpen/bin/bullpen"):
        self.bullpen_path = bullpen_path

    def _fetch_direct_polymarket_leaderboard(self, period: str = "week", sort_by: str = "PNL", limit: int = 50) -> List[Dict[str, Any]]:
        """
        Direct Polymarket Data API fetch. Completely independent of local tools.
        """
        period_map = {"1d": "day", "day": "day", "7d": "week", "week": "week", "30d": "month", "month": "month", "all": "all"}
        p_clean = period_map.get(period, "week")
        url = f"https://data-api.polymarket.com/v1/leaderboard?timePeriod={p_clean}&orderBy={sort_by}&limit={limit}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
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
        min_win_rate: float = 0.60,
        min_pnl: float = 2000.0,
        min_volume: float = 2000.0,
        exclude_high_risk: bool = True,
        limit: int = 100
    ) -> List[MasterTrader]:
        """
        Fetches and curates top master traders from Polymarket data leaderboards.
        Prioritizes direct API without requiring external CLIs.
        """
        period_alias_map = {"1d": "day", "7d": "week", "30d": "month", "all": "all"}
        clean_period = period_alias_map.get(time_period, time_period)

        all_entries: Dict[str, Dict[str, Any]] = {}

        # 1. Fetch from direct Polymarket Data API
        for sort_by in ("PNL", "VOL"):
            direct_items = self._fetch_direct_polymarket_leaderboard(period=clean_period, sort_by=sort_by, limit=limit)
            for item in direct_items:
                addr = item.get("proxyWallet") or item.get("address")
                if addr and addr.lower() not in all_entries:
                    pnl = float(item.get("pnl") or 0.0)
                    vol = float(item.get("vol") or item.get("volume") or 0.0)
                    uname = item.get("userName") or item.get("name") or f"{addr[:6]}...{addr[-4:]}"
                    
                    # Tag sports traders
                    is_sports = any(k in uname.lower() for k in ("bet", "lal", "epl", "goal", "nba", "sport", "fc", "ufc"))
                    all_entries[addr.lower()] = {
                        "address": addr,
                        "name": uname,
                        "pnl": pnl,
                        "volume": vol,
                        "win_rate": 0.75 if pnl > 0 else 0.50,
                        "category": "Sports" if is_sports else "Mixed",
                        "risk_tier": "low",
                        "style": "balanced"
                    }

        curated: List[MasterTrader] = []
        for addr_lower, t in all_entries.items():
            wr = t.get("win_rate", 0.75)
            pnl = t.get("pnl", 0.0)
            vol = t.get("volume", 0.0)
            risk = t.get("risk_tier", "low")
            category = t.get("category", "Sports")

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
                        category=category,
                        risk_tier=risk,
                        style=t.get("style", "balanced"),
                        enabled=True,
                        copy_amount_usd=5.0
                    )
                )

        # Fallback if strict filter is empty
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
                            category=t.get("category", "Sports"),
                            risk_tier="low",
                            style="balanced",
                            enabled=True,
                            copy_amount_usd=5.0
                        )
                    )

        curated.sort(key=lambda x: x.pnl_7d, reverse=True)
        return curated


