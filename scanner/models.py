"""Modèles de données du scanner (immuables)."""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Opportunity:
    """Une opportunité détectée sur les options d'un ticker."""
    ticker: str
    kind: str          # type de signal : unusual_activity, prime_riche, skew, earnings, achat_cheap
    strategy: str      # action concrète suggérée (jamais exécutée automatiquement)
    score: float       # 0-100, plus c'est haut plus c'est rare/intéressant
    summary: str       # phrase lisible
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "kind": self.kind,
            "strategy": self.strategy,
            "score": round(self.score, 1),
            "summary": self.summary,
            "details": self.details,
        }


@dataclass(frozen=True)
class MarketSnapshot:
    """Photo instantanée d'un ticker : spot, vol réalisée, chaînes d'options."""
    ticker: str
    spot: float
    realized_vol_20d: float          # vol réalisée annualisée (20 jours)
    chains: tuple                     # tuple de (expiry_str, dte, calls_df, puts_df)
    earnings_days: int | None = None  # jours avant prochains résultats, None si inconnu
    closes: tuple = ()                # clôtures quotidiennes (~3 mois) pour momentum/RSI
    volumes: tuple = ()               # volumes quotidiens alignés sur closes (features ML)
