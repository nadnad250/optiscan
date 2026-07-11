"""Préparation d'ordres dans TWS — JAMAIS d'envoi direct.

Tous les ordres sont créés avec transmit=False : ils apparaissent dans TWS
pré-remplis (contrat, quantité, prix limite) mais restent INACTIFS tant que
l'utilisateur ne clique pas lui-même sur « Transmettre » dans TWS.

Prérequis côté TWS : Configuration globale > API > Paramètres >
décocher « API en lecture seule » (sinon TWS refuse même les ordres inactifs).
"""
import asyncio
import threading
from datetime import datetime

PORTS = (7497, 7496, 4002, 4001)   # paper d'abord
MAX_QUANTITY = 5                    # garde-fou : jamais plus de 5 contrats
VALID_RIGHTS = {"call": "C", "put": "P"}
VALID_ACTIONS = {"BUY", "SELL"}


def _validate(req: dict) -> dict | str:
    """Valide et normalise la demande. Retourne un dict propre ou un message d'erreur."""
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
    if not (1 <= quantity <= MAX_QUANTITY):
        return f"quantité invalide (1 à {MAX_QUANTITY} contrats)"
    if not (0.01 <= price <= 10_000):
        return "prix limite invalide"
    return {"symbol": symbol, "right": right, "action": action, "strike": strike,
            "quantity": quantity, "price": price, "currency": currency, "expiry": expiry}


def stage_order(req: dict, cfg: dict, dry_run: bool = False) -> dict:
    """Prépare un ordre limite INACTIF dans TWS (transmit=False).

    dry_run=True : valide la demande et le contrat sans créer d'ordre.
    """
    clean = _validate(req)
    if isinstance(clean, str):
        return {"ok": False, "error": clean}

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
    for port in PORTS:
        try:
            # clientId dédié aux ordres (différent du scanner)
            ib.connect(cfg["ib_host"], port, clientId=cfg["ib_client_id"] + 1, timeout=5)
            connected = True
            break
        except Exception:
            ib.disconnect()
    if not connected:
        return {"ok": False, "error": "TWS injoignable — lance TWS et connecte-toi d'abord"}

    try:
        contract = Option(clean["symbol"], clean["expiry"], clean["strike"],
                          clean["right"], "SMART", currency=clean["currency"])
        qualified = [c for c in ib.qualifyContracts(contract) if c is not None]
        if not qualified:
            return {"ok": False, "error": f"contrat introuvable chez IBKR : "
                    f"{clean['symbol']} {clean['right']} {clean['strike']} {clean['expiry']}"}
        contract = qualified[0]

        if dry_run:
            return {"ok": True, "dry_run": True,
                    "message": f"Contrat vérifié : {contract.localSymbol}"}

        order = LimitOrder(clean["action"], clean["quantity"], clean["price"])
        order.transmit = False          # l'ordre reste INACTIF dans TWS
        order.tif = "DAY"
        trade = ib.placeOrder(contract, order)
        ib.sleep(2)

        errors = [e.message for e in trade.log if "rror" in e.status or e.errorCode]
        status = trade.orderStatus.status or "Inactive"
        if errors and status not in ("PreSubmitted", "Submitted", "Inactive", "ApiPending", "PendingSubmit"):
            return {"ok": False, "error": " / ".join(errors[-2:]) +
                    " — vérifie que « API en lecture seule » est DÉCOCHÉ dans TWS "
                    "(Configuration globale > API > Paramètres)"}

        return {"ok": True, "order_id": trade.order.orderId, "status": status,
                "contract": contract.localSymbol,
                "message": (f"Ordre préparé dans TWS : {clean['action']} {clean['quantity']} x "
                            f"{contract.localSymbol} limite {clean['price']:.2f} — "
                            f"ouvre TWS et clique TRANSMETTRE pour l'envoyer")}
    except Exception as exc:
        return {"ok": False, "error": f"échec de préparation : {exc}"}
    finally:
        ib.disconnect()
