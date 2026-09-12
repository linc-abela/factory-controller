"""Factory-owned Notion credential resolution for unattended AWE continuation.

Resolution order:

1. explicit token argument (interactive/debug CLI only)
2. ``NOTION_TOKEN`` or ``NOTION_API_KEY`` in the process environment
3. the Factory macOS Keychain item ``factory-controller.notion`` /
   ``awe-continuation``

This module never scavenges Cursor, ChatGPT, MCP, or other harness stores,
never writes the secret into a service manifest, argv, logs, or status, and
never uses ``security -w`` (which would place the password on a process
command line).
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import POINTER, c_char_p, c_int32, c_uint32, c_void_p
from dataclasses import dataclass
from typing import Protocol

KEYCHAIN_SERVICE = "factory-controller.notion"
KEYCHAIN_ACCOUNT = "awe-continuation"
CREDENTIAL_PROVIDER_ID = "env_then_keychain"

ERR_SEC_SUCCESS = 0
ERR_SEC_ITEM_NOT_FOUND = -25300
ERR_SEC_DUPLICATE_ITEM = -25299


class CredentialError(RuntimeError):
    """A credential operation that failed closed without exposing the secret."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class CredentialStatus:
    """Non-secret view of whether a Notion credential can be resolved."""

    configured: bool
    source: str
    code: str
    provider: str = CREDENTIAL_PROVIDER_ID
    keychain_service: str = KEYCHAIN_SERVICE
    keychain_account: str = KEYCHAIN_ACCOUNT

    def as_dict(self) -> dict[str, object]:
        return {
            "configured": self.configured,
            "source": self.source,
            "code": self.code,
            "provider": self.provider,
            "keychain_service": self.keychain_service,
            "keychain_account": self.keychain_account,
        }


@dataclass(frozen=True)
class ResolvedCredential:
    secret: str
    source: str
    code: str

    @property
    def configured(self) -> bool:
        return bool(self.secret)


class SecretStore(Protocol):
    service: str
    account: str

    def get(self) -> tuple[str, str]:
        """Return ``(secret_or_empty, status_code)``. Never logs the secret."""

    def put(self, secret: str) -> str:
        """Persist ``secret``. Return a non-secret status code."""

    def delete(self) -> str:
        """Remove this store's item only. Return a non-secret status code."""


class NullSecretStore:
    """Used on non-macOS hosts: no persistent Factory Notion credential."""

    service = KEYCHAIN_SERVICE
    account = KEYCHAIN_ACCOUNT

    def get(self) -> tuple[str, str]:
        return "", "KEYCHAIN_UNSUPPORTED"

    def put(self, secret: str) -> str:
        raise CredentialError("KEYCHAIN_UNSUPPORTED", "macOS Keychain is required")

    def delete(self) -> str:
        return "KEYCHAIN_UNSUPPORTED"


class MemorySecretStore:
    """Test double. Process-local unless the caller shares the instance."""

    def __init__(
        self,
        service: str = KEYCHAIN_SERVICE,
        account: str = KEYCHAIN_ACCOUNT,
        initial: str = "",
    ) -> None:
        self.service = service
        self.account = account
        self._secret = initial

    def get(self) -> tuple[str, str]:
        if not self._secret:
            return "", "KEYCHAIN_MISSING"
        if not self._secret.strip():
            return "", "KEYCHAIN_MALFORMED"
        return self._secret.strip(), "KEYCHAIN_PRESENT"

    def put(self, secret: str) -> str:
        if not secret or not secret.strip():
            raise CredentialError("CREDENTIAL_EMPTY")
        self._secret = secret.strip()
        return "KEYCHAIN_STORED"

    def delete(self) -> str:
        existed = bool(self._secret)
        self._secret = ""
        return "KEYCHAIN_DELETED" if existed else "KEYCHAIN_MISSING"


