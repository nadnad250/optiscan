"""Ingénierie des features — UNE SEULE implémentation, partagée entre
l'entraînement et la détection en production (aucun écart train/inférence).

Toutes les features regardent STRICTEMENT vers le passé (indice t inclus) :
aucune information future ne peut fuir dans le calcul.
"""
import math

import numpy as np

MIN_HISTORY = 130          # jours de clôture minimum pour calculer les features
LABEL_HORIZON = 5          # horizon du label : grand mouvement sous 5 séances
LABEL_K = 1.645            # seuil : mouvement > 1.645 x sigma_5j "normal"

FEATURE_NAMES = (
    "rv5_sur_rv20", "rv20_sur_rv60", "rv20_pctl_1an",
    "bollinger_width", "bbw_pctl_6mois", "compression_5_60",
    "max_abs_ret_5j", "abs_ret_veille", "jours_depuis_choc",
    "rsi14", "mom5", "mom20", "mom60",
    "dist_haut_20j", "dist_bas_20j", "dist_haut_60j", "dist_bas_60j",
    "autocorr_1j", "skew_20j", "kurt_20j",
    "volume_5_sur_60", "volume_5_sur_20", "log_dollar_volume",
)


def _std(x: np.ndarray) -> float:
    return float(np.std(x, ddof=1)) if len(x) > 2 else 0.0


def _rsi14(closes: np.ndarray) -> float:
    diffs = np.diff(closes)
    n = 14
    if len(diffs) < n + 1:
        return 50.0
    avg_gain = float(np.clip(diffs[:n], 0, None).mean())
    avg_loss = float(np.clip(-diffs[:n], 0, None).mean())
    for d in diffs[n:]:
        avg_gain = (avg_gain * (n - 1) + max(d, 0.0)) / n
        avg_loss = (avg_loss * (n - 1) + max(-d, 0.0)) / n
    if avg_loss <= 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def compute_features(closes, volumes) -> list | None:
    """Vecteur de features à la dernière date de `closes`. None si historique court."""
    c = np.asarray(closes, dtype=float)
    v = np.asarray(volumes, dtype=float) if volumes is not None and len(volumes) == len(c) \
        else np.ones_like(c)
    if len(c) < MIN_HISTORY or np.any(c[-MIN_HISTORY:] <= 0):
        return None

    r = np.diff(np.log(c))
    spot = c[-1]

    rv5 = _std(r[-5:]) * math.sqrt(252)
    rv20 = _std(r[-20:]) * math.sqrt(252)
    rv60 = _std(r[-60:]) * math.sqrt(252)
    if rv20 <= 1e-6 or rv60 <= 1e-6:
        return None

    # percentile de la vol 20j sur ~1 an
    rv20_hist = [_std(r[i - 20:i]) for i in range(max(21, len(r) - 231), len(r) + 1, 5)]
    rv20_now = _std(r[-20:])
    rv20_pctl = float(np.mean([h <= rv20_now for h in rv20_hist])) if rv20_hist else 0.5

    sma20 = float(c[-20:].mean())
    bbw = 4 * float(np.std(c[-20:], ddof=1)) / sma20 if sma20 > 0 else 0.0
    bbw_hist = []
    for i in range(max(20, len(c) - 126), len(c) + 1, 3):
        w = c[i - 20:i]
        m = float(w.mean())
        if m > 0:
            bbw_hist.append(4 * float(np.std(w, ddof=1)) / m)
    bbw_pctl = float(np.mean([h <= bbw for h in bbw_hist])) if bbw_hist else 0.5

    abs_r = np.abs(r)
    compression = float(abs_r[-5:].mean() / abs_r[-60:].mean()) if abs_r[-60:].mean() > 0 else 1.0

    sigma_daily = rv20 / math.sqrt(252)
    chocs = np.where(abs_r > 2 * sigma_daily)[0]
    jours_depuis_choc = float(len(r) - 1 - chocs[-1]) if len(chocs) else 252.0

    r20 = r[-20:]
    mu, sd = float(r20.mean()), _std(r20)
    skew = float(np.mean(((r20 - mu) / sd) ** 3)) if sd > 0 else 0.0
    kurt = float(np.mean(((r20 - mu) / sd) ** 4) - 3) if sd > 0 else 0.0
    ac = float(np.corrcoef(r[-21:-1], r[-20:])[0, 1]) if _std(r[-21:]) > 0 else 0.0

    v5, v20, v60 = float(v[-5:].mean()), float(v[-20:].mean()), float(v[-60:].mean())

    return [
        rv5 / rv20, rv20 / rv60, rv20_pctl,
        bbw, bbw_pctl, compression,
        float(abs_r[-5:].max()), float(abs_r[-1]), min(jours_depuis_choc, 252.0),
        _rsi14(c), spot / c[-6] - 1, spot / c[-21] - 1, spot / c[-61] - 1,
        spot / float(c[-20:].max()) - 1, spot / float(c[-20:].min()) - 1,
        spot / float(c[-60:].max()) - 1, spot / float(c[-60:].min()) - 1,
        0.0 if math.isnan(ac) else ac, skew, kurt,
        v5 / v60 if v60 > 0 else 1.0, v5 / v20 if v20 > 0 else 1.0,
        math.log10(max(spot * v20, 1.0)),
    ]


def compute_label(closes, t: int) -> int | None:
    """Label au jour t : 1 si un mouvement > LABEL_K x sigma_5j survient
    dans les LABEL_HORIZON séances suivantes (max intra-fenêtre, pas seulement
    le point final). None si la fenêtre future est incomplète."""
    c = np.asarray(closes, dtype=float)
    if t + LABEL_HORIZON >= len(c) or t < MIN_HISTORY:
        return None
    r = np.diff(np.log(c[: t + 1]))
    sigma5 = _std(r[-20:]) * math.sqrt(LABEL_HORIZON)
    if sigma5 <= 1e-6:
        return None
    future = c[t + 1: t + 1 + LABEL_HORIZON]
    max_move = float(np.max(np.abs(future / c[t] - 1)))
    return int(max_move > LABEL_K * sigma5)
