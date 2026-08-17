from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from pal.bunshin.v2.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.bunshin.v2.paths import bunshin_data_root


class DeliveryReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["3"] = "3"
    kind: Literal["patch"] = "patch"
    base_commit_sha: str
    commit_sha: str
    commit_ref: str
    patch_path: str
    patch_content_sha256: str
    patch_ref: dict[str, Any]
    apply_mode: Literal["git_am", "git_apply"]
    apply_hint: str
    verification_ref: dict[str, Any]


@dataclass
class DeliveryService:
    runtime_root: Path
    artifacts: ContentAddressedArtifactStore

    def publish(
        self,
        *,
        workflow_id: str,
        workflow_key: str,
        task_title: str,
        repository: Path,
        commit_sha: str,
        source_snapshot: Mapping[str, Any],
        verification_ref: ArtifactRef,
    ) -> ArtifactRef:
        if _git(repository, "rev-parse", f"{commit_sha}^{{commit}}").strip() != commit_sha:
            raise ValueError("delivery commit is unavailable in the sink module repository")
        base_commit_sha = str(
            source_snapshot.get("snapshot_commit_sha")
            or source_snapshot.get("original_head")
            or ""
        ).strip()
        if not base_commit_sha:
            raise ValueError("delivery source snapshot has no base commit")
        if _git(repository, "rev-parse", f"{base_commit_sha}^{{commit}}").strip() != base_commit_sha:
            raise ValueError("delivery base commit is unavailable in the sink module repository")
        if not _is_ancestor(repository, base_commit_sha, commit_sha):
            raise ValueError("verified delivery commit does not preserve source snapshot ancestry")

        commit_ref = _delivery_commit_ref(workflow_id)
        apply_mode = _patch_apply_mode(
            source_snapshot=source_snapshot,
            base_commit_sha=base_commit_sha,
        )
        patch = _format_patch(
            repository=repository,
            base_commit_sha=base_commit_sha,
            commit_sha=commit_sha,
        )
        _verify_patch_reconstructs_tree(
            repository=repository,
            base_commit_sha=base_commit_sha,
            commit_sha=commit_sha,
            patch=patch,
            apply_mode=apply_mode,
        )
        _pin_delivery_ref(repository, commit_ref, commit_sha)
        patch_ref = self.artifacts.put_bytes(
            patch,
            artifact_type="GitFormatPatchArtifact",
            schema_version="1",
            media_type="application/vnd.git-format-patch",
            provenance={
                "owner": "manager",
                "workflow_id": workflow_id,
                "base_commit_sha": base_commit_sha,
                "commit_sha": commit_sha,
                "commit_ref": commit_ref,
            },
            child_refs=((verification_ref.sha256, "sink_verification"),),
        )
        patch_path = self._publish_patch_file(
            workflow_id=workflow_id,
            workflow_key=workflow_key,
            task_title=task_title,
            commit_sha=commit_sha,
            patch=patch,
        )
        receipt = DeliveryReceipt(
            base_commit_sha=base_commit_sha,
            commit_sha=commit_sha,
            commit_ref=commit_ref,
            patch_path=str(patch_path),
            patch_content_sha256=hashlib.sha256(patch).hexdigest(),
            patch_ref=patch_ref.to_dict(),
            apply_mode=apply_mode,
            apply_hint=_patch_apply_hint(apply_mode, patch_path.name),
            verification_ref=verification_ref.to_dict(),
        )
        return self._store(receipt, verification_ref, patch_ref)

    def _publish_patch_file(
        self,
        *,
        workflow_id: str,
        workflow_key: str,
        task_title: str,
        commit_sha: str,
        patch: bytes,
    ) -> Path:
        component = _path_slug(task_title or workflow_key or "delivery")
        workflow_digest = hashlib.sha256(workflow_id.encode("utf-8")).hexdigest()
        patch_digest = hashlib.sha256(patch).hexdigest()
        destination_root = (
            bunshin_data_root(self.runtime_root)
            / "deliveries"
            / f"{component}-{workflow_digest}"
        )
        destination_root.mkdir(parents=True, exist_ok=True)
        destination = destination_root / f"result-{commit_sha[:12]}-{patch_digest}.patch"
        if destination.exists() or destination.is_symlink():
            if (
                destination.is_file()
                and not destination.is_symlink()
                and destination.read_bytes() == patch
            ):
                return destination
            raise FileExistsError(
                f"patch projection already exists with different content: {destination}"
            )
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(patch)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError:
                if (
                    destination.is_file()
                    and not destination.is_symlink()
                    and destination.read_bytes() == patch
                ):
                    return destination
                raise FileExistsError(
                    f"patch projection already exists with different content: {destination}"
                ) from None
            _fsync_directory(destination_root)
        finally:
            if temporary.exists():
                temporary.unlink()
        return destination

    def _store(
        self,
        receipt: DeliveryReceipt,
        verification_ref: ArtifactRef,
        patch_ref: ArtifactRef,
    ) -> ArtifactRef:
        return self.artifacts.put_json(
            receipt.model_dump(mode="json"),
            artifact_type="DeliveryReceiptArtifact",
            schema_version="3",
            provenance={"owner": "manager", "kind": receipt.kind},
            child_refs=(
                (patch_ref.sha256, "format_patch"),
                (verification_ref.sha256, "sink_verification"),
            ),
        )


