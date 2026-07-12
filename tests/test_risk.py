"""Tests du moteur de risque — les invariants qui protègent le capital."""
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from scanner.web.broker import (BENIGN_ERROR_CODES, LIVE_CONFIRMATION,
                                PORTS_BY_MODE, _intent_id, _validate)
from scanner.web.risk import (RISK_DEFAULTS, check_quote, check_risk,
                              check_whatif, risk_config, trade_risk_amount)

RISK = risk_config({})
COMPTE_SAIN = {
    "net_liq": 50_000, "available_funds": 30_000, "excess_liquidity": 25_000,
    "cash_in_currency": 30_000, "daily_pnl": 0.0, "positions_count": 0,
    "short_put_commitment": 0.0, "options_exposure": 0.0,
}


def _achat(prix=0.80, qty=1):
    return {"action": "BUY", "side": "call", "right": "C", "strike": 20.0,
            "quantity": qty, "price": prix, "multiplier": 100,
            "currency": "USD", "is_covered": False}


def _vente_put(strike=20.0, qty=1, couvert=True):
    return {"action": "SELL", "side": "put", "right": "P", "strike": strike,
            "quantity": qty, "price": 0.60, "multiplier": 100,
            "currency": "USD", "is_covered": couvert}


# ---- séparation paper/live -------------------------------------------------

def test_ports_paper_ne_contiennent_jamais_les_ports_reels():
    assert 7496 not in PORTS_BY_MODE["paper"]
    assert 4001 not in PORTS_BY_MODE["paper"]


def test_confirmation_live_exigee():
    assert LIVE_CONFIRMATION


# ---- validation des demandes ------------------------------------------------

def test_validation_refuse_quantite_excessive():
    req = {"symbol": "SOFI", "side": "call", "strike": 20, "expiry": "20990807",
           "limit_price": 0.8, "quantity": 99}
    assert isinstance(_validate(req, max_qty=1), str)


def test_validation_refuse_expiration_passee():
    req = {"symbol": "SOFI", "side": "call", "strike": 20, "expiry": "20200101",
           "limit_price": 0.8, "quantity": 1}
    assert "passée" in _validate(req, 1)


def test_intent_id_deterministe():
    clean = _validate({"symbol": "SOFI", "side": "call", "strike": 20,
                       "expiry": "20990807", "limit_price": 0.8, "quantity": 1}, 1)
    assert _intent_id(clean) == _intent_id(dict(clean))


# ---- risque : le NOTIONNEL des puts vendus (revue n°1) -----------------------

def test_risque_put_vendu_est_le_notionnel_complet():
    # strike 20 x 100 = 2000$, pas la prime de 60$
    assert trade_risk_amount(_vente_put()) == 2_000.0
    assert trade_risk_amount(_achat(0.80)) == 80.0


def test_put_vendu_refuse_par_limite_de_risque_par_defaut():
    # défaut : 0.25% de 50k = 125$ max — un put strike 20 (2000$ de notionnel)
    # DOIT être refusé même si le cash le couvre
    refus = check_risk(_vente_put(couvert=True), RISK, COMPTE_SAIN)
    assert refus and "notionnel" in refus


def test_put_vendu_accepte_si_limite_relevee_et_cash_suffisant():
    risk = risk_config({"risk": {"max_risk_per_trade_pct": 5.0,
                                 "max_total_options_risk_pct": 10.0}})
    assert check_risk(_vente_put(couvert=True), risk, COMPTE_SAIN) is None


# ---- risque : cash dans la DEVISE du contrat (revue n°2) ---------------------

def test_cash_devise_manquant_refuse():
    risk = risk_config({"risk": {"max_risk_per_trade_pct": 5.0,
                                 "max_total_options_risk_pct": 10.0}})
    compte = dict(COMPTE_SAIN, cash_in_currency=None)
    refus = check_risk(_vente_put(couvert=True), risk, compte)
    assert refus and "indisponible" in refus


def test_cash_devise_insuffisant_refuse():
    risk = risk_config({"risk": {"max_risk_per_trade_pct": 5.0,
                                 "max_total_options_risk_pct": 10.0}})
    compte = dict(COMPTE_SAIN, cash_in_currency=1_000)
    refus = check_risk(_vente_put(couvert=True), risk, compte)
    assert refus and "insuffisant" in refus


def test_engagements_existants_comptes():
    risk = risk_config({"risk": {"max_risk_per_trade_pct": 5.0,
                                 "max_total_options_risk_pct": 10.0}})
    compte = dict(COMPTE_SAIN, cash_in_currency=30_000, short_put_commitment=29_000)
    assert "insuffisant" in check_risk(_vente_put(couvert=True), risk, compte)


def test_put_nu_refuse():
    risk = risk_config({"risk": {"max_risk_per_trade_pct": 5.0}})
    assert "NON couverte" in check_risk(_vente_put(couvert=False), risk, COMPTE_SAIN)


# ---- risque : achats et limites globales -------------------------------------

