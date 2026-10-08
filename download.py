#!/usr/bin/env python3
"""Bulk-download every file from Telegram using TDLib (python-telegram).

Features: native async TDLib downloads, bounded concurrency, resume (TDLib keeps partial
files), stall/99% auto-retry, skip-complete, persistent state, progress + aggregate speed.

This module is both the command-line tool (``python download.py``) and the engine behind
``gui.py``: both call ``run_download(settings, callbacks, stop_event)``.

Files created in DATA_DIR (next to this script, or in a per-user data folder when packaged):
    config.json       api_id / api_hash / phone / db key   (chmod 600)
    state_*.json      msg_id -> destination + done flag (per chat)
    tdlib-data/       TDLib database + partial downloads   (keep it private! it holds your session)
Final files go to --out (default: downloads/ next to the script).
"""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import signal
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from telegram.client import AuthorizationState, Telegram

# --------------------------------------------------------------------------- paths
APP_NAME = "TelegramBulkDownloader"
FROZEN = bool(getattr(sys, "frozen", False))  # True inside a PyInstaller bundle
if FROZEN:  # packaged app: the folder next to the executable is not a place to keep a session
    from platformdirs import user_data_dir

    DATA_DIR = Path(user_data_dir(APP_NAME, appauthor=False))
    DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    DEFAULT_OUT = Path.home() / "Downloads" / "Telegram"
else:
    DATA_DIR = Path(__file__).resolve().parent
    DEFAULT_OUT = DATA_DIR / "downloads"
CONFIG_PATH = DATA_DIR / "config.json"
TD_DIR = DATA_DIR / "tdlib-data"
IS_TTY = bool(sys.stdout) and sys.stdout.isatty()  # sys.stdout is None in a windowed app


# --------------------------------------------------------------------------- helpers
def fmt_bytes(n: float) -> str:
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def fmt_eta(seconds) -> str:
    if seconds is None or seconds != seconds or seconds > 10**7:
        return "--:--:--"
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def log(msg: str) -> None:
    """Terminal logger (CLI only): print a line without being garbled by the live status line."""
    if IS_TTY:
        sys.stdout.write("\r\x1b[2K")
    print(msg, flush=True)


def write_json_atomic(path: Path, data, private: bool = False) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def safe_name(name: str, limit: int = 235) -> str:
    name = re.sub(r"[\x00-\x1f/]", "_", name).strip().strip(".") or "unnamed"
    stem, ext = os.path.splitext(name)
    while len((stem + ext).encode()) > limit and stem:
        stem = stem[:-1]
    return (stem + ext) or "unnamed"


# --------------------------------------------------------------------------- errors
class UserError(Exception):
    """A problem the user can fix. ``title`` and ``str(exc)`` are safe to show in a dialog."""

    def __init__(self, title: str, message: str):
        super().__init__(message)
        self.title = title


class Stopped(Exception):
    """Raised inside a run when the stop event is set while we were waiting."""


# --------------------------------------------------------------------------- settings / callbacks
LogCb = Callable[[str], None]
# (done_bytes, total_bytes, speed B/s, eta seconds or None, done_files, total_files, active, retrying)
ProgressCb = Callable[[int, int, float, Optional[float], int, int, int, int], None]


@dataclass
class Settings:
    """Everything one run needs. Built from CLI args by main() or from the form by gui.py."""

    api_id: int
    api_hash: str
    phone: str
    db_key: str
    target: str = ""  # chat id / @username / t.me link; blank = Saved Messages
    out_dir: Path = DEFAULT_OUT
    layout: str = "flat"  # flat | type | month
    concurrency: int = 5
    stall_timeout: int = 60
    max_attempts: int = 8
    photos: bool = False
    list_only: bool = False  # scan and report, download nothing
    lib: Optional[str] = None  # path to libtdjson (None = the one bundled with python-telegram)


def _noop(*_args, **_kwargs) -> None:
    pass


def _no_input() -> Optional[str]:
    return None


@dataclass
class Callbacks:
    """Hooks supplied by the caller. Prompts return the typed text, or None if cancelled."""

    on_log: LogCb = _noop
    on_progress: ProgressCb = _noop
    on_scan: Callable[[int, int], None] = _noop  # (messages scanned, files found)
    on_need_code: Callable[[], Optional[str]] = _no_input
    on_need_password: Callable[[], Optional[str]] = _no_input


