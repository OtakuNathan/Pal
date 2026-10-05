from typing import Literal

from pydantic import Field
from pal.execution.tool_facade import StrictToolModel


class PackageInstallInput(StrictToolModel):
    path: str = Field(description="Local .palpkg or legacy provider .whl path.")
    wait_ms: int = Field(default=1000, ge=0, le=5000, description="Wait briefly for completion; a longer job returns its id and reports whether a completion event is scheduled to wake Pal for the initiating task. Does not set the job's deadline.")


class PackagePrepareInput(StrictToolModel):
    name: str = Field(description="Installed plugin, channel provider, or builtin plugin id; select the matching kind.")
    kind: Literal["plugin", "provider", "builtin"] = "plugin"
    wait_ms: int = Field(default=1000, ge=0, le=5000, description="Bounded completion wait, not a job deadline. Longer jobs return a handle and notification availability.")


class PackageStatusInput(StrictToolModel):
    job_id: str | None = Field(default=None, description="Exact job_id returned by install_package, prepare_package, or uninstall_plugin, or listed by inspect_package_status. Omit for recent jobs and package records.")
    wait_ms: int = Field(default=0, ge=0, le=5000, description="Optional bounded wait for this job before returning status; requires job_id. Use when no completion notification is available, not for repeated short polling.")


class PluginUninstallInput(StrictToolModel):
    name: str = Field(description="Exact installed third-party plugin id from list_plugins.")
    purge_data: bool = Field(default=False, description="Also delete declared plugin-owned data and retained configuration. Requires [uninstall] data_paths in the plugin manifest; absent declarations reject purge before detach.")
    wait_ms: int = Field(default=1000, ge=0, le=5000, description="Bounded completion wait, not a job deadline. Longer jobs return a handle and notification availability.")
