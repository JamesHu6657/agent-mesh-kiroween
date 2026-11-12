"""Registry of model/agent execution adapters."""

import os
from typing import Any, Dict, List, Optional, Type

import mesh.constants as c
from mesh.errors import MeshError
from mesh.adapters.base import (
    Adapter,
    TestDoubleDisabled,
)
from mesh.adapters.droid import DroidAdapter
from mesh.adapters.agy import AgyAdapter
from mesh.adapters.kiro import KiroAdapter
from mesh.adapters.vibe import VibeAdapter
from mesh.adapters.hermes import HermesAdapter
from mesh.adapters.openclaw import OpenclawAdapter
from mesh.adapters.none_adapter import NoneAdapter
from mesh.adapters.fake import FakeAdapter


ADAPTER_CLASSES: Dict[str, Type[Adapter]] = {
    "droid": DroidAdapter,
    "agy": AgyAdapter,
    "kiro": KiroAdapter,
    "vibe": VibeAdapter,
    "hermes": HermesAdapter,
    "openclaw": OpenclawAdapter,
    "none": NoneAdapter,
    "fake": FakeAdapter,
}

STANDARD_ADAPTER_ORDER = [
    "droid",
    "agy",
    "kiro",
    "vibe",
    "hermes",
    "openclaw",
    "none",
]


def is_testing_mode(testing_mode: Optional[bool] = None) -> bool:
    if testing_mode is not None:
        return testing_mode
    return os.environ.get("MESH_TESTING") == "1"


def get_adapter(
    adapter_id: str,
    testing_mode: Optional[bool] = None,
    options: Optional[Dict[str, Any]] = None,
) -> Adapter:
    """Retrieve an adapter by id.
    
    Raises TestDoubleDisabled if 'fake' is requested outside testing mode.
    Raises MeshError(INVALID_ARGUMENT) if adapter_id is unknown.
    """
    if adapter_id not in ADAPTER_CLASSES:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"Unknown adapter '{adapter_id}'",
            details={"param": "adapter", "adapter": adapter_id},
        )

    if adapter_id == "fake":
        if not is_testing_mode(testing_mode):
            raise TestDoubleDisabled()
        resume_verified = False
        if options:
            resume_verified = bool(options.get("resume_verified", False))
        return FakeAdapter(resume_verified=resume_verified)

    cls = ADAPTER_CLASSES[adapter_id]
    return cls()


def list_adapters(testing_mode: Optional[bool] = None) -> List[Adapter]:
    """List all registered adapters.
    
    Includes 'fake' only when in testing mode.
    """
    adapters: List[Adapter] = []
    for aid in STANDARD_ADAPTER_ORDER:
        adapters.append(get_adapter(aid, testing_mode=testing_mode))

    if is_testing_mode(testing_mode):
        adapters.append(get_adapter("fake", testing_mode=True))

    return adapters
