"""Exécution (CDC §6) : aucune position sans stop posé chez la plateforme — si impossible,
fermeture immédiate. Stop logiciel en plus sur bid/ask. Ordre incertain : jamais renvoyé,
recherché à chaque cycle, entrées bloquées sur ce marché, pause + alerte après 30 min.
Arrêt d'urgence : réessaie à chaque cycle jusqu'à tout fermer, alerte toutes les 5 tentatives."""
