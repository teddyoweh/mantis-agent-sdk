"""Compatibility import alias for the ``mantis-agent-sdk`` distribution.

The canonical import package is :mod:`mantis_agent`, but users often infer
``mantis_agent_sdk`` from the PyPI distribution name. Re-export the public API
so both imports work.
"""

from __future__ import annotations

from mantis_agent import *  # noqa: F403
from mantis_agent import __all__, __version__
