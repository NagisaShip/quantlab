# QuantLab v 0.1 preview · Isolation Capability Matrix

> Evaluated 2026-10-18. Results reflect current code and local probes. “Partially effective” is never displayed as “fully isolated”.

[中文](隔离能力矩阵.md) | [Documentation index](README.en.md) | [Back to README](../README.md)

## 1. Matrix

| Capability | Linux root/container | Linux regular user | Windows development | Notes |
|---|---|---|---|---|
| Separate subprocess | Yes | Yes | Yes | Strategies run in a separate Python process |
| Environment scrubbing | Yes | Yes | Yes | Minimal environment only |
| Temporary working directory | Yes | Yes | Yes | One directory per run, removed afterward |
| Timeout termination | Yes | Yes | Yes | Hard time limit |
| CPU / memory / file-size limits | Yes | Yes | No | POSIX `setrlimit` |
| Privilege drop (`nobody`) | Yes | Needs permission | No | POSIX uid/gid |
| New session / process group | Yes | Yes | No | POSIX `setsid` |
| Network namespace / offline mode | Yes | Needs permission | No | Linux network namespaces |
| AST source review | Yes | Yes | Yes | Blocks network, file I/O and dangerous imports |
| Package integrity | Yes | Yes | Yes | SHA-256 and `.qstrat` manifest verification |

## 2. Code references

- `src/quant_agent/platform/isolation.py`: POSIX `_preexec()` for rlimits, sessions, privilege drop and optional network namespaces.
- `src/quant_agent/platform/sandbox_exec.py`: JSON-line protocol with the strategy subprocess.
- `src/quant_agent/sdk/runner.py`: `FORBIDDEN_NAMES` and AST checks for dangerous imports and calls.

## 3. UI behavior

The runtime banner reports `verified` or `partially effective` for strategy isolation. Windows normally shows `partially effective`; this is expected.

## 4. Deployment guidance

### Production / public trial

- Use Linux containers.
- Run API and worker as non-root while granting only the capabilities required for rlimits/network namespaces.
- Use an independent network, read-only root filesystem, quotas and centralized logs.
- Never mount the host Docker socket into the application container.

### Windows development

- Development and demonstration only.
- Do not assume resource limits or network isolation.
- Run micro-live and real-money operations on a Linux deployment.

## 5. Known limitations

1. Isolation depends on OS, kernel and container permissions.
2. `probe()` reflects only the current process permissions.
3. Micro live depends on exchange stops, key permissions and capital limits rather than the strategy sandbox.
4. Real perpetuals add a separate switch, secret store, futures-account checks, exposure limits, exchange stops and a kill file, but still are not a production exchange risk engine.
