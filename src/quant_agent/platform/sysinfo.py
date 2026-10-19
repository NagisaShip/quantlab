"""轻量系统信息：不引入 psutil，Windows 用 ctypes，Linux 读 /proc。

只读、失败返回 0，供 worker 心跳和部署管理员总览使用。
"""

from __future__ import annotations

import ctypes
import os
import threading
import time
from typing import Any


def process_memory() -> dict[str, int]:
    """当前进程的常驻内存与私有内存（字节）。失败返回 0。"""
    if os.name == "nt":
        try:
            from ctypes import wintypes

            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
                _fields_ = [*PROCESS_MEMORY_COUNTERS._fields_, ("PrivateUsage", ctypes.c_size_t)]

            counters = PROCESS_MEMORY_COUNTERS_EX()
            counters.cb = ctypes.sizeof(counters)
            psapi = ctypes.WinDLL("psapi")
            kernel32 = ctypes.windll.kernel32
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.GetProcessMemoryInfo.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX),
                wintypes.DWORD,
            ]
            ok = psapi.GetProcessMemoryInfo(
                kernel32.GetCurrentProcess(),
                ctypes.byref(counters),
                ctypes.sizeof(counters),
            )
            if ok:
                return {
                    "rss_bytes": int(counters.WorkingSetSize),
                    "private_bytes": int(counters.PrivateUsage),
                    "peak_rss_bytes": int(counters.PeakWorkingSetSize),
                }
        except Exception:
            pass
        return {"rss_bytes": 0, "private_bytes": 0, "peak_rss_bytes": 0}

    try:
        values: dict[str, int] = {}
        with open("/proc/self/status", encoding="utf-8") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                if key in ("VmRSS", "VmHWM"):
                    values[key] = int(rest.strip().split()[0]) * 1024
        rss = values.get("VmRSS", 0)
        return {"rss_bytes": rss, "private_bytes": rss, "peak_rss_bytes": values.get("VmHWM", rss)}
    except Exception:
        return {"rss_bytes": 0, "private_bytes": 0, "peak_rss_bytes": 0}


def process_stats() -> dict[str, Any]:
    """worker 心跳用的进程指标。"""
    mem = process_memory()
    return {
        **mem,
        "cpu_seconds": round(float(time.process_time()), 3),
        "threads": threading.active_count(),
    }


def system_memory() -> dict[str, int | float]:
    """整机内存：total / available / used / percent（字节与百分比）。"""
    total = available = 0
    if os.name == "nt":
        try:

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MEMORYSTATUSEX()
            status.dwLength = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                total = int(status.ullTotalPhys)
                available = int(status.ullAvailPhys)
        except Exception:
            pass
    else:
        try:
            info: dict[str, int] = {}
            with open("/proc/meminfo", encoding="utf-8") as fh:
                for line in fh:
                    key, _, rest = line.partition(":")
                    info[key] = int(rest.strip().split()[0]) * 1024
            total = info.get("MemTotal", 0)
            available = info.get("MemAvailable", info.get("MemFree", 0))
        except Exception:
            try:
                total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
                available = total
            except Exception:
                pass
    used = max(0, total - available)
    return {
        "total": total,
        "available": available,
        "used": used,
        "percent": round((used / total) * 100, 1) if total else 0.0,
    }


__all__ = ["process_memory", "process_stats", "system_memory"]
