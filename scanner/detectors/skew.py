"""Détecteur de skew anormal : IV des puts OTM vs calls OTM (~5% du spot).

Un skew très élevé = le marché paie très cher la protection (peur) ;
un skew inversé (calls plus chers que les puts) est rare et signale
une spéculation haussière agressive — signal peu suivi par le grand public.
"""
from ..models import MarketSnapshot, Opportunity
from .common import clamp_score, iv_at_strike, nearest_chain

OTM_DISTANCE = 0.05  # 5 % hors de la monnaie de chaque côté


def detect(snap: MarketSnapshot, cfg: dict) -> list[Opportunity]:
    expiry, dte, calls, puts = nearest_chain(snap)
    put_iv = iv_at_strike(puts, snap.spot * (1 - OTM_DISTANCE))
    call_iv = iv_at_strike(calls, snap.spot * (1 + OTM_DISTANCE))
    if put_iv is None or call_iv is None:
        return []

    skew_pts = (put_iv - call_iv) * 100  # en points d'IV
    opps = []
    if skew_pts >= cfg["skew_extreme"]:
        score = clamp_score(30 + (skew_pts - cfg["skew_extreme"]) * 2.5)
        opps.append(Opportunity(
            ticker=snap.ticker,
            kind="skew_peur",
            strategy="Vendre des put spreads (la protection est surpayée) ou prudence sur le titre",
            score=score,
            summary=(f"{snap.ticker} : skew extrême, puts -5% à {put_iv:.0%} vs calls +5% à "
                     f"{call_iv:.0%} ({skew_pts:+.1f} pts) — le marché paie très cher la peur"),
            details={"expiry": expiry, "dte": dte, "put_iv": round(put_iv, 4),
                     "call_iv": round(call_iv, 4), "skew_pts": round(skew_pts, 1)},
        ))
    elif skew_pts <= -3.0:
        score = clamp_score(45 + abs(skew_pts) * 4)  # skew inversé = rare
        opps.append(Opportunity(
            ticker=snap.ticker,
            kind="skew_inverse",
            strategy="Signal rare : spéculation haussière agressive — surveiller un squeeze / call spreads",
            score=score,
            summary=(f"{snap.ticker} : skew INVERSÉ, calls +5% à {call_iv:.0%} plus chers que "
                     f"puts -5% à {put_iv:.0%} ({skew_pts:+.1f} pts) — demande haussière anormale"),
            details={"expiry": expiry, "dte": dte, "put_iv": round(put_iv, 4),
                     "call_iv": round(call_iv, 4), "skew_pts": round(skew_pts, 1)},
        ))
    return opps
