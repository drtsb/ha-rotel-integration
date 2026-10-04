"""Shared test fixtures.

The integration is loaded twice on purpose:

* as the synthetic package ``rotel_control_under_test`` for the standalone
  protocol/TCP tests, which must run without a Home Assistant instance;
* through Home Assistant itself (``custom_components.rotel_control``) for
  the end-to-end tests.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest
from fake_rotel import FakeRotel

REPO_ROOT = Path(__file__).parent.parent
INTEGRATION_PATH = REPO_ROOT / "custom_components" / "rotel_control"
PACKAGE_NAME = "rotel_control_under_test"

# Make the integration importable as custom_components.rotel_control.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_package() -> types.ModuleType:
    """Register the integration folder as an importable package."""
    if PACKAGE_NAME in sys.modules:
        return sys.modules[PACKAGE_NAME]
    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [str(INTEGRATION_PATH)]  # type: ignore[attr-defined]
    sys.modules[PACKAGE_NAME] = package
    return package


_load_package()


@pytest.fixture(scope="session")
def api_module() -> types.ModuleType:
    """Return the api module of the integration."""
    name = f"{PACKAGE_NAME}.api"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, INTEGRATION_PATH / "api.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def protocol_module() -> types.ModuleType:
    """Return the protocol module of the integration."""
    name = f"{PACKAGE_NAME}.protocol"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, INTEGRATION_PATH / "protocol.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(name="device")
async def device_fixture(socket_enabled: None):
    """Start a fake amplifier listening on a loopback port.

    Depending on ``socket_enabled`` lifts the sandbox that pytest-socket
    installs in Home Assistant test suites: the fake device needs a real
    loopback listener.
    """
    device = FakeRotel()
    await device.start()
    yield device
    await device.stop()
