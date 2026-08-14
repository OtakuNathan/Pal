from __future__ import annotations

import fnmatch
import shlex
from typing import Any, Callable, Mapping


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
    {"--exclude", "--exclude-from", "--exclude-per-directory", "--format"}
)


def scoped_role_git_read_command(
    *,
    prompt_pack: Mapping[str, Any],
    assignment: Mapping[str, Any],
    artifact_reader: Callable[[Mapping[str, Any]], Any],
    policy: Any,
) -> str:
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
        return shlex.join(tokens)
    if subcommand == "branch":
        if args != ["--show-current"]:
            raise ValueError("Git branch is limited to the current workspace identity")
        return shlex.join(tokens)
    if not allowed_paths:
        raise ValueError(
            "Git read has no Manager-authenticated module or dependency contract path scope"
        )

    if subcommand == "blame":
        _assert_paths(
            _option_values(args, "-S", "--ignore-revs-file"),
            allowed_paths,
        )
    elif subcommand == "ls-files":
        _assert_paths(
            _option_values(args, "--exclude-from"),
            allowed_paths,
        )
        if _has_option(args, "--exclude-per-directory"):
            raise ValueError(
                "Git ls-files --exclude-per-directory is outside the assigned module read surface"
            )

    before, _separator, explicit_paths = _split_git_pathspec(args)
    if explicit_paths:
        _assert_paths(explicit_paths, allowed_paths)
    if subcommand in {"diff", "log", "rev-list", "show"}:
        _assert_revisions(
            before,
            allowed_paths=allowed_paths,
            allowed_revisions=allowed_revisions,
            command=subcommand,
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
            _assert_paths(positionals, allowed_paths)
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
        _assert_paths(explicit_paths, allowed_paths)
        for revision in revisions:
            if revision not in allowed_revisions:
                raise ValueError(
                    f"Git blame revision is outside the Manager-bound Candidate range: {revision}"
                )
    elif subcommand == "grep":
        explicit_pattern = _has_option(before, "-e", "--regexp", "-f", "--file")
        positionals = _option_aware_positionals(
            before,
            value_options=_GREP_VALUE_OPTIONS,
        )
        revisions = positionals if explicit_pattern else positionals[1:]
        for revision in revisions:
            if revision not in allowed_revisions:
                raise ValueError(
                    f"Git grep revision is outside the Manager-bound Candidate range: {revision}"
                )

    if explicit_paths or subcommand == "blame":
        return shlex.join(tokens)
    return shlex.join([*tokens, "--", *allowed_paths])


def _authenticated_git_scope(
    *,
    prompt_pack: Mapping[str, Any],
    assignment: Mapping[str, Any],
    artifact_reader: Callable[[Mapping[str, Any]], Any],
) -> tuple[list[str], set[str]]:
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
    allowed_paths: set[str] = set()
    workspace_policy = dict(workspace.get("workspace_policy") or {})
    if str(workspace_policy.get("mode") or "").strip().lower() == "read_only_repo":
        # Submission-scope reviewers are physically bound to a read-only
        # repository by the sandbox.  Their authenticated surface is the
        # complete candidate, rather than one implementation module.
        allowed_paths.add(".")
    for value in (
        workspace.get("write_path_scopes"),
        workspace.get("read_only_overlay_paths"),
    ):
        _collect_paths(value, allowed_paths)
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


def _split_git_pathspec(args: list[str]) -> tuple[list[str], bool, list[str]]:
    if "--" not in args:
        return list(args), False, []
    index = args.index("--")
    return list(args[:index]), True, list(args[index + 1 :])


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


def _option_values(args: list[str], *names: str) -> list[str]:
    values: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        option, separator, inline_value = arg.partition("=")
        if option in names:
            if separator:
                if not inline_value:
                    raise ValueError(f"Git option {option} requires a value")
                values.append(inline_value)
            else:
                index += 1
                if index >= len(args):
                    raise ValueError(f"Git option {option} requires a value")
                values.append(args[index])
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


def _collect_paths(value: Any, output: set[str]) -> None:
    if isinstance(value, str):
        normalized = _normalize_path(value)
        if normalized and not normalized.startswith("/") and ".." not in normalized.split("/"):
            output.add(normalized.rstrip("/"))
        return
    if isinstance(value, Mapping):
        preferred = value.get("path")
        if isinstance(preferred, str):
            _collect_paths(preferred, output)
        else:
            for item in value.values():
                _collect_paths(item, output)
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            _collect_paths(item, output)


def _assert_paths(paths: list[str], allowed_paths: list[str]) -> None:
    if not allowed_paths:
        raise ValueError("Git read has no Manager-authenticated path scope")
    rejected = [path for path in paths if not _path_allowed(path, allowed_paths)]
    if rejected:
        raise ValueError(
            "Git path is outside the assigned module and dependency contract edges: "
            + ", ".join(rejected)
        )


def _path_allowed(path: str, allowed_paths: list[str]) -> bool:
    normalized = _normalize_path(path)
    if not normalized or normalized.startswith("/") or ".." in normalized.split("/"):
        return False
    for scope in allowed_paths:
        normalized_scope = _normalize_path(scope).rstrip("/")
        if not normalized_scope:
            continue
        if normalized_scope == ".":
            return True
        if any(character in normalized_scope for character in "*?["):
            if fnmatch.fnmatch(normalized, normalized_scope):
                return True
        elif normalized == normalized_scope or normalized.startswith(normalized_scope + "/"):
            return True
    return False


def _normalize_path(value: Any) -> str:
    normalized = str(value or "").replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _assert_revisions(
    args: list[str],
    *,
    allowed_paths: list[str],
    allowed_revisions: set[str],
    command: str,
) -> None:
    for arg in _revision_positionals(args, allowed_paths=allowed_paths):
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
    allowed_paths: list[str],
) -> list[str]:
    positionals: list[str] = []
    skip_value = False
    value_options = {"-n", "--max-count", "--format", "--pretty", "--since", "--until"}
    for arg in args:
        if skip_value:
            skip_value = False
            continue
        option = arg.split("=", 1)[0]
        if option in value_options and "=" not in arg:
            skip_value = True
            continue
        if arg.startswith("-") or _path_allowed(arg, allowed_paths):
            continue
        positionals.append(arg)
    return positionals


__all__ = ["scoped_role_git_read_command"]
