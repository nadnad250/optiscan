"""Fonctions partagées entre détecteurs."""
import pandas as pd

from ..models import MarketSnapshot

MIN_VALID_IV = 0.01
MAX_VALID_IV = 5.0


def valid_iv(df: pd.DataFrame) -> pd.DataFrame:
    """Filtre les lignes avec une IV exploitable."""
    if "impliedVolatility" not in df.columns:
        return df.iloc[0:0]
    return df[(df["impliedVolatility"] > MIN_VALID_IV) & (df["impliedVolatility"] < MAX_VALID_IV)]


def nearest_chain(snap: MarketSnapshot, target_dte: int = 30):
    """Retourne (expiry, dte, calls, puts) le plus proche du DTE cible."""
    return min(snap.chains, key=lambda c: abs(c[1] - target_dte))


def iv_at_strike(df: pd.DataFrame, strike: float) -> float | None:
    """IV du contrat dont le strike est le plus proche du strike demandé."""
    df = valid_iv(df)
    if df.empty:
        return None
    row = df.iloc[(df["strike"] - strike).abs().argsort().iloc[0]]
    return float(row["impliedVolatility"])


def atm_iv(snap: MarketSnapshot) -> float | None:
    """IV à la monnaie (moyenne call/put) sur l'échéance ~30 jours."""
    _, _, calls, puts = nearest_chain(snap)
    ivs = [iv for iv in (iv_at_strike(calls, snap.spot), iv_at_strike(puts, snap.spot)) if iv]
    return sum(ivs) / len(ivs) if ivs else None


def clamp_score(x: float) -> float:
    return max(0.0, min(100.0, x))
