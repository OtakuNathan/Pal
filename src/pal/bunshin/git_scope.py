from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Mapping

from pal.execution.git_tool import GitCommandPolicy, ScopedGitReadPlan


_READ_SUBCOMMANDS = frozenset(
    {
        "status",
        "diff",
        "log",
        "rev-list",
        "show",
        "blame",
        "branch",
        "grep",
        "ls-files",
        "rev-parse",
    }
)
_SAFE_REV_PARSE = frozenset(
    {"HEAD", "--show-toplevel", "--is-inside-work-tree", "--show-prefix", "--show-cdup"}
)
_BLAME_VALUE_OPTIONS = frozenset(
    {"-L", "--contents", "--ignore-rev", "--ignore-revs-file", "-S"}
)
_GREP_VALUE_OPTIONS = frozenset(
    {
        "-e",
        "--regexp",
        "-f",
        "--file",
        "-A",
        "--after-context",
        "-B",
        "--before-context",
        "-C",
        "--context",
        "-m",
        "--max-count",
        "--threads",
    }
)
_STATUS_VALUE_OPTIONS = frozenset(
    {"--untracked-files", "--ignore-submodules", "--column"}
)
_LS_FILES_VALUE_OPTIONS = frozenset(
    {"-x", "-X", "--exclude", "--exclude-from", "--exclude-per-directory", "--format"}
)
_HISTORY_VALUE_OPTIONS = frozenset(
    {"-n", "--max-count", "--format", "--pretty", "--since", "--until", "--after",
     "--before", "--author", "--committer", "--grep", "--date", "--encoding"}
)
_DIFF_VALUE_OPTIONS = frozenset(
    {"-S", "-G", "-I", "--ignore-matching-lines", "--find-object", "--diff-filter",
     "--word-diff-regex", "--src-prefix", "--dst-prefix", "--line-prefix",
     "--inter-hunk-context", "--anchored", "--rotate-to", "--skip-to"}
)


@dataclass(frozen=True, order=True)
class _PathScope:
    path: str
    literal: bool = False

    @property
    def operand(self) -> str:
        # Overlay names are concrete paths, even when they contain glob or
        # pathspec magic. Existing Manager glob scopes retain Git's wildmatch.
        magic = "top,literal" if self.literal or not _has_glob(self.path) else "top"
        return f":({magic}){'' if self.path == '.' else self.path}"


