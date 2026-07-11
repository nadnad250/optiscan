"""Registre des détecteurs d'opportunités."""
from . import directional, earnings, iv_rv, ml_move, optimal, premium, skew, unusual

ALL_DETECTORS = (
    unusual.detect,
    iv_rv.detect,
    skew.detect,
    earnings.detect,
    premium.detect,
    optimal.detect,
    directional.detect,
    ml_move.detect,
)


def run_all(snapshot, cfg) -> list:
    opportunities = []
    for detect in ALL_DETECTORS:
        try:
            opportunities.extend(detect(snapshot, cfg))
        except Exception as exc:
            print(f"[detecteur {detect.__module__}] {snapshot.ticker}: erreur ({exc})")
    return opportunities
