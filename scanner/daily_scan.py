"""Scan quotidien planifié — protocole d'évaluation prospective GELÉ.

Usage : python -m scanner.daily_scan [--force] [--tickers A,B,C]

Règles (fixées avant l'expérience, ne pas modifier en cours de route) :
- Lun-Ven à 09:45 heure de New York (15 min après l'ouverture : les
  fourchettes bid/ask de l'ouverture sont trop bruitées)
- jours fériés US détectés automatiquement (pas de bougie SPY du jour)
- TOUS les tickers sont journalisés, signaux acceptés ou non
- résultats capturés à +1, +3 et +5 séances (horizons figés)
- ni le modèle, ni le seuil, ni les features ne changent pendant l'essai
"""
import argparse
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import load_config
from .journal import update_outcomes
from .report import save_json
from .scan import run_scan

NY = ZoneInfo("America/New_York")
# Fenêtre étroite : les deux crons UTC (13h45 + 14h45, pour couvrir été/hiver)
# ne peuvent jamais tomber tous les deux dedans -> pas de double scan
WINDOW = ((9, 40), (10, 30))   # fenêtre de tir acceptée, heure de New York


def _market_open_today() -> bool:
    """Jour férié US ? SPY n'a pas de bougie du jour -> marché fermé."""
    try:
        import yfinance as yf
        hist = yf.Ticker("SPY").history(period="5d")
        if hist.empty:
            return False
        last = hist.index[-1].tz_convert(NY).date()
        return last == datetime.now(NY).date()
    except Exception:
        return True  # en cas de doute, on tente le scan (la ligne sera marquée)


def main():
    parser = argparse.ArgumentParser(description="Scan quotidien OptiScan (journal prospectif)")
    parser.add_argument("--force", action="store_true",
                        help="ignorer la fenêtre horaire et le calendrier (test)")
    parser.add_argument("--tickers", help="liste réduite pour test (ex: SOFI,MARA)")
    args = parser.parse_args()

    now = datetime.now(NY)
    print(f"=== Scan quotidien — {now:%Y-%m-%d %H:%M} (New York) ===")

    if not args.force:
        if now.weekday() >= 5:
            print("Week-end : pas de scan.")
            return
        hm = (now.hour, now.minute)
        if not (WINDOW[0] <= hm <= WINDOW[1]):
            print(f"Hors fenêtre {WINDOW[0][0]}:{WINDOW[0][1]:02d}-"
                  f"{WINDOW[1][0]}:{WINDOW[1][1]:02d} NY : pas de scan.")
            return
        if not _market_open_today():
            print("Jour férié US (pas de bougie SPY du jour) : pas de scan.")
            return
        from .journal import already_logged_today
        if already_logged_today():
            print("Journal déjà alimenté aujourd'hui : pas de second scan.")
            return

    cfg = load_config()
    watchlist = ([t.strip().upper() for t in args.tickers.split(",")]
                 if args.tickers else cfg["watchlist"])

    # IBKR d'abord (si TWS tourne), sinon Yahoo — la source est journalisée
    opportunities = None
    for source in ("ib", "yahoo"):
        try:
            opportunities = run_scan(
                cfg, watchlist, source, journalize=True,
                progress=lambda i, t, n: print(f"  [{i + 1}/{len(watchlist)}] {t}"))
            print(f"Source utilisée : {source}")
            break
        except RuntimeError as exc:
            print(f"[{source}] indisponible ({exc}) — bascule…")
    if opportunities is None:
        print("Aucune source de données disponible.")
        return

    from pathlib import Path
    out = save_json(opportunities, Path(__file__).resolve().parent.parent / "output")
    print(f"{len(opportunities)} opportunités — export {out.name}")

    res = update_outcomes()
    print(f"Résultats capturés : {res['maj']} lignes mises à jour, "
          f"{res['completes']} complétées (+5 séances)")


if __name__ == "__main__":
    main()
