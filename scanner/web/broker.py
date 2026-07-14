"""Préparation d'ordres dans TWS — JAMAIS d'envoi direct, JAMAIS sans contrôle.

Garanties (P0 + revue n°2) :
- transmit=False TOUJOURS ; ordre INACTIF tant que l'utilisateur ne clique
  pas « Transmettre » dans TWS
- séparation stricte PAPER/LIVE (ports dédiés + préfixe de compte vérifié)
- compte EXPLICITE : si plusieurs comptes gérés, ib_account doit être défini
- en LIVE : refus si le P&L ou les données de compte sont indisponibles
- earnings : ordre refusé si des résultats tombent avant l'expiration
  (allow_earnings_trades=false)
- quote RE-DEMANDÉE juste avant l'ordre : bid/ask frais, non croisés,
  fourchette bornée, prix limite cohérent
- risque : notionnel complet des puts vendus, cash dans la DEVISE du contrat
- WhatIf IBKR (marges, commission) avant tout ordre
- idempotence ATOMIQUE (SQLite, survit aux redémarrages)
- statuts/erreurs : liste blanche stricte — tout code inattendu annule l'ordre
- kill switch : bloque les préparations ET peut annuler les ordres préparés
"""
import asyncio
import math
from datetime import date, datetime
from pathlib import Path

from . import ledger

BASE_DIR = Path(__file__).resolve().parent.parent.parent
KILL_SWITCH = BASE_DIR / "output" / "KILL_SWITCH"

PORTS_BY_MODE = {"paper": (7497, 4002), "live": (7496, 4001)}
LIVE_CONFIRMATION = "JE COMPRENDS LES RISQUES DU TRADING REEL"
VALID_RIGHTS = {"call": "C", "put": "P"}
VALID_ACTIONS = {"BUY", "SELL"}

# codes IBKR bénins (messages d'information sur les fermes de données) :
# TOUT autre code d'erreur pendant la préparation => annulation + échec
BENIGN_ERROR_CODES = {399, 2103, 2104, 2105, 2106, 2107, 2108, 2119, 2158,
                      10090, 10091, 10167}


def _validate(req: dict, max_qty: int) -> dict | str:
    """Valide et normalise la demande. Retourne un dict propre ou une erreur."""
    try:
        symbol = str(req["symbol"]).strip().upper()
        right = VALID_RIGHTS.get(str(req["side"]).lower())
        action = str(req.get("action", "BUY")).upper()
        strike = float(req["strike"])
        quantity = int(req.get("quantity", 1))
        price = round(float(req["limit_price"]), 2)
        currency = str(req.get("currency", "USD")).upper()
        expiry = str(req["expiry"]).replace("-", "")
        expiry_date = datetime.strptime(expiry, "%Y%m%d").date()
    except (KeyError, ValueError, TypeError) as exc:
        return f"demande invalide : {exc}"
    if not symbol or not symbol.isalnum():
        return "symbole invalide"
    if right is None:
        return "type d'option invalide (call/put)"
    if action not in VALID_ACTIONS:
        return "action invalide (BUY/SELL)"
    if strike <= 0:
        return "strike invalide"
    if expiry_date <= date.today():
        return "expiration passée ou aujourd'hui : refus"
    if not (1 <= quantity <= max_qty):
        return f"quantité invalide (1 à {max_qty} contrat(s))"
    if not (0.01 <= price <= 10_000):
        return "prix limite invalide"
    return {"symbol": symbol, "right": right, "action": action, "strike": strike,
            "quantity": quantity, "price": price, "currency": currency,
            "expiry": expiry, "dte": (expiry_date - date.today()).days}


def _intent_id(clean: dict) -> str:
    return (f"{clean['symbol']}-{clean['expiry']}-{clean['strike']:g}-"
            f"{clean['right']}-{clean['action']}-{date.today().isoformat()}")


def _select_account(ib, cfg: dict) -> tuple[str | None, str | None]:
    """Compte EXPLICITE (revue n°5). Retourne (account, erreur)."""
    accounts = ib.managedAccounts()
    wanted = str(cfg.get("ib_account") or "").strip()
    if wanted:
        if wanted not in accounts:
            return None, (f"compte configuré {wanted} introuvable parmi les "
                          f"comptes gérés {accounts}")
        return wanted, None
    if len(accounts) == 1:
        return accounts[0], None
    return None, (f"plusieurs comptes gérés {accounts} : définis \"ib_account\" "
                  f"explicitement dans config.json")