# --------------------------------------------------------------------------- config
ENV = {"api_id": "TG_API_ID", "api_hash": "TG_API_HASH", "phone": "TG_PHONE"}
PROMPTS = {
    "api_id": ("Telegram API ID: ", False),
    "api_hash": ("Telegram API hash: ", True),
    "phone": ("Phone number, international format (e.g. +91XXXXXXXXXX): ", False),
}


def parse_api_id(value: str) -> int:
    try:
        api_id = int(str(value).strip())
    except ValueError:
        api_id = 0
    if api_id <= 0:
        raise UserError(
            "Invalid API ID",
            "The API ID must be a number, e.g. 1234567.\nYou can get it at https://my.telegram.org.",
        )
    return api_id


def normalize_phone(phone: str) -> str:
    return re.sub(r"[\s-]", "", phone)


def read_config() -> dict:
    """Return the contents of config.json ({} if it does not exist)."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        # Don't overwrite it: it holds the key that unlocks the TDLib session.
        raise UserError("Config unreadable", f"Cannot read {CONFIG_PATH}: {exc}\nFix or delete the file.") from exc


def save_config(stored: dict) -> None:
    write_json_atomic(CONFIG_PATH, stored, private=True)


def ensure_db_key(stored: dict) -> bool:
    """Add the random TDLib database key if missing. Returns True if ``stored`` changed."""
    if stored.get("db_key"):
        return False
    stored["db_key"] = secrets.token_hex(16)
    return True


def load_config() -> dict:
    """CLI only: env vars > config.json > interactive prompt. Saves anything newly entered."""
    stored = read_config()
    changed, cfg = False, {}
    for key, (prompt, secret) in PROMPTS.items():
        val = os.environ.get(ENV[key]) or stored.get(key)
        if not val:
            val = (getpass.getpass if secret else input)(prompt).strip()
            stored[key] = val
            changed = True
        cfg[key] = val
    changed = ensure_db_key(stored) or changed
    cfg["db_key"] = stored["db_key"]
    if changed:
        save_config(stored)
        log(f"Saved credentials to {CONFIG_PATH} (mode 600)")
    cfg["api_id"] = parse_api_id(cfg["api_id"])
    cfg["phone"] = normalize_phone(cfg["phone"])
    return cfg


# --------------------------------------------------------------------------- TDLib calls
class TdError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


class Client:
    """Thin wrapper over python-telegram's call_method with retry/backoff."""

    def __init__(self, tg: Telegram, log: LogCb, stop: threading.Event):
        self.tg, self.log, self.stop = tg, log, stop

    def call(self, method: str, params: dict | None = None, timeout: int = 60, retries: int = 6):
        delay = 2
        for attempt in range(1, retries + 1):
            try:
                res = self.tg.call_method(method, params or {})
                res.wait(timeout=timeout, raise_exc=False)
                if res.error:
                    info = res.error_info or {}
                    err = TdError(int(info.get("code", -1)), str(info.get("message", "unknown error")))
                elif res.update is None:
                    err = TdError(-1, "timeout")
                else:
                    return res.update
            except TimeoutError:
                err = TdError(-1, "timeout")
            if err.code in (400, 401, 403, 404) or attempt == retries:
                raise err
            m = re.search(r"retry after (\d+)", err.message, re.I)  # FloodWait-style
            wait = int(m.group(1)) + 1 if m else delay
            self.log(f"! {method}: {err} - retrying in {wait}s ({attempt}/{retries})")
            if self.stop.wait(wait):  # was time.sleep(wait); wakes up early on Stop
                raise Stopped()
            delay = min(delay * 2, 60)


# --------------------------------------------------------------------------- scanning
MEDIA = {  # content @type -> (folder/kind, key in content, key of the File inside it)
    "messageDocument": ("documents", "document", "document"),
    "messageVideo": ("videos", "video", "video"),
    "messageAudio": ("audio", "audio", "audio"),
    "messageAnimation": ("animations", "animation", "animation"),
    "messageVoiceNote": ("voice", "voice_note", "voice"),
    "messageVideoNote": ("video_notes", "video_note", "video"),
}


@dataclass
class Item:
    msg_id: int
    date: int
    kind: str
    file_id: int
    unique_id: str
    size: int
    name: str
    dest: Path | None = None


