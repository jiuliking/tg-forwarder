import asyncio
import os
from pathlib import Path

from telethon import TelegramClient, utils

from common import load_env_file

BASE_DIR = Path(os.environ.get("TG_FWD_DIR") or Path(__file__).resolve().parent)
ENV_PATH = BASE_DIR / ".env"
SESSION_PATH = BASE_DIR / "session" / "userbot"


async def main() -> None:
    load_env_file(ENV_PATH)
    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]

    client = TelegramClient(str(SESSION_PATH), api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        print("尚未登录，请先运行 login.py")
        await client.disconnect()
        return

    print(f"{'ID':<16} {'Type':<12} {'NoFwd':<6} Title / Username")
    print("-" * 72)
    async for d in client.iter_dialogs():
        if not (d.is_channel or d.is_group):
            continue
        ent = d.entity
        pid = utils.get_peer_id(ent)
        kind = "channel" if getattr(ent, "broadcast", False) else ("supergroup" if d.is_channel else "group")
        nofwd = "yes" if getattr(ent, "noforwards", False) else "no"
        uname = f"@{ent.username}" if getattr(ent, "username", None) else ""
        print(f"{pid:<16} {kind:<12} {nofwd:<6} {d.name} {uname}".strip())

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
