# OptiScan — Scanner d'opportunités sur options US

Bot **scanner + alertes** (aucun ordre envoyé, jamais) qui détecte les opportunités
sur les options que peu de gens regardent, connecté soit à **Yahoo Finance**
(gratuit, différé ~15 min) soit à **Interactive Brokers** (temps réel).

## Les 5 détecteurs

| Signal | Ce qu'il détecte | Pourquoi c'est intéressant |
|---|---|---|
| **Flux inhabituel** | Volume du jour ≫ open interest sur un strike précis, avec un flux de prime significatif | Quelqu'un ouvre une grosse position nouvelle — souvent un acteur informé. Signal peu visible du grand public. |
| **Prime riche** | IV implicite ≫ volatilité réalisée (ratio > 1.35) | Les options sont statistiquement trop chères → avantage au vendeur de prime |
| **IV bon marché** | IV < 80 % de la vol réalisée | Le marché sous-estime le mouvement → acheter des options est anormalement bon marché |
| **Skew anormal** | Puts OTM très chers vs calls (peur) ou skew **inversé** (rare, spéculation haussière agressive) | Le skew inversé est un des signaux les moins suivis |
| **Earnings / IV crush** | Résultats < 10 jours + IV gonflée | Setup d'écrasement de volatilité après l'annonce |
| **Put cash-secured** | Strike concret ~delta 0.30 avec rendement annualisé ≥ 15 % | Opportunité prête à trader : strike, prix, prob. de profit, capital requis |

Chaque signal reçoit un **score 0-100** ; le rapport les classe du plus rare au plus banal.

## Utilisation

```bash
# Scan de la watchlist complète (source Yahoo, gratuite)
python -m scanner.main

# Tickers spécifiques
python -m scanner.main --tickers NVDA,TSLA,COIN

# Source Interactive Brokers (temps réel, voir ci-dessous)
python -m scanner.main --source ib
```

Sortie : tableau console + export JSON dans `output/`.

La watchlist et tous les seuils se règlent dans [config.json](config.json).

## Brancher Interactive Brokers (temps réel)

1. **Installer IB Gateway** (léger, recommandé pour un bot) ou TWS :
   https://www.interactivebrokers.com/en/trading/ibgateway-stable.php
2. Se connecter avec ton compte **paper** d'abord (créé automatiquement avec ton compte réel, visible dans Account Management).
3. Activer l'API : `Configure > Settings > API > Settings` →
   cocher **Enable ActiveX and Socket Clients**, décocher *Read-Only API* n'est PAS nécessaire (le bot ne trade pas).
4. Noter le port : **4002** (Gateway paper), **7497** (TWS paper) — à reporter dans `config.json` (`ib_port`).
5. Installer la librairie : `pip install ib_async`
6. Lancer : `python -m scanner.main --source ib`

**Données de marché** : sans abonnement, IBKR fournit des données différées (le bot
bascule automatiquement en mode différé). Pour le temps réel options US, souscrire
**OPRA (US Options Exchanges)** ≈ 1,50 $/mois dans Account Management > Market Data Subscriptions.

## Avertissements

- Outil d'**aide à la décision** uniquement : aucun ordre n'est jamais envoyé.
- Les options comportent un risque de perte élevé ; la vente de puts cash-secured
  engage le capital du strike × 100.
- Données Yahoo différées ~15 min : les prix affichés ne sont pas exécutables tels quels.
- Rien ici n'est un conseil en investissement.
