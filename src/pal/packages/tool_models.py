from typing import Literal

from pydantic import Field
from pal.execution.tool_facade import StrictToolModel


class PackageInstallInput(StrictToolModel):
    path: str = Field(description="Local .palpkg or legacy provider .whl path.")


class PackagePrepareInput(StrictToolModel):
    name: str = Field(description="Installed package or builtin plugin id.")
    kind: Literal["plugin", "provider", "builtin"] = "plugin"


class PackageStatusInput(StrictToolModel):
    job_id: str | None = Field(default=None, description="Installation job id, or omit for recent jobs and package records.")
