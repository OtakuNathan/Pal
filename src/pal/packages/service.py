from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import tomllib
import uuid
from pathlib import Path
from typing import Any

from pal.packages.archive import PackageArtifact, digest, unpack, valid_id
from pal.packages.environment import PackageEnvironment, RECEIPT
from pal.packages.process import PackageError, atomic_json, check_cancelled, error_text, install_lock, run_command, runtime_lease


class PackageService:
    def __init__(self, runtime_root: Path, *, activation: Any = None):
        self.runtime_root = Path(runtime_root).expanduser().resolve()
        self.root = self.runtime_root / "packages"
        self.activation = activation

    def _record_path(self, kind: str, name: str) -> Path:
        if kind not in {"plugin", "provider", "builtin"}:
            raise PackageError("Unknown package kind")
        return self.root / "records" / f"{kind}-{valid_id(name)}.json"

    def status(self, *, name: str | None = None, kind: str = "plugin") -> dict:
        paths = [self._record_path(kind, name)] if name else sorted((self.root / "records").glob("*.json"))
        records = []
        errors = []
        for path in paths:
            if not path.is_file():
                continue
            try:
                record = json.loads(path.read_text())
                if not isinstance(record, dict):
                    raise ValueError("Installation record must be an object")
                records.append(record)
            except (OSError, ValueError) as exc:
                errors.append({"record": path.name, "error": error_text(exc)})
        return {"items": records, "errors": errors}

    def _save(self, record: dict, stage: str, **updates: Any) -> None:
        if updates.get("status") not in {"failed", "ready"}:
            check_cancelled()
        if "error" in updates:
            updates["error"] = error_text(updates["error"])
        record.update(stage=stage, updated_at=time.time(), **updates)
        atomic_json(self._record_path(record["kind"], record["id"]), record)

    def install(self, path: Path) -> dict:
        path = Path(path).expanduser().resolve(strict=True)
        with install_lock(self.runtime_root):
            if path.suffix == ".whl":
                return self._legacy_provider(path)
            if path.suffix != ".palpkg":
                raise PackageError("Expected a .palpkg or legacy provider .whl")
            sha = digest(path)
            artifact_root = self.root / "artifacts" / sha
            from pal.packages.archive import inspect_extracted
            if artifact_root.exists():
                try:
                    artifact = inspect_extracted(artifact_root, sha)
                except (OSError, ValueError, KeyError, PackageError):
                    # Cached extraction may have been interrupted by an older
                    # installer. Rebuild from the supplied archive on retry.
                    shutil.rmtree(artifact_root)
                    artifact = self._cache_artifact(path, artifact_root)
            else:
                artifact = self._cache_artifact(path, artifact_root)
            return self._install_artifact(artifact)

    def _cache_artifact(self, path: Path, destination: Path) -> PackageArtifact:
        from pal.packages.archive import inspect_extracted
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".unpack-", dir=destination.parent) as temporary:
            candidate = Path(temporary) / "artifact"
            artifact = unpack(path, candidate)
            if artifact.sha256 != destination.name:
                raise PackageError("Package changed while being installed; retry with a stable archive")
            os.replace(candidate, destination)
        return inspect_extracted(destination, artifact.sha256)

    def _target(self, artifact: PackageArtifact) -> Path:
        if artifact.kind == "provider":
            return self.runtime_root / "channel" / "providers" / artifact.package_id
        from pal.plugins.host import _source_plugins_root
        if ((self.runtime_root / "plugins" / "_builtin" / artifact.package_id).exists()
                or (_source_plugins_root() / artifact.package_id / "plugin.toml").is_file()):
            raise PackageError("Community package id conflicts with a built-in plugin")
        return self.runtime_root / "plugins" / "community" / artifact.package_id

    def _install_artifact(self, artifact: PackageArtifact) -> dict:
        target = self._target(artifact)
        if target.is_symlink():
            raise PackageError("Package target must not be a symlink")
        record = dict(id=artifact.package_id, kind=artifact.kind, version=artifact.manifest["version"],
                      sha256=artifact.sha256, artifact=str(artifact.root), status="running")
        environment = None
        if artifact.manifest["python"] == "venv":
            abi = f"py{sys.version_info.major}.{sys.version_info.minor}"
            base = self.root / "environments" / artifact.kind / artifact.package_id
            candidate = base / f"{artifact.sha256}-{abi}"
            # Hooks may mutate their interpreter. Never prepare in an environment
            # that an installed (or archived) sidecar can still be using.
            if candidate.exists():
                candidate = base / f"{artifact.sha256}-{abi}-{uuid.uuid4().hex[:12]}"
            environment = PackageEnvironment(candidate)
        record["environment"] = str(environment.root) if environment else None
        context = dict(runtime_root=str(self.runtime_root), package_dir=str(artifact.root),
                       target_dir=str(target), python_executable=str(environment.root / "bin/python") if environment else sys.executable)
        hooks = artifact.root / artifact.manifest["hooks"] if artifact.manifest.get("hooks") else None
        try:
            self._save(record, "check")
            self._hook(hooks, "check", context)
            self._save(record, "install")
            if environment:
                self._environment(artifact, environment)
            for stage in ("prepare", "verify"):
                self._save(record, stage)
                record[f"{stage}_result"] = self._hook(hooks, stage, context, environment=environment)
            self._save(record, "publish")
            self._publish(artifact.root / "host", target, record)
            self._save(record, "complete", status="ready")
            return record
        except Exception as exc:
            self._save(record, record.get("stage", "check"), status="failed", error=str(exc))
            raise

    def _environment(self, artifact: PackageArtifact, environment: PackageEnvironment) -> None:
        complete = environment.root / ".wheel-installed"
        if complete.is_file() and (environment.root / "bin/python").is_file():
            return
        # This path is final from creation onwards: venv scripts contain absolute shebangs.
        if environment.root.exists():
            shutil.rmtree(environment.root)
        environment.root.parent.mkdir(parents=True, exist_ok=True)
        run_command([sys.executable, "-m", "venv", str(environment.root)])
        env = environment.child_env()
        # User pip options must not redirect this install into another environment.
        for key in tuple(env):
            if key.startswith("PIP_") and key not in {"PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_CERT", "PIP_TRUSTED_HOST"}:
                env.pop(key)
        env["PIP_CONFIG_FILE"] = os.devnull
        run_command([str(environment.python_executable), "-m", "pip", "install", "--disable-pip-version-check",
                     "--no-input", str(artifact.root / artifact.manifest["wheel"])], env=env)
        run_command([str(environment.python_executable), "-m", "pip", "check"], env=env)
        complete.write_text(artifact.sha256)

    def _hook(self, file: Path | None, stage: str, context: dict,
              *, environment: PackageEnvironment | None = None, module: str | None = None) -> dict:
        if file is None and module is None:
            return {"ok": True, "skipped": True}
        with tempfile.TemporaryDirectory(prefix="pal-install-hook-") as temporary:
            workspace = Path(temporary)
            atomic_json(workspace / "context.json", context)
            python = str(environment.python_executable) if environment else sys.executable
            env = environment.child_env() if environment else dict(os.environ)
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            argv = [python, str(Path(__file__).with_name("hook_runner.py")), "--stage", stage,
                    "--context", str(workspace / "context.json"), "--result", str(workspace / "result.json")]
            if module:
                argv += ["--module", module, "--source-root", str(Path(__file__).resolve().parents[2])]
            else:
                argv += ["--file", str(file)]
            try:
                run_command(argv, env=env, cwd=workspace)
            except Exception as exc:
                result_file = workspace / "result.json"
                detail = json.loads(result_file.read_text()) if result_file.is_file() else {}
                raise PackageError(f"{stage} failed: {detail or str(exc)}") from exc
            result = json.loads((workspace / "result.json").read_text())
            if result.get("ok") is not True:
                raise PackageError(f"{stage} failed: {result}")
            return result

    def _publish(self, source: Path, target: Path, record: dict) -> None:
        if target.is_symlink() or (target.exists() and not target.is_dir()):
            raise PackageError("Package target must be a directory, not a symlink or file")
        target.parent.mkdir(parents=True, exist_ok=True)
        staging_root = self.root / "staging"
        staging_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f"{record['id']}-", dir=staging_root))
        # Even dot-directories match Path.glob('*/plugin.toml'); keep candidates
        # and previous generations completely outside the runtime discovery roots.
        archives = self.root / "previous" / record["kind"] / record["id"]
        archives.mkdir(parents=True, exist_ok=True)
        old = archives / uuid.uuid4().hex
        gate = self.activation.gate() if self.activation else runtime_lease(self.runtime_root)
        moved = False
        replaced = False
        try:
            shutil.copytree(source, staging, dirs_exist_ok=True)
            atomic_json(staging / RECEIPT, {key: record.get(key) for key in ("id", "kind", "version", "sha256", "environment")})
            with gate:
                check_cancelled()
                state = self.activation.before(record["kind"], record["id"]) if self.activation else None
                try:
                    if target.exists():
                        os.replace(target, old)
                        moved = True
                    os.replace(staging, target)
                    replaced = True
                    record["activation"] = self.activation.after(record["kind"], record["id"], state) if self.activation else "pending_rescan"
                except Exception as exc:
                    if replaced:
                        if self.activation:
                            # Activation can fail after starting some resources.
                            # Stop those through their owner before restoring files.
                            try:
                                self.activation.before(record["kind"], record["id"])
                            except Exception as cleanup:
                                raise PackageError(f"Activation failed: {exc}; new generation cleanup failed: {cleanup}; installed files retained") from exc
                        shutil.rmtree(target)
                    if moved:
                        os.replace(old, target)
                    if self.activation:
                        try:
                            self.activation.restore(record["kind"], record["id"], state)
                        except Exception as recovery:
                            raise PackageError(f"Activation failed: {exc}; restoring previous version also failed: {recovery}") from exc
                    raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def prepare(self, name: str, *, kind: str = "plugin") -> dict:
        valid_id(name)
        with install_lock(self.runtime_root):
            if kind == "builtin":
                return self._prepare_builtin(name)
            path = self._record_path(kind, name)
            if not path.is_file():
                if kind == "plugin":
                    return self._prepare_builtin(name)
                raise PackageError("Package has no installation record")
            record = json.loads(path.read_text())
            if not record.get("artifact"):
                raise PackageError("Legacy provider has no cached package artifact; reinstall its .whl with package_install")
            from pal.packages.archive import inspect_extracted
            artifact = inspect_extracted(Path(record["artifact"]), record["sha256"])
            return self._install_artifact(artifact)

    def _prepare_builtin(self, name: str) -> dict:
        from pal.plugins.host import _source_plugins_root
        manifest_path = _source_plugins_root() / name / "plugin.toml"
        if not manifest_path.is_file():
            raise PackageError("Unknown builtin package")
        manifest = tomllib.loads(manifest_path.read_text())
        installation = manifest.get("installation", {})
        module = installation.get("entrypoint")
        if module and installation.get("protocol") != "pal.install.v1":
            raise PackageError("Unsupported builtin installation protocol")
        record = dict(kind="builtin", id=name, version=manifest["version"], status="running")
        try:
            for stage in ("check", "prepare", "verify"):
                self._save(record, stage)
                record[f"{stage}_result"] = self._hook(None, stage, {"runtime_root": str(self.runtime_root)}, module=module)
            self._save(record, "complete", status="ready")
            return record
        except Exception as exc:
            self._save(record, record["stage"], status="failed", error=str(exc))
            raise

    def _legacy_provider(self, path: Path) -> dict:
        from datetime import datetime, timezone
        from pal.provider_install import inspect_provider_wheel, _extract_provider_payload, _RECEIPT_FILENAME
        wheel = inspect_provider_wheel(path)
        record = dict(kind="provider", id=valid_id(wheel.provider_id), version=wheel.provider_version,
                      sha256=wheel.wheel_sha256, status="running", environment=None)
        try:
            self._save(record, "install")
            with tempfile.TemporaryDirectory(prefix="pal-provider-") as temporary:
                source = Path(temporary)
                _extract_provider_payload(wheel, source)
                atomic_json(source / _RECEIPT_FILENAME, {
                    "schema_version": 1, "provider_id": wheel.provider_id,
                    "provider_version": wheel.provider_version, "distribution": wheel.distribution,
                    "distribution_version": wheel.distribution_version,
                    "wheel_filename": wheel.wheel_path.name, "wheel_sha256": wheel.wheel_sha256,
                    "installed_at": datetime.now(timezone.utc).isoformat(),
                })
                self._publish(source, self.runtime_root / "channel/providers" / wheel.provider_id, record)
            self._save(record, "complete", status="ready")
            return record
        except Exception as exc:
            self._save(record, record["stage"], status="failed", error=str(exc))
            raise
