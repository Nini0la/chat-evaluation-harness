import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ShadowSlot = Literal["red", "green", "blue"]


class ShadowSelection(BaseModel):
    slot: ShadowSlot
    model_deployment_id: int


class SessionCreate(BaseModel):
    anonymous_tester_id: str | None = Field(default=None, max_length=255)
    client_metadata: dict | None = None
    shadows: list[ShadowSelection] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def unique_shadows(self):
        slots = [shadow.slot for shadow in self.shadows]
        deployments = [shadow.model_deployment_id for shadow in self.shadows]
        if len(slots) != len(set(slots)):
            raise ValueError("Shadow slots must be unique")
        if len(deployments) != len(set(deployments)):
            raise ValueError("Shadow deployments must be unique")
        return self


class SessionShadowRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    slot: ShadowSlot
    model_deployment_id: int


class SessionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    model_deployment_id: int
    anonymous_tester_id: str | None
    client_metadata: dict | None
    created_at: datetime
    shadows: list[SessionShadowRead]


class MessageCreate(BaseModel):
    message: str = Field(min_length=1, max_length=20000)


class ShadowResponseRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slot: ShadowSlot
    model_deployment_id: int
    assistant_response: str | None
    status: str
    error_type: str | None
    error_message: str | None
    input_tokens: int | None
    output_tokens: int | None
    time_to_first_token_ms: float | None
    inference_latency_ms: float | None
    total_latency_ms: float | None
    tokens_per_second: float | None
    provider_request_id: str | None
    request_started_at: datetime
    inference_started_at: datetime | None
    first_token_at: datetime | None
    response_completed_at: datetime | None


class TurnRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    session_id: uuid.UUID
    turn_number: int
    user_message: str
    assistant_response: str | None
    status: str
    error_type: str | None
    error_message: str | None
    input_tokens: int | None
    output_tokens: int | None
    time_to_first_token_ms: float | None
    inference_latency_ms: float | None
    total_latency_ms: float | None
    tokens_per_second: float | None
    provider_request_id: str | None
    model_deployment_id: int
    created_at: datetime
    shadow_responses: list[ShadowResponseRead]


class FeedbackCreate(BaseModel):
    rating: Literal[-1, 1] | None = None
    failure_category: str | None = Field(default=None, max_length=100)
    comment: str | None = Field(default=None, max_length=5000)


class FeedbackRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    turn_id: uuid.UUID
    rating: int | None
    failure_category: str | None
    comment: str | None
    created_at: datetime


class EvalCandidateCreate(BaseModel):
    source: Literal["primary", "red", "green", "blue"] = "primary"
    note: str | None = Field(default=None, max_length=5000)


class EvalCandidateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    turn_id: uuid.UUID
    source: Literal["primary", "red", "green", "blue"]
    note: str | None
    created_at: datetime


class DeploymentCreate(BaseModel):
    provider: str = Field(max_length=50)
    model_id: str = Field(max_length=255)
    model_version: str = Field(max_length=255)
    endpoint_reference: str | None = Field(default=None, max_length=1000)
    configuration_json: dict = Field(default_factory=dict)
    activate: bool = False


class DeploymentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    provider: str
    model_id: str
    model_version: str
    endpoint_reference: str | None
    configuration_json: dict
    active: bool
    created_at: datetime


class DeploymentOptionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    provider: str
    model_id: str
    model_version: str
    active: bool
