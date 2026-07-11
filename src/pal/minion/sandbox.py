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

from pal.foundation.log_paths import pal_log_root
from pal.foundation.sidecar import python_subprocess_env
from pal.shared import RUN_SHELL_SCOPE_HINT, MinionInvocationPack, format_dedicated_tool_route_hints


PAL_MINION_SANDBOX_SCRATCH_ROOT_ENV = "PAL_MINION_SANDBOX_SCRATCH_ROOT"
PAL_MINION_SANDBOX_MIN_FREE_MB_ENV = "PAL_MINION_SANDBOX_MIN_FREE_MB"
PAL_MINION_SANDBOX_MAX_RUN_DIRS_ENV = "PAL_MINION_SANDBOX_MAX_RUN_DIRS"
DEFAULT_MINION_SANDBOX_MIN_FREE_MB = 256
DEFAULT_MINION_SANDBOX_MAX_RUN_DIRS = 128

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
    "rm",
    "unlink",
    "rmdir",
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
    git_metadata_bind_paths: tuple[Path, ...] = field(default_factory=tuple)

    def to_metadata(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "backend": self.backend,
            "run_id": self.run_id,
            "workspace_path": str(self.workspace_path or ""),
            "scratch_dir": str(self.scratch_dir or ""),
            "blacklist_commands": list(self.blacklist_commands),
            "git_metadata_bind_paths": [str(path) for path in self.git_metadata_bind_paths],
            "network": "open",
            "secret_policy": "host_llm_broker",
        }


def sandbox_supported_backend() -> str:
    system = platform.system().lower()
    if system == "linux" and shutil.which("bwrap"):
        return "bwrap"
    return ""


def with_minion_sandbox_metadata(runtime_root: Path, pack: MinionInvocationPack, *, run_id: str) -> MinionInvocationPack:
    metadata = dict(pack.metadata or {})
    sandbox_config = dict(metadata.get("sandbox") or {})
    if _falsey(sandbox_config.get("enabled")) or _falsey(os.environ.get("PAL_MINION_SANDBOX")):
        metadata["sandbox"] = {"enabled": False, "backend": "disabled"}
        return MinionInvocationPack.from_dict({**pack.to_dict(), "metadata": metadata})
    backend = str(sandbox_config.get("backend") or os.environ.get("PAL_MINION_SANDBOX_BACKEND") or sandbox_supported_backend()).strip()
    if not backend:
        metadata["sandbox"] = {"enabled": True, "backend": "unavailable", "reason": "no supported minion sandbox backend found"}
        return MinionInvocationPack.from_dict({**pack.to_dict(), "metadata": metadata})
    if backend != "bwrap":
        metadata["sandbox"] = {"enabled": True, "backend": "unavailable", "reason": f"unsupported minion sandbox backend: {backend}"}
        return MinionInvocationPack.from_dict({**pack.to_dict(), "metadata": metadata})
    workspace_path = _workspace_path_from_pack(pack)
    scratch_dir = minion_sandbox_scratch_dir(runtime_root, run_id)
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
        git_metadata_bind_paths=_git_worktree_metadata_bind_paths(workspace_path),
    ).to_metadata()
    return MinionInvocationPack.from_dict({**pack.to_dict(), "metadata": metadata})


def minion_sandbox_is_enabled(pack: MinionInvocationPack | dict[str, Any] | None) -> bool:
    metadata = dict(pack.metadata or {}) if isinstance(pack, MinionInvocationPack) else dict((pack or {}).get("metadata") or {})
    sandbox = dict(metadata.get("sandbox") or {})
    return bool(sandbox.get("enabled"))


def minion_sandbox_scratch_dir(runtime_root: Path, run_id: str) -> Path:
    safe_run_id = _safe_component(run_id or "run")
    temp_root = _preferred_sandbox_scratch_root(runtime_root)
    if _scratch_root_is_usable(temp_root):
        return temp_root / safe_run_id
    return _runtime_sandbox_scratch_root(runtime_root) / safe_run_id


