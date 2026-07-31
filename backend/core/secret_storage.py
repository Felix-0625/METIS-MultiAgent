"""Fail-closed encryption for persisted API and agent configuration secrets.

Set ``METIS_DATA_ENCRYPTION_KEY`` to a Fernet key generated with
``Fernet.generate_key()``. Sensitive configuration is stored as an ``enc:v1``
envelope. Deployments that already have a stable ``JWT_SECRET`` derive a
domain-separated Fernet key from it, so per-user LLM credentials survive a
restart without introducing another mandatory Render secret. Without either
stable secret, new plaintext secrets are cleared before persistence.
"""

from __future__ import annotations

import copy
import base64
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping

from cryptography.fernet import Fernet, InvalidToken


logger = logging.getLogger(__name__)
ENCRYPTED_PREFIX = "enc:v1:"
ENCRYPTION_KEY_ENV = "METIS_DATA_ENCRYPTION_KEY"
FALLBACK_KEY_ENV = "JWT_SECRET"
_JWT_DERIVATION_CONTEXT = b"metis:data-encryption:v1:"
_SENSITIVE_KEY = re.compile(
    r"(?i)(?:^|[_-])(?:api[_-]?key|authorization|client[_-]?secret|credential|"
    r"database[_-]?url|dsn|jwt|pass(?:word|phrase)?|private[_-]?key|"
    r"refresh[_-]?token|secret|session[_-]?token|smtp[_-]?password|token)"
    r"(?:$|[_-])"
)


class SecretStorageError(RuntimeError):
    """Base error for unavailable or invalid encrypted configuration."""


class SecretStorageUnavailable(SecretStorageError):
    """Encrypted configuration exists but its key is unavailable."""


@dataclass(frozen=True)
class RestoredConfig:
    value: Any
    replacement: Any | None = None
    migrated: bool = False
    secrets_cleared: bool = False


def _is_sensitive_key(value: Any) -> bool:
    key = str(value or "")
    collapsed = re.sub(r"[^a-z0-9]", "", key.casefold())
    return bool(_SENSITIVE_KEY.search(key)) or any(
        marker in collapsed
        for marker in (
            "accesstoken",
            "apikey",
            "authorization",
            "clientsecret",
            "credential",
            "databaseurl",
            "jwtsecret",
            "password",
            "privatekey",
            "refreshtoken",
            "sessiontoken",
            "smtppassword",
        )
    )


def _has_secret(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _is_sensitive_key(key) and item not in (None, "", False, [], {}):
                return True
            if _has_secret(item):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_has_secret(item) for item in value)
    return False


def clear_sensitive_values(value: Any) -> Any:
    """Return a deep copy with credential-bearing fields emptied."""
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if _is_sensitive_key(key):
                result[key] = ""
            else:
                result[key] = clear_sensitive_values(item)
        return result
    if isinstance(value, (list, tuple)):
        return [clear_sensitive_values(item) for item in value]
    return copy.deepcopy(value)


def _fernet() -> Fernet | None:
    raw = os.getenv(ENCRYPTION_KEY_ENV, "").strip()
    if raw:
        try:
            return Fernet(raw.encode("ascii"))
        except (TypeError, ValueError) as exc:
            logger.error("%s is not a valid Fernet key", ENCRYPTION_KEY_ENV)
            raise SecretStorageError(f"{ENCRYPTION_KEY_ENV} is not a valid Fernet key") from exc

    jwt_secret = os.getenv(FALLBACK_KEY_ENV, "").strip()
    if not jwt_secret:
        return None
    derived = hashlib.sha256(_JWT_DERIVATION_CONTEXT + jwt_secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


def protect_config(value: Any, *, context: str = "configuration") -> Any:
    """Encrypt a secret-bearing JSON value or clear secrets if no key exists."""
    if not _has_secret(value):
        return copy.deepcopy(value)
    fernet = _fernet()
    if fernet is None:
        logger.warning(
            "Sensitive %s values were not persisted because %s is unavailable",
            context,
            ENCRYPTION_KEY_ENV,
        )
        return clear_sensitive_values(value)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return ENCRYPTED_PREFIX + fernet.encrypt(encoded).decode("ascii")


def restore_config(value: Any, *, context: str = "configuration") -> RestoredConfig:
    """Decrypt an envelope and produce a replacement for legacy plaintext.

    A legacy plaintext object is immediately converted to encrypted storage
    when a key exists, or to a secret-cleared object when it does not.
    """
    if isinstance(value, str) and value.startswith(ENCRYPTED_PREFIX):
        fernet = _fernet()
        if fernet is None:
            logger.error(
                "Encrypted %s cannot be loaded because %s is unavailable",
                context,
                ENCRYPTION_KEY_ENV,
            )
            raise SecretStorageUnavailable(
                f"encrypted {context} cannot be loaded without {ENCRYPTION_KEY_ENV}"
            )
        try:
            plaintext = fernet.decrypt(value[len(ENCRYPTED_PREFIX):].encode("ascii"))
            decoded = json.loads(plaintext.decode("utf-8"))
        except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
            logger.error("Encrypted %s is invalid or was encrypted with another key", context)
            raise SecretStorageError(f"encrypted {context} is invalid") from exc
        if not isinstance(decoded, (dict, list)):
            logger.error("Encrypted %s payload has an invalid type", context)
            return RestoredConfig(value={})
        return RestoredConfig(value=decoded)

    if _has_secret(value):
        replacement = protect_config(value, context=context)
        cleared = not isinstance(replacement, str)
        return RestoredConfig(
            value=clear_sensitive_values(value) if cleared else copy.deepcopy(value),
            replacement=replacement,
            migrated=True,
            secrets_cleared=cleared,
        )
    return RestoredConfig(value=copy.deepcopy(value))