def extract(msg: dict, photos: bool) -> Item | None:
    content = msg.get("content") or {}
    ctype = content.get("@type")
    kind = fobj = None
    name = mime = ""
    if ctype in MEDIA:
        kind, outer, inner = MEDIA[ctype]
        media = content.get(outer) or {}
        fobj = media.get(inner)
        name = media.get("file_name") or ""
        mime = media.get("mime_type") or ""
    elif ctype == "messagePhoto" and photos:
        sizes = (content.get("photo") or {}).get("sizes") or []
        if sizes:
            best = max(sizes, key=lambda s: s.get("width", 0) * s.get("height", 0))
            kind, fobj, mime = "photos", best.get("photo"), "image/jpeg"
    if not fobj or fobj.get("id") is None:
        return None
    if (fobj.get("local") or {}).get("can_be_downloaded") is False:
        return None
    if not name:
        ext = (mimetypes.guess_extension(mime) if mime else None) or ""
        name = f"{kind}_{msg['id']}{ext}"
    return Item(
        msg_id=msg["id"],
        date=int(msg.get("date") or 0),
        kind=kind,
        file_id=fobj["id"],
        unique_id=(fobj.get("remote") or {}).get("unique_id") or f"local-{fobj['id']}",
        size=int(fobj.get("size") or fobj.get("expected_size") or 0),
        name=safe_name(name),
    )


def scan_history(client: Client, chat_id: int, photos: bool, on_scan: Callable[[int, int], None] = _noop) -> list[Item]:
    items: dict[int, Item] = {}
    seen: set[int] = set()
    from_id, strikes, last_print = 0, 0, 0.0
    while True:
        if client.stop.is_set():
            raise Stopped()
        res = client.call(
            "getChatHistory",
            {"chat_id": chat_id, "from_message_id": from_id, "offset": 0, "limit": 100, "only_local": False},
        )
        msgs = res.get("messages") or []
        new = [m for m in msgs if m["id"] not in seen]
        if not new:  # empty / only already-seen: TDLib may still be loading, so retry a few times
            strikes += 1
            if strikes >= 3:
                break
            client.stop.wait(1.0)  # was time.sleep(1.0)
            continue
        strikes = 0
        for m in new:
            seen.add(m["id"])
            it = extract(m, photos)
            if it:
                items[it.msg_id] = it
        from_id = min(m["id"] for m in msgs)
        if time.monotonic() - last_print > 0.5:
            last_print = time.monotonic()
            on_scan(len(seen), len(items))
    client.log(f"Scan complete: {len(seen)} messages, {len(items)} downloadable files")
    return sorted(items.values(), key=lambda i: i.msg_id)


# --------------------------------------------------------------------------- planning / state
def load_state(state_path: Path, log_fn: LogCb) -> dict:
    if state_path.exists():
        try:
            return json.loads(state_path.read_text())
        except json.JSONDecodeError:
            log_fn(f"! {state_path.name} is corrupt; rebuilding from files on disk")
    return {}


def plan(items: list[Item], state: dict, out_dir: Path, layout: str, state_path: Path):
    """Assign stable destination paths. Returns (to_download, skipped_count, skipped_bytes)."""
    reserved = {s["dest"]: int(mid) for mid, s in state.items()}
    todo, skipped, skipped_bytes = [], 0, 0
    for it in items:
        key = str(it.msg_id)
        entry = state.get(key)
        if entry:
            rel = entry["dest"]
        else:
            sub = {"type": it.kind, "month": datetime.fromtimestamp(it.date).strftime("%Y-%m"), "flat": ""}[layout]
            stem, ext = os.path.splitext(it.name)
            n, cand = 1, str(Path(sub) / it.name)
            while True:
                taken = cand in reserved and reserved[cand] != it.msg_id
                on_disk = (out_dir / cand).exists()
                adoptable = on_disk and cand not in reserved and (not it.size or (out_dir / cand).stat().st_size == it.size)
                if not taken and (not on_disk or adoptable):
                    break
                n += 1
                cand = str(Path(sub) / safe_name(f"{stem} ({n}){ext}"))
            rel = cand
            reserved[rel] = it.msg_id
            state[key] = {"dest": rel, "size": it.size, "unique_id": it.unique_id, "done": False}
        it.dest = out_dir / rel
        if it.dest.is_file() and (not it.size or it.dest.stat().st_size == it.size):
            state[key]["done"] = True
            skipped += 1
            skipped_bytes += it.size
        else:
            state[key]["done"] = False
            todo.append(it)
    write_json_atomic(state_path, state)
    return todo, skipped, skipped_bytes