def _earnings_gate(clean: dict, risk: dict, mode: str) -> str | None:
    """Refus si des résultats tombent avant l'expiration (revue n°4).

    Titres non-US (dates invérifiables automatiquement) : REFUS en live,
    simple avertissement en paper — le paper sert justement à s'entraîner.
    """
    if risk["allow_earnings_trades"]:
        return None
    if clean["currency"] != "USD":
        if mode == "live":
            return ("date de résultats non vérifiable pour ce titre non-US : "
                    "ordre refusé en LIVE (allow_earnings_trades=false)")
        return None  # paper : autorisé, un avertissement est joint à la réponse
    try:
        import yfinance as yf
        from ..data.yahoo import _earnings_days
        days = _earnings_days(yf.Ticker(clean["symbol"]))
    except Exception:
        days = None
    if days is None:
        return None  # pas d'annonce connue dans la fenêtre : acceptable
    if days <= clean["dte"]:
        return (f"résultats de {clean['symbol']} dans {days}j, AVANT "
                f"l'expiration ({clean['dte']}j) : refusé "
                f"(allow_earnings_trades=false)")
    return None


def _fresh_quote(ib, contract) -> dict:
    """Re-demande la quote du contrat MAINTENANT (revue n°6/12)."""
    ib.reqMarketDataType(4)
    tk = ib.reqMktData(contract, "", False, False)
    ib.sleep(3)
    quote = {
        "bid": float(tk.bid) if tk.bid and tk.bid > 0 else None,
        "ask": float(tk.ask) if tk.ask and tk.ask > 0 else None,
        "data_type": {1: "temps réel", 2: "gelé", 3: "différé", 4: "différé-gelé"}.get(
            tk.marketDataType, str(tk.marketDataType)),
        "horodatage": datetime.now().isoformat(timespec="seconds"),
    }
    ib.cancelMktData(contract)
    return quote


def _account_state(ib, account: str, currency: str) -> dict:
    """État RÉEL du compte, cash dans la DEVISE du contrat (revue n°2)."""
    tags = {}
    for row in ib.accountSummary(account):
        if row.tag in ("NetLiquidation", "AvailableFunds", "ExcessLiquidity", "Cushion"):
            try:
                tags[row.tag] = float(row.value)
            except ValueError:
                pass

    cash_in_currency = None
    try:
        for v in ib.accountValues(account):
            if v.tag == "TotalCashBalance" and v.currency == currency:
                cash_in_currency = float(v.value)
                break
    except Exception:
        pass
    if cash_in_currency is None and currency == "USD":
        # secours raisonnable pour la devise de base uniquement
        cash_in_currency = tags.get("AvailableFunds")

    positions = [p for p in ib.positions(account) if p.position]
    short_put_commitment = 0.0
    options_exposure = 0.0
    for p in positions:
        c = p.contract
        if c.secType == "OPT":
            mult = int(c.multiplier or 100)
            options_exposure += abs(p.position) * float(p.avgCost or 0)
            if c.right == "P" and p.position < 0:
                short_put_commitment += abs(p.position) * float(c.strike) * mult

    for tr in ib.openTrades():
        c, o = tr.contract, tr.order
        if (c.secType == "OPT" and c.right == "P" and o.action == "SELL"
                and tr.orderStatus.status not in ("Filled", "Cancelled", "Inactive")):
            short_put_commitment += float(c.strike) * int(c.multiplier or 100) * o.totalQuantity

    daily_pnl = None
    try:
        pnl = ib.reqPnL(account)
        ib.sleep(1.5)
        if pnl.dailyPnL is not None and not math.isnan(pnl.dailyPnL):
            daily_pnl = float(pnl.dailyPnL)
        ib.cancelPnL(account)
    except Exception:
        pass

    return {
        "net_liq": tags.get("NetLiquidation"),
        "available_funds": tags.get("AvailableFunds"),
        "excess_liquidity": tags.get("ExcessLiquidity"),
        "cushion": tags.get("Cushion"),
        "cash_in_currency": cash_in_currency,
        "daily_pnl": daily_pnl,
        "positions_count": len(positions),
        "short_put_commitment": short_put_commitment,
        "options_exposure": options_exposure,
    }


