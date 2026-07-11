"""Black-Scholes minimal (sans scipy) : delta et prob. ITM approximative."""
import math


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def d1(spot: float, strike: float, iv: float, t_years: float, r: float = 0.045) -> float:
    if spot <= 0 or strike <= 0 or iv <= 0 or t_years <= 0:
        return 0.0
    return (math.log(spot / strike) + (r + 0.5 * iv * iv) * t_years) / (iv * math.sqrt(t_years))


def call_delta(spot: float, strike: float, iv: float, t_years: float) -> float:
    return norm_cdf(d1(spot, strike, iv, t_years))


def put_delta(spot: float, strike: float, iv: float, t_years: float) -> float:
    return call_delta(spot, strike, iv, t_years) - 1.0


def prob_otm_put(spot: float, strike: float, iv: float, t_years: float) -> float:
    """Probabilité approx. qu'un put expire sans valeur (pour vendeur de prime)."""
    d = d1(spot, strike, iv, t_years) - iv * math.sqrt(max(t_years, 1e-9))
    return norm_cdf(d)
