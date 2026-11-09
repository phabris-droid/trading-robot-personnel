"""Binance spot (CDC §2, §6).

- REST signée HMAC-SHA256, horloge synchronisée sur GET /api/v3/time, recvWindow.
- Bougies et prix : TOUJOURS le vrai marché (market_data_url = api.binance.com).
- Ordres : demo-api.binance.com en démo, api.binance.com en réel.
- Achat uniquement. Après l'achat : OCO de vente (objectif LIMIT_MAKER + STOP_LOSS_LIMIT
  dont la limite est 0,5 % sous le stop). OCO impossible -> vente immédiate.
- Fermeture : annuler l'OCO puis vendre la quantité libre.
- Requêtes de lecture : nouvelles tentatives avec attente croissante sur 5xx / 429 / réseau.
  Ordres : AUCUNE nouvelle tentative ; timeout ou 5xx -> OrderUncertain (recherche par
  identifiant client, jamais de renvoi).
- Avoirs présents au démarrage (personnels) : mémorisés et ignorés.

À VÉRIFIER LE JOUR 1 DE LA DÉMO (non vérifiable hors ligne, CDC §9) :
  1. Le mode démo (demo-api.binance.com) accepte POST /api/v3/orderList/oco et exécute
     réellement le stop STOP_LOSS_LIMIT quand le prix le franchit.
  2. GET /api/v3/orderList?origClientOrderId=<listClientOrderId> retrouve bien l'OCO
     (sinon basculer sur GET /api/v3/openOrderList).
  3. Codes d'erreur « introuvable » : -2011 (annulation) et -2013 (consultation).
  4. Les filtres du symbole en démo (exchangeInfo) sont identiques à ceux du réel.
  5. Frais prélevés en actif de base (BTC) : la quantité nette reçue est bien
     executedQty - commission, et l'OCO sur cette quantité n'est pas refusé.
  6. La synchronisation d'horloge suffit avec recvWindow = 5000 ms depuis le VPS.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from urllib.parse import urlencode

import httpx
import pandas as pd

from tradebot.brokers.base import (
    ApiError,
    Broker,
    BrokerError,
    BrokerUnavailable,
    IpBanned,
    OrderRejected,
    OrderUncertain,
    PositionInconsistent,
    RateLimited,
    StopNotPlaced,
    StopStatus,
    parse_retry_after,   # D6 : défini dans base, commun aux plateformes
)

# --- Constantes de l'API (isolées pour être corrigées facilement au jour 1) ------------------
EP_TIME = "/api/v3/time"
EP_KLINES = "/api/v3/klines"
EP_EXCHANGE_INFO = "/api/v3/exchangeInfo"
EP_BOOK_TICKER = "/api/v3/ticker/bookTicker"
EP_ACCOUNT = "/api/v3/account"
EP_ORDER = "/api/v3/order"
# Adresses OCO — À VÉRIFIER J1 DÉMO (création, consultation, annulation, historique)
EP_OCO = "/api/v3/orderList/oco"              # création d'un OCO
EP_ORDER_LIST = "/api/v3/orderList"           # consultation (origClientOrderId), annulation
EP_ALL_ORDER_LIST = "/api/v3/allOrderList"    # historique des OCO (repli si introuvable)
ALL_ORDER_LIST_LIMIT = 100
EP_OPEN_ORDERS = "/api/v3/openOrders"
EP_MY_TRADES = "/api/v3/myTrades"
CODE_TIMESTAMP = -1021            # horodatage hors recvWindow : requête refusée, resynchroniser
CODES_NOT_FOUND = {-2011, -2013}  # ordre / liste introuvable
OCO_ACTIVE = {"EXECUTING"}        # listOrderStatus d'un OCO encore en place
ORDER_FINAL_STATUSES = {"REJECTED", "EXPIRED", "CANCELED", "EXPIRED_IN_MATCH"}
CLIENT_ID_MAX = 28                # Binance accepte 36 : suffixes « -oNNa » réservés
HOUR_MS = 3_600_000
NOT_FOUND_TO_GIVE_UP = 2         # recherches consécutives avant « jamais exécuté » (M7)


@dataclass(frozen=True)
class SymbolFilters:
    base_asset: str
    quote_asset: str
    step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal
    tick_size: Decimal
    min_notional: Decimal

    def floor_qty(self, qty: float) -> Decimal:
        return _floor(Decimal(str(qty)), self.step_size)


@dataclass
class BinancePosition:
    id: str
    symbol: str
    qty: float
    entry: float
    stop: float
    target: float
    client_id: str
    oco_id: str = ""
    oco_seq: int = 0
    exit_seq: int = 0
    status: str = "open"   # open | unprotected | inconsistent | closed
    exit_price: float = 0.0   # A6 : prix moyen réellement exécuté à la sortie (0 = inconnu)
    exit_uncertain: str = ""  # C2 : identifiant d'une vente de sortie au statut incertain
    exit_not_found: int = 0   # C2 : recherches infructueuses de cette vente
    # T1 : quantité de sortie déjà comptée par ordre (orderId -> executedQty) : une même
    # exécution relue au cycle suivant n'est JAMAIS soustraite deux fois
    exits_applied: dict = field(default_factory=dict)


BAN_DEFAULT_S = 300.0             # 418 sans durée indiquée : 5 min sans aucune requête
BAN_MAX_S = 3 * 24 * 3600.0


def _dec(value) -> Decimal | None:
    """Nombre décimal lu dans une réponse ; None si absent, nul ou illisible."""
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None
    return d if d.is_finite() else None


def _avg_price(order: dict | None) -> float:
    """A6 : prix moyen d'un ordre exécuté (cummulativeQuoteQty / executedQty), 0 si illisible."""
    try:
        qty = float((order or {}).get("executedQty") or 0)
        quote = float((order or {}).get("cummulativeQuoteQty") or 0)
    except (TypeError, ValueError):
        return 0.0
    price = quote / qty if qty > 0 else 0.0
    return price if math.isfinite(price) and price > 0 else 0.0


