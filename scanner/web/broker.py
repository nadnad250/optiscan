"""Préparation d'ordres dans TWS — JAMAIS d'envoi direct, JAMAIS sans contrôle.

Garanties (P0 de la revue de production) :
- transmit=False TOUJOURS : l'ordre reste INACTIF dans TWS tant que
  l'utilisateur ne clique pas lui-même « Transmettre »
- séparation stricte PAPER/LIVE : en mode paper, SEULS les ports paper
  (7497/4002) sont essayés, et le compte connecté doit être un compte
  de simulation (préfixe D) — sinon refus immédiat
- mode live : exige la phrase de confirmation exacte dans config.json
  ET un compte non-simulé ; aucun port live n'est jamais tenté en paper
- moteur de risque (risk.py) sur l'état RÉEL du compte IBKR
  (NetLiquidation, AvailableFunds, positions, engagements de puts vendus)
- simulation WhatIf IBKR avant tout ordre (marges, commission)
- idempotence : une même intention (contrat+sens+jour) ne peut pas être
  préparée deux fois ; les ordres ouverts identiques bloquent aussi
- kill switch : le fichier output/KILL_SWITCH bloque toute préparation
"""
import asyncio
import json
from datetime import date, datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
KILL_SWITCH = BASE_DIR / "output" / "KILL_SWITCH"
LEDGER = BASE_DIR / "output" / "ordres_prepares.json"

PORTS_BY_MODE = {"paper": (7497, 4002), "live": (7496, 4001)}
LIVE_CONFIRMATION = "JE COMPRENDS LES RISQUES DU TRADING REEL"
VALID_RIGHTS = {"call": "C", "put": "P"}
VALID_ACTIONS = {"BUY", "SELL"}


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
        datetime.strptime(expiry, "%Y%m%d")
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
    if not (1 <= quantity <= max_qty):
        return f"quantité invalide (1 à {max_qty} contrat(s))"
    if not (0.01 <= price <= 10_000):
        return "prix limite invalide"
    return {"symbol": symbol, "right": right, "action": action, "strike": strike,
            "quantity": quantity, "price": price, "currency": currency, "expiry": expiry}


def _intent_id(clean: dict) -> str:
    return (f"{clean['symbol']}-{clean['expiry']}-{clean['strike']:g}-"
            f"{clean['right']}-{clean['action']}-{date.today().isoformat()}")


def _ledger_load() -> dict:
    if LEDGER.exists():
        try:
            return json.loads(LEDGER.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _ledger_add(intent: str, info: dict) -> None:
    ledger = _ledger_load()
    ledger[intent] = info
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(ledger, indent=1, ensure_ascii=False), encoding="utf-8")


def _account_state(ib, account: str) -> dict:
    """État RÉEL du compte : capital, fonds, positions, engagements."""
    tags = {}
    for row in ib.accountSummary(account):
        if row.tag in ("NetLiquidation", "AvailableFunds", "ExcessLiquidity", "Cushion"):
            try:
                tags[row.tag] = float(row.value)
            except ValueError:
                pass

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

    # ordres en attente : les puts vendus non exécutés engagent aussi du cash
    for tr in ib.openTrades():
        c, o = tr.contract, tr.order
        if (c.secType == "OPT" and c.right == "P" and o.action == "SELL"
                and tr.orderStatus.status not in ("Filled", "Cancelled", "Inactive")):
            short_put_commitment += float(c.strike) * int(c.multiplier or 100) * o.totalQuantity

    daily_pnl = None
    try:
        pnl = ib.reqPnL(account)
        ib.sleep(1.5)
        if pnl.dailyPnL is not None and pnl.dailyPnL == pnl.dailyPnL:  # exclut NaN
            daily_pnl = float(pnl.dailyPnL)
        ib.cancelPnL(account)
    except Exception:
        pass

    return {
        "net_liq": tags.get("NetLiquidation"),
        "available_funds": tags.get("AvailableFunds"),
        "excess_liquidity": tags.get("ExcessLiquidity"),
        "cushion": tags.get("Cushion"),
        "daily_pnl": daily_pnl,
        "positions_count": len(positions),
        "short_put_commitment": short_put_commitment,
        "options_exposure": options_exposure,
    }


def cash_secured_ok(order: dict, account: dict) -> bool:
    """Un SELL PUT est autorisé UNIQUEMENT si le cash disponible couvre
    strike x multiplicateur x quantité EN PLUS des engagements existants."""
    if order["action"] != "SELL" or order["right"] != "P":
        return False
    required = float(order["strike"]) * int(order["multiplier"]) * int(order["quantity"])
    available = float(account.get("available_funds") or 0)
    committed = float(account.get("short_put_commitment") or 0)
    return available >= required + committed


