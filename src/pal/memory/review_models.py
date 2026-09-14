from pydantic import BaseModel, ConfigDict, Field


class CommitMemoryCandidatesInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    batch_id: str = Field(description="Exact batch_id returned by /memory_review or its review card; requires the user’s final batch-submit authorization.")