def _floor(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _ceil(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def _fmt(value: Decimal | float) -> str:
    """Nombre sans notation scientifique, comme l'attend l'API."""
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    text = format(d.normalize(), "f")
    return text if text not in ("-0", "") else "0"


class BinanceSpot(Broker):
    name = "binance"

    def __init__(self, api_key: str, api_secret: str, trading_url: str, market_data_url: str,
                 stop_limit_offset_pct: float = 0.5, *, client: httpx.Client | None = None,
                 recv_window: int = 5000, max_retries: int = 3,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time, baseline: dict | None = None,
                 symbols: list[str] | None = None):
        if not api_key or not api_secret:
            raise BrokerError("clés API Binance absentes")
        self.symbols: list[str] = list(symbols or [])   # marchés configurés (capital)
        self._key = api_key
        self._secret = api_secret.encode()
        self.trading_url = trading_url.rstrip("/")
        self.market_data_url = market_data_url.rstrip("/")
        self.stop_limit_offset = Decimal(str(stop_limit_offset_pct)) / 100
        self._http = client or httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0))
        self.recv_window = recv_window
        self.max_retries = max_retries
        self._sleep = sleep
        self._clock = clock
        self._offset_ms = 0
        self.banned_until = 0.0        # horodatage de fin de bannissement (HTTP 418)
        self._filters: dict[str, SymbolFilters] = {}
        self._positions: dict[str, BinancePosition] = {}
        self._not_found: dict[str, int] = {}     # ordres incertains introuvables (M7)
        self.baseline: dict[str, float] = dict(baseline or {})

    def __repr__(self) -> str:   # jamais de secret dans les journaux
        return f"BinanceSpot(trading_url={self.trading_url!r})"

    @classmethod
    def from_settings(cls, settings, env: dict, **kw) -> BinanceSpot:
        cfg = settings.platforms.binance
        url = cfg.trading_url_live if settings.mode == "live" else cfg.trading_url_demo
        symbols = [m.symbol for m in settings.markets if m.platform == "binance" and m.enabled]
        return cls(env.get("BINANCE_API_KEY", ""), env.get("BINANCE_API_SECRET", ""), url,
                   cfg.market_data_url, cfg.stop_limit_offset_pct, symbols=symbols, **kw)

    # --- transport --------------------------------------------------------------------------

    def _now_ms(self) -> int:
        return int(self._clock() * 1000) + self._offset_ms

    def sync_time(self) -> None:
        before = self._clock()
        server = self._request("GET", self.trading_url, EP_TIME)["serverTime"]
        after = self._clock()
        self._offset_ms = int(server - (before + after) / 2 * 1000)

    def sign(self, query: str) -> str:
        return hmac.new(self._secret, query.encode(), hashlib.sha256).hexdigest()

    def _request(self, method: str, base: str, path: str, params: dict | None = None, *,
                 signed: bool = False, kind: str = "read", client_id: str = ""):
        """kind = "read" | "cancel" (nouvelles tentatives) ou "order" (jamais renvoyé)."""
        attempt, resynced = 0, False
        while True:
            attempt += 1
            if self._clock() < self.banned_until:   # 418 : plus aucune requête n'est envoyée
                raise IpBanned(self.banned_until)
            p = {k: v for k, v in (params or {}).items() if v is not None}
            if signed:
                p["timestamp"] = self._now_ms()
                p["recvWindow"] = self.recv_window
            query = urlencode(p)
            if signed:
                query = f"{query}&signature={self.sign(query)}"
            url = f"{base}{path}" + (f"?{query}" if query else "")
            headers = {"X-MBX-APIKEY": self._key} if signed else {}
            retry_in = 0.5 * 2 ** (attempt - 1)
            try:
                resp = self._http.request(method, url, headers=headers)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                # la connexion n'a pas été établie : la requête n'est pas partie
                if kind == "order" or attempt >= self.max_retries:
                    raise BrokerUnavailable(f"Binance injoignable : {type(exc).__name__}") from None
                self._sleep(retry_in)
                continue
            except httpx.HTTPError as exc:
                if kind == "order":
                    raise OrderUncertain(client_id, type(exc).__name__) from None
                if attempt >= self.max_retries:
                    raise BrokerUnavailable(f"Binance : {type(exc).__name__}") from None
                self._sleep(retry_in)
                continue

            status = resp.status_code
            if status == 418:
                # IP bannie : on coupe TOUT pendant la durée indiquée (sinon 5 min)
                now = self._clock()
                self.banned_until = now + parse_retry_after(
                    resp.headers.get("Retry-After"), now, default=BAN_DEFAULT_S, cap=BAN_MAX_S)
                raise IpBanned(self.banned_until)
            if status == 429:
                if kind == "order" or attempt >= self.max_retries:
                    raise RateLimited(f"Binance : limite de requêtes (HTTP {status})")
                wait = parse_retry_after(resp.headers.get("Retry-After"), self._clock(),
                                         default=retry_in)
                self._sleep(max(retry_in, wait))
                continue
            if status >= 500:
                if kind == "order":
                    raise OrderUncertain(client_id, f"HTTP {status}")
                if attempt >= self.max_retries:
                    raise BrokerUnavailable(f"Binance : HTTP {status}")
                self._sleep(retry_in)
                continue
            try:
                data = resp.json()
            except ValueError:
                if kind == "order":
                    raise OrderUncertain(client_id, "réponse illisible") from None
                raise ApiError("réponse Binance illisible", http_status=status) from None
            if status >= 400:
                code = data.get("code") if isinstance(data, dict) else None
                msg = data.get("msg", "") if isinstance(data, dict) else str(data)
                if code == CODE_TIMESTAMP and not resynced:
                    resynced = True          # requête refusée de façon certaine : on peut réessayer
                    self.sync_time()
                    continue
                err = OrderRejected if kind == "order" else ApiError
                raise err(f"Binance {code} : {msg}", code=code, http_status=status)
            return data

    # --- marché (toujours le vrai marché) ---------------------------------------------------

    def candles(self, symbol: str, limit: int = 300) -> pd.DataFrame:
        rows = self._request("GET", self.market_data_url, EP_KLINES,
                             {"symbol": symbol, "interval": "1h", "limit": min(limit + 1, 1000)})
        now = self._now_ms()
        closed = [r for r in rows if int(r[6]) < now]     # closeTime passé : bougie clôturée
        df = pd.DataFrame({
            "ts": [int(r[0]) // 1000 for r in closed],
            "close_ts": [(int(r[6]) + 1) // 1000 for r in closed],
            "open": [float(r[1]) for r in closed],
            "high": [float(r[2]) for r in closed],
            "low": [float(r[3]) for r in closed],
            "close": [float(r[4]) for r in closed],
            "volume": [float(r[5]) for r in closed],
        })
        return df.tail(limit).reset_index(drop=True)

    def quote(self, symbol: str) -> tuple[float, float]:
        d = self._request("GET", self.market_data_url, EP_BOOK_TICKER, {"symbol": symbol})
        bid, ask = float(d["bidPrice"]), float(d["askPrice"])
        if not (bid > 0 and ask >= bid):
            raise ApiError(f"cotation incohérente pour {symbol} : {bid} / {ask}")
        return bid, ask

    def filters(self, symbol: str) -> SymbolFilters:
        if symbol not in self._filters:
            info = self._request("GET", self.trading_url, EP_EXCHANGE_INFO, {"symbol": symbol})
            s = info["symbols"][0]
            f = {x["filterType"]: x for x in s["filters"]}
            notional = f.get("NOTIONAL") or f.get("MIN_NOTIONAL") or {}
            self._filters[symbol] = SymbolFilters(
                base_asset=s["baseAsset"], quote_asset=s["quoteAsset"],
                step_size=Decimal(f["LOT_SIZE"]["stepSize"]),
                min_qty=Decimal(f["LOT_SIZE"]["minQty"]),
                max_qty=Decimal(f["LOT_SIZE"]["maxQty"]),
                tick_size=Decimal(f["PRICE_FILTER"]["tickSize"]),
                min_notional=Decimal(notional.get("minNotional", "0")))
        return self._filters[symbol]

    def min_qty(self, symbol: str) -> float:
        """Taille minimale d'un ordre (LOT_SIZE), pour la règle petit compte."""
        return float(self.filters(symbol).min_qty)

    # --- compte -----------------------------------------------------------------------------

    def balances(self) -> dict[str, tuple[float, float]]:
        acc = self._request("GET", self.trading_url, EP_ACCOUNT, signed=True)
        return {b["asset"]: (float(b["free"]), float(b["locked"])) for b in acc["balances"]}

    def snapshot_baseline(self) -> dict[str, float]:
        """Relevé des avoirs actuels (personnels). Utiliser ensure_baseline() : le relevé ne
        doit être fait qu'au PREMIER démarrage du mode, puis relu depuis le journal."""
        self.baseline = {a: free + locked for a, (free, locked) in self.balances().items()
                         if free + locked > 0}
        return dict(self.baseline)

    def ensure_baseline(self, journal) -> dict[str, float]:
        """Avoirs de départ : relus depuis le journal, ou relevés et enregistrés une seule
        fois au premier démarrage. Jamais recalculés après un redémarrage (CDC §6)."""
        saved = journal.baseline()
        if saved is None:
            saved = self.snapshot_baseline()
            journal.save_baseline(saved)
        self.baseline = dict(saved)
        return dict(saved)

    def uncovered_positions(self) -> list[str]:
        """Positions suivies que le solde réel (libre + bloqué) ne couvre plus (CDC §6 : le
        solde doit couvrir chaque position du journal). Tolérance : 1 % (frais, arrondis)."""
        by_asset: dict[str, list[BinancePosition]] = {}
        for pos in self._positions.values():
            if pos.status != "closed":
                by_asset.setdefault(self.filters(pos.symbol).base_asset, []).append(pos)
        if not by_asset:
            return []
        bal = self.balances()
        out: list[str] = []
        for asset, plist in by_asset.items():
            free, locked = bal.get(asset, (0.0, 0.0))
            need = sum((Decimal(str(p.qty)) for p in plist), Decimal(0))
            if Decimal(str(free + locked)) < need * Decimal("0.99"):
                out.extend(p.id for p in plist)
        return out

    def untracked_balances(self) -> dict[str, float]:
        """Avoirs inexpliqués (ni personnels au départ, ni positions suivies) : signe d'un
        ordre perdu, la réconciliation met alors le robot en pause (CDC §6)."""
        held: dict[str, Decimal] = {}
        for pos in self._positions.values():
            if pos.status != "closed":
                asset = self.filters(pos.symbol).base_asset
                held[asset] = held.get(asset, Decimal(0)) + Decimal(str(pos.qty))
        out = {}
        for asset, (free, locked) in self.balances().items():
            if asset in {self.filters(p.symbol).quote_asset for p in self._positions.values()}:
                continue
            extra = (Decimal(str(free + locked)) - Decimal(str(self.baseline.get(asset, 0.0)))
                     - held.get(asset, Decimal(0)))
            if extra > 0:
                out[asset] = float(extra)
        return out

    def quote_assets(self) -> set[str]:
        """Devises de cotation (quoteAsset) des marchés configurés et des positions suivies."""
        symbols = set(self.symbols) | {p.symbol for p in self._positions.values()
                                       if p.status != "closed"}
        return {self.filters(s).quote_asset for s in symbols}

    def equity(self) -> float:
        """Capital du robot : soldes des devises de cotation des marchés configurés (lues dans
        exchangeInfo, jamais supposées) + valeur des positions suivies. Les avoirs personnels
        en actifs de base ne sont pas comptés. Plusieurs devises de cotation (ex. USDT et USDC)
        sont additionnées comme équivalentes (limite connue, CDC §11)."""
        bal = self.balances()
        total = 0.0
        for asset in self.quote_assets():
            free, locked = bal.get(asset, (0.0, 0.0))
            total += free + locked
        # M4 : une position n'est comptée que pour la quantité réellement détenue par le robot
        # (solde de l'actif - avoirs personnels de départ). Sinon, entre l'exécution de l'OCO et
        # sa constatation, la vente serait comptée deux fois (USDT reçus + position) et le plus
        # haut gonflé fausserait le drawdown.
        left: dict[str, float] = {}
        for pos in self._positions.values():
            if pos.status == "closed":
                continue
            asset = self.filters(pos.symbol).base_asset
            if asset not in left:
                free, locked = bal.get(asset, (0.0, 0.0))
                left[asset] = max(0.0, free + locked - float(self.baseline.get(asset, 0.0)))
            held = min(pos.qty, left[asset])
            left[asset] -= held
            if held > 0:
                bid, _ = self.quote(pos.symbol)
                total += held * bid
        return total

    # --- ordres -----------------------------------------------------------------------------

    def find_order(self, client_id: str, symbol: str) -> dict | None:
        try:
            return self._request("GET", self.trading_url, EP_ORDER,
                                 {"symbol": symbol, "origClientOrderId": client_id}, signed=True)
        except ApiError as exc:
            if exc.code in CODES_NOT_FOUND:
                return None
            raise

    def _find_oco(self, list_client_id: str) -> dict | None:
        try:
            return self._request("GET", self.trading_url, EP_ORDER_LIST,
                                 {"origClientOrderId": list_client_id}, signed=True)
        except ApiError as exc:
            if exc.code in CODES_NOT_FOUND:
                return None
            raise

    def _market(self, symbol: str, side: str, qty: Decimal, client_id: str) -> dict:
        return self._request("POST", self.trading_url, EP_ORDER, {
            "symbol": symbol, "side": side.upper(), "type": "MARKET", "quantity": _fmt(qty),
            "newClientOrderId": client_id, "newOrderRespType": "FULL",
        }, signed=True, kind="order", client_id=client_id)

    def _place_oco(self, pos: BinancePosition, qty: Decimal) -> None:
        f = self.filters(pos.symbol)
        # achat : stop arrondi vers le BAS (jamais au-dessus du stop calculé), objectif vers le
        # bas (sens prudent : plus facile à atteindre)
        stop = _floor(Decimal(str(pos.stop)), f.tick_size)
        target = _floor(Decimal(str(pos.target)), f.tick_size)
        bid, _ask = self.quote(pos.symbol)
        if stop >= Decimal(str(bid)):
            # le prix est déjà au stop : un STOP_LOSS_LIMIT serait refusé -> un cran sous le bid
            stop = _floor(Decimal(str(bid)) - f.tick_size, f.tick_size)
        limit = _floor(stop * (1 - self.stop_limit_offset), f.tick_size)
        pos.oco_seq += 1
        list_id = f"{pos.client_id}-o{pos.oco_seq}"
        params = {
            "symbol": pos.symbol, "side": "SELL", "quantity": _fmt(qty),
            "listClientOrderId": list_id,
            # identifiants des 2 jambes : retrouvables dans les ordres ouverts
            "aboveClientOrderId": f"{list_id}a", "belowClientOrderId": f"{list_id}b",
            "aboveType": "LIMIT_MAKER", "abovePrice": _fmt(target),
            "belowType": "STOP_LOSS_LIMIT", "belowStopPrice": _fmt(stop),
            "belowPrice": _fmt(limit), "belowTimeInForce": "GTC",
        }
        try:
            self._request("POST", self.trading_url, EP_OCO, params, signed=True, kind="order",
                          client_id=list_id)
        except OrderUncertain:
            # statut inconnu : on vérifie s'il existe avant de conclure (jamais de renvoi)
            found = self._find_oco(list_id)
            if not found or found.get("listOrderStatus") not in OCO_ACTIVE:
                raise
        pos.oco_id = list_id
        pos.status = "open"

    def _locate_oco(self, pos: BinancePosition) -> dict | None:
        """OCO de la position : par son identifiant, sinon dans l'historique des listes
        (allOrderList) par préfixe « <client_id>-o ». None = introuvable partout."""
        if pos.oco_id:
            lst = self._find_oco(pos.oco_id)
            if lst:
                return lst
        history = self._request("GET", self.trading_url, EP_ALL_ORDER_LIST,
                                {"limit": ALL_ORDER_LIST_LIMIT}, signed=True)
        mine = [x for x in history or []
                if str(x.get("listClientOrderId", "")).startswith(f"{pos.client_id}-o")]
        return max(mine, key=lambda x: x.get("transactionTime", 0)) if mine else None

    def _oco_fill(self, list_client_id: str) -> dict | None:
        """Jambe de l'OCO réellement exécutée (quantité > 0), sinon None."""
        return self._filled_leg(self._find_oco(list_client_id) if list_client_id else None)

    def _legs_state(self, lst: dict | None) -> tuple[dict | None, dict | None]:
        """(jambe encore ouverte d'un OCO terminé, jambe exécutée) — une seule lecture par
        jambe, arrêt dès qu'une jambe parle."""
        active = lst is not None and lst.get("listOrderStatus") in OCO_ACTIVE
        for leg in (lst or {}).get("orders", []):
            od = self._request("GET", self.trading_url, EP_ORDER,
                               {"symbol": leg["symbol"], "orderId": leg["orderId"]}, signed=True)
            if not active and od.get("status") in ("NEW", "PARTIALLY_FILLED"):
                return od, None
            if (_dec(od.get("executedQty")) or Decimal(0)) > 0:
                return None, od
        return None, None

    def _finish_triggered_stop(self, pos: BinancePosition, od: dict) -> StopStatus:
        """C1 : annule l'ordre limite resté ouvert puis vend au marché le reste. La quantité
        déjà exécutée est lue UNE fois, après l'annulation (jamais soustraite deux fois)."""
        pos.status = "unprotected"
        try:
            self._request("DELETE", self.trading_url, EP_ORDER,
                          {"symbol": pos.symbol, "orderId": od["orderId"]}, signed=True,
                          kind="cancel")
        except ApiError as exc:
            if exc.code not in CODES_NOT_FOUND:      # déjà exécuté ou annulé : on relit
                raise
        final = self._request("GET", self.trading_url, EP_ORDER,
                              {"symbol": pos.symbol, "orderId": od["orderId"]}, signed=True)
        executed = _dec(final.get("executedQty")) or Decimal(0)
        f = self.filters(pos.symbol)
        if self._consume_exit(pos, executed, final):     # T1 : compté une seule fois
            return "closed_by_platform"
        free, _locked = self.balances().get(f.base_asset, (0.0, 0.0))
        self._sell(pos, f.floor_qty(min(pos.qty, free)))   # incomplet -> PositionInconsistent
        return "closed_no_stop"

    def _filled_leg(self, lst: dict | None) -> dict | None:
        for leg in (lst or {}).get("orders", []):
            od = self._request("GET", self.trading_url, EP_ORDER,
                               {"symbol": leg["symbol"], "orderId": leg["orderId"]}, signed=True)
            if Decimal(str(od.get("executedQty", "0"))) > 0:
                return od
        return None

    def _sell(self, pos: BinancePosition, qty: Decimal) -> None:
        """Vente au marché de `qty`. La position n'est déclarée fermée que si la vente
        exécutée couvre la quantité suivie ; sinon elle devient « incohérente »."""
        f = self.filters(pos.symbol)
        if pos.exit_uncertain and self._resolve_uncertain_exit(pos):
            return                               # la vente précédente a été exécutée
        if qty < f.min_qty or qty <= 0:
            pos.status = "inconsistent"
            raise PositionInconsistent(pos.id, f"quantité vendable {qty} sous le minimum "
                                               f"({f.min_qty}) pour une position de {pos.qty}")
        pos.exit_seq += 1
        cid = f"{pos.client_id}-x{pos.exit_seq}"
        try:
            fill = self._market(pos.symbol, "sell", qty, cid)
        except OrderUncertain:
            pos.exit_uncertain, pos.exit_not_found = cid, 0   # cherchée avant tout renvoi
            raise
        except OrderRejected as exc:
            pos.status = "inconsistent"
            raise PositionInconsistent(pos.id, f"vente refusée : {exc}") from None
        executed = _dec(fill.get("executedQty"))
        if executed is None:                     # réponse illisible : vente peut-être faite
            pos.exit_uncertain, pos.exit_not_found = cid, 0
            raise OrderUncertain(cid, "réponse de vente sans quantité exécutée lisible")
        self._apply_exit(pos, executed, fill)

    def _resolve_uncertain_exit(self, pos: BinancePosition) -> bool:
        """C2 : vente de sortie incertaine recherchée AVANT toute nouvelle vente (sinon une
        vente déjà exécutée serait répétée sur les avoirs personnels). True si exécutée ;
        False si refusée, ou introuvable deux fois de suite (jamais exécutée) ; sinon
        OrderUncertain (on recommencera au prochain cycle)."""
        cid = pos.exit_uncertain
        od = self.find_order(cid, pos.symbol)
        if od is None:
            pos.exit_not_found += 1
            if pos.exit_not_found < NOT_FOUND_TO_GIVE_UP:
                raise OrderUncertain(cid, "vente de sortie introuvable, nouvelle recherche")
            pos.exit_uncertain = ""
            return False
        executed = _dec(od.get("executedQty"))
        if executed is None or (executed <= 0 and od.get("status") not in ORDER_FINAL_STATUSES):
            raise OrderUncertain(cid, f"vente de sortie au statut {od.get('status')}")
        pos.exit_uncertain = ""
        if executed <= 0:
            return False
        self._apply_exit(pos, executed, od)          # partielle -> PositionInconsistent
        return True

    def _consume_exit(self, pos: BinancePosition, executed: Decimal,
                      fill: dict | None = None) -> bool:
        """Compte une exécution de sortie UNE seule fois par ordre (T1). True si la position
        est entièrement sortie ; sinon pos.qty = reste à protéger ou à vendre."""
        f = self.filters(pos.symbol)
        key = str((fill or {}).get("orderId") or (fill or {}).get("clientOrderId") or "")
        already = Decimal(str(pos.exits_applied.get(key, "0"))) if key else Decimal(0)
        delta = max(executed - already, Decimal(0))
        if key:
            pos.exits_applied[key] = str(max(executed, already))
        pos.exit_price = _avg_price(fill) or pos.exit_price
        remaining = Decimal(str(pos.qty)) - delta
        if remaining < f.min_qty:
            pos.status = "closed"
            return True
        pos.qty = float(remaining)
        return False

    def _apply_exit(self, pos: BinancePosition, executed: Decimal, fill: dict | None = None) -> None:
        if self._consume_exit(pos, executed, fill):
            return
        pos.status = "inconsistent"
        raise PositionInconsistent(pos.id, f"sortie partielle : {pos.qty} restant")

    def open_position(self, symbol: str, side: str, qty: float, stop: float, target: float,
                      client_id: str) -> str:
        if side != "buy":
            raise ValueError("Binance spot : achat uniquement (aucune vente à découvert)")
        if not client_id or len(client_id) > CLIENT_ID_MAX:
            raise ValueError(f"identifiant client invalide (1 à {CLIENT_ID_MAX} caractères)")
        if not all(map(math.isfinite, (qty, stop, target))):
            raise ValueError("quantité, stop ou objectif invalide")
        f = self.filters(symbol)
        bid, ask = self.quote(symbol)
        if not stop < bid <= ask < target:
            raise ValueError(f"il faut stop < prix < objectif ({stop} / {bid}-{ask} / {target})")
        q = f.floor_qty(qty)
        if q < f.min_qty or q > f.max_qty:
            raise OrderRejected(f"{symbol} : quantité {q} hors des limites de la plateforme")
        if q * Decimal(str(ask)) < f.min_notional:
            raise OrderRejected(f"{symbol} : montant sous le minimum ({f.min_notional})")

        fill = self._market(symbol, "buy", q, client_id)   # OrderUncertain remonte tel quel
        executed = _dec(fill.get("executedQty"))
        if executed is None:
            # réponse sans quantité lisible (ACK, champ nul, tronquée) : peut-être exécuté
            raise OrderUncertain(client_id, "réponse d'achat sans quantité exécutée lisible")
        if executed <= 0:
            if fill.get("status") in ORDER_FINAL_STATUSES:
                raise OrderRejected(f"{symbol} : achat non exécuté ({fill.get('status')})")
            raise OrderUncertain(client_id, f"achat au statut {fill.get('status')}")
        return self._protect_new(symbol, client_id, executed, fill.get("fills") or [],
                                 _dec(fill.get("cummulativeQuoteQty")) or Decimal(0), stop,
                                 target)

    def recover_order(self, client_id: str, symbol: str, stop: float, target: float,
                      **_context) -> str | None:
        """Ordre d'achat au statut incertain (CDC §6), recherché à chaque cycle :
        - introuvable ou refusé sans exécution : None (aucune position) ;
        - exécuté : la position est créée et l'OCO posé avec le stop et l'objectif du
          signal d'origine ; si l'OCO échoue, vente immédiate (StopNotPlaced) ;
        - toujours indéterminé : OrderUncertain (on recommencera au cycle suivant)."""
        if client_id in self._positions:
            return client_id
        od = self.find_order(client_id, symbol)
        if od is None:
            # introuvable : non exécuté seulement après 2 recherches consécutives (M7), au cas
            # où la plateforme publierait l'ordre avec retard
            self._not_found[client_id] = self._not_found.get(client_id, 0) + 1
            if self._not_found[client_id] >= NOT_FOUND_TO_GIVE_UP:
                self._not_found.pop(client_id, None)
                return None
            raise OrderUncertain(client_id, "introuvable, nouvelle recherche au prochain cycle")
        self._not_found.pop(client_id, None)
        executed = _dec(od.get("executedQty"))
        if executed is None:
            raise OrderUncertain(client_id, "quantité exécutée illisible")
        if executed <= 0:
            if od.get("status") in ORDER_FINAL_STATUSES:
                return None
            raise OrderUncertain(client_id, f"statut {od.get('status')}")
        trades = self._request("GET", self.trading_url, EP_MY_TRADES,
                               {"symbol": symbol, "orderId": od["orderId"]}, signed=True)
        return self._protect_new(symbol, client_id, executed, trades,
                                 _dec(od.get("cummulativeQuoteQty")) or Decimal(0), stop, target)

    def _protect_new(self, symbol: str, client_id: str, executed: Decimal, fills: list,
                     quote_spent: Decimal, stop: float, target: float) -> str:
        """Achat exécuté : position suivie puis OCO posé aussitôt, sinon vente immédiate."""
        f = self.filters(symbol)
        # commission illisible = 0 : la position est TOUJOURS suivie et protégée (l'OCO est
        # de toute façon plafonné au solde libre réel)
        fee_base = sum((_dec(x.get("commission")) or Decimal(0) for x in fills
                        if isinstance(x, dict) and x.get("commissionAsset") == f.base_asset),
                       Decimal(0))
        net = f.floor_qty(float(executed - fee_base))
        entry = float(quote_spent / executed) if quote_spent > 0 else self.quote(symbol)[1]

        pos = BinancePosition(client_id, symbol, float(net), entry, stop, target, client_id,
                              status="unprotected")
        self._positions[pos.id] = pos
        try:
            self._place_oco(pos, net)
        except BrokerError as exc:
            # CDC §6 : si le stop ne peut pas être posé, la position est fermée aussitôt
            try:
                self._sell(pos, net)
            except BrokerError as sell_exc:
                raise StopNotPlaced(pos.id, closed=False, cause=f"{exc} / {sell_exc}") from None
            raise StopNotPlaced(pos.id, closed=True, cause=str(exc)) from None
        return pos.id

    def ensure_stop(self, position_id: str) -> StopStatus:
        pos = self._positions[position_id]
        if pos.status == "closed":
            return "closed_by_platform"
        oco = self._locate_oco(pos)
        if oco and oco.get("listOrderStatus") in OCO_ACTIVE:
            pos.oco_id = oco.get("listClientOrderId") or pos.oco_id
            pos.status = "open"
            return "ok"
        # C1 : OCO terminé mais une jambe encore ouverte = stop déclenché dont la limite a été
        # sautée (écart de prix) : la position n'est plus protégée -> annuler et vendre
        live, filled = self._legs_state(oco)
        if live is not None:
            return self._finish_triggered_stop(pos, live)
        # « fermée par la plateforme » UNIQUEMENT si une vente exécutée de l'OCO est vue
        if filled and self._consume_exit(pos, _dec(filled.get("executedQty")) or Decimal(0),
                                         filled):
            return "closed_by_platform"
        # sortie partielle (T1) : le reste n'est plus protégé -> OCO reposé ci-dessous
        # aucune exécution vue : le stop est absent -> le reposer, sinon vendre (CDC §6)
        f = self.filters(pos.symbol)
        free, _locked = self.balances().get(f.base_asset, (0.0, 0.0))
        held = f.floor_qty(min(pos.qty, free))      # quantité suivie, plafonnée au solde libre
        pos.status = "unprotected"
        try:
            if held < f.min_qty or held <= 0:
                raise BrokerError(f"solde libre {free} insuffisant pour reposer le stop")
            self._place_oco(pos, held)
        except BrokerError:
            self._sell(pos, held)            # quantité nulle ou refus -> PositionInconsistent
            return "closed_no_stop"
        if held < f.floor_qty(pos.qty):
            pos.status = "inconsistent"
            raise PositionInconsistent(pos.id, f"stop reposé sur {held} seulement, "
                                               f"position suivie {pos.qty}")
        return "replaced"

    def close_position(self, position_id: str) -> None:
        """Annule l'OCO puis vend. Ne déclare JAMAIS la position fermée sans avoir vu une
        vente exécutée ou l'exécution de l'OCO (CDC §6)."""
        pos = self._positions[position_id]
        if pos.status == "closed":
            return
        f = self.filters(pos.symbol)
        if pos.oco_id:
            try:
                self._request("DELETE", self.trading_url, EP_ORDER_LIST,
                              {"symbol": pos.symbol, "listClientOrderId": pos.oco_id},
                              signed=True, kind="cancel")
            except ApiError as exc:
                if exc.code not in CODES_NOT_FOUND:   # déjà exécuté ou annulé : on vérifie
                    raise
            filled = self._oco_fill(pos.oco_id)
            if filled and self._consume_exit(pos, _dec(filled.get("executedQty")) or Decimal(0),
                                             filled):
                return                                 # stop ou objectif exécuté en entier
        pos.status = "unprotected"
        # l'annulation doit avoir libéré la quantité : plus aucun ordre du robot ouvert
        still_open = [o for o in self._request("GET", self.trading_url, EP_OPEN_ORDERS,
                                               {"symbol": pos.symbol}, signed=True)
                      if str(o.get("clientOrderId", "")).startswith(pos.client_id)]
        if still_open:
            pos.status = "inconsistent"
            raise PositionInconsistent(pos.id, f"{len(still_open)} ordre(s) encore ouvert(s) "
                                               "après l'annulation de l'OCO")
        free, _locked = self.balances().get(f.base_asset, (0.0, 0.0))
        # quantité suivie, plafonnée au solde libre réel (jamais « solde − départ »)
        self._sell(pos, f.floor_qty(min(pos.qty, free)))

    def adopt(self, position: dict) -> None:
        """Reprend une position (format de positions()) après un redémarrage."""
        pos = BinancePosition(**position)
        self._positions[pos.id] = pos

    def adopt_journal_row(self, row: dict) -> None:
        """Reprend une position ouverte du journal : quantité, stop, objectif et OCO suivis."""
        ref = row.get("protection_ref") or ""
        seq = int(ref.rsplit("-o", 1)[1]) if "-o" in ref and ref.rsplit("-o", 1)[1].isdigit() else 0
        self.adopt({"id": row["id"], "symbol": row["market"], "qty": row["qty"],
                    "entry": row["entry"], "stop": row["stop"], "target": row["target"],
                    "client_id": row["client_id"], "oco_id": ref, "oco_seq": seq,
                    "status": "open" if ref else "unprotected"})

    def exit_price(self, position_id: str) -> float | None:
        """A6 : prix de sortie réellement exécuté, None si inconnu."""
        pos = self._positions.get(position_id)
        return pos.exit_price if pos and pos.exit_price > 0 else None

    def position(self, position_id: str) -> dict | None:
        pos = self._positions.get(position_id)
        return asdict(pos) if pos else None

    def positions(self) -> list[dict]:
        return [asdict(p) for p in self._positions.values() if p.status != "closed"]
