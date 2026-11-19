import sys
from pathlib import Path

ROOT = Path(__file__).parent
for p in (
    "packages/eap_core", "packages/eap_metamodel", "packages/eap_store",
    "services/governor", "services/runtime", "services/chat",
    "services/registry-api", "services/workflow", "services/eval-runner", "services/resolver",
    "services/compiler", "services/config-mcp", "sdk/python", "templates/mcp-server", ".",
):
    sys.path.insert(0, str(ROOT / p))
