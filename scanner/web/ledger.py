"""Registre ATOMIQUE des intentions d'ordres (SQLite, revue : point n°7).

L'unicité est garantie par la contrainte PRIMARY KEY : deux threads (double
clic, reconnexion, redémarrage) ne peuvent JAMAIS réserver la même intention.
Le fichier survit aux redémarrages — l'idempotence aussi.
"""
import json
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent.parent / "output" / "ordres.db"


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=5)
    con.execute("""CREATE TABLE IF NOT EXISTS intents(
        id TEXT PRIMARY KEY,
        reserved_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'reserved',
        order_id INTEGER,
        info TEXT)""")
    return con


def try_reserve(intent: str) -> bool:
    """Réservation ATOMIQUE : True si l'intention est nouvelle, False sinon."""
    con = _connect()
    try:
        con.execute("INSERT INTO intents(id, reserved_at) VALUES(?, ?)",
                    (intent, datetime.now().isoformat(timespec="seconds")))
        con.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        con.close()


def complete(intent: str, order_id: int, info: dict) -> None:
    con = _connect()
    try:
        con.execute("UPDATE intents SET status='staged', order_id=?, info=? WHERE id=?",
                    (order_id, json.dumps(info, ensure_ascii=False), intent))
        con.commit()
    finally:
        con.close()


def release(intent: str) -> None:
    """Libère une réservation dont la préparation a ÉCHOUÉ (permet de réessayer)."""
    con = _connect()
    try:
        con.execute("DELETE FROM intents WHERE id=? AND status='reserved'", (intent,))
        con.commit()
    finally:
        con.close()


def staged_order_ids() -> list[int]:
    """Ids des ordres préparés par CE système (pour le kill switch)."""
    con = _connect()
    try:
        rows = con.execute(
            "SELECT order_id FROM intents WHERE status='staged' AND order_id IS NOT NULL")
        return [int(r[0]) for r in rows.fetchall()]
    finally:
        con.close()


def list_intents(limit: int = 30) -> list[dict]:
    """Dernières intentions d'ordres (pour le panneau « Ordres préparés »)."""
    con = _connect()
    try:
        rows = con.execute(
            "SELECT id, reserved_at, status, order_id, info FROM intents "
            "ORDER BY reserved_at DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for rid, ts, status, order_id, info in rows:
            entry = {"intent": rid, "date": ts, "statut": status, "order_id": order_id}
            if info:
                try:
                    entry.update(json.loads(info))
                except json.JSONDecodeError:
                    pass
            out.append(entry)
        return out
    finally:
        con.close()


def mark_cancelled(order_id: int) -> None:
    con = _connect()
    try:
        con.execute("UPDATE intents SET status='cancelled' WHERE order_id=?", (order_id,))
        con.commit()
    finally:
        con.close()
