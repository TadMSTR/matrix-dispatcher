import os
import sys

# Ensure the repo root (where dispatcher.py lives) is importable when pytest
# collects tests from the tests/ subdirectory.
sys.path.insert(0, os.path.dirname(__file__))
