"""Capital.com CFD (CDC §2, §6).

- Session : POST /api/v1/session (clé API + identifiant + mot de passe) ; les en-têtes CST et
  X-SECURITY-TOKEN de la réponse authentifient les requêtes suivantes. Ping si aucune requête
  depuis N minutes (la session expire après 10 min d'inactivité). Erreur 401 -> reconnexion
  automatique, UNE seule nouvelle tentative par requête (un 401 garantit que rien n'a été
  exécuté, même pour un ordre).
- Comptes démo et réel séparés (url_demo / url_live de config.yaml).
- Bougies 1 h CLÔTURÉES (/api/v1/prices), prix moyen bid/ask ; bid/ask, taille minimale et
  levier depuis /api/v1/markets.
- Ouverture avec stopLevel ET profitLevel attachés, puis confirmation par la dealReference
  (/api/v1/confirms). Stop absent après confirmation -> ajouté par PUT, sinon fermeture.
- ensure_stop : stop absent -> ajouté par PUT ; impossible -> fermeture (DELETE).
- Une position n'est considérée fermée qu'après 2 absences consécutives dans /api/v1/positions.
- Une position ouverte à la main est signalée (manual_positions), jamais reprise.
- Ordre au statut incertain : jamais renvoyé. Capital.com ne permet pas de fixer un identifiant
  client : on le recherche par sa dealReference si elle est connue, sinon parmi les positions
  ouvertes (même marché, même sens, même taille, ouverte après l'envoi, non suivie) ; il n'est
  déclaré non exécuté qu'après 2 recherches infructueuses consécutives.

À VÉRIFIER LE JOUR 1 DE LA DÉMO (noms de champs isolés ci-dessous, CDC §9) :
  1. Champs du solde (balance / profitLoss) et du compte préféré dans /api/v1/accounts.
  2. Levier : instrument.marginFactor (+ marginFactorUnit) dans /api/v1/markets/{epic}.
  3. Taille minimale : dealingRules.minDealSize.value ; pas : dealingRules.minSizeIncrement.
  4. Distance minimale du stop : dealingRules.minStopOrProfitDistance (unité PERCENTAGE/POINTS).
  5. Bougies : snapshotTimeUTC = début de la bougie ; prix bid/ask dans openPrice/closePrice…
  6. Confirmation : dealStatus ACCEPTED/REJECTED, affectedDeals[0].dealId = identifiant de la
     position ; stopLevel renvoyé quand le stop est bien attaché.
  7. Positions : positions[].position.{dealId, direction, size, level, stopLevel, profitLevel,
     createdDateUTC} et positions[].market.epic.
  8. Codes d'erreur « introuvable » d'un DELETE sur une position déjà fermée (404).
"""
from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import ROUND_FLOOR, Decimal

import httpx
import pandas as pd

from tradebot.brokers.base import (
    ApiError,
    Broker,
    BrokerError,
    BrokerUnavailable,
    OrderRejected,
    OrderUncertain,
    PositionInconsistent,
    RateLimited,
    StopNotPlaced,
    StopStatus,
    parse_retry_after,
)

# --- Constantes de l'API (isolées pour être corrigées facilement au jour 1) ------------------
EP_SESSION = "/api/v1/session"
EP_PING = "/api/v1/ping"
EP_ACCOUNTS = "/api/v1/accounts"
EP_MARKET = "/api/v1/markets/{epic}"
EP_PRICES = "/api/v1/prices/{epic}"
EP_POSITIONS = "/api/v1/positions"
EP_POSITION = "/api/v1/positions/{deal_id}"
EP_CONFIRM = "/api/v1/confirms/{deal_reference}"
H_API_KEY = "X-CAP-API-KEY"
H_CST = "CST"
H_SECURITY_HEADER = "X-SECURITY-TOKEN"
F_DEAL_REFERENCE = "dealReference"
F_DEAL_STATUS = "dealStatus"
F_AFFECTED = "affectedDeals"
ACCEPTED = "ACCEPTED"
TRADEABLE = "TRADEABLE"
RESOLUTION_1H = "HOUR"
MAX_CANDLES = 1000
HOUR_S = 3600
ABSENCES_TO_CLOSE = 2          # CDC §6 : fermée seulement après 2 absences consécutives
UNCERTAIN_MATCH_WINDOW_S = 120  # une position ouverte jusqu'à 2 min avant l'envoi peut être la nôtre


