from __future__ import annotations

import argparse
import getpass
import os
import socket
import threading
import time
import urllib.request
import webbrowser

import uvicorn

from . import __version__
from .config import data_path


def main():
    parser = argparse.ArgumentParser(
        description="Track app visibility. Opens the web UI on a free localhost port."
    )
    parser.add_argument("--version", action="version", version=f"apptrail {__version__}")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--port", type=int, default=0, help="Port to listen on; 0 selects a free port (default)."
    )
    parser.add_argument(
        "--data-dir",
        help="Persistent data directory; defaults to your user application data folder.",
    )
    parser.add_argument(
        "--no-browser", action="store_true", help="Print the URL without opening a browser."
    )
    parser.add_argument(
        "--reset-password",
        action="store_true",
        help="Reset the owner's password from the server terminal (stop AppTrail first).",
    )
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535.")
    from .api import create_app

    if args.reset_password:
        from fastapi import HTTPException
        from filelock import FileLock, Timeout

        from .auth import Auth, valid_new_password
        from .config import Config
        from .db import Database

        config = Config(args.data_dir)
        try:
            with FileLock(config.directory / "instance.lock", timeout=0):
                db = Database(config.directory)
                try:
                    auth = Auth(db, config)
                    if not auth.has_owner():
                        parser.exit(1, "No account exists yet. Create it in the web interface.\n")
                    password = getpass.getpass(
                        "New password (8 to 128 characters, including a number and a special character): "
                    )
                    valid_new_password(password)
                    if password != getpass.getpass("Confirm password: "):
                        parser.exit(1, "Passwords do not match.\n")
                    username = auth.reset_password(password)
                    print(f"Password reset for {username}. All sessions have been signed out.")
                finally:
                    db.close()
        except Timeout:
            parser.exit(1, "Stop AppTrail before resetting the password.\n")
        except HTTPException as exc:
            parser.exit(1, str(exc.detail) + "\n")
        return

    app = create_app(args.data_dir)
    family = socket.AF_INET6 if ":" in args.host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    if os.name == "posix":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((args.host, args.port))
    except OSError as exc:
        parser.exit(1, f"Could not listen on {args.host}:{args.port}: {exc}\n")
    sock.listen(128)
    display_host = "127.0.0.1" if args.host == "0.0.0.0" else args.host
    if ":" in display_host:
        display_host = f"[{display_host}]"
    url = f"http://{display_host}:{sock.getsockname()[1]}"
    print(
        f"\nAppTrail {__version__}\nWeb UI: {url}\nData:   {data_path(args.data_dir)}\n\nKeep this process running for scheduled checks. Press Ctrl+C to stop.\n",
        flush=True,
    )
    if token := app.state.auth.setup_token():
        print(
            f"Create your owner account in the web UI.\nOne-time setup code: {token}\n", flush=True
        )

    def open_browser():
        for _ in range(100):
            try:
                with urllib.request.urlopen(url + "/healthz", timeout=1):
                    webbrowser.open(url)
                    return
            except OSError:
                time.sleep(0.1)

    if not args.no_browser:
        threading.Thread(target=open_browser, daemon=True).start()
    try:
        server = uvicorn.Server(
            uvicorn.Config(app, access_log=False, log_level="info", timeout_graceful_shutdown=10)
        )
        server.run(sockets=[sock])
    finally:
        sock.close()


if __name__ == "__main__":
    main()
