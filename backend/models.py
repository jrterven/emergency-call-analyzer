from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class CreateSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["live", "file"]
    title: str | None = Field(default=None, max_length=200)


class ConnectSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sdp: str = Field(min_length=1, max_length=65_536)


class FinishSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    complete: bool
    duration_ms: int = Field(ge=0, le=3_600_000)
    usage: dict[str, Any] | None = None


class TranscriptFragment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=200)
    speaker: Literal["caller", "assistant"]
    text: str = Field(max_length=20_000)
    start_ms: int | None = Field(default=None, ge=0, le=3_600_000)
    end_ms: int | None = Field(default=None, ge=0, le=3_600_000)


class IncidentSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    incident: str | None
    location: str | None
    people_affected: str | None
    immediate_risks: list[str]
    missing_information: list[str]
    note: str | None
