from typing import Protocol, runtime_checkable


@runtime_checkable
class AuthProvider(Protocol):
    async def verify_id_token(self, token: str) -> dict: ...