def test_achat_raisonnable_accepte():
    assert check_risk(_achat(0.80), RISK, COMPTE_SAIN) is None


def test_achat_trop_gros_refuse():
    assert "risque du trade" in check_risk(_achat(3.00), RISK, COMPTE_SAIN)


def test_perte_quotidienne_bloque_tout():
    compte = dict(COMPTE_SAIN, daily_pnl=-500.0)
    assert "perte quotidienne" in check_risk(_achat(0.50), RISK, compte)


def test_trop_de_positions_refuse():
    compte = dict(COMPTE_SAIN, positions_count=3)
    assert "positions ouvertes" in check_risk(_achat(0.50), RISK, compte)


def test_net_liq_manquant_refuse():
    compte = dict(COMPTE_SAIN, net_liq=None)
    assert check_risk(_achat(0.50), RISK, compte) is not None


# ---- quote re-demandée avant l'ordre (revue n°6/12) ---------------------------

def test_quote_croisee_refusee():
    assert "croisée" in check_quote({"bid": 1.10, "ask": 1.00}, _achat(1.0), RISK)


def test_quote_absente_refusee():
    assert check_quote({"bid": None, "ask": 1.0}, _achat(1.0), RISK) is not None


def test_quote_fourchette_excessive_refusee():
    assert "fourchette" in check_quote({"bid": 0.10, "ask": 0.50}, _achat(0.3), RISK)


def test_limite_achat_trop_haute_refusee():
    q = {"bid": 0.80, "ask": 0.85}
    assert "au-dessus" in check_quote(q, _achat(1.50), RISK)


def test_quote_saine_acceptee():
    q = {"bid": 0.78, "ask": 0.82}
    assert check_quote(q, _achat(0.80), RISK) is None


# ---- WhatIf --------------------------------------------------------------------

def test_whatif_incomplet_refuse():
    assert check_whatif({}, RISK, 50_000) is not None


def test_whatif_marge_insuffisante_refuse():
    etat = {"init_margin_after": 49_000, "equity_with_loan_after": 50_000}
    assert "liquidité excédentaire" in check_whatif(etat, RISK, 50_000)


def test_whatif_sain_accepte():
    etat = {"init_margin_after": 10_000, "equity_with_loan_after": 50_000}
    assert check_whatif(etat, RISK, 50_000) is None


# ---- idempotence ATOMIQUE (SQLite, revue n°7) ----------------------------------

def test_reservation_atomique_sous_concurrence(tmp_path, monkeypatch):
    from scanner.web import ledger
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "ordres.db")
    resultats = []
    def tenter():
        resultats.append(ledger.try_reserve("SOFI-20990807-20-C-BUY-2026-07-12"))
    threads = [threading.Thread(target=tenter) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert resultats.count(True) == 1          # UNE seule réservation gagne
    assert resultats.count(False) == 7


def test_release_permet_de_reessayer(tmp_path, monkeypatch):
    from scanner.web import ledger
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "ordres.db")
    assert ledger.try_reserve("X") is True
    ledger.release("X")
    assert ledger.try_reserve("X") is True     # échec libéré => retentable
    ledger.complete("X", 42, {})
    ledger.release("X")                        # ne libère PAS un ordre préparé
    assert ledger.try_reserve("X") is False
    assert ledger.staged_order_ids() == [42]


# ---- liste blanche des erreurs (revue n°8) -------------------------------------

def test_liste_blanche_ne_contient_que_des_avertissements_connus():
    # 201 (rejet), 110 (prix invalide), 203 (titre interdit) ne doivent JAMAIS
    # être considérés bénins
    for code in (110, 201, 203, 321, 10197):
        assert code not in BENIGN_ERROR_CODES


# ---- schéma de configuration (revue n°11) ---------------------------------------

def test_config_cle_de_risque_inconnue_refusee(tmp_path):
    from scanner.config import load_config
    cfg = tmp_path / "config.json"
    cfg.write_text('{"risk": {"max_risk_per_trad_pct": 1.0}}', encoding="utf-8")
    with pytest.raises(ValueError, match="INCONNUE"):
        load_config(str(cfg))


def test_config_mode_invalide_refuse(tmp_path):
    from scanner.config import load_config
    cfg = tmp_path / "config.json"
    cfg.write_text('{"trading_mode": "yolo"}', encoding="utf-8")
    with pytest.raises(ValueError, match="trading_mode"):
        load_config(str(cfg))


def test_defauts_conservateurs():
    assert RISK_DEFAULTS["allow_naked_options"] is False
    assert RISK_DEFAULTS["allow_market_orders"] is False
    assert RISK_DEFAULTS["allow_earnings_trades"] is False
    assert RISK_DEFAULTS["max_contracts_per_order"] == 1


def test_config_par_defaut_est_sure():
    from scanner.config import DEFAULTS
    assert DEFAULTS["trading_mode"] == "paper"
    assert DEFAULTS["enable_order_staging"] is False
