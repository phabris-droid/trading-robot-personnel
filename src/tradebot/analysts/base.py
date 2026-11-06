"""Chaîne de conseillers (CDC §5) : 1 refus suffit, le premier refus arrête la chaîne.
Aucun conseiller ne peut ouvrir un trade, modifier un stop ou bloquer une sortie."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Verdict:
    advisor: str
    blocked: bool
    reason: str = ""


class Advisor:
    name = "advisor"

    def review(self, context: dict) -> Verdict:
        raise NotImplementedError


def run_chain(advisors: list[Advisor], context: dict) -> list[Verdict]:
    verdicts = []
    for adv in advisors:
        v = adv.review(context)
        verdicts.append(v)
        if v.blocked:
            break
    return verdicts
