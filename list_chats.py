import asyncio
import os
from pathlib import Path

from telethon import TelegramClient, utils

from common import load_env_file, load_raw_config, resolve_tg_api

BASE_DIR = Path(os.environ.get("TG_FWD_DIR") or Path(__file__).resolve().parent)
ENV_PATH = BASE_DIR / ".env"
CONFIG_PATH = BASE_DIR / "config.yaml"
SESSION_PATH = BASE_DIR / "session" / "userbot"


async def main() -> None:
    load_env_file(ENV_PATH)
    api_id, api_hash, _ = resolve_tg_api(load_raw_config(CONFIG_PATH))
    if not api_id or not api_hash:
        print("缺少 TELEGRAM_API_ID / TELEGRAM_API_HASH，请先在 .env 或 config.yaml 中配置")
        return

    client = TelegramClient(str(SESSION_PATH), api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized() or await client.get_me() is None:
        print("尚未登录或会话已失效，请先运行 login.py")
        await client.disconnect()
        return

    print(f"{'ID':<16} {'Type':<12} {'NoFwd':<6} Title / Username")
    print("-" * 72)
    async for d in client.iter_dialogs():
        if not (d.is_channel or d.is_group):
            continue
        ent = d.entity
        if getattr(ent, "deactivated", False) or getattr(ent, "migrated_to", None):
            continue
        pid = utils.get_peer_id(ent)
        kind = "channel" if getattr(ent, "broadcast", False) else ("supergroup" if d.is_channel else "group")
        nofwd = "yes" if getattr(ent, "noforwards", False) else "no"
        uname = f"@{ent.username}" if getattr(ent, "username", None) else ""
        print(f"{pid:<16} {kind:<12} {nofwd:<6} {d.name} {uname}".strip())

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
