"""Tests du moteur de risque — les invariants qui protègent le capital."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from scanner.web.broker import LIVE_CONFIRMATION, PORTS_BY_MODE, _intent_id, _validate, cash_secured_ok
from scanner.web.risk import RISK_DEFAULTS, check_risk, check_whatif, risk_config

RISK = risk_config({})
COMPTE_SAIN = {
    "net_liq": 50_000, "available_funds": 30_000, "excess_liquidity": 25_000,
    "daily_pnl": 0.0, "positions_count": 0,
    "short_put_commitment": 0.0, "options_exposure": 0.0,
}


def _achat(prix=0.80, qty=1):
    return {"action": "BUY", "side": "call", "right": "C", "strike": 20.0,
            "quantity": qty, "price": prix, "multiplier": 100, "is_covered": False}


def _vente_put(strike=20.0, qty=1, couvert=True):
    return {"action": "SELL", "side": "put", "right": "P", "strike": strike,
            "quantity": qty, "price": 0.60, "multiplier": 100, "is_covered": couvert}


# ---- séparation paper/live -------------------------------------------------

def test_ports_paper_ne_contiennent_jamais_les_ports_reels():
    assert 7496 not in PORTS_BY_MODE["paper"]
    assert 4001 not in PORTS_BY_MODE["paper"]


def test_confirmation_live_exigee():
    assert LIVE_CONFIRMATION  # la phrase existe et n'est pas vide


# ---- validation des demandes ------------------------------------------------

def test_validation_refuse_quantite_excessive():
    req = {"symbol": "SOFI", "side": "call", "strike": 20, "expiry": "20260807",
           "limit_price": 0.8, "quantity": 99}
    assert isinstance(_validate(req, max_qty=1), str)


def test_validation_refuse_symbole_bizarre():
    req = {"symbol": "SO;FI", "side": "call", "strike": 20, "expiry": "20260807",
           "limit_price": 0.8, "quantity": 1}
    assert isinstance(_validate(req, 1), str)


def test_intent_id_deterministe():
    clean = _validate({"symbol": "SOFI", "side": "call", "strike": 20,
                       "expiry": "20260807", "limit_price": 0.8, "quantity": 1}, 1)
    assert _intent_id(clean) == _intent_id(dict(clean))


# ---- moteur de risque : achats ----------------------------------------------

def test_achat_raisonnable_accepte():
    assert check_risk(_achat(0.80), RISK, COMPTE_SAIN) is None


def test_achat_trop_gros_refuse():
    # 0.25% de 50k = 125$ max ; un contrat à 3$ = 300$ -> refus
    assert "risque du trade" in check_risk(_achat(3.00), RISK, COMPTE_SAIN)


def test_perte_quotidienne_bloque_tout():
    compte = dict(COMPTE_SAIN, daily_pnl=-500.0)  # > 0.75% de 50k = 375
    assert "perte quotidienne" in check_risk(_achat(0.50), RISK, compte)


def test_trop_de_positions_refuse():
    compte = dict(COMPTE_SAIN, positions_count=3)
    assert "positions ouvertes" in check_risk(_achat(0.50), RISK, compte)


def test_net_liq_manquant_refuse():
    compte = dict(COMPTE_SAIN, net_liq=None)
    assert check_risk(_achat(0.50), RISK, compte) is not None


# ---- moteur de risque : ventes de puts (LE point dangereux) ------------------

def test_vente_put_nue_refusee():
    assert "NON couverte" in check_risk(_vente_put(couvert=False), RISK, COMPTE_SAIN)


def test_vente_put_sans_cash_refusee():
    compte = dict(COMPTE_SAIN, available_funds=1_000)  # strike 20 -> 2000$ requis
    assert "cash insuffisant" in check_risk(_vente_put(couvert=True), RISK, compte)


def test_vente_put_engagements_existants_comptes():
    # 30k dispo, 29k déjà engagés sur d'autres puts vendus -> 2000$ de plus = refus
    compte = dict(COMPTE_SAIN, short_put_commitment=29_000)
    assert "cash insuffisant" in check_risk(_vente_put(couvert=True), RISK, compte)


def test_cash_secured_ok_verifie_le_vrai_cash():
    ordre = _vente_put()
    assert cash_secured_ok(ordre, COMPTE_SAIN) is True
    assert cash_secured_ok(ordre, dict(COMPTE_SAIN, available_funds=500)) is False
    # un BUY n'est jamais "cash secured"
    assert cash_secured_ok(_achat(), COMPTE_SAIN) is False


# ---- WhatIf ------------------------------------------------------------------

def test_whatif_incomplet_refuse_par_prudence():
    assert check_whatif({}, RISK, 50_000) is not None


def test_whatif_marge_insuffisante_refuse():
    etat = {"init_margin_after": 49_000, "equity_with_loan_after": 50_000,
            "commission": 1.0}
    # excédent 1000 < 10% de 50k -> refus
    assert "liquidité excédentaire" in check_whatif(etat, RISK, 50_000)


def test_whatif_sain_accepte():
    etat = {"init_margin_after": 10_000, "equity_with_loan_after": 50_000,
            "commission": 1.0}
    assert check_whatif(etat, RISK, 50_000) is None


# ---- défauts sûrs -------------------------------------------------------------

def test_defauts_conservateurs():
    assert RISK_DEFAULTS["allow_naked_options"] is False
    assert RISK_DEFAULTS["allow_market_orders"] is False
    assert RISK_DEFAULTS["allow_earnings_trades"] is False
    assert RISK_DEFAULTS["max_contracts_per_order"] == 1


def test_config_par_defaut_est_sure():
    from scanner.config import DEFAULTS
    assert DEFAULTS["trading_mode"] == "paper"
    assert DEFAULTS["enable_order_staging"] is False
