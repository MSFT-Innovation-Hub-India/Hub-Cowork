"""Centralised credential factory.

Prefers Windows Account Manager (WAM) via `azure-identity-broker`'s
`InteractiveBrowserBrokerCredential`, falling back to the classic
`InteractiveBrowserCredential` if the broker package or its native runtime
isn't available (e.g. on macOS, on Windows without pymsalruntime, or in
CI sandboxes).

WAM uses the native Windows account picker — the same dialog Teams,
Outlook, and Office show — instead of opening a default browser. This
both avoids the "scary" surprise browser popup at startup and lets the
dialog be parented to our pywebview window via `parent_window_handle`.

The window handle is supplied lazily by the desktop host after pywebview
creates its native HWND, via `set_parent_window_handle()`. Credentials
created before the HWND is known will use the desktop window (handled
internally by `parent_window_handle=...0` semantics in azure-identity-broker
1.3+, which falls back to the foreground window).
"""

from __future__ import annotations

import ctypes
import logging
import sys
from typing import Any

from hub_cowork.core.app_paths import APP_HOME

logger = logging.getLogger("hub_se_agent")

# Per-cache authentication-record store. Lets the broker silently reuse
# the previously signed-in account across process restarts, instead of
# falling back to interactive every time.
_AUTH_RECORD_DIR = APP_HOME / "auth_records"


def _record_path(cache_name: str) -> Any:
    return _AUTH_RECORD_DIR / f"{cache_name}.json"


def _load_record(cache_name: str) -> Any | None:
    path = _record_path(cache_name)
    if not path.exists():
        return None
    try:
        from azure.identity import AuthenticationRecord
        rec = AuthenticationRecord.deserialize(path.read_text(encoding="utf-8"))
        logger.info("Auth: loaded saved record for cache=%s (account=%s)",
                    cache_name, getattr(rec, "username", "?"))
        return rec
    except Exception as ex:
        logger.warning("Auth: failed to load record for cache=%s: %s", cache_name, ex)
        return None


def _save_record(record: Any, cache_name: str) -> None:
    try:
        _AUTH_RECORD_DIR.mkdir(parents=True, exist_ok=True)
        _record_path(cache_name).write_text(record.serialize(), encoding="utf-8")
        logger.info("Auth: saved record for cache=%s", cache_name)
    except Exception as ex:
        logger.warning("Auth: failed to save record for cache=%s: %s", cache_name, ex)

# Cached HWND of the main pywebview window (set after window creation).
_parent_hwnd: int | None = None

# Probe the broker package once so we don't pay an import cost per credential.
try:  # pragma: no cover - depends on platform / extras
    from azure.identity.broker import InteractiveBrowserBrokerCredential  # type: ignore

    _BROKER_AVAILABLE = True
except Exception as ex:  # pragma: no cover
    InteractiveBrowserBrokerCredential = None  # type: ignore[assignment]
    _BROKER_AVAILABLE = False
    logger.info("WAM broker credential unavailable (%s) — falling back to "
                "InteractiveBrowserCredential", ex.__class__.__name__)


def set_parent_window_handle(hwnd: int | None) -> None:
    """Record the pywebview HWND so future broker credentials can be
    parented to the app window. Call once after `webview.start()` returns
    a usable native handle."""
    global _parent_hwnd
    _parent_hwnd = int(hwnd) if hwnd else None
    if _parent_hwnd:
        logger.info("Auth: parent window handle registered (hwnd=%s)", _parent_hwnd)


def _resolve_hwnd() -> int:
    """Return the best parent HWND we have. Prefers the registered
    pywebview window; falls back to the current foreground window so the
    WAM dialog at least appears on top of *something* during startup
    (before pywebview has a window). Returns 0 as a last resort — the
    broker treats 0 as the desktop window."""
    if _parent_hwnd:
        return _parent_hwnd
    if sys.platform == "win32":
        try:
            return int(ctypes.windll.user32.GetForegroundWindow())
        except Exception:
            return 0
    return 0


