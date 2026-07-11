"""Source de données Interactive Brokers (via TWS ou IB Gateway).

Prérequis :
  pip install ib_async
  TWS/Gateway lancé et connecté, API activée :
  Fichier > Configuration globale > API > Paramètres >
  cocher "Activer les clients ActiveX et Socket" (+ "API en lecture seule" conseillé).

Le port est détecté automatiquement : 7497 (TWS paper), 7496 (TWS réel),
4002 (Gateway paper), 4001 (Gateway réel).
Sans abonnement OPRA, l'API bascule sur les données différées (~15 min).
"""
import asyncio
import math
import threading
from datetime import date, datetime

import numpy as np
import pandas as pd

from ..models import MarketSnapshot
from . import normalize_entry, pick_spread

PORTS_AUTO = (7497, 7496, 4002, 4001)   # paper d'abord, par sécurité
BATCH_SIZE = 40                          # limite de lignes de données simultanées
WAIT_SECONDS = 5                         # temps laissé aux ticks pour arriver

_ib = None
_ib_thread = None


def _connect(cfg: dict):
    """Connexion partagée (par thread), avec détection automatique du port."""
    global _ib, _ib_thread
    tid = threading.get_ident()
    if _ib is not None and _ib.isConnected() and _ib_thread == tid:
        return _ib
    if _ib is not None:  # connexion d'un autre thread : on repart proprement
        try:
            _ib.disconnect()
        except Exception:
            pass
        _ib = None

    # chaque thread (scan web) doit avoir sa propre boucle asyncio
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    try:
        from ib_async import IB
    except ImportError as exc:
        raise RuntimeError("ib_async n'est pas installé : pip install ib_async") from exc

    ports = [cfg["ib_port"]] + [p for p in PORTS_AUTO if p != cfg["ib_port"]]
    last_error = None
    for port in ports:
        ib = IB()
        try:
            ib.connect(cfg["ib_host"], port, clientId=cfg["ib_client_id"], timeout=6)
            mode = "PAPER" if port in (7497, 4002) else "RÉEL"
            print(f"[ibkr] connecté sur le port {port} ({mode})")
            # 4 = différé-gelé : temps réel si abonné, sinon différé (~15 min),
            # et dernières valeurs connues quand le marché est fermé
            ib.reqMarketDataType(4)
            _ib = ib
            _ib_thread = tid
            return ib
        except Exception as exc:
            last_error = exc
            ib.disconnect()
    raise RuntimeError(
        f"Impossible de joindre TWS/Gateway sur {cfg['ib_host']} "
        f"(ports essayés : {ports}). Lance TWS, connecte-toi et active l'API. "
        f"Dernière erreur : {last_error}"
    )


def _earnings_days_via_yahoo(ticker: str, currency: str) -> int | None:
    """Date des prochains résultats via Yahoo (fiable pour les dates,
    contrairement à ses quotes pré-ouverture). Indispensable pour ne pas
    proposer une stratégie qui enjambe des earnings sans le signaler.
    Non disponible pour les titres européens (symboles Yahoo différents)."""
    if currency != "USD":
        return None
    try:
        import yfinance as yf
        from .yahoo import _earnings_days
        return _earnings_days(yf.Ticker(ticker))
    except Exception:
        return None


def _realized_vol(closes: list[float]) -> float:
    arr = np.array(closes, dtype=float)
    rets = np.diff(np.log(arr))
    if len(rets) < 10:
        return 0.0
    return float(np.std(rets[-20:]) * math.sqrt(252))


def _row_from_ticker(tk) -> dict:
    g = tk.modelGreeks
    right = tk.contract.right
    oi = tk.callOpenInterest if right == "C" else tk.putOpenInterest
    last = tk.last if tk.last and tk.last > 0 else (tk.close if tk.close and tk.close > 0 else 0.0)
    return {
        "strike": tk.contract.strike,
        "impliedVolatility": g.impliedVol if g and g.impliedVol and not math.isnan(g.impliedVol) else float("nan"),
        "bid": tk.bid if tk.bid and tk.bid > 0 else 0.0,
        "ask": tk.ask if tk.ask and tk.ask > 0 else 0.0,
        "lastPrice": last,
        "volume": int(tk.volume) if tk.volume and not math.isnan(tk.volume) else 0,
        "openInterest": int(oi) if oi and not math.isnan(oi) else 0,
    }


