"""Strict v2 interaction grammar. Importing contracts activates no runtime."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

CAPABILITY = "browser.interact.v2"
JOB_KIND = "browser_interact_v2"
PROFILE = "httpbin.forms.v1"
ORIGIN = "https://httpbin.org"
DOCUMENT_URL = ORIGIN + "/forms/post"
SOURCE_SHA256 = "d9cd9adbe7554d4e82a597d722dccdcea70c8dd918836670e3f9687531f54d74"
MAX_DOCUMENT_BYTES = 65536
MAX_SNAPSHOT_BYTES = 32768
MAX_PREVIEW_BYTES = 16384
MAX_FIELD_BYTES = 2048
MAX_ACTIONS = 20
MAX_ORIGINS = 5
MAX_RUNTIME_SECONDS = 180
FIELDS = frozenset({"custname", "custtel", "custemail", "size", "topping", "delivery", "comments"})


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value) -> str:
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


class InteractionError(ValueError):
    def __init__(self, code: str, *, status_code: int = 409):
        self.code, self.status_code = code, status_code
        super().__init__(code)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class BrowserConnection(Strict):
    """Read-only canonical projection; no reusable credential or write scope."""
    owner: str = Field(min_length=1, max_length=128)
    site_origin: Literal["https://codeberg.org"]
    profile_ref: Literal["forgejo.issue-create.v1", "forgejo.issue-comment.v1"]
    credential_ref: str = Field(pattern=r"^credential-binding:[a-f0-9]{64}$")
    expiry: datetime
    read_scope: Literal["forgejo_private_read"]
    revision: int = Field(ge=1)


class FormTransaction(Strict):
    """Exact reviewed source/body and single submit; private literals live elsewhere."""
    profile_ref: Literal["forgejo.issue-create.v1", "forgejo.issue-comment.v1"]
    page_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    form_identity: str = Field(pattern=r"^[a-f0-9]{64}$")
    field_digests: dict[str, str] = Field(max_length=10)
    submit_node: Literal["new-issue:ordinary-primary", "comment-form:ordinary-primary"]
    expected_destination: str = Field(min_length=1, max_length=256)
    readback_contract: Literal["numeric-basic-api-full-literal.v1"]
    encoded_body_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    mutation_allowance: Literal[1] = 1

    @field_validator("mutation_allowance", mode="before")
    @classmethod
    def one_literal_mutation(cls,value):
        if type(value) is not int or value!=1:
            raise ValueError("one literal mutation required")
        return value

    @model_validator(mode="after")
    def exact_profile(self):
        names=set(self.field_digests)
        create={"title","content","ref","edit_mode","search","label_ids","milestone_id","project_id","assignee_ids"}
        allowed=(create,) if self.profile_ref=="forgejo.issue-create.v1" else ({"content"},)
        if names not in allowed or any(not re.fullmatch(r"[a-f0-9]{64}",value) for value in self.field_digests.values()):
            raise ValueError("exact Forgejo field digests required")
        expected="new-issue:ordinary-primary" if self.profile_ref=="forgejo.issue-create.v1" else "comment-form:ordinary-primary"
        if self.submit_node!=expected or type(self.mutation_allowance) is not int or self.mutation_allowance!=1:
            raise ValueError("one exact ordinary submit required")
        return self


class BrowserActionV2(Strict):
    schema_version: Literal[2] = 2
    kind: Literal["navigate", "extract", "click", "fill", "select", "check", "wait"]
    locator_ref: str | None = Field(default=None, pattern=r"^node-[a-f0-9]{32}$")
    expected_page_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    input_value_ref: str | None = Field(default=None, pattern=r"^value-[a-f0-9]{32}$")

    @field_validator("schema_version", mode="before")
    @classmethod
    def literal_version(cls, value):
        if type(value) is not int or value != 2:
            raise ValueError("the literal v2 schema is required")
        return value

    @model_validator(mode="after")
    def grammar(self):
        node_action = self.kind in {"click", "fill", "select", "check"}
        value_action = self.kind in {"fill", "select", "check"}
        if node_action != (self.locator_ref is not None):
            raise ValueError("this action's locator presence is invalid")
        if value_action != (self.input_value_ref is not None):
            raise ValueError("this action's private input reference presence is invalid")
        return self


class AccessibleNode(Strict):
    node_id: str = Field(pattern=r"^node-[a-f0-9]{32}$")
    role: str = Field(max_length=64)
    name: str = Field(max_length=256)
    actions: list[Literal["fill", "select", "check", "click"]] = Field(max_length=4)


class PageSnapshot(Strict):
    url: Literal[DOCUMENT_URL]
    origin: Literal[ORIGIN] = ORIGIN
    document_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    accessible_nodes: list[AccessibleNode] = Field(max_length=64)
    captured_at: datetime


class InteractionPrepare(Strict):
    profile_id: Literal[PROFILE]
    goal_id: str = Field(min_length=1, max_length=128)
    goal_revision: int = Field(ge=1)
    request_key: str = Field(pattern=r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")
    read_ack: StrictBool

    @model_validator(mode="after")
    def explicit_acknowledgement(self):
        if self.read_ack is not True:
            raise ValueError("explicit public document contact acknowledgement is required")
        return self


class InteractionActionRequest(Strict):
    expected_revision: int = Field(ge=1)
    fencing_token: int = Field(ge=1)
    action: BrowserActionV2
    # Transport only; persisted solely in an encrypted private artifact.
    private_input: str | bool | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def bounded_value(self):
        if self.action.kind in {"fill", "select"}:
            if type(self.private_input) is not str or len(self.private_input.encode()) > 2048:
                raise ValueError("a bounded literal private input is required")
        elif self.action.kind == "check":
            if type(self.private_input) is not bool:
                raise ValueError("a literal private boolean is required")
        elif self.private_input is not None:
            raise ValueError("private input is not accepted for this action")
        return self


class InteractionClose(Strict):
    expected_revision: int = Field(ge=1)
    fencing_token: int = Field(ge=1)


class InteractionCleanup(Strict):
    cleanup_ack: StrictBool

    @model_validator(mode="after")
    def explicit_cleanup(self):
        if self.cleanup_ack is not True:
            raise ValueError("explicit physical cleanup acknowledgement is required")
        return self