@dataclass
class CapitalPosition:
    id: str                    # dealId de la position
    symbol: str                # epic
    side: str                  # buy | sell
    qty: float
    entry: float
    stop: float
    target: float
    client_id: str
    deal_reference: str = ""
    oco_id: str = ""           # (sans objet : stop et objectif sont attachés à la position)
    absences: int = 0
    status: str = "open"       # open | unprotected | inconsistent | closed
    exit_price: float = 0.0    # A6 : niveau de clôture confirmé par Capital.com (0 = inconnu)


def _num(value) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _utc_ts(text: str) -> int:
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp())


class CapitalCFD(Broker):
    name = "capital"

    def __init__(self, api_key: str, identifier: str, password: str, base_url: str,
                 keepalive_minutes: int = 5, *, client: httpx.Client | None = None,
                 max_retries: int = 3, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time, symbols: list[str] | None = None):
        if not api_key or not identifier or not password:
            raise BrokerError("identifiants Capital.com absents")
        self._key, self._identifier, self._password = api_key, identifier, password
        self.base_url = base_url.rstrip("/")
        self.keepalive_s = keepalive_minutes * 60
        self._http = client or httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0))
        self.max_retries = max_retries
        self._sleep = sleep
        self._clock = clock
        self.symbols: list[str] = list(symbols or [])
        self._cst = ""
        self._token = ""
        self._last_request = float("-inf")
        self.account_currency = ""
        self._markets: dict[str, dict] = {}
        self._positions: dict[str, CapitalPosition] = {}
        self._pending: dict[str, dict] = {}      # client_id -> ordre au statut incertain

    def __repr__(self) -> str:   # jamais de secret dans les journaux
        return f"CapitalCFD(base_url={self.base_url!r})"

    @classmethod
    def from_settings(cls, settings, env: dict, **kw) -> CapitalCFD:
        cfg = settings.platforms.capital
        url = cfg.url_live if settings.mode == "live" else cfg.url_demo
        symbols = [m.symbol for m in settings.markets if m.platform == "capital" and m.enabled]
        return cls(env.get("CAPITAL_API_KEY", ""), env.get("CAPITAL_IDENTIFIER", ""),
                   env.get("CAPITAL_PASSWORD", ""), url, cfg.keepalive_minutes,
                   symbols=symbols, **kw)

    # --- session ----------------------------------------------------------------------------

    def login(self) -> None:
        """Ouvre une session : CST + X-SECURITY-TOKEN (CDC §2)."""
        try:
            resp = self._http.post(f"{self.base_url}{EP_SESSION}",
                                   headers={H_API_KEY: self._key},
                                   json={"identifier": self._identifier,
                                         "password": self._password,
                                         "encryptedPassword": False})
        except httpx.HTTPError as exc:
            raise BrokerUnavailable(f"Capital.com injoignable : {type(exc).__name__}") from None
        if resp.status_code != 200:
            raise ApiError(f"connexion Capital.com refusée (HTTP {resp.status_code})",
                           http_status=resp.status_code)
        self._cst = resp.headers.get(H_CST, "")
        self._token = resp.headers.get(H_SECURITY_HEADER, "")
        if not self._cst or not self._token:
            raise ApiError("connexion Capital.com : jetons de session absents")
        try:
            self.account_currency = str(resp.json().get("currencyIsoCode", ""))
        except ValueError:
            self.account_currency = ""
        self._last_request = self._clock()

    def keepalive(self) -> None:
        """Ping si la session n'a servi depuis keepalive_minutes (expire après 10 min)."""
        if self._cst and self._clock() - self._last_request >= self.keepalive_s:
            self._request("GET", EP_PING)

    def _headers(self) -> dict:
        return {H_API_KEY: self._key, H_CST: self._cst, H_SECURITY_HEADER: self._token}

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 body: dict | None = None, kind: str = "read", client_id: str = ""):
        """kind = "read" (nouvelles tentatives) ou "order" (jamais renvoyé si incertain)."""
        if not self._cst:
            self.login()
        if path != EP_PING:
            self.keepalive()
        attempt, relogged = 0, False
        while True:
            attempt += 1
            retry_in = 0.5 * 2 ** (attempt - 1)
            try:
                resp = self._http.request(method, f"{self.base_url}{path}", params=params,
                                          json=body, headers=self._headers())
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                if kind == "order" or attempt >= self.max_retries:
                    raise BrokerUnavailable(f"Capital.com injoignable : {type(exc).__name__}") \
                        from None
                self._sleep(retry_in)
                continue
            except httpx.HTTPError as exc:
                if kind == "order":
                    raise OrderUncertain(client_id, type(exc).__name__) from None
                if attempt >= self.max_retries:
                    raise BrokerUnavailable(f"Capital.com : {type(exc).__name__}") from None
                self._sleep(retry_in)
                continue
            self._last_request = self._clock()
            status = resp.status_code
            if status == 401 and not relogged:
                relogged = True              # session expirée : une seule reconnexion
                self.login()
                continue
            if status == 429:
                if kind == "order" or attempt >= self.max_retries:
                    raise RateLimited(f"Capital.com : limite de requêtes (HTTP {status})")
                self._sleep(max(retry_in, parse_retry_after(resp.headers.get("Retry-After"),
                                                            self._clock(), default=retry_in)))
                continue
            if status >= 500:
                if kind == "order":
                    raise OrderUncertain(client_id, f"HTTP {status}")
                if attempt >= self.max_retries:
                    raise BrokerUnavailable(f"Capital.com : HTTP {status}")
                self._sleep(retry_in)
                continue
            try:
                data = resp.json() if resp.content else {}
            except ValueError:
                if kind == "order":
                    raise OrderUncertain(client_id, "réponse illisible") from None
                raise ApiError("réponse Capital.com illisible", http_status=status) from None
            if status >= 400:
                code = data.get("errorCode", "") if isinstance(data, dict) else ""
                err = OrderRejected if kind == "order" else ApiError
                raise err(f"Capital.com {status} : {code}", http_status=status)
            return data

    # --- marché -----------------------------------------------------------------------------

    def market(self, symbol: str, refresh: bool = False) -> dict:
        if refresh or symbol not in self._markets:
            self._markets[symbol] = self._request("GET", EP_MARKET.format(epic=symbol))
        return self._markets[symbol]

    def quote(self, symbol: str) -> tuple[float, float]:
        snap = self.market(symbol, refresh=True).get("snapshot", {})
        bid, ask = _num(snap.get("bid")), _num(snap.get("offer"))
        if snap.get("marketStatus") not in (None, TRADEABLE):
            raise BrokerUnavailable(f"{symbol} : marché fermé ({snap.get('marketStatus')})")
        if bid is None or ask is None or not (0 < bid <= ask):
            raise ApiError(f"cotation incohérente pour {symbol} : {bid} / {ask}")
        return bid, ask

    def min_qty(self, symbol: str) -> float:
        rules = self.market(symbol).get("dealingRules", {})
        return _num(rules.get("minDealSize", {}).get("value")) or 0.0

    def size_step(self, symbol: str) -> float:
        """Pas de taille (dealingRules.minSizeIncrement) ; 0 si inconnu."""
        rules = self.market(symbol).get("dealingRules", {})
        return _num((rules.get("minSizeIncrement") or {}).get("value")) or 0.0

    def _same_size(self, symbol: str, a: float | None, b: float | None) -> bool:
        """A1 : tailles égales à un demi-pas près (arrondi de la plateforme, flottants)."""
        if a is None or b is None:
            return False
        tol = max(self.size_step(symbol) / 2, 1e-9 * max(abs(a), abs(b), 1.0))
        return abs(self.round_size(symbol, b) - a) <= tol or abs(a - b) <= tol

    def round_size(self, symbol: str, qty: float) -> float:
        """A2 : taille arrondie VERS LE BAS au pas de la plateforme (jamais plus de risque)."""
        step = self.size_step(symbol)
        if step <= 0:
            return qty
        d_step = Decimal(str(step))
        return float((Decimal(str(qty)) / d_step).to_integral_value(rounding=ROUND_FLOOR)
                     * d_step)

    def leverage(self, symbol: str) -> float:
        """Levier = 100 / marge exigée (%) ; sert au plafond de 25 % calculé sur la marge."""
        inst = self.market(symbol).get("instrument", {})
        factor = _num(inst.get("marginFactor"))
        if not factor or inst.get("marginFactorUnit", "PERCENTAGE") != "PERCENTAGE":
            raise ApiError(f"{symbol} : levier illisible ({inst.get('marginFactor')})")
        return 100.0 / factor

    def candles(self, symbol: str, limit: int = 300) -> pd.DataFrame:
        data = self._request("GET", EP_PRICES.format(epic=symbol),
                             params={"resolution": RESOLUTION_1H,
                                     "max": min(limit + 1, MAX_CANDLES)})
        return self._to_frame(data.get("prices", []), limit)

    def history(self, symbol: str, start_ts: float, end_ts: float) -> pd.DataFrame:
        """Bougies 1 h clôturées entre deux dates, par paquets de 1000 (validation, étape 9)."""
        prices: list[dict] = []
        step = MAX_CANDLES * HOUR_S
        t = int(start_ts)
        while t < end_ts:
            to = min(int(end_ts), t + step)
            fmt = "%Y-%m-%dT%H:%M:%S"
            data = self._request("GET", EP_PRICES.format(epic=symbol), params={
                "resolution": RESOLUTION_1H, "max": MAX_CANDLES,
                "from": datetime.fromtimestamp(t, UTC).strftime(fmt),
                "to": datetime.fromtimestamp(to, UTC).strftime(fmt)})
            prices += data.get("prices", [])
            t = to
        return self._to_frame(prices, None)

    def _to_frame(self, raw_prices: list[dict], limit: int | None) -> pd.DataFrame:
        now = self._clock()
        rows = []
        seen: set[int] = set()
        for p in raw_prices:
            ts = _utc_ts(p["snapshotTimeUTC"])
            if ts + HOUR_S > now or ts in seen:   # bougie en cours ou en double : ignorée
                continue
            seen.add(ts)

            def mid(field: str, p=p) -> float:
                q = p[field]
                return (float(q["bid"]) + float(q["ask"])) / 2

            rows.append({"ts": ts, "close_ts": ts + HOUR_S, "open": mid("openPrice"),
                         "high": mid("highPrice"), "low": mid("lowPrice"),
                         "close": mid("closePrice"),
                         "volume": float(p.get("lastTradedVolume") or 0)})
        df = pd.DataFrame(rows, columns=["ts", "close_ts", "open", "high", "low", "close",
                                         "volume"])
        df = df.sort_values("ts")
        return (df.tail(limit) if limit else df).reset_index(drop=True)

    # --- compte -----------------------------------------------------------------------------

    def equity(self) -> float:
        """Capital = solde + résultat latent du compte préféré (devise du compte, voir A10)."""
        accounts = self._request("GET", EP_ACCOUNTS).get("accounts", [])
        acc = next((a for a in accounts if a.get("preferred")), accounts[0] if accounts else None)
        if acc is None:
            raise ApiError("aucun compte Capital.com")
        bal = acc.get("balance", {})
        total = (_num(bal.get("balance")) or 0.0) + (_num(bal.get("profitLoss")) or 0.0)
        self.account_currency = acc.get("currency", self.account_currency)
        return total

    def _open_on_platform(self) -> list[dict]:
        out = []
        body = self._request("GET", EP_POSITIONS)
        items = body.get("positions") if isinstance(body, dict) else None
        if not isinstance(items, list):
            # ADV10 : corps vide ou tronqué = réponse illisible, JAMAIS « aucune position »
            # (sinon une position ouverte serait déclarée fermée après 2 absences)
            raise ApiError("liste des positions illisible (réponse vide ou tronquée)")
        for item in items:
            if not isinstance(item, dict):
                raise ApiError("position illisible dans la liste des positions")
            pos, mkt = item.get("position") or {}, item.get("market") or {}
            if not pos.get("dealId"):
                raise ApiError("position sans dealId dans la liste des positions")
            out.append({"deal_id": str(pos.get("dealId", "")), "epic": mkt.get("epic", ""),
                        "side": str(pos.get("direction", "")).lower(),
                        "size": _num(pos.get("size")), "level": _num(pos.get("level")),
                        "stop": _num(pos.get("stopLevel")),
                        "target": _num(pos.get("profitLevel")),
                        "created": pos.get("createdDateUTC", "")})
        return out

    # --- ordres -----------------------------------------------------------------------------

    def _confirm(self, deal_reference: str, client_id: str) -> dict:
        """C3 / ADV7 : après un envoi ACCEPTÉ, toute erreur de confirmation (404, 4xx, 429,
        corps illisible) laisse l'ordre INCERTAIN : il est peut-être exécuté."""
        try:
            conf = self._request("GET", EP_CONFIRM.format(deal_reference=deal_reference))
        except BrokerError as exc:
            raise OrderUncertain(client_id, f"confirmation impossible : {exc}") from None
        if not isinstance(conf, dict) or not conf.get(F_DEAL_STATUS):
            raise OrderUncertain(client_id, "confirmation illisible (dealStatus absent)")
        return conf

    def pending_reference(self, client_id: str) -> str:
        """dealReference d'un ordre envoyé mais non confirmé (M7 : gardée dans le journal)."""
        return str(self._pending.get(client_id, {}).get("deal_reference", ""))

    def find_order(self, client_id: str, symbol: str) -> dict | None:
        """Ordre recherché par sa référence de transaction (dealReference) si connue."""
        ref = self._pending.get(client_id, {}).get("deal_reference")
        if not ref:
            pos = self._positions.get(client_id) or next(
                (p for p in self._positions.values() if p.client_id == client_id), None)
            ref = pos.deal_reference if pos else ""
        return self._confirm(ref, client_id) if ref else None

    def open_position(self, symbol: str, side: str, qty: float, stop: float, target: float,
                      client_id: str) -> str:
        if side not in ("buy", "sell"):
            raise ValueError(f"sens inconnu : {side}")
        if not all(map(math.isfinite, (qty, stop, target))) or qty <= 0:
            raise ValueError("quantité, stop ou objectif invalide")
        bid, ask = self.quote(symbol)
        price = ask if side == "buy" else bid
        if (side == "buy" and not stop < bid <= ask < target) or \
           (side == "sell" and not target < bid <= ask < stop):
            raise ValueError(f"stop et objectif du mauvais côté du prix ({stop} / {bid}-{ask} "
                             f"/ {target})")
        qty = self.round_size(symbol, qty)
        if qty <= 0 or qty < self.min_qty(symbol):
            raise OrderRejected(f"{symbol} : taille {qty} sous le minimum {self.min_qty(symbol)}")
        self._check_stop_distance(symbol, price, stop)
        self._pending[client_id] = {"symbol": symbol, "side": side, "qty": qty, "stop": stop,
                                    "target": target, "sent": self._clock(), "absences": 0,
                                    "price": price}
        resp = self._request("POST", EP_POSITIONS, kind="order", client_id=client_id, body={
            "epic": symbol, "direction": side.upper(), "size": qty, "guaranteedStop": False,
            "stopLevel": stop, "profitLevel": target})
        ref = str(resp.get(F_DEAL_REFERENCE, ""))
        if not ref:
            raise OrderUncertain(client_id, "aucune dealReference dans la réponse")
        self._pending[client_id]["deal_reference"] = ref
        return self._from_confirmation(self._confirm(ref, client_id), client_id)

    def _check_stop_distance(self, symbol: str, price: float, stop: float) -> None:
        rule = self.market(symbol).get("dealingRules", {}).get("minStopOrProfitDistance") or {}
        value = _num(rule.get("value"))
        if not value:
            return
        minimum = price * value / 100 if rule.get("unit") == "PERCENTAGE" else value
        if abs(price - stop) < minimum:
            raise OrderRejected(f"{symbol} : stop trop proche ({abs(price - stop)} < {minimum})")

    def _from_confirmation(self, conf: dict, client_id: str) -> str:
        """Confirmation acceptée -> position suivie, stop vérifié ; refusée -> OrderRejected."""
        pend = self._pending.get(client_id, {})
        if conf.get(F_DEAL_STATUS) != ACCEPTED:
            self._pending.pop(client_id, None)
            raise OrderRejected(f"{pend.get('symbol', '')} : ordre refusé par Capital.com "
                                f"({conf.get('reason', conf.get(F_DEAL_STATUS))})")
        deals = conf.get(F_AFFECTED) or []
        first = deals[0] if deals and isinstance(deals[0], dict) else None
        raw_id = first.get("dealId") if first is not None else conf.get("dealId")
        deal_id = "" if raw_id is None else str(raw_id).strip()
        if not deal_id or deal_id.lower() in ("none", "null"):          # ADV9
            raise OrderUncertain(client_id, "confirmation sans identifiant de position")
        pos = CapitalPosition(
            id=deal_id, symbol=pend.get("symbol", conf.get("epic", "")),
            side=pend.get("side", str(conf.get("direction", "")).lower()),
            qty=_num(conf.get("size")) or pend.get("qty", 0.0),
            # ADV8 : niveau absent -> prix au moment de l'envoi (jamais 0)
            entry=_num(conf.get("level")) or pend.get("price", 0.0), stop=pend.get("stop", 0.0),
            target=pend.get("target", 0.0), client_id=client_id,
            deal_reference=pend.get("deal_reference", ""), status="unprotected")
        return self._track_and_protect(pos, attached_stop=_num(conf.get("stopLevel")),
                                       client_id=client_id)

    def _track_and_protect(self, pos: CapitalPosition, attached_stop: float | None,
                           client_id: str) -> str:
        self._positions[pos.id] = pos
        self._pending.pop(client_id, None)
        if attached_stop is not None:
            pos.status = "open"
            return pos.id
        # CDC §6 : stop absent -> ajouté par modification, sinon fermeture immédiate
        try:
            self._put_stop(pos)
        except BrokerError as exc:
            try:
                self._delete(pos)
            except BrokerError as close_exc:
                raise StopNotPlaced(pos.id, closed=False, cause=f"{exc} / {close_exc}") \
                    from None
            raise StopNotPlaced(pos.id, closed=True, cause=str(exc)) from None
        return pos.id

    def recover_order(self, client_id: str, symbol: str, stop: float, target: float, *,
                      side: str | None = None, qty: float | None = None,
                      created_ts: float | None = None, reference: str = "") -> str | None:
        """Ordre au statut incertain, recherché à chaque cycle (jamais renvoyé). `reference` :
        dealReference relue dans le journal après un redémarrage (M7)."""
        tracked = next((p for p in self._positions.values() if p.client_id == client_id), None)
        if tracked:
            return tracked.id
        pend = self._pending.setdefault(client_id, {
            "symbol": symbol, "side": side, "qty": qty, "stop": stop, "target": target,
            "sent": created_ts if created_ts is not None else self._clock(), "absences": 0})
        if reference and not pend.get("deal_reference"):
            pend["deal_reference"] = reference
        if pend.get("deal_reference"):
            try:
                return self._from_confirmation(self._confirm(pend["deal_reference"], client_id),
                                               client_id)
            except OrderRejected:
                return None
        # pas de référence : recherche parmi les positions ouvertes non suivies
        known = set(self._positions)
        for p in self._open_on_platform():
            if (p["deal_id"] not in known and p["epic"] == symbol and p["side"] == pend["side"]
                    and self._same_size(symbol, p["size"], pend["qty"]) and p["created"]
                    and _utc_ts(p["created"]) >= pend["sent"] - UNCERTAIN_MATCH_WINDOW_S):
                pos = CapitalPosition(id=p["deal_id"], symbol=symbol, side=p["side"],
                                      qty=p["size"] or 0.0, entry=p["level"] or 0.0,
                                      stop=stop, target=target, client_id=client_id,
                                      status="unprotected")
                return self._track_and_protect(pos, attached_stop=p["stop"],
                                               client_id=client_id)
        pend["absences"] += 1
        if pend["absences"] >= ABSENCES_TO_CLOSE:
            self._pending.pop(client_id, None)
            return None                         # jamais exécuté
        raise OrderUncertain(client_id, "position introuvable, nouvelle recherche au prochain "
                                        "cycle")

    def _put_stop(self, pos: CapitalPosition) -> None:
        resp = self._request("PUT", EP_POSITION.format(deal_id=pos.id), kind="order",
                             client_id=pos.client_id,
                             body={"stopLevel": pos.stop, "profitLevel": pos.target})
        conf = self._confirm(str(resp.get(F_DEAL_REFERENCE, "")), pos.client_id)
        if conf.get(F_DEAL_STATUS) != ACCEPTED:
            raise OrderRejected(f"{pos.symbol} : ajout du stop refusé ({conf.get('reason')})")
        pos.status = "open"

    def _delete(self, pos: CapitalPosition) -> None:
        resp = self._request("DELETE", EP_POSITION.format(deal_id=pos.id), kind="order",
                             client_id=pos.client_id)
        conf = self._confirm(str(resp.get(F_DEAL_REFERENCE, "")), pos.client_id)
        if conf.get(F_DEAL_STATUS) != ACCEPTED:
            pos.status = "inconsistent"
            raise PositionInconsistent(pos.id, f"fermeture refusée ({conf.get('reason')})")
        pos.exit_price = _num(conf.get("level")) or 0.0
        pos.status = "closed"

    def ensure_stop(self, position_id: str) -> StopStatus:
        pos = self._positions[position_id]
        if pos.status == "closed":
            return "closed_by_platform"
        live = next((p for p in self._open_on_platform() if p["deal_id"] == position_id), None)
        if live is None:
            pos.absences += 1
            if pos.absences >= ABSENCES_TO_CLOSE:          # 2 absences consécutives
                pos.status = "closed"
                return "closed_by_platform"
            return "absent_once"
        pos.absences = 0
        if live["stop"] is not None:
            pos.status = "open"
            return "ok"
        pos.status = "unprotected"
        try:
            self._put_stop(pos)
            return "replaced"
        except BrokerError:
            self._delete(pos)                 # en cas d'échec, l'erreur remonte : réessayer
            return "closed_no_stop"

    def close_position(self, position_id: str) -> None:
        pos = self._positions[position_id]
        if pos.status == "closed":
            return
        try:
            self._delete(pos)
        except OrderRejected as exc:
            if exc.http_status != 404:
                raise
            # déjà absente chez Capital.com : fermée seulement après 2 absences constatées
            if self.ensure_stop(position_id) != "closed_by_platform":
                raise BrokerUnavailable(f"{pos.symbol} : fermeture à confirmer au prochain "
                                        "cycle") from None

    def manual_positions(self) -> list[dict]:
        """Positions ouvertes à la main : signalées, jamais reprises (CDC §6)."""
        return [p for p in self._open_on_platform() if p["deal_id"] not in self._positions]

    def adopt_journal_row(self, row: dict) -> None:
        """Reprend une position ouverte du journal après un redémarrage."""
        self._positions[row["id"]] = CapitalPosition(
            id=row["id"], symbol=row["market"], side=row["side"], qty=row["qty"],
            entry=row["entry"], stop=row["stop"], target=row["target"],
            client_id=row["client_id"], status="open")

    def exit_price(self, position_id: str) -> float | None:
        """A6 : prix de sortie réellement exécuté, None si inconnu."""
        pos = self._positions.get(position_id)
        return pos.exit_price if pos and pos.exit_price > 0 else None

    def position(self, position_id: str) -> dict | None:
        pos = self._positions.get(position_id)
        return asdict(pos) if pos else None

    def positions(self) -> list[dict]:
        return [asdict(p) for p in self._positions.values() if p.status != "closed"]
