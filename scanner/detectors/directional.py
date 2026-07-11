"""Détecteur directionnel : ACHETER un call ou un put AVANT le mouvement.

Fusionne 4 familles de signaux indépendants en un score de direction [-1, +1] :

  1. FLUX D'OPTIONS (poids 35 %) — où va l'argent réel : prime échangée sur
     les calls OTM vs les puts OTM. Les gros acheteurs informés laissent
     cette trace avant les mouvements.
  2. MOMENTUM PRIX (25 %) — tendance 20 jours + RSI 14 : un titre en
     tendance a statistiquement plus de chances de continuer qu'inverser.
  3. CASSURE / BREAKOUT (20 %) — position dans le canal 20 jours : une
     clôture près des plus hauts (bas) précède souvent l'extension.
  4. SKEW (20 %) — quand les calls OTM se paient plus cher que les puts OTM
     (skew inversé, rare), la demande spéculative haussière est anormale.

Un achat n'est proposé QUE si les signaux convergent (|score| >= seuil)
ET que l'option est bon marché en absolu (prime plafonnée) — l'acheteur
ne peut jamais perdre plus que la prime payée.
"""
import math

from ..models import MarketSnapshot, Opportunity
from ..quant.greeks import call_delta, prob_above, prob_below, put_delta
from .common import atm_iv, clamp_score, iv_at_strike, nearest_chain, valid_iv

FLOW_W, MOM_W, BREAK_W, SKEW_W = 0.35, 0.25, 0.20, 0.20
PREF_DTE = (15, 50)          # fenêtre d'échéance préférée (évite le theta court)
OTM_SKEW_DIST = 0.05


def _rsi(closes: tuple, n: int = 14) -> float:
    """RSI de Wilder (lissage exponentiel classique, pas une moyenne simple)."""
    if len(closes) < n + 2:
        return 50.0
    diffs = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    avg_gain = sum(d for d in diffs[:n] if d > 0) / n
    avg_loss = -sum(d for d in diffs[:n] if d < 0) / n
    for d in diffs[n:]:
        avg_gain = (avg_gain * (n - 1) + max(d, 0)) / n
        avg_loss = (avg_loss * (n - 1) + max(-d, 0)) / n
    if avg_loss <= 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def _signals(snap: MarketSnapshot) -> dict | None:
    closes = snap.closes
    if len(closes) < 21:
        return None

    # 1. flux de prime OTM calls vs puts.
    # Échéances < 10 j exclues : leur volume est dominé par les covered calls
    # et les roulements hebdomadaires, pas par des paris directionnels
    # (constat de l'audit adversarial sur MARA).
    call_flow = put_flow = 0.0
    for _, dte, calls, puts in snap.chains:
        if dte < 10:
            continue
        for df, side in ((calls, "C"), (puts, "P")):
            d = valid_iv(df).dropna(subset=["volume", "lastPrice"])
            for _, r in d.iterrows():
                k, v, px = float(r["strike"]), float(r["volume"]), float(r["lastPrice"])
                if v <= 0 or px <= 0:
                    continue
                if side == "C" and k > snap.spot:
                    call_flow += v * px
                elif side == "P" and k < snap.spot:
                    put_flow += v * px
    total_flow = call_flow + put_flow
    flow = (call_flow - put_flow) / total_flow if total_flow > 0 else 0.0

    # 2. momentum : tendance 20 j, tempérée par le RSI (évite d'acheter l'excès)
    mom20 = snap.spot / closes[-21] - 1
    rsi = _rsi(closes)
    mom_sig = math.tanh(mom20 * 8)
    if rsi > 75:   # déjà suracheté : on refroidit le signal haussier
        mom_sig = min(mom_sig, 0.3)
    elif rsi < 25:  # déjà survendu : on refroidit le signal baissier
        mom_sig = max(mom_sig, -0.3)

    # 3. cassure : position dans le canal 20 jours, bornée à ±1
    # (le spot intraday peut sortir du canal des clôtures)
    hi20, lo20 = max(closes[-20:]), min(closes[-20:])
    brk = ((snap.spot - lo20) / (hi20 - lo20) - 0.5) * 2 if hi20 > lo20 else 0.0
    brk = max(-1.0, min(1.0, brk))

    # 4. skew : calls OTM plus chers que puts OTM = demande haussière anormale.
    # Correction d'artefact (audit adversarial) : certaines sources violent la
    # parité call-put (IV call != IV put AU MÊME strike). On mesure ce biais à
    # l'ATM et on le soustrait avant d'interpréter l'écart OTM comme du skew.
    _, _, calls, puts = nearest_chain(snap)
    put_iv = iv_at_strike(puts, snap.spot * (1 - OTM_SKEW_DIST))
    call_iv = iv_at_strike(calls, snap.spot * (1 + OTM_SKEW_DIST))
    call_iv_atm = iv_at_strike(calls, snap.spot)
    put_iv_atm = iv_at_strike(puts, snap.spot)
    if put_iv and call_iv and call_iv_atm and put_iv_atm:
        parity_bias = call_iv_atm - put_iv_atm      # ~0 si les données sont saines
        diff = (call_iv - put_iv) - parity_bias
        # centré sur le skew "normal" (~ -3 pts) : 0 = rien d'anormal
        skew_sig = max(-1.0, min(1.0, (diff + 0.03) / 0.06))
    else:
        skew_sig = 0.0

    direction = (FLOW_W * flow + MOM_W * mom_sig + BREAK_W * brk + SKEW_W * skew_sig)
    return {
        "direction": direction, "flux": round(flow, 2), "momentum_20j": round(mom20, 3),
        "rsi": round(rsi, 0), "breakout": round(brk, 2), "skew": round(skew_sig, 2),
    }


