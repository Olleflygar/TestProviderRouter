from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    content: str


class RouterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[ChatMessage]
    temperature: float | None = None
    max_tokens: int | None = None
    requires_tools: bool = False
    requires_streaming: bool = False
    requires_json_mode: bool = False

    @classmethod
    def from_input(cls, value: str | RouterRequest) -> RouterRequest:
        if isinstance(value, str):
            return cls(messages=[ChatMessage(role="user", content=value)])
        return value


class TokenUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


class RouterResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_name: str
    model: str
    text: str
    raw: dict[str, object] | None = None
    usage: TokenUsage | None = None
