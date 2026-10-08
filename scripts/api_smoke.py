"""Live smoke test for the /v1 client. Read-only; everything goes to stderr.

Prints per-call latency so a regression in the API path is visible immediately.
With a warm session this must not launch Chromium at all -- check with
`ps aux | grep -c [c]hromium` while it runs.

    python scripts/api_smoke.py            # warm path, no browser
    python scripts/api_smoke.py --relogin  # allow the browser login fallback
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from schoology_mcp.api import SchoologyAPI  # noqa: E402


async def main() -> int:
    relogin = None
    if "--relogin" in sys.argv:
        from schoology_mcp.browser import relogin_only
        relogin = relogin_only

    api = SchoologyAPI(relogin=relogin)
    try:
        t0 = time.time()
        uid = await api.uid()
        print(f"{time.time() - t0:5.2f}s  /v1/users/me  uid={uid}", file=sys.stderr)

        for path in (
            f"/v1/users/{uid}/sections",
            f"/v1/users/{uid}/grades",
            "/v1/messages/inbox",
            "/v1/recent?limit=5",
        ):
            t0 = time.time()
            data = await api.get(path)
            key = next(iter(data)) if isinstance(data, dict) else "?"
            n = len(data.get(key, [])) if isinstance(data, dict) else 0
            print(f"{time.time() - t0:5.2f}s  {path}  {key}={n}", file=sys.stderr)
    finally:
        await api.close()
    return 0


sys.exit(asyncio.run(main()))