def _expiry_contracts(ib, symbol: str, expiry: str, trading_class: str,
                      spot: float, exchange: str, currency: str) -> list:
    """Contrats réellement cotés pour une échéance, strikes ±20 % du spot."""
    from ib_async import Option
    wildcard = Option(symbol, expiry, exchange=exchange, currency=currency,
                      tradingClass=trading_class)
    details = ib.reqContractDetails(wildcard)
    return [d.contract for d in details
            if 0.80 * spot <= d.contract.strike <= 1.20 * spot]


def _chain_frames(ib, contracts: list):
    """Chaîne calls/puts (colonnes compatibles yahoo).

    Chaque contrat est souscrit avec les ticks génériques 100/101/106
    (volume options, open interest, IV) puis désabonné, par lots pour
    respecter la limite de lignes de données simultanées.
    """
    rows = {"C": [], "P": []}
    for i in range(0, len(contracts), BATCH_SIZE):
        batch = contracts[i:i + BATCH_SIZE]
        tickers = [ib.reqMktData(c, "100,101,106", False, False) for c in batch]
        ib.sleep(WAIT_SECONDS)
        for tk in tickers:
            rows[tk.contract.right].append(_row_from_ticker(tk))
            ib.cancelMktData(tk.contract)
    return pd.DataFrame(rows["C"]), pd.DataFrame(rows["P"])


def fetch_snapshot(entry, cfg: dict) -> MarketSnapshot | None:
    entry = normalize_entry(entry)
    ticker, currency = entry["symbol"], entry["currency"]
    label = ticker if currency == "USD" else f"{ticker} ({currency})"
    try:
        from ib_async import Stock
        ib = _connect(cfg)

        stock = Stock(ticker, "SMART", currency,
                      primaryExchange=entry["primary"] or "")
        if not ib.qualifyContracts(stock):
            print(f"[ibkr] {label}: contrat introuvable")
            return None

        # >= 130 séances requises par les features ML.
        # ADJUSTED_LAST (dividendes ajustés) pour rester cohérent avec les
        # données d'entraînement Yahoo auto_adjust (audit : skew train/prod)
        bars = ib.reqHistoricalData(stock, "", "12 M", "1 day", "ADJUSTED_LAST", useRTH=True)
        if not bars:
            bars = ib.reqHistoricalData(stock, "", "12 M", "1 day", "TRADES", useRTH=True)
        if not bars:
            print(f"[ibkr] {label}: pas d'historique")
            return None
        closes = [b.close for b in bars]
        volumes = [float(b.volume) if b.volume and b.volume > 0 else 0.0 for b in bars]
        spot = closes[-1]

        params = ib.reqSecDefOptParams(stock.symbol, "", stock.secType, stock.conId)
        # US : SMART disponible ; Europe : prendre la place avec le plus d'échéances
        chain_def = next((p for p in params if p.exchange == "SMART"), None)
        if chain_def is None and params:
            chain_def = max(params, key=lambda p: len(p.expirations))
        if chain_def is None:
            print(f"[ibkr] {label}: pas de chaîne d'options")
            return None

        today = date.today()
        valid = []
        for expiry in sorted(chain_def.expirations):
            dte = (datetime.strptime(expiry, "%Y%m%d").date() - today).days
            if cfg["min_dte"] <= dte <= cfg["max_dte"]:
                valid.append((expiry, dte))
        chains = []
        for expiry, dte in pick_spread(valid, cfg["max_expirations"]):
            contracts = _expiry_contracts(ib, stock.symbol, expiry,
                                          chain_def.tradingClass, spot,
                                          chain_def.exchange, currency)
            if not contracts:
                continue
            calls, puts = _chain_frames(ib, contracts)
            chains.append((expiry, dte, calls, puts))

        if not chains:
            print(f"[ibkr] {label}: aucune échéance dans la fenêtre "
                  f"{cfg['min_dte']}-{cfg['max_dte']}j")
            return None

        return MarketSnapshot(
            ticker=label,
            spot=spot,
            realized_vol_20d=_realized_vol(closes),
            chains=tuple(chains),
            earnings_days=_earnings_days_via_yahoo(ticker, currency),
            closes=tuple(float(c) for c in closes),
            volumes=tuple(volumes),
        )
    except RuntimeError:
        raise  # erreur de connexion : inutile de continuer ticker par ticker
    except Exception as exc:
        print(f"[ibkr] {ticker}: erreur ({exc})")
        return None
