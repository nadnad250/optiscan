"""Optimiseur algorithmique : meilleur strike × échéance × stratégie.

Pour chaque ticker, énumère des centaines de combinaisons (bull put spreads,
bear call spreads, iron condors, straddles), évalue chacune par :
  EV (espérance de gain), POP (prob. de profit), ratio gain/risque,
  rendement annualisé du capital à risque, fraction de Kelly, liquidité.
Ne remonte que les combinaisons dont l'edge statistique est positif.
"""
from ..models import MarketSnapshot, Opportunity
from ..quant import strategies as st
from ..quant.greeks import (call_delta, call_price, gamma, put_delta,
                            put_price, theta_call, theta_put, vega)
from .common import atm_iv, clamp_score, valid_iv

SHORT_PUT_DELTA = (0.15, 0.40)    # zone du strike vendu côté put
SHORT_CALL_DELTA = (0.10, 0.35)   # zone du strike vendu côté call
MAX_WIDTH_STEPS = 4               # écart max (en strikes) entre jambes
MIN_CREDIT = 0.08                 # crédit net minimal par action


IV_BAND_VS_ATM = (0.5, 1.9)   # IV d'un strike hors de cette bande vs ATM = quote suspecte
MAX_MID_VS_THEO = 0.60         # écart mid / prix théorique (à l'IV du strike) toléré
SLIPPAGE_PCT = 0.10            # décote d'exécution sur le crédit (traversée des fourchettes)
COMMISSION = 0.02              # commissions aller-retour par action (~2 $ le spread)


def _rows(df, spot, t, delta_fn, price_fn, atm):
    """Lignes exploitables, nettoyées des quotes gelées/désynchronisées.

    Deux garde-fous anti-quotes périmées (découverts par audit adversarial) :
    l'IV du strike doit rester dans une bande raisonnable autour de l'ATM,
    et le mid doit rester cohérent avec le prix théorique à sa propre IV.
    """
    out = []
    for _, r in valid_iv(df).iterrows():
        bid, ask = float(r.get("bid", 0) or 0), float(r.get("ask", 0) or 0)
        if bid <= 0 or ask <= 0 or ask < bid:
            continue
        mid = (bid + ask) / 2
        if mid <= 0:
            continue
        strike, iv = float(r["strike"]), float(r["impliedVolatility"])
        if not (IV_BAND_VS_ATM[0] * atm <= iv <= IV_BAND_VS_ATM[1] * atm):
            continue
        theo = price_fn(spot, strike, iv, t)
        if theo > 0.10 and abs(mid - theo) / theo > MAX_MID_VS_THEO:
            continue
        out.append({
            "strike": strike, "iv": iv, "mid": mid,
            "liq": min(1.0, (ask - bid) / mid),
            "delta": abs(delta_fn(spot, strike, iv, t)),
        })
    return sorted(out, key=lambda x: x["strike"])


def _put_spreads(puts, spot, sigma, t, dte, expiry, cfg):
    lo, hi = SHORT_PUT_DELTA
    for i, short in enumerate(puts):
        if not (lo <= short["delta"] <= hi):
            continue
        for j in range(max(0, i - MAX_WIDTH_STEPS), i):
            long = puts[j]
            credit = short["mid"] - long["mid"]
            width = short["strike"] - long["strike"]
            # crédit >= 90 % de la largeur = quasi-arbitrage impossible => quotes fausses
            if credit < MIN_CREDIT or credit >= 0.9 * width:
                continue
            cand = st.bull_put_spread(spot, short["strike"], long["strike"], credit,
                                      sigma, t, dte, expiry, max(short["liq"], long["liq"]))
            if cand:
                yield cand


def _call_spreads(calls, spot, sigma, t, dte, expiry, cfg):
    lo, hi = SHORT_CALL_DELTA
    for i, short in enumerate(calls):
        if not (lo <= short["delta"] <= hi):
            continue
        for j in range(i + 1, min(len(calls), i + 1 + MAX_WIDTH_STEPS)):
            long = calls[j]
            credit = short["mid"] - long["mid"]
            width = long["strike"] - short["strike"]
            if credit < MIN_CREDIT or credit >= 0.9 * width:
                continue
            cand = st.bear_call_spread(spot, short["strike"], long["strike"], credit,
                                       sigma, t, dte, expiry, max(short["liq"], long["liq"]))
            if cand:
                yield cand


def _straddle(calls, puts, spot, sigma, t, dte, expiry):
    atm_c = min(calls, key=lambda x: abs(x["strike"] - spot), default=None)
    atm_p = min((p for p in puts if atm_c and p["strike"] == atm_c["strike"]),
                key=lambda x: abs(x["strike"] - spot), default=None)
    if not atm_c or not atm_p:
        return None
    return st.long_straddle(spot, atm_c["strike"], atm_c["mid"] + atm_p["mid"],
                            sigma, t, dte, expiry, max(atm_c["liq"], atm_p["liq"]))


def _score(c: st.Candidate) -> float:
    return clamp_score(25 + c.edge * 400 + (c.pop - 0.60) * 80
                       + min(c.roc_annual, 1.5) * 12 - c.liq_penalty * 30)


