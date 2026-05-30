#!/usr/bin/env python3

import ctypes
import os
import subprocess
import sys
import tempfile
import threading
import queue
import shutil
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "requests"])
    import requests

import tkinter as tk
from tkinter import ttk, font as tkfont

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CHROMIUM_DIR = Path(os.environ["LOCALAPPDATA"]) / "Chromium"
APPLICATION_DIR = CHROMIUM_DIR / "Application"
GITHUB_API = "https://api.github.com"
HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}

# ---------------------------------------------------------------------------
# Backend logic
# ---------------------------------------------------------------------------

def log(q: queue.Queue, msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    q.put(f"[{ts}] {msg}\n")


def kill_chromium():
    """Kill any running Chromium/Chrome processes installed in AppData."""
    import time
    try:
        for name in ("chrome.exe", "chromium.exe"):
            subprocess.run(["taskkill", "/F", "/IM", name], capture_output=True)
    except Exception:
        pass
    time.sleep(2)  # give OS time to release file handles


def cleanup_leftover_installer_state(q: queue.Queue):
    """
    Remove files/folders that cause mini_installer to return exit code 4:
      INSTALL_FAILED_PRIOR_INSTALLATION_INCOMPLETE
    This happens when a previous install left behind an 'installer' subfolder
    or other lock artefacts inside the Chromium directory.
    """
    import time
    targets = [
        CHROMIUM_DIR / "Application" / "installer",
        CHROMIUM_DIR / "installer",
    ]
    # Also remove any version-numbered 'installer' subdirs under Application
    if APPLICATION_DIR.exists():
        for child in APPLICATION_DIR.iterdir():
            if child.is_dir():
                ins = child / "installer"
                if ins.exists():
                    targets.append(ins)

    for t in targets:
        if t.exists():
            log(q, f"Removing leftover: {t.relative_to(CHROMIUM_DIR)}")
            shutil.rmtree(str(t), ignore_errors=True)
            time.sleep(0.3)


def backup_application(q: queue.Queue) -> Path | None:
    if APPLICATION_DIR.exists():
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = CHROMIUM_DIR / f"Application-backup-{ts}"
        log(q, f"Backing up Application → {backup.name}")
        try:
            shutil.move(str(APPLICATION_DIR), str(backup))
            return backup
        except Exception as e:
            log(q, f"Warning: backup failed: {e}")
    return None


def restore_backup(backup: Path | None, q: queue.Queue):
    if backup and backup.exists():
        log(q, "Restoring backup...")
        try:
            shutil.move(str(backup), str(APPLICATION_DIR))
            log(q, "Backup restored.")
        except Exception as e:
            log(q, f"Failed to restore backup: {e}")


def remove_backup(backup: Path | None, q: queue.Queue):
    if backup and backup.exists():
        log(q, "Removing backup...")
        shutil.rmtree(str(backup), ignore_errors=True)


def download_file(url: str, dest: Path, q: queue.Queue):
    log(q, f"Downloading from:\n  {url}")
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        downloaded = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = downloaded * 100 // total
                    q.put(f"\rDownload: {pct}%  ({downloaded // (1024*1024)} / {total // (1024*1024)} MB)   ")
    q.put("\n")
    log(q, "Download complete.")


def run_installer(installer: Path, q: queue.Queue) -> int:
    log(q, f"Running installer: {installer.name}")
    try:
        proc = subprocess.run(
            [str(installer)],
            capture_output=True
        )
        return proc.returncode
    except Exception as e:
        log(q, f"Failed to run installer: {e}")
        return -1


# ---------------------------------------------------------------------------
# Per-build download URL resolvers
# ---------------------------------------------------------------------------

def get_url_stable(q: queue.Queue) -> str:
    """Hibbiki/chromium-win64 latest release mini_installer.exe"""
    log(q, "Fetching latest Hibbiki/chromium-win64 release...")
    r = requests.get(f"{GITHUB_API}/repos/Hibbiki/chromium-win64/releases/latest",
                     headers=HEADERS, timeout=15)
    r.raise_for_status()
    data = r.json()
    for asset in data.get("assets", []):
        if asset["name"] == "mini_installer.exe":
            log(q, f"Found: {data['tag_name']} - {asset['name']}")
            return asset["browser_download_url"]
    raise RuntimeError("mini_installer.exe not found in Hibbiki release assets.")


def get_url_dev(q: queue.Queue) -> str:
    """RobRich999/Chromium_Clang - prefer win64-avx2, fall back to win64."""
    log(q, "Fetching RobRich999/Chromium_Clang releases...")
    r = requests.get(f"{GITHUB_API}/repos/RobRich999/Chromium_Clang/releases",
                     headers=HEADERS, timeout=15)
    r.raise_for_status()
    releases = r.json()

    for variant in ("win64-avx2", "win64"):
        for release in releases:
            if variant in (release.get("name") or "").lower():
                for asset in release.get("assets", []):
                    if asset["name"] == "mini_installer.exe":
                        log(q, f"Found: {release['name']} ({variant}) - {asset['name']}")
                        return asset["browser_download_url"]

    raise RuntimeError("mini_installer.exe not found in RobRich999/Chromium_Clang releases.")


def get_url_latest(q: queue.Queue) -> str:
    """Official Chromium snapshot - no codecs/DRM.
    Not every revision has a mini_installer.exe, so we walk back from
    LAST_CHANGE until we find one that does (max 50 steps).
    """
    platform = "Win_x64" if sys.maxsize > 2**32 else "Win"
    base = "https://commondatastorage.googleapis.com/chromium-browser-snapshots"
    log(q, f"Fetching latest official Chromium snapshot ({platform})...")
    r = requests.get(f"{base}/{platform}/LAST_CHANGE", timeout=15)
    r.raise_for_status()
    start_rev = int(r.text.strip())
    log(q, f"Latest revision: {start_rev} - searching for mini_installer.exe...")
    for rev in range(start_rev, start_rev - 50, -1):
        url = f"{base}/{platform}/{rev}/mini_installer.exe"
        head = requests.head(url, timeout=10, allow_redirects=True)
        if head.status_code == 200:
            log(q, f"Found mini_installer.exe at revision {rev}")
            return url
        log(q, f"  r{rev}: not found, trying earlier...")
    raise RuntimeError(f"No mini_installer.exe found in last 50 revisions from {start_rev}.")


URL_RESOLVERS = {
    "stable": get_url_stable,
    "dev":    get_url_dev,
    "latest": get_url_latest,
}


# ---------------------------------------------------------------------------
# Install workflow
# ---------------------------------------------------------------------------

def wipe_version_from_registry(q: queue.Queue):
    """Remove Chromium version from registry so mini_installer allows downgrades."""
    import winreg
    keys = [
        (winreg.HKEY_CURRENT_USER,  r"Software\Chromium"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Chromium"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Chromium"),
    ]
    for hive, subkey in keys:
        for value in ("pv", "version"):
            try:
                with winreg.OpenKey(hive, subkey, 0, winreg.KEY_SET_VALUE) as k:
                    winreg.DeleteValue(k, value)
                    log(q, f"Cleared registry: {subkey}\\{value}")
            except FileNotFoundError:
                pass
            except Exception as e:
                log(q, f"Registry warning: {e}")


def install_build(channel: str, q: queue.Queue, done_event: threading.Event):
    try:
        resolver = URL_RESOLVERS[channel]
        url = resolver(q)

        tmp = Path(tempfile.mktemp(suffix=".exe", prefix=f"chromium_{channel}_"))
        download_file(url, tmp, q)

        log(q, "Stopping existing Chromium processes...")
        kill_chromium()

        cleanup_leftover_installer_state(q)
        wipe_version_from_registry(q)

        backup = backup_application(q)

        code = run_installer(tmp, q)

        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass

        if code == 0:
            log(q, f"✓ Chromium [{channel}] installed successfully.")
            remove_backup(backup, q)
        else:
            log(q, f"✗ Installer exited with code {code}.")
            restore_backup(backup, q)

    except Exception as e:
        log(q, f"Error: {e}")
    finally:
        done_event.set()


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

BG       = "#0e0e12"
SURFACE  = "#16161d"
BORDER   = "#2a2a38"
ACCENT   = "#5e9bff"
ACCENT2  = "#a78bfa"
TEXT     = "#e8e8f0"
MUTED    = "#6b6b80"
SUCCESS  = "#4ade80"
ERROR    = "#f87171"

BUILD_META = {
    "stable": {
        "label": "Stable",
        "sub":   "Hibbiki · win64 · synced with Chrome stable",
        "color": "#4ade80",
    },
    "dev": {
        "label": "Dev",
        "sub":   "RobRich Clang · win64-avx2 · bleeding edge",
        "color": "#60a5fa",
    },
    "latest": {
        "label": "Latest",
        "sub":   "Official snapshot · no codecs / DRM / API keys",
        "color": "#a78bfa",
    },
}


class SwitcherApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Chromium Switcher")
        root.geometry("720x540")
        root.configure(bg=BG)
        root.resizable(True, True)

        self.q: queue.Queue = queue.Queue()
        self._running = False
        self._btn_refs: dict[str, tk.Button] = {}

        self._build_ui()
        self._poll()

    # ------------------------------------------------------------------
    def _build_ui(self):
        root = self.root

        # Title bar area
        header = tk.Frame(root, bg=BG)
        header.pack(fill=tk.X, padx=24, pady=(20, 0))

        title_f = tkfont.Font(family="Segoe UI", size=18, weight="bold")
        sub_f   = tkfont.Font(family="Segoe UI", size=9)
        mono_f  = tkfont.Font(family="Consolas", size=9)

        tk.Label(header, text="⬡  Chromium Switcher", font=title_f,
                 bg=BG, fg=TEXT).pack(side=tk.LEFT)

        self.status_lbl = tk.Label(header, text="idle", font=sub_f,
                                   bg=BG, fg=MUTED)
        self.status_lbl.pack(side=tk.RIGHT, pady=6)

        tk.Frame(root, bg=BORDER, height=1).pack(fill=tk.X, padx=24, pady=(10, 16))

        # Channel cards
        cards_frame = tk.Frame(root, bg=BG)
        cards_frame.pack(fill=tk.X, padx=24)

        for channel, meta in BUILD_META.items():
            self._make_card(cards_frame, channel, meta)

        tk.Frame(root, bg=BORDER, height=1).pack(fill=tk.X, padx=24, pady=(16, 8))

        # Log area
        log_outer = tk.Frame(root, bg=SURFACE, highlightbackground=BORDER,
                             highlightthickness=1)
        log_outer.pack(fill=tk.BOTH, expand=True, padx=24, pady=(0, 16))

        log_header = tk.Frame(log_outer, bg=SURFACE)
        log_header.pack(fill=tk.X, padx=8, pady=(6, 0))
        tk.Label(log_header, text="Output", font=tkfont.Font(family="Segoe UI", size=8, weight="bold"),
                 bg=SURFACE, fg=MUTED).pack(side=tk.LEFT)
        tk.Button(log_header, text="Clear", font=tkfont.Font(family="Segoe UI", size=8),
                  bg=SURFACE, fg=MUTED, activebackground=BORDER, activeforeground=TEXT,
                  relief=tk.FLAT, cursor="hand2", bd=0,
                  command=lambda: self.log.delete("1.0", tk.END)).pack(side=tk.RIGHT)

        self.log = tk.Text(log_outer, bg=SURFACE, fg=TEXT,
                           font=mono_f,
                           insertbackground=TEXT, relief=tk.FLAT,
                           wrap=tk.NONE, bd=0, padx=8, pady=4)
        scroll_y = ttk.Scrollbar(log_outer, orient=tk.VERTICAL, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll_y.set)
        scroll_y.pack(side=tk.RIGHT, fill=tk.Y)
        self.log.pack(fill=tk.BOTH, expand=True)

        # Tag colours
        self.log.tag_config("ok",  foreground=SUCCESS)
        self.log.tag_config("err", foreground=ERROR)

    def _make_card(self, parent: tk.Frame, channel: str, meta: dict):
        color = meta["color"]

        card = tk.Frame(parent, bg=SURFACE, highlightbackground=BORDER,
                        highlightthickness=1)
        card.pack(fill=tk.X, pady=4)

        accent_bar = tk.Frame(card, bg=color, width=4)
        accent_bar.pack(side=tk.LEFT, fill=tk.Y)

        inner = tk.Frame(card, bg=SURFACE)
        inner.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=12, pady=10)

        lbl_f = tkfont.Font(family="Segoe UI", size=11, weight="bold")
        sub_f = tkfont.Font(family="Segoe UI", size=8)

        tk.Label(inner, text=meta["label"], font=lbl_f,
                 bg=SURFACE, fg=color).pack(anchor=tk.W)
        tk.Label(inner, text=meta["sub"], font=sub_f,
                 bg=SURFACE, fg=MUTED).pack(anchor=tk.W)

        btn_f = tkfont.Font(family="Segoe UI", size=9, weight="bold")
        btn = tk.Button(
            card, text="Install",
            font=btn_f,
            bg=color, fg="#0e0e12",
            activebackground=TEXT, activeforeground="#0e0e12",
            relief=tk.FLAT, bd=0, padx=16, pady=6,
            cursor="hand2",
            command=lambda c=channel: self._start(c)
        )
        btn.pack(side=tk.RIGHT, padx=12, pady=10)
        self._btn_refs[channel] = btn

    # ------------------------------------------------------------------
    def _start(self, channel: str):
        if self._running:
            self._append("Another install is already running.\n", "err")
            return

        self._running = True
        self._set_buttons(False)
        self.status_lbl.config(text=f"installing {channel}…", fg=ACCENT)

        done = threading.Event()
        t = threading.Thread(target=install_build, args=(channel, self.q, done), daemon=True)
        t.start()
        self.root.after(200, lambda: self._wait_done(done))

    def _wait_done(self, done: threading.Event):
        if done.is_set():
            self._running = False
            self._set_buttons(True)
            self.status_lbl.config(text="idle", fg=MUTED)
        else:
            self.root.after(200, lambda: self._wait_done(done))

    def _set_buttons(self, enabled: bool):
        for btn in self._btn_refs.values():
            btn.config(state=tk.NORMAL if enabled else tk.DISABLED)

    def _append(self, text: str, tag: str = ""):
        if text.startswith("\r"):
            # overwrite last line
            self.log.delete("end-2l lineend", "end-1c")
            text = text.lstrip("\r")
        self.log.insert(tk.END, text, tag)
        self.log.see(tk.END)

    def _poll(self):
        try:
            while True:
                line = self.q.get_nowait()
                tag = "ok" if "✓" in line else ("err" if "✗" in line or "Error" in line else "")
                self._append(line, tag)
        except queue.Empty:
            pass
        self.root.after(80, self._poll)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if sys.platform != "win32":
        print("This tool is Windows-only (Chromium mini_installer.exe).")
        sys.exit(1)

    root = tk.Tk()

    # Dark title bar on Windows 10/11
    try:
        HWND = ctypes.windll.user32.GetParent(root.winfo_id())
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            HWND, 20, ctypes.byref(ctypes.c_int(1)), ctypes.sizeof(ctypes.c_int)
        )
    except Exception:
        pass

    style = ttk.Style()
    try:
        style.theme_use("clam")
    except Exception:
        pass
    style.configure("Vertical.TScrollbar", background=BORDER, troughcolor=SURFACE,
                    arrowcolor=MUTED, bordercolor=SURFACE)

    app = SwitcherApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()