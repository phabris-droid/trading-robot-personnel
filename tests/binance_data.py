"""Données Binance simulées partagées par les tests (D8) : une seule copie des filtres de
symbole et du carnet, réutilisée par test_binance, test_execution et les tests adverses."""

DEMO = "demo-api.binance.com"
LIVE = "api.binance.com"
EXCHANGE_INFO = {"symbols": [{
    "symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT",
    "filters": [
        {"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "1000000",
         "tickSize": "0.01"},
        {"filterType": "LOT_SIZE", "minQty": "0.00001", "maxQty": "9000",
         "stepSize": "0.00001"},
        {"filterType": "NOTIONAL", "minNotional": "5.00000000"},
    ]}]}
BOOK = {"symbol": "BTCUSDT", "bidPrice": "60000.00", "askPrice": "60001.00"}
