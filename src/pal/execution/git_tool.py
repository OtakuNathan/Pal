from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from pal.execution.contracts import CapabilityResult
from pal.shared import RuntimeStatus


GIT_TOOL_DESCRIPTION = (
    "Run git through Pal's structured git wrapper instead of shell. Use this for repository status, diffs, history, "
    "changed-file evidence, and conservative audited git mutations. Dangerous history or destructive operations are refused."
)

GIT_TOOL_CMD_DESCRIPTION = (
    "Git command without shell syntax, for example `status --short`, `diff -- src/app.py`, "
    "`log --oneline -5`, `restore -- path.py`, or `revert --no-commit HEAD`. "
    "An optional leading `git` is accepted."
)

_SHELL_SYNTAX_RE = re.compile(r"&&|\|\||[;|<>`]|[$][(]")
_DISALLOWED_GLOBAL_OPTIONS = {
    "-C",
    "-c",
    "--exec-path",
    "--git-dir",
    "--namespace",
    "--super-prefix",
    "--work-tree",
}
_READ_ONLY_SUBCOMMANDS = {
    "annotate",
    "archive",
    "blame",
    "cat-file",
    "check-attr",
    "check-ignore",
    "check-mailmap",
    "check-ref-format",
    "cherry",
    "count-objects",
    "describe",
    "diff",
    "diff-files",
    "diff-index",
    "diff-tree",
    "for-each-ref",
    "fsck",
    "get-tar-commit-id",
    "grep",
    "log",
    "ls-files",
    "ls-tree",
    "merge-base",
    "name-rev",
    "patch-id",
    "range-diff",
    "rev-list",
    "rev-parse",
    "shortlog",
    "show",
    "show-branch",
    "show-index",
    "show-ref",
    "status",
    "verify-pack",
    "verify-commit",
    "verify-tag",
    "var",
    "version",
    "whatchanged",
}
_ALLOWED_MUTATION_SUBCOMMANDS = {"restore", "revert"}
_REJECTED_SUBCOMMANDS = {
    "add",
    "am",
    "apply",
    "bisect",
    "checkout",
    "cherry-pick",
    "clean",
    "clone",
    "commit",
    "config",
    "fetch",
    "filter-branch",
    "gc",
    "init",
    "merge",
    "mv",
    "pull",
    "push",
    "rebase",
    "reset",
    "rm",
    "stash",
    "submodule",
    "switch",
    "tag",
    "update-index",
    "update-ref",
    "worktree",
}
_DANGEROUS_FLAGS = {
    "-f",
    "--force",
    "--hard",
    "--prune",
}
_READ_ESCAPE_OR_EFFECT_FLAGS = {
    "-o",
    "--add-file",
    "--add-virtual-file",
    "--contents",
    "--exec",
    "--ext-diff",
    "--filters",
    "--in-place",
    "--lost-found",
    "--no-index",
    "--open-files-in-pager",
    "--output",
    "--remote",
    "--textconv",
}
_READ_COMMANDS_WITH_DIFF_DRIVERS = {
    "diff",
    "diff-files",
    "diff-index",
    "diff-tree",
    "log",
    "show",
    "whatchanged",
}
_BRANCH_READ_ONLY_OPTIONS = {
    "-a",
    "-l",
    "-r",
    "-v",
    "-vv",
    "--all",
    "--color",
    "--column",
    "--contains",
    "--format",
    "--ignore-case",
    "--list",
    "--merged",
    "--no-color",
    "--no-column",
    "--no-contains",
    "--no-merged",
    "--points-at",
    "--remotes",
    "--show-current",
    "--sort",
    "--verbose",
}
_BRANCH_MUTATING_OPTIONS = {
    "-c",
    "-C",
    "-d",
    "-D",
    "-f",
    "-m",
    "-M",
    "-t",
    "-u",
    "--copy",
    "--delete",
    "--edit-description",
    "--force",
    "--move",
    "--no-track",
    "--set-upstream-to",
    "--track",
    "--unset-upstream",
}
_TAG_READ_ONLY_OPTIONS = {
    "-l",
    "--color",
    "--column",
    "--contains",
    "--format",
    "--ignore-case",
    "--list",
    "--merged",
    "--no-color",
    "--no-column",
    "--no-contains",
    "--no-merged",
    "--points-at",
    "--sort",
    "--verify",
}
_TAG_MUTATING_OPTIONS = {
    "-a",
    "-d",
    "-F",
    "-f",
    "-m",
    "-s",
    "-u",
    "--annotate",
    "--cleanup",
    "--create-reflog",
    "--delete",
    "--file",
    "--force",
    "--local-user",
    "--message",
    "--sign",
}
_RESTORE_REJECTED_OPTIONS = {
    "--merge",
    "--ours",
    "--pathspec-from-file",
    "--patch",
    "-p",
    "--staged",
    "--theirs",
}
_RESTORE_OPTIONS_WITH_VALUE = {"--source", "-s"}
_REVERT_REJECTED_OPTIONS = {
    "--cleanup",
    "--continue",
    "--edit",
    "-e",
    "--gpg-sign",
    "-S",
    "--mainline",
    "-m",
    "--quit",
    "--signoff",
    "-s",
    "--skip",
    "--strategy",
    "--strategy-option",
    "-X",
}


