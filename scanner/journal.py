"""Journal prospectif — le protocole d'évaluation GELÉ.

Chaque scan enregistre TOUS les tickers (même sans signal) avec toutes les
probabilités, la raison d'acceptation/rejet, les quotes réelles et les grecs.
Les résultats sont capturés après 1, 3 et 5 séances (horizons figés d'avance).

Groupes de comparaison (jugement final : >= 50 signaux, idéalement 100) :
  A = signal directionnel seul
  B = signal directionnel + timing ML (le filtre à prouver)
  C = contrôle aléatoire apparié (même jour, même univers)
Ni le modèle, ni le seuil, ni les horizons ne doivent changer pendant l'essai.
"""
import csv
import json
import random
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .models import MarketSnapshot

JOURNAL_PATH = Path(__file__).resolve().parent.parent / "output" / "journal_prospectif.csv"
TZ = ZoneInfo("America/New_York")

COLUMNS = [
    "horodatage", "timezone", "sha_model", "groupe", "ticker", "source", "spot",
    "prob_ml", "seuil_ml", "prob_implicite", "ratio", "direction", "decision", "raison",
    "side", "strike", "expiry", "dte", "bid", "ask", "mid",
    "iv", "delta", "theta_jour", "vega", "cout_entree_ask",
    "ret_1s", "ret_3s", "ret_5s",
    "exit_bid_1s", "exit_bid_3s", "exit_bid_5s",
    "pnl_option_est_5s_pct", "resultat_direction_seule", "statut",
]

# symboles Yahoo des titres européens (pour la capture des résultats)
EU_YAHOO = {"DTE": "DTE.DE", "LHA": "LHA.DE", "EOAN": "EOAN.DE", "CBK": "CBK.DE",
            "TEF": "TEF.MC", "SAN": "SAN.MC", "IBE": "IBE.MC", "ORA": "ORA.PA",
            "ENGI": "ENGI.PA", "AF": "AF.PA", "ENEL": "ENEL.MI", "ISP": "ISP.MI"}


def _yahoo_symbol(ticker: str) -> str:
    sym = ticker.split(" ")[0]
    return EU_YAHOO.get(sym, sym) if "(EUR)" in ticker else sym


def _row_base(snap: MarketSnapshot, source: str, ev: dict) -> dict:
    return {
        "horodatage": datetime.now(TZ).isoformat(timespec="seconds"),
        "timezone": "America/New_York",
        "sha_model": ev.get("sha_model") or "",
        "ticker": snap.ticker, "source": source, "spot": round(snap.spot, 4),
        "prob_ml": ev.get("prob"), "seuil_ml": ev.get("seuil"),
        "prob_implicite": ev.get("p_implied"), "ratio": ev.get("ratio"),
        "direction": ev.get("direction"),
        "decision": ev.get("decision"), "raison": (ev.get("raison") or "").replace(";", ","),
        "statut": "en_attente",
    }


def _fill_contract(row: dict, contrat: dict | None) -> dict:
    if contrat:
        row.update({
            "side": contrat["side"], "strike": contrat["strike"],
            "expiry": contrat["expiry"], "dte": contrat["dte"],
            "bid": contrat["bid"], "ask": contrat["ask"], "mid": contrat["mid"],
            "iv": contrat["iv"], "delta": contrat["delta"],
            "theta_jour": contrat["theta_jour"], "vega": contrat["vega"],
            # coût d'entrée RÉALISTE : on paie l'ask, pas le mid
            "cout_entree_ask": round(contrat["ask"] * 100, 2),
        })
    return row


def _append(rows: list[dict]) -> None:
    JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    new_file = not JOURNAL_PATH.exists()
    with open(JOURNAL_PATH, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, delimiter=";", extrasaction="ignore")
        if new_file:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)


