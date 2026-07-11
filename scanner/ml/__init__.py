"""Machine learning : détection de la probabilité d'un grand mouvement imminent.

Principe scientifique (honnête) : la DIRECTION d'un grand mouvement est
quasi imprévisible, mais son OCCURRENCE l'est partiellement — la volatilité
se regroupe en grappes et la compression précède souvent l'explosion
(fait empirique documenté depuis Mandelbrot 1963 / Engle 1982).

L'edge exploité : quand le modèle estime P(grand mouvement sous 5 jours)
bien au-dessus de ce que les options font payer, acheter le mouvement
(straddle / call / put) est statistiquement bon marché.
"""
