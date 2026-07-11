"""Construction et évaluation de stratégies à risque défini.

Principe de l'edge : le crédit encaissé est facturé au prix de l'IV du
marché ; la valeur théorique de ce qu'on vend est recalculée avec la vol
estimée (mélange vol réalisée / IV). EV = crédit - coût théorique.
EV > 0 = on vend plus cher que ça ne vaut, statistiquement.
"""
from dataclasses import dataclass

from .greeks import call_price, prob_above, prob_below, put_price


@dataclass(frozen=True)
class Candidate:
    """Une stratégie candidate entièrement évaluée (montants par action)."""
    name: str
    legs: tuple            # descriptions lisibles des jambes
    credit: float          # net encaissé (négatif = débit payé)
    max_loss: float        # perte maximale
    pop: float             # probabilité de profit (vol estimée, sans dérive)
    ev: float              # espérance de gain
    dte: int
    expiry: str
    liq_penalty: float     # 0 (parfait) → 1 (illiquide)

    @property
    def edge(self) -> float:
        """EV par unité de risque."""
        return self.ev / self.max_loss if self.max_loss > 0 else 0.0

    @property
    def ratio_gain_risque(self) -> float:
        return self.credit / self.max_loss if self.max_loss > 0 else 0.0

    @property
    def roc_annual(self) -> float:
        """Rendement du capital à risque, annualisé."""
        if self.max_loss <= 0 or self.dte <= 0:
            return 0.0
        return (self.credit / self.max_loss) * (365 / self.dte)

    @property
    def kelly(self) -> float:
        """Fraction de Kelly (approx. binaire : gain=crédit, perte=max_loss).

        max_loss est déjà net du crédit encaissé (width - crédit), donc la
        perte en cas d'échec est bien max_loss, pas max_loss - crédit.
        """
        if self.max_loss <= 0 or self.credit <= 0:
            return 0.0
        b = self.credit / self.max_loss
        f = self.pop - (1 - self.pop) / b
        return max(0.0, min(f, 1.0))

    def to_details(self) -> dict:
        return {
            "strategie": self.name,
            "jambes": list(self.legs),
            "expiry": self.expiry,
            "dte": self.dte,
            "credit_par_action": round(self.credit, 2),
            "risque_max_par_action": round(self.max_loss, 2),
            "prob_profit": round(self.pop, 3),
            "esperance_gain_EV": round(self.ev, 3),
            "edge_par_risque": round(self.edge, 3),
            "ratio_gain_risque": round(self.ratio_gain_risque, 3),
            "rendement_annualise": round(self.roc_annual, 3),
            "kelly_pleine": round(self.kelly, 3),
            "demi_kelly_conseillee": round(self.kelly / 2, 3),
        }


def bull_put_spread(spot, k_short, k_long, credit, sigma_true, t, dte, expiry, liq):
    """Vente put k_short / achat put k_long (k_long < k_short)."""
    width = k_short - k_long
    max_loss = width - credit
    if max_loss <= 0 or credit <= 0:
        return None
    theo = put_price(spot, k_short, sigma_true, t) - put_price(spot, k_long, sigma_true, t)
    breakeven = k_short - credit
    return Candidate(
        name="bull put spread",
        legs=(f"VENDRE put {k_short:g}", f"ACHETER put {k_long:g}"),
        credit=credit, max_loss=max_loss,
        pop=prob_above(spot, breakeven, sigma_true, t),
        ev=credit - theo, dte=dte, expiry=expiry, liq_penalty=liq,
    )


def bear_call_spread(spot, k_short, k_long, credit, sigma_true, t, dte, expiry, liq):
    """Vente call k_short / achat call k_long (k_long > k_short)."""
    width = k_long - k_short
    max_loss = width - credit
    if max_loss <= 0 or credit <= 0:
        return None
    theo = call_price(spot, k_short, sigma_true, t) - call_price(spot, k_long, sigma_true, t)
    breakeven = k_short + credit
    return Candidate(
        name="bear call spread",
        legs=(f"VENDRE call {k_short:g}", f"ACHETER call {k_long:g}"),
        credit=credit, max_loss=max_loss,
        pop=prob_below(spot, breakeven, sigma_true, t),
        ev=credit - theo, dte=dte, expiry=expiry, liq_penalty=liq,
    )


def iron_condor(put_side: Candidate, call_side: Candidate, spot, sigma_true, t):
    """Combinaison des deux spreads (même échéance)."""
    credit = put_side.credit + call_side.credit
    width_put = put_side.max_loss + put_side.credit
    width_call = call_side.max_loss + call_side.credit
    max_loss = max(width_put, width_call) - credit
    if max_loss <= 0:
        return None
    k_short_put = float(put_side.legs[0].split()[-1])
    k_short_call = float(call_side.legs[0].split()[-1])
    pop = 1.0 - prob_below(spot, k_short_put - credit, sigma_true, t) \
              - prob_above(spot, k_short_call + credit, sigma_true, t)
    return Candidate(
        name="iron condor",
        legs=put_side.legs + call_side.legs,
        credit=credit, max_loss=max_loss, pop=max(pop, 0.0),
        ev=put_side.ev + call_side.ev,
        dte=put_side.dte, expiry=put_side.expiry,
        liq_penalty=max(put_side.liq_penalty, call_side.liq_penalty),
    )


def long_straddle(spot, strike, debit, sigma_true, t, dte, expiry, liq):
    """Achat call + put ATM : rentable si le marché sous-estime le mouvement."""
    if debit <= 0:
        return None
    theo = call_price(spot, strike, sigma_true, t) + put_price(spot, strike, sigma_true, t)
    pop = prob_below(spot, strike - debit, sigma_true, t) \
        + prob_above(spot, strike + debit, sigma_true, t)
    return Candidate(
        name="straddle acheté",
        legs=(f"ACHETER call {strike:g}", f"ACHETER put {strike:g}"),
        credit=-debit, max_loss=debit, pop=pop,
        ev=theo - debit, dte=dte, expiry=expiry, liq_penalty=liq,
    )
