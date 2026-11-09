"""Journalisation technique en JSON (structlog, pile imposée par CLAUDE.md) : une ligne JSON par
événement sur la sortie d'erreur, horodatage UTC ISO, niveau, message en français.

    {"event": "BTCUSDT : stop reposé", "level": "warning", "timestamp": "2026-11-09T10:00:00Z"}

Lisible par `docker logs` comme par un outil d'agrégation. Les messages ne contiennent jamais
de secret (CLAUDE.md) : seuls des textes déjà nettoyés et des noms d'exceptions y passent.
"""
from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

LEVELS = ("debug", "info", "warning", "error", "critical")
_PRE_CHAIN: list[Any] = [
    structlog.processors.add_log_level,
    structlog.processors.TimeStamper(fmt="iso", utc=True),
]
_state = {"configured": False}


def _configure() -> None:
    if _state["configured"]:
        return
    structlog.configure(
        processors=[*_PRE_CHAIN, structlog.processors.JSONRenderer(ensure_ascii=False)],
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
        # sys.stderr relu à chaque appel (redirections, tests)
        logger_factory=lambda *_a: structlog.PrintLogger(sys.stderr),
        cache_logger_on_first_use=False,
    )
    _state["configured"] = True


def log(level: str, message: str, **fields: Any) -> None:
    """Écrit une ligne JSON. Niveau inconnu -> « error ». Ne lève jamais."""
    _configure()
    method = level if level in LEVELS else "error"
    try:
        getattr(structlog.get_logger("tradebot"), method)(message, **fields)
    except Exception:  # noqa: BLE001 - une ligne de log ne doit jamais casser le robot
        return


def configure_stdlib() -> None:
    """Les modules qui passent par `logging` (journal, bibliothèques) écrivent aussi en JSON."""
    handler = logging.StreamHandler()
    handler.setFormatter(structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(ensure_ascii=False),
        foreign_pre_chain=_PRE_CHAIN))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.WARNING)
