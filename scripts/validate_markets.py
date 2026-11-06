"""Validation historique (CDC §3) : rejoue chaque couple marché-stratégie sur 12 mois,
frais, spread et financement CFD compris. Verdict « OK » si profit factor ≥ 1,1 avec
au moins 20 trades, sinon « A_REVOIR » (marché à désactiver dans config.yaml)."""

MIN_PROFIT_FACTOR = 1.1
MIN_TRADES = 20


def verdict(profit_factor: float, trades: int) -> str:
    return "OK" if trades >= MIN_TRADES and profit_factor >= MIN_PROFIT_FACTOR else "A_REVOIR"


if __name__ == "__main__":
    raise SystemExit("À implémenter : téléchargement des bougies 1 h puis backtest.")
