from typing import Literal
from pydantic import BaseModel, Field


class ChatTurn(BaseModel):
    role: Literal["user", "model"]
    content: str = Field(min_length=1, max_length=6000)


class CopilotRequest(BaseModel):
    question: str = Field(max_length=6000)
    case_id: str | None = Field(default=None, max_length=200)
    history: list[ChatTurn] = Field(default_factory=list, max_length=24)
    selected_node_id: str | None = Field(default=None, max_length=200)
