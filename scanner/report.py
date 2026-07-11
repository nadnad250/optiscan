"""Affichage console (rich) et export JSON des opportunités."""
import json
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .models import Opportunity

KIND_LABELS = {
    "activite_inhabituelle": "[red]Flux inhabituel[/]",
    "prime_riche": "[green]Prime riche[/]",
    "options_bon_marche": "[cyan]IV bon marché[/]",
    "skew_peur": "[magenta]Skew extrême[/]",
    "skew_inverse": "[bold magenta]Skew inversé[/]",
    "earnings_iv": "[yellow]Earnings/IV[/]",
    "put_cash_secured": "[green]Put cash-secured[/]",
    "strategie_optimale": "[bold yellow]Stratégie optimale[/]",
    "achat_call": "[bold green]ACHAT CALL (hausse)[/]",
    "achat_put": "[bold red]ACHAT PUT (baisse)[/]",
    "ml_mouvement": "[bold blue]IA : mouvement imminent[/]",
}


def print_report(opps: list[Opportunity], top_n: int, source: str) -> None:
    console = Console()
    console.print(f"\n[bold cyan]OptiScan[/] — {len(opps)} opportunités détectées "
                  f"(source : {source}, {datetime.now():%d/%m/%Y %H:%M})\n")
    if not opps:
        console.print("[yellow]Aucune opportunité au-dessus des seuils actuels. "
                      "Assouplis les seuils dans config.json ou élargis la watchlist.[/]")
        return

    table = Table(show_lines=True)
    table.add_column("Score", justify="center", style="bold")
    table.add_column("Ticker", style="bold cyan")
    table.add_column("Signal")
    table.add_column("Détail", max_width=70)
    table.add_column("Action suggérée", max_width=45, style="green")

    for opp in opps[:top_n]:
        color = "red" if opp.score >= 70 else "yellow" if opp.score >= 50 else "white"
        table.add_row(
            f"[{color}]{opp.score:.0f}[/]",
            opp.ticker,
            KIND_LABELS.get(opp.kind, opp.kind),
            opp.summary,
            opp.strategy,
        )
    console.print(table)
    delay_note = ("Données IBKR différées (~15 min) sans abonnement temps réel."
                  if source == "ib" else "Données Yahoo différées (~15 min).")
    console.print(f"[dim]Outil d'aide à la décision — aucun ordre n'est envoyé. {delay_note}[/]\n")


def save_json(opps: list[Opportunity], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"opportunites_{datetime.now():%Y%m%d_%H%M}.json"
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "count": len(opps),
        "opportunities": [o.to_dict() for o in opps],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path
