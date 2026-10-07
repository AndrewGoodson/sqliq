from __future__ import annotations

import base64
import hashlib
import json
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def canonical(value: BaseModel | dict) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value: BaseModel | dict) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


class Target(StrictModel):
    tenant: str
    subscription: str
    resource_group: str = Field(pattern=r"^[A-Za-z0-9_-]{1,90}$")
    server: str = Field(pattern=r"^[a-z][a-z0-9-]{0,61}[a-z0-9]$")
    database: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")

    @field_validator("tenant", "subscription")
    @classmethod
    def uuid_string(cls, value: str) -> str:
        if str(UUID(value)) != value:
            raise ValueError("UUID must be canonical lowercase")
        return value


class Policy(StrictModel):
    version: Literal[1] = 1
    live_enabled: bool = False
    target: Target
    approver_public_key: str
    identity: Literal["managed_identity", "azure_cli"] = "managed_identity"
    managed_identity_client_id: str | None = None
    max_rows: int = Field(default=100, ge=1, le=100)

    @field_validator("approver_public_key")
    @classmethod
    def key(cls, value: str) -> str:
        if len(base64.b64decode(value, validate=True)) != 32:
            raise ValueError("Expected raw Ed25519 public key, base64 encoded")
        return value

    @field_validator("managed_identity_client_id")
    @classmethod
    def client_id(cls, value: str | None) -> str | None:
        if value is not None and str(UUID(value)) != value:
            raise ValueError("Invalid client ID")
        return value


CATALOG = {
    "schema_inventory": (
        "SELECT TOP (?) s.name AS schema_name, t.name AS table_name, "
        "c.name AS column_name, ty.name AS type_name, c.max_length, c.is_nullable "
        "FROM sys.tables AS t JOIN sys.schemas AS s ON t.schema_id=s.schema_id "
        "JOIN sys.columns AS c ON c.object_id=t.object_id "
        "JOIN sys.types AS ty ON ty.user_type_id=c.user_type_id "
        "WHERE t.is_ms_shipped=0 ORDER BY s.name,t.name,c.column_id"
    ),
    "index_inventory": (
        "SELECT TOP (?) s.name AS schema_name, t.name AS table_name, "
        "i.name AS index_name, i.type_desc, i.is_unique, i.is_disabled "
        "FROM sys.indexes AS i JOIN sys.tables AS t ON t.object_id=i.object_id "
        "JOIN sys.schemas AS s ON s.schema_id=t.schema_id "
        "WHERE t.is_ms_shipped=0 ORDER BY s.name,t.name,i.index_id"
    ),
}


class Plan(StrictModel):
    version: Literal[1] = 1
    action: Literal["schema_inventory", "index_inventory"]
    target: Target
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    skills_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sql: str
    row_limit: int = Field(ge=1, le=100)
    output: Literal["metadata_to_stdout"] = "metadata_to_stdout"
    azure_reads: tuple[str, ...] = (
        "server", "database", "azureADOnlyAuthentications/Default"
    )
    timeout_seconds: Literal[10] = 10
    max_output_bytes: Literal[65536] = 65536


def make_plan(policy: Policy, action: str, skills_sha256: str) -> Plan:
    if action not in CATALOG:
        raise ValueError("Only fixed metadata catalog actions are permitted")
    return Plan(action=action, target=policy.target, policy_sha256=digest(policy),
                skills_sha256=skills_sha256, sql=CATALOG[action], row_limit=policy.max_rows)


class Approval(StrictModel):
    audience: Literal["azure-sql-read-broker/v1"]
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    nonce: str
    issued_at: int
    expires_at: int
    signature: str

    @field_validator("nonce")
    @classmethod
    def nonce_uuid(cls, value: str) -> str:
        if str(UUID(value)) != value:
            raise ValueError("Invalid nonce")
        return value

    def payload(self) -> dict:
        return self.model_dump(exclude={"signature"})
