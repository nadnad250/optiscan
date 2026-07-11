"""Point d'entrée : python -m scanner.main [--source yahoo|ib] [--tickers AAPL,TSLA]"""
import argparse
from pathlib import Path

from .config import load_config
from .report import print_report, save_json
from .scan import run_scan


def parse_args():
    p = argparse.ArgumentParser(description="OptiScan — scanner d'opportunités options (alertes uniquement)")
    p.add_argument("--source", choices=["yahoo", "ib"], default="ib",
                   help="ib = Interactive Brokers (défaut, TWS/Gateway requis), yahoo = secours gratuit/différé")
    p.add_argument("--tickers", help="liste séparée par des virgules (sinon watchlist du config.json)")
    p.add_argument("--config", help="chemin d'un config.json alternatif")
    p.add_argument("--top", type=int, help="nombre d'opportunités affichées")
    return p.parse_args()


def main():
    args = parse_args()
    cfg = load_config(args.config)
    if args.top:
        cfg["top_n"] = args.top
    watchlist = [t.strip().upper() for t in args.tickers.split(",")] if args.tickers else cfg["watchlist"]

    print(f"Scan de {len(watchlist)} tickers ({args.source})...")
    opportunities = run_scan(
        cfg, watchlist, args.source,
        progress=lambda i, t, n: print(f"  [{i + 1}/{len(watchlist)}] {t} ({n} signaux cumulés)"),
    )
    print_report(opportunities, cfg["top_n"], args.source)
    out = save_json(opportunities, Path(__file__).resolve().parent.parent / "output")
    print(f"Export JSON : {out}")


if __name__ == "__main__":
    main()
