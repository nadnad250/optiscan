"""Sources de données : yahoo (gratuit, différé) ou ibkr (temps réel)."""


def normalize_entry(entry) -> dict:
    """Uniformise une entrée de watchlist.

    Accepte 'AAPL' (action US) ou {'symbol': 'LHA', 'currency': 'EUR',
    'primary': 'IBIS'} pour les actions européennes (bourse primaire requise
    pour lever les ambiguïtés, ex. SAN = Santander à Madrid / Sanofi à Paris).
    """
    if isinstance(entry, str):
        return {"symbol": entry.strip().upper(), "currency": "USD", "primary": None}
    return {
        "symbol": str(entry["symbol"]).strip().upper(),
        "currency": str(entry.get("currency", "USD")).upper(),
        "primary": entry.get("primary"),
    }


def pick_spread(items: list, n: int) -> list:
    """Choisit n éléments répartis sur toute la liste (proche / moyen / lointain).

    Évite de ne prendre que les échéances les plus proches : avec n=3 sur
    des échéances de 7 à 60 jours, on couvre court, moyen et long terme.
    """
    if len(items) <= n:
        return list(items)
    if n == 1:
        return [items[0]]
    idx = []
    for i in range(n):
        j = round(i * (len(items) - 1) / (n - 1))
        if j not in idx:
            idx.append(j)
    return [items[j] for j in idx]
