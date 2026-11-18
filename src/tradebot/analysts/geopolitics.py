"""Veille géopolitique (demande du propriétaire, 2026-11-10) : communiqués officiels
(Maison-Blanche, Pentagone, Kremlin via TASS) et grandes chaînes d'information (NBC/CNBC, CBS,
ABC, Fox, BBC, Al Jazeera, France 24).

Un ÉVÉNEMENT MAJEUR = un mot-clé à fort impact (droits de douane, sanctions, embargo, blocus,
invasion, déclaration de guerre, loi martiale, frappe militaire, mobilisation…) repris par au
moins N sources distinctes dans une fenêtre de M minutes, une source officielle comptant
double. Les nouvelles de fond (un seul média, mot isolé) ne bloquent rien : sinon le robot
serait bloqué en permanence.

Effet : nouvelles entrées bloquées pendant `block_hours` (4 h par défaut) sur tous les
marchés. Comme les autres conseillers, la veille ne peut que BLOQUER : elle n'ouvre, ne ferme
et ne modifie jamais rien. Sources en panne trop longtemps : erreur (bloquante en réel).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from tradebot.analysts.base import Advisor, AdvisorContext, Verdict

STALE_REFRESHES = 3
SHORT_KEYWORD = 4                   # mot-clé court (« coup ») : mot entier exigé


@dataclass(frozen=True)
class GeoEvent:
    keyword: str
    ts: float                       # moment où la confirmation est atteinte
    sources: tuple[str, ...]
    title: str                      # premier titre officiel, sinon le premier titre


def _pattern(keyword: str) -> re.Pattern:
    """Début de mot exigé (« mobiliz » ne trouve pas « immobilized ») ; mot entier pour les
    mots-clés courts (« coup » ne trouve pas « coupon ») ; sinon préfixe (« sanction » couvre
    « sanctions »)."""
    k = re.escape(keyword.lower())
    end = r"(?!\w)" if len(keyword) <= SHORT_KEYWORD else ""
    return re.compile(rf"(?<!\w){k}{end}")


def matched_keywords(title: str, keywords: list[str]) -> list[str]:
    low = title.lower()
    return [k for k in keywords if _pattern(k).search(low)]


def matched_keyword(title: str, keywords: list[str]) -> str | None:
    found = matched_keywords(title, keywords)
    return found[0] if found else None


def organisation(url: str) -> str:
    """Une organisation = un nom de site : les deux flux de la Maison-Blanche comptent pour
    une seule source."""
    host = urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def detect_events(items: list, official: set[str], keywords: list[str], confirm: int,
                  window_s: float) -> list[GeoEvent]:
    """items : NewsItem (title, published_ts, source). Pour chaque mot-clé, DERNIER instant où
    le poids des organisations distinctes de la fenêtre atteint `confirm` (organisation
    officielle = 2). Un titre compte pour chacun des mots-clés qu'il contient. Seuls les
    mots-clés comptent : l'IA ne peut pas, seule, regrouper des sujets différents."""
    official_orgs = {organisation(u) for u in official}
    by_kw: dict[str, list] = {}
    for it in items:
        for kw in matched_keywords(it.title, keywords):
            by_kw.setdefault(kw, []).append(it)
    events: list[GeoEvent] = []
    for kw, hits in by_kw.items():
        hits.sort(key=lambda x: x.published_ts)
        latest: GeoEvent | None = None
        for i, last in enumerate(hits):
            window = [h for h in hits[:i + 1] if last.published_ts - h.published_ts <= window_s]
            orgs = {organisation(h.source) for h in window}
            weight = sum(2 if o in official_orgs else 1 for o in orgs)
            if weight >= confirm:
                first = next((h for h in window if organisation(h.source) in official_orgs),
                             window[0])
                latest = GeoEvent(kw, last.published_ts, tuple(sorted(orgs)), first.title[:200])
        if latest is not None:
            events.append(latest)               # la confirmation la plus récente compte
    return sorted(events, key=lambda e: e.ts)


class GeoWatch(Advisor):
    name = "géopolitique"

    def __init__(self, cfg, cache=None):
        self.cfg = cfg
        self.cache = cache

    def events(self, now: float) -> list[GeoEvent]:
        if self.cache is None:
            return []
        items = [it for its in getattr(self.cache, "geo", {}).values() for it in its]
        return detect_events(items, set(self.cfg.official_feeds), list(self.cfg.keywords),
                             self.cfg.confirm_sources, self.cfg.confirm_window_minutes * 60)

    def review(self, ctx: AdvisorContext) -> Verdict:
        if not self.cfg.enabled or self.cache is None:
            return Verdict(self.name, False)
        stale = getattr(self.cache, "geo_stale_for", None)
        if stale is not None and stale(ctx.now) > 0:
            raise RuntimeError("veille géopolitique non relue depuis trop longtemps "
                               "(sources en panne)")
        horizon = self.cfg.block_hours * 3600
        for ev in reversed(self.events(ctx.now)):
            if 0 <= ctx.now - ev.ts <= horizon:
                return Verdict(self.name, True,
                               f"événement géopolitique « {ev.keyword} » confirmé par "
                               f"{len(ev.sources)} sources : « {ev.title[:120]} » — entrées "
                               f"bloquées {self.cfg.block_hours:g} h")
        return Verdict(self.name, False)
