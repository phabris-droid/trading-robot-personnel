"""Veille marché (CDC §5, §8, décision A9).

Sources : calendrier économique (flux JSON public hebdomadaire de Forex Factory, non officiel)
et flux RSS (CoinDesk, Cointelegraph, Fed, BCE, FXStreet). Chaque lecture est plafonnée à
2 Mo et 20 s (CDC §8) ; une source en panne garde ses dernières données et n'arrête rien.

Bloque une entrée si :
- une annonce majeure du calendrier tombe dans les 30 min avant ou après, sur une devise
  du marché (crypto : USD) ;
- une actualité à fort impact a moins de 2 h (flux crypto -> marchés crypto ; flux macro ->
  tous les marchés) ;
- la dernière bougie 1 h dépasse 3 ATR (choc de prix).
Impact d'une actualité : mots-clés de config.yaml ; l'IA peut seulement RELEVER un impact,
jamais l'abaisser, et ne peut jamais lever un blocage d'actualité.
"""
from __future__ import annotations

import calendar as _cal
import io
import ipaddress
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime

import feedparser
import httpx

from tradebot.analysts.base import Advisor, AdvisorContext, Verdict
from tradebot.strategies.indicators import atr

MAX_ITEMS_PER_FEED = 50


@dataclass(frozen=True)
class NewsItem:
    title: str
    published_ts: float
    source: str
    category: str          # crypto | macro
    impact: str = "normal"  # normal | high


@dataclass(frozen=True)
class CalendarEvent:
    title: str
    currency: str
    ts: float
    impact: str            # High | Medium | Low | Holiday


def currencies_of(symbol: str, crypto: tuple[str, ...] | list[str] = ("USD",)) -> set[str]:
    """Devises dont les annonces touchent le marché. Cryptos et or : `crypto` (config A9,
    crypto_announcement_currencies) ; paire de devises : ses deux devises."""
    if symbol.endswith(("USDT", "USDC", "BUSD")) or symbol in ("GOLD", "XAUUSD"):
        return {c.upper() for c in crypto}
    if len(symbol) == 6 and symbol.isalpha():
        return {symbol[:3], symbol[3:]}
    return set()


def is_crypto(symbol: str) -> bool:
    return symbol.endswith(("USDT", "USDC", "BUSD"))


def feed_category(url: str) -> str:
    return "crypto" if ("coin" in url.lower() or "crypto" in url.lower()) else "macro"


def fetch_limited(client: httpx.Client, url: str, max_bytes: int, timeout_s: float) -> bytes:
    """Téléchargement plafonné en taille ET en durée totale (un flux piégé ne fige rien)."""
    deadline = time.monotonic() + timeout_s
    # S4 : redirections suivies à la main (3 au plus, HTTPS vers un nom public seulement :
    # jamais vers le tableau de bord local ni une adresse interne) ; contenu non compressé
    for _ in range(MAX_REDIRECTS + 1):
        _check_public_https(url)
        chunks, size = [], 0
        left = max(deadline - time.monotonic(), 0.1)
        with client.stream("GET", url, timeout=left, follow_redirects=False,
                           headers={"Accept-Encoding": "identity"}) as resp:
            if resp.is_redirect:
                url = str(resp.url.join(resp.headers.get("location", "")))
                continue
            resp.raise_for_status()
            # audit n° 9 : « identity » n'est qu'une demande ; un corps compressé quand même
            # est refusé (jamais décompressé : pas de bombe gzip avant le contrôle de taille)
            encoding = resp.headers.get("content-encoding", "identity").strip().lower()
            if encoding not in ("", "identity"):
                raise ValueError(f"flux {url} compressé ({encoding}) refusé")
            for chunk in resp.iter_raw():
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError(f"flux {url} au-delà de {max_bytes} octets")
                if time.monotonic() > deadline:
                    raise TimeoutError(f"flux {url} au-delà de {timeout_s} s")
                chunks.append(chunk)
        return b"".join(chunks)
    raise ValueError(f"flux {url} : plus de {MAX_REDIRECTS} redirections")


MAX_REDIRECTS = 3
STALE_REFRESHES = 3              # S3 : calendrier périmé après 3 rafraîchissements manqués


def _check_public_https(url: str) -> None:
    parts = httpx.URL(url)
    host = (parts.host or "").lower()
    if parts.scheme != "https" or not host:
        raise ValueError(f"redirection refusée (non HTTPS) : {url}")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    # audit n° 8 : noms trompeurs aussi refusés (point final, nom sans point = nom interne ou
    # Docker, nombre seul = adresse IP écrite en décimal)
    internal_name = (host.endswith(".") or "." not in host or host.replace(".", "").isdigit()
                     or host == "localhost" or host.endswith((".local", ".internal")))
    if internal_name or (ip is not None and not ip.is_global):
        raise ValueError(f"redirection refusée (adresse interne) : {url}")


