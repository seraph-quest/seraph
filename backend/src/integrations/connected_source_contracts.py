"""Strict local connected-task references, with no lifecycle or authority activation."""
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

class ConnectionReference(ContractModel):
    id: str = Field(min_length=1, max_length=256)
    revision: int = Field(ge=1)

class SourceItemRef(ContractModel):
    provider: Literal["gmail", "calendar"]
    opaque_id: str
    revision: str
    content_digest: str
    privacy: Literal["owner_private"] = "owner_private"
    expires_at: str


class ConnectedSourceSelection(ContractModel):
    connection_ref: ConnectionReference
    item_refs: list[SourceItemRef] = Field(min_length=1, max_length=10)


class ConnectedSourceTaskInput(ContractModel):
    connected_sources: list[ConnectedSourceSelection] | None = Field(default=None, max_length=3)
    acknowledge_connected_sources: Literal[True] | None = None

    @model_validator(mode="after")
    def explicit_selection(self):
        if not self.connected_sources:
            self.connected_sources = None
            self.acknowledge_connected_sources = None
            return self
        if self.acknowledge_connected_sources is not True:
            raise ValueError("Related source use requires explicit acknowledgment")
        connections = [group.connection_ref.id for group in self.connected_sources]
        refs = [(ref.provider, ref.opaque_id) for group in self.connected_sources for ref in group.item_refs]
        if len(set(connections)) != len(connections) or len(refs) > 10 or len(set(refs)) != len(refs):
            raise ValueError("Select at most three distinct connections and ten unique items")
        for group in self.connected_sources:
            if len({ref.provider for ref in group.item_refs}) != 1:
                raise ValueError("A connection selection must use one provider")
            for ref in group.item_refs:
                if not 1 <= len(ref.opaque_id) <= 256 or not 1 <= len(ref.revision) <= 128 or len(ref.content_digest) != 64 or any(c not in "0123456789abcdef" for c in ref.content_digest):
                    raise ValueError("Related source refs must be bounded exact citations")
                expiry = datetime.fromisoformat(ref.expires_at.replace("Z", "+00:00"))
                if expiry.tzinfo is None or len(ref.expires_at) > 64:
                    raise ValueError("Related source expiry requires an explicit timezone")
        return self

