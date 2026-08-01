from __future__ import annotations

import contextlib
import os
import platform
import shlex
import shutil
import site
import sys
import sysconfig
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from pal.foundation.log_paths import pal_log_root
from pal.foundation.sidecar import python_subprocess_env
from pal.minion.ipc import (
    ROLE_GATEWAY_TOKEN_ENV,
    minion_role_socket_path,
)
from pal.minion.workspace_tools import _normalized_reference_paths
from pal.shared import MinionInvocationPack


PAL_MINION_SANDBOX_SCRATCH_ROOT_ENV = "PAL_MINION_SANDBOX_SCRATCH_ROOT"
PAL_MINION_SANDBOX_MIN_FREE_MB_ENV = "PAL_MINION_SANDBOX_MIN_FREE_MB"
PAL_MINION_SANDBOX_MAX_RUN_DIRS_ENV = "PAL_MINION_SANDBOX_MAX_RUN_DIRS"
PAL_MINION_RUNTIME_ROOT_ENV = "PAL_MINION_RUNTIME_ROOT"
DEFAULT_MINION_SANDBOX_MIN_FREE_MB = 256
DEFAULT_MINION_SANDBOX_MAX_RUN_DIRS = 128
MINION_SANDBOX_REFERENCE_ROOT = PurePosixPath("/pal/references")

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

    def to_metadata(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "backend": self.backend,
            "run_id": self.run_id,
            "workspace_path": str(self.workspace_path or ""),
            "scratch_dir": str(self.scratch_dir or ""),
            "network": "isolated",
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
        raise RuntimeError("Minion execution requires an OS sandbox and cannot be disabled")
    backend = str(sandbox_config.get("backend") or os.environ.get("PAL_MINION_SANDBOX_BACKEND") or sandbox_supported_backend()).strip()
    if not backend:
        raise RuntimeError("Minion execution requires bubblewrap on Linux")
    if backend != "bwrap":
        raise RuntimeError(f"Minion execution requires the bubblewrap adapter; unsupported backend: {backend}")
    workspace_path = _workspace_path_from_pack(pack)
    scratch_dir = minion_sandbox_scratch_dir(runtime_root, run_id)
    sandbox_metadata = MinionSandboxSpec(
        enabled=True,
        backend=backend,
        runtime_root=Path(runtime_root),
        run_id=run_id,
        workspace_path=workspace_path,
        scratch_dir=scratch_dir,
    ).to_metadata()
    existing_reference_binds = [
        dict(item or {})
        for item in list(sandbox_config.get("reference_binds") or [])
        if isinstance(item, dict)
    ]
    if existing_reference_binds:
        workspace = _reconcile_projected_reference_paths(
            dict(pack.workspace or {}),
            existing_reference_binds,
        )
        reference_binds = existing_reference_binds
    else:
        workspace, reference_binds = _project_sandbox_references(dict(pack.workspace or {}))
    sandbox_metadata["reference_binds"] = reference_binds
    metadata["sandbox"] = sandbox_metadata
    return MinionInvocationPack.from_dict(
        {**pack.to_dict(), "workspace": workspace, "metadata": metadata}
    )


def _project_sandbox_references(
    workspace: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    projected_workspace = dict(workspace or {})
    projected_references: list[dict[str, Any]] = []
    reference_binds: list[dict[str, Any]] = []
    targets: set[str] = set()
    for reference in _normalized_reference_paths(projected_workspace):
        item = dict(reference)
        if bool(item.get("bound_input")):
            projected_references.append(item)
            continue
        name = str(item.get("name") or "reference").strip()
        target = str(MINION_SANDBOX_REFERENCE_ROOT / _safe_reference_component(name))
        if target in targets:
            raise RuntimeError(f"sandbox reference path collision: {name}")
        targets.add(target)
        bind = {
            "name": name,
            "source_path": str(item.get("path") or ""),
            "target_path": target,
            "include": list(item.get("include") or []),
            "required": bool(item.get("required", True)),
        }
        reference_binds.append(bind)
        item["path"] = _projected_reference_path(bind)
        projected_references.append(item)
    projected_workspace["reference_paths"] = projected_references
    return projected_workspace, reference_binds


def _reconcile_projected_reference_paths(
    workspace: dict[str, Any],
    reference_binds: list[dict[str, Any]],
) -> dict[str, Any]:
    projected = dict(workspace or {})
    binds_by_name = {
        str(item.get("name") or ""): dict(item)
        for item in reference_binds
        if str(item.get("name") or "")
    }
    references: list[dict[str, Any]] = []
    for raw in list(projected.get("reference_paths") or []):
        item = dict(raw or {})
        bind = binds_by_name.get(str(item.get("name") or ""))
        if bind is not None and not bool(item.get("bound_input")):
            item["path"] = _projected_reference_path(bind)
        references.append(item)
    projected["reference_paths"] = references
    return projected


def _projected_reference_path(reference_bind: dict[str, Any]) -> str:
    target = PurePosixPath(str(reference_bind.get("target_path") or ""))
    includes = [
        str(item).replace("\\", "/").strip()
        for item in list(reference_bind.get("include") or [])
        if str(item).strip()
    ]
    if len(includes) == 1 and not any(char in includes[0] for char in "*?["):
        source_file = Path(str(reference_bind.get("source_path") or "")).expanduser() / includes[0]
        if source_file.is_file():
            return str(target.joinpath(*PurePosixPath(includes[0]).parts))
    return str(target)


def _safe_reference_component(value: str) -> str:
    safe = [
        char.lower() if char.isalnum() else char if char in {"-", "_"} else "_"
        for char in str(value or "")
    ]
    return ("".join(safe).strip("_-") or "reference")[:80]


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
        raise RuntimeError("Minion runner invocation is missing its required OS sandbox")
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
    assignment_token = str(dict(env or {}).get(ROLE_GATEWAY_TOKEN_ENV) or "")
    for key, value in dict(env or {}).items():
        upper = str(key or "").upper()
        if any(marker in upper for marker in _SECRET_ENV_MARKERS):
            continue
        if upper in {"AWS_SESSION_TOKEN", "GOOGLE_APPLICATION_CREDENTIALS"}:
            continue
        result[str(key)] = str(value)
    scratch = _coerce_scratch_dir(runtime_root, run_id, scratch_dir)
    result["PAL_MINION_SANDBOXED"] = "1"
    result.pop("PAL_MINION_CONTINUATION_RETRY", None)
    result["PAL_MINION_WEB_BROKER"] = "1"
    result["PAL_DATABASE_READ_ONLY"] = "1"
    result[PAL_MINION_RUNTIME_ROOT_ENV] = str(Path(runtime_root).expanduser().resolve())
    if assignment_token:
        result[ROLE_GATEWAY_TOKEN_ENV] = assignment_token
    # bubblewrap mounts the host-side scratch/tmp directory at /tmp. Environment
    # paths must name the sandbox projection, not the now-hidden host path.
    result["HOME"] = "/tmp/home"
    result["TMPDIR"] = "/tmp"
    result["XDG_CACHE_HOME"] = "/tmp/cache"
    result["PYTHONPYCACHEPREFIX"] = "/tmp/pycache"
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
    workspace = dict(pack.workspace or {})
    manager_bound_names = {
        "PAL_WORKSPACE_ROOT",
        "PAL_BUILD_SCRATCH",
        "PAL_REVIEW_SCRATCH",
    }
    workspace_root = _workspace_path_from_pack(pack)
    if workspace_root is not None:
        env["PAL_WORKSPACE_ROOT"] = str(workspace_root)
    build_scratch = str(workspace.get("build_scratch_dir") or "").strip()
    if build_scratch:
        env["PAL_BUILD_SCRATCH"] = build_scratch
    review_scratch = str(workspace.get("review_scratch_dir") or "").strip()
    if review_scratch:
        env["PAL_REVIEW_SCRATCH"] = review_scratch
    execution_env = dict(workspace.get("execution_env") or {})
    vars_payload = execution_env.get("vars")
    if isinstance(vars_payload, dict):
        for name, value in dict(vars_payload).items():
            key = str(name or "").strip()
            if key in manager_bound_names or not _safe_workspace_env_var_name(key):
                continue
            text = str(value or "").strip()
            if text:
                env[key] = text
    path_prepend = execution_env.get("path_prepend")
    if not isinstance(path_prepend, dict):
        return
    for name, paths in dict(path_prepend).items():
        key = str(name or "").strip()
        if not key or key in manager_bound_names:
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
    scratch_dir: str | Path | None = None,
) -> tuple[Path, Path]:
    sandbox_root = Path(runtime_root) / "data" / "minion" / "sandbox"
    scratch = _coerce_scratch_dir(runtime_root, run_id, scratch_dir)
    shim_dir = sandbox_root / "shim-bin"
    projected_tmp = scratch / "tmp"
    for path in (
        projected_tmp,
        projected_tmp / "home",
        projected_tmp / "cache",
        projected_tmp / "pycache",
        shim_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)
    _prune_sandbox_run_dirs(scratch.parent, keep_run_id=_safe_component(run_id or "run"))
    git_wrapper = shim_dir / "git"
    git_wrapper.write_text(_git_wrapper_text(), encoding="utf-8")
    git_wrapper.chmod(0o755)
    git_internal_wrapper = shim_dir / "git-internal"
    git_internal_wrapper.write_text(_git_internal_wrapper_text(), encoding="utf-8")
    git_internal_wrapper.chmod(0o755)
    return scratch, shim_dir


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
    scratch, shim_dir = ensure_sandbox_files(
        runtime_root,
        run_id=run_id,
        scratch_dir=sandbox.get("scratch_dir"),
    )
    workspace_path = Path(str(sandbox.get("workspace_path") or _workspace_path_from_pack(pack) or "")).expanduser()

    args: list[str] = [
        bwrap,
        "--die-with-parent",
        "--unshare-user",
        "--disable-userns",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-net",
        "--proc",
        "/proc",
        "--dev",
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
    _append_runtime_root_binds(args, Path(runtime_root), pack)
    source_root = _pal_source_root()
    _append_bind_path(args, source_root, read_only=True)
    for python_path in _python_dependency_paths():
        _append_bind_path(args, Path(python_path), read_only=True)
    nvm_root = Path.home() / ".nvm"
    if nvm_root.exists():
        _append_bind_path(args, nvm_root, read_only=True)
    workspace_policy = dict(pack.workspace.get("workspace_policy") or {})
    read_only_workspace = str(workspace_policy.get("mode") or "").strip().lower() == "read_only_repo"
    if workspace_path and workspace_path.exists():
        _append_bind_path(
            args,
            workspace_path,
            read_only=read_only_workspace,
        )
        for raw_relative in list(
            pack.workspace.get("read_only_overlay_paths") or []
        ):
            relative = str(raw_relative or "").strip().replace("\\", "/")
            target = (workspace_path.resolve() / relative).resolve()
            if not relative or not target.is_relative_to(workspace_path.resolve()):
                raise RuntimeError(
                    f"read-only overlay escapes its worktree: {relative}"
                )
            if not target.exists():
                raise RuntimeError(
                    f"read-only overlay does not exist: {relative}"
                )
            _append_bind_path(args, target, read_only=True)
        git_marker = workspace_path / ".git"
        if git_marker.exists() or git_marker.is_symlink():
            _append_bind_path(args, git_marker, read_only=True)
    reference_binds = [
        dict(item or {})
        for item in list(sandbox.get("reference_binds") or [])
        if isinstance(item, dict)
    ]
    if not reference_binds:
        _, reference_binds = _project_sandbox_references(dict(pack.workspace or {}))
    _append_reference_projection_binds(args, reference_binds)
    git_wrapper = shim_dir / "git"
    for target in _git_entrypoint_targets():
        if target.exists():
            args.extend(["--ro-bind", str(git_wrapper), str(target)])
    git_internal_wrapper = shim_dir / "git-internal"
    for target in _git_internal_targets():
        args.extend(["--ro-bind", str(git_internal_wrapper), str(target)])
    args.extend(["--chdir", _sandbox_cwd(pack), "--"])
    args.extend(argv)
    return args


def _append_reference_projection_binds(
    args: list[str],
    references: list[dict[str, Any]],
) -> None:
    created_dirs: set[str] = set()
    for reference in references:
        source = Path(str(reference.get("source_path") or "")).expanduser()
        target = PurePosixPath(str(reference.get("target_path") or ""))
        required = bool(reference.get("required", True))
        if not source.exists() or not source.is_dir():
            if required:
                raise RuntimeError(
                    f"sandbox reference source is unavailable: {reference.get('name') or source}"
                )
            continue
        if not target.is_absolute() or target.parent != MINION_SANDBOX_REFERENCE_ROOT:
            raise RuntimeError(f"invalid sandbox reference target: {target}")
        includes = [str(item).strip() for item in list(reference.get("include") or []) if str(item).strip()]
        _append_virtual_dirs(args, target, created_dirs)
        if not includes:
            args.extend(["--ro-bind", str(source.resolve()), str(target)])
            continue
        args.extend(["--tmpfs", str(target)])
        matches = _reference_projection_matches(source, includes)
        if not matches and required:
            raise RuntimeError(
                f"sandbox reference include set is empty: {reference.get('name') or source}"
            )
        bound_directories: list[Path] = []
        for relative, resolved in matches:
            if any(relative == parent or relative.is_relative_to(parent) for parent in bound_directories):
                continue
            destination = target.joinpath(*relative.parts)
            if resolved.is_dir():
                _append_virtual_dirs(args, destination, created_dirs)
                args.extend(["--ro-bind", str(resolved), str(destination)])
                bound_directories.append(relative)
            else:
                _append_virtual_dirs(args, destination.parent, created_dirs)
                args.extend(["--ro-bind", str(resolved), str(destination)])
        args.extend(["--remount-ro", str(target)])


def _reference_projection_matches(
    source: Path,
    includes: list[str],
) -> list[tuple[Path, Path]]:
    root = source.resolve()
    matches: dict[Path, Path] = {}
    for pattern in includes:
        normalized = pattern.replace("\\", "/").strip()
        parts = PurePosixPath(normalized).parts
        if not normalized or normalized.startswith("/") or ".." in parts:
            raise RuntimeError(f"invalid sandbox reference include pattern: {pattern}")
        for candidate in source.glob(normalized):
            try:
                resolved = candidate.resolve(strict=True)
                relative = candidate.relative_to(source)
            except (OSError, ValueError):
                continue
            if not resolved.is_relative_to(root):
                raise RuntimeError(f"sandbox reference include escapes its source root: {candidate}")
            if relative.parts:
                matches[relative] = resolved
    return sorted(matches.items(), key=lambda item: (len(item[0].parts), str(item[0])))


def _append_virtual_dirs(
    args: list[str],
    path: PurePosixPath,
    created: set[str],
) -> None:
    current = PurePosixPath("/")
    for part in path.parts[1:]:
        current /= part
        value = str(current)
        if value in created:
            continue
        args.extend(["--dir", value])
        created.add(value)


def _append_runtime_root_binds(
    args: list[str],
    runtime_root: Path,
    pack: MinionInvocationPack,
) -> None:
    runtime_root = Path(runtime_root).expanduser()
    _append_dir_scaffold(args, runtime_root)
    for file_name in ("pal.sqlite3", "pal.sqlite3-shm", "pal.sqlite3-wal", "config.toml"):
        path = runtime_root / file_name
        if path.exists():
            args.extend(["--ro-bind", str(path), str(path)])
    for dir_name in ("data/lsp", "plugins", "SKILL"):
        path = runtime_root / dir_name
        if path.exists():
            _append_dir_scaffold(args, path)
            args.extend(["--ro-bind", str(path), str(path)])
    run_dir_value = str(pack.workspace.get("run_dir") or "").strip()
    run_dir = Path(run_dir_value).expanduser() if run_dir_value else None
    if run_dir is not None and run_dir.is_dir():
        (run_dir / "tool-results").mkdir(parents=True, exist_ok=True)
        _append_bind_path(args, run_dir, read_only=False)
    endpoint_path = minion_role_socket_path(runtime_root)
    if not endpoint_path.exists():
        raise RuntimeError(
            "sandboxed Minion requires the assignment-scoped Unix role gateway"
        )
    _append_bind_path(args, endpoint_path, read_only=True)


def _append_bind_path(args: list[str], path: Path, *, read_only: bool) -> None:
    path = Path(path).expanduser()
    _append_dir_scaffold(args, path if path.is_dir() else path.parent)
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
    return tuple(
        Path(prefix) / command
        for prefix in ("/usr/bin", "/bin", "/usr/local/bin", "/usr/sbin", "/sbin")
    )


def _git_entrypoint_targets() -> tuple[Path, ...]:
    candidates = [*_command_targets("git")]
    for root in (Path("/usr/lib/git-core"), Path("/usr/libexec/git-core")):
        candidate = root / "git"
        if candidate.exists():
            candidates.append(candidate)
    return tuple(dict.fromkeys(candidates))


def _git_internal_targets() -> tuple[Path, ...]:
    targets: list[Path] = []
    for root in (Path("/usr/lib/git-core"), Path("/usr/libexec/git-core")):
        if not root.is_dir():
            continue
        targets.extend(
            path
            for path in root.glob("git-*")
            if path.is_file() and os.access(path, os.X_OK)
        )
    return tuple(dict.fromkeys(targets))


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


def _git_wrapper_text() -> str:
    shim = shlex.quote(str(Path(__file__).with_name("git_shim.py")))
    return (
        "#!/bin/sh\n"
        f"exec /usr/bin/python3 {shim} \"$@\"\n"
    )


def _git_internal_wrapper_text() -> str:
    return (
        "#!/bin/sh\n"
        "echo \"blocked internal Git entry point. Run a classified read-only git subcommand through run_shell.\" >&2\n"
        "exit 126\n"
    )
