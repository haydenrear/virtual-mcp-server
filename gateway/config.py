from __future__ import annotations

from pathlib import Path
from typing import List
import json

from pydantic import BaseModel, Field

from .models import ClientConfig


class ClientConfigModel(BaseModel):
    server_id: str
    transport: str
    command: List[str] | None = None
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    namespace: str = ""
    tool_path_prefix: str = ""
    protocol_version: str | None = None
    refresh_interval_seconds: int = 30
    startup_timeout_seconds: int = 20
    request_timeout_seconds: int = 60
    env_header_prefix: str = "x-env-"
    include_header_names: List[str] = Field(default_factory=list)
    exclude_header_names: List[str] = Field(default_factory=list)

    def to_internal(self) -> ClientConfig:
        return ClientConfig(**self.model_dump())


class GatewayConfigModel(BaseModel):
    protocol_version: str = "2025-11-05"
    disclosure_ttl_seconds: int | None = 1800
    clients: List[ClientConfigModel]


def load_config(path: str | Path) -> GatewayConfigModel:
    raw = Path(path).read_text(encoding="utf-8")
    return GatewayConfigModel.model_validate(json.loads(raw))
