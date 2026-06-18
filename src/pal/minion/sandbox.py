from __future__ import annotations

import contextlib
import os
import platform
import shutil
import site
import sys
import sysconfig
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pal.foundation.sidecar import python_subprocess_env
from pal.shared import TaskContextPack, llm_tool_name


MINION_SANDBOX_BLACKLIST_COMMANDS = (
    "sudo",
    "su",
    "doas",
    "mount",
    "umount",
    "unshare",
    "nsenter",
    "chroot",
    "bwrap",
    "setpriv",
    "capsh",
    "systemctl",
    "service",
    "launchctl",
    "docker",
    "podman",
    "colima",
    "ssh",
    "scp",
    "rsync",
)

_SECRET_ENV_MARKERS = (
    "API_KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "CREDENTIAL",
    "PRIVATE_KEY",
    "ACCESS_KEY",
)


@dataclass(frozen=True)
class MinionSandboxSpec:
    enabled: bool
    backend: str
    runtime_root: Path
    run_id: str
    workspace_path: Path | None = None
    scratch_dir: Path | None = None
    deny_dir: Path | None = None
    blacklist_commands: tuple[str, ...] = field(default_factory=lambda: MINION_SANDBOX_BLACKLIST_COMMANDS)

    def to_metadata(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "backend": self.backend,
            "run_id": self.run_id,
            "workspace_path": str(self.workspace_path or ""),
            "scratch_dir": str(self.scratch_dir or ""),
            "blacklist_commands": list(self.blacklist_commands),
            "network": "open",
            "secret_policy": "host_llm_broker",
        }


def sandbox_supported_backend() -> str:
    system = platform.system().lower()
    if system == "linux" and shutil.which("bwrap"):
        return "bwrap"
    return ""


def with_minion_sandbox_metadata(runtime_root: Path, pack: TaskContextPack, *, run_id: str) -> TaskContextPack:
    metadata = dict(pack.metadata or {})
    sandbox_config = dict(metadata.get("sandbox") or {})
    if _falsey(sandbox_config.get("enabled")) or _falsey(os.environ.get("PAL_MINION_SANDBOX")):
        metadata["sandbox"] = {"enabled": False, "backend": "disabled"}
        return TaskContextPack.from_dict({**pack.to_dict(), "metadata": metadata})
    backend = str(sandbox_config.get("backend") or os.environ.get("PAL_MINION_SANDBOX_BACKEND") or sandbox_supported_backend()).strip()
    if not backend:
        metadata["sandbox"] = {"enabled": True, "backend": "unavailable", "reason": "no supported minion sandbox backend found"}
        return TaskContextPack.from_dict({**pack.to_dict(), "metadata": metadata})
    if backend != "bwrap":
        metadata["sandbox"] = {"enabled": True, "backend": "unavailable", "reason": f"unsupported minion sandbox backend: {backend}"}
        return TaskContextPack.from_dict({**pack.to_dict(), "metadata": metadata})
    workspace_path = _workspace_path_from_pack(pack)
    scratch_dir = Path(runtime_root) / "data" / "minion" / "sandbox" / "runs" / _safe_component(run_id)
    deny_dir = Path(runtime_root) / "data" / "minion" / "sandbox" / "deny-bin"
    metadata["sandbox"] = MinionSandboxSpec(
        enabled=True,
        backend=backend,
        runtime_root=Path(runtime_root),
        run_id=run_id,
        workspace_path=workspace_path,
        scratch_dir=scratch_dir,
        deny_dir=deny_dir,
        blacklist_commands=tuple(
            str(item).strip()
            for item in list(sandbox_config.get("blacklist_commands") or MINION_SANDBOX_BLACKLIST_COMMANDS)
            if str(item).strip()
        ),
    ).to_metadata()
    return TaskContextPack.from_dict({**pack.to_dict(), "metadata": metadata})


def minion_sandbox_is_enabled(pack: TaskContextPack | dict[str, Any] | None) -> bool:
    metadata = dict(pack.metadata or {}) if isinstance(pack, TaskContextPack) else dict((pack or {}).get("metadata") or {})
    sandbox = dict(metadata.get("sandbox") or {})
    return bool(sandbox.get("enabled"))


