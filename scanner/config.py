"""Chargement de la configuration (config.json à la racine du projet)."""
import json
from pathlib import Path

DEFAULTS = {
    "watchlist": [
        # grandes valeurs US (référence marché)
        "AAPL", "NVDA", "TSLA", "AMD", "META", "SPY", "QQQ", "COIN", "PLTR",
        # actions US pas chères (options liquides, capital requis faible)
        "SOFI", "MARA", "F", "T", "PFE", "AAL", "NIO", "INTC",
        "PLUG", "RIVN", "LCID", "SNAP", "WBD", "VALE", "PBR", "GOLD",
        "CLF", "CCL", "RIOT", "TLRY", "BAC", "AGNC", "KVUE",
        # Europe (via Interactive Brokers uniquement, devise EUR)
        {"symbol": "DTE", "currency": "EUR", "primary": "IBIS"},    # Deutsche Telekom
        {"symbol": "LHA", "currency": "EUR", "primary": "IBIS"},    # Lufthansa
        {"symbol": "EOAN", "currency": "EUR", "primary": "IBIS"},   # E.ON
        {"symbol": "CBK", "currency": "EUR", "primary": "IBIS"},    # Commerzbank
        {"symbol": "TEF", "currency": "EUR", "primary": "BM"},      # Telefonica
        {"symbol": "SAN", "currency": "EUR", "primary": "BM"},      # Santander
        {"symbol": "IBE", "currency": "EUR", "primary": "BM"},      # Iberdrola
        {"symbol": "ORA", "currency": "EUR", "primary": "SBF"},     # Orange
        {"symbol": "ENGI", "currency": "EUR", "primary": "SBF"},    # Engie
        {"symbol": "AF", "currency": "EUR", "primary": "SBF"},      # Air France-KLM
        {"symbol": "ENEL", "currency": "EUR", "primary": "BVME"},   # Enel
        {"symbol": "ISP", "currency": "EUR", "primary": "BVME"},    # Intesa Sanpaolo
    ],
    "max_expirations": 3,          # nb d'échéances analysées par ticker
    "min_dte": 5,                  # jours min avant expiration
    "max_dte": 60,                 # jours max avant expiration
    "unusual_vol_oi_ratio": 3.0,   # volume / open interest pour "activité inhabituelle"
    "unusual_min_volume": 500,     # volume minimum du contrat
    "unusual_min_oi": 20,          # OI minimal (écarte les données pré-ouverture corrompues)
    "unusual_min_premium_usd": 50_000,   # flux de prime minimum (abaissé pour les actions pas chères)
    "iv_rv_rich": 1.35,            # IV/RV au-dessus => prime chère (vendre)
    "iv_rv_cheap": 0.80,           # IV/RV en dessous => options bon marché (acheter)
    "skew_extreme": 12.0,          # écart (points d'IV) put25d - call25d jugé extrême
    "earnings_max_days": 10,       # fenêtre earnings
    "csp_target_delta": 0.30,      # delta cible pour put cash-secured
    "dir_min_signal": 0.32,        # convergence minimale des signaux directionnels [-1..1]
    "dir_max_premium": 3.0,        # prime max par action (3 $ = 300 $/contrat) — options pas chères
    "dir_target_delta": 0.40,      # delta cible du call/put acheté
    "dir_min_delta": 0.25,         # delta minimal (écarte les tickets de loterie)
    "ml_min_ratio": 1.5,           # prob. modèle / prob. facturée par les options minimale
    "opt_min_edge": 0.03,          # EV/risque max minimal (3 %) pour l'optimiseur
    "opt_min_pop": 0.62,           # prob. de profit minimale (stratégies à crédit)
    "opt_max_liq": 0.35,           # pénalité de liquidité max (spread bid-ask / mid)
    "opt_top_per_ticker": 2,       # nb de stratégies optimales gardées par ticker
    "top_n": 15,                   # nb d'opportunités affichées
    "ib_host": "127.0.0.1",
    "ib_port": 7497,               # 7497 = paper TWS, 4002 = paper Gateway
    "ib_client_id": 42,
}


def load_config(path: str | None = None) -> dict:
    cfg = dict(DEFAULTS)
    cfg_path = Path(path) if path else Path(__file__).resolve().parent.parent / "config.json"
    if cfg_path.exists():
        try:
            user_cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            cfg.update({k: v for k, v in user_cfg.items() if k in DEFAULTS})
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[config] fichier {cfg_path} illisible ({exc}), valeurs par défaut utilisées")
    return cfg
