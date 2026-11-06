"""Binance spot (CDC §2, §6).

- REST signée HMAC-SHA256, synchronisation d'horloge (GET /api/v3/time, recvWindow).
- Démo : demo-api.binance.com pour les ordres ; prix toujours depuis api.binance.com.
- Achat uniquement ; OCO (stop-limit à 0,5 % sous le stop + objectif) posé juste après l'achat.
- Fermeture : annuler l'OCO puis vendre la quantité libre.
- À vérifier J1 de la démo : recherche d'un OCO par identifiant client, passage réel du stop OCO.
"""
from tradebot.brokers.base import Broker


class BinanceSpot(Broker):
    name = "binance"
