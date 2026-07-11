"""Source de données Yahoo Finance (gratuite, différée ~15 min).

Utilisée par défaut tant qu'IB Gateway n'est pas installé.
"""
from datetime import date, datetime

import numpy as np
import yfinance as yf

from ..models import MarketSnapshot
from . import normalize_entry, pick_spread


def _realized_vol(hist_close) -> float:
    rets = np.log(hist_close / hist_close.shift(1)).dropna()
    if len(rets) < 10:
        return 0.0
    return float(rets.tail(20).std() * np.sqrt(252))


def _earnings_days(tk: yf.Ticker) -> int | None:
    try:
        cal = tk.calendar
        dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if not dates:
            return None
        nxt = min(d for d in dates if isinstance(d, date))
        delta = (nxt - date.today()).days
        return delta if delta >= 0 else None
    except Exception:
        return None


def fetch_snapshot(entry, cfg: dict) -> MarketSnapshot | None:
    """Récupère spot, vol réalisée et chaînes d'options pour un ticker US."""
    entry = normalize_entry(entry)
    ticker = entry["symbol"]
    if entry["currency"] != "USD":
        print(f"[yahoo] {ticker}: Yahoo n'a pas les chaînes d'options européennes "
              f"— utilise la source Interactive Brokers pour ce titre")
        return None
    try:
        tk = yf.Ticker(ticker)
        hist = tk.history(period="1y")  # >= 130 séances requises par les features ML
        if hist.empty:
            print(f"[yahoo] {ticker}: pas d'historique, ignoré")
            return None
        spot = float(hist["Close"].iloc[-1])
        rv = _realized_vol(hist["Close"])

        today = date.today()
        valid = []
        for exp in (tk.options or ()):
            dte = (datetime.strptime(exp, "%Y-%m-%d").date() - today).days
            if cfg["min_dte"] <= dte <= cfg["max_dte"]:
                valid.append((exp, dte))
        chains = []
        for exp, dte in pick_spread(valid, cfg["max_expirations"]):
            try:
                oc = tk.option_chain(exp)
            except Exception as exc:
                print(f"[yahoo] {ticker} {exp}: chaîne illisible ({exc})")
                continue
            chains.append((exp, dte, oc.calls, oc.puts))

        if not chains:
            print(f"[yahoo] {ticker}: aucune échéance dans la fenêtre {cfg['min_dte']}-{cfg['max_dte']}j")
            return None

        return MarketSnapshot(
            ticker=ticker,
            spot=spot,
            realized_vol_20d=rv,
            chains=tuple(chains),
            earnings_days=_earnings_days(tk),
            closes=tuple(float(c) for c in hist["Close"]),
            volumes=tuple(float(v) for v in hist["Volume"]),
        )
    except Exception as exc:
        print(f"[yahoo] {ticker}: erreur ({exc}), ignoré")
        return None
