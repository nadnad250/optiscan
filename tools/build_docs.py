"""Construit le site statique GitHub Pages dans docs/.

Copie le dashboard (mode statique auto-détecté côté JS), le guide,
le dernier scan et le journal prospectif. Lancé par le workflow après
chaque scan quotidien : le site reflète toujours le dernier état.
"""
import shutil
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DOCS = BASE / "docs"
DATA = DOCS / "data"


def build() -> None:
    DATA.mkdir(parents=True, exist_ok=True)

    web = BASE / "scanner" / "web"
    shutil.copy(web / "index.html", DOCS / "index.html")
    shutil.copy(web / "guide.html", DOCS / "guide.html")

    outputs = sorted((BASE / "output").glob("opportunites_*.json"))
    if outputs:
        shutil.copy(outputs[-1], DATA / "latest.json")

    journal = BASE / "output" / "journal_prospectif.csv"
    if journal.exists():
        shutil.copy(journal, DATA / "journal_prospectif.csv")

    (DOCS / ".nojekyll").touch()
    print(f"Site statique construit dans {DOCS}")


if __name__ == "__main__":
    build()