def stage_order(req: dict, cfg: dict, dry_run: bool = False) -> dict:
    """Prépare un ordre limite INACTIF dans TWS, après TOUS les contrôles."""
    from .risk import check_quote, check_risk, check_whatif, risk_config

    if not cfg.get("enable_order_staging", False):
        return {"ok": False, "error": "préparation d'ordres DÉSACTIVÉE "
                "(enable_order_staging=false dans config.json)"}
    if KILL_SWITCH.exists():
        return {"ok": False, "error": "KILL SWITCH actif : aucune préparation d'ordre."}
    mode = str(cfg.get("trading_mode", "paper")).lower()
    if mode not in PORTS_BY_MODE:
        return {"ok": False, "error": f"trading_mode invalide : {mode}"}
    if mode == "live" and cfg.get("live_confirmation") != LIVE_CONFIRMATION:
        return {"ok": False, "error": "mode LIVE non confirmé : ajoute "
                f"\"live_confirmation\": \"{LIVE_CONFIRMATION}\" dans config.json"}

    risk = risk_config(cfg)
    clean = _validate(req, risk["max_contracts_per_order"])
    if isinstance(clean, str):
        return {"ok": False, "error": clean}

    avertissements = []
    if clean["currency"] != "USD":
        avertissements.append("dates de résultats NON vérifiées (titre non-US) : "
                              "vérifie-les manuellement avant de transmettre")
    refus = _earnings_gate(clean, risk, mode)
    if refus:
        return {"ok": False, "error": f"EARNINGS : {refus}"}

    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    try:
        from ib_async import IB, LimitOrder, Option
    except ImportError:
        return {"ok": False, "error": "ib_async n'est pas installé"}

    ib = IB()
    connected = False
    for port in PORTS_BY_MODE[mode]:   # UNIQUEMENT les ports du mode configuré
        try:
            ib.connect(cfg["ib_host"], port, clientId=cfg["ib_client_id"] + 1, timeout=5)
            connected = True
            break
        except Exception:
            ib.disconnect()
    if not connected:
        return {"ok": False, "error": f"TWS injoignable sur les ports {mode.upper()} "
                f"{PORTS_BY_MODE[mode]}"}

    intent = _intent_id(clean)
    reserved = False
    try:
        account, err = _select_account(ib, cfg)
        if err:
            return {"ok": False, "error": err}
        is_paper_account = account.startswith("D")
        if mode == "paper" and not is_paper_account:
            return {"ok": False, "error": f"SÉCURITÉ : mode paper mais compte RÉEL "
                    f"({account}) connecté — refus. Connecte TWS en mode simulé."}
        if mode == "live" and is_paper_account:
            return {"ok": False, "error": f"mode live mais compte simulé ({account}) : refus"}

        contract = Option(clean["symbol"], clean["expiry"], clean["strike"],
                          clean["right"], "SMART", currency=clean["currency"])
        qualified = [c for c in ib.qualifyContracts(contract) if c is not None]
        if not qualified:
            return {"ok": False, "error": "contrat introuvable chez IBKR"}
        contract = qualified[0]
        multiplier = int(contract.multiplier or 100)

        for tr in ib.openTrades():
            if (tr.contract.conId == contract.conId
                    and tr.order.action == clean["action"]
                    and tr.orderStatus.status not in ("Filled", "Cancelled")):
                return {"ok": False, "error": f"ordre identique déjà présent dans TWS "
                        f"(id {tr.order.orderId})"}

        if dry_run:
            return {"ok": True, "dry_run": True, "mode": mode, "compte": account,
                    "message": f"Contrat vérifié : {contract.localSymbol}"}

        # ---- quote fraîche re-demandée MAINTENANT (revue n°6)
        quote = _fresh_quote(ib, contract)
        refus = check_quote(quote, clean, risk)
        if refus:
            return {"ok": False, "error": f"QUOTE : {refus}", "quote": quote}

        # ---- état réel du compte + complétude exigée en LIVE (revue n°3)
        account_state = _account_state(ib, account, clean["currency"])
        if mode == "live":
            manquants = [k for k in ("net_liq", "available_funds", "daily_pnl")
                         if account_state.get(k) is None]
            if manquants:
                return {"ok": False, "error": f"LIVE refusé : données de compte "
                        f"indisponibles ({', '.join(manquants)})"}

        from .risk import trade_risk_amount  # is_covered = cash vérifié en devise
        order_info = {**clean, "multiplier": multiplier, "is_covered": False}
        if clean["action"] == "SELL" and clean["right"] == "P":
            cash = account_state.get("cash_in_currency")
            required = clean["strike"] * multiplier * clean["quantity"]
            committed = account_state.get("short_put_commitment") or 0
            order_info["is_covered"] = (cash is not None
                                        and float(cash) >= required + committed)
        refus = check_risk(order_info, risk, account_state)
        if refus:
            return {"ok": False, "error": f"MOTEUR DE RISQUE : {refus}",
                    "compte": account, "mode": mode}

        # ---- simulation WhatIf IBKR (marges + commission)
        order = LimitOrder(clean["action"], clean["quantity"], clean["price"])
        order.tif = "DAY"
        state = ib.whatIfOrder(contract, order)

        def _f(x):
            try:
                v = float(str(x).replace(",", ""))
                return v if abs(v) < 1e300 else None
            except (TypeError, ValueError):
                return None
        whatif = {
            "init_margin_after": _f(getattr(state, "initMarginAfter", None)),
            "equity_with_loan_after": _f(getattr(state, "equityWithLoanAfter", None)),
            "commission": _f(getattr(state, "maxCommission", None))
                          or _f(getattr(state, "commission", None)),
        }
        refus = check_whatif(whatif, risk, account_state["net_liq"])
        if refus:
            return {"ok": False, "error": f"SIMULATION WHATIF : {refus}",
                    "whatif": whatif, "compte": account, "mode": mode}

        # ---- réservation ATOMIQUE de l'intention (revue n°7)
        if not ledger.try_reserve(intent):
            return {"ok": False, "error": f"intention déjà réservée aujourd'hui "
                    f"({intent}) : doublon refusé"}
        reserved = True

        # ---- préparation de l'ordre INACTIF (invariant absolu)
        order.transmit = False
        assert order.transmit is False
        trade = ib.placeOrder(contract, order)
        ib.sleep(2)

        # ---- liste blanche stricte (revue n°8) : tout code inattendu = échec
        bad = [f"{e.errorCode}: {e.message}" for e in trade.log
               if e.errorCode and e.errorCode not in BENIGN_ERROR_CODES]
        status = trade.orderStatus.status or "Inactive"
        expected = ("PreSubmitted", "Submitted", "Inactive", "ApiPending", "PendingSubmit")
        if bad or status not in expected:
            try:
                ib.cancelOrder(order)
            except Exception:
                pass
            ledger.release(intent)
            reserved = False
            return {"ok": False, "error": "réponse IBKR inattendue — ordre annulé : "
                    + ("; ".join(bad) if bad else f"statut {status}")}

        ledger.complete(intent, trade.order.orderId, {
            "compte": account, "mode": mode, "contrat": contract.localSymbol,
            "quantite": clean["quantity"], "limite": clean["price"],
            "quote": quote, "commission_estimee": whatif["commission"],
        })
        message = (f"[{mode.upper()} — compte {account}] Ordre préparé : "
                   f"{clean['action']} {clean['quantity']} x {contract.localSymbol} "
                   f"limite {clean['price']:.2f} (bid/ask actuels "
                   f"{quote['bid']}/{quote['ask']}, {quote['data_type']}, "
                   f"commission estimée {whatif['commission'] or '?'}$) — "
                   f"INACTIF dans TWS, clique TRANSMETTRE pour l'envoyer")
        if avertissements:
            message += " | ⚠ " + " | ⚠ ".join(avertissements)
        return {"ok": True, "order_id": trade.order.orderId, "status": status,
                "contract": contract.localSymbol, "mode": mode, "compte": account,
                "quote": quote, "commission_estimee": whatif["commission"],
                "marge_apres": whatif["init_margin_after"],
                "avertissements": avertissements,
                "message": message}
    except Exception as exc:
        if reserved:
            ledger.release(intent)
        return {"ok": False, "error": f"échec de préparation : {exc}"}
    finally:
        ib.disconnect()


