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
CLASSLINK_URL = os.getenv("CLASSLINK_URL", "https://login.classlink.com/my/pausd")
USERNAME = os.getenv("SCHOOLOGY_USERNAME")
HEADLESS = _flag("SCHOOLOGY_HEADLESS", True)

# The password is resolved at runtime and never required to live in a file:
#   1. SCHOOLOGY_PASSWORD env var, if explicitly set (e.g. an MCP `env` block);
#   2. otherwise a keyring backend under KEYRING_SERVICE.
#
# The keyring backend is cross-platform:
#   - "os" (default): the native OS keychain via the `keyring` library --
#     macOS Keychain, Windows Credential Manager, or Linux Secret Service.
#   - "cryptfile": an AES-encrypted file (`keyrings.cryptfile`) unlocked by a
#     master passphrase. Intended for headless Linux (servers, WSL, Docker,
#     cron) where no OS secret store is running.
KEYRING_SERVICE = "schoology-mcp"
_PASSWORD_ENV = os.getenv("SCHOOLOGY_PASSWORD")

# Keyring backend selection. A set passphrase implicitly selects "cryptfile".
KEYRING_BACKEND = (os.getenv("SCHOOLOGY_KEYRING_BACKEND") or "os").strip().lower()
_KEYRING_PASS = os.getenv("SCHOOLOGY_KEYRING_PASS")
KEYRING_FILE = Path(
    os.getenv("SCHOOLOGY_KEYRING_FILE")
    or (Path.home() / ".local" / "share" / "schoology-mcp" / "credentials.cfg")
)


def _use_cryptfile() -> bool:
    """True when the encrypted-file backend should be used.

    Explicitly selected via SCHOOLOGY_KEYRING_BACKEND=cryptfile, or implicitly
    when a master passphrase is set and no other backend was requested.
    """
    if KEYRING_BACKEND == "cryptfile":
        return True
    return KEYRING_BACKEND == "os" and bool(_KEYRING_PASS)


def _get_keyring():
    """Return the keyring backend to read/write the password, or None.

    Returns None (rather than prompting or raising) when the backend cannot be
    used unattended -- e.g. the cryptfile backend with no passphrase available,
    or a missing dependency. Callers treat None as "no password resolvable".
    """
    if _use_cryptfile():
        try:
            from keyrings.cryptfile.cryptfile import CryptFileKeyring
        except ImportError:
            return None
        if not _KEYRING_PASS:
            # No passphrase: do not trigger the library's interactive prompt
            # in the (unattended) server path.
            return None
        kr = CryptFileKeyring()
        kr.file_path = str(KEYRING_FILE)
        try:
            # Assigning the key eagerly unlocks an existing file; a wrong
            # passphrase raises here -- degrade to None rather than leak a
            # traceback.
            kr.keyring_key = _KEYRING_PASS
        except Exception:  # noqa: BLE001 - wrong passphrase / corrupt file
            return None
        return kr
    try:
        import keyring
    except ImportError:
        return None
    try:
        return keyring.get_keyring()
    except Exception:  # noqa: BLE001 - no usable backend
        return None

# Keep-alive: periodically re-visit Schoology so the (short-lived) session
# stays warm between tool calls. Re-logs in automatically if it expired anyway.
KEEPALIVE_ENABLED = _flag("SCHOOLOGY_KEEPALIVE", True)
KEEPALIVE_SECONDS = max(60, _int_env("SCHOOLOGY_KEEPALIVE_MINUTES", 8) * 60)

# Persisted Playwright session (cookies + localStorage) so restarts skip login.
STORAGE_STATE_PATH = Path(
    os.getenv("SCHOOLOGY_STORAGE_STATE", str(PROJECT_ROOT / "storage_state.json"))
)

# Where downloaded/exported materials are cached (a temp area, not a store), and
# where scripts/watch_once.py keeps its change-detection baseline. Both live
# here rather than being read ad hoc so `.env` remains the answer to "what can I
# configure?".
EXPORT_DIR = os.getenv("SCHOOLOGY_EXPORT_DIR")
WATCH_STATE_PATH = Path(
    os.getenv("SCHOOLOGY_WATCH_STATE")
    or (Path.home() / ".local" / "share" / "schoology-mcp" / "watch-state.json")
)


def is_schoology_url(url: str) -> bool:
    """True for an absolute URL on this Schoology host, or a bare path."""
    if not url:
        return False
    return url.startswith("/") or BASE_URL.split("//")[-1] in url


def _password_from_keyring() -> str | None:
    """Read the password from the configured keyring backend, if available."""
    if not USERNAME:
        return None
    kr = _get_keyring()
    if kr is None:
        return None
    try:
        return kr.get_password(KEYRING_SERVICE, USERNAME)
    except Exception:  # noqa: BLE001 - locked / wrong passphrase / unavailable
        return None


def get_password() -> str | None:
    """Resolve the password: explicit env var first, then the keyring."""
    return _PASSWORD_ENV or _password_from_keyring()


def require_credentials() -> None:
    """Raise a clear, actionable error if credentials are not configured."""
    if not USERNAME:
        raise RuntimeError(
            "SCHOOLOGY_USERNAME is not set. Copy .env.example to "
            f"{PROJECT_ROOT / '.env'} and set your 8-digit student ID."
        )
    if not get_password():
        if _use_cryptfile() and not _KEYRING_PASS:
            raise RuntimeError(
                "Encrypted-file keyring selected but SCHOOLOGY_KEYRING_PASS is "
                "not set. Set the master passphrase in this process's "
                "environment (the same one used with set_credentials.py)."
            )
        raise RuntimeError(
            "No password found. Store it once with:\n"
            "    python scripts/set_credentials.py\n"
            "On macOS/Windows/Linux-desktop this uses the OS keychain. On "
            "headless Linux, set SCHOOLOGY_KEYRING_PASS first to use the "
            "encrypted-file store.\n"
            "(Or, as a fallback, set the SCHOOLOGY_PASSWORD environment variable.)"
        )
