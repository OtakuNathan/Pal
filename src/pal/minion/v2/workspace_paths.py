from __future__ import annotations

from typing import Iterable


SYSTEM_VERIFICATION_CORPUS_PATH = "tests/system/verifier"
MANAGER_ARCHITECT_DIRECTORY = ".pal-minion-architect"
ARCHITECT_AUTHORING_RELATIVE_PATH = (
    f"{MANAGER_ARCHITECT_DIRECTORY}/architect.yaml"
)
_REPOSITORY_CONTROL_ROOTS = frozenset(
    {".git", ".hg", ".svn", MANAGER_ARCHITECT_DIRECTORY}
)


def repository_path_targets_control_plane(path: str) -> bool:
    """Return whether a repository-relative path names Manager/VCS state."""

    normalized = str(path or "").strip().replace("\\", "/")
    return any(
        component in _REPOSITORY_CONTROL_ROOTS
        for component in normalized.split("/")
    )


def module_test_root(module_name: str) -> str:
    normalized = str(module_name or "").strip()
    if (
        not normalized
        or normalized in {".", ".."}
        or "/" in normalized
        or "\\" in normalized
        or "\x00" in normalized
    ):
        raise ValueError(
            f"module name is not a safe repository path component: "
            f"{normalized or '<empty>'}"
        )
    return f"tests/{normalized}"


def module_developer_test_path(module_name: str) -> str:
    """Return the module Coder-owned durable test corpus path."""

    return f"{module_test_root(module_name)}/developer"


def module_verification_corpus_path(module_name: str) -> str:
    """Return the module Verifier-owned durable test corpus path."""

    return f"{module_test_root(module_name)}/verifier"


def manager_owned_test_corpus_paths(
    module_names: Iterable[str],
) -> frozenset[str]:
    """Compile every Manager-owned test corpus path for one graph generation."""

    names = tuple(dict.fromkeys(str(name) for name in module_names))
    return frozenset(
        {
            *(module_developer_test_path(name) for name in names),
            *(module_verification_corpus_path(name) for name in names),
            SYSTEM_VERIFICATION_CORPUS_PATH,
        }
    )