def cancel_staged_orders(cfg: dict) -> dict:
    """Annule les ordres préparés par CE système (kill switch, revue n°9).
    Ne touche PAS aux ordres placés manuellement dans TWS."""
    ids = ledger.staged_order_ids()
    if not ids:
        return {"ok": True, "annules": 0, "message": "aucun ordre préparé à annuler"}
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    from ib_async import IB
    mode = str(cfg.get("trading_mode", "paper")).lower()
    ib = IB()
    for port in PORTS_BY_MODE.get(mode, (7497, 4002)):
        try:
            ib.connect(cfg["ib_host"], port, clientId=cfg["ib_client_id"] + 2, timeout=5)
            break
        except Exception:
            ib.disconnect()
    if not ib.isConnected():
        return {"ok": False, "annules": 0,
                "error": "TWS injoignable : annule manuellement dans TWS"}
    try:
        cancelled = 0
        for tr in ib.openTrades():
            if tr.order.orderId in ids and tr.orderStatus.status != "Cancelled":
                ib.cancelOrder(tr.order)
                ledger.mark_cancelled(tr.order.orderId)
                cancelled += 1
        ib.sleep(1)
        return {"ok": True, "annules": cancelled,
                "message": f"{cancelled} ordre(s) préparé(s) annulé(s)"}
    finally:
        ib.disconnect()
