"""Opt-in real-bwrap smoke check, using only disposable runtime data.

Run from the checkout with its installed Python environment. This never starts
Pal, binds a socket, loads configuration/credentials, or calls a model/provider.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from pal.bunshin.sandbox import build_sandboxed_runner_invocation
from pal.shared import BunshinInvocationPack


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="pal_git_mount_smoke_") as temporary:
        root = Path(temporary)
        runtime = root / "runtime"
        workspace = root / "workspace"
        workspace.mkdir()
        endpoint = runtime / "data" / "bunshin-role" / "role.sock"
        endpoint.parent.mkdir(parents=True)
        # A placeholder satisfies mount construction only. No listener or token
        # is created: gateway probes must fail closed before any connection.
        endpoint.write_text("disposable mount smoke placeholder\n")
        pack = BunshinInvocationPack(
            invocation_id="git-mount-smoke",
            goal="Verify sandbox mount startup without a model",
            workspace={"repo_path": str(workspace), "workspace_policy": {"mode": "read_only_repo"}},
            metadata={"sandbox": {
                "enabled": True, "backend": "bwrap", "run_id": "git-mount-smoke",
                "scratch_dir": str(root / "scratch"),
            }},
        )
        code = r'''
import errno, json, os, pathlib, subprocess, sys
import msgpack
import pal.foundation.sidecar
host_net = sys.argv[1]
assert os.readlink('/proc/self/ns/net') != host_net, 'network namespace was not isolated'
assert 'API_KEY' not in os.environ, 'secret-shaped variable was not scrubbed'
cores = [pathlib.Path(p) for p in ('/usr/lib/git-core', '/usr/libexec/git-core') if pathlib.Path(p).is_dir()]
assert cores, 'no host Git exec directory found'
gateways = [pathlib.Path('/usr/bin/git')] + [p / 'git' for p in cores if (p / 'git').exists()]
for gateway in gateways:
    result = subprocess.run([str(gateway), 'status', '--short'], capture_output=True, text=True)
    assert result.returncode == 126 and 'assignment token is missing' in result.stderr, (str(gateway), result.returncode, result.stderr)
helpers = []
for core in cores:
    assert os.statvfs(core).f_flag & os.ST_RDONLY, ('writable Git exec directory', str(core))
    for name in ('git-add', 'git-commit', 'git-status', 'git-remote-http'):
        helper = core / name
        if not helper.exists():
            continue
        for argv in ([], ['status', '--short'], ['add', '.']):
            result = subprocess.run([str(helper), *argv], capture_output=True, text=True)
            assert result.returncode == 126 and 'blocked internal Git entry point' in result.stderr, (str(helper), argv, result.returncode, result.stderr)
        assert os.statvfs(helper).f_flag & os.ST_RDONLY, ('writable Git shim', str(helper))
        try:
            fd = os.open(helper, os.O_WRONLY | os.O_APPEND)
        except OSError as error:
            assert error.errno in (errno.EROFS, errno.EACCES, errno.EPERM), str(error)
        else:
            os.close(fd)
            raise AssertionError(('Git shim accepted a write-open', str(helper)))
        helpers.append(str(helper))
assert helpers, 'no Git helpers checked'
assert subprocess.run(['unshare', '-Ur', 'true'], capture_output=True).returncode != 0, 'nested user namespace unexpectedly allowed'
print(json.dumps({'result': 'PASS', 'sandbox_startup': True, 'imports': True, 'network_isolated': True, 'nested_userns_blocked': True, 'git_direct_helpers_blocked': helpers, 'gateway_without_token_fails_closed': [str(p) for p in gateways], 'git_mounts_read_only': True}))
'''
        argv, env = build_sandboxed_runner_invocation(
            runtime_root=runtime, pack=pack,
            argv=["/usr/bin/python3", "-c", code, os.readlink("/proc/self/ns/net")],
            env={"PATH": "/usr/bin:/bin", "API_KEY": "non-secret-test-marker"},
        )
        result = subprocess.run(argv, env=env, cwd=workspace, capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise SystemExit(f"FAIL: bwrap exit {result.returncode}\n{result.stderr}\n{result.stdout}")
        print(result.stdout.strip())
        assert not (workspace / ".git").exists()
        print("Disposable runtime removed on exit; no Pal service, socket, credentials, or model used")


if __name__ == "__main__":
    main()
