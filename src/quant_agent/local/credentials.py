"""Protect the local project's encryption master key with Windows CurrentUser DPAPI."""

from __future__ import annotations

import base64
import ctypes
import hashlib
import os
from pathlib import Path


def crypt(data, protect=True):
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    incoming = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    out = Blob()
    api = ctypes.WinDLL("crypt32", use_last_error=True)
    function = api.CryptProtectData if protect else api.CryptUnprotectData
    function.argtypes = [
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(Blob),
    ]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(out)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(out.data, out.size)
    finally:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree(out.data)


def master_key(home):
    if os.name != "nt":
        raise RuntimeError("Windows DPAPI仅在Windows可用")
    base = Path(
        os.environ.get("QUANTLAB_CREDENTIALS")
        or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "QuantLab" / "keys"
    )
    base.mkdir(parents=True, exist_ok=True)
    identity = hashlib.sha256(str(Path(home).resolve()).encode()).hexdigest()
    path = base / (identity + ".dpapi")
    if path.exists():
        return crypt(path.read_bytes(), False)
    key = base64.urlsafe_b64encode(os.urandom(32))
    temp = path.with_suffix(".tmp")
    temp.write_bytes(crypt(key))
    os.replace(temp, path)
    return key
