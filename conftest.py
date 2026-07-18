import os
import sys

_here = os.path.dirname(__file__)
# src-layout: make the `matrix_dispatcher` package importable without requiring
# an editable install (CI installs it, but a bare `pytest` run should still work).
sys.path.insert(0, os.path.join(_here, "src"))
# Repo root too, so the `dispatcher.py` entry shim remains importable if referenced.
sys.path.insert(0, _here)
