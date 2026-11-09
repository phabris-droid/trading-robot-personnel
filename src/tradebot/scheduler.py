"""Tâches planifiées (CDC §7, §8) — APScheduler 3, fuseau UTC :
- rapport du matin (reports.morning_utc, 07:00) : message de vie, analyse, agenda ;
- audit hebdomadaire (reports.audit_day/audit_utc, dimanche 18:00) dans data/audits/ ;
- chien de garde (chaque minute) : alerte si aucun cycle du moteur depuis 5 min ;
- annonces majeures 1 h avant (toutes les 5 min, une seule alerte par annonce) ;
- ping Healthchecks.io facultatif (HEALTHCHECKS_URL), seulement si le moteur est vivant.
Chaque tâche est une fonction testable ; une tâche qui échoue n'arrête pas les autres.
"""
from __future__ import annotations

import sys
from collections.abc import Callable

import httpx

from tradebot.interfaces import reports

ANNOUNCE_BEFORE_S = 3600


class Jobs:
    def __init__(self, engine, notify: Callable[[str], None], news=None, ai=None,
                 healthchecks_url: str = "", http: httpx.Client | None = None):
        self.engine = engine
        self.notify = notify
        self.news, self.ai = news, ai
        self.healthchecks_url = healthchecks_url
        self._http = http or httpx.Client(timeout=10)
        self._announced: set[tuple[str, float]] = set()
        self._silent_alerted = False

    def _safe(self, name: str, fn) -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            print(f"[ERROR] tâche {name} : {type(exc).__name__}: {exc}", file=sys.stderr)

    def morning(self) -> None:
        self._safe("rapport du matin",
                   lambda: self.notify(reports.morning_report(self.engine, self.news, self.ai)))

    def weekly_audit(self) -> None:
        def run():
            summary, _path = reports.weekly_audit(self.engine.journal, self.engine.now(),
                                                  self.engine.settings.storage.audits_dir)
            self.notify(summary)
        self._safe("audit", run)

    def watchdog(self) -> None:
        """Alerte si le moteur n'a pas fait de cycle depuis watchdog_minutes (une fois)."""
        last = self.engine.last_cycle_ts
        limit = self.engine.settings.engine.watchdog_minutes * 60
        silent = last is not None and self.engine.now() - last > limit
        if silent and not self._silent_alerted:
            self.notify(f"🚨 Robot muet : aucun cycle depuis "
                        f"{int((self.engine.now() - last) // 60)} min")
        elif not silent and self._silent_alerted:
            self.notify("✅ Robot de nouveau actif")
        self._silent_alerted = silent

    def announcements(self) -> None:
        if self.news is None:
            return
        now = self.engine.now()
        impacts = self.engine.settings.analysts.news_watch.announcement_impacts
        for ev in self.news.events:
            key = (ev.title, ev.ts)
            if ev.impact in impacts and 0 < ev.ts - now <= ANNOUNCE_BEFORE_S \
                    and key not in self._announced:
                self._announced.add(key)
                self.notify(f"📅 Annonce majeure dans {int((ev.ts - now) // 60)} min : "
                            f"{ev.currency} — {ev.title} (entrées bloquées ±30 min)")

    def healthcheck(self) -> None:
        last = self.engine.last_cycle_ts
        limit = self.engine.settings.engine.watchdog_minutes * 60
        if not self.healthchecks_url or last is None or self.engine.now() - last > limit:
            return
        self._safe("healthchecks", lambda: self._http.get(self.healthchecks_url))


def build_scheduler(jobs: Jobs, settings):
    """Planificateur en arrière-plan (fuseau UTC). Renvoie le BackgroundScheduler non démarré."""
    from apscheduler.schedulers.background import BackgroundScheduler

    sched = BackgroundScheduler(timezone="UTC")
    mh, mm = settings.reports.morning_utc.split(":")
    ah, am = settings.reports.audit_utc.split(":")
    sched.add_job(jobs.morning, "cron", hour=int(mh), minute=int(mm), id="rapport_matin")
    sched.add_job(jobs.weekly_audit, "cron", day_of_week=settings.reports.audit_day,
                  hour=int(ah), minute=int(am), id="audit_hebdomadaire")
    sched.add_job(jobs.watchdog, "interval", minutes=1, id="chien_de_garde")
    sched.add_job(jobs.announcements, "interval", minutes=5, id="annonces")
    sched.add_job(jobs.healthcheck, "interval", minutes=5, id="healthchecks")
    return sched
