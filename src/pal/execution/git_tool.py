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
from pal.shared.result_rendering import render_head_tail_preview_for_llm


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
    "blame",
    "diff",
    "grep",
    "log",
    "ls-files",
    "rev-parse",
    "show",
    "show-ref",
    "status",
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
    name: str = "op_git"
    display_name: str = "Git"
    family: str = "system"
    description: str = GIT_TOOL_DESCRIPTION
    tags: tuple[str, ...] = ("git", "repository", "diff", "history", "system")
    keywords: tuple[str, ...] = ("git", "status", "diff", "log", "show", "restore", "revert")
    default_timeout_ms: int = 30_000
    default_output_limit: int = 12_000
    args_schema: dict[str, Any] = field(default_factory=dict)
    result_schema: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.args_schema:
            self.args_schema = {
                "type": "object",
                "properties": {
                    "cmd": {"type": "string", "description": GIT_TOOL_CMD_DESCRIPTION},
                    "cwd": {"type": "string", "description": "Optional repository working directory."},
                    "timeout_ms": {"type": "integer", "minimum": 1, "description": "Optional timeout in milliseconds."},
                    "output_limit": {"type": "integer", "minimum": 256, "description": "Maximum stdout/stderr characters kept."},
                },
                "required": ["cmd"],
                "additionalProperties": False,
            }
        if not self.result_schema:
            self.result_schema = {
                "type": "object",
                "properties": {
                    "cmd": {"type": "string"},
                    "tokens": {"type": "array", "items": {"type": "string"}},
                    "cwd": {"type": "string"},
                    "classification": {"type": "object"},
                    "returncode": {"type": "integer"},
                    "stdout": {"type": "string"},
                    "stderr": {"type": "string"},
                    "changed_files": {"type": "array", "items": {"type": "string"}},
                    "audit_id": {"type": "string"},
                    "before_head": {"type": "string"},
                    "after_head": {"type": "string"},
                    "before_status": {"type": "string"},
                    "after_status": {"type": "string"},
                    "diff_stat": {"type": "string"},
                    "undo_hint": {"type": "string"},
                    "error_code": {"type": "string"},
                },
            }

    def invoke(self, args: dict[str, Any]) -> CapabilityResult:
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
        output_limit = _positive_int(args.get("output_limit"), default=self.default_output_limit, minimum=256)
        before = _git_snapshot(cwd) if policy.is_mutation else {}
        completed = _run_git(policy.tokens, cwd=cwd, timeout_ms=timeout_ms)
        stdout, stdout_truncated = _truncate(completed.stdout or "", output_limit)
        stderr, stderr_truncated = _truncate(completed.stderr or "", output_limit)
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
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
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
        _ = kwargs
        return self.invoke(args)


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
        return GitCommandPolicy(raw=raw, tokens=tuple(tokens), subcommand=subcommand, operation_kind="read")
    if subcommand == "branch":
        return _classify_branch(raw, tokens, args)
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
    for arg in args:
        if arg in _BRANCH_MUTATING_OPTIONS:
            return GitCommandPolicy(raw=raw, tokens=tuple(tokens), subcommand="branch", reason=f"mutating branch option is not allowed: {arg}")
        if arg.startswith("--") and "=" in arg and arg.split("=", 1)[0] in _BRANCH_MUTATING_OPTIONS:
            return GitCommandPolicy(raw=raw, tokens=tuple(tokens), subcommand="branch", reason=f"mutating branch option is not allowed: {arg}")
        if arg.startswith("-") and arg not in _BRANCH_READ_ONLY_OPTIONS and not arg.startswith("--format="):
            return GitCommandPolicy(raw=raw, tokens=tuple(tokens), subcommand="branch", reason=f"unsupported branch option: {arg}")
    return GitCommandPolicy(raw=raw, tokens=tuple(tokens), subcommand="branch", operation_kind="read")


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


def _resolve_cwd(value: object) -> Path:
    text = str(value or "").strip()
    return Path(text).expanduser().resolve() if text else Path.cwd().resolve()


def _positive_int(value: object, *, default: int, minimum: int) -> int:
    try:
        coerced = int(value) if value is not None and value != "" else default
    except (TypeError, ValueError):
        coerced = default
    return max(minimum, coerced)


def _run_git(tokens: tuple[str, ...], *, cwd: Path, timeout_ms: int) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "GIT_PAGER": "cat",
        "GIT_TERMINAL_PROMPT": "0",
        "NO_COLOR": "1",
    }
    return subprocess.run(
        ["git", "--no-pager", *tokens],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout_ms / 1000.0,
        check=False,
    )


def _run_git_probe(tokens: list[str], *, cwd: Path) -> str:
    try:
        completed = _run_git(tuple(tokens), cwd=cwd, timeout_ms=10_000)
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


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    preview, _preview_size = render_head_tail_preview_for_llm(text, max_chars=limit)
    return preview, True


def _blocked_text(policy: GitCommandPolicy) -> str:
    reason = policy.reason or "git command is not allowed"
    return (
        f"Blocked git command: {reason}. Use this git tool for read-only repository inspection and conservative audited "
        "mutations only; use checkpoint completion tools for commits."
    )


def _err(status: str, text: str, **structured: Any) -> CapabilityResult:
    return CapabilityResult(status=status, text=text, llm_text=text, structured=structured)