def scoped_role_git_read_plan(
    *,
    prompt_pack: Mapping[str, Any],
    assignment: Mapping[str, Any],
    artifact_reader: Callable[[Mapping[str, Any]], Any],
    policy: GitCommandPolicy,
    repository_root: Path,
    cwd_prefix: str = "",
) -> ScopedGitReadPlan:
    subcommand = str(policy.subcommand or "")
    if subcommand not in _READ_SUBCOMMANDS:
        raise ValueError(
            f"Git {subcommand or 'command'} is outside the assigned module read surface"
        )
    tokens = list(policy.tokens)
    args = tokens[1:]
    allowed_paths, allowed_revisions = _authenticated_git_scope(
        prompt_pack=prompt_pack,
        assignment=assignment,
        artifact_reader=artifact_reader,
    )
    if subcommand == "rev-parse":
        if not args or any(arg not in _SAFE_REV_PARSE for arg in args):
            raise ValueError("Git rev-parse is limited to HEAD and workspace identity")
        return ScopedGitReadPlan(policy, repository_root)
    if subcommand == "branch":
        if args != ["--show-current"]:
            raise ValueError("Git branch is limited to the current workspace identity")
        return ScopedGitReadPlan(policy, repository_root)
    if not allowed_paths:
        raise ValueError(
            "Git read has no Manager-authenticated module or dependency contract path scope"
        )

    before, separator, explicit_paths = _split_git_pathspec(args, command=subcommand)
    if subcommand == "blame":
        _assert_paths(
            _option_values(before, "-S", "--ignore-revs-file", value_options=_BLAME_VALUE_OPTIONS),
            allowed_paths,
            cwd_prefix=cwd_prefix,
        )
    elif subcommand == "ls-files":
        _assert_paths(
            _option_values(before, "-X", "--exclude-from", value_options=_LS_FILES_VALUE_OPTIONS),
            allowed_paths,
            cwd_prefix=cwd_prefix,
        )
        if _has_option(before, "--exclude-per-directory"):
            raise ValueError(
                "Git ls-files --exclude-per-directory is outside the assigned module read surface"
            )

    if explicit_paths:
        _assert_paths(explicit_paths, allowed_paths, cwd_prefix=cwd_prefix)
    if subcommand in {"diff", "log", "rev-list", "show"}:
        _assert_revisions(
            before,
            allowed_paths=allowed_paths,
            allowed_revisions=allowed_revisions,
            command=subcommand,
            cwd_prefix=cwd_prefix,
        )
    elif subcommand in {"status", "ls-files"}:
        positionals = _option_aware_positionals(
            before,
            value_options=(
                _STATUS_VALUE_OPTIONS
                if subcommand == "status"
                else _LS_FILES_VALUE_OPTIONS
            ),
        )
        if positionals:
            _assert_paths(positionals, allowed_paths, cwd_prefix=cwd_prefix)
            explicit_paths = positionals
    elif subcommand == "blame":
        positionals = _option_aware_positionals(
            before,
            value_options=_BLAME_VALUE_OPTIONS,
        )
        if explicit_paths:
            if len(explicit_paths) != 1:
                raise ValueError("Git blame requires exactly one assigned path")
            revisions = positionals
        else:
            if not positionals:
                raise ValueError("Git blame requires one assigned path")
            explicit_paths = positionals[-1:]
            revisions = positionals[:-1]
        _assert_paths(explicit_paths, allowed_paths, cwd_prefix=cwd_prefix)
        for revision in revisions:
            if revision not in allowed_revisions:
                raise ValueError(
                    f"Git blame revision is outside the Manager-bound Candidate range: {revision}"
                )
    elif subcommand == "grep":
        _assert_paths(
            _option_values(before, "-f", "--file", value_options=_GREP_VALUE_OPTIONS),
            allowed_paths, cwd_prefix=cwd_prefix,
        )
        explicit_pattern = _has_option(before, "-e", "--regexp", "-f", "--file")
        positionals = _option_aware_positionals(
            before,
            value_options=_GREP_VALUE_OPTIONS,
        )
        if explicit_pattern:
            _option_values(
                before, "-e", "--regexp", "-f", "--file", allow_empty=True,
                value_options=_GREP_VALUE_OPTIONS,
            )
        elif not positionals:
            raise ValueError("Git grep requires a worker-supplied pattern")
        revisions = positionals if explicit_pattern else positionals[1:]
        for revision in revisions:
            if revision not in allowed_revisions:
                raise ValueError(
                    f"Git grep revision is outside the Manager-bound Candidate range: {revision}"
                )

    if explicit_paths or subcommand == "blame":
        return ScopedGitReadPlan(policy, repository_root)
    return ScopedGitReadPlan(
        policy, repository_root, tuple(scope.operand for scope in allowed_paths),
        has_path_separator=separator,
    )


def _authenticated_git_scope(
    *,
    prompt_pack: Mapping[str, Any],
    assignment: Mapping[str, Any],
    artifact_reader: Callable[[Mapping[str, Any]], Any],
) -> tuple[list[_PathScope], set[str]]:
    input_refs = dict(assignment.get("input_refs") or {})
    views: list[Mapping[str, Any]] = []
    for name in ("module_work_view", "unit_work_view", "candidate_diff"):
        ref = input_refs.get(name)
        if not isinstance(ref, Mapping) or not ref.get("sha256"):
            continue
        try:
            value = artifact_reader(ref)
        except (OSError, ValueError, KeyError) as exc:
            raise ValueError(
                f"Manager-bound Git scope artifact {name!r} is unavailable or invalid"
            ) from exc
        if not isinstance(value, Mapping):
            raise ValueError(
                f"Manager-bound Git scope artifact {name!r} must be a mapping"
            )
        views.append(value)

    workspace = dict(prompt_pack.get("workspace") or {})
    allowed_paths: set[_PathScope] = set()
    workspace_policy = dict(workspace.get("workspace_policy") or {})
    if str(workspace_policy.get("mode") or "").strip().lower() == "read_only_repo":
        # Submission-scope reviewers are physically bound to a read-only
        # repository by the sandbox.  Their authenticated surface is the
        # complete candidate, rather than one implementation module.
        allowed_paths.add(_PathScope(".", literal=True))
    _collect_paths(workspace.get("write_path_scopes"), allowed_paths)
    _collect_paths(
        workspace.get("read_only_overlay_paths"), allowed_paths, literal=True
    )
    for view in views:
        for key in (
            "contract_paths",
            "implementation_scopes",
            "developer_tests",
            "verification_corpus",
        ):
            _collect_paths(view.get(key), allowed_paths)
        for dependency in dict(view.get("dependency_contracts") or {}).values():
            if isinstance(dependency, Mapping):
                _collect_paths(dependency.get("contract_paths"), allowed_paths)

    allowed_revisions = {"HEAD"}
    for view in views:
        for key in ("base_sha", "target_sha"):
            revision = str(view.get(key) or "").strip()
            if revision:
                allowed_revisions.add(revision)
    revisions = list(allowed_revisions)
    allowed_revisions.update(
        f"{left}{operator}{right}"
        for left in revisions
        for right in revisions
        for operator in ("..", "...")
    )
    return sorted(allowed_paths), allowed_revisions


