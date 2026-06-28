"""Authentication config — API-key-ONLY (Phase 6).

The agent authenticates the Claude Agent SDK solely via `ANTHROPIC_API_KEY`. There is no
session-auth fallback and no interactive login: every real-model entry point calls
`ensure_api_key()` first, which loads `.env` and fails fast with a clear message if the key is
missing. The key is put into `os.environ` so the `claude` CLI subprocess the SDK spawns inherits
it and uses key-based model auth.

The key is NEVER printed or logged — error messages and the BOM-stripping below deliberately
avoid echoing the value.

Note: `.env` files created by Windows editors (Notepad/PowerShell) are often UTF-8 *with BOM*,
which makes a naive loader parse the first variable as ``\\ufeffANTHROPIC_API_KEY`` and miss the
key. We load with `encoding="utf-8-sig"` and strip a stray BOM defensively so that just works.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

_API_KEY_ENV = "ANTHROPIC_API_KEY"
_NOTION_TOKEN_ENV = "NOTION_TOKEN"
_BOM = "﻿"

_MISSING_MESSAGE = (
    "ANTHROPIC_API_KEY is not set. Add it to .env (ANTHROPIC_API_KEY=sk-ant-...) or your "
    "environment — see .env.example. The agent runs headless and will not fall back to any "
    "other authentication."
)

_MISSING_NOTION_MESSAGE = (
    "NOTION_TOKEN is not set. Add it to .env (NOTION_TOKEN=ntn_...) or your environment — see "
    ".env.example. The CRM is read through our own Notion MCP server, which authenticates with "
    "this integration token; there is no claude.ai connector fallback."
)


class MissingAPIKeyError(RuntimeError):
    """Raised when ANTHROPIC_API_KEY is absent — the one and only auth path is unavailable."""


class MissingNotionTokenError(RuntimeError):
    """Raised when NOTION_TOKEN is absent — the CRM (Notion MCP server) cannot authenticate."""


def load_env() -> None:
    """Load `.env` (BOM-safe) without overriding values already set in the real environment.

    No-op if `.env` is absent, so an externally-exported key still works. Also repairs a key that
    a BOM smuggled in under a ``\\ufeff``-prefixed name.
    """
    env_path = Path.cwd() / ".env"
    if env_path.exists():
        # utf-8-sig strips a leading BOM so the first variable name parses correctly.
        load_dotenv(dotenv_path=env_path, encoding="utf-8-sig", override=False)

    # Defensive: if a BOM still smuggled a var in under a prefixed name, recover it; then strip
    # stray whitespace/BOM from the value itself. Applies to every secret we read from .env.
    for var in (_API_KEY_ENV, _NOTION_TOKEN_ENV):
        if not os.environ.get(var):
            for name, value in list(os.environ.items()):
                if name.lstrip(_BOM) == var and value:
                    os.environ[var] = value.strip()
                    break
        val = os.environ.get(var)
        if val:
            os.environ[var] = val.strip().lstrip(_BOM)


def require_api_key() -> str:
    """Return the API key from the environment, or fail fast. Never prints the value."""
    key = os.environ.get(_API_KEY_ENV, "").strip()
    if not key:
        raise MissingAPIKeyError(_MISSING_MESSAGE)
    # Ensure the (cleaned) key is in os.environ so the SDK's CLI subprocess inherits it.
    os.environ[_API_KEY_ENV] = key
    return key


def require_notion_token() -> str:
    """Return the Notion integration token from the environment, or fail fast. Never prints it."""
    tok = os.environ.get(_NOTION_TOKEN_ENV, "").strip()
    if not tok:
        raise MissingNotionTokenError(_MISSING_NOTION_MESSAGE)
    os.environ[_NOTION_TOKEN_ENV] = tok
    return tok


def ensure_api_key() -> str:
    """Load `.env` then require the API key. The single call every entry point makes."""
    load_env()
    return require_api_key()


def ensure_credentials() -> None:
    """Load `.env` then require BOTH the API key and the Notion token (fail fast, no fallback).

    Every entry point that reaches the CRM calls this so a missing credential is reported up front
    with a clear message rather than failing deep inside the agent loop.
    """
    load_env()
    require_api_key()
    require_notion_token()