def _pick_contract(snap: MarketSnapshot, bullish: bool, cfg: dict):
    """Choisit LE contrat à acheter : delta ~cible, échéance 15-50 j, prime plafonnée."""
    chains = [c for c in snap.chains if PREF_DTE[0] <= c[1] <= PREF_DTE[1]] or list(snap.chains)
    expiry, dte, calls, puts = min(chains, key=lambda c: abs(c[1] - 30))
    df, delta_fn = (calls, call_delta) if bullish else (puts, put_delta)
    t = dte / 365

    best = None
    for _, r in valid_iv(df).iterrows():
        bid, ask = float(r.get("bid", 0) or 0), float(r.get("ask", 0) or 0)
        if bid <= 0 or ask <= 0 or ask < bid:
            continue
        mid = (bid + ask) / 2
        if not (0.05 <= mid <= cfg["dir_max_premium"]):
            continue
        k, iv = float(r["strike"]), float(r["impliedVolatility"])
        delta = abs(delta_fn(snap.spot, k, iv, t))
        # un delta trop faible = ticket de loterie : breakeven inatteignable.
        # Sur les actions chères, aucun contrat ne passe les deux filtres
        # (delta ET prime plafonnée) -> le ticker est simplement ignoré.
        if delta < cfg["dir_min_delta"]:
            continue
        gap = abs(delta - cfg["dir_target_delta"])
        if best is None or gap < best["gap"]:
            best = {"gap": gap, "strike": k, "iv": iv, "mid": mid,
                    "bid": bid, "ask": ask,
                    "delta": delta, "expiry": expiry, "dte": dte, "t": t}
    return best


