"""Read-only Windows desktop runtime detection."""

import os


def desktop_runtime():
    result = {
        "webview2": None,
        "required": os.name == "nt",
        "download": "https://developer.microsoft.com/microsoft-edge/webview2/",
    }
    if os.name != "nt":
        return result
    import winreg

    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for path in (r"SOFTWARE\Microsoft\EdgeUpdate\Clients", r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"):
            try:
                with winreg.OpenKey(hive, path) as key:
                    for i in range(winreg.QueryInfoKey(key)[0]):
                        try:
                            with winreg.OpenKey(key, winreg.EnumKey(key, i)) as child:
                                name = winreg.QueryValueEx(child, "name")[0]
                                version = winreg.QueryValueEx(child, "pv")[0]
                                if "webview2" in str(name).lower() and version != "0.0.0.0":
                                    result["webview2"] = str(version)
                        except OSError:
                            continue
            except OSError:
                continue
    return result
