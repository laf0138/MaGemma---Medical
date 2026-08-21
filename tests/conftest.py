import os
import sys

os.environ.setdefault("SPECTER_LOG", "/tmp/specter_test.log")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPECTER_SRC = os.path.join(ROOT, "specter")
if SPECTER_SRC not in sys.path:
    sys.path.insert(0, SPECTER_SRC)