@dataclass(frozen=True)
class GitCommandPolicy:
    raw: str
    tokens: tuple[str, ...]
    subcommand: str = ""
    operation_kind: str = "blocked"
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.operation_kind in {"read", "mutation"}

    @property
    def is_mutation(self) -> bool:
        return self.operation_kind == "mutation"

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "tokens": list(self.tokens),
            "subcommand": self.subcommand,
            "operation_kind": self.operation_kind,
            "reason": self.reason,
            "allowed": self.allowed,
            "is_mutation": self.is_mutation,
        }


@dataclass
class GitTool:
    default_timeout_ms: int = 30_000
    def invoke(self, args: dict[str, Any]) -> CapabilityResult:
        return self._invoke(args, budget=None)

    def _invoke(self, args: dict[str, Any], *, budget: Any = None) -> CapabilityResult:
        cmd = str(args.get("cmd") or "").strip()
        if not cmd:
            return _err(RuntimeStatus.INVALID, "cmd is required", error_code="MISSING_CMD")
        policy = classify_git_command(cmd)
        if not policy.allowed:
            text = _blocked_text(policy)
            return CapabilityResult(
                status=RuntimeStatus.FORBIDDEN,
                text=text,
                llm_text=text,
                structured={"error_code": "GIT_COMMAND_BLOCKED", "classification": policy.to_dict()},
            )

        cwd = _resolve_cwd(args.get("cwd"))
        timeout_ms = _positive_int(args.get("timeout_ms"), default=self.default_timeout_ms, minimum=1)
        before = _git_snapshot(cwd) if policy.is_mutation else {}
        completed = _run_git(
            policy.tokens,
            cwd=cwd,
            timeout_ms=timeout_ms,
            read_only=policy.operation_kind == "read",
        )
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        after = _git_snapshot(cwd) if policy.is_mutation else {}
        ok = completed.returncode == 0
        display = _render_git_result(
            policy=policy,
            returncode=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            before=before,
            after=after,
        )
        structured: dict[str, Any] = {
            "cmd": cmd,
            "tokens": ["git", *policy.tokens],
            "cwd": str(cwd),
            "classification": policy.to_dict(),
            "returncode": completed.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": False,
            "stderr_truncated": False,
            "timeout_ms": timeout_ms,
        }
        if policy.is_mutation:
            structured.update(_mutation_audit_payload(policy=policy, before=before, after=after))
        return CapabilityResult(
            status=RuntimeStatus.OK if ok else RuntimeStatus.ERROR,
            text=display,
            llm_text=display,
            structured=structured,
        )

    async def ainvoke(self, args: dict[str, Any], **kwargs: Any) -> CapabilityResult:
        return self._invoke(args, budget=kwargs.get("budget"))