def classify(title: str, keywords: list[str]) -> str:
    low = title.lower()
    return "high" if any(k.lower() in low for k in keywords) else "normal"


def parse_feed(raw: bytes, url: str, keywords: list[str]) -> list[NewsItem]:
    # sécurité : un objet fichier, jamais des octets bruts (feedparser essaie d'ouvrir une
    # chaîne ou des octets comme un CHEMIN local : un corps « /dev/zero » figerait le robot)
    parsed = feedparser.parse(io.BytesIO(raw))
    out = []
    for entry in parsed.entries[:MAX_ITEMS_PER_FEED]:
        title = str(entry.get("title", "")).strip()
        stamp = entry.get("published_parsed") or entry.get("updated_parsed")
        if not title or not stamp:
            continue
        out.append(NewsItem(title=title[:300], published_ts=float(_cal.timegm(stamp)),
                            source=url, category=feed_category(url),
                            impact=classify(title, keywords)))
    return out


def parse_calendar(raw: bytes) -> list[CalendarEvent]:
    out = []
    for ev in json.loads(raw):
        try:
            ts = datetime.fromisoformat(str(ev["date"])).timestamp()
        except (KeyError, ValueError):
            continue
        out.append(CalendarEvent(title=str(ev.get("title", ""))[:200],
                                 currency=str(ev.get("country", "")).upper(),
                                 ts=ts, impact=str(ev.get("impact", ""))))
    return out


class NewsCache:
    """Flux et calendrier relus au plus toutes les N minutes ; dernières données gardées
    en cas de panne d'une source."""

    def __init__(self, cfg, client: httpx.Client | None = None,
                 clock: Callable[[], float] = time.time, ai=None, geo=None):
        self.cfg = cfg
        self.geo_cfg = geo                 # veille géopolitique (GeoCfg) ou None
        self.geo: dict[str, list[NewsItem]] = {}
        self.geo_ok_ts = clock()
        self._http = client or httpx.Client()
        self._clock = clock
        self.ai = ai                       # ClaudeAdvisor : relève les impacts (budget veille)
        self.news: dict[str, list[NewsItem]] = {}
        self.events: list[CalendarEvent] = []
        self.errors: dict[str, str] = {}
        self._last_refresh = float("-inf")
        self._worker: threading.Thread | None = None
        # S3 : dernière lecture réussie du calendrier ; trop ancienne (source en panne, fil de
        # veille figé) -> la veille lève une erreur : bloquante en réel, signalée en démo
        self.calendar_ok_ts = clock()

    def refresh(self, force: bool = False, background: bool = False) -> None:
        """Relit les sources si les données ont plus de N minutes. En arrière-plan, le moteur
        n'attend jamais la veille (une source lente ne retarde ni le cycle ni l'arrêt)."""
        now = self._clock()
        if not force and now - self._last_refresh < self.cfg.refresh_minutes * 60:
            return
        if self._worker is not None and self._worker.is_alive():
            return
        self._last_refresh = now
        if background:
            self._worker = threading.Thread(target=self._fetch_all, name="veille", daemon=True)
            self._worker.start()
        else:
            self._fetch_all()

    def _fetch_all(self) -> None:
        """Prépare de nouvelles copies puis les remplace d'un coup : le moteur ne lit jamais
        des données à moitié mises à jour."""
        news = dict(self.news)
        errors = dict(self.errors)
        events = self.events
        for url in self.cfg.feeds:
            try:
                raw = fetch_limited(self._http, url, self.cfg.feed_max_bytes,
                                    self.cfg.feed_timeout_s)
                news[url] = parse_feed(raw, url, self.cfg.high_impact_keywords)
                errors.pop(url, None)
            except Exception as exc:  # noqa: BLE001
                errors[url] = f"{type(exc).__name__}: {exc}"
        try:
            raw = fetch_limited(self._http, self.cfg.calendar_url, self.cfg.feed_max_bytes,
                                self.cfg.feed_timeout_s)
            events = parse_calendar(raw)
            errors.pop("calendrier", None)
            self.calendar_ok_ts = self._clock()
        except Exception as exc:  # noqa: BLE001
            errors["calendrier"] = f"{type(exc).__name__}: {exc}"
        geo = dict(self.geo)
        if self.geo_cfg is not None and self.geo_cfg.enabled:
            feeds = list(self.geo_cfg.official_feeds) + list(self.geo_cfg.media_feeds)
            ok = False
            for url in feeds:
                try:
                    raw = fetch_limited(self._http, url, self.cfg.feed_max_bytes,
                                        self.cfg.feed_timeout_s)
                    geo[url] = parse_feed(raw, url, list(self.geo_cfg.keywords))
                    errors.pop(url, None)
                    ok = True
                except Exception as exc:  # noqa: BLE001
                    errors[url] = f"{type(exc).__name__}: {exc}"
            if ok or not feeds:
                self.geo_ok_ts = self._clock()
        self.geo = geo
        if self.ai is not None:
            news = self._raise_impacts(news)
        self.news, self.events, self.errors = news, events, errors

    def geo_stale_for(self, now: float) -> float:
        """Veille géopolitique : secondes au-delà de 3 rafraîchissements sans aucune source."""
        return max(0.0, now - self.geo_ok_ts - STALE_REFRESHES * self.cfg.refresh_minutes * 60)

    def stale_for(self, now: float) -> float:
        """Secondes au-delà du délai toléré (3 rafraîchissements) ; 0 si à jour."""
        return max(0.0, now - self.calendar_ok_ts - STALE_REFRESHES * self.cfg.refresh_minutes * 60)

    def _raise_impacts(self, news: dict[str, list[NewsItem]]) -> dict[str, list[NewsItem]]:
        """L'IA ne peut que RELEVER un impact (normal -> high), jamais l'abaisser."""
        news = {url: list(items) for url, items in news.items()}
        normal = [(url, i, item) for url, items in news.items()
                  for i, item in enumerate(items) if item.impact == "normal"]
        if not normal:
            return news
        levels = self.ai.rate_headlines([item.title for _, _, item in normal])
        for (url, i, item), level in zip(normal, levels, strict=False):
            if level == "high":
                news[url][i] = replace(item, impact="high")
        return news

    def all_news(self) -> list[NewsItem]:
        return [item for items in list(self.news.values()) for item in items]


