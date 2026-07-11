"""Détecteur prime riche / options bon marché : IV implicite vs vol réalisée.

Quand le marché paie les options beaucoup plus cher que la volatilité
réellement observée (IV/RV élevé), vendre de la prime a un avantage
statistique. À l'inverse, une IV sous la vol réalisée rend l'achat
d'options anormalement bon marché.
"""
from ..models import MarketSnapshot, Opportunity
from .common import atm_iv, clamp_score


def detect(snap: MarketSnapshot, cfg: dict) -> list[Opportunity]:
    iv = atm_iv(snap)
    rv = snap.realized_vol_20d
    # IV ATM < 5 % sur une action = flux de données corrompu (pré-ouverture), pas un signal
    if not iv or iv < 0.05 or rv <= 0.02:
        return []
    ratio = iv / rv
    opps = []

    if ratio >= cfg["iv_rv_rich"]:
        score = clamp_score(35 + (ratio - cfg["iv_rv_rich"]) * 60)
        opps.append(Opportunity(
            ticker=snap.ticker,
            kind="prime_riche",
            strategy="Vente de prime : put cash-secured, covered call ou iron condor",
            score=score,
            summary=(f"{snap.ticker} : IV {iv:.0%} vs vol réalisée {rv:.0%} "
                     f"(ratio x{ratio:.2f}) — les options sont chères, avantage au vendeur"),
            details={"iv_atm": round(iv, 4), "rv_20d": round(rv, 4), "ratio": round(ratio, 2)},
        ))
    elif ratio <= cfg["iv_rv_cheap"]:
        score = clamp_score(35 + (cfg["iv_rv_cheap"] - ratio) * 80)
        opps.append(Opportunity(
            ticker=snap.ticker,
            kind="options_bon_marche",
            strategy="Achat d'options pas chères : calls/puts longs ou straddle",
            score=score,
            summary=(f"{snap.ticker} : IV {iv:.0%} sous la vol réalisée {rv:.0%} "
                     f"(ratio x{ratio:.2f}) — le marché sous-estime le mouvement"),
            details={"iv_atm": round(iv, 4), "rv_20d": round(rv, 4), "ratio": round(ratio, 2)},
        ))
    return opps