def make_credential(
    *,
    tenant_id: str | None,
    cache_name: str,
    authentication_record: Any | None = None,
    redirect_uri: str | None = None,
) -> Any:
    """Build a credential. Uses the WAM broker on Windows when available,
    otherwise falls back to `InteractiveBrowserCredential` with persistent
    cache.

    `cache_name` namespaces the persistent token cache so different
    tenants/scopes don't fight over the same blob in Windows Credential
    Manager.

    `redirect_uri` is only used by the fallback path. The broker uses the
    fixed MSA/MSAL broker redirect URI internally.
    """
    # Persistent cache options (azure-identity)
    cache_opts: Any = None
    try:
        from azure.identity import TokenCachePersistenceOptions
        cache_opts = TokenCachePersistenceOptions(name=cache_name)
    except Exception:
        cache_opts = None

    # Auto-load any saved AuthenticationRecord for this cache so the
    # broker can silently identify the account on subsequent runs.
    if authentication_record is None:
        authentication_record = _load_record(cache_name)
    have_record = authentication_record is not None

    if _BROKER_AVAILABLE and InteractiveBrowserBrokerCredential is not None:
        kwargs: dict[str, Any] = {
            "parent_window_handle": _resolve_hwnd(),
            "use_default_broker_account": True,
        }
        if tenant_id:
            kwargs["tenant_id"] = tenant_id
        if cache_opts is not None:
            kwargs["cache_persistence_options"] = cache_opts
        if authentication_record is not None:
            kwargs["authentication_record"] = authentication_record
        try:
            inner = InteractiveBrowserBrokerCredential(**kwargs)
            return _RecordPersistingCredential(inner, cache_name, have_record)
        except Exception as ex:
            logger.warning("WAM broker credential failed to construct (%s); "
                           "falling back to InteractiveBrowserCredential", ex)

    # Fallback: classic browser credential
    from azure.identity import InteractiveBrowserCredential
    fb_kwargs: dict[str, Any] = {}
    if tenant_id:
        fb_kwargs["tenant_id"] = tenant_id
    if cache_opts is not None:
        fb_kwargs["cache_persistence_options"] = cache_opts
    if authentication_record is not None:
        fb_kwargs["authentication_record"] = authentication_record
    if redirect_uri:
        fb_kwargs["redirect_uri"] = redirect_uri
    inner = InteractiveBrowserCredential(**fb_kwargs)
    return _RecordPersistingCredential(inner, cache_name, have_record)


def is_broker_available() -> bool:
    return _BROKER_AVAILABLE


class _RecordPersistingCredential:
    """Thin wrapper that persists an AuthenticationRecord to disk on the
    first successful `get_token` call, so subsequent process runs can
    silently reuse the signed-in account instead of re-prompting."""

    def __init__(self, inner: Any, cache_name: str, already_have_record: bool):
        self._inner = inner
        self._cache_name = cache_name
        self._persisted = already_have_record

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def get_token(self, *scopes: str, **kwargs: Any) -> Any:
        token = self._inner.get_token(*scopes, **kwargs)
        if not self._persisted:
            try:
                rec = self._inner.authenticate(scopes=list(scopes))
                _save_record(rec, self._cache_name)
                self._persisted = True
            except Exception as ex:
                logger.debug("Auth: could not capture record for %s: %s",
                             self._cache_name, ex)
        return token

    def get_token_info(self, *scopes: str, **kwargs: Any) -> Any:
        info = self._inner.get_token_info(*scopes, **kwargs)
        if not self._persisted:
            try:
                rec = self._inner.authenticate(scopes=list(scopes))
                _save_record(rec, self._cache_name)
                self._persisted = True
            except Exception as ex:
                logger.debug("Auth: could not capture record for %s: %s",
                             self._cache_name, ex)
        return info

    def close(self) -> None:
        try:
            self._inner.close()
        except Exception:
            pass