def _format_patch(
    *,
    repository: Path,
    base_commit_sha: str,
    commit_sha: str,
) -> bytes:
    result_tree = _git(repository, "rev-parse", f"{commit_sha}^{{tree}}").strip()
    commit_date = _git(repository, "show", "-s", "--format=%cI", commit_sha).strip()
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_AUTHOR_NAME": "Pal Bunshin",
            "GIT_AUTHOR_EMAIL": "bunshin@localhost",
            "GIT_COMMITTER_NAME": "Pal Bunshin",
            "GIT_COMMITTER_EMAIL": "bunshin@localhost",
            "GIT_AUTHOR_DATE": commit_date,
            "GIT_COMMITTER_DATE": commit_date,
        }
    )
    squashed = subprocess.run(
        ["git", "commit-tree", result_tree, "-p", base_commit_sha],
        cwd=repository,
        env=environment,
        input=(
            "Bunshin verified delivery\n\n"
            f"Verified-Commit: {commit_sha}\n"
            f"Base-Commit: {base_commit_sha}\n"
        ).encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if squashed.returncode != 0 or not squashed.stdout.strip():
        raise RuntimeError(
            (squashed.stderr or b"failed to create squashed delivery commit")
            .decode("utf-8", errors="replace")
            .strip()
        )
    squashed_commit = squashed.stdout.decode("ascii").strip()
    completed = subprocess.run(
        [
            "git",
            "format-patch",
            "--stdout",
            "--binary",
            "--full-index",
            "-1",
            squashed_commit,
        ],
        cwd=repository,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            (completed.stderr or b"failed to generate Git format patch")
            .decode("utf-8", errors="replace")
            .strip()
        )
    return completed.stdout


def _verify_patch_reconstructs_tree(
    *,
    repository: Path,
    base_commit_sha: str,
    commit_sha: str,
    patch: bytes,
    apply_mode: Literal["git_am", "git_apply"],
) -> None:
    expected_tree = _git(repository, "rev-parse", f"{commit_sha}^{{tree}}").strip()
    base_tree = _git(repository, "rev-parse", f"{base_commit_sha}^{{tree}}").strip()
    if not patch:
        if expected_tree != base_tree:
            raise RuntimeError("empty delivery patch does not reconstruct the verified tree")
        return
    with tempfile.TemporaryDirectory(prefix="pal-bunshin-patch-check-") as temporary:
        checkout = Path(temporary) / "checkout"
        cloned = subprocess.run(
            ["git", "clone", "--no-local", "--no-checkout", str(repository), str(checkout)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if cloned.returncode != 0:
            raise RuntimeError(
                (cloned.stderr or b"failed to prepare patch verification checkout")
                .decode("utf-8", errors="replace")
                .strip()
            )
        subprocess.run(
            ["git", "checkout", "--detach", base_commit_sha],
            cwd=checkout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        command = (
            [
                "git",
                "-c",
                "user.name=Pal Bunshin",
                "-c",
                "user.email=bunshin@localhost",
                "am",
                "--3way",
                "--empty=keep",
            ]
            if apply_mode == "git_am"
            else ["git", "apply", "--binary", "--index", "--allow-empty"]
        )
        applied = subprocess.run(
            command,
            cwd=checkout,
            input=patch,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if applied.returncode != 0:
            raise RuntimeError(
                f"generated delivery patch failed its isolated {apply_mode} preflight: "
                + (applied.stderr or applied.stdout).decode("utf-8", errors="replace").strip()
            )
        actual_tree = (
            _git(checkout, "rev-parse", "HEAD^{tree}").strip()
            if apply_mode == "git_am"
            else _git(checkout, "write-tree").strip()
        )
        if actual_tree != expected_tree:
            raise RuntimeError("generated delivery patch does not reconstruct the verified tree")


def _delivery_commit_ref(workflow_id: str) -> str:
    digest = hashlib.sha256(str(workflow_id).encode("utf-8")).hexdigest()
    return f"refs/bunshin/deliveries/{digest}"


def _patch_apply_mode(
    *,
    source_snapshot: Mapping[str, Any],
    base_commit_sha: str,
) -> Literal["git_am", "git_apply"]:
    original_head = str(source_snapshot.get("original_head") or "").strip()
    if source_snapshot.get("source_clean") is True and original_head == base_commit_sha:
        return "git_am"
    return "git_apply"


def _patch_apply_hint(
    apply_mode: Literal["git_am", "git_apply"],
    file_name: str,
) -> str:
    if apply_mode == "git_am":
        return f"git am --3way --empty=keep {file_name}"
    return f"git apply --binary --allow-empty {file_name}"


def _pin_delivery_ref(repository: Path, commit_ref: str, commit_sha: str) -> None:
    created = subprocess.run(
        ["git", "update-ref", commit_ref, commit_sha, "0" * 40],
        cwd=repository,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if created.returncode == 0:
        return
    existing = subprocess.run(
        ["git", "rev-parse", "--verify", commit_ref],
        cwd=repository,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if existing.returncode == 0 and existing.stdout.strip() == commit_sha:
        return
    raise RuntimeError("delivery commit ref already points to a different result")


def _is_ancestor(repository: Path, ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=repository,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def is_github_pull_request_remote(remote_url: str) -> bool:
    """Return whether a push URL identifies a github.com repository."""

    value = remote_url.strip()
    if re.match(
        r"^git@github\.com:[^/\s]+/[^/\s]+(?:\.git)?/?$",
        value,
        re.IGNORECASE,
    ):
        return True
    return bool(
        re.match(
            r"^(?:https?|ssh|git)://(?:[^/@\s]+@)?github\.com/[^/\s]+/[^/\s]+(?:\.git)?/?$",
            value,
            re.IGNORECASE,
        )
    )


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repository,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    ).stdout


def _path_slug(value: str, *, max_length: int = 120) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    normalized = normalized.strip(".-")
    if not normalized:
        return "workflow"
    # A task title is user-controlled and can be arbitrarily long.  This
    # component is used as one directory name, so keep it comfortably below
    # NAME_MAX even after the workflow-id suffix is appended by the caller.
    normalized = normalized[:max(1, int(max_length))].rstrip(".-")
    return normalized or "workflow"


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
