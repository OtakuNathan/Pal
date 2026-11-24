"""Run two bubblewrap sandboxes in one owned process group with a private channel."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile

CHANNEL_ENV = "PAL_BWRAP_COMPANION_FD"
_WORKER_ENV_PREFIX = "_PAL_BWRAP_WORKER_"


def shadow_file_mounts(files: dict[str, Path], root: Path) -> tuple[dict[Path, Path], dict[str, str]]:
    """Bind each distinct parent once. Only the trusted companion receives these mounts."""
    parents: dict[Path, Path] = {}
    aliases = {}
    for key, path in files.items():
        parent = parents.setdefault(path.parent, root / str(len(parents)))
        aliases[key] = str(parent / path.name)
    return parents, aliases


def with_companion(
    worker: list[str], companion: list[str], env: dict[str, str], mounts: dict[Path, Path],
) -> tuple[list[str], dict[str, str]]:
    # The outer supervisor and companion must not load role-supplied libraries or Python paths.
    launcher = [sys.executable, "-I", "-S", str(Path(__file__).resolve()), str(len(companion)),
                json.dumps({str(src): str(dest) for src, dest in mounts.items()})]
    return [*launcher, *companion, *worker], {_WORKER_ENV_PREFIX + key: value for key, value in env.items()}


def main() -> int:
    count = int(sys.argv[1])
    companion = sys.argv[3:3 + count]
    worker = sys.argv[3 + count:]
    # Command construction precedes worktree acquisition. Revalidate the shared
    # parents under the owner's lock, before either sandbox can start writing.
    mounts = []
    for source, dest in json.loads(sys.argv[2]).items():
        directory = Path(source)
        if directory.resolve(strict=True) != directory or not directory.is_dir():
            raise ValueError(f"shadow mount source is no longer a canonical directory: {source}")
        mounts.extend(["--bind", source, dest])
    command_start = companion.index("--")
    companion[command_start:command_start] = mounts
    environment = {
        key.removeprefix(_WORKER_ENV_PREFIX): value
        for key, value in os.environ.items() if key.startswith(_WORKER_ENV_PREFIX)
    }
    server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    with server, client, tempfile.TemporaryFile() as options:
        environment[CHANNEL_ENV] = str(client.fileno())
        # Keep credentials off argv. Apply the role environment inside its sandbox.
        for key, value in environment.items():
            if not key or "=" in key or "\0" in key or "\0" in value:
                raise ValueError("invalid role environment entry")
            for arg in ("--setenv", key, value):
                options.write(os.fsencode(arg) + b"\0")
        options.seek(0)
        with subprocess.Popen(companion, stdin=server, stdout=subprocess.DEVNULL, env={}) as helper:
            server.close()
            try:
                return subprocess.call(
                    [worker[0], "--args", str(options.fileno()), *worker[1:]],
                    pass_fds=(options.fileno(), client.fileno()), env={},
                )
            finally:
                client.close()
                helper.kill()


if __name__ == "__main__":
    sys.exit(main())
