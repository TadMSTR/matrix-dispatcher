"""Entry shim — keeps ``python dispatcher.py`` working after the monolith split.

The dispatcher now lives in the ``matrix_dispatcher`` package under ``src/``. The
live PM2 service (``matrix-dispatcher-forge``) execs THIS file directly via the
on-disk ``start-forge.sh``, so it must run without an editable install: prepend
``src/`` to ``sys.path`` before importing the package. Do NOT delete this file —
deleting it breaks the running service. The clean entry points are the
``matrix-dispatcher`` console script and ``python -m matrix_dispatcher``.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from matrix_dispatcher.__main__ import main

if __name__ == "__main__":
    main()
