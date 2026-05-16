from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class Message:
    role: str
    content: str


@runtime_checkable
class ChatProvider(Protocol):
    def complete(
        self,
        messages: list[Message],
        *,
        max_tokens: int = 600,
        temperature: float = 0.1,
        json_mode: bool = False,
    ) -> str: ...
