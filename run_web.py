"""Lanceur de l'interface web OptiScan (exécutable depuis n'importe quel dossier)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scanner.web.server import main

if __name__ == "__main__":
    main()