def log_ticker(snap: MarketSnapshot, cfg: dict, source: str) -> None:
    """Une ligne 'scan' pour CHAQUE ticker + lignes A/B/C selon les signaux."""
    from .detectors.ml_move import evaluate
    try:
        ev = evaluate(snap, cfg)
    except Exception as exc:
        ev = {"decision": "erreur", "raison": str(exc)}
    rows = [dict(_row_base(snap, source, ev), groupe="scan")]

    contrat = ev.get("contrat")
    has_direction = abs(ev.get("direction") or 0.0) >= cfg["dir_min_signal"]
    ml_ok = ev.get("decision") in ("signal_combine", "signal_sans_direction",
                                   "direction_sans_contrat")
    if has_direction and contrat:
        # A : la stratégie directionnelle seule (avec ou sans ML)
        rows.append(_fill_contract(dict(_row_base(snap, source, ev), groupe="A"), contrat))
        if ml_ok:
            # B : directionnelle + timing ML — le filtre dont on teste la valeur
            rows.append(_fill_contract(dict(_row_base(snap, source, ev), groupe="B"), contrat))
    _append(rows)


def log_control(snap: MarketSnapshot, cfg: dict, source: str) -> None:
    """Groupe C : contrôle apparié — même jour, même univers, direction ALÉATOIRE
    (graine = date : reproductible, décidée avant de voir les résultats)."""
    from .detectors.directional import _pick_contract
    from .detectors.ml_move import evaluate
    rng = random.Random(datetime.now(TZ).strftime("%Y%m%d") + snap.ticker)
    bullish = rng.random() < 0.5
    contrat_raw = _pick_contract(snap, bullish, cfg)
    try:
        ev = evaluate(snap, cfg)
    except Exception:
        ev = {}
    row = dict(_row_base(snap, source, ev), groupe="C",
               decision="controle_aleatoire",
               raison=f"contrôle apparié, direction aléatoire {'hausse' if bullish else 'baisse'}")
    if contrat_raw:
        from .quant.greeks import theta_call, theta_put, vega  # grecs identiques aux groupes A/B
        t = contrat_raw["dte"] / 365
        theta_fn = theta_call if bullish else theta_put
        row = _fill_contract(row, {
            "side": "call" if bullish else "put",
            "strike": contrat_raw["strike"], "expiry": contrat_raw["expiry"],
            "dte": contrat_raw["dte"], "bid": contrat_raw["bid"], "ask": contrat_raw["ask"],
            "mid": round(contrat_raw["mid"], 3), "iv": round(contrat_raw["iv"], 4),
            "delta": round(contrat_raw["delta"], 3),
            "theta_jour": round(theta_fn(snap.spot, contrat_raw["strike"], contrat_raw["iv"], t), 4),
            "vega": round(vega(snap.spot, contrat_raw["strike"], contrat_raw["iv"], t), 4),
        })
    _append([row])


def already_logged_today() -> bool:
    """Le journal contient-il déjà des lignes de scan pour la date NY du jour ?
    (garde anti-doublon : deux crons UTC ou relance manuelle le même jour)."""
    if not JOURNAL_PATH.exists():
        return False
    today = datetime.now(TZ).date().isoformat()
    try:
        with open(JOURNAL_PATH, encoding="utf-8") as fh:
            return any(line.startswith(today) for line in fh)
    except OSError:
        return False