def build_sandboxed_runner_invocation(
    *,
    runtime_root: Path,
    pack: TaskContextPack,
    argv: list[str],
    env: dict[str, str] | None = None,
) -> tuple[list[str], dict[str, str]]:
    metadata = dict(pack.metadata or {})
    sandbox = dict(metadata.get("sandbox") or {})
    if not bool(sandbox.get("enabled")):
        final_env = dict(env or python_subprocess_env())
        final_env["PAL_MINION_LLM_BROKER"] = "0"
        return argv, final_env
    backend = str(sandbox.get("backend") or "").strip()
    if backend == "bwrap":
        final_env = scrub_minion_sandbox_env(env or python_subprocess_env(), runtime_root=runtime_root, run_id=str(sandbox.get("run_id") or ""))
        return _build_bwrap_invocation(runtime_root=runtime_root, pack=pack, sandbox=sandbox, argv=argv), final_env
    if backend == "docker":
        raise RuntimeError("macOS Docker minion sandbox requires PAL_MINION_DOCKER_IMAGE; the Docker launcher is not wired yet")
    raise RuntimeError(f"unsupported minion sandbox backend: {backend or 'unknown'}")


def scrub_minion_sandbox_env(env: dict[str, str], *, runtime_root: Path, run_id: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in dict(env or {}).items():
        upper = str(key or "").upper()
        if any(marker in upper for marker in _SECRET_ENV_MARKERS):
            continue
        if upper in {"AWS_SESSION_TOKEN", "GOOGLE_APPLICATION_CREDENTIALS"}:
            continue
        result[str(key)] = str(value)
    scratch = Path(runtime_root) / "data" / "minion" / "sandbox" / "runs" / _safe_component(run_id or "run")
    result["PAL_MINION_SANDBOXED"] = "1"
    result["PAL_MINION_LLM_BROKER"] = "1"
    result["HOME"] = str(scratch / "home")
    result["TMPDIR"] = str(scratch / "tmp")
    result["XDG_CACHE_HOME"] = str(scratch / "cache")
    result["PYTHONPYCACHEPREFIX"] = str(scratch / "pycache")
    result["PYTHONDONTWRITEBYTECODE"] = "1"
    python_paths = [str(_pal_source_root()), *_python_dependency_paths()]
    existing_pythonpath = str(result.get("PYTHONPATH") or "").strip()
    if existing_pythonpath:
        python_paths.extend(item for item in existing_pythonpath.split(os.pathsep) if item)
    if python_paths:
        result["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(python_paths))
    return result


def ensure_sandbox_files(runtime_root: Path, *, run_id: str, blacklist_commands: tuple[str, ...]) -> tuple[Path, Path]:
    sandbox_root = Path(runtime_root) / "data" / "minion" / "sandbox"
    scratch = sandbox_root / "runs" / _safe_component(run_id or "run")
    deny_dir = sandbox_root / "deny-bin"
    for path in (scratch / "tmp", scratch / "home", scratch / "cache", scratch / "pycache", deny_dir):
        path.mkdir(parents=True, exist_ok=True)
    for command in blacklist_commands:
        target = deny_dir / command
        target.write_text(_deny_wrapper_text(command), encoding="utf-8")
        target.chmod(0o755)
    return scratch, deny_dir


def _build_bwrap_invocation(
    *,
    runtime_root: Path,
    pack: TaskContextPack,
    sandbox: dict[str, Any],
    argv: list[str],
) -> list[str]:
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise RuntimeError("bubblewrap is required for Linux minion sandboxing")
    run_id = str(sandbox.get("run_id") or (pack.metadata or {}).get("run_id") or "run")
    blacklist = tuple(str(item).strip() for item in list(sandbox.get("blacklist_commands") or MINION_SANDBOX_BLACKLIST_COMMANDS) if str(item).strip())
    scratch, deny_dir = ensure_sandbox_files(runtime_root, run_id=run_id, blacklist_commands=blacklist)
    workspace_path = Path(str(sandbox.get("workspace_path") or _workspace_path_from_pack(pack) or "")).expanduser()

    args: list[str] = [
        bwrap,
        "--die-with-parent",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--share-net",
        "--proc",
        "/proc",
        "--dev-bind",
        "/dev",
        "/dev",
        "--bind",
        str(scratch / "tmp"),
        "/tmp",
    ]
    for link_name, target_name in (("/bin", "usr/bin"), ("/sbin", "usr/sbin"), ("/lib", "usr/lib")):
        if Path(link_name).is_symlink():
            args.extend(["--symlink", target_name, link_name])
    for path in ("/usr", "/etc", "/lib64"):
        host_path = Path(path)
        if host_path.exists():
            args.extend(["--dir", str(host_path)])
            args.extend(["--ro-bind", str(host_path), str(host_path)])
    _append_dir_scaffold(args, Path(runtime_root))
    _append_runtime_root_binds(args, Path(runtime_root))
    source_root = _pal_source_root()
    _append_bind_path(args, source_root, read_only=True)
    for python_path in _python_dependency_paths():
        _append_bind_path(args, Path(python_path), read_only=True)
    nvm_root = Path.home() / ".nvm"
    if nvm_root.exists():
        _append_bind_path(args, nvm_root, read_only=True)
    if workspace_path and workspace_path.exists():
        _append_bind_path(args, workspace_path, read_only=False)
    for command in blacklist:
        wrapper = deny_dir / command
        if not wrapper.exists():
            continue
        for target in _command_targets(command):
            if target.exists():
                args.extend(["--ro-bind", str(wrapper), str(target)])
    args.extend(["--chdir", _sandbox_cwd(pack), "--"])
    args.extend(argv)
    return args


def _append_runtime_root_binds(args: list[str], runtime_root: Path) -> None:
    runtime_root = Path(runtime_root).expanduser()
    _append_dir_scaffold(args, runtime_root)
    for file_name in ("pal.sqlite3", "pal.sqlite3-shm", "pal.sqlite3-wal", "config.toml"):
        path = runtime_root / file_name
        if path.exists():
            read_only = file_name == "config.toml"
            args.extend(["--ro-bind" if read_only else "--bind", str(path), str(path)])
    for dir_name, read_only in (
        ("data/minion", False),
        ("data/lsp", False),
        ("artifacts", False),
        ("tool_results", False),
        ("plugins", True),
        ("SKILL", True),
    ):
        path = runtime_root / dir_name
        if path.exists():
            _append_dir_scaffold(args, path)
            args.extend(["--ro-bind" if read_only else "--bind", str(path), str(path)])


def _append_bind_path(args: list[str], path: Path, *, read_only: bool) -> None:
    path = Path(path).expanduser()
    _append_dir_scaffold(args, path)
    args.extend(["--ro-bind" if read_only else "--bind", str(path), str(path)])


def _append_dir_scaffold(args: list[str], path: Path) -> None:
    parts = Path(path).expanduser().parts
    if not parts or parts[0] != "/":
        return
    current = Path("/")
    for part in parts[1:]:
        current = current / part
        args.extend(["--dir", str(current)])


def _command_targets(command: str) -> tuple[Path, ...]:
    return tuple(Path(prefix) / command for prefix in ("/usr/bin", "/bin", "/usr/local/bin", "/usr/sbin", "/sbin"))


def _sandbox_cwd(pack: TaskContextPack) -> str:
    path = _workspace_path_from_pack(pack)
    return str(path) if path else "/tmp"


def _workspace_path_from_pack(pack: TaskContextPack) -> Path | None:
    workspace = dict(pack.workspace or {})
    for key in ("repo_path", "task_repo_path", "target_repo_path", "run_dir"):
        value = str(workspace.get(key) or "").strip()
        if value:
            return Path(value).expanduser()
    return None


def _safe_component(value: str) -> str:
    safe = [char if char.isalnum() or char in {"-", "_", "."} else "_" for char in str(value or "")]
    return ("".join(safe).strip("._") or "run")[:120]


def _python_dependency_paths() -> list[str]:
    candidates: list[str] = []
    for key in ("purelib", "platlib"):
        value = sysconfig.get_paths().get(key)
        if value:
            candidates.append(value)
    with contextlib.suppress(Exception):
        candidates.append(site.getusersitepackages())
    for value in sys.path:
        text = str(value or "").strip()
        if "site-packages" in text or "dist-packages" in text:
            candidates.append(text)
    paths: list[str] = []
    for value in candidates:
        path = Path(value).expanduser()
        if not path.is_absolute() or not path.exists() or not path.is_dir():
            continue
        paths.append(str(path))
    return list(dict.fromkeys(paths))


def _pal_source_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _falsey(value: Any) -> bool:
    return str(value or "").strip().lower() in {"0", "false", "no", "off", "disabled"}


def _deny_wrapper_text(command: str) -> str:
    capability_hint = (
        "Use Pal resident capabilities when available: "
        f"{llm_tool_name('op_file_read')} for repo file reads, "
        f"{llm_tool_name('op_file_edit')} for precise text edits, "
        f"{llm_tool_name('op_file_write')} for file writes, "
        f"{llm_tool_name('op_path_delete')} for deleting repo paths, "
        f"{llm_tool_name('op_tree')} for structured listings, "
        f"{llm_tool_name('op_search')} for repo text search, "
        f"{llm_tool_name('op_git')} for git inspection/audited git changes, and "
        f"{llm_tool_name('op_minion_checkpoint_commit')} for milestone checkpoint commits."
    )
    return (
        "#!/bin/sh\n"
        f"echo \"pal minion sandbox blocked command '{command}'. This command is outside the sandboxed minion authority.\" >&2\n"
        f"echo \"{capability_hint}\" >&2\n"
        "echo \"Keep run_shell for sandbox-local tests, builds, scripts, package commands, and process probes.\" >&2\n"
        "exit 126\n"
    )
