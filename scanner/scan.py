"""Logique de scan réutilisable (CLI et interface web)."""
from .data import normalize_entry
from .detectors import run_all


def run_scan(cfg: dict, watchlist: list, source: str = "yahoo",
             progress=None, journalize: bool = False) -> list:
    """Scanne la watchlist (symboles US ou entrées {symbol, currency, primary})
    et retourne les opportunités triées par score.

    progress(index, symbol, found_count) est appelé avant chaque ticker.
    journalize=True : alimente le journal prospectif (tous les tickers +
    groupes A/B/C), utilisé par le scan quotidien planifié.
    """
    if source == "ib":
        from .data.ibkr import fetch_snapshot
    else:
        from .data.yahoo import fetch_snapshot

    control_symbol = None
    if journalize:
        import random
        from datetime import date
        from . import journal
        # contrôle apparié du jour : choisi par graine de date AVANT le scan
        symbols = [normalize_entry(w)["symbol"] for w in watchlist]
        control_symbol = random.Random(date.today().isoformat()).choice(symbols)

    opportunities = []
    for i, raw in enumerate(watchlist):
        entry = normalize_entry(raw)
        if progress:
            progress(i, entry["symbol"], len(opportunities))
        snap = fetch_snapshot(entry, cfg)
        if snap is None:
            continue
        opportunities.extend(run_all(snap, cfg))
        if journalize:
            from . import journal
            try:
                journal.log_ticker(snap, cfg, source)
                if entry["symbol"] == control_symbol:
                    journal.log_control(snap, cfg, source)
            except Exception as exc:
                print(f"[journal] {entry['symbol']}: erreur ({exc})")

    return sorted(opportunities, key=lambda o: o.score, reverse=True)