class DarwinKeychainStore:
    """Login-keychain generic password via Security.framework (no argv secret)."""

    def __init__(
        self,
        service: str = KEYCHAIN_SERVICE,
        account: str = KEYCHAIN_ACCOUNT,
    ) -> None:
        self.service = service
        self.account = account

    def get(self) -> tuple[str, str]:
        status, secret, item = _find(self.service, self.account)
        _release(item)
        if status == ERR_SEC_ITEM_NOT_FOUND:
            return "", "KEYCHAIN_MISSING"
        if status != ERR_SEC_SUCCESS:
            return "", "KEYCHAIN_UNREADABLE"
        if not secret.strip():
            return "", "KEYCHAIN_MALFORMED"
        return secret, "KEYCHAIN_PRESENT"

    def put(self, secret: str) -> str:
        if not secret or not secret.strip():
            raise CredentialError("CREDENTIAL_EMPTY")
        payload = secret.strip().encode("utf-8")
        status, _, item = _find(self.service, self.account)
        if status == ERR_SEC_SUCCESS and item:
            try:
                modify = _security().SecKeychainItemModifyContent
                rc = modify(item, None, c_uint32(len(payload)), payload)
            finally:
                _release(item)
            if rc != ERR_SEC_SUCCESS:
                raise CredentialError("KEYCHAIN_STORE_FAILED", f"status={rc}")
            return "KEYCHAIN_UPDATED"
        _release(item)
        add = _security().SecKeychainAddGenericPassword
        item_out = c_void_p()
        service_b = self.service.encode("utf-8")
        account_b = self.account.encode("utf-8")
        rc = add(
            None,
            c_uint32(len(service_b)),
            service_b,
            c_uint32(len(account_b)),
            account_b,
            c_uint32(len(payload)),
            payload,
            ctypes.byref(item_out),
        )
        _release(item_out)
        if rc == ERR_SEC_DUPLICATE_ITEM:
            return self.put(secret)
        if rc != ERR_SEC_SUCCESS:
            raise CredentialError("KEYCHAIN_STORE_FAILED", f"status={rc}")
        return "KEYCHAIN_STORED"

    def delete(self) -> str:
        status, _, item = _find(self.service, self.account)
        if status == ERR_SEC_ITEM_NOT_FOUND or not item:
            _release(item)
            return "KEYCHAIN_MISSING"
        try:
            rc = _security().SecKeychainItemDelete(item)
        finally:
            _release(item)
        if rc != ERR_SEC_SUCCESS:
            raise CredentialError("KEYCHAIN_DELETE_FAILED", f"status={rc}")
        return "KEYCHAIN_DELETED"


_SECURITY = None
_DEFAULT_STORE: SecretStore | None = None


def _security():
    global _SECURITY
    if _SECURITY is None:
        lib = ctypes.CDLL(
            "/System/Library/Frameworks/Security.framework/Security"
        )
        lib.SecKeychainFindGenericPassword.argtypes = [
            c_void_p,
            c_uint32,
            c_char_p,
            c_uint32,
            c_char_p,
            POINTER(c_uint32),
            POINTER(c_void_p),
            POINTER(c_void_p),
        ]
        lib.SecKeychainFindGenericPassword.restype = c_int32
        lib.SecKeychainAddGenericPassword.argtypes = [
            c_void_p,
            c_uint32,
            c_char_p,
            c_uint32,
            c_char_p,
            c_uint32,
            c_char_p,
            POINTER(c_void_p),
        ]
        lib.SecKeychainAddGenericPassword.restype = c_int32
        lib.SecKeychainItemModifyContent.argtypes = [
            c_void_p,
            c_void_p,
            c_uint32,
            c_char_p,
        ]
        lib.SecKeychainItemModifyContent.restype = c_int32
        lib.SecKeychainItemDelete.argtypes = [c_void_p]
        lib.SecKeychainItemDelete.restype = c_int32
        lib.SecKeychainItemFreeContent.argtypes = [c_void_p, c_void_p]
        lib.SecKeychainItemFreeContent.restype = c_int32
        _SECURITY = lib
    return _SECURITY


