import sys
from pathlib import Path

# Algo1.py / Config.py use flat, non-package imports (e.g. `from Config import
# MMConfig`), so the algo1_ directory itself must be importable as top-level
# modules. Adding it to sys.path here keeps that quirk contained to test
# collection instead of touching production code.
ALGO1_DIR = Path(__file__).resolve().parent.parent
if str(ALGO1_DIR) not in sys.path:
    sys.path.insert(0, str(ALGO1_DIR))
