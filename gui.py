#!/usr/bin/env python3
"""Tkinter front-end for the Telegram bulk downloader.  Run:  python gui.py

Threading model
    * All TDLib / download work runs in one background thread (``App._worker``).
    * The worker talks to the GUI only through ``App.q`` (a queue.Queue); the Tk thread
      drains it every 100 ms in ``App._drain``. Widgets are never touched from the worker.
    * Login prompts: the worker puts a ``Prompt`` on the queue and waits on its Event until
      the dialog on the Tk thread has been answered (or Stop / close cancels it).
"""
from __future__ import annotations

import logging
import queue
import threading
import time
import tkinter as tk
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Callable, Optional

import check_tdlib
import download

LAYOUTS = ("flat", "type", "month")
API_URL = "https://my.telegram.org"
SHUTDOWN_GRACE = 15.0  # seconds to wait for TDLib to shut down when the window is closed
logger = logging.getLogger(__name__)


@dataclass
class Prompt:
    """A request from the worker thread for typed input. The GUI fills in ``value`` (None = cancelled)."""

    title: str
    text: str
    secret: bool = False
    value: Optional[str] = None
    done: threading.Event = field(default_factory=threading.Event)


class PromptDialog(tk.Toplevel):
    """Modal text prompt. Never blocks the event loop; calls ``callback(value or None)`` exactly once."""

    def __init__(self, parent: tk.Misc, prompt: Prompt, callback: Callable[[Optional[str]], None]):
        super().__init__(parent)
        self._callback, self._finished, self._secret = callback, False, prompt.secret
        self.title(prompt.title)
        self.transient(parent)
        self.resizable(False, False)

        frm = ttk.Frame(self, padding=14)
        frm.pack()
        ttk.Label(frm, text=prompt.text, wraplength=330, justify="left").pack(anchor="w")
        self.var = tk.StringVar()
        self.entry = ttk.Entry(frm, textvariable=self.var, show="*" if prompt.secret else "", width=36)
        self.entry.pack(fill="x", pady=10)
        buttons = ttk.Frame(frm)
        buttons.pack(anchor="e")
        ttk.Button(buttons, text="Cancel", command=self.cancel).pack(side="right", padx=(6, 0))
        ttk.Button(buttons, text="OK", command=self._ok).pack(side="right")

        self.bind("<Return>", lambda _e: self._ok())
        self.bind("<Escape>", lambda _e: self.cancel())
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.geometry(f"+{parent.winfo_rootx() + 80}+{parent.winfo_rooty() + 80}")
        self.after(50, self._grab)  # grab_set fails if the window is not mapped yet

    def _grab(self) -> None:
        try:
            self.grab_set()
        except tk.TclError:
            pass
        self.entry.focus_force()

    def _ok(self) -> None:
        value = self.var.get() if self._secret else self.var.get().strip()
        if not value:
            self.bell()
            return
        self._finish(value)

    def cancel(self) -> None:
        self._finish(None)

    def _finish(self, value: Optional[str]) -> None:
        if self._finished:
            return
        self._finished = True
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()
        self._callback(value)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: Optional[threading.Thread] = None
        self.dialog: Optional[PromptDialog] = None
        self.scan_only = False
        self.closing = False
        self.close_deadline = 0.0
        self._build_ui()
        self._load_saved()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._drain)

    # ------------------------------------------------------------------ UI construction
    @staticmethod
    def _entry_row(parent, row: int, label: str, var: tk.StringVar, show: str = "", button=None) -> ttk.Entry:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=2)
        entry = ttk.Entry(parent, textvariable=var, show=show)
        entry.grid(row=row, column=1, sticky="ew", pady=2)
        if button:
            ttk.Button(parent, text=button[0], command=button[1]).grid(row=row, column=2, padx=(6, 0), pady=2)
        return entry

    @staticmethod
    def _spin(parent, label: str, var: tk.StringVar, lo: int, hi: int) -> None:
        ttk.Label(parent, text=label).pack(side="left")
        ttk.Spinbox(parent, from_=lo, to=hi, textvariable=var, width=5).pack(side="left", padx=(4, 14))

    def _build_ui(self) -> None:
        root = self.root
        root.title("Telegram Bulk Downloader")
        root.minsize(660, 600)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(4, weight=1)  # the log grows with the window
        pad = {"padx": 10, "pady": (8, 0)}

        # --- Account
        acc = ttk.LabelFrame(root, text="Account", padding=8)
        acc.grid(row=0, column=0, sticky="ew", **pad)
        acc.columnconfigure(1, weight=1)
        self.api_id, self.api_hash, self.phone, self.lib = (tk.StringVar() for _ in range(4))
        self.save = tk.BooleanVar(value=True)
        self._entry_row(acc, 0, "API ID", self.api_id)
        self._entry_row(acc, 1, "API hash", self.api_hash, show="*")
        self._entry_row(acc, 2, "Phone", self.phone)
        self._entry_row(acc, 3, "libtdjson (optional)", self.lib, button=("Browse…", self._browse_lib))
        ttk.Checkbutton(acc, text="Save these in config.json (plain text, owner-only permissions)",
                        variable=self.save).grid(row=4, column=0, columnspan=3, sticky="w", pady=(4, 0))
        hint = ttk.Frame(acc)
        hint.grid(row=5, column=0, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Label(hint, text="Get your API ID and hash at ", foreground="gray").pack(side="left")
        link = ttk.Label(hint, text=API_URL, foreground="#0b57d0", cursor="hand2")
        link.pack(side="left")
        link.bind("<Button-1>", lambda _e: webbrowser.open(API_URL))
        ttk.Label(hint, text="  (phone in international format, e.g. +14155550123)",
                  foreground="gray").pack(side="left")

        # --- Job
        job = ttk.LabelFrame(root, text="Job", padding=8)
        job.grid(row=1, column=0, sticky="ew", **pad)
        job.columnconfigure(1, weight=1)
        self.target = tk.StringVar()
        self.out_dir = tk.StringVar(value=str(download.DEFAULT_OUT))
        self.layout = tk.StringVar(value="flat")
        self.concurrency, self.stall, self.attempts = tk.StringVar(value="5"), tk.StringVar(value="60"), tk.StringVar(value="8")
        self.photos = tk.BooleanVar(value=False)
        self._entry_row(job, 0, "Target", self.target)
        ttk.Label(job, text="Chat ID, @username or t.me link. Leave blank for Saved Messages.",
                  foreground="gray").grid(row=1, column=1, columnspan=2, sticky="w")
        self._entry_row(job, 2, "Output folder", self.out_dir, button=("Browse…", self._browse_out))
        opts = ttk.Frame(job)
        opts.grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(opts, text="Layout").pack(side="left")
        ttk.Combobox(opts, textvariable=self.layout, values=LAYOUTS, state="readonly", width=7).pack(side="left", padx=(4, 14))
        self._spin(opts, "Concurrency", self.concurrency, 1, 20)
        self._spin(opts, "Stall timeout (s)", self.stall, 5, 3600)
        self._spin(opts, "Max attempts", self.attempts, 1, 100)
        ttk.Checkbutton(job, text="Include photos", variable=self.photos).grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))

        # --- Buttons
        bar = ttk.Frame(root)
        bar.grid(row=2, column=0, sticky="ew", **pad)
        self.scan_btn = ttk.Button(bar, text="Scan only", command=lambda: self._start(scan_only=True))
        self.start_btn = ttk.Button(bar, text="Start", command=lambda: self._start(scan_only=False))
        self.stop_btn = ttk.Button(bar, text="Stop", command=self._stop, state="disabled")
        self.check_btn = ttk.Button(bar, text="Check TDLib", command=self._check_tdlib)
        for btn in (self.scan_btn, self.start_btn, self.stop_btn):
            btn.pack(side="left", padx=(0, 6))
        self.check_btn.pack(side="right")

        # --- Progress
        prog = ttk.LabelFrame(root, text="Progress", padding=8)
        prog.grid(row=3, column=0, sticky="ew", **pad)
        prog.columnconfigure(tuple(range(5)), weight=1)
        self.status, self.pct = tk.StringVar(value="Idle"), tk.StringVar(value="0.0%")
        self.speed, self.eta, self.files = tk.StringVar(value="-"), tk.StringVar(value="--:--:--"), tk.StringVar(value="0 / 0")
        self.active, self.retrying = tk.StringVar(value="0"), tk.StringVar(value="0")
        ttk.Label(prog, textvariable=self.status).grid(row=0, column=0, columnspan=5, sticky="w")
        self.bar = ttk.Progressbar(prog, maximum=100, mode="determinate")
        self.bar.grid(row=1, column=0, columnspan=5, sticky="ew", pady=4)
        ttk.Label(prog, textvariable=self.pct).grid(row=2, column=0, columnspan=5, sticky="w")
        cells = (("Speed", self.speed), ("ETA", self.eta), ("Files", self.files), ("Active", self.active), ("Retrying", self.retrying))
        for col, (caption, var) in enumerate(cells):
            ttk.Label(prog, text=caption, foreground="gray").grid(row=3, column=col, sticky="w", pady=(6, 0))
            ttk.Label(prog, textvariable=var).grid(row=4, column=col, sticky="w")

        # --- Log
        box = ttk.LabelFrame(root, text="Log", padding=8)
        box.grid(row=4, column=0, sticky="nsew", padx=10, pady=(8, 10))
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        self.log = scrolledtext.ScrolledText(box, height=8, wrap="word", state="disabled", font="TkFixedFont")
        self.log.grid(row=0, column=0, sticky="nsew")
        ttk.Button(box, text="Clear", command=self._clear_log).grid(row=1, column=0, sticky="e", pady=(6, 0))

    # ------------------------------------------------------------------ small helpers
    def _load_saved(self) -> None:
        """Prefill the account fields from config.json (if present)."""
        self._append_log(f"Config and TDLib session are stored in: {download.DATA_DIR}")
        try:
            stored = download.read_config()
        except download.UserError as exc:
            self._append_log(f"! {exc}")
            return
        self.api_id.set(str(stored.get("api_id", "")))
        self.api_hash.set(stored.get("api_hash", ""))
        self.phone.set(stored.get("phone", ""))
        self.lib.set(stored.get("lib", ""))

    def _append_log(self, text: str) -> None:
        at_bottom = self.log.yview()[1] >= 0.999
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.configure(state="disabled")
        if at_bottom:
            self.log.see("end")

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _browse_lib(self) -> None:
        path = filedialog.askopenfilename(
            title="Select libtdjson",
            filetypes=[("TDLib library", ("*.so", "*.so.*", "*.dylib", "*.dll")), ("All files", "*")],
        )
        if path:
            self.lib.set(path)

    def _browse_out(self) -> None:
        path = filedialog.askdirectory(title="Output folder", initialdir=self.out_dir.get() or None)
        if path:
            self.out_dir.set(path)

    def _set_buttons(self, running: bool, checking: bool = False) -> None:
        state = "disabled" if (running or checking) else "normal"
        for btn in (self.scan_btn, self.start_btn, self.check_btn):
            btn.configure(state=state)
        self.stop_btn.configure(state="normal" if running else "disabled")

    def _reset_progress(self) -> None:
        self.bar["value"] = 0
        self.pct.set("0.0%")
        self.speed.set("-")
        self.eta.set("--:--:--")
        self.files.set("0 / 0")
        self.active.set("0")
        self.retrying.set("0")

    # ------------------------------------------------------------------ reading the form
    @staticmethod
    def _int(var: tk.StringVar, label: str, lo: int, hi: int) -> int:
        try:
            n = int(var.get().strip())
        except ValueError:
            n = lo - 1
        if not lo <= n <= hi:
            raise download.UserError("Check your settings", f"{label} must be a whole number between {lo} and {hi}.")
        return n

    def _collect(self, list_only: bool) -> download.Settings:
        """Validate the form and build Settings (db_key is filled in by _start). Raises UserError."""
        api_id = download.parse_api_id(self.api_id.get())
        api_hash = self.api_hash.get().strip()
        if not api_hash:
            raise download.UserError("Missing API hash", f"Enter your API hash. You can get it at {API_URL}.")
        phone = download.normalize_phone(self.phone.get().strip())
        if not phone.lstrip("+").isdigit():
            raise download.UserError("Invalid phone number",
                                     "Enter your phone number in international format, e.g. +14155550123.")
        out = self.out_dir.get().strip()
        if not out:
            raise download.UserError("Missing output folder", "Choose a folder for the downloaded files.")
        return download.Settings(
            api_id=api_id,
            api_hash=api_hash,
            phone=phone,
            db_key="",
            target=self.target.get().strip(),
            out_dir=Path(out).expanduser(),
            layout=self.layout.get() if self.layout.get() in LAYOUTS else "flat",
            concurrency=self._int(self.concurrency, "Concurrency", 1, 20),
            stall_timeout=self._int(self.stall, "Stall timeout", 5, 3600),
            max_attempts=self._int(self.attempts, "Max attempts", 1, 100),
            photos=self.photos.get(),
            list_only=list_only,
            lib=self.lib.get().strip() or None,
        )

    def _remember(self, stored: dict) -> bool:
        """Copy the account fields into ``stored`` (same format as the CLI). True if anything changed."""
        fields = {
            "api_id": self.api_id.get().strip(),
            "api_hash": self.api_hash.get().strip(),
            "phone": self.phone.get().strip(),
        }
        lib = self.lib.get().strip()
        changed = False
        for key, value in fields.items():
            if stored.get(key) != value:
                stored[key], changed = value, True
        if lib and stored.get("lib") != lib:
            stored["lib"], changed = lib, True
        elif not lib and "lib" in stored:
            del stored["lib"]
            changed = True
        return changed

    # ------------------------------------------------------------------ run / stop
    def _start(self, scan_only: bool) -> None:
        try:
            settings = self._collect(scan_only)
            stored = download.read_config()
            changed = download.ensure_db_key(stored)  # the TDLib session is encrypted with this key
            if self.save.get():
                changed = self._remember(stored) or changed
            if changed:
                download.save_config(stored)
            settings.db_key = stored["db_key"]
        except download.UserError as exc:
            messagebox.showerror(exc.title, str(exc))
            return
        except OSError as exc:
            messagebox.showerror("Cannot save settings", f"Could not write {download.CONFIG_PATH}:\n{exc}")
            return

        self.scan_only = scan_only
        self.stop_event = threading.Event()
        self._reset_progress()
        self._set_buttons(running=True)
        self.status.set("Starting…")
        self.worker = threading.Thread(target=self._worker, args=(settings, self.stop_event), daemon=True)
        self.worker.start()

    def _stop(self) -> None:
        self.stop_event.set()
        self._cancel_prompt()
        self.stop_btn.configure(state="disabled")
        self.status.set("Stopping…")
        self._append_log("Stopping… progress is saved.")

    def _worker(self, settings: download.Settings, stop: threading.Event) -> None:
        """Background thread: runs the whole job; talks to the GUI only via the queue."""
        put = self.q.put
        callbacks = download.Callbacks(
            on_log=lambda msg: put(("log", msg)),
            on_progress=lambda *args: put(("progress", args)),
            on_scan=lambda messages, files: put(("scan", messages, files)),
            on_need_code=lambda: self._ask(
                stop, "Telegram login code",
                "Telegram sent a login code to your Telegram app (or by SMS).\nEnter it below.", False),
            on_need_password=lambda: self._ask(
                stop, "Two-step verification", "This account has a 2FA password.\nEnter it below.", True),
        )
        rc, error = 1, None
        try:
            rc = download.run_download(settings, callbacks, stop)
        except download.UserError as exc:
            error = (exc.title, str(exc))
        except Exception as exc:  # noqa: BLE001 - never show a traceback to the user
            logger.exception("Unexpected error in worker thread")
            error = ("Something went wrong", f"An unexpected error occurred ({type(exc).__name__}: {exc}).")
        put(("done", rc, error))

    def _ask(self, stop: threading.Event, title: str, text: str, secret: bool) -> Optional[str]:
        """Worker thread: ask the Tk thread for input and wait for the answer (None = cancelled)."""
        prompt = Prompt(title, text, secret)
        self.q.put(("prompt", prompt))
        while not prompt.done.wait(0.2):
            if stop.is_set():
                return None
        return prompt.value

    # ------------------------------------------------------------------ Tk thread: queue handling
    def _drain(self) -> None:
        self.root.after(100, self._drain)  # re-arm first, so a modal dialog can't stop the loop
        for _ in range(500):  # bounded, so a flood of log lines can't freeze the window
            try:
                msg = self.q.get_nowait()
            except queue.Empty:
                break
            self._handle(msg)

    def _handle(self, msg: tuple) -> None:
        kind = msg[0]
        if kind == "log":
            self._append_log(msg[1])
        elif kind == "progress":
            self._update_progress(*msg[1])
        elif kind == "scan":
            if not self.stop_event.is_set():
                self.status.set(f"Scanning history… {msg[1]} messages, {msg[2]} files")
        elif kind == "prompt":
            self._show_prompt(msg[1])
        elif kind == "checked":
            self._set_buttons(running=False)
        elif kind == "done":
            self._on_done(msg[1], msg[2])

    def _update_progress(self, done: int, total: int, speed: float, eta: Optional[float],
                         done_files: int, total_files: int, active: int, retrying: int) -> None:
        pct = 100 * done / total if total else 100.0
        self.bar["value"] = pct
        self.pct.set(f"{pct:.1f}%   ({download.fmt_bytes(done)} / {download.fmt_bytes(total)})")
        self.speed.set(f"{download.fmt_bytes(speed)}/s")
        self.eta.set(download.fmt_eta(eta))
        self.files.set(f"{done_files} / {total_files}")
        self.active.set(str(active))
        self.retrying.set(str(retrying))
        if not self.stop_event.is_set():
            self.status.set("Downloading…")

    def _show_prompt(self, prompt: Prompt) -> None:
        if self.stop_event.is_set() or self.closing:
            prompt.done.set()  # value stays None -> the worker treats it as cancelled
            return

        def answered(value: Optional[str]) -> None:
            prompt.value = value
            prompt.done.set()
            self.dialog = None

        self.dialog = PromptDialog(self.root, prompt, answered)

    def _cancel_prompt(self) -> None:
        if self.dialog is not None:
            self.dialog.cancel()

    def _on_done(self, rc: int, error: Optional[tuple]) -> None:
        self._cancel_prompt()
        self.worker = None
        self._set_buttons(running=False)
        if self.closing:
            return
        if error:
            self.status.set("Error")
            self._append_log(f"Error: {error[1]}")
            messagebox.showerror(error[0], error[1])
        elif rc == 130:
            self.status.set("Stopped (progress saved - press Start to resume)")
        elif rc == 0:
            self.status.set("Scan complete" if self.scan_only else "Done")
        else:
            self.status.set("Finished with errors (see log)")

    # ------------------------------------------------------------------ Check TDLib
    def _check_tdlib(self) -> None:
        lib = self.lib.get().strip() or None
        self._set_buttons(running=False, checking=True)
        self._append_log("--- Check TDLib ---")

        def work() -> None:
            try:
                check_tdlib.run_checks(lib, lambda line: self.q.put(("log", line)))
            except Exception as exc:  # noqa: BLE001
                logger.exception("TDLib check crashed")
                self.q.put(("log", f"Check failed: {type(exc).__name__}: {exc}"))
            self.q.put(("checked",))

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------ closing the window
    def _on_close(self) -> None:
        if self.worker is None or not self.worker.is_alive():
            self.root.destroy()
            return
        if not messagebox.askyesno(
            "Quit",
            "A job is still running.\n\nStop it and quit? Progress is saved, so you can resume next time.",
        ):
            return
        # The worker owns the TDLib client: signalling it makes run_download() leave its loop and
        # call tg.stop() in its `finally`. We wait for that (without blocking Tk), then exit.
        self.closing = True
        self.stop_event.set()
        self._cancel_prompt()
        self.status.set("Shutting down TDLib…")
        self.stop_btn.configure(state="disabled")
        self.close_deadline = time.monotonic() + SHUTDOWN_GRACE
        self._wait_for_worker()

    def _wait_for_worker(self) -> None:
        worker = self.worker  # _on_done() sets this to None once the job (and tg.stop()) has finished
        if worker is not None and worker.is_alive() and time.monotonic() < self.close_deadline:
            self.root.after(100, self._wait_for_worker)
        else:
            self.root.destroy()


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
