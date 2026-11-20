"""Shared core for the Enterprise Agent Platform.

Both the Governor (control plane) and the Agent Runtime (data plane) depend on this
package; neither depends on the other's code. They talk only over HTTP APIs and events.
"""

__version__ = "0.1.0"


def versioned_index(static_dir):  # type: ignore[no-untyped-def]
    """index.html with ?v=<content hash> on /static/ assets, so a new deployment is never mixed
    with JavaScript or CSS cached by the browser from the previous one."""
    import hashlib
    import re
    from pathlib import Path

    d = Path(static_dir)
    h = hashlib.sha256()
    for f in sorted(d.rglob("*")):
        if f.is_file():
            h.update(f.name.encode() + f.read_bytes())
    v = h.hexdigest()[:12]
    html = (d / "index.html").read_text()
    return re.sub(r'(/static/[\w./-]+\.(?:js|css))"', rf'\1?v={v}"', html), v
