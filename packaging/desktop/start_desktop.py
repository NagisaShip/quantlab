"""Entry point for the bundled CPython runtime (no system Python required)."""

import os
import sys
import traceback
from pathlib import Path

root = Path(__file__).resolve().parent
sys.path.insert(0, str(root / "src"))
os.environ["PYTHONPATH"] = str(root / "src")
try:
    from quant_agent.local.launcher import main

    raise SystemExit(main())
except Exception as exc:
    log = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "QuantLab" / "startup-error.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(traceback.format_exc(), encoding="utf-8")
    if os.name == "nt":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, str(exc) + "\n详情见：" + str(log), "QuantLab", 16)
    raise
