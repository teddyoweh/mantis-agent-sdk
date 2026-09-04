"""Bring-your-own GPU provider: deploy any open model as an OpenAI-compatible endpoint.

Public surface::

    from mantis_agent.deploy import (
        deploy, connect, teardown, list_deployments, status, logs,
        providers, gpus, inspect_model, search_models, fit,
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
from .manager import (
    connect,
    deploy,
    fit,
    gpus,
    inspect_model,
    list_deployments,
    logs,
    providers,
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
    "providers",
    "register_provider",
    "search_models",
    "status",
    "teardown",
]
