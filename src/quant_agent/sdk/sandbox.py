"""策略代码沙箱：AST 静态审查 + 受限内置函数 + 运行时长守卫。

为什么不只做字符串匹配：`__import__("o" + "s")`、`getattr(x, "__class__")`、
`().__class__.__mro__[1].__subclasses__()` 这类写法可以轻松绕开关键字黑名单。
这里在三层上把门：

1. **AST 审查**（加载前）：只允许白名单模块的 import；禁止 eval/exec/open 等内置；
   禁止访问以下划线开头的属性（堵住 `__class__` / `__globals__` / `_runtime` 这类逃逸路径）。
2. **受限内置**（执行时）：策略命名空间里的 `__builtins__` 只包含安全子集，
   `__import__` 被替换为白名单版本——即使 AST 审查漏了，运行时也拿不到 os / socket。
3. **时长守卫**（运行时）：对策略自身代码帧做行级计时，死循环会在超时后被打断，
   不会把整个服务拖死。C 层面的长计算（如超大 range 求和）不在此列，
   生产部署应再叠加进程级隔离与资源限额（见 README「生产部署提醒」）。

这些规则同时用于：编辑器即时检查、保存、回测、实时模拟与 Agent 生成代码的自检。
"""

from __future__ import annotations

import ast
import builtins
import re
import sys
import time
from typing import Any

# 允许策略 import 的模块（纯计算、无 IO）。numpy / pandas 若已安装也允许。
ALLOWED_MODULES = frozenset(
    {
        "math",
        "statistics",
        "random",
        "datetime",
        "collections",
        "itertools",
        "functools",
        "operator",
        "bisect",
        "heapq",
        "decimal",
        "fractions",
        "typing",
        "dataclasses",
        "enum",
        "json",
        "re",
        "copy",
        "string",
        "numpy",
        "pandas",
        "quant_agent",
        "quant_agent.sdk",
    }
)

# 明确拒绝的模块：给出更具体的提示（网络 / 进程 / 文件系统）
NETWORK_MODULES = frozenset(
    {
        "requests",
        "httpx",
        "urllib",
        "urllib3",
        "socket",
        "aiohttp",
        "http",
        "websocket",
        "websockets",
        "ftplib",
        "smtplib",
    }
)
SYSTEM_MODULES = frozenset(
    {
        "os",
        "sys",
        "subprocess",
        "shutil",
        "pathlib",
        "importlib",
        "ctypes",
        "multiprocessing",
        "threading",
        "signal",
        "pickle",
        "marshal",
        "builtins",
        "inspect",
        "gc",
        "io",
        "time",
        "asyncio",
        "tempfile",
        "glob",
        "platform",
    }
)

# 策略里不允许出现的内置函数调用
FORBIDDEN_BUILTINS = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "open",
        "__import__",
        "globals",
        "locals",
        "vars",
        "getattr",
        "setattr",
        "delattr",
        "input",
        "breakpoint",
        "exit",
        "quit",
        "help",
        "memoryview",
        "classmethod",
        "staticmethod",
        "super",
        "type",
        "object",
    }
)

# numpy / pandas 自带文件读写接口，这些属性名一律拒绝
_FILE_IO_ATTR = re.compile(
    r"^(read_\w+|to_(csv|excel|json|pickle|parquet|sql|hdf|feather|html|clipboard|stata|latex|markdown|xml|orc)"
    r"|load|loadtxt|save|savez|savez_compressed|savetxt|fromfile|tofile|genfromtxt|memmap|ctypeslib|DataSource)$"
)

# 安全的内置子集
_SAFE_BUILTIN_NAMES = (
    "abs",
    "all",
    "any",
    "ascii",
    "bin",
    "bool",
    "bytes",
    "callable",
    "chr",
    "complex",
    "dict",
    "dir",
    "divmod",
    "enumerate",
    "filter",
    "float",
    "format",
    "frozenset",
    "hasattr",
    "hash",
    "hex",
    "id",
    "int",
    "isinstance",
    "issubclass",
    "iter",
    "len",
    "list",
    "map",
    "max",
    "min",
    "next",
    "oct",
    "ord",
    "pow",
    "print",
    "range",
    "repr",
    "reversed",
    "round",
    "set",
    "slice",
    "sorted",
    "str",
    "sum",
    "tuple",
    "zip",
    "property",
    # 异常类型：策略里 try/except 常用
    "ArithmeticError",
    "AssertionError",
    "AttributeError",
    "Exception",
    "IndexError",
    "KeyError",
    "LookupError",
    "NameError",
    "OverflowError",
    "RuntimeError",
    "StopIteration",
    "TypeError",
    "ValueError",
    "ZeroDivisionError",
    "True",
    "False",
    "None",
    "NotImplemented",
)