class NewsWatch(Advisor):
    name = "veille"

    def __init__(self, cfg, cache: NewsCache | None = None, atr_period: int = 14):
        self.cfg = cfg
        self.cache = cache
        self.atr_period = atr_period

    def review(self, ctx: AdvisorContext) -> Verdict:
        if self.cache is not None and self.cache.stale_for(ctx.now) > 0:
            raise RuntimeError("calendrier économique non relu depuis trop longtemps "
                               "(veille figée ou source en panne)")
        if self.cache is not None:
            v = self._calendar(ctx) or self._news(ctx)
            if v:
                return v
        return self._shock(ctx) or Verdict(self.name, False)

    def _calendar(self, ctx: AdvisorContext) -> Verdict | None:
        if self.cache is None:
            return None
        window = self.cfg.event_window_min * 60
        currencies = currencies_of(ctx.symbol, self.cfg.crypto_announcement_currencies)
        for ev in self.cache.events:
            if (ev.impact in self.cfg.announcement_impacts and ev.currency in currencies
                    and abs(ev.ts - ctx.now) <= window):
                when = datetime.fromtimestamp(ev.ts, UTC).strftime("%H:%M")
                return Verdict(self.name, True, f"annonce majeure {ev.currency} « {ev.title} » "
                                                f"à {when} UTC (±{self.cfg.event_window_min} min)")
        return None

    def _news(self, ctx: AdvisorContext) -> Verdict | None:
        if self.cache is None:
            return None
        horizon = self.cfg.high_impact_news_hours * 3600
        for item in self.cache.all_news():
            relevant = item.category == "macro" or is_crypto(ctx.symbol)
            if (item.impact == "high" and relevant
                    and 0 <= ctx.now - item.published_ts <= horizon):
                return Verdict(self.name, True, f"actualité à fort impact de moins de "
                                                f"{self.cfg.high_impact_news_hours:g} h : "
                                                f"« {item.title[:120]} »")
        return None

    def _shock(self, ctx: AdvisorContext) -> Verdict | None:
        df = ctx.candles
        if len(df) < self.atr_period + 2:
            return None
        a = atr(df.iloc[:-1], self.atr_period).iloc[-1]      # ATR avant la dernière bougie
        rng = float(df["high"].iloc[-1] - df["low"].iloc[-1])
        ctx.analyses["bougie_en_atr"] = round(rng / a, 2) if a > 0 else None
        if a > 0 and rng > self.cfg.shock_candle_atr * a:
            return Verdict(self.name, True, f"choc de prix : dernière bougie de {rng / a:.1f} ATR "
                                            f"(> {self.cfg.shock_candle_atr:g})")
        return None