def _find(service: str, account: str) -> tuple[int, str, c_void_p]:
    service_b = service.encode("utf-8")
    account_b = account.encode("utf-8")
    length = c_uint32(0)
    data = c_void_p()
    item = c_void_p()
    status = _security().SecKeychainFindGenericPassword(
        None,
        c_uint32(len(service_b)),
        service_b,
        c_uint32(len(account_b)),
        account_b,
        ctypes.byref(length),
        ctypes.byref(data),
        ctypes.byref(item),
    )
    secret = ""
    if status == ERR_SEC_SUCCESS and data.value and length.value:
        raw = ctypes.string_at(data.value, length.value)
        secret = raw.decode("utf-8", errors="strict")
        _security().SecKeychainItemFreeContent(None, data)
    elif data.value:
        _security().SecKeychainItemFreeContent(None, data)
    return status, secret, item


def _release(item: c_void_p | None) -> None:
    if item and getattr(item, "value", None):
        try:
            cf = ctypes.CDLL(
                "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
            )
            cf.CFRelease.argtypes = [c_void_p]
            cf.CFRelease.restype = None
            cf.CFRelease(item)
        except OSError:
            pass


def default_secret_store() -> SecretStore:
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        if sys.platform == "darwin":
            _DEFAULT_STORE = DarwinKeychainStore()
        else:
            _DEFAULT_STORE = NullSecretStore()
    return _DEFAULT_STORE


def set_default_secret_store(store: SecretStore | None) -> None:
    """Test seam. Production code leaves this unset."""
    global _DEFAULT_STORE
    _DEFAULT_STORE = store


def _env_secret() -> str:
    for name in ("NOTION_TOKEN", "NOTION_API_KEY"):
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return ""


def resolve_notion_credential(
    token: str | None = None,
    *,
    store: SecretStore | None = None,
) -> ResolvedCredential:
    """Resolve a Notion credential without scavenging foreign stores."""
    if token and token.strip():
        return ResolvedCredential(token.strip(), "argument", "NOTION_CONFIGURED")
    env_token = _env_secret()
    if env_token:
        return ResolvedCredential(env_token, "env", "NOTION_CONFIGURED")
    backend = store if store is not None else default_secret_store()
    secret, keychain_code = backend.get()
    if secret:
        return ResolvedCredential(secret, "keychain", "NOTION_CONFIGURED")
    if keychain_code in {"KEYCHAIN_UNREADABLE", "KEYCHAIN_MALFORMED"}:
        return ResolvedCredential("", "none", keychain_code)
    return ResolvedCredential("", "none", "NOTION_NOT_CONFIGURED")


def credential_status(store: SecretStore | None = None) -> CredentialStatus:
    resolved = resolve_notion_credential(store=store)
    backend = store if store is not None else default_secret_store()
    return CredentialStatus(
        configured=resolved.configured,
        source=resolved.source,
        code=resolved.code,
        keychain_service=backend.service,
        keychain_account=backend.account,
    )


def store_host_credential(secret: str, store: SecretStore | None = None) -> CredentialStatus:
    """One-time setup: persist the token in the Factory Keychain item only."""
    backend = store if store is not None else default_secret_store()
    backend.put(secret)
    return credential_status(store=backend)


def manifest_credential_metadata() -> dict[str, str]:
    """Non-secret provider selector stored in the continuation service manifest."""
    return {
        "credential_provider": CREDENTIAL_PROVIDER_ID,
        "credential_keychain_service": KEYCHAIN_SERVICE,
        "credential_keychain_account": KEYCHAIN_ACCOUNT,
    }


def command_contains_secret(command: list[str], secret: str) -> bool:
    if not secret:
        return False
    return any(secret in part for part in command)


def text_contains_secret(text: str, secret: str) -> bool:
    return bool(secret) and secret in text
