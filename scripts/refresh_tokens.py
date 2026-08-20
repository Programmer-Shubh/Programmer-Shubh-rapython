import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "ratrade.db"))

from routes import broker


async def main():
    result = await broker.refresh_tokens()
    ok = []
    failed = []
    for k, v in (result.get("results") or {}).items():
        if v.get("success"):
            ok.append(k)
        else:
            failed.append(f"{k}: {v.get('error')}")
    print(f"Token refresh complete. OK: {ok}")
    if failed:
        print(f"Failed: {failed}")


if __name__ == "__main__":
    asyncio.run(main())