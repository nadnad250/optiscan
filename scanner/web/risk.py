"""Moteur de risque — vérifié AVANT toute préparation d'ordre.

Logique PURE (testable sans IBKR) : reçoit l'état du compte sous forme de
dict et retourne le motif de refus, ou None si l'ordre est acceptable.
L'état du compte est lu en direct chez IBKR par broker.py.

Règle capitale (revue) : le risque d'un PUT VENDU est le NOTIONNEL COMPLET
strike x multiplicateur x quantité (l'action peut aller à zéro) — la limite
de risque par trade s'applique à ce montant, pas à la prime encaissée.
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
    "max_quote_spread_pct": 35.0,       # fourchette bid-ask max acceptée (%)
}


def risk_config(cfg: dict) -> dict:
    merged = dict(RISK_DEFAULTS)
    merged.update(cfg.get("risk") or {})
    return merged


def trade_risk_amount(order: dict) -> float:
    """Risque RÉEL du trade en devise du contrat.

    BUY  : la prime payée (perte max).
    SELL : le notionnel complet strike x multiplicateur x quantité
           (un put vendu peut coûter tout le strike si l'action va à zéro).
    """
    mult = int(order.get("multiplier") or 100)
    qty = int(order["quantity"])
    if order["action"] == "BUY":
        return float(order["price"]) * mult * qty
    return float(order["strike"]) * mult * qty


def check_risk(order: dict, risk: dict, account: dict) -> str | None:
    """Retourne le motif de REFUS, ou None si acceptable.

    order   : {action, side, right, strike, quantity, price, multiplier,
               currency, is_covered}
    risk    : configuration de risque (voir RISK_DEFAULTS)
    account : {net_liq, available_funds, cash_in_currency, daily_pnl,
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

    # ---- limite de risque par trade : s'applique à TOUT ordre, y compris
    # le notionnel complet d'un put vendu (revue : point n°1)
    risque = trade_risk_amount(order)
    max_risk = net_liq * risk["max_risk_per_trade_pct"] / 100
    if risque > max_risk:
        return (f"risque du trade {risque:,.0f} ({'prime' if order['action'] == 'BUY' else 'notionnel du put vendu'}) "
                f"> {risk['max_risk_per_trade_pct']}% du capital ({max_risk:,.0f})")

    if order["action"] == "SELL":
        if not order.get("is_covered") and not risk["allow_naked_options"]:
            return "vente d'option NON couverte interdite (allow_naked_options=false)"
        # ---- cash RÉEL dans la devise du contrat (revue : point n°2)
        cash = account.get("cash_in_currency")
        if cash is None:
            return (f"cash en {order.get('currency', '?')} indisponible : "
                    f"impossible de vérifier la couverture — ordre refusé")
        required = float(order["strike"]) * int(order.get("multiplier") or 100) * qty
        committed = float(account.get("short_put_commitment") or 0)
        if float(cash) < required + committed:
            return (f"cash {order.get('currency', '')} insuffisant : requis "
                    f"{required:,.0f} + engagements existants {committed:,.0f} "
                    f"> disponible {float(cash):,.0f}")

    exposure = float(account.get("options_exposure") or 0)
    new_exposure = exposure + risque
    max_expo = net_liq * risk["max_total_options_risk_pct"] / 100
    if new_exposure > max_expo:
        return (f"exposition options totale {new_exposure:,.0f} > "
                f"{risk['max_total_options_risk_pct']}% du capital ({max_expo:,.0f})")

    return None


def check_quote(quote: dict, order: dict, risk: dict) -> str | None:
    """Contrôle de la quote RE-DEMANDÉE juste avant l'ordre (revue : n°6/12).

    quote : {bid, ask, age_seconds, data_type}
    """
    bid, ask = quote.get("bid"), quote.get("ask")
    if not bid or not ask or bid <= 0 or ask <= 0:
        return "quote incomplète (bid ou ask absent) : ordre refusé"
    if ask < bid:
        return f"quote croisée (bid {bid} > ask {ask}) : données corrompues, refus"
    mid = (bid + ask) / 2
    spread_pct = (ask - bid) / mid * 100
    if spread_pct > risk["max_quote_spread_pct"]:
        return (f"fourchette {spread_pct:.0f}% > max accepté "
                f"{risk['max_quote_spread_pct']}% : trop illiquide")
    price = float(order["price"])
    if order["action"] == "BUY" and price > ask * 1.05:
        return (f"prix limite {price} trop au-dessus de l'ask actuel {ask} "
                f"(quote périmée côté interface ?)")
    if order["action"] == "SELL" and price < bid * 0.90:
        return (f"prix limite {price} trop en-dessous du bid actuel {bid} "
                f"(tu brades : quote périmée côté interface ?)")
    return None


def check_whatif(state: dict, risk: dict, net_liq: float) -> str | None:
    """Contrôle du résultat de la simulation WhatIf d'IBKR."""
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