def build_sandboxed_runner_invocation(
    *,
    runtime_root: Path,
    pack: MinionInvocationPack,
    argv: list[str],
    env: dict[str, str] | None = None,
) -> tuple[list[str], dict[str, str]]:
    metadata = dict(pack.metadata or {})
    sandbox = dict(metadata.get("sandbox") or {})
    if not bool(sandbox.get("enabled")):
        final_env = dict(env or python_subprocess_env())
        _apply_workspace_execution_env(final_env, pack)
        final_env["PAL_MINION_LLM_BROKER"] = "0"
        return argv, final_env
    backend = str(sandbox.get("backend") or "").strip()
    if backend == "bwrap":
        final_env = scrub_minion_sandbox_env(
            env or python_subprocess_env(),
            runtime_root=runtime_root,
            run_id=str(sandbox.get("run_id") or ""),
            pack=pack,
            scratch_dir=sandbox.get("scratch_dir"),
        )
        return _build_bwrap_invocation(runtime_root=runtime_root, pack=pack, sandbox=sandbox, argv=argv), final_env
    if backend == "docker":
        raise RuntimeError("macOS Docker minion sandbox requires PAL_MINION_DOCKER_IMAGE; the Docker launcher is not wired yet")
    raise RuntimeError(f"unsupported minion sandbox backend: {backend or 'unknown'}")


def _coerce_scratch_dir(runtime_root: Path, run_id: str, scratch_dir: str | Path | None) -> Path:
    text = str(scratch_dir or "").strip()
    if text:
        return Path(text).expanduser()
    return minion_sandbox_scratch_dir(runtime_root, run_id)