def _split_git_pathspec(
    args: list[str], *, command: str
) -> tuple[list[str], bool, list[str]]:
    # A '--' consumed by a value-taking option is not a path delimiter.
    # History formatting accepts '=value' spellings; bare --pretty must leave
    # a following delimiter intact rather than treating it as a format value.
    value_options = {
        "grep": _GREP_VALUE_OPTIONS,
        "blame": _BLAME_VALUE_OPTIONS,
        "ls-files": _LS_FILES_VALUE_OPTIONS,
        "diff": _DIFF_VALUE_OPTIONS,
        "log": (_HISTORY_VALUE_OPTIONS - {"--pretty", "--format"}) | _DIFF_VALUE_OPTIONS,
        "show": (_HISTORY_VALUE_OPTIONS - {"--pretty", "--format"}) | _DIFF_VALUE_OPTIONS,
        "rev-list": _HISTORY_VALUE_OPTIONS - {"--pretty", "--format"},
    }.get(command, frozenset())
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            return list(args[:index]), True, list(args[index + 1 :])
        if arg in value_options:
            index += 1
            if index >= len(args):
                raise ValueError(f"Git option {arg} requires a value")
        elif arg.startswith("-") and not arg.startswith("--") and len(arg) > 2:
            if arg[:2] not in value_options and any(
                f"-{character}" in value_options for character in arg[2:]
            ):
                raise ValueError("Git clustered value-taking options must be supplied separately")
        index += 1
    return list(args), False, []


def _option_aware_positionals(
    args: list[str],
    *,
    value_options: frozenset[str],
) -> list[str]:
    positionals: list[str] = []
    skip_value = False
    for arg in args:
        if skip_value:
            skip_value = False
            continue
        option = arg.split("=", 1)[0]
        if option in value_options:
            if "=" not in arg:
                skip_value = True
            continue
        if arg.startswith("-"):
            continue
        positionals.append(arg)
    return positionals


def _has_option(args: list[str], *names: str) -> bool:
    for arg in args:
        option = arg.split("=", 1)[0]
        if option in names:
            return True
        if any(
            name.startswith("-")
            and not name.startswith("--")
            and arg.startswith(name)
            and arg != name
            for name in names
        ):
            return True
    return False


def _option_values(
    args: list[str], *names: str, allow_empty: bool = False,
    value_options: frozenset[str] = frozenset(),
) -> list[str]:
    values: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        option, separator, inline_value = arg.partition("=")
        if option in names:
            if separator:
                if not inline_value and not allow_empty:
                    raise ValueError(f"Git option {option} requires a value")
                values.append(inline_value)
            else:
                index += 1
                if index >= len(args):
                    raise ValueError(f"Git option {option} requires a value")
                values.append(args[index])
        elif option in value_options:
            if not separator:
                index += 1
        else:
            for name in names:
                if name.startswith("--") or not arg.startswith(name) or arg == name:
                    continue
                value = arg[len(name) :]
                if value.startswith("="):
                    value = value[1:]
                if not value:
                    raise ValueError(f"Git option {name} requires a value")
                values.append(value)
                break
        index += 1
    return values


