"""Entraînement du modèle "grand mouvement sous 5 jours".

Usage : python -m scanner.ml.train

Rigueur anti-illusion (leçons du projet Omega de l'utilisateur) :
- séparation train/test PAR DATE (pas aléatoire) : le test est le futur
- purge de LABEL_HORIZON jours entre train et test (les labels se chevauchent)
- métriques honnêtes : AUC hors-échantillon, Brier, lift du top-décile
- le seuil de signal est calibré sur le jeu de VALIDATION, pas sur le test
"""
import json
import pickle
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from .features import (FEATURE_NAMES, LABEL_HORIZON, MIN_HISTORY,
                       compute_features, compute_label)

MODEL_DIR = Path(__file__).resolve().parent
MODEL_PATH = MODEL_DIR / "model.pkl"
META_PATH = MODEL_DIR / "model_meta.json"

# Univers d'entraînement : grandes capitalisations liquides + valeurs pas chères
# volatiles (même profil que la watchlist) — ~110 titres, 3 ans de données.
UNIVERSE = """
AAPL MSFT NVDA TSLA AMD META AMZN GOOGL NFLX AVGO CRM ORCL ADBE INTC QCOM MU
TXN AMAT LRCX KLAC PANW CRWD SNOW PLTR COIN MSTR SQ PYPL SHOP UBER LYFT ABNB
DASH RBLX U DKNG HOOD SOFI AFRM UPST NIO XPEV LI RIVN LCID F GM T VZ TMUS
PFE MRK JNJ ABBV LLY UNH CVS WBA BMY GILD MRNA BNTX AMGN BA GE CAT DE MMM
HON UPS FDX DAL UAL AAL LUV CCL RCL NCLH MAR WYNN LVS MGM JPM BAC WFC C GS
MS SCHW AXP V MA COF AGNC KVUE XOM CVX COP OXY SLB HAL DVN FCX CLF X AA VALE
PBR GOLD NEM PLUG FCEL BE RIOT MARA CLSK HUT TLRY CGC SNAP PINS WBD PARA KO
PEP MCD SBUX NKE TGT WMT COST HD LOW
""".split()

TEST_START = "2026-02-01"     # le test = les ~5 derniers mois (jamais vus)
VALID_START = "2025-09-01"    # la validation calibre le seuil de signal


def build_dataset() -> pd.DataFrame:
    import yfinance as yf
    print(f"Téléchargement de {len(UNIVERSE)} titres (3 ans, quotidien)...")
    raw = yf.download(" ".join(UNIVERSE), period="3y", interval="1d",
                      group_by="ticker", auto_adjust=True, threads=True,
                      progress=False)
    rows = []
    for sym in UNIVERSE:
        try:
            df = raw[sym].dropna(subset=["Close"])
        except KeyError:
            continue
        closes = df["Close"].to_numpy()
        volumes = df["Volume"].to_numpy()
        dates = df.index
        if len(closes) < MIN_HISTORY + LABEL_HORIZON + 10:
            continue
        # un point tous les 2 jours : réduit l'autocorrélation des exemples
        for t in range(MIN_HISTORY, len(closes) - LABEL_HORIZON, 2):
            label = compute_label(closes, t)
            if label is None:
                continue
            feats = compute_features(closes[: t + 1], volumes[: t + 1])
            if feats is None or any(np.isnan(feats)) or any(np.isinf(feats)):
                continue
            rows.append([sym, dates[t].date().isoformat(), label] + feats)
    cols = ["ticker", "date", "label"] + list(FEATURE_NAMES)
    ds = pd.DataFrame(rows, columns=cols)
    print(f"Dataset : {len(ds):,} exemples, taux de base {ds['label'].mean():.1%}")
    return ds


def train():
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.metrics import brier_score_loss, roc_auc_score

    ds = build_dataset()
    # 14 jours calendaires : 5 séances peuvent couvrir jusqu'à 11 jours
    # calendaires autour des fêtes (constat de l'audit adversarial)
    purge = timedelta(days=14)
    valid_cut = date.fromisoformat(VALID_START)
    test_cut = date.fromisoformat(TEST_START)
    d = pd.to_datetime(ds["date"]).dt.date

    train_m = d < (valid_cut - purge)
    valid_m = (d >= valid_cut) & (d < (test_cut - purge))
    test_m = d >= test_cut
    X = ds[list(FEATURE_NAMES)].to_numpy()
    y = ds["label"].to_numpy()
    print(f"Train {train_m.sum():,} | Valid {valid_m.sum():,} | Test {test_m.sum():,} "
          f"(purge {purge.days}j entre les blocs)")

    # early_stopping désactivé : son split interne est ALÉATOIRE, ce qui
    # fuit entre exemples chevauchants (audit) — itérations fixes à la place
    model = HistGradientBoostingClassifier(
        max_iter=250, learning_rate=0.05, max_leaf_nodes=31,
        min_samples_leaf=200, l2_regularization=1.0,
        early_stopping=False, random_state=42)
    model.fit(X[train_m], y[train_m])

    metrics = {}
    for name, mask in (("train", train_m), ("valid", valid_m), ("test", test_m)):
        p = model.predict_proba(X[mask])[:, 1]
        metrics[name] = {
            "n": int(mask.sum()),
            "taux_de_base": round(float(y[mask].mean()), 4),
            "auc": round(float(roc_auc_score(y[mask], p)), 4),
            "brier": round(float(brier_score_loss(y[mask], p)), 4),
        }
        # lift du top-décile : le décile le plus confiant contient-il
        # vraiment plus d'explosions que la moyenne ?
        top = p >= np.quantile(p, 0.9)
        metrics[name]["taux_top_decile"] = round(float(y[mask][top].mean()), 4)
        metrics[name]["lift_top_decile"] = round(
            float(y[mask][top].mean() / max(y[mask].mean(), 1e-9)), 2)

    # seuil de signal calibré sur la VALIDATION (jamais sur le test)
    p_valid = model.predict_proba(X[valid_m])[:, 1]
    seuil = float(np.quantile(p_valid, 0.85))

    with open(MODEL_PATH, "wb") as fh:
        pickle.dump(model, fh)
    meta = {
        "entraine_le": date.today().isoformat(),
        "seuil_signal_p85_valid": round(seuil, 4),
        "metrics": metrics,
        "features": list(FEATURE_NAMES),
        "label": f"mouvement > {1.645} x sigma_5j sous {LABEL_HORIZON} séances",
        "univers": len(UNIVERSE),
        "limites_connues": [
            "biais de survivance : univers = titres cotés en 2026, appliqué rétroactivement "
            "(métriques modestement optimistes)",
            "test = une seule fenêtre de 5 mois, labels chevauchants : IC95 AUC ~[0.60, 0.65]",
            "l'edge économique net (après thêta + fourchettes) n'est PAS démontré : "
            "utiliser comme signal de timing, pas comme stratégie autonome",
        ],
    }
    META_PATH.write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")

    print(json.dumps(metrics, indent=1))
    print(f"Seuil de signal (p85 validation) : {seuil:.3f}")
    print(f"Modèle sauvé : {MODEL_PATH}")
    return metrics


if __name__ == "__main__":
    train()
