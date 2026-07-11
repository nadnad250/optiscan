"""Détecteur earnings : résultats imminents + IV gonflée = setup IV crush."""
from ..models import MarketSnapshot, Opportunity
from .common import atm_iv, clamp_score


def detect(snap: MarketSnapshot, cfg: dict) -> list[Opportunity]:
    days = snap.earnings_days
    if days is None or days > cfg["earnings_max_days"]:
        return []
    iv = atm_iv(snap)
    rv = snap.realized_vol_20d
    if not iv:
        return []

    inflation = iv / rv if rv > 0.02 else 1.0
    score = clamp_score(40 + (cfg["earnings_max_days"] - days) * 4 + max(0, inflation - 1) * 25)
    return [Opportunity(
        ticker=snap.ticker,
        kind="earnings_iv",
        strategy=("Jouer l'IV crush : iron condor / strangle vendu APRÈS l'annonce, "
                  "ou calendar spread avant — éviter l'achat sec de calls/puts"),
        score=score,
        summary=(f"{snap.ticker} : résultats dans {days} j, IV gonflée à {iv:.0%}"
                 + (f" (x{inflation:.2f} vs réalisée)" if rv > 0.02 else "")
                 + " — l'IV va s'écraser après l'annonce"),
        details={"earnings_days": days, "iv_atm": round(iv, 4),
                 "rv_20d": round(rv, 4), "iv_inflation": round(inflation, 2)},
    )]
