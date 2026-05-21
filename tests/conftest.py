import asyncio
import pytest


@pytest.fixture(scope="session", autouse=True)
def _set_event_loop():
    """Python 3.12+ no longer creates an implicit event loop via get_event_loop().
    Create one explicitly so tests that use asyncio.get_event_loop() still work."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    loop.close()