def _module_root(name: str) -> str:
    return name.split(".")[0]


def module_allowed(name: str) -> bool:
    if _module_root(name) == "quant_agent":
        return name in {"quant_agent", "quant_agent.sdk", "quant_agent.sdk.ft"}  # ft：Freqtrade 兼容层
    return _module_root(name) in ALLOWED_MODULES


def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    if level != 0:
        raise ImportError("策略不支持相对导入")
    if not module_allowed(name):
        raise ImportError(f"策略运行在沙箱内，不允许导入 `{name}`")
    return builtins.__import__(name, globals, locals, fromlist, level)


def safe_builtins() -> dict[str, Any]:
    """供 exec 使用的受限 __builtins__。"""
    out: dict[str, Any] = {}
    for name in _SAFE_BUILTIN_NAMES:
        if hasattr(builtins, name):
            out[name] = getattr(builtins, name)
    out["__import__"] = _safe_import
    # 允许在策略里定义类（dataclass 风格的小工具类）
    out["__build_class__"] = builtins.__build_class__
    out["__name__"] = "strategy"
    return out


def audit(source: str) -> list[str]:
    """AST 审查。返回人能看懂的问题列表（空 = 通过）。语法错误不在这里报，由加载器给出行号。"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    issues: list[str] = []

    def add(node: ast.AST, msg: str) -> None:
        line = getattr(node, "lineno", None)
        text = f"第 {line} 行：{msg}" if line else msg
        if text not in issues:
            issues.append(text)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _check_module(alias.name, node, add)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                add(node, "不允许相对导入")
            elif node.module:
                _check_module(node.module, node, add)
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                add(node, f"不允许访问以下划线开头的属性 `{node.attr}`（沙箱边界）")
            elif _FILE_IO_ATTR.match(node.attr):
                add(node, f"不允许调用文件读写接口 `{node.attr}`：策略数据只能来自平台 API")
        elif isinstance(node, ast.Name):
            if node.id in FORBIDDEN_BUILTINS and isinstance(node.ctx, ast.Load):
                add(node, f"不允许使用内置函数 `{node.id}`（沙箱边界）")
            elif node.id.startswith("__") and node.id not in {"__name__", "__doc__"}:
                add(node, f"不允许使用双下划线名字 `{node.id}`")
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            # global 本身无害，但策略状态应放在 g 上，给出提示而不是拒绝
            continue
        elif isinstance(node, (ast.AsyncFunctionDef, ast.Await, ast.AsyncFor, ast.AsyncWith)):
            add(node, "策略回调必须是同步函数，不支持 async")
    return issues


def _check_module(name: str, node: ast.AST, add) -> None:
    root = _module_root(name)
    if root in NETWORK_MODULES:
        add(node, f"不允许导入 `{root}`：策略运行在无网络沙箱内")
    elif root in SYSTEM_MODULES:
        add(node, f"不允许导入 `{root}`：策略不能访问系统、进程或文件")
    elif not module_allowed(name):
        allowed = "、".join(sorted(m for m in ALLOWED_MODULES if "." not in m and m != "quant_agent"))
        add(node, f"不允许导入 `{name}`。可用模块：{allowed}")


class TimeGuard:
    """对策略代码做行级计时。超时后在策略代码内部抛出 StrategyTimeout。

    只追踪文件名等于 `filename` 的帧，平台自身代码不受影响、也几乎没有额外开销。
    """

    CHECK_EVERY = 2000  # 每执行这么多行看一次表

    def __init__(self, filename: str, seconds: float | None) -> None:
        self.filename = filename
        self.seconds = seconds
        self.deadline = 0.0
        self._count = 0
        self._previous = None

    def __enter__(self) -> TimeGuard:
        if not self.seconds:
            return self
        self.deadline = time.monotonic() + self.seconds
        self._previous = sys.gettrace()
        sys.settrace(self._global)
        return self

    def __exit__(self, *exc: Any) -> None:
        if self.seconds:
            sys.settrace(self._previous)

    def _global(self, frame, event, arg):
        if event == "call" and frame.f_code.co_filename == self.filename:
            return self._local
        return None

    def _local(self, frame, event, arg):
        if event == "line":
            self._count += 1
            if self._count % self.CHECK_EVERY == 0 and time.monotonic() > self.deadline:
                raise StrategyTimeout(f"策略运行超过 {self.seconds:g} 秒，已中止。请检查是否存在死循环或过重的计算。")
        return self._local


class StrategyTimeout(RuntimeError):
    """策略运行超时。"""
