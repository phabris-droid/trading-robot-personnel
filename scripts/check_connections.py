"""Vérification des connexions (jour 1 de la démo) — LECTURE SEULE, aucun ordre.

Avec les clés du fichier .env, vérifie en une minute, API par API :
- Binance : heure du serveur, solde du compte, règles de chaque marché, prix actuel ;
- Capital.com : ouverture de session, solde, devise du compte (USD attendu), chaque marché ;
- Telegram : le bot existe, et un message de test arrive au propriétaire ;
- Claude (Anthropic) : une réponse très courte (coût négligeable).
Aucune clé, aucun mot de passe ni jeton n'est jamais affiché.

    python scripts/check_connections.py          (dans le conteneur :
    docker compose run --rm tradebot python scripts/check_connections.py)

Code de sortie 0 si tout ce qui est configuré fonctionne, 1 sinon.
"""
from __future__ import annotations

import sys
from collections.abc import Callable

import httpx

from tradebot.settings import ConfigError, load_env, load_settings

OK, KO, SKIP = "✅", "❌", "⏭️"


def _safe_error(exc: BaseException) -> str:
    """Nom de l'erreur et code HTTP seulement : jamais un message pouvant contenir une URL
    avec un jeton (Telegram) ou un identifiant."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return f"{type(exc).__name__}" + (f" (HTTP {status})" if status else "")


def check(name: str, fn: Callable[[], str], results: list) -> None:
    try:
        detail = fn()
        results.append((OK, name, detail))
    except Exception as exc:  # noqa: BLE001
        results.append((KO, name, _safe_error(exc)))


def run(settings, env: dict, http: httpx.Client | None = None) -> list[tuple[str, str, str]]:
    results: list[tuple[str, str, str]] = []
    http = http or httpx.Client(timeout=20)

    # --- Binance -----------------------------------------------------------------------------
    if not (env.get("BINANCE_API_KEY") and env.get("BINANCE_API_SECRET")):
        results.append((SKIP, "Binance", "clés absentes du .env (BINANCE_API_KEY / SECRET)"))
    else:
        from tradebot.brokers.binance import BinanceSpot

        bn = BinanceSpot.from_settings(settings, env)
        check("Binance : heure du serveur", lambda: (bn.sync_time(), "synchronisée")[1],
              results)
        check("Binance : solde du compte", lambda: f"capital du robot {bn.equity():.2f}",
              results)
        for m in settings.markets:
            if m.platform == "binance" and m.enabled:
                check(f"Binance : {m.symbol}",
                      lambda s=m.symbol: (f"pas {bn.filters(s).step_size}, prix "
                                          f"{bn.quote(s)[1]:g}"), results)

    # --- Capital.com -------------------------------------------------------------------------
    if not (env.get("CAPITAL_API_KEY") and env.get("CAPITAL_IDENTIFIER")
            and env.get("CAPITAL_PASSWORD")):
        results.append((SKIP, "Capital.com", "identifiants absents du .env"))
    else:
        from tradebot.brokers.capital import CapitalCFD

        cap = CapitalCFD.from_settings(settings, env)
        check("Capital.com : session", lambda: (cap.login(), "ouverte")[1], results)

        def equity() -> str:
            value = cap.equity()
            cur = cap.account_currency or "inconnue"
            if cur != "USD":
                raise ValueError(f"devise du compte {cur} (USD attendu, décision A10)")
            return f"capital {value:.2f} {cur}"
        check("Capital.com : solde et devise", equity, results)
        for m in settings.markets:
            if m.platform == "capital" and m.enabled:
                check(f"Capital.com : {m.symbol}",
                      lambda s=m.symbol: (f"taille min {cap.min_qty(s):g}, prix "
                                          f"{cap.quote(s)[1]:g}, {len(cap.candles(s, 5))} "
                                          "bougies"), results)

    # --- Telegram ----------------------------------------------------------------------------
    token, owner = env.get("TELEGRAM_BOT_TOKEN", ""), env.get("TELEGRAM_OWNER_CHAT_ID", "")
    if not (token and owner):
        results.append((SKIP, "Telegram", "TELEGRAM_BOT_TOKEN / OWNER_CHAT_ID absents"))
    else:
        def telegram() -> str:
            base = f"https://api.telegram.org/bot{token}"
            me = http.get(f"{base}/getMe")
            me.raise_for_status()
            sent = http.post(f"{base}/sendMessage", json={
                "chat_id": owner, "text": "✅ tradebot : test de connexion réussi"})
            sent.raise_for_status()
            return f"bot @{me.json()['result'].get('username', '?')}, message de test envoyé"
        check("Telegram", telegram, results)

    # --- Claude (Anthropic) ------------------------------------------------------------------
    key = env.get("ANTHROPIC_API_KEY", "")
    if not key:
        results.append((SKIP, "Claude (IA)", "ANTHROPIC_API_KEY absente : calculs seuls"))
    else:
        def claude() -> str:
            import anthropic

            client = anthropic.Anthropic(api_key=key, timeout=20)
            reply = client.messages.create(model=settings.analysts.ai.model, max_tokens=5,
                                           messages=[{"role": "user", "content": "Réponds OK"}])
            return f"modèle {settings.analysts.ai.model} : réponse reçue ({len(reply.content)})"
        check("Claude (IA)", claude, results)
    return results


def main(argv: list[str] | None = None) -> int:
    env = load_env()
    try:
        settings = load_settings(env=env)
    except ConfigError as exc:
        print(f"Configuration refusée : {exc}", file=sys.stderr)
        return 2
    print(f"Vérification des connexions — mode {settings.mode.upper()} (lecture seule, "
          "aucun ordre)")
    results = run(settings, env)
    for status, name, detail in results:
        print(f"{status} {name} : {detail}")
    failed = [r for r in results if r[0] == KO]
    missing = [r for r in results if r[0] == SKIP]
    print(f"\n{len(results) - len(failed) - len(missing)} réussi(s), {len(failed)} échec(s), "
          f"{len(missing)} non configuré(s).")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