def _collect_paths(
    value: Any, output: set[_PathScope], *, literal: bool = False
) -> None:
    if isinstance(value, str):
        if not _valid_relative_path(value) or (value.startswith(":") and not literal):
            raise ValueError("Manager-authenticated Git scope contains an invalid relative path")
        output.add(_PathScope(value, literal=literal))
        return
    if isinstance(value, Mapping):
        preferred = value.get("path")
        if isinstance(preferred, str):
            _collect_paths(preferred, output, literal=literal)
        else:
            for item in value.values():
                _collect_paths(item, output, literal=literal)
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            _collect_paths(item, output, literal=literal)


def _assert_paths(
    paths: list[str], allowed_paths: list[_PathScope], *, cwd_prefix: str = ""
) -> None:
    if not allowed_paths:
        raise ValueError("Git read has no Manager-authenticated path scope")
    rejected = [
        path for path in paths
        if not _path_allowed(path, allowed_paths, cwd_prefix=cwd_prefix)
    ]
    if rejected:
        raise ValueError(
            "Git path is outside the assigned module and dependency contract edges: "
            + ", ".join(rejected)
        )


def _path_allowed(
    path: str, allowed_paths: list[_PathScope], *, cwd_prefix: str = ""
) -> bool:
    if not _valid_relative_path(path) or path.startswith(":"):
        return False
    normalized = f"{cwd_prefix}/{path}" if cwd_prefix else path
    for scope in allowed_paths:
        normalized_scope = scope.path
        if normalized_scope == ".":
            return True
        if not scope.literal and _has_glob(normalized_scope):
            # Matching the spelling of one pattern against another does not
            # prove containment (e.g. data/*.txt matches data/?.txt as text).
            if normalized == normalized_scope or (
                not _has_glob(path)
                and "[" not in normalized_scope
                # Git's bracket syntax differs from Python's, and Git matches
                # '?' against bytes rather than Unicode code points. Only use
                # fnmatch for the shared bracket-free byte wildcard grammar.
                and fnmatch.fnmatchcase(normalized.encode(), normalized_scope.encode())
            ):
                return True
        elif (
            normalized == normalized_scope or normalized.startswith(normalized_scope + "/")
        ) and not (scope.literal and _has_glob(normalized_scope)):
            # The worker's raw pathspec has not opted into literal matching.
            # A concrete overlay named e.g. a* cannot authorize the pattern a*.
            return True
    return False


def _has_glob(path: str) -> bool:
    return any(character in path for character in "*?[")


def _valid_relative_path(path: str) -> bool:
    if not path or "\x00" in path or "\\" in path or PureWindowsPath(path).drive:
        return False
    if path == ".":
        return True
    # Do not silently strip whitespace, collapse components, or reinterpret
    # platform-specific separators in authenticated concrete filenames.
    return not any(part in {"", ".", ".."} for part in path.split("/"))


def _assert_revisions(
    args: list[str],
    *,
    allowed_paths: list[_PathScope],
    allowed_revisions: set[str],
    command: str,
    cwd_prefix: str = "",
) -> None:
    for arg in _revision_positionals(
        args, allowed_paths=allowed_paths, cwd_prefix=cwd_prefix
    ):
        if arg in allowed_revisions:
            continue
        if ":" in arg:
            raise ValueError("Git object:path reads are outside the assigned module surface")
        raise ValueError(
            f"Git {command} revision is outside the Manager-bound Candidate range: {arg}"
        )


def _revision_positionals(
    args: list[str],
    *,
    allowed_paths: list[_PathScope],
    cwd_prefix: str = "",
) -> list[str]:
    positionals: list[str] = []
    skip_value = False
    value_options = _HISTORY_VALUE_OPTIONS | _DIFF_VALUE_OPTIONS
    for arg in args:
        if skip_value:
            skip_value = False
            continue
        option = arg.split("=", 1)[0]
        if option in value_options and "=" not in arg:
            skip_value = True
            continue
        if arg.startswith("-") or _path_allowed(arg, allowed_paths, cwd_prefix=cwd_prefix):
            continue
        positionals.append(arg)
    return positionals


__all__ = ["scoped_role_git_read_plan"]
