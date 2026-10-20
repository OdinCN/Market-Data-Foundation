"""Strict public response base shared by the bounded R5 projections."""

from pydantic import BaseModel, ConfigDict


class DTO(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
