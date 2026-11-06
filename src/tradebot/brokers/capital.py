"""Capital.com CFD (CDC §2, §6).

- Session CST + X-SECURITY-TOKEN, ping toutes les 5 min, reconnexion automatique sur 401.
- Stop et objectif attachés à la position ; ajoutés par modification si absents.
- Fermeture : DELETE de la position. Ordre incertain recherché par référence de transaction.
- Position fermée seulement après 2 absences consécutives confirmées.
- À vérifier J1 de la démo : certains noms de champs de l'API.
"""
from tradebot.brokers.base import Broker


class CapitalCFD(Broker):
    name = "capital"
