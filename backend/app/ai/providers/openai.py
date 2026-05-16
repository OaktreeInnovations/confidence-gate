from app.ai.base import Message


class OpenAIChatProvider:
    def __init__(self, api_key: str, model: str = "gpt-4o-mini") -> None:
        from openai import OpenAI
        self._client = OpenAI(api_key=api_key)
        self._model = model

    def complete(
        self,
        messages: list[Message],
        *,
        max_tokens: int = 600,
        temperature: float = 0.1,
        json_mode: bool = False,
    ) -> str:
        kwargs: dict = {
            "model": self._model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        response = self._client.chat.completions.create(**kwargs)
        return response.choices[0].message.content or ""
