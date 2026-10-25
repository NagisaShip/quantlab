"""Optional live model observations; never required for execution correctness."""

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_observer: ContextVar[Callable[[], bool] | None] = ContextVar("runtime_first_token", default=None)


@contextmanager
def model_observation(callback: Callable[[], bool]) -> Iterator[None]:
    token = _observer.set(callback)
    try:
        yield
    finally:
        _observer.reset(token)


def first_token_observer() -> Callable[[], bool]:
    """Capture inside generate; a streaming client may invoke it on another thread."""
    callback = _observer.get()
    recorded = False

    def observe() -> bool:
        nonlocal recorded
        if callback is None or recorded:
            return False
        try:
            recorded = callback()
            return recorded
        except Exception:
            logging.getLogger(__name__).exception("first-token observation failed")
            return False

    return observe


def observe_first_token() -> bool:
    """Call only for a nonempty provider streaming delta, never a final response."""
    callback = _observer.get()
    if callback is None:
        return False
    try:
        return callback()
    except Exception:
        logging.getLogger(__name__).exception("first-token observation failed")
        return False
