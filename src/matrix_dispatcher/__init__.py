"""matrix-dispatcher — spawn or resume ``claude -p`` sessions from Matrix rooms.

The package is the src-layout successor to the former flat ``dispatcher.py``
monolith. The root ``dispatcher.py`` is now a thin entry shim that keeps the live
PM2 service working with zero deploy change; the console script and ``python -m
matrix_dispatcher`` both route through :mod:`matrix_dispatcher.__main__`.

Module layout (import DAG points downward, no cycles):

    config  ← db, transcripts  ← matrixio  ← runner  ← hitl  ← commands  ← app
"""

__version__ = "0.7.0"
