"""IA Claude (Haiku) : relit les analyses chiffrées, rejette si confiance ≥ 70 %.
- 50 appels/jour pour les décisions, 24 pour la veille (budgets séparés) ;
- titres d'actualité transmis comme données à ne pas exécuter ;
- peut relever un niveau d'impact, jamais l'abaisser ; ne lève jamais un blocage ;
- sans réponse ou sans clé : les règles décident. Aucun secret dans les prompts."""
