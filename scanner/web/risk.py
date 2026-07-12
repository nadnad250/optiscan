"""Moteur de risque — vérifié AVANT toute préparation d'ordre.

Logique PURE (testable sans IBKR) : reçoit l'état du compte sous forme de
dict et retourne le motif de refus, ou None si l'ordre est acceptable.
L'état du compte est lu en direct chez IBKR par broker.py.
"""

RISK_DEFAULTS = {
    "max_risk_per_trade_pct": 0.25,     # % du capital risqué par trade
    "max_daily_loss_pct": 0.75,         # perte quotidienne max avant blocage
    "max_total_options_risk_pct": 3.0,  # exposition options totale max
    "max_positions": 3,                 # positions simultanées max
    "max_contracts_per_order": 1,       # contrats max par ordre
    "min_excess_liquidity_pct": 10.0,   # liquidité excédentaire min après ordre
    "allow_naked_options": False,       # vente d'options non couvertes interdite
    "allow_market_orders": False,       # ordres au marché interdits
    "allow_earnings_trades": False,     # pas de trade pendant les résultats
}


def risk_config(cfg: dict) -> dict:
    merged = dict(RISK_DEFAULTS)
    merged.update(cfg.get("risk") or {})
    return merged


def check_risk(order: dict, risk: dict, account: dict) -> str | None:
    """Retourne le motif de REFUS, ou None si acceptable.

    order   : {action, side, strike, quantity, price, multiplier, is_covered}
    risk    : configuration de risque (voir RISK_DEFAULTS)
    account : {net_liq, available_funds, excess_liquidity, daily_pnl,
               positions_count, short_put_commitment, options_exposure}
    """
    net_liq = float(account.get("net_liq") or 0)
    if net_liq <= 0:
        return "NetLiquidation indisponible ou nul : impossible d'évaluer le risque"

    qty = int(order["quantity"])
    if qty > risk["max_contracts_per_order"]:
        return (f"quantité {qty} > max_contracts_per_order "
                f"({risk['max_contracts_per_order']})")

    daily_pnl = account.get("daily_pnl")
    if daily_pnl is not None:
        max_daily = net_liq * risk["max_daily_loss_pct"] / 100
        if float(daily_pnl) <= -max_daily:
            return (f"perte quotidienne {daily_pnl:,.0f} atteint la limite "
                    f"(-{max_daily:,.0f}) : plus aucun ordre aujourd'hui")

    if account.get("positions_count", 0) >= risk["max_positions"]:
        return (f"{account['positions_count']} positions ouvertes >= "
                f"max_positions ({risk['max_positions']})")

    mult = int(order.get("multiplier") or 100)
    price = float(order["price"])

    if order["action"] == "BUY":
        trade_risk = price * mult * qty  # perte max d'un achat = la prime
        max_risk = net_liq * risk["max_risk_per_trade_pct"] / 100
        if trade_risk > max_risk:
            return (f"risque du trade {trade_risk:,.0f} > "
                    f"{risk['max_risk_per_trade_pct']}% du capital ({max_risk:,.0f})")
    else:  # SELL
        if not order.get("is_covered") and not risk["allow_naked_options"]:
            return "vente d'option NON couverte interdite (allow_naked_options=false)"
        required = float(order["strike"]) * mult * qty
        available = float(account.get("available_funds") or 0)
        committed = float(account.get("short_put_commitment") or 0)
        if available < required + committed:
            return (f"cash insuffisant pour un put cash-secured : requis "
                    f"{required:,.0f} + engagements existants {committed:,.0f} "
                    f"> fonds disponibles {available:,.0f}")

    exposure = float(account.get("options_exposure") or 0)
    new_exposure = exposure + price * mult * qty
    max_expo = net_liq * risk["max_total_options_risk_pct"] / 100
    if new_exposure > max_expo:
        return (f"exposition options totale {new_exposure:,.0f} > "
                f"{risk['max_total_options_risk_pct']}% du capital ({max_expo:,.0f})")

    return None


def check_whatif(state: dict, risk: dict, net_liq: float) -> str | None:
    """Contrôle du résultat de la simulation WhatIf d'IBKR.

    state : {init_margin_after, equity_with_loan_after, commission}
    """
    init_after = state.get("init_margin_after")
    equity_after = state.get("equity_with_loan_after")
    if init_after is None or equity_after is None:
        return "simulation WhatIf incomplète : ordre refusé par prudence"
    excess_after = float(equity_after) - float(init_after)
    min_excess = net_liq * risk["min_excess_liquidity_pct"] / 100
    if excess_after < min_excess:
        return (f"liquidité excédentaire après ordre {excess_after:,.0f} < "
                f"minimum requis {min_excess:,.0f} "
                f"({risk['min_excess_liquidity_pct']}% du capital)")
    return None
