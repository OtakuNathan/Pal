from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class DreamingInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    operation: Literal["status", "start", "resume", "report", "config", "configure", "enable", "disable"] = "status"
    run_id: str | None = Field(default=None, description="Exact run_id returned by memory_dreaming status/start or /dreaming status; omit to select the latest run.")
    dry_run: bool = False
    config: dict | None = Field(default=None, description="Partial configuration object for configure; inspect config first for supported fields. enabled controls automatic scheduling, not manual runs. Changes affect the next run.")


class MemoryHistoryInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    mem_ref: str = Field(default="", description="Exact fact:/case: reference returned by recall_memory, remember_memory, update_memory or an archive result; never invent an ID.")
    query: str = ""
    limit: int = Field(default=8, ge=1, le=50)
