"""Pricing Black-Scholes complet et probabilités réelles (sans scipy).

Deux volatilités jouent des rôles différents :
  - l'IV du marché sert à lire ce que le marché FACTURE ;
  - la "vol estimée" (mélange vol réalisée / IV) sert à estimer ce qui va
    VRAIMENT se passer. L'écart entre les deux est la source de l'edge.
"""
import math

RISK_FREE = 0.045


def ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def npdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1_d2(spot: float, strike: float, sigma: float, t: float):
    if min(spot, strike, sigma, t) <= 0:
        return None
    v = sigma * math.sqrt(t)
    d1 = (math.log(spot / strike) + (RISK_FREE + 0.5 * sigma * sigma) * t) / v
    return d1, d1 - v


def put_price(spot: float, strike: float, sigma: float, t: float) -> float:
    d = _d1_d2(spot, strike, sigma, t)
    if d is None:
        return max(strike - spot, 0.0)
    d1, d2 = d
    return strike * math.exp(-RISK_FREE * t) * ncdf(-d2) - spot * ncdf(-d1)


def call_price(spot: float, strike: float, sigma: float, t: float) -> float:
    d = _d1_d2(spot, strike, sigma, t)
    if d is None:
        return max(spot - strike, 0.0)
    d1, d2 = d
    return spot * ncdf(d1) - strike * math.exp(-RISK_FREE * t) * ncdf(d2)


def put_delta(spot: float, strike: float, sigma: float, t: float) -> float:
    d = _d1_d2(spot, strike, sigma, t)
    return ncdf(d[0]) - 1.0 if d else 0.0


def call_delta(spot: float, strike: float, sigma: float, t: float) -> float:
    d = _d1_d2(spot, strike, sigma, t)
    return ncdf(d[0]) if d else 0.0


def gamma(spot: float, strike: float, sigma: float, t: float) -> float:
    """Sensibilité du delta au prix (identique call/put)."""
    d = _d1_d2(spot, strike, sigma, t)
    if d is None:
        return 0.0
    return npdf(d[0]) / (spot * sigma * math.sqrt(t))


def vega(spot: float, strike: float, sigma: float, t: float) -> float:
    """Variation du prix pour +1 point de volatilité (en $/action)."""
    d = _d1_d2(spot, strike, sigma, t)
    if d is None:
        return 0.0
    return spot * npdf(d[0]) * math.sqrt(t) / 100


def theta_call(spot: float, strike: float, sigma: float, t: float) -> float:
    """Perte de valeur PAR JOUR d'un call détenu (négatif)."""
    d = _d1_d2(spot, strike, sigma, t)
    if d is None:
        return 0.0
    d1, d2 = d
    term = -spot * npdf(d1) * sigma / (2 * math.sqrt(t))
    return (term - RISK_FREE * strike * math.exp(-RISK_FREE * t) * ncdf(d2)) / 365


def theta_put(spot: float, strike: float, sigma: float, t: float) -> float:
    """Perte de valeur PAR JOUR d'un put détenu (négatif en général)."""
    d = _d1_d2(spot, strike, sigma, t)
    if d is None:
        return 0.0
    d1, d2 = d
    term = -spot * npdf(d1) * sigma / (2 * math.sqrt(t))
    return (term + RISK_FREE * strike * math.exp(-RISK_FREE * t) * ncdf(-d2)) / 365


def prob_below(spot: float, level: float, sigma: float, t: float) -> float:
    """P(S_T < level) sous une lognormale sans dérive (monde réel prudent)."""
    if min(spot, level, sigma, t) <= 0:
        return 1.0 if spot < level else 0.0
    return ncdf((math.log(level / spot) + 0.5 * sigma * sigma * t)
                / (sigma * math.sqrt(t)))


def prob_above(spot: float, level: float, sigma: float, t: float) -> float:
    return 1.0 - prob_below(spot, level, sigma, t)


def expected_move(spot: float, sigma: float, t: float) -> float:
    """Mouvement attendu à 1 écart-type (en $)."""
    return spot * sigma * math.sqrt(max(t, 0.0))
