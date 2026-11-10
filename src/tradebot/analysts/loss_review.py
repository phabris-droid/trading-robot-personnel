"""Analyse de chaque trade perdant (demande du propriétaire, 2026-11-10).

Pour chaque perte : COMMENT (sortie, durée, perte en dollars et en R — la mise risquée —,
part des frais), POURQUOI (avis des analystes à l'entrée, parcours du prix pendant le trade),
SOURCES (actualités, annonces et événements géopolitiques publiés autour du trade) et
COMMENT L'ÉVITER (pistes classées). Ce ne sont que des PROPOSITIONS : le robot ne change rien
seul (décision du propriétaire). Analyse envoyée sur Telegram, gardée dans le journal et
regroupée chaque dimanche dans l'audit du superviseur.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from urllib.parse import urlsplit

import pandas as pd

STATE_PREFIX = "loss_review:"
SOURCES_BEFORE_S = 2 * 3600            # actualités publiées jusqu'à 2 h avant l'entrée
FALSE_START_R = 0.3                    # moins de 0,3 R de gain atteint : départ manqué
GIVEN_BACK_R = 1.0                     # plus de 1 R atteint puis rendu : gain non protégé


@dataclass
class LossReview:
    position_id: str
    market: str
    strategy: str
    side: str
    opened_ts: float
    closed_ts: float
    close_reason: str
    pnl: float
    fees: float
    r_multiple: float                  # perte en multiples de la mise risquée
    best_r: float                      # meilleur gain atteint pendant le trade (en R)
    duration_h: float
    causes: list[str] = field(default_factory=list)      # codes : frais, faux_depart…
    analysts: list[str] = field(default_factory=list)    # avis à l'entrée
    sources: list[str] = field(default_factory=list)     # « source : titre »
    advice: list[str] = field(default_factory=list)


def _host(url: str) -> str:
    return urlsplit(url).netloc.replace("www.", "") or url


def entry_candle_ts(client_id: str) -> int | None:
    """Les ordres du moteur portent la bougie du signal : « SYMBOLE-<horodatage> »."""
    tail = str(client_id).rsplit("-", 1)[-1]
    return int(tail) if tail.isdigit() else None


def best_excursion_r(candles: pd.DataFrame | None, side: str, entry: float, risk_unit: float,
                     opened_ts: float, closed_ts: float) -> float:
    """Meilleur gain atteint pendant le trade, en R (0 si inconnu)."""
    if candles is None or candles.empty or risk_unit <= 0:
        return 0.0
    during = candles[(candles["close_ts"] > opened_ts) & (candles["ts"] < closed_ts)]
    if during.empty:
        return 0.0
    best = (float(during["high"].max()) - entry if side == "buy"
            else entry - float(during["low"].min()))
    return max(0.0, best / risk_unit)


def news_around(news_cache, opened_ts: float, closed_ts: float) -> list[str]:
    """Actualités (veille et géopolitique) et annonces économiques autour du trade."""
    if news_cache is None:
        return []
    out = []
    items = []
    if hasattr(news_cache, "all_news"):
        items += list(news_cache.all_news())
    for its in getattr(news_cache, "geo", {}).values():
        items += list(its)
    for it in sorted(items, key=lambda x: x.published_ts):
        if opened_ts - SOURCES_BEFORE_S <= it.published_ts <= closed_ts:
            mark = " [fort impact]" if getattr(it, "impact", "") == "high" else ""
            out.append(f"{_host(it.source)} : {it.title[:110]}{mark}")
    for ev in getattr(news_cache, "events", []):
        if opened_ts - SOURCES_BEFORE_S <= ev.ts <= closed_ts and ev.impact in ("High",
                                                                                 "Medium"):
            out.append(f"calendrier économique : {ev.currency} — {ev.title} ({ev.impact})")
    return out[:8]


def review_loss(position: dict, trade: dict, decisions: list[dict], candles, news_cache,
                validation_verdict: str | None, now: float) -> LossReview:
    side, entry, stop = position["side"], float(position["entry"]), float(position["stop"])
    risk_unit = abs(entry - stop)
    risk = risk_unit * float(position["qty"])
    pnl, fees = float(trade["pnl"]), float(trade["fees"])
    opened, closed = float(position["opened_ts"]), float(position.get("closed_ts") or now)
    reason = str(position.get("close_reason") or "inconnue")
    best = best_excursion_r(candles, side, entry, risk_unit, opened, closed)
    rv = LossReview(position_id=position["id"], market=position["market"],
                    strategy=position.get("strategy") or "", side=side, opened_ts=opened,
                    closed_ts=closed, close_reason=reason, pnl=pnl, fees=fees,
                    r_multiple=pnl / risk if risk > 0 else 0.0, best_r=best,
                    duration_h=max(0.0, (closed - opened) / 3600))
    rv.analysts = [f"{d['advisor']} : {d['reason'] or 'accord'}" for d in decisions
                   if not d.get("blocked")]
    rv.sources = news_around(news_cache, opened, closed)

    # --- causes et pistes (propositions seulement) ---------------------------------------
    if pnl + fees > 0:
        rv.causes.append("frais")
        rv.advice.append(f"Le prix est allé dans le bon sens (brut {pnl + fees:+.2f} $) mais les "
                         f"frais ({fees:.2f} $) ont tout effacé : mouvement trop petit pour "
                         "ce marché. Pistes : frais payés en BNB (−25 %), ou marché sans "
                         "commission (Capital.com).")
    if best >= GIVEN_BACK_R:
        rv.causes.append("gain_rendu")
        rv.advice.append(f"Le trade a atteint +{best:.1f} R avant de se retourner : un stop "
                         "suiveur (amélioration P2 du cahier des charges) aurait protégé ce "
                         "gain.")
    elif best < FALSE_START_R and "stop" in reason:
        rv.causes.append("faux_depart")
        rv.advice.append("Le prix n'est jamais parti dans le bon sens (fausse cassure ou "
                         "retournement immédiat). Si cela se répète sur ce marché, la veille de "
                         "référence évaluera une autre stratégie du cahier des charges.")
    if "anticip" in reason:
        rv.causes.append("sortie_anticipee")
        rv.advice.append("La stratégie a coupé tôt (petite perte) : comportement prévu, il "
                         "évite souvent une perte plus grande.")
    strong = [s for s in rv.sources if "[fort impact]" in s or "calendrier" in s]
    if strong:
        rv.causes.append("actualite")
        rv.advice.append("Une actualité ou annonce importante est tombée autour du trade "
                         f"({strong[0][:90]}). Pistes : allonger le blocage après les "
                         "actualités fortes, ou ajouter ce sujet aux mots-clés surveillés.")
    if validation_verdict is not None and validation_verdict != "OK":
        rv.causes.append("marche_non_rentable")
        rv.advice.append(f"{rv.market} n'est pas rentable sur 12 mois (validation : "
                         f"{validation_verdict}) : il n'est pas tradé en mode réel.")
    if not rv.causes:
        rv.causes.append("aleas")
        rv.advice.append("Perte normale de la stratégie (le stop a joué son rôle, perte "
                         "limitée à la mise prévue) : aucune action, à juger sur au moins 20 "
                         "trades.")
    return rv


CAUSE_LABELS = {
    "frais": "frais plus grands que le mouvement",
    "gain_rendu": "gain atteint puis rendu",
    "faux_depart": "faux départ (fausse cassure)",
    "sortie_anticipee": "sortie anticipée de la stratégie",
    "actualite": "actualité ou annonce importante",
    "marche_non_rentable": "marché non rentable sur 12 mois",
    "aleas": "perte normale (stop respecté)",
}


def message(rv: LossReview) -> str:
    lines = [f"🔍 Analyse de la perte {rv.market} ({rv.strategy}, {rv.side}) : "
             f"{rv.pnl:+.2f} $ ({rv.r_multiple:+.2f} R), sortie « {rv.close_reason} » après "
             f"{rv.duration_h:.0f} h, frais {rv.fees:.2f} $, meilleur gain atteint "
             f"+{rv.best_r:.1f} R.",
             "Pourquoi : " + ", ".join(CAUSE_LABELS[c] for c in rv.causes) + "."]
    if rv.sources:
        lines.append("Sources autour du trade : " + " | ".join(rv.sources[:4]))
    else:
        lines.append("Sources autour du trade : aucune actualité ni annonce relevée.")
    lines.append("Comment l'éviter (propositions, rien n'est modifié) : "
                 + " ".join(f"• {a}" for a in rv.advice))
    return "\n".join(lines)


def to_json(rv: LossReview) -> str:
    return json.dumps(asdict(rv), ensure_ascii=False)


def summarize(reviews: list[dict]) -> list[str]:
    """Regroupement hebdomadaire : causes les plus fréquentes et sources récurrentes."""
    if not reviews:
        return ["- aucune perte analysée sur la période"]
    causes = Counter(c for r in reviews for c in r.get("causes", []))
    hosts = Counter(s.split(" : ", 1)[0] for r in reviews for s in r.get("sources", []))
    lines = [f"- {len(reviews)} perte(s) analysée(s), {sum(r['pnl'] for r in reviews):+.2f} $ "
             f"au total, dont {sum(r['fees'] for r in reviews):.2f} $ de frais"]
    lines += [f"- {CAUSE_LABELS.get(c, c)} : {n} fois" for c, n in causes.most_common()]
    if hosts:
        lines.append("- sources les plus présentes autour des pertes : "
                     + ", ".join(f"{h} ({n})" for h, n in hosts.most_common(5)))
    return lines
