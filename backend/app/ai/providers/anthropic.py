from app.ai.base import Message


class AnthropicChatProvider:
    def __init__(self, api_key: str, model: str = "claude-3-5-haiku-20241022") -> None:
        import anthropic
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def complete(
        self,
        messages: list[Message],
        *,
        max_tokens: int = 600,
        temperature: float = 0.1,
        json_mode: bool = False,
    ) -> str:
        system_parts = [m.content for m in messages if m.role == "system"]
        user_messages = [{"role": m.role, "content": m.content} for m in messages if m.role != "system"]
        system = "\n\n".join(system_parts) if system_parts else None

        kwargs: dict = {
            "model": self._model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": user_messages,
        }
        if system:
            kwargs["system"] = system

        response = self._client.messages.create(**kwargs)
        return response.content[0].text if response.content else ""
