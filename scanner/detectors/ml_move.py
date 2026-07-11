"""Détecteur ML : probabilité d'un GRAND MOUVEMENT sous 5 séances.

Modèle : gradient boosting entraîné sur ~40 000 exemples (133 titres, 3 ans),
23 features de compression/volatilité/volume. Performance HORS-ÉCHANTILLON
mesurée avec purge anti-fuite : AUC ~0.63, le top-décile du modèle contient
~1.6x plus d'explosions que la moyenne (voir ml/model_meta.json).

L'edge : signaler quand le modèle voit l'explosion venir ALORS QUE les
options la font payer bon marché. On ne prédit PAS la direction — on
achète le mouvement (straddle), ou call/put si les signaux directionnels
de l'autre détecteur penchent d'un côté.
"""
import json
import math
import pickle
from pathlib import Path

from ..ml.features import LABEL_HORIZON, LABEL_K, compute_features
from ..models import MarketSnapshot, Opportunity
from ..quant.greeks import ncdf
from .common import atm_iv, clamp_score, valid_iv

_MODEL = None
_META = None
PREF_DTE = (10, 45)


def _load():
    global _MODEL, _META
    if _MODEL is None:
        base = Path(__file__).resolve().parent.parent / "ml"
        with open(base / "model.pkl", "rb") as fh:
            _MODEL = pickle.load(fh)
        _META = json.loads((base / "model_meta.json").read_text(encoding="utf-8"))
    return _MODEL, _META


MAX_SPREAD_PCT = 0.25   # au-delà, la friction d'exécution mange l'edge


def _atm_straddle(snap: MarketSnapshot):
    """Prix du straddle ATM (~21 j), avec filtre de liquidité sur les jambes."""
    chains = [c for c in snap.chains if PREF_DTE[0] <= c[1] <= PREF_DTE[1]] or list(snap.chains)
    expiry, dte, calls, puts = min(chains, key=lambda c: abs(c[1] - 21))
    cost = 0.0
    strike = None
    worst_spread = 0.0
    for df in (calls, puts):
        d = valid_iv(df)
        d = d[(d["bid"] > 0) & (d["ask"] > 0)]
        if d.empty:
            return None
        row = d.iloc[(d["strike"] - snap.spot).abs().argsort().iloc[0]]
        if strike is None:
            strike = float(row["strike"])
        mid = (float(row["bid"]) + float(row["ask"])) / 2
        if mid <= 0:
            return None
        worst_spread = max(worst_spread, (float(row["ask"]) - float(row["bid"])) / mid)
        cost += mid
    if worst_spread > MAX_SPREAD_PCT:
        return None  # trop illiquide : la fourchette mange l'edge
    return {"expiry": expiry, "dte": dte, "strike": strike, "cout": cost,
            "spread_max_jambes": round(worst_spread, 3)}


# La formule terminale lognormale SOUS-ESTIME la proba du max intra-fenêtre
# d'un facteur ~1.3 dans la zone utile (Monte Carlo 400k chemins, audit) —
# sans correction, le ratio serait gonflé de ~30 % en faveur du signal.
MAX_WINDOW_FACTOR = 1.3


def evaluate(snap: MarketSnapshot, cfg: dict) -> dict:
    """Diagnostic COMPLET du ticker (y compris les rejets), pour le détecteur
    ET le journal prospectif — toutes les probabilités sont enregistrées,
    pas seulement les signaux acceptés (protocole d'évaluation gelé)."""
    from ..quant.greeks import theta_call, theta_put, vega
    from .directional import _pick_contract, _signals as directional_signals

    out = {"prob": None, "seuil": None, "p_implied": None, "ratio": None,
           "direction": 0.0, "iv_atm": None, "rv": round(snap.realized_vol_20d, 4),
           "decision": "donnees_insuffisantes", "raison": "",
           "contrat": None, "straddle": None, "sha_model": None}
    try:
        model, meta = _load()
    except (OSError, json.JSONDecodeError):
        out["raison"] = "modèle non entraîné"
        return out
    out["seuil"] = meta["seuil_signal_p85_valid"]
    out["sha_model"] = meta.get("gel", {}).get("sha256_model", "")[:16]
    out["meta"] = meta

    feats = compute_features(snap.closes, snap.volumes)
    if feats is None:
        out["raison"] = "historique insuffisant (<130 séances)"
        return out
    out["prob"] = round(float(model.predict_proba([feats])[0, 1]), 4)

    dir_sig = directional_signals(snap)
    out["direction"] = round(dir_sig["direction"], 3) if dir_sig else 0.0
    has_direction = abs(out["direction"]) >= cfg["dir_min_signal"]
    if has_direction:
        contract = _pick_contract(snap, out["direction"] > 0, cfg)
        if contract:
            t = contract["dte"] / 365
            theta_fn = theta_call if out["direction"] > 0 else theta_put
            out["contrat"] = {
                "side": "call" if out["direction"] > 0 else "put",
                "strike": contract["strike"], "expiry": contract["expiry"],
                "dte": contract["dte"], "bid": contract["bid"], "ask": contract["ask"],
                "mid": round(contract["mid"], 3), "iv": round(contract["iv"], 4),
                "delta": round(contract["delta"], 3),
                "theta_jour": round(theta_fn(snap.spot, contract["strike"], contract["iv"], t), 4),
                "vega": round(vega(snap.spot, contract["strike"], contract["iv"], t), 4),
            }

    rv, iv = snap.realized_vol_20d, atm_iv(snap)
    if iv:
        out["iv_atm"] = round(iv, 4)
    if not iv or rv <= 0.02:
        out["raison"] = "IV ou vol réalisée indisponible"
        return out

    out["p_implied"] = round(min(1.0, MAX_WINDOW_FACTOR * 2 * (1 - ncdf(LABEL_K * rv / iv))), 4)
    out["ratio"] = round(out["prob"] / max(out["p_implied"], 0.01), 3)

    if out["prob"] < out["seuil"]:
        out["decision"] = "rejet_prob"
        out["raison"] = f"prob {out['prob']:.3f} < seuil {out['seuil']:.3f}"
    elif out["ratio"] < cfg["ml_min_ratio"]:
        out["decision"] = "rejet_ratio"
        out["raison"] = (f"options déjà chères : ratio {out['ratio']:.2f} < "
                         f"{cfg['ml_min_ratio']} (p_implicite {out['p_implied']:.3f})")
    elif has_direction and out["contrat"]:
        out["decision"] = "signal_combine"
        out["raison"] = "timing ML + direction indépendante convergents"
    elif has_direction:
        out["decision"] = "direction_sans_contrat"
        out["raison"] = "direction convergente mais aucun contrat de qualité"
    else:
        out["decision"] = "signal_sans_direction"
        out["raison"] = "mouvement probable mais aucune direction claire : pas de trade"
    return out


