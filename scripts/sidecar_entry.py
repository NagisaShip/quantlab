"""PyInstaller entry point for the frozen local backend sidecar."""

from quant_agent.local.launcher import main

if __name__ == "__main__":
    raise SystemExit(main())
