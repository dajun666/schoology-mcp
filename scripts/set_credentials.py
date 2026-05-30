"""Store / remove the Schoology password in a keyring -- never a plaintext file.

The backend is cross-platform and chosen the same way the server resolves it
(see schoology_mcp/config.py):

  - macOS         -> Keychain
  - Windows       -> Credential Manager
  - Linux desktop -> Secret Service (GNOME Keyring / KWallet)
  - headless Linux-> AES-encrypted file (keyrings.cryptfile), unlocked by a
                     master passphrase (SCHOOLOGY_KEYRING_PASS).

The password input is read with getpass, so it is never echoed or saved to
shell history, and it is encrypted at rest by whichever backend is in use.

Usage:
    python scripts/set_credentials.py            # store (hidden prompt)
    python scripts/set_credentials.py --delete    # remove the stored password
"""

import getpass
import pathlib
import sys

# Make the `schoology_mcp` package importable when run as a script.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from schoology_mcp import config  # noqa: E402


def _resolve_keyring():
    """Return a keyring backend for interactive setup.

    Like config._get_keyring(), but when the cryptfile backend is selected
    without a passphrase in the environment, prompt for a master passphrase
    here (interactive setup is the right place to create the encrypted file).
    """
    kr = config._get_keyring()
    if kr is not None:
        return kr

    if config._use_cryptfile():
        try:
            from keyrings.cryptfile.cryptfile import CryptFileKeyring
        except ImportError:
            sys.exit(
                "The 'keyrings.cryptfile' package is required for the encrypted "
                "file backend:  pip install -r requirements.txt"
            )
        passphrase = config._KEYRING_PASS or getpass.getpass(
            "Master passphrase for the encrypted credentials file (hidden): "
        )
        if not passphrase:
            sys.exit("No master passphrase entered; nothing stored.")
        # Create the parent directory (private) before the file is written.
        config.KEYRING_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        kr = CryptFileKeyring()
        kr.file_path = str(config.KEYRING_FILE)
        kr.keyring_key = passphrase
        return kr

    sys.exit(
        "No usable keyring backend was found. Install one "
        "(pip install -r requirements.txt) or, on headless Linux, set "
        "SCHOOLOGY_KEYRING_BACKEND=cryptfile."
    )


def main() -> None:
    username = config.USERNAME or input(
        "Student ID (8-digit, starts with 950): "
    ).strip()
    if not username:
        sys.exit("No student ID. Set SCHOOLOGY_USERNAME in .env, then re-run.")

    kr = _resolve_keyring()

    if "--delete" in sys.argv:
        try:
            kr.delete_password(config.KEYRING_SERVICE, username)
            print(f"Removed stored password for '{username}'.")
        except Exception:  # noqa: BLE001 - nothing stored
            print(f"No password was stored for '{username}'.")
        return

    password = getpass.getpass(f"Schoology password for {username} (hidden): ")
    if not password:
        sys.exit("No password entered; nothing stored.")

    kr.set_password(config.KEYRING_SERVICE, username, password)
    print(
        f"Stored in the keyring "
        f"(service '{config.KEYRING_SERVICE}', account '{username}')."
    )
    if config._use_cryptfile():
        print(
            f"Encrypted file: {config.KEYRING_FILE}\n"
            "Run the server with the same SCHOOLOGY_KEYRING_PASS in its "
            "environment (e.g. a systemd EnvironmentFile with perms 600, or the "
            "MCP client's `env` block) so it can unlock this file."
        )
    else:
        print(
            "Leave SCHOOLOGY_PASSWORD unset in .env -- the keyring is used "
            "automatically."
        )


if __name__ == "__main__":
    main()