# --------------------------------------------------------------------------- downloader
@dataclass
class Job:
    item: Item
    attempts: int = 0
    next_try: float = 0.0
    started: float = 0.0
    last_progress: float = 0.0
    last_bytes: int = 0


class Downloader:
    def __init__(
        self,
        client: Client,
        items: list[Item],
        state: dict,
        concurrency: int,
        stall: int,
        max_attempts: int,
        state_path: Path,
        on_progress: ProgressCb = _noop,
        stop: threading.Event | None = None,
    ):
        self.client, self.state = client, state
        self.log = client.log
        self.on_progress = on_progress
        self.stop = stop or threading.Event()
        self.jobs = [Job(i) for i in items]
        self.concurrency, self.stall, self.max_attempts = concurrency, stall, max_attempts
        self.state_path = state_path
        self.lock = threading.Lock()
        self.files: dict[int, dict] = {}  # file_id -> latest File object from updateFile
        self.watch = {i.file_id for i in items}
        self.total_bytes = sum(i.size for i in items)
        self.done_files = self.done_bytes = 0
        self.samples: deque = deque()
        client.tg.add_update_handler("updateFile", self._on_update_file)

    # called from python-telegram's worker thread
    def _on_update_file(self, update: dict) -> None:
        f = update.get("file") or {}
        if f.get("id") in self.watch:
            with self.lock:
                self.files[f["id"]] = f

    def _cancel(self, file_id: int) -> None:
        try:
            self.client.call("cancelDownloadFile", {"file_id": file_id, "only_if_pending": False}, retries=2)
        except TdError:
            pass

    def _start(self, job: Job) -> bool:
        it = job.item
        try:
            f = self.client.call(
                "downloadFile",
                {"file_id": it.file_id, "priority": 16, "offset": 0, "limit": 0, "synchronous": False},
                retries=3,
            )
        except TdError as exc:
            self.log(f"! could not start {it.name}: {exc}")
            return False
        with self.lock:
            self.files[it.file_id] = f
        now = time.monotonic()
        job.started = job.last_progress = now
        job.last_bytes = int((f.get("local") or {}).get("downloaded_size", 0))
        return True

    def _finalize(self, job: Job, f: dict) -> bool:
        it = job.item
        src = Path((f.get("local") or {}).get("path") or "")
        try:
            actual = src.stat().st_size
        except OSError:
            self.log(f"! {it.name}: TDLib reports complete but file is missing; will retry")
            return False
        if it.size and actual != it.size:
            self.log(f"! {it.name}: size mismatch ({actual} != {it.size}); discarding and retrying")
            try:
                self.client.call("deleteFile", {"file_id": it.file_id}, retries=2)
            except TdError:
                pass
            return False
        it.dest.parent.mkdir(parents=True, exist_ok=True)
        part = it.dest.with_name(it.dest.name + ".part")
        shutil.move(str(src), str(part))
        os.replace(part, it.dest)
        if it.date:
            os.utime(it.dest, (it.date, it.date))
        self.state[str(it.msg_id)]["done"] = True
        write_json_atomic(self.state_path, self.state)
        self.done_files += 1
        self.done_bytes += actual
        self.log(f"OK  {fmt_bytes(actual):>10}  {it.dest.name}")
        return True

    def _check(self, job: Job, now: float) -> str:
        it = job.item
        with self.lock:
            f = self.files.get(it.file_id)
        if not f:
            return "active"
        loc = f.get("local") or {}
        if loc.get("is_downloading_completed"):
            return "done" if self._finalize(job, f) else "retry"
        dl = int(loc.get("downloaded_size", 0))
        if dl > job.last_bytes:
            job.last_bytes, job.last_progress = dl, now
        if now - job.started > 5 and not loc.get("is_downloading_active"):
            self.log(f"! {it.name}: download stopped at {fmt_bytes(dl)}/{fmt_bytes(it.size)}; resuming")
            return "retry"
        if now - job.last_progress > self.stall:
            pct = 100 * dl / it.size if it.size else 0
            self.log(f"! {it.name}: stalled at {pct:.1f}% for {self.stall}s; cancel + resume")
            return "retry"
        return "active"

    def _retry_or_fail(self, job: Job, queue: list, failed: list) -> None:
        self._cancel(job.item.file_id)
        job.attempts += 1
        if job.attempts >= self.max_attempts:
            self.log(f"X  giving up on {job.item.name} after {job.attempts} attempts (rerun the script to try again)")
            failed.append(job)
        else:
            job.next_try = time.monotonic() + min(60, 2**job.attempts)
            queue.append(job)

    def _report(self, active: dict, queue: list, now: float) -> None:
        """Compute aggregate speed/ETA and hand the numbers to on_progress."""
        active_bytes = sum(min(j.last_bytes, j.item.size or j.last_bytes) for j in active.values())
        done = self.done_bytes + active_bytes
        self.samples.append((now, done))
        while len(self.samples) > 2 and now - self.samples[0][0] > 8:
            self.samples.popleft()
        t0, b0 = self.samples[0]
        speed = (done - b0) / (now - t0) if now > t0 else 0.0
        eta = (max(self.total_bytes - done, 0) / speed) if speed > 1 else None
        retrying = sum(1 for j in queue if j.attempts)
        self.on_progress(done, self.total_bytes, speed, eta, self.done_files, len(self.jobs), len(active), retrying)

    def run(self) -> list[Job]:
        """Download everything. Returns the failed jobs. Returns early (state saved) if stop is set."""
        queue, active, failed = list(self.jobs), {}, []
        while (queue or active) and not self.stop.is_set():
            now = time.monotonic()
            while len(active) < self.concurrency:
                job = next((j for j in queue if j.next_try <= now), None)
                if job is None:
                    break
                queue.remove(job)
                if self._start(job):
                    active[job.item.file_id] = job
                else:
                    self._retry_or_fail(job, queue, failed)
            for fid, job in list(active.items()):
                outcome = self._check(job, now)
                if outcome == "done":
                    del active[fid]
                elif outcome == "retry":
                    del active[fid]
                    self._retry_or_fail(job, queue, failed)
            self._report(active, queue, time.monotonic())
            self.stop.wait(0.5)  # was time.sleep(0.5); wakes up early on Stop
        return failed


