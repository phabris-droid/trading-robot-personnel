# Décisions A2 à A11 (document « Décisions_A2-A11_tradebot », 6 novembre 2026)

Toutes les recommandations sont **acceptées** par le propriétaire. Les trois qui bloquaient les
corrections C1/C2 (A2, A3, A4) ont été confirmées une seconde fois le 2026-11-09.

| # | Décision acceptée | Où dans le code | Test |
|---|---|---|---|
| A2 | 1 % de risque calculé sur le capital de **la plateforme** qui passe l'ordre ; les devises ne sont jamais additionnées pour le risque | `engine.py` `_size` (`self.equity[platform]`), `risk/sizing.py` | `test_engine_integration.py::test_decision_a2_…`, `test_risk.py` |
| A3 | Perte du jour 3 % et baisse 15 % **par plateforme** : un arrêt de sécurité Binance ne ferme rien chez Capital.com | `risk/breakers.py` (état `breakers:<plateforme>`), `engine.py` `_safety_close` | `test_engine_integration.py::test_decision_a3_…`, `test_breakers.py` |
| A4 | L'or compte dans les **2 positions forex** et dans la **fermeture du week-end** (vendredi 20 h – dimanche 22 h 15 UTC) | `risk/breakers.py` `FOREX_PLATFORMS`, `analysts/risk_analyst.py` | `test_engine_integration.py::test_decision_a4_…`, `test_analysts.py` |
| A5 | Groupes liés : crypto (BTC, ETH, SOL) et dollar (EURUSD, GBPUSD, GOLD), 2 positions au plus par groupe | `config.yaml` `correlation_groups` | `test_analysts.py` |
| A6 | Tendance de fond en 4 h, recalculée depuis les bougies 1 h | `analysts/financial.py` | `test_analysts.py` |
| A7 | Sortie anticipée en vente du retour à la moyenne : RSI < 45 ou retour sous la moyenne | `strategies/mean_reversion.py` | `test_strategies_no_lookahead.py` |
| A8 | 10 trades par jour = ouvertures, jour UTC | `risk/breakers.py`, `journal.count_opened_since` | `test_breakers.py` |
| A9 | Calendrier : flux JSON public hebdomadaire, 2 Mo / 20 s, en cache | `analysts/news_watch.py` | `test_analysts.py` |
| A10 | Compte Capital.com hors USD : alerte, marchés Capital.com en pause en mode réel | `engine.py` `_check_account_currency` | `test_engine_integration.py::test_capital_account_not_in_usd_…` |
| A11 | Risque ouvert > 5 % = somme (distance au stop × taille) / capital de la plateforme | `analysts/risk_analyst.py` | `test_analysts.py` |
