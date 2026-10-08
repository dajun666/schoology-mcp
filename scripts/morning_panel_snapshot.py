import asyncio
import json
import os
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(os.environ.get('SCHOOL_SNAPSHOT_OUT', 'morning-school-snapshot.json'))

async def main():
    params = StdioServerParameters(
        command=str(ROOT / '.venv/bin/python'),
        args=[str(ROOT / 'server.py')],
        cwd=str(ROOT),
    )
    calls = [
        ('get_schedule', {'term': 'S1', 'schedule': None}),
        ('get_upcoming_assignments', {'days': 14, 'include_info': False}),
        ('get_calendar_events', {'days_ahead': 14, 'days_back': 1}),
        ('get_recent_posts', {'limit': 20, 'download_images': False}),
    ]
    data = {}
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names = {t.name for t in (await session.list_tools()).tools}
            data['_tools'] = sorted(names)
            for name, args in calls:
                if name not in names:
                    data[name] = {'error': 'tool unavailable'}
                    continue
                try:
                    result = await session.call_tool(name, args, read_timeout_seconds=timedelta(minutes=4))
                    if result.structuredContent is not None:
                        data[name] = result.structuredContent
                    else:
                        texts = [getattr(c, 'text', '') for c in result.content]
                        data[name] = json.loads('\n'.join(texts))
                except Exception as exc:
                    data[name] = {'error': f'{type(exc).__name__}: {exc}'}
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    print(OUT)

if __name__ == '__main__':
    asyncio.run(main())
