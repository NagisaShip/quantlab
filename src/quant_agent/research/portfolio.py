"""Compatibility import: use the shared research engine without duplicating algorithms."""

import sys

from quantlab_research.engines import portfolio as _engine

sys.modules[__name__] = _engine
