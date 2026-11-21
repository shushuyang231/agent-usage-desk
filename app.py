"""Windows entry point and loopback dashboard."""

from __future__ import annotations

import argparse
import ctypes
import os
import re
import secrets
import subprocess
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from engine import UsageIndex, json_data

APP = "Agent Usage Desk"
UI = Path(__file__).resolve().parent / "ui"
HOST = UI.parent / "host" / "DeskWindow.exe"
THEME_ART = {
    "starlight": "local-art/starlight.png",
    "flowers": "local-art/flowers.png",
}
ASSETS = {
    "index.html": "text/html; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
    "themes.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "icon.png": "image/png",
    **dict.fromkeys(THEME_ART.values(), "image/png"),
}


def message(text):
    if sys.stderr:
        print(text, file=sys.stderr)
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        ctypes.windll.user32.MessageBoxW(None, text, APP, 0x10)


def focus_dashboard():
    if sys.platform != "win32":
        return False
    user32 = ctypes.windll.user32
    user32.FindWindowW.restype = ctypes.c_void_p
    window = user32.FindWindowW(None, APP)
    if not window:
        return False
    handle = ctypes.c_void_p(window)
    if user32.IsIconic(handle):
        user32.ShowWindow(handle, 9)
    if not user32.SetForegroundWindow(handle):
        user32.FlashWindow(handle, True)
    return True


def open_dashboard(url, state):
    if not HOST.is_file():
        raise FileNotFoundError("缺少 Windows 窗口组件。请使用打包版或先运行 build-windows.ps1。")
    subprocess.run([str(HOST), url, str(state / "WebView2")], check=True)


@contextmanager
def one_instance(directory):
    lock = (directory / "window.lock").open("a+b")
    locked = False
    try:
        if sys.platform == "win32":
            import msvcrt
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                locked = True
            except OSError:
                yield False
                return
        yield True
    finally:
        if locked:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        lock.close()


def load_theme(preference, themes):
    try:
        theme = preference.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return "classic"
    return theme if theme in themes else "classic"


def handler_for(index, prefix, preference):
    themes = ("classic", *(name for name, image in THEME_ART.items() if (UI / image).is_file()))
    theme_route = prefix + "api/theme/"
    preference_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            origin = self.origin()
            if origin is None:
                self.send_error(404)
                return
            route = urlsplit(self.path).path
            if not route.startswith(prefix):
                self.send_error(404)
                return
            target = route[len(prefix):] or "index.html"
            if target in ("api/status", "api/data"):
                snapshot, status = index.data()
                result = {"progress": status, "scannedAt": (snapshot or {}).get("scannedAt")}
                if target == "api/data":
                    result["snapshot"] = snapshot
                self.reply(json_data(result).encode(), "application/json; charset=utf-8")
            elif target in ASSETS:
                file = UI / target
                if file.is_file():
                    content = file.read_bytes()
                    if target == "index.html":
                        theme = load_theme(preference, themes)
                        content = content.replace(
                            b'data-theme="classic"', f'data-theme="{theme}"'.encode("ascii"), 1,
                        ).replace(
                            b'data-themes="classic"', f'data-themes="{" ".join(themes)}"'.encode("ascii"), 1,
                        )
                    self.reply(content, ASSETS[target])
                else:
                    self.send_error(404)
            else:
                self.send_error(404)

        def do_POST(self):
            origin = self.origin()
            if origin is None or self.headers.get("Origin") != origin:
                self.send_error(403)
                return
            if self.path == prefix + "api/refresh":
                index.start()
            elif self.path == prefix + "api/quit":
                if index.data()[1]["running"]:
                    self.send_error(409)
                    return
                self.reply(b'{"ok":true}', "application/json")
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            elif self.path.startswith(theme_route):
                theme = self.path[len(theme_route):]
                if theme not in themes:
                    self.send_error(404)
                    return
                try:
                    with preference_lock:
                        pending = preference.with_suffix(".tmp")
                        pending.write_text(theme + "\n", encoding="ascii")
                        pending.replace(preference)
                except OSError:
                    self.send_error(500)
                    return
            else:
                self.send_error(404)
                return
            self.reply(b'{"ok":true}', "application/json")

        def origin(self):
            expected = f"127.0.0.1:{self.server.server_port}"
            return f"http://{expected}" if self.headers.get("Host") == expected else None

        def reply(self, result, content_type):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(result)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
            )
            self.end_headers()
            try:
                self.wfile.write(result)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    return Handler


def main():
    parser = argparse.ArgumentParser(description=APP)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-scan", action="store_true")
    parser.add_argument("--state", type=Path)
    parser.add_argument("--codex-root", type=Path)
    parser.add_argument("--gemini-root", type=Path)
    args = parser.parse_args()
    home = Path.home()
    state = args.state or Path(os.getenv("LOCALAPPDATA", home)) / "AgentUsageDesk"
    state.mkdir(parents=True, exist_ok=True)
    active_file = state / "active-url.txt"
    codex = args.codex_root or Path(os.getenv("CODEX_HOME") or home / ".codex")
    gemini = args.gemini_root or Path(os.getenv("GEMINI_CLI_HOME") or home / ".gemini")
    with one_instance(state) as owner:
        if not owner:
            if not args.no_browser and not focus_dashboard() and active_file.exists():
                url = active_file.read_text(encoding="utf-8").strip()
                if re.fullmatch(r"http://127\.0\.0\.1:\d{1,5}/[A-Za-z0-9_-]+/", url):
                    open_dashboard(url, state)
            return
        index = UsageIndex(state, codex.resolve(), gemini.resolve())
        prefix = "/" + secrets.token_urlsafe(24) + "/"
        preference = state / "appearance.txt"
        with ThreadingHTTPServer(("127.0.0.1", 0), handler_for(index, prefix, preference)) as server:
            url = f"http://127.0.0.1:{server.server_port}{prefix}"
            active_file.write_text(url, encoding="utf-8")
            try:
                if not args.no_scan:
                    index.start()
                if args.no_browser:
                    if sys.stdout:
                        print(url, flush=True)
                    server.serve_forever()
                else:
                    service = threading.Thread(target=server.serve_forever, daemon=True)
                    service.start()
                    try:
                        open_dashboard(url, state)
                    finally:
                        server.shutdown()
                        service.join()
            finally:
                active_file.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        message(f"无法启动本地看板。\n\n{error}")
        sys.exit(1)