def classify_git_command(cmd: object) -> GitCommandPolicy:
    raw = str(cmd or "").strip()
    if not raw:
        return GitCommandPolicy(raw=raw, tokens=(), reason="cmd is required")
    if _SHELL_SYNTAX_RE.search(raw):
        return GitCommandPolicy(raw=raw, tokens=(), reason="shell syntax is not accepted by the git wrapper")
    try:
        tokens = shlex.split(raw, posix=(os.name != "nt"))
    except ValueError as exc:
        return GitCommandPolicy(raw=raw, tokens=(), reason=f"could not parse git command: {exc}")
    if tokens and Path(tokens[0]).name == "git":
        tokens = tokens[1:]
    if tokens and tokens[0] == "--no-pager":
        tokens = tokens[1:]
    if not tokens:
        return GitCommandPolicy(raw=raw, tokens=(), reason="git subcommand is required")

    blocked_global = _blocked_global_option(tokens)
    if blocked_global:
        return GitCommandPolicy(raw=raw, tokens=tuple(tokens), reason=f"global option is not allowed: {blocked_global}")

    subcommand = tokens[0]
    args = tokens[1:]
    if subcommand in _READ_ONLY_SUBCOMMANDS:
        dangerous = _dangerous_flag(args)
        if dangerous:
            return GitCommandPolicy(raw=raw, tokens=tuple(tokens), subcommand=subcommand, reason=f"dangerous flag is not allowed: {dangerous}")
        unsafe_read = _unsafe_read_flag(args)
        if unsafe_read:
            return GitCommandPolicy(
                raw=raw,
                tokens=tuple(tokens),
                subcommand=subcommand,
                reason=f"read command option may escape the repository or cause an effect: {unsafe_read}",
            )
        return GitCommandPolicy(raw=raw, tokens=tuple(tokens), subcommand=subcommand, operation_kind="read")
    if subcommand == "branch":
        return _classify_branch(raw, tokens, args)
    if subcommand == "tag":
        return _classify_tag(raw, tokens, args)
    if subcommand == "stash":
        return _classify_named_read_mode(raw, tokens, args, read_modes={"list", "show"})
    if subcommand == "reflog":
        return _classify_named_read_mode(raw, tokens, args, read_modes={"exists", "show"}, default_read=True)
    if subcommand == "worktree":
        return _classify_named_read_mode(raw, tokens, args, read_modes={"list"})
    if subcommand == "notes":
        return _classify_named_read_mode(raw, tokens, args, read_modes={"get-ref", "list", "show"}, default_read=True)
    if subcommand == "sparse-checkout":
        return _classify_named_read_mode(raw, tokens, args, read_modes={"list"})
    if subcommand == "submodule":
        return _classify_named_read_mode(raw, tokens, args, read_modes={"status", "summary"}, default_read=True)
    if subcommand == "remote":
        return _classify_remote(raw, tokens, args)
    if subcommand == "symbolic-ref":
        return _classify_symbolic_ref(raw, tokens, args)
    if subcommand in _ALLOWED_MUTATION_SUBCOMMANDS:
        if subcommand == "restore":
            return _classify_restore(raw, tokens, args)
        if subcommand == "revert":
            return _classify_revert(raw, tokens, args)
    if subcommand in _REJECTED_SUBCOMMANDS:
        return GitCommandPolicy(raw=raw, tokens=tuple(tokens), subcommand=subcommand, reason=f"git {subcommand} is not allowed")
    return GitCommandPolicy(raw=raw, tokens=tuple(tokens), subcommand=subcommand, reason=f"unsupported git subcommand: {subcommand}")


def git_command_is_mutation(cmd: object) -> bool:
    return classify_git_command(cmd).is_mutation