def _preferred_sandbox_scratch_root(runtime_root: Path) -> Path:
    override = str(os.environ.get(PAL_MINION_SANDBOX_SCRATCH_ROOT_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    return pal_log_root(runtime_root) / "minion" / "sandbox" / "runs"


def _runtime_sandbox_scratch_root(runtime_root: Path) -> Path:
    return Path(runtime_root) / "data" / "minion" / "sandbox" / "runs"


def _scratch_root_is_usable(root: Path) -> bool:
    probe = Path(root).expanduser() / f".probe_{os.getpid()}"
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe.mkdir()
        (probe / "write").write_text("ok", encoding="utf-8")
        minimum = _sandbox_min_free_bytes()
        if minimum > 0 and shutil.disk_usage(root).free < minimum:
            return False
        return True
    except OSError:
        return False
    finally:
        with contextlib.suppress(OSError):
            (probe / "write").unlink()
        with contextlib.suppress(OSError):
            probe.rmdir()


def _sandbox_min_free_bytes() -> int:
    return max(0, _env_int(PAL_MINION_SANDBOX_MIN_FREE_MB_ENV, DEFAULT_MINION_SANDBOX_MIN_FREE_MB)) * 1024 * 1024


def _sandbox_max_run_dirs() -> int:
    return max(0, _env_int(PAL_MINION_SANDBOX_MAX_RUN_DIRS_ENV, DEFAULT_MINION_SANDBOX_MAX_RUN_DIRS))


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default


def scrub_minion_sandbox_env(
    env: dict[str, str],
    *,
    runtime_root: Path,
    run_id: str,
    pack: MinionInvocationPack | None = None,
    scratch_dir: str | Path | None = None,
) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in dict(env or {}).items():
        upper = str(key or "").upper()
        if any(marker in upper for marker in _SECRET_ENV_MARKERS):
            continue
        if upper in {"AWS_SESSION_TOKEN", "GOOGLE_APPLICATION_CREDENTIALS"}:
            continue
        result[str(key)] = str(value)
    scratch = _coerce_scratch_dir(runtime_root, run_id, scratch_dir)
    result["PAL_MINION_SANDBOXED"] = "1"
    result["PAL_MINION_LLM_BROKER"] = "1"
    result["HOME"] = str(scratch / "home")
    result["TMPDIR"] = str(scratch / "tmp")
    result["XDG_CACHE_HOME"] = str(scratch / "cache")
    result["PYTHONPYCACHEPREFIX"] = str(scratch / "pycache")
    result["PYTHONDONTWRITEBYTECODE"] = "1"
    python_user_base = _python_user_base()
    if python_user_base is not None:
        result["PYTHONUSERBASE"] = str(python_user_base)
    python_paths = [str(_pal_source_root()), *_python_dependency_paths()]
    existing_pythonpath = str(result.get("PYTHONPATH") or "").strip()
    if existing_pythonpath:
        python_paths.extend(item for item in existing_pythonpath.split(os.pathsep) if item)
    if python_paths:
        result["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(python_paths))
    if pack is not None:
        _apply_workspace_execution_env(result, pack)
    return result


def _apply_workspace_execution_env(env: dict[str, str], pack: MinionInvocationPack) -> None:
    execution_env = dict((pack.workspace or {}).get("execution_env") or {})
    vars_payload = execution_env.get("vars")
    if isinstance(vars_payload, dict):
        for name, value in dict(vars_payload).items():
            key = str(name or "").strip()
            if not _safe_workspace_env_var_name(key):
                continue
            text = str(value or "").strip()
            if text:
                env[key] = text
    path_prepend = execution_env.get("path_prepend")
    if not isinstance(path_prepend, dict):
        return
    for name, paths in dict(path_prepend).items():
        key = str(name or "").strip()
        if not key:
            continue
        if isinstance(paths, str):
            raw_paths = [paths]
        elif isinstance(paths, (list, tuple)):
            raw_paths = list(paths)
        else:
            continue
        additions = [str(item).strip() for item in raw_paths if str(item).strip()]
        if not additions:
            continue
        existing = [item for item in str(env.get(key) or "").split(os.pathsep) if item]
        env[key] = os.pathsep.join(dict.fromkeys([*additions, *existing]))


def _safe_workspace_env_var_name(name: str) -> bool:
    if not name:
        return False
    if any(marker in name.upper() for marker in _SECRET_ENV_MARKERS):
        return False
    return all(char.isalnum() or char == "_" for char in name)


def ensure_sandbox_files(
    runtime_root: Path,
    *,
    run_id: str,
    blacklist_commands: tuple[str, ...],
    scratch_dir: str | Path | None = None,
) -> tuple[Path, Path]:
    sandbox_root = Path(runtime_root) / "data" / "minion" / "sandbox"
    scratch = _coerce_scratch_dir(runtime_root, run_id, scratch_dir)
    deny_dir = sandbox_root / "deny-bin"
    for path in (scratch / "tmp", scratch / "home", scratch / "cache", scratch / "pycache", deny_dir):
        path.mkdir(parents=True, exist_ok=True)
    _prune_sandbox_run_dirs(scratch.parent, keep_run_id=_safe_component(run_id or "run"))
    for command in blacklist_commands:
        target = deny_dir / command
        target.write_text(_deny_wrapper_text(command), encoding="utf-8")
        target.chmod(0o755)
    return scratch, deny_dir


def _prune_sandbox_run_dirs(root: Path, *, keep_run_id: str) -> None:
    max_dirs = _sandbox_max_run_dirs()
    if max_dirs <= 0:
        return
    try:
        entries = [path for path in Path(root).iterdir() if path.is_dir() and not path.name.startswith(".")]
    except OSError:
        return
    if len(entries) <= max_dirs:
        return

    def sort_key(path: Path) -> tuple[int, float, str]:
        try:
            stat = path.stat()
            mtime = float(stat.st_mtime)
        except OSError:
            mtime = 0.0
        is_current = 1 if path.name == keep_run_id else 0
        return (is_current, mtime, path.name)

    keep: set[Path] = set(sorted(entries, key=sort_key, reverse=True)[:max_dirs])
    for path in entries:
        if path in keep:
            continue
        shutil.rmtree(path, ignore_errors=True)


def _build_bwrap_invocation(
    *,
    runtime_root: Path,
    pack: MinionInvocationPack,
    sandbox: dict[str, Any],
    argv: list[str],
) -> list[str]:
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise RuntimeError("bubblewrap is required for Linux minion sandboxing")
    run_id = str(sandbox.get("run_id") or (pack.metadata or {}).get("run_id") or "run")
    blacklist = tuple(str(item).strip() for item in list(sandbox.get("blacklist_commands") or MINION_SANDBOX_BLACKLIST_COMMANDS) if str(item).strip())
    scratch, deny_dir = ensure_sandbox_files(
        runtime_root,
        run_id=run_id,
        blacklist_commands=blacklist,
        scratch_dir=sandbox.get("scratch_dir"),
    )
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
    workspace_policy = dict(pack.workspace.get("workspace_policy") or {})
    read_only_workspace = str(workspace_policy.get("mode") or "").strip().lower() == "read_only_repo"
    for bind_path in _sandbox_git_metadata_bind_paths(sandbox):
        _append_bind_path(args, bind_path, read_only=read_only_workspace)
    if workspace_path and workspace_path.exists():
        _append_bind_path(args, workspace_path, read_only=read_only_workspace)
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
        ("data/tool_results", False),
        ("artifacts", False),
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


def _sandbox_git_metadata_bind_paths(sandbox: dict[str, Any]) -> tuple[Path, ...]:
    paths: list[Path] = []
    for raw in list(sandbox.get("git_metadata_bind_paths") or []):
        value = str(raw or "").strip()
        if not value:
            continue
        path = Path(value).expanduser()
        if path.exists():
            paths.append(path.resolve())
    return _dedupe_bind_roots(paths)


def _git_worktree_metadata_bind_paths(workspace_path: Path | None) -> tuple[Path, ...]:
    if not workspace_path:
        return ()
    workspace = Path(workspace_path).expanduser()
    git_file = workspace / ".git"
    if not git_file.is_file():
        return ()
    try:
        text = git_file.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ()
    prefix = "gitdir:"
    if not text.lower().startswith(prefix):
        return ()
    git_dir_text = text.split(":", 1)[1].strip()
    if not git_dir_text:
        return ()
    git_dir = Path(git_dir_text).expanduser()
    if not git_dir.is_absolute():
        git_dir = workspace / git_dir
    try:
        git_dir = git_dir.resolve()
    except OSError:
        return ()
    if not git_dir.exists():
        return ()
    common_dir = _git_common_dir_from_worktree_admin(git_dir)
    candidates = [path for path in (common_dir, git_dir) if path is not None and path.exists()]
    return _dedupe_bind_roots(candidates)


def _git_common_dir_from_worktree_admin(git_dir: Path) -> Path | None:
    common_file = git_dir / "commondir"
    if not common_file.is_file():
        return None
    try:
        common_text = common_file.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not common_text:
        return None
    common_dir = Path(common_text).expanduser()
    if not common_dir.is_absolute():
        common_dir = git_dir / common_dir
    try:
        return common_dir.resolve()
    except OSError:
        return None


def _dedupe_bind_roots(paths: list[Path]) -> tuple[Path, ...]:
    roots: list[Path] = []
    for raw_path in paths:
        try:
            path = raw_path.resolve()
        except OSError:
            continue
        if any(_path_is_relative_to(path, existing) for existing in roots):
            continue
        roots = [existing for existing in roots if not _path_is_relative_to(existing, path)]
        roots.append(path)
    return tuple(roots)


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


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


def _sandbox_cwd(pack: MinionInvocationPack) -> str:
    path = _workspace_path_from_pack(pack)
    return str(path) if path else "/tmp"


def _workspace_path_from_pack(pack: MinionInvocationPack) -> Path | None:
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


def _python_user_base() -> Path | None:
    with contextlib.suppress(Exception):
        value = site.getuserbase()
        path = Path(value).expanduser()
        if path.is_absolute():
            return path
    return None


def _pal_source_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _falsey(value: Any) -> bool:
    return str(value or "").strip().lower() in {"0", "false", "no", "off", "disabled"}


def _deny_wrapper_text(command: str) -> str:
    capability_hint = f"Use Pal resident capabilities when available: {format_dedicated_tool_route_hints()}."
    return (
        "#!/bin/sh\n"
        f"echo \"pal minion sandbox blocked command '{command}'. This command is outside the sandboxed minion authority.\" >&2\n"
        f"echo \"{capability_hint}\" >&2\n"
        f"echo \"{RUN_SHELL_SCOPE_HINT}\" >&2\n"
        "exit 126\n"
    )
