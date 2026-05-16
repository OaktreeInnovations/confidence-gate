"""Local JWT auth provider — for development without Firebase.

Tokens are signed HS256 JWTs. The secret is set via LOCAL_AUTH_SECRET.
Issue a token:
    python -c "
    from jose import jwt
    print(jwt.encode({'uid': 'dev', 'email': 'dev@local'}, 'change-me-in-production', algorithm='HS256'))
    "
"""

import structlog
from jose import JWTError, jwt

logger = structlog.get_logger(__name__)

_ALGORITHM = "HS256"


class LocalAuthProvider:
    def __init__(self, secret: str) -> None:
        self._secret = secret

    async def verify_id_token(self, token: str) -> dict:
        try:
            payload = jwt.decode(token, self._secret, algorithms=[_ALGORITHM])
        except JWTError as exc:
            raise ValueError(f"Invalid local JWT: {exc}") from exc
        if "uid" not in payload:
            raise ValueError("JWT missing 'uid' claim")
        return payload
