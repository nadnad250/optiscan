"""Détecteur de puts cash-secured concrets : trouve LE strike à vendre.

Quand la prime est riche, propose un put ~delta 0.30 avec rendement
annualisé et probabilité de succès calculés — prêt à trader (manuellement).
"""
from ..bs import prob_otm_put, put_delta
from ..models import MarketSnapshot, Opportunity
from ..quant.greeks import gamma, theta_put
from .common import clamp_score, valid_iv

MIN_ANNUAL_YIELD = 0.15   # 15 % annualisé minimum pour signaler
MIN_MID_PRICE = 0.10


def detect(snap: MarketSnapshot, cfg: dict) -> list[Opportunity]:
    best = None
    target = cfg["csp_target_delta"]

    for expiry, dte, _calls, puts in snap.chains:
        if dte < 14:  # trop court pour du cash-secured confortable
            continue
        t = dte / 365
        df = valid_iv(puts).dropna(subset=["bid", "ask"])
        df = df[(df["bid"] > 0) & (df["strike"] < snap.spot)]
        for _, row in df.iterrows():
            strike, iv = float(row["strike"]), float(row["impliedVolatility"])
            delta = abs(put_delta(snap.spot, strike, iv, t))
            if not (target - 0.08 <= delta <= target + 0.08):
                continue
            mid = (float(row["bid"]) + float(row["ask"])) / 2
            if mid < MIN_MID_PRICE:
                continue
            annual_yield = (mid / strike) * (365 / dte)
            if annual_yield < MIN_ANNUAL_YIELD:
                continue
            pop = prob_otm_put(snap.spot, strike, iv, t)
            cand = {
                "expiry": expiry, "dte": dte, "strike": strike, "mid": round(mid, 2),
                "delta": round(delta, 2), "annual_yield": round(annual_yield, 3),
                "prob_profit": round(pop, 2), "capital_requis": int(strike * 100),
                # grecs de la position (put vendu => thêta positif : le temps te paie)
                "greeks": {
                    "delta": round(delta, 4),
                    "gamma": round(-gamma(snap.spot, strike, iv, t), 4),
                    "theta_jour": round(-theta_put(snap.spot, strike, iv, t), 4),
                },
            }
            if best is None or annual_yield > best["annual_yield"]:
                best = cand

    if not best:
        return []
    score = clamp_score(15 + min(best["annual_yield"], 1.0) * 70 + (best["prob_profit"] - 0.5) * 60)
    return [Opportunity(
        ticker=snap.ticker,
        kind="put_cash_secured",
        strategy=(f"Vendre put {snap.ticker} {best['strike']:g} exp {best['expiry']} "
                  f"@ ~{best['mid']:.2f}$ (capital requis {best['capital_requis']:,}$)"),
        score=score,
        summary=(f"{snap.ticker} : put {best['strike']:g} ({best['dte']}j, delta {best['delta']}) "
                 f"rapporte ~{best['annual_yield']:.0%} annualisé, "
                 f"prob. de profit ~{best['prob_profit']:.0%}"),
        details=best,
    )]