def detect(snap: MarketSnapshot, cfg: dict) -> list[Opportunity]:
    ev = evaluate(snap, cfg)
    if ev["decision"] in ("donnees_insuffisantes", "rejet_prob", "rejet_ratio"):
        return []
    meta = ev["meta"]
    prob, seuil, p_implied, ratio = ev["prob"], ev["seuil"], ev["p_implied"], ev["ratio"]
    rv, iv = snap.realized_vol_20d, ev["iv_atm"]
    direction = ev["direction"]

    straddle = _atm_straddle(snap)
    move_seuil = LABEL_K * rv * math.sqrt(LABEL_HORIZON / 252)
    base = meta["metrics"]["test"]["taux_de_base"]
    lift = prob / max(base, 1e-6)

    score = clamp_score(20 + (prob - seuil) * 160 + min(ratio, 3.0) * 10
                        + (8 if iv <= rv * 1.1 else 0))
    details = {
        "prob_modele_5j": round(prob, 3),
        "taux_de_base": round(base, 3),
        "confiance_vs_moyenne": f"x{lift:.1f}",
        "prob_implicite_options": round(p_implied, 3),
        "ratio_modele_options": round(ratio, 2),
        "mouvement_seuil": round(move_seuil, 3),
        "iv_atm": round(iv, 3), "vol_realisee": round(rv, 3),
        "auc_test_du_modele": meta["metrics"]["test"]["auc"],
        "spot": round(snap.spot, 2),
    }
    # Règle (audit + revue utilisateur) : Signal = ML(volatilité) x
    # Direction(indépendante) x Exécution. Sans direction -> PAS de trade.
    details["direction_independante"] = round(direction, 2)
    if straddle:
        details["straddle_reference"] = {**straddle, "cout": round(straddle["cout"], 2),
                                         "cout_par_contrat": int(straddle["cout"] * 100)}

    if ev["decision"] == "signal_combine":
        c = ev["contrat"]
        bullish = direction > 0
        score += 12  # convergence timing x direction : le vrai setup
        strategy = (f"ACHETER {c['side']} {c['strike']:g} exp {c['expiry']} "
                    f"@ ~{c['mid']:.2f}$ ({int(c['mid']*100)}$/contrat) — "
                    f"timing ML + direction {'haussière' if bullish else 'baissière'} convergents")
        details["contrat_suggere"] = c
    elif ev["decision"] == "direction_sans_contrat":
        strategy = (f"Direction {'haussière' if direction > 0 else 'baissière'} convergente mais "
                    f"aucun contrat de qualité (delta/prime) — surveiller")
    else:
        # mouvement probable mais sens inconnu : on n'achète rien
        score = min(score, 45)
        strategy = ("PAS DE TRADE : mouvement probable mais aucune direction claire — "
                    "mettre sous surveillance et attendre un signal directionnel")
    if snap.earnings_days is not None and snap.earnings_days <= LABEL_HORIZON + 2:
        details["attention_earnings"] = (f"résultats dans {snap.earnings_days}j — le "
                                         f"mouvement prévu est peut-être déjà facturé")

    return [Opportunity(
        ticker=snap.ticker,
        kind="ml_mouvement",
        strategy=strategy,
        score=clamp_score(score),
        summary=(f"{snap.ticker} : le modèle ML voit {prob:.0%} de chances d'un mouvement "
                 f">{move_seuil:.1%} sous 5 séances ({lift:.1f}x la moyenne), alors que les "
                 f"options n'en facturent que {p_implied:.0%} — le mouvement est bon marché"),
        details=details,
    )]