def stage_order(req: dict, cfg: dict, dry_run: bool = False) -> dict:
    """Prépare un ordre limite INACTIF dans TWS, après tous les contrôles."""
    from .risk import check_risk, check_whatif, risk_config

    # ---- garde-fous de configuration (P0.1 / P0.2 / P0.9)
    if not cfg.get("enable_order_staging", False):
        return {"ok": False, "error": "préparation d'ordres DÉSACTIVÉE "
                "(enable_order_staging=false dans config.json)"}
    if KILL_SWITCH.exists():
        return {"ok": False, "error": "KILL SWITCH actif : aucune préparation d'ordre. "
                "Supprime output/KILL_SWITCH pour réarmer."}
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

    intent = _intent_id(clean)
    if intent in _ledger_load():
        return {"ok": False, "error": "ordre déjà préparé aujourd'hui pour cette "
                f"intention ({intent}) : doublon refusé"}

    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    try:
        from ib_async import IB, LimitOrder, Option
    except ImportError:
        return {"ok": False, "error": "ib_async n'est pas installé"}

    ib = IB()
    connected_port = None
    for port in PORTS_BY_MODE[mode]:   # UNIQUEMENT les ports du mode configuré
        try:
            ib.connect(cfg["ib_host"], port, clientId=cfg["ib_client_id"] + 1, timeout=5)
            connected_port = port
            break
        except Exception:
            ib.disconnect()
    if connected_port is None:
        return {"ok": False, "error": f"TWS injoignable sur les ports {mode.upper()} "
                f"{PORTS_BY_MODE[mode]} — lance TWS en mode "
                f"{'simulé' if mode == 'paper' else 'réel'} d'abord"}

    try:
        accounts = ib.managedAccounts()
        account = accounts[0] if accounts else ""
        is_paper_account = account.startswith("D")
        if mode == "paper" and not is_paper_account:
            return {"ok": False, "error": f"SÉCURITÉ : mode paper mais le compte "
                    f"connecté ({account}) est un compte RÉEL — ordre refusé. "
                    f"Connecte TWS en mode simulé."}
        if mode == "live" and is_paper_account:
            return {"ok": False, "error": f"mode live mais compte simulé ({account}) "
                    f"connecté : incohérence, ordre refusé"}

        contract = Option(clean["symbol"], clean["expiry"], clean["strike"],
                          clean["right"], "SMART", currency=clean["currency"])
        qualified = [c for c in ib.qualifyContracts(contract) if c is not None]
        if not qualified:
            return {"ok": False, "error": f"contrat introuvable chez IBKR : "
                    f"{clean['symbol']} {clean['right']} {clean['strike']} {clean['expiry']}"}
        contract = qualified[0]
        multiplier = int(contract.multiplier or 100)

        # doublon côté IBKR : même contrat + même sens déjà en attente ?
        for tr in ib.openTrades():
            if (tr.contract.conId == contract.conId
                    and tr.order.action == clean["action"]
                    and tr.orderStatus.status not in ("Filled", "Cancelled")):
                return {"ok": False, "error": "un ordre identique existe déjà dans TWS "
                        f"(id {tr.order.orderId}, statut {tr.orderStatus.status})"}

        if dry_run:
            return {"ok": True, "dry_run": True, "mode": mode, "compte": account,
                    "message": f"Contrat vérifié : {contract.localSymbol}"}

        # ---- moteur de risque sur l'état réel du compte (P0.3 / P0.4)
        account_state = _account_state(ib, account)
        order_info = {**clean, "multiplier": multiplier,
                      # la 'couverture' d'un SELL PUT, c'est le CASH réel :
                      # vérifiée par cash_secured_ok, jamais présumée
                      "is_covered": cash_secured_ok(
                          {**clean, "multiplier": multiplier}, account_state)}
        refus = check_risk(order_info, risk, account_state)
        if refus:
            return {"ok": False, "error": f"MOTEUR DE RISQUE : {refus}",
                    "compte": account, "mode": mode, "etat_compte": account_state}

        # ---- simulation WhatIf IBKR (P0.5) : marges et commission AVANT l'ordre
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

        # ---- préparation de l'ordre INACTIF (transmit=False, invariant absolu)
        order.transmit = False
        assert order.transmit is False
        trade = ib.placeOrder(contract, order)
        ib.sleep(2)

        errors = [e.message for e in trade.log if e.errorCode]
        status = trade.orderStatus.status or "Inactive"
        if errors and status not in ("PreSubmitted", "Submitted", "Inactive",
                                     "ApiPending", "PendingSubmit"):
            return {"ok": False, "error": " / ".join(errors[-2:]) +
                    " — vérifie que « API en lecture seule » est DÉCOCHÉ dans TWS"}

        _ledger_add(intent, {
            "horodatage": datetime.now().isoformat(timespec="seconds"),
            "compte": account, "mode": mode, "order_id": trade.order.orderId,
            "contrat": contract.localSymbol, "quantite": clean["quantity"],
            "limite": clean["price"], "commission_estimee": whatif["commission"],
        })
        return {"ok": True, "order_id": trade.order.orderId, "status": status,
                "contract": contract.localSymbol, "mode": mode, "compte": account,
                "commission_estimee": whatif["commission"],
                "marge_apres": whatif["init_margin_after"],
                "message": (f"[{mode.upper()} — compte {account}] Ordre préparé : "
                            f"{clean['action']} {clean['quantity']} x {contract.localSymbol} "
                            f"limite {clean['price']:.2f} (commission estimée "
                            f"{whatif['commission'] or '?'}$) — INACTIF dans TWS, "
                            f"clique TRANSMETTRE pour l'envoyer")}
    except Exception as exc:
        return {"ok": False, "error": f"échec de préparation : {exc}"}
    finally:
        ib.disconnect()
