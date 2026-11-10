"""Équipe d'analyse (CDC §5) : veille marché, veille géopolitique (2026-11-10), analyste
financier, analyste des risques, IA Claude — dans cet ordre, le premier refus arrête la chaîne."""
from __future__ import annotations

import time
from collections.abc import Callable

from tradebot.analysts.base import Advisor
from tradebot.analysts.claude_ai import ClaudeAdvisor
from tradebot.analysts.geopolitics import GeoWatch
from tradebot.analysts.financial import FinancialAnalyst
from tradebot.analysts.news_watch import NewsCache, NewsWatch
from tradebot.analysts.risk_analyst import RiskAnalyst


def build_analysts(settings, env: dict, clock: Callable[[], float] = time.time, http=None,
                   ai_client=None, journal=None) -> tuple[list[Advisor], NewsCache]:
    a = settings.analysts
    ai = ClaudeAdvisor(a.ai, env.get("ANTHROPIC_API_KEY", ""), client=ai_client, clock=clock,
                      journal=journal)   # A3 : budgets IA conservés au redémarrage
    cache = NewsCache(a.news_watch, client=http, clock=clock, ai=ai if ai.enabled else None,
                      geo=a.geopolitics)
    chain: list[Advisor] = [
        NewsWatch(a.news_watch, cache, settings.strategies.atr_period),
        GeoWatch(a.geopolitics, cache),          # veille géopolitique (2026-11-10)
        FinancialAnalyst(a.financial, settings.strategies.atr_period),
        RiskAnalyst(a.risk),
        ai,
    ]
    return chain, cache
