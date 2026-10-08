"""Keep a worker's process-group identity alive until its owner terminates it.

Executed as a standalone script: the worker environment need not import Pal.
The private socket reports the worker exit status independently of inherited
stdout/stderr, and its EOF terminates the group if the Manager disappears.
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading


def main() -> None:
    control = socket.socket(fileno=int(sys.argv[1]))
    # Never let the worker or its descendants keep the owner's lifeline open.
    control.set_inheritable(False)

    def watch_owner() -> None:
        try:
            while control.recv(1):
                pass
        finally:
            os.killpg(os.getpgrp(), signal.SIGKILL)

    threading.Thread(target=watch_owner, daemon=True).start()
    try:
        try:
            worker = subprocess.Popen(sys.argv[2:], close_fds=True)
            returncode = worker.wait()
        except Exception as exc:
            print(f"bunshin worker spawn failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            returncode = 125
        control.sendall(f"{returncode}\n".encode("ascii"))
        # Remain the live group leader until the owner kills this group. This
        # prevents PID/PGID reuse between worker exit and descendant cleanup.
        threading.Event().wait()
    except BaseException as exc:
        print(f"bunshin supervisor failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        raise
    finally:
        # A failed status write must not let the main thread exit before the
        # lifeline watcher has terminated descendants. We still own this ID.
        os.killpg(os.getpgrp(), signal.SIGKILL)


if __name__ == "__main__":
    main()
