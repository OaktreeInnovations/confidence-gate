import json

import httpx

from app.ai.base import Message


class OllamaChatProvider:
    def __init__(self, base_url: str = "http://localhost:11434", model: str = "llama3.2") -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model

    def complete(
        self,
        messages: list[Message],
        *,
        max_tokens: int = 600,
        temperature: float = 0.1,
        json_mode: bool = False,
    ) -> str:
        payload: dict = {
            "model": self._model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        if json_mode:
            payload["format"] = "json"

        response = httpx.post(
            f"{self._base_url}/api/chat",
            json=payload,
            timeout=120,
        )
        response.raise_for_status()
        return response.json()["message"]["content"]