# --------------------------------------------------------------------------- login
_LOGIN_HINTS = (  # (substrings of TDLib's error text, dialog title, message)
    (("api_id", "api_hash"), "Invalid API credentials",
     "Telegram rejected the API ID / API hash.\nGet them at https://my.telegram.org and check for typos."),
    (("phone_number_invalid",), "Invalid phone number",
     "Telegram rejected the phone number. Use the international format, e.g. +14155550123."),
    (("flood",), "Too many attempts", "Telegram is rate-limiting this number. Wait a while and try again."),
)


def login_error(exc: Exception) -> UserError:
    text = str(exc)
    for needles, title, message in _LOGIN_HINTS:
        if any(n in text.lower() for n in needles):
            return UserError(title, message)
    return UserError("Login failed", f"Telegram refused the login: {text}")


def login(tg: Telegram, cb: Callbacks, stop: threading.Event) -> bool:
    """Non-blocking login: ask for the code / 2FA password via callbacks.

    Returns False if the user cancelled (or stop was set); raises UserError on failure.
    """
    asks = {  # state -> (callback, python-telegram sender, what we are asking for)
        AuthorizationState.WAIT_CODE: (cb.on_need_code, tg.send_code, "login code"),
        AuthorizationState.WAIT_PASSWORD: (cb.on_need_password, tg.send_password, "2FA password"),
    }
    wrong = 0
    try:
        state = tg.login(blocking=False)
        while state != AuthorizationState.READY:
            if stop.is_set():
                return False
            if state not in asks:
                raise UserError(
                    "Login not supported",
                    f"Telegram asked for a login step this app cannot handle ({state.name}).\n"
                    "Is this phone number registered on Telegram?",
                )
            ask, send, what = asks[state]
            value = ask()
            if not value:
                return False
            try:
                send(value)
            except RuntimeError as exc:  # wrong code / password: TDLib stays in the same state
                wrong += 1
                if wrong >= 3:
                    raise UserError("Login failed", f"Too many wrong attempts ({what}). Start again.") from exc
                cb.on_log(f"! {what} was not accepted ({exc}); try again")
            state = tg.login(blocking=False)  # continue the login process
    except RuntimeError as exc:  # TDLib error while sending phone number / parameters
        raise login_error(exc) from exc
    return True


