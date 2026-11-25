"""Adapters for external CLI models and agents."""

from mesh.adapters.base import (
    Adapter,
    Capability,
    CommandSpec,
    Probe,
    RunRequest,
    RunResult,
    AdapterNotInstalled,
    AdapterUnsupported,
    EffortNotAllowed,
    EffortUnsupported,
    ProfileUnsupported,
    PromptTooLong,
    TestDoubleDisabled,
)
from mesh.adapters.registry import get_adapter, list_adapters

__all__ = [
    "Adapter",
    "Capability",
    "CommandSpec",
    "Probe",
    "RunRequest",
    "RunResult",
    "AdapterNotInstalled",
    "AdapterUnsupported",
    "EffortNotAllowed",
    "EffortUnsupported",
    "ProfileUnsupported",
    "PromptTooLong",
    "TestDoubleDisabled",
    "get_adapter",
    "list_adapters",
]