def _classify_branch(raw: str, tokens: list[str], args: list[str]) -> GitCommandPolicy:
    list_mode = False
    for arg in args:
        option = arg.split("=", 1)[0] if arg.startswith("--") else arg
        if option in _BRANCH_MUTATING_OPTIONS:
            return GitCommandPolicy(raw=raw, tokens=tuple(tokens), subcommand="branch", reason=f"mutating branch option is not allowed: {arg}")
        if arg.startswith("-") and option not in _BRANCH_READ_ONLY_OPTIONS:
            return GitCommandPolicy(raw=raw, tokens=tuple(tokens), subcommand="branch", reason=f"unsupported branch option: {arg}")
        if option in {
            "-l",
            "--contains",
            "--list",
            "--merged",
            "--no-contains",
            "--no-merged",
            "--points-at",
        }:
            list_mode = True
            continue
        if not arg.startswith("-") and not list_mode:
            return GitCommandPolicy(
                raw=raw,
                tokens=tuple(tokens),
                subcommand="branch",
                reason=f"positional branch argument may create or change a branch: {arg}",
            )
    return GitCommandPolicy(raw=raw, tokens=tuple(tokens), subcommand="branch", operation_kind="read")


def _classify_tag(raw: str, tokens: list[str], args: list[str]) -> GitCommandPolicy:
    list_mode = not args
    for arg in args:
        option = arg.split("=", 1)[0] if arg.startswith("--") else arg
        if option in _TAG_MUTATING_OPTIONS:
            return GitCommandPolicy(
                raw=raw,
                tokens=tuple(tokens),
                subcommand="tag",
                reason=f"mutating tag option is not allowed: {arg}",
            )
        if arg == "-n" or (arg.startswith("-n") and arg[2:].isdigit()):
            list_mode = True
            continue
        if arg.startswith("-") and option not in _TAG_READ_ONLY_OPTIONS:
            return GitCommandPolicy(
                raw=raw,
                tokens=tuple(tokens),
                subcommand="tag",
                reason=f"unsupported tag option: {arg}",
            )
        if option in _TAG_READ_ONLY_OPTIONS:
            list_mode = True
            continue
        if not list_mode:
            return GitCommandPolicy(
                raw=raw,
                tokens=tuple(tokens),
                subcommand="tag",
                reason=f"positional tag argument may create or change a tag: {arg}",
            )
    return GitCommandPolicy(raw=raw, tokens=tuple(tokens), subcommand="tag", operation_kind="read")


def _classify_named_read_mode(
    raw: str,
    tokens: list[str],
    args: list[str],
    *,
    read_modes: set[str],
    default_read: bool = False,
) -> GitCommandPolicy:
    subcommand = tokens[0]
    if not args:
        if default_read:
            return GitCommandPolicy(raw=raw, tokens=tuple(tokens), subcommand=subcommand, operation_kind="read")
        return GitCommandPolicy(
            raw=raw,
            tokens=tuple(tokens),
            subcommand=subcommand,
            reason=f"git {subcommand} requires an explicit read-only mode",
        )
    mode = next((arg for arg in args if not arg.startswith("-")), "")
    if mode not in read_modes:
        return GitCommandPolicy(
            raw=raw,
            tokens=tuple(tokens),
            subcommand=subcommand,
            reason=f"git {subcommand} mode is not read-only: {mode or args[0]}",
        )
    dangerous = _dangerous_flag(args)
    if dangerous:
        return GitCommandPolicy(
            raw=raw,
            tokens=tuple(tokens),
            subcommand=subcommand,
            reason=f"dangerous flag is not allowed: {dangerous}",
        )
    unsafe_read = _unsafe_read_flag(args)
    if unsafe_read:
        return GitCommandPolicy(
            raw=raw,
            tokens=tuple(tokens),
            subcommand=subcommand,
            reason=f"read command option may escape the repository or cause an effect: {unsafe_read}",
        )
    return GitCommandPolicy(raw=raw, tokens=tuple(tokens), subcommand=subcommand, operation_kind="read")


