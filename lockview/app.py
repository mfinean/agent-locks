"""Entry point: a native window (GTK + WebKit, from the system's python3-gi) over the local server.

    lockview              open the window, or raise it if it is already open
    lockview --browser    open it in a browser app window instead
    lockview --serve      only serve; print the URL (port 0: any free one)
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

from . import server

# The .desktop file and the icon are named after the app id, so the dock matches the window to them.
APP_ID = "io.github.mfinean.lockview"
WINDOW_TITLE = "Locks & Leases"
SHARE = Path(__file__).resolve().parent.parent / "share"  # the icon, in a checkout or an install


def _window(port: int, interval: float) -> bool:
    try:
        import gi

        gi.require_version("Gdk", "3.0")
        gi.require_version("Gtk", "3.0")
        gi.require_version("WebKit2", "4.1")
        from gi.repository import GLib

        # Before GTK starts: it takes the window's WM class ("lockview", "Lockview") from the program name.
        GLib.set_prgname("lockview")
        GLib.set_application_name(WINDOW_TITLE)
        from gi.repository import Gdk, Gtk, WebKit2
    except (ImportError, ValueError):
        return False
    if Gdk.Display.get_default() is None:
        return False

    try:
        Gtk.Window.set_default_icon_from_file(str(SHARE / f"{APP_ID}.svg"))
    except GLib.Error:
        Gtk.Window.set_default_icon_name("utilities-system-monitor")

    def activate(app: Gtk.Application) -> None:
        # Only the first instance serves. A second launch hands its activation to it over D-Bus and exits.
        win = app.get_active_window()
        if win is None:
            httpd, _ = server.start(port, interval)
            # A data manager of its own keeps the page's settings (theme, range, project colours) across runs.
            data = server.STATE_DIR / "webkit"
            manager = WebKit2.WebsiteDataManager(base_data_directory=str(data / "data"), base_cache_directory=str(data / "cache"))
            view = WebKit2.WebView.new_with_context(WebKit2.WebContext.new_with_website_data_manager(manager))
            win = Gtk.ApplicationWindow(application=app, title=WINDOW_TITLE)
            win.set_default_size(1440, 920)
            win.add(view)
            view.load_uri(f"http://127.0.0.1:{httpd.server_address[1]}/")
            win.show_all()
        win.present()

    app = Gtk.Application(application_id=APP_ID)
    app.connect("activate", activate)
    app.run(sys.argv[:1])
    return True


def _browser(url: str) -> None:
    chrome = next((c for c in ("google-chrome", "chromium", "chromium-browser") if shutil.which(c)), None)
    if chrome:
        subprocess.Popen([chrome, f"--app={url}", "--new-window"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        webbrowser.open(url)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="lockview", description="Watch the gate's queue, agents' flock locks and other machines' jobs.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--browser", action="store_true", help="open in a browser app window, not a native one")
    mode.add_argument("--serve", action="store_true", help="only run the server and print its URL")
    ap.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default: any free one)")
    ap.add_argument("--interval", type=float, default=3.0, help="seconds between readings (default 3)")
    args = ap.parse_args(argv)

    if not (args.browser or args.serve):
        if _window(args.port, args.interval):
            return 0  # the window closed, or it was already open and has been raised
        print("lockview: no GTK/WebKit2 4.1 for a native window (python3-gi, gir1.2-webkit2-4.1); using a browser", file=sys.stderr)
        args.browser = True

    httpd, _ = server.start(args.port, args.interval)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    if args.browser:
        _browser(url)
    print(url, flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
