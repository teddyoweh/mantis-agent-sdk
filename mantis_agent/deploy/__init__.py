"""Bring-your-own GPU provider: deploy any open model as an OpenAI-compatible endpoint.

Public surface::

    from mantis_agent.deploy import (
        deploy, connect, teardown, list_deployments, status, logs,
        gpus, inspect_model, search_models, fit,
    )

See :mod:`mantis_agent.deploy.base` for the types and the provider contract,
:mod:`mantis_agent.deploy.manager` for the high-level operations.
"""

from .base import (
    DEPLOY_PROVIDERS,
    Account,
    CostEstimate,
    CredentialField,
    DeployError,
    DeployOpts,
    DeployProvider,
    Deployment,
    DeploymentStatus,
    Engine,
    GpuFamily,
    GpuSpec,
    ModelInfo,
    NotSupported,
    get_provider,
    register_provider,
)
# NOTE: ``manager.providers`` is deliberately NOT re-exported here. This
# package contains a ``providers`` SUBPACKAGE (the adapters), and importing it
# — which every manager call does, to register the built-ins — rebinds
# ``mantis_agent.deploy.providers`` from any function we bound to the module
# object. A name that stops being callable partway through a process is worse
# than no name at all, so the list is reached through ``manager.providers``,
# which is what every caller already uses.
from .manager import (
    connect,
    deploy,
    fit,
    gpus,
    inspect_model,
    list_deployments,
    logs,
    search_models,
    status,
    teardown,
)

__all__ = [
    "DEPLOY_PROVIDERS",
    "Account",
    "CostEstimate",
    "CredentialField",
    "DeployError",
    "DeployOpts",
    "DeployProvider",
    "Deployment",
    "DeploymentStatus",
    "Engine",
    "GpuFamily",
    "GpuSpec",
    "ModelInfo",
    "NotSupported",
    "connect",
    "deploy",
    "fit",
    "get_provider",
    "gpus",
    "inspect_model",
    "list_deployments",
    "logs",
    "register_provider",
    "search_models",
    "status",
    "teardown",
]
