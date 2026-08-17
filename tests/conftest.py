"""Make the package importable without installing it.

The package must be testable straight out of a checkout — that is half of
what "independently buildable" means — so the tests put ``src`` on the path
themselves rather than relying on an editable install. Notably this does
*not* import or require any host application; ``pytest tests`` and
``pytest`` from this directory behave identically.
"""
from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SRC = PACKAGE_ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
