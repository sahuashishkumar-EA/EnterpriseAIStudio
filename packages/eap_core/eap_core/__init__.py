"""Shared core for the Enterprise Agent Platform.

Both the Governor (control plane) and the Agent Runtime (data plane) depend on this
package; neither depends on the other's code. They talk only over HTTP APIs and events.
"""

__version__ = "0.1.0"
