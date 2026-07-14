"""The distribution is named ``mantis-agent-sdk``; support the inferred import.

The canonical package is ``mantis_agent``, but users naturally try
``import mantis_agent_sdk`` after installing ``mantis-agent-sdk`` from PyPI.
This alias must behave exactly like the canonical public API.
"""

from __future__ import annotations


def test_mantis_agent_sdk_import_alias_exports_public_api() -> None:
    import mantis_agent
    import mantis_agent_sdk

    assert mantis_agent_sdk.__version__ == mantis_agent.__version__
    assert mantis_agent_sdk.__all__ == mantis_agent.__all__
    for name in mantis_agent.__all__:
        assert getattr(mantis_agent_sdk, name) is getattr(mantis_agent, name)