def _classify_remote(raw: str, tokens: list[str], args: list[str]) -> GitCommandPolicy:
    if not args or all(arg in {"-v", "--verbose"} for arg in args):
        return GitCommandPolicy(raw=raw, tokens=tuple(tokens), subcommand="remote", operation_kind="read")
    positional = [arg for arg in args if not arg.startswith("-")]
    mode = positional[0] if positional else ""
    if mode == "get-url":
        unsupported = [arg for arg in args if arg.startswith("-") and arg not in {"--all", "--push"}]
        if unsupported:
            return GitCommandPolicy(
                raw=raw,
                tokens=tuple(tokens),
                subcommand="remote",
                reason=f"unsupported remote get-url option: {unsupported[0]}",
            )
        if len(positional) != 2:
            return GitCommandPolicy(
                raw=raw,
                tokens=tuple(tokens),
                subcommand="remote",
                reason="remote get-url requires exactly one remote name",
            )
        return GitCommandPolicy(raw=raw, tokens=tuple(tokens), subcommand="remote", operation_kind="read")
    if mode == "show" and any(arg in {"-n", "--no-query"} for arg in args):
        return GitCommandPolicy(raw=raw, tokens=tuple(tokens), subcommand="remote", operation_kind="read")
    return GitCommandPolicy(
        raw=raw,
        tokens=tuple(tokens),
        subcommand="remote",
        reason=f"git remote mode may mutate the repository or contact a remote: {mode or args[0]}",
    )


def _classify_symbolic_ref(raw: str, tokens: list[str], args: list[str]) -> GitCommandPolicy:
    if any(arg in {"-d", "--delete"} for arg in args):
        return GitCommandPolicy(
            raw=raw,
            tokens=tuple(tokens),
            subcommand="symbolic-ref",
            reason="symbolic-ref deletion is not allowed",
        )
    supported_options = {"-q", "--no-recurse", "--quiet", "--recurse", "--short"}
    unsupported = [arg for arg in args if arg.startswith("-") and arg not in supported_options]
    if unsupported:
        return GitCommandPolicy(
            raw=raw,
            tokens=tuple(tokens),
            subcommand="symbolic-ref",
            reason=f"unsupported symbolic-ref option: {unsupported[0]}",
        )
    refs = [arg for arg in args if not arg.startswith("-")]
    if len(refs) != 1:
        return GitCommandPolicy(
            raw=raw,
            tokens=tuple(tokens),
            subcommand="symbolic-ref",
            reason="symbolic-ref reads require exactly one ref argument",
        )
    return GitCommandPolicy(raw=raw, tokens=tuple(tokens), subcommand="symbolic-ref", operation_kind="read")


def _classify_restore(raw: str, tokens: list[str], args: list[str]) -> GitCommandPolicy:
    if not args:
        return GitCommandPolicy(raw=raw, tokens=tuple(tokens), subcommand="restore", reason="restore requires explicit path arguments")
    index = 0
    saw_path = False
    while index < len(args):
        arg = args[index]
        if arg == "--":
            saw_path = saw_path or index + 1 < len(args)
            break
        if arg in _RESTORE_REJECTED_OPTIONS or arg.startswith("--conflict"):
            return GitCommandPolicy(raw=raw, tokens=tuple(tokens), subcommand="restore", reason=f"restore option is not allowed: {arg}")
        if arg in _RESTORE_OPTIONS_WITH_VALUE:
            if index + 1 >= len(args):
                return GitCommandPolicy(raw=raw, tokens=tuple(tokens), subcommand="restore", reason=f"restore option requires a value: {arg}")
            index += 2
            continue
        if any(arg.startswith(option + "=") for option in _RESTORE_OPTIONS_WITH_VALUE if option.startswith("--")):
            index += 1
            continue
        if arg.startswith("-"):
            return GitCommandPolicy(raw=raw, tokens=tuple(tokens), subcommand="restore", reason=f"unsupported restore option: {arg}")
        saw_path = True
        index += 1
    if not saw_path:
        return GitCommandPolicy(raw=raw, tokens=tuple(tokens), subcommand="restore", reason="restore requires explicit path arguments")
    return GitCommandPolicy(raw=raw, tokens=tuple(tokens), subcommand="restore", operation_kind="mutation")