# --------------------------------------------------------------------------- running
def resolve_target(client: Client, me: dict, target: str) -> tuple[int, str, Path]:
    """Turn a chat id / @username / t.me link (blank = Saved Messages) into (chat_id, name, state file)."""
    target = target.strip()
    if not target:
        chat = client.call("createPrivateChat", {"user_id": me["id"], "force": False})
        return chat["id"], "Saved Messages", DATA_DIR / "state.json"
    clean_target = target
    if "t.me/" in clean_target:
        clean_target = clean_target.split("t.me/")[-1].strip("/")
    if clean_target.startswith("@"):
        clean_target = clean_target[1:]
    try:
        chat_id = int(clean_target)
    except ValueError:
        try:
            chat = client.call("searchPublicChat", {"username": clean_target})
        except TdError as e:
            raise UserError("Chat not found", f"Could not find public chat '{clean_target}': {e}") from e
        chat_id = chat["id"]
        chat_name = chat.get("title") or clean_target
    else:
        try:
            chat = client.call("getChat", {"chat_id": chat_id})
        except TdError as e:
            raise UserError("Chat not found", f"Could not access chat ID {clean_target}: {e}") from e
        chat_name = chat.get("title") or str(chat_id)
    return chat_id, chat_name, DATA_DIR / f"state_{chat_id}.json"


def _open_client(s: Settings) -> Telegram:
    if s.lib and not Path(s.lib).is_file():
        raise UserError("libtdjson not found", f"The libtdjson file does not exist:\n{s.lib}")
    kwargs = dict(
        api_id=s.api_id,
        api_hash=s.api_hash,
        phone=s.phone,
        database_encryption_key=s.db_key,
        files_directory=str(TD_DIR),
    )
    if s.lib:
        kwargs["library_path"] = s.lib
    try:
        return Telegram(**kwargs)
    except Exception as exc:  # noqa: BLE001 - ctypes/pkg_resources/etc. all mean "cannot load TDLib"
        raise UserError(
            "Cannot load TDLib",
            f"libtdjson could not be loaded ({exc}).\n\n"
            "Pick a libtdjson file yourself (Browse / --lib / TDLIB_PATH), or run 'Check TDLib' for details.",
        ) from exc


def _run(tg: Telegram, s: Settings, cb: Callbacks, stop: threading.Event) -> int:
    say = cb.on_log
    if not login(tg, cb, stop):
        say("Login cancelled.")
        return 130
    client = Client(tg, say, stop)
    try:
        client.call("setLogVerbosityLevel", {"new_verbosity_level": 1}, retries=1)
    except TdError:
        pass

    me = client.call("getMe")
    say(f"Logged in as {me.get('first_name', '')} (id {me['id']})")

    chat_id, chat_name, state_path = resolve_target(client, me, s.target)
    say(f"Using target chat: {chat_name} (id {chat_id})")

    items = scan_history(client, chat_id, s.photos, cb.on_scan)
    state = load_state(state_path, say)
    todo, skipped, skipped_bytes = plan(items, state, s.out_dir, s.layout, state_path)

    by_kind: dict[str, int] = {}
    for it in items:
        by_kind[it.kind] = by_kind.get(it.kind, 0) + 1
    say("Found: " + ", ".join(f"{k}={v}" for k, v in sorted(by_kind.items())))
    say(f"Already complete: {skipped} files ({fmt_bytes(skipped_bytes)}). To download: {len(todo)} files "
        f"({fmt_bytes(sum(i.size for i in todo))})")
    if s.list_only or not todo:
        return 0

    started = time.monotonic()
    dl = Downloader(client, todo, state, s.concurrency, s.stall_timeout, s.max_attempts, state_path,
                    cb.on_progress, stop)
    failed = dl.run()
    if stop.is_set():
        say("Interrupted. Progress is saved - rerun to resume.")
        return 130
    elapsed = time.monotonic() - started
    say(f"Done: {dl.done_files}/{len(todo)} files, {fmt_bytes(dl.done_bytes)} in {fmt_eta(elapsed)} "
        f"(avg {fmt_bytes(dl.done_bytes / max(elapsed, 1))}/s)")
    if failed:
        say(f"{len(failed)} file(s) failed this run; rerun to retry them:")
        for j in failed:
            say(f"  - msg {j.item.msg_id}: {j.item.name}")
        return 1
    return 0