def _ev_prudent(c: st.Candidate) -> float:
    """EV après décote d'exécution (slippage sur les fourchettes) et commissions."""
    return c.ev - SLIPPAGE_PCT * abs(c.credit) - COMMISSION


def _greeks_position(c: st.Candidate, spot: float, sigma: float) -> dict:
    """Grecs nets de la position (VENDRE = signe négatif sur l'option détenue)."""
    t = c.dte / 365
    tot = {"delta": 0.0, "gamma": 0.0, "theta_jour": 0.0, "vega": 0.0}
    for leg in c.legs:
        side, typ, strike = leg.split()
        sign, k = (-1 if side == "VENDRE" else 1), float(strike)
        if typ == "put":
            tot["delta"] += sign * put_delta(spot, k, sigma, t)
            tot["theta_jour"] += sign * theta_put(spot, k, sigma, t)
        else:
            tot["delta"] += sign * call_delta(spot, k, sigma, t)
            tot["theta_jour"] += sign * theta_call(spot, k, sigma, t)
        tot["gamma"] += sign * gamma(spot, k, sigma, t)
        tot["vega"] += sign * vega(spot, k, sigma, t)
    return {k: round(v, 4) for k, v in tot.items()}


def _summary(ticker: str, c: st.Candidate, earnings_flag: bool) -> str:
    flux = (f"crédit {c.credit:.2f}$" if c.credit > 0 else f"débit {-c.credit:.2f}$")
    txt = (f"{ticker} : {c.name} {'/'.join(l.split()[-1] for l in c.legs)} "
           f"exp {c.expiry} ({c.dte}j) — {flux}, risque max {c.max_loss:.2f}$, "
           f"POP {c.pop:.0%}, EV prudent {_ev_prudent(c):+.2f}$/action, "
           f"gain max {c.ratio_gain_risque:.0%} du risque en {c.dte}j")
    if earnings_flag:
        txt += " [ATTENTION : earnings avant l'expiration]"
    return txt


def detect(snap: MarketSnapshot, cfg: dict) -> list[Opportunity]:
    iv = atm_iv(snap)
    rv = snap.realized_vol_20d
    if not iv or iv < 0.05:  # IV corrompue (pré-ouverture) → pas de calcul fiable
        return []
    # vol estimée : mélange prudent vol réalisée / vol implicite
    sigma_true = max(0.6 * (rv if rv > 0.02 else iv) + 0.4 * iv, 0.08)

    candidates = []
    for expiry, dte, calls_df, puts_df in snap.chains:
        if dte < 7:
            continue
        t = dte / 365
        puts = _rows(puts_df, snap.spot, t, put_delta, put_price, iv)
        calls = _rows(calls_df, snap.spot, t, call_delta, call_price, iv)

        ps = list(_put_spreads(puts, snap.spot, sigma_true, t, dte, expiry, cfg))
        cs = list(_call_spreads(calls, snap.spot, sigma_true, t, dte, expiry, cfg))
        candidates += ps + cs

        best_put = max(ps, key=lambda c: c.edge, default=None)
        best_call = max(cs, key=lambda c: c.edge, default=None)
        if best_put and best_call:
            condor = st.iron_condor(best_put, best_call, snap.spot, sigma_true, t)
            if condor:
                candidates.append(condor)

        straddle = _straddle(calls, puts, snap.spot, sigma_true, t, dte, expiry)
        if straddle:
            candidates.append(straddle)

    kept = [c for c in candidates
            if _ev_prudent(c) > 0
            and _ev_prudent(c) / c.max_loss >= cfg["opt_min_edge"]
            and c.liq_penalty <= cfg["opt_max_liq"]
            and (c.credit < 0 or c.pop >= cfg["opt_min_pop"])]
    kept.sort(key=_score, reverse=True)

    opps = []
    for c in kept[:cfg["opt_top_per_ticker"]]:
        # résultats publiés avant l'expiration = risque de gap non modélisé
        earnings_flag = (snap.earnings_days is not None
                         and snap.earnings_days <= c.dte)
        details = c.to_details()
        details["spot"] = round(snap.spot, 2)
        details["vol_estimee"] = round(sigma_true, 3)
        details["iv_atm"] = round(iv, 3)
        details["ev_prudent_apres_frais"] = round(_ev_prudent(c), 3)
        details["greeks"] = _greeks_position(c, snap.spot, sigma_true)
        if earnings_flag:
            details["attention_earnings"] = (
                f"résultats dans {snap.earnings_days}j, avant l'expiration — risque de gap")
        score = _score(c) * (0.8 if earnings_flag else 1.0)
        opps.append(Opportunity(
            ticker=snap.ticker,
            kind="strategie_optimale",
            strategy=" + ".join(c.legs) + f" exp {c.expiry} "
                     + (f"(crédit ~{c.credit:.2f}$)" if c.credit > 0 else f"(débit ~{-c.credit:.2f}$)"),
            score=score,
            summary=_summary(snap.ticker, c, earnings_flag),
            details=details,
        ))
    return opps