def _classify_revert(raw: str, tokens: list[str], args: list[str]) -> GitCommandPolicy:
    if args == ["--abort"]:
        return GitCommandPolicy(raw=raw, tokens=tuple(tokens), subcommand="revert", operation_kind="mutation")
    if not args:
        return GitCommandPolicy(raw=raw, tokens=tuple(tokens), subcommand="revert", reason="revert requires --no-commit and at least one revision")
    saw_no_commit = False
    saw_revision = False
    for arg in args:
        if arg in {"--no-commit", "-n"}:
            saw_no_commit = True
            continue
        if arg in _REVERT_REJECTED_OPTIONS:
            return GitCommandPolicy(raw=raw, tokens=tuple(tokens), subcommand="revert", reason=f"revert option is not allowed: {arg}")
        if any(arg.startswith(option + "=") for option in _REVERT_REJECTED_OPTIONS if option.startswith("--")):
            return GitCommandPolicy(raw=raw, tokens=tuple(tokens), subcommand="revert", reason=f"revert option is not allowed: {arg}")
        if arg.startswith("-"):
            return GitCommandPolicy(raw=raw, tokens=tuple(tokens), subcommand="revert", reason=f"unsupported revert option: {arg}")
        saw_revision = True
    if not saw_no_commit:
        return GitCommandPolicy(raw=raw, tokens=tuple(tokens), subcommand="revert", reason="revert must use --no-commit so Pal controls checkpoint commits")
    if not saw_revision:
        return GitCommandPolicy(raw=raw, tokens=tuple(tokens), subcommand="revert", reason="revert --no-commit requires at least one revision")
    return GitCommandPolicy(raw=raw, tokens=tuple(tokens), subcommand="revert", operation_kind="mutation")


def _blocked_global_option(tokens: list[str]) -> str:
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("-") or token == "--":
            return ""
        if token in _DISALLOWED_GLOBAL_OPTIONS:
            return token
        if any(token.startswith(option + "=") for option in _DISALLOWED_GLOBAL_OPTIONS if option.startswith("--")):
            return token
        if token == "--no-pager":
            index += 1
            continue
        return ""
    return ""


def _dangerous_flag(args: list[str]) -> str:
    for arg in args:
        if arg in _DANGEROUS_FLAGS:
            return arg
        if any(arg.startswith(flag + "=") for flag in _DANGEROUS_FLAGS if flag.startswith("--")):
            return arg
    return ""


def _unsafe_read_flag(args: list[str]) -> str:
    for arg in args:
        option = arg.split("=", 1)[0] if arg.startswith("--") else arg
        if option in _READ_ESCAPE_OR_EFFECT_FLAGS:
            return arg
    return ""


def _resolve_cwd(value: object) -> Path:
    text = str(value or "").strip()
    return Path(text).expanduser().resolve() if text else Path.cwd().resolve()


def _positive_int(value: object, *, default: int, minimum: int) -> int:
    try:
        coerced = int(value) if value is not None and value != "" else default
    except (TypeError, ValueError):
        coerced = default
    return max(minimum, coerced)