def run_download(settings: Settings, callbacks: Callbacks, stop_event: threading.Event) -> int:
    """Log in, scan the chat and download everything. Blocking: call it from a worker thread.

    Returns an exit code: 0 ok, 1 some files failed, 130 stopped/cancelled (progress is saved).
    Raises UserError for problems the user can fix (bad credentials, chat not found, no libtdjson).
    """
    TD_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    settings.out_dir.mkdir(parents=True, exist_ok=True)
    tg = _open_client(settings)
    try:
        return _run(tg, settings, callbacks, stop_event)
    except Stopped:
        callbacks.on_log("Interrupted. Progress is saved - rerun to resume.")
        return 130
    finally:
        tg.stop()  # required by python-telegram for a clean TDLib shutdown


# --------------------------------------------------------------------------- CLI
class CliProgress:
    """Terminal status line: live on a TTY, one line every 15 s otherwise."""

    def __init__(self) -> None:
        self.last_print = 0.0

    def __call__(self, done, total, speed, eta, done_files, total_files, active, retrying) -> None:
        pct = 100 * done / total if total else 100
        line = (
            f"[{done_files}/{total_files}] {fmt_bytes(done)}/{fmt_bytes(total)} "
            f"({pct:.1f}%) | {fmt_bytes(speed)}/s | ETA {fmt_eta(eta)} | active {active} | retrying {retrying}"
        )
        if IS_TTY:
            cols = shutil.get_terminal_size((120, 20)).columns
            sys.stdout.write("\r\x1b[2K" + line[: cols - 1])
            sys.stdout.flush()
        elif time.monotonic() - self.last_print >= 15:
            self.last_print = time.monotonic()
            print(line, flush=True)


def cli_scan(messages: int, files: int) -> None:
    msg = f"Scanning history... {messages} messages, {files} files"
    sys.stdout.write(("\r\x1b[2K" if IS_TTY else "") + msg + ("" if IS_TTY else "\n"))
    sys.stdout.flush()


def main() -> int:
    ap = argparse.ArgumentParser(description="Download all files from Telegram via TDLib")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output directory")
    ap.add_argument("--layout", choices=["type", "month", "flat"], default="flat", help="subfolder scheme")
    ap.add_argument("--concurrency", type=int, default=5, help="simultaneous file downloads (default 5)")
    ap.add_argument("--stall-timeout", type=int, default=60, help="seconds without progress before cancel+resume")
    ap.add_argument("--max-attempts", type=int, default=8, help="attempts per file before giving up this run")
    ap.add_argument("--photos", action="store_true", help="also download photos (documents/video/audio always included)")
    ap.add_argument("--list-only", action="store_true", help="scan and report, download nothing")
    ap.add_argument("--lib", help="path to libtdjson.so (default: the one bundled with python-telegram)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING)
    stop = threading.Event()

    def on_sigint(signum, frame) -> None:
        stop.set()
        signal.signal(signal.SIGINT, signal.default_int_handler)  # a 2nd Ctrl+C raises KeyboardInterrupt
        log("Stopping... progress is saved (Ctrl+C again to force quit)")

    signal.signal(signal.SIGINT, on_sigint)

    callbacks = Callbacks(
        on_log=log,
        on_progress=CliProgress(),
        on_scan=cli_scan,
        on_need_code=lambda: input("Enter the login code Telegram sent you: ").strip(),
        on_need_password=lambda: getpass.getpass("Enter your 2FA password: "),
    )
    try:
        cfg = load_config()
        target = input("\nEnter Chat ID, @username, or link to download from (press Enter for Saved Messages): ").strip()
        settings = Settings(
            api_id=cfg["api_id"],
            api_hash=cfg["api_hash"],
            phone=cfg["phone"],
            db_key=cfg["db_key"],
            target=target,
            out_dir=args.out,
            layout=args.layout,
            concurrency=args.concurrency,
            stall_timeout=args.stall_timeout,
            max_attempts=args.max_attempts,
            photos=args.photos,
            list_only=args.list_only,
            lib=args.lib or os.environ.get("TDLIB_PATH"),
        )
        return run_download(settings, callbacks, stop)
    except UserError as exc:
        log(str(exc))
        return 1
    except KeyboardInterrupt:
        log("Interrupted. Progress is saved - rerun the script to resume.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
