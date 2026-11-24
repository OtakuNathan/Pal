"""Apply filesystem permissions to one directory in a bubblewrap namespace."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath


def append_dir_scaffold(args: list[str], path: Path) -> None:
    parts = path.expanduser().parts
    if not parts or parts[0] != "/":
        return
    current = Path("/")
    for part in parts[1:]:
        current /= part
        args.extend(["--dir", str(current)])


def append_bind_path(args: list[str], path: Path, *, read_only: bool) -> None:
    path = path.expanduser()
    append_dir_scaffold(args, path if path.is_dir() else path.parent)
    args.extend(["--ro-bind" if read_only else "--bind", str(path), str(path)])


@dataclass(frozen=True)
class ReadOnlyBind:
    """A host-provided filesystem dependency and its path in the sandbox."""

    source: Path
    target: Path
    required: bool = True


def _grant_target(root: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    if path.is_absolute() or not path.parts or ".." in path.parts or relative != path.as_posix():
        raise ValueError(f"invalid directory write grant: {relative!r}")
    target = root / path
    if target.resolve() != target:
        raise ValueError(f"write grant traverses a symbolic link: {relative}")
    return target


@dataclass(frozen=True)
class DirectoryPermissions:
    """Root mount mode and its exceptions. Exact files use an isolated editor."""

    read_only: bool
    writable_directories: tuple[str, ...] = ()
    editable_files: tuple[str, ...] = ()
    protected_paths: tuple[str, ...] = ()

    def requires_editor(self, relative: str) -> bool:
        path = PurePosixPath(relative)
        return (self.read_only and relative in self.editable_files
                and not any(path.is_relative_to(parent) for parent in self.writable_directories)
                and not any(path.is_relative_to(parent) for parent in self.protected_paths))

    def mount_into(self, args: list[str], root: Path) -> dict[str, Path]:
        """Mount the tree, then grants, then protections. Return exact editor grants."""
        root = root.expanduser().absolute()
        source = root.resolve(strict=True)
        if not source.is_dir():
            raise NotADirectoryError(source)
        protected: list[Path] = []
        for relative in self.protected_paths:
            path = Path(relative)
            target = (source / path).resolve()
            if not relative or path.is_absolute() or not target.is_relative_to(source):
                raise ValueError(f"read-only overlay escapes its root: {relative}")
            if not target.exists():
                raise RuntimeError(f"read-only overlay does not exist: {relative}")
            protected.append(target)

        directories = [_grant_target(source, relative) for relative in self.writable_directories]
        targets = {relative: _grant_target(source, relative) for relative in self.editable_files}
        append_bind_path(args, root, read_only=self.read_only)
        for directory in directories:
            if any(directory.is_relative_to(path) for path in protected):
                continue
            directory.mkdir(parents=True, exist_ok=True)
            append_bind_path(args, root / directory.relative_to(source), read_only=False)
        files: dict[str, Path] = {}
        for relative, target in targets.items():
            if any(target.is_relative_to(path) for path in protected):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if self.requires_editor(relative):
                files[relative] = target
        for path in protected:
            append_bind_path(args, root / path.relative_to(source), read_only=True)
        return files
