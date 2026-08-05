"""Encrypted local secret storage (fallback when no external secret manager is configured)."""
import logging
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select

from app.config.settings import settings
from app.storage.database import async_session_scope
from app.storage.models import Secret

logger = logging.getLogger("app.security.secrets")

ANONCOIN_API_KEY_NAME = "anoncoin_api_key"


class SecretsManager:
    def __init__(self):
        self._fernet = Fernet(settings.secret_encryption_key.encode())
        self._cache: dict[str, str] = {}

    def _encrypt(self, raw: str) -> str:
        return self._fernet.encrypt(raw.encode()).decode()

    def _decrypt(self, token: str) -> Optional[str]:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken:
            logger.error("secret_decrypt_failed")
            return None

    async def set_secret(self, name: str, raw_value: str) -> None:
        encrypted = self._encrypt(raw_value)
        async with async_session_scope() as session:
            existing = (
                await session.execute(select(Secret).where(Secret.key_name == name))
            ).scalar_one_or_none()
            if existing:
                existing.encrypted_value = encrypted
            else:
                session.add(Secret(key_name=name, encrypted_value=encrypted))
            await session.commit()
        self._cache[name] = raw_value
        logger.info("secret_stored", extra={"key_name": name})

    async def get_secret(self, name: str) -> Optional[str]:
        if name in self._cache:
            return self._cache[name]
        async with async_session_scope() as session:
            existing = (
                await session.execute(select(Secret).where(Secret.key_name == name))
            ).scalar_one_or_none()
        if existing:
            value = self._decrypt(existing.encrypted_value)
            if value:
                self._cache[name] = value
            return value
        return None

    async def get_anoncoin_api_key(self) -> Optional[str]:
        stored = await self.get_secret(ANONCOIN_API_KEY_NAME)
        if stored:
            return stored
        return settings.anoncoin_api_key

    async def set_anoncoin_api_key(self, raw_key: str) -> None:
        await self.set_secret(ANONCOIN_API_KEY_NAME, raw_key)


secrets_manager = SecretsManager()
