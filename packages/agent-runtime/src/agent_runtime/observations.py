"""Optional live model observations; never required for execution correctness."""

import hashlib
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelTraceContext:
    trace_id: int
    span_id: int

    @property
    def traceparent(self) -> str:
        return f"00-{self.trace_id:032x}-{self.span_id:016x}-01"


def span_identity(value: str, size: int) -> int:
    return int.from_bytes(hashlib.sha256(value.encode()).digest()[:size], "big") or 1


def model_trace_context(root_run_id: str, attempt_id: str) -> ModelTraceContext:
    return ModelTraceContext(span_identity("run:" + root_run_id, 16), span_identity("attempt:" + attempt_id, 8))


_trace: ContextVar[ModelTraceContext | None] = ContextVar("runtime_model_trace", default=None)


def current_model_trace() -> ModelTraceContext | None:
    """Read inside generate; capture explicitly before handing work to a new thread."""
    return _trace.get()


_observer: ContextVar[Callable[[], bool] | None] = ContextVar("runtime_first_token", default=None)


@contextmanager
def model_observation(
    callback: Callable[[], bool], *, trace_context: ModelTraceContext | None = None
) -> Iterator[None]:
    token = _observer.set(callback)
    trace_token = _trace.set(trace_context)
    try:
        yield
    finally:
        _trace.reset(trace_token)
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
