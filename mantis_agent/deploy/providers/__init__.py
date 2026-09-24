"""Built-in deploy provider adapters. Importing this package registers them.

Each module defines one ``@register_provider`` class. Keep imports lazy and
cheap: no provider SDKs at module top (Modal's SDK is imported inside the
adapter's methods only).
"""

from __future__ import annotations

import importlib
import logging

_log = logging.getLogger(__name__)

# One module per provider; each is imported for its registration side effect.
# A broken/missing optional module must not take the others down.
_MODULES = ("runpod", "hf_endpoints", "modal_deploy", "deepinfra", "baseten", "vastai", "fireworks_dedicated")

for _name in _MODULES:
    try:
        importlib.import_module(f"{__name__}.{_name}")
    except ImportError as e:  # pragma: no cover - only when a module is absent mid-build
        _log.debug("deploy provider %s not loaded: %s", _name, e)
