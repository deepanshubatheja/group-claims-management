"""Backend health service for the restricted Phase 3 protocol."""

from __future__ import annotations

import platform
from collections.abc import Mapping

from group_claims_backend import __version__


def get_health_status() -> Mapping[str, object]:
    """Return runtime facts only; no local data or sensitive configuration is read."""

    return {
        "status": "ok",
        "applicationVersion": __version__,
        "pythonVersion": platform.python_version(),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
    }
