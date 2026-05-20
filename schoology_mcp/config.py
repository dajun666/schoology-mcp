"""Configuration loaded from the project's .env file."""

import os
from pathlib import Path

from dotenv import load_dotenv

# Repo root = parent of this package directory.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Load .env explicitly from the repo root so it works regardless of the
# process CWD (MCP clients launch the server from arbitrary directories).
load_dotenv(dotenv_path=PROJECT_ROOT / ".env")


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


BASE_URL = os.getenv("SCHOOLOGY_BASE_URL", "https://pausd.schoology.com").rstrip("/")
CLASSLINK_URL = os.getenv("CLASSLINK_URL", "https://launchpad.classlink.com/pausd")
USERNAME = os.getenv("SCHOOLOGY_USERNAME")
HEADLESS = _flag("SCHOOLOGY_HEADLESS", True)

# The password is resolved at runtime and never required to live in a file:
#   1. SCHOOLOGY_PASSWORD env var, if explicitly set (e.g. an MCP `env` block);
#   2. otherwise the OS keychain (macOS Keychain) under KEYRING_SERVICE.
KEYRING_SERVICE = "schoology-mcp"
_PASSWORD_ENV = os.getenv("SCHOOLOGY_PASSWORD")

# Keep-alive: periodically re-visit Schoology so the (short-lived) session
# stays warm between tool calls. Re-logs in automatically if it expired anyway.
KEEPALIVE_ENABLED = _flag("SCHOOLOGY_KEEPALIVE", True)
KEEPALIVE_SECONDS = max(60, _int_env("SCHOOLOGY_KEEPALIVE_MINUTES", 8) * 60)

# Persisted Playwright session (cookies + localStorage) so restarts skip login.
STORAGE_STATE_PATH = Path(
    os.getenv("SCHOOLOGY_STORAGE_STATE", str(PROJECT_ROOT / "storage_state.json"))
)


def _password_from_keyring() -> str | None:
    """Read the password from the OS keychain, if available."""
    if not USERNAME:
        return None
    try:
        import keyring
    except ImportError:
        return None
    try:
        return keyring.get_password(KEYRING_SERVICE, USERNAME)
    except Exception:  # noqa: BLE001 - keychain locked / backend unavailable
        return None


def get_password() -> str | None:
    """Resolve the password: explicit env var first, then the OS keychain."""
    return _PASSWORD_ENV or _password_from_keyring()


def require_credentials() -> None:
    """Raise a clear, actionable error if credentials are not configured."""
    if not USERNAME:
        raise RuntimeError(
            "SCHOOLOGY_USERNAME is not set. Copy .env.example to "
            f"{PROJECT_ROOT / '.env'} and set your 8-digit student ID."
        )
    if not get_password():
        raise RuntimeError(
            "No password found. Store it once in the OS keychain with:\n"
            "    python scripts/set_credentials.py\n"
            "(or, as a fallback, set the SCHOOLOGY_PASSWORD environment variable)."
        )