def detect(snap: MarketSnapshot, cfg: dict) -> list[Opportunity]:
    sig = _signals(snap)
    if sig is None:
        return []
    direction = sig["direction"]
    if abs(direction) < cfg["dir_min_signal"]:
        return []
    bullish = direction > 0

    c = _pick_contract(snap, bullish, cfg)
    if c is None:
        return []

    rv = snap.realized_vol_20d
    iv_a = atm_iv(snap) or c["iv"]
    sigma = max(0.6 * (rv if rv > 0.02 else iv_a) + 0.4 * iv_a, 0.08)

    breakeven = c["strike"] + c["mid"] if bullish else c["strike"] - c["mid"]
    move_needed = breakeven / snap.spot - 1
    move_1sig = sigma * math.sqrt(c["t"])
    if bullish:
        pop = prob_above(snap.spot, breakeven, sigma, c["t"])
        s_1sig = snap.spot * (1 + move_1sig)
        value_1sig = max(s_1sig - c["strike"], 0.0)
    else:
        pop = prob_below(snap.spot, breakeven, sigma, c["t"])
        s_1sig = snap.spot * (1 - move_1sig)
        value_1sig = max(c["strike"] - s_1sig, 0.0)
    gain_1sig = (value_1sig - c["mid"]) / c["mid"] if c["mid"] > 0 else 0.0
    # le breakeven doit être ATTEIGNABLE : à l'intérieur du mouvement 1σ attendu
    if gain_1sig <= 0 or abs(move_needed) > move_1sig:
        return []

    # options bon marché en relatif (IV <= RV) = bonus de convexité
    cheap_iv_bonus = max(0.0, (rv / iv_a - 1) * 40) if iv_a > 0.02 and rv > 0.02 else 0.0
    score = clamp_score(20 + abs(direction) * 70 + cheap_iv_bonus
                        + (10 if gain_1sig >= 1.0 else 0))

    earnings_flag = (snap.earnings_days is not None
                     and snap.earnings_days <= c["dte"])
    side, sens = ("call", "HAUSSE") if bullish else ("put", "BAISSE")
    n_conv = sum(1 for k in ("flux", "momentum_20j", "breakout", "skew")
                 if (sig[k] > 0) == bullish and abs(sig[k]) > 0.05)
    details = {
        "sens": sens, "side": side, "strike": c["strike"], "expiry": c["expiry"],
        "dte": c["dte"], "prime_par_action": round(c["mid"], 2),
        "cout_par_contrat": int(c["mid"] * 100), "delta": round(c["delta"], 2),
        "breakeven": round(breakeven, 2), "mouvement_necessaire": round(move_needed, 3),
        "mouvement_attendu_1sigma": round(move_1sig if bullish else -move_1sig, 3),
        "gain_si_mouvement_1sigma": round(gain_1sig, 2),
        "prob_profit_a_echeance": round(pop, 2),
        "score_direction": round(direction, 2), "signaux": sig,
        "iv_contrat": round(c["iv"], 3), "vol_realisee": round(rv, 3), "spot": round(snap.spot, 2),
    }
    if earnings_flag:
        details["attention_earnings"] = (
            f"résultats dans {snap.earnings_days}j, avant l'expiration : tu paies l'IV "
            f"gonflée et elle s'écrasera après l'annonce (IV crush)")
    if earnings_flag:
        score *= 0.85
    return [Opportunity(
        ticker=snap.ticker,
        kind="achat_call" if bullish else "achat_put",
        strategy=(f"ACHETER {side} {c['strike']:g} exp {c['expiry']} @ ~{c['mid']:.2f}$ "
                  f"({int(c['mid']*100)}$/contrat, perte max = la prime)"),
        score=score,
        summary=(f"{snap.ticker} : {n_conv}/4 signaux {sens.lower()} convergents "
                 f"(flux {sig['flux']:+.0%}, momentum {sig['momentum_20j']:+.1%}, "
                 f"RSI {sig['rsi']:.0f}, skew {sig['skew']:+.2f}) → {side} {c['strike']:g} "
                 f"({c['dte']}j) @ {c['mid']:.2f}$ — si mouvement 1σ ({move_1sig:.1%}) : "
                 f"{gain_1sig:+.0%} sur la prime"),
        details=details,
    )]
