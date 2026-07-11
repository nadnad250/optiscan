"""Détecteur d'activité inhabituelle : gros volume vs open interest.

C'est le signal "que peu de gens voient" : quand le volume du jour sur un
strike précis dépasse largement l'open interest existant, quelqu'un ouvre
une grosse position nouvelle (souvent un acteur informé).
"""
import math

import pandas as pd

from ..models import MarketSnapshot, Opportunity
from .common import clamp_score, valid_iv


def _scan_side(df: pd.DataFrame, side: str, snap: MarketSnapshot,
               expiry: str, dte: int, cfg: dict) -> list[Opportunity]:
    out = []
    df = valid_iv(df)
    needed = {"volume", "openInterest", "strike", "lastPrice"}
    if df.empty or not needed.issubset(df.columns):
        return out

    df = df.dropna(subset=["volume", "openInterest", "lastPrice"])
    df = df[df["volume"] >= cfg["unusual_min_volume"]]
    # OI quasi nul = données pas encore consolidées (avant l'ouverture) → bruit
    df = df[df["openInterest"] >= cfg["unusual_min_oi"]]
    for _, row in df.iterrows():
        oi = max(float(row["openInterest"]), 1.0)
        ratio = float(row["volume"]) / oi
        premium_flow = float(row["volume"]) * float(row["lastPrice"]) * 100
        if ratio < cfg["unusual_vol_oi_ratio"] or premium_flow < cfg["unusual_min_premium_usd"]:
            continue
        otm_pct = (float(row["strike"]) / snap.spot - 1) * 100
        # échelle log : ratio ET taille du flux comptent, sans saturer à 100
        score = clamp_score(20 + 9 * math.log2(ratio) + 10 * math.log10(1 + premium_flow / 1e5))
        direction = "haussier" if side == "call" else "baissier (ou couverture)"
        out.append(Opportunity(
            ticker=snap.ticker,
            kind="activite_inhabituelle",
            strategy=f"Surveiller / suivre le flux : {side} {row['strike']:g} exp {expiry}",
            score=score,
            summary=(f"{snap.ticker} : volume {int(row['volume']):,} vs OI {int(oi):,} "
                     f"(x{ratio:.1f}) sur {side} {row['strike']:g} ({otm_pct:+.1f}% du spot), "
                     f"~{premium_flow/1e6:.2f}M$ de prime — flux {direction}"),
            details={
                "expiry": expiry, "dte": dte, "side": side,
                "strike": float(row["strike"]), "volume": int(row["volume"]),
                "open_interest": int(oi), "ratio_vol_oi": round(ratio, 2),
                "premium_flow_usd": int(premium_flow),
            },
        ))
    return out


def detect(snap: MarketSnapshot, cfg: dict) -> list[Opportunity]:
    opps = []
    for expiry, dte, calls, puts in snap.chains:
        opps += _scan_side(calls, "call", snap, expiry, dte, cfg)
        opps += _scan_side(puts, "put", snap, expiry, dte, cfg)
    # ne garder que les 3 plus gros flux par ticker pour éviter le bruit
    return sorted(opps, key=lambda o: o.score, reverse=True)[:3]
