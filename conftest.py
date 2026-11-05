import sys
from pathlib import Path

ROOT = Path(__file__).parent
for p in ("packages/eap_core", "services/governor", "services/runtime", "."):
    sys.path.insert(0, str(ROOT / p))
