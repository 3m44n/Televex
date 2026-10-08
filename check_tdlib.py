#!/usr/bin/env python3
"""Verify python-telegram + libtdjson work on this machine.

Usage:
    python check_tdlib.py                      # tests the bundled lib + /usr/lib/libtdjson.so if present
    python check_tdlib.py /path/to/libtdjson.so

The GUI's "Check TDLib" button calls run_checks() directly.
"""
from __future__ import annotations

import ctypes
import inspect
import json
import sys
from pathlib import Path
from typing import Callable, Optional


def run_checks(lib_arg: Optional[str] = None, emit: Callable[[str], None] = print) -> int:
    """Run all checks, sending each output line to ``emit``.

    Returns 0 if at least one libtdjson loads, 1 if python-telegram cannot be imported,
    2 if no libtdjson works.
    """
    emit(f"Python: {sys.version.split()[0]}")

    try:
        import telegram
        from telegram.client import Telegram
        from telegram.utils import AsyncResult
    except Exception as exc:  # noqa: BLE001
        emit(f"FAIL: cannot import python-telegram: {exc!r}")
        emit("      If the error mentions pkg_resources: pip install \"setuptools<81\"")
        return 1

    try:
        from importlib.metadata import version

        emit(f"python-telegram: {version('python-telegram')}")
    except Exception:  # noqa: BLE001
        pass

    pkg = Path(telegram.__file__).parent
    emit(f"package dir: {pkg}")

    # ---- locate candidate libtdjson files ---------------------------------------
    if lib_arg:
        candidates = [Path(lib_arg)]
    else:
        candidates = sorted(pkg.glob("lib/**/libtdjson*"))
        for p in ("/usr/lib/libtdjson.so", "/usr/local/lib/libtdjson.so"):
            if Path(p).exists():
                candidates.append(Path(p))

    if not candidates:
        emit("FAIL: no libtdjson found (bundled or system)")

    working = []
    for lib in candidates:
        emit(f"\n--- {lib}")
        try:
            dll = ctypes.CDLL(str(lib))
        except OSError as exc:
            emit(f"  LOAD FAILED: {exc}")
            emit(f"  hint: run  ldd {lib} | grep 'not found'")
            continue
        try:
            dll.td_execute.restype = ctypes.c_char_p
            dll.td_execute.argtypes = [ctypes.c_char_p]
            raw = dll.td_execute(json.dumps({"@type": "getOption", "name": "version"}).encode())
            emit(f"  LOADED OK, TDLib version: {json.loads(raw).get('value')}")
        except Exception as exc:  # noqa: BLE001
            emit(f"  loaded, but td_execute probe failed: {exc!r}")
        working.append(lib)

    # ---- API surface the downloader relies on -----------------------------------
    emit("\n--- python-telegram API surface")
    for label, obj in [
        ("Telegram.__init__", Telegram.__init__),
        ("Telegram.login", Telegram.login),
        ("Telegram.send_code", getattr(Telegram, "send_code", None)),
        ("Telegram.send_password", getattr(Telegram, "send_password", None)),
        ("Telegram.call_method", Telegram.call_method),
        ("Telegram.add_update_handler", Telegram.add_update_handler),
        ("Telegram.stop", Telegram.stop),
        ("AsyncResult.wait", AsyncResult.wait),
    ]:
        if obj is None:
            emit(f"  {label}: MISSING (upgrade python-telegram: pip install -U python-telegram)")
            continue
        try:
            emit(f"  {label}{inspect.signature(obj)}")
        except Exception as exc:  # noqa: BLE001
            emit(f"  {label}: <{exc!r}>")

    try:
        src = inspect.getsource(AsyncResult)
        for attr in ("error_info", "self.error", "self.update"):
            emit(f"  AsyncResult uses '{attr}': {attr in src}")
    except (OSError, TypeError):  # no .py source inside a packaged app
        emit("  (AsyncResult source not available in a packaged app; skipped)")

    emit("\nRESULT: " + ("OK - at least one libtdjson loads" if working else "NO WORKING libtdjson"))
    if working:
        emit("Use with downloader:  --lib <path>   (omit --lib to use the bundled one if it was listed as OK)")
    return 0 if working else 2


if __name__ == "__main__":
    sys.exit(run_checks(sys.argv[1] if len(sys.argv) > 1 else None))