def update_outcomes() -> dict:
    """Capture les résultats aux horizons figés (+1, +3, +5 séances)."""
    import math

    import pandas as pd
    import yfinance as yf

    from .quant.greeks import call_price, put_price

    if not JOURNAL_PATH.exists():
        return {"maj": 0, "completes": 0}
    df = pd.read_csv(JOURNAL_PATH, sep=";")
    pending = df[df["statut"] != "complet"]
    if pending.empty:
        return {"maj": 0, "completes": 0}

    chain_cache: dict = {}

    def _real_exit_bid(ysym: str, row) -> float | None:
        """VRAI bid du contrat au moment de la capture (revue n°15) —
        pas une estimation Black-Scholes. US uniquement (chaînes Yahoo)."""
        if "(EUR)" in str(row["ticker"]) or pd.isna(row.get("strike")):
            return None
        exp = str(row["expiry"]).replace("-", "").replace(".0", "")
        if len(exp) != 8:
            return None
        exp_iso = f"{exp[:4]}-{exp[4:6]}-{exp[6:]}"
        key = (ysym, exp_iso)
        if key not in chain_cache:
            try:
                oc = yf.Ticker(ysym).option_chain(exp_iso)
                chain_cache[key] = {"call": oc.calls, "put": oc.puts}
            except Exception:
                chain_cache[key] = None
        chains = chain_cache[key]
        if not chains:
            return None
        df_side = chains.get(str(row["side"]))
        if df_side is None or df_side.empty:
            return None
        match = df_side[abs(df_side["strike"] - float(row["strike"])) < 0.01]
        if match.empty:
            return None
        bid = float(match.iloc[0].get("bid") or 0)
        last = float(match.iloc[0].get("lastPrice") or 0)
        if bid <= 0:
            return None
        # garde-fou de plausibilité (audit) : sur les strikes illiquides, le
        # bid Yahoo est parfois un « stub » à 0.01-0.06 non représentatif
        # (VALE affichait bid 0.01 alors que le contrat cotait 0.17/0.33) —
        # on refuse un bid effondré à plus de 75 % sous le dernier échange
        if last > 0 and bid < 0.25 * last:
            return None
        return round(bid, 3)

    updated = completed = 0
    hist_cache: dict[str, pd.DataFrame] = {}
    for idx, row in pending.iterrows():
        ysym = _yahoo_symbol(str(row["ticker"]))
        if ysym not in hist_cache:
            try:
                hist_cache[ysym] = yf.Ticker(ysym).history(period="3mo")
            except Exception:
                hist_cache[ysym] = pd.DataFrame()
        hist = hist_cache[ysym]
        if hist.empty:
            continue
        entry_date = pd.Timestamp(str(row["horodatage"])[:10])
        closes = hist["Close"]
        # UNIQUEMENT les séances TERMINÉES : la bougie du jour en cours est
        # partielle (bug découvert par audit : LCID affichait +0.27% au moment
        # de la capture matinale alors que la séance a clôturé à -16.15%)
        today_ny = pd.Timestamp(datetime.now(TZ).date())
        dates = closes.index.tz_localize(None).normalize()
        after = closes[(dates > entry_date) & (dates < today_ny)]
        if after.empty:
            continue
        spot0 = float(row["spot"])
        changed = False
        for h, col in ((1, "ret_1s"), (3, "ret_3s"), (5, "ret_5s")):
            if pd.isna(row.get(col)) and len(after) >= h:
                df.at[idx, col] = round(float(after.iloc[h - 1]) / spot0 - 1, 5)
                changed = True
            # VRAI bid de sortie, capturé LE JOUR de l'horizon (updater quotidien)
            bcol = f"exit_bid_{h}s"
            if bcol in df.columns and pd.isna(row.get(bcol)) and len(after) == h:
                real_bid = _real_exit_bid(ysym, row)
                if real_bid is not None:
                    df.at[idx, bcol] = real_bid
                    changed = True
        if len(after) >= 5:
            # P&L option estimé à +5 séances : réévaluation Black-Scholes avec
            # l'IV d'entrée FIGÉE (approximation documentée, pas une quote réelle)
            if not pd.isna(row.get("strike")) and not pd.isna(row.get("iv")):
                s5 = float(after.iloc[4])
                t_rest = max((float(row["dte"]) - 7) / 365, 1e-4)
                fn = call_price if row["side"] == "call" else put_price
                val = fn(s5, float(row["strike"]), float(row["iv"]), t_rest)
                cost = float(row["ask"])
                if cost > 0:
                    df.at[idx, "pnl_option_est_5s_pct"] = round((val - cost) / cost, 4)
            if not pd.isna(row.get("direction")) and float(row["direction"] or 0) != 0:
                sens = 1 if float(row["direction"]) > 0 else -1
                df.at[idx, "resultat_direction_seule"] = round(
                    sens * (float(after.iloc[4]) / spot0 - 1), 5)
            df.at[idx, "statut"] = "complet"
            completed += 1
        if changed:
            updated += 1
    df.to_csv(JOURNAL_PATH, sep=";", index=False)
    return {"maj": updated, "completes": completed}
