"""Telegram (CDC §7, §8) — commandes acceptées UNIQUEMENT depuis TELEGRAM_OWNER_CHAT_ID :
/statut /positions /trades /pnl /vie /rapport /news /agenda /audit /pret /pause /reprendre
/urgence puis /confirmer (60 s) /aide.

Notifications automatiques (via `Notifier.send`) : ouvertures et fermetures, blocages par un
analyste, annonces majeures 1 h avant, disjoncteurs, erreurs, robot muet depuis 5 min.
Messages trop longs découpés (limite Telegram 4096 caractères). Jamais de secret envoyé.

La logique des commandes (`Commands`) est pure et testable ; `TelegramService` n'est que le
transport (python-telegram-bot v21, dans un fil dédié avec sa propre boucle asyncio).
"""
from __future__ import annotations

import asyncio
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from tradebot.interfaces import reports

TELEGRAM_LIMIT = 4096
CONFIRM_WINDOW_S = 60

HELP = ("/statut /positions /trades /pnl /vie /rapport /news /agenda /audit /pret "
        "/pause /reprendre /urgence (puis /confirmer sous 60 s)")


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Découpe aux fins de ligne si possible ; jamais de morceau vide ni trop long."""
    text = text or "(vide)"
    parts: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > 0 else limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    parts.append(text)
    return [p for p in parts if p]


class Commands:
    def __init__(self, engine, owner_chat_id: str | int, news=None, ai=None,
                 audits_dir: str = "data/audits", validation_path: str = "data/validation.json",
                 clock: Callable[[], float] = time.time):
        self.engine = engine
        self.owner = str(owner_chat_id)
        self.news, self.ai = news, ai
        self.audits_dir, self.validation_path = audits_dir, validation_path
        self._clock = clock
        self._urgence_requested_at: float | None = None

    def handle(self, chat_id: str | int, text: str) -> list[str]:
        """Réponses à envoyer. Toute commande d'un autre chat est ignorée (aucune réponse)."""
        if not self.owner or str(chat_id) != self.owner:
            return []
        cmd = (text or "").strip().split()[0].split("@")[0].lower() if text.strip() else ""
        e = self.engine
        now = self._clock()
        if cmd == "/statut":
            reply = (f"Mode {e.settings.mode.upper()} — {e.status()}\n"
                     f"Plateformes connectées : {', '.join(sorted(e.brokers)) or 'aucune'}\n"
                     f"Positions ouvertes : {len(e.open_positions())}")
        elif cmd == "/positions":
            reply = reports.positions_message(e)
        elif cmd == "/trades":
            reply = reports.trades_message(e.journal)
        elif cmd == "/pnl":
            p = reports.pnl_summary(e.journal, now)
            rate = "—" if p["reussite_pct"] is None else f"{p['reussite_pct']} %"
            reply = (f"P&L jour : {p['jour']:+.2f}\nP&L 7 jours : {p['sept_jours']:+.2f} "
                     f"({p['trades_7j']} trades, réussite {rate})")
        elif cmd == "/vie":
            reply = reports.life_message(e)
        elif cmd == "/rapport":
            reply = reports.morning_report(e, self.news, self.ai)
        elif cmd == "/news":
            reply = reports.news_message(self.news, now)
        elif cmd == "/agenda":
            reply = reports.agenda_message(self.news, now)
        elif cmd == "/audit":
            reply = reports.last_audit(self.audits_dir)
        elif cmd == "/pret":
            reply = reports.readiness_message(
                reports.readiness(e.journal, e.settings, now, self.validation_path))
        elif cmd == "/pause":
            e.pause_entries("commande /pause")
            reply = "Nouvelles entrées en pause. Les positions ouvertes restent gérées."
        elif cmd == "/reprendre":
            e.resume_entries()
            reply = ("Reprise refusée : arrêt d'urgence en cours." if e.control["emergency"]
                     else "Entrées reprises.")
        elif cmd == "/urgence":
            self._urgence_requested_at = now
            reply = ("⚠️ Fermer TOUTES les positions ? Envoyez /confirmer dans les "
                     f"{CONFIRM_WINDOW_S} secondes.")
        elif cmd == "/confirmer":
            asked = self._urgence_requested_at
            self._urgence_requested_at = None
            if asked is None or now - asked > CONFIRM_WINDOW_S:
                reply = "Aucun arrêt d'urgence à confirmer (ou délai de 60 s dépassé)."
            else:
                e.request_emergency_stop("Telegram /urgence + /confirmer")
                reply = "ARRÊT D'URGENCE lancé : fermeture de toutes les positions."
        else:
            reply = f"Commandes : {HELP}"
        return split_message(reply)


class TelegramService:
    """Transport python-telegram-bot v21 : polling dans un fil dédié ; `send` est utilisable
    depuis n'importe quel fil (moteur, tâches planifiées). Sans jeton : désactivé."""

    def __init__(self, token: str, owner_chat_id: str | int, commands: Commands | None = None,
                 bot=None):
        self.enabled = bool(token and owner_chat_id)
        self._token = token
        self.owner = str(owner_chat_id)
        self.commands = commands
        self._bot: Any = bot
        self._app: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()

    def __repr__(self) -> str:       # jamais de jeton dans les journaux
        return f"TelegramService(enabled={self.enabled})"

    def start(self) -> None:
        if not self.enabled:
            return
        threading.Thread(target=self._run, name="telegram", daemon=True).start()
        self._ready.wait(timeout=30)

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            if self._bot is None:
                self._loop.run_until_complete(self._start_polling())
        except Exception as exc:  # noqa: BLE001
            print(f"[ERROR] Telegram indisponible : {type(exc).__name__}", file=sys.stderr)
        finally:
            self._ready.set()
        self._loop.run_forever()

    async def _start_polling(self) -> None:
        from telegram.ext import Application, MessageHandler, filters

        app = Application.builder().token(self._token).build()
        app.add_handler(MessageHandler(filters.Chat(chat_id=int(self.owner)) & filters.TEXT,
                                       self._on_message))
        await app.initialize()
        await app.start()
        if app.updater is None:
            raise RuntimeError("Telegram : aucun updater pour la réception des commandes")
        await app.updater.start_polling(drop_pending_updates=True)
        self._app, self._bot = app, app.bot

    async def _on_message(self, update, context) -> None:
        if self.commands is None or update.effective_message is None:
            return
        replies = await asyncio.to_thread(self.commands.handle, update.effective_chat.id,
                                          update.effective_message.text or "")
        for reply in replies:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=reply)

    def send(self, text: str) -> None:
        """Notification au propriétaire (découpée si trop longue). Ne lève jamais."""
        if not self.enabled or self._loop is None or self._bot is None:
            return
        for part in split_message(text):
            try:
                asyncio.run_coroutine_threadsafe(
                    self._bot.send_message(chat_id=self.owner, text=part), self._loop)
            except Exception as exc:  # noqa: BLE001
                print(f"[ERROR] envoi Telegram impossible : {type(exc).__name__}",
                      file=sys.stderr)


def notification_filter(level: str, message: str) -> bool:
    """Ce qui part sur Telegram (CDC §7) : tout sauf le bruit purement informatif de
    connexion ; ouvertures, fermetures, blocages, disjoncteurs, erreurs passent."""
    return not (level == "info" and "connecté" in message)