def _run_git(
    tokens: tuple[str, ...],
    *,
    cwd: Path,
    timeout_ms: int,
    read_only: bool = False,
) -> subprocess.CompletedProcess[str]:
    inherited_env = dict(os.environ)
    if read_only:
        inherited_env = {
            key: value
            for key, value in inherited_env.items()
            if not key.startswith("GIT_")
        }
    env = {
        **inherited_env,
        "GIT_PAGER": "cat",
        "GIT_TERMINAL_PROMPT": "0",
        "NO_COLOR": "1",
    }
    if read_only:
        env["GIT_OPTIONAL_LOCKS"] = "0"
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_CONFIG_SYSTEM"] = os.devnull
    safe_tokens = list(tokens)
    if read_only and safe_tokens and safe_tokens[0] in _READ_COMMANDS_WITH_DIFF_DRIVERS:
        safe_tokens[1:1] = ["--no-ext-diff", "--no-textconv"]
    git_prefix = ["git", "--no-pager"]
    if read_only:
        git_prefix.extend(
            [
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=/dev/null",
            ]
        )
    return subprocess.run(
        [*git_prefix, *safe_tokens],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout_ms / 1000.0,
        check=False,
    )


def _run_git_probe(tokens: list[str], *, cwd: Path) -> str:
    try:
        completed = _run_git(tuple(tokens), cwd=cwd, timeout_ms=10_000, read_only=True)
    except Exception:
        return ""
    return (completed.stdout if completed.returncode == 0 else "").strip()


def _git_snapshot(cwd: Path) -> dict[str, Any]:
    return {
        "head": _run_git_probe(["rev-parse", "--verify", "HEAD"], cwd=cwd),
        "status": _run_git_probe(["status", "--porcelain=v1", "--untracked-files=all"], cwd=cwd),
        "changed_files": _changed_files(cwd),
        "diff_stat": _run_git_probe(["diff", "--stat"], cwd=cwd),
    }


def _changed_files(cwd: Path) -> list[str]:
    status = _run_git_probe(["status", "--porcelain=v1", "--untracked-files=all"], cwd=cwd)
    files: list[str] = []
    for line in status.splitlines():
        path = line[3:].strip() if len(line) > 3 else ""
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        if path:
            files.append(path)
    return files


def _mutation_audit_payload(*, policy: GitCommandPolicy, before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    undo_hint = "Use git restore -- <paths> to discard working-tree changes from this operation, or inspect git diff first."
    if policy.subcommand == "revert":
        undo_hint = "Use git revert --abort if a revert is in progress, or inspect git diff before restoring paths."
    return {
        "audit_id": f"git_audit_{uuid4().hex[:12]}",
        "before_head": str(before.get("head") or ""),
        "after_head": str(after.get("head") or ""),
        "before_status": str(before.get("status") or ""),
        "after_status": str(after.get("status") or ""),
        "changed_files": list(after.get("changed_files") or []),
        "diff_stat": str(after.get("diff_stat") or ""),
        "undo_hint": undo_hint,
    }


def _render_git_result(
    *,
    policy: GitCommandPolicy,
    returncode: int,
    stdout: str,
    stderr: str,
    before: dict[str, Any],
    after: dict[str, Any],
) -> str:
    heading = f"git {policy.subcommand} ({policy.operation_kind}, exit {returncode})"
    body = (stdout if returncode == 0 else stderr or stdout).strip()
    if policy.is_mutation:
        changed = ", ".join(list(after.get("changed_files") or [])) or "none"
        audit = [
            heading,
            f"before_head: {before.get('head') or '-'}",
            f"after_head: {after.get('head') or '-'}",
            f"changed_files: {changed}",
        ]
        diff_stat = str(after.get("diff_stat") or "").strip()
        if diff_stat:
            audit.append("diff_stat:\n" + diff_stat)
        if body:
            audit.append("output:\n" + body)
        return "\n".join(audit).strip()
    return f"{heading}\n{body}".strip() if body else heading


def _blocked_text(policy: GitCommandPolicy) -> str:
    reason = policy.reason or "git command is not allowed"
    return (
        f"Blocked git command: {reason}. Git commands issued through run_shell are routed through the audited git trap; "
        "use read-only inspection or conservative mutations, and use checkpoint completion tools for commits."
    )


def _err(status: str, text: str, **structured: Any) -> CapabilityResult:
    return CapabilityResult(status=status, text=text, llm_text=text, structured=structured)
