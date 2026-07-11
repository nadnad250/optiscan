"""Serveur web OptiScan : dashboard + API JSON, 100 % stdlib.

Lancement : python run_web.py [--port 8765]
Endpoints :
  GET  /             dashboard
  GET  /api/latest   dernier scan (JSON)
  GET  /api/status   progression du scan en cours
  POST /api/scan     lance un scan en arrière-plan  {"source": "yahoo", "tickers": [...]}
"""
import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..config import load_config
from ..report import save_json
from ..scan import run_scan

BASE_DIR = Path(__file__).resolve().parent.parent.parent   # options-scanner/
OUTPUT_DIR = BASE_DIR / "output"
INDEX_HTML = Path(__file__).resolve().parent / "index.html"
GUIDE_HTML = Path(__file__).resolve().parent / "guide.html"

_lock = threading.Lock()
_status = {"running": False, "done": 0, "total": 0, "current": "", "error": None}


def _set_status(**kwargs) -> None:
    with _lock:
        _status.update(kwargs)


def _get_status() -> dict:
    with _lock:
        return dict(_status)


def _latest_scan() -> dict:
    files = sorted(OUTPUT_DIR.glob("opportunites_*.json"))
    if not files:
        return {"generated_at": None, "count": 0, "opportunities": []}
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {"generated_at": None, "count": 0, "opportunities": [], "error": str(exc)}


def _scan_worker(source: str, tickers: list[str] | None) -> None:
    cfg = load_config()
    watchlist = tickers or cfg["watchlist"]
    _set_status(running=True, done=0, total=len(watchlist), current="", error=None)
    try:
        opps = run_scan(cfg, watchlist, source,
                        progress=lambda i, t, n: _set_status(done=i, current=t))
        save_json(opps, OUTPUT_DIR)
        _set_status(running=False, done=len(watchlist), current="")
    except Exception as exc:
        _set_status(running=False, error=str(exc))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # journal minimal
        print(f"[web] {self.address_string()} {fmt % args}")

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict, code: int = 200) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, INDEX_HTML.read_bytes(), "text/html; charset=utf-8")
        elif self.path in ("/guide", "/guide.html"):
            self._send(200, GUIDE_HTML.read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/latest":
            self._send_json(_latest_scan())
        elif self.path == "/api/status":
            self._send_json(_get_status())
        else:
            self._send_json({"error": "introuvable"}, 404)

    def do_POST(self):
        if self.path == "/api/order/stage":
            self._handle_stage_order()
            return
        if self.path != "/api/scan":
            self._send_json({"error": "introuvable"}, 404)
            return
        if _get_status()["running"]:
            self._send_json({"error": "un scan est déjà en cours"}, 409)
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except json.JSONDecodeError:
            self._send_json({"error": "JSON invalide"}, 400)
            return
        source = body.get("source", "yahoo")
        if source not in ("yahoo", "ib"):
            self._send_json({"error": "source inconnue (yahoo|ib)"}, 400)
            return
        tickers = body.get("tickers")
        if tickers is not None and not (isinstance(tickers, list)
                                        and all(isinstance(t, str) and t.strip() for t in tickers)):
            self._send_json({"error": "tickers doit être une liste de symboles"}, 400)
            return
        tickers = [t.strip().upper() for t in tickers] if tickers else None
        threading.Thread(target=_scan_worker, args=(source, tickers), daemon=True).start()
        self._send_json({"ok": True, "message": "scan lancé"})


    def _handle_stage_order(self):
        """Prépare un ordre INACTIF dans TWS (transmit=False, l'utilisateur
        doit cliquer Transmettre dans TWS lui-même)."""
        from .broker import stage_order
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except json.JSONDecodeError:
            self._send_json({"ok": False, "error": "JSON invalide"}, 400)
            return
        result = stage_order(body, load_config(), dry_run=bool(body.get("dry_run")))
        self._send_json(result, 200 if result.get("ok") else 422)


def main():
    parser = argparse.ArgumentParser(description="Interface web OptiScan")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"OptiScan web : http://127.0.0.1:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
