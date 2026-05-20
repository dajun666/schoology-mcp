"""Store / remove the Schoology password in the OS keychain.

This keeps the password out of any plaintext file. On macOS it uses the system
Keychain: the password is encrypted at rest and unlocked by your macOS login.
The input is read with getpass, so it is never echoed or saved to shell history.

Usage:
    python scripts/set_credentials.py            # store (hidden prompt)
    python scripts/set_credentials.py --delete    # remove the stored password
"""

import getpass
import pathlib
import sys

# Make the `schoology_mcp` package importable when run as a script.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

try:
    import keyring
except ImportError:
    sys.exit("The 'keyring' package is required:  pip install -r requirements.txt")

from schoology_mcp import config  # noqa: E402


def main() -> None:
    username = config.USERNAME or input(
        "Student ID (8-digit, starts with 950): "
    ).strip()
    if not username:
        sys.exit("No student ID. Set SCHOOLOGY_USERNAME in .env, then re-run.")

    if "--delete" in sys.argv:
        try:
            keyring.delete_password(config.KEYRING_SERVICE, username)
            print(f"Removed keychain password for '{username}'.")
        except Exception:  # noqa: BLE001 - nothing stored
            print(f"No keychain password was stored for '{username}'.")
        return

    password = getpass.getpass(f"Schoology password for {username} (hidden): ")
    if not password:
        sys.exit("No password entered; nothing stored.")

    keyring.set_password(config.KEYRING_SERVICE, username, password)
    print(
        f"Stored in the OS keychain "
        f"(service '{config.KEYRING_SERVICE}', account '{username}').\n"
        "Leave SCHOOLOGY_PASSWORD unset in .env -- the keychain is used "
        "automatically."
    )


if __name__ == "__main__":
    main()
