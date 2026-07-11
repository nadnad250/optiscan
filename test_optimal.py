"""Test synthétique de l'optimiseur : chaîne d'options construite avec
IV 55 % alors que la 'vraie' vol est 35 % → les stratégies à crédit
doivent ressortir avec EV > 0. Vérifie aussi la cohérence des formules.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd

from scanner.config import load_config
from scanner.detectors.optimal import detect
from scanner.models import MarketSnapshot
from scanner.quant.greeks import call_price, prob_above, put_price

SPOT, IV_MARCHE, RV_REELLE, DTE = 100.0, 0.55, 0.30, 30
T = DTE / 365


def fake_chain():
    rows_c, rows_p = [], []
    for k in range(70, 131, 5):
        c = call_price(SPOT, k, IV_MARCHE, T)
        p = put_price(SPOT, k, IV_MARCHE, T)
        rows_c.append({"strike": float(k), "impliedVolatility": IV_MARCHE,
                       "bid": round(c * 0.98, 2), "ask": round(c * 1.02, 2),
                       "lastPrice": round(c, 2), "volume": 1000, "openInterest": 500})
        rows_p.append({"strike": float(k), "impliedVolatility": IV_MARCHE,
                       "bid": round(p * 0.98, 2), "ask": round(p * 1.02, 2),
                       "lastPrice": round(p, 2), "volume": 1000, "openInterest": 500})
    return pd.DataFrame(rows_c), pd.DataFrame(rows_p)


calls, puts = fake_chain()
snap = MarketSnapshot(ticker="TEST", spot=SPOT, realized_vol_20d=RV_REELLE,
                      chains=(("2099-01-01", DTE, calls, puts),), earnings_days=None)

cfg = load_config()
opps = detect(snap, cfg)

print(f"Stratégies retenues : {len(opps)}")
assert opps, "ÉCHEC : IV 55% vs RV 30% devrait produire des stratégies EV+"
for o in opps:
    d = o.details
    print(f"\n[{o.score:.0f}] {o.summary}")
    print(f"    jambes : {d['jambes']}")
    print(f"    EV {d['esperance_gain_EV']:+.3f}$ | edge {d['edge_par_risque']:+.1%} | "
          f"POP {d['prob_profit']:.0%} | R/R {d['ratio_gain_risque']:.2f} | "
          f"annualisé {d['rendement_annualise']:.0%} | demi-Kelly {d['demi_kelly_conseillee']:.1%}")
    assert d["esperance_gain_EV"] > 0
    assert 0 < d["prob_profit"] <= 1

# cohérences de base
assert abs(put_price(SPOT, 100, 0.3, T) + SPOT - 100 * 2.718281828 ** (-0.045 * T)
           - call_price(SPOT, 100, 0.3, T)) < 0.01, "parité call-put violée"
assert prob_above(SPOT, 80, 0.3, T) > 0.9
assert prob_above(SPOT, 120, 0.3, T) < 0.1
print("\nOK : parité call-put et probabilités cohérentes. Maths validées.")
