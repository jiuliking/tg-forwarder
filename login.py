import argparse
import asyncio
import os
from pathlib import Path

import qrcode
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from common import load_env_file

BASE_DIR = Path(os.environ.get("TG_FWD_DIR") or Path(__file__).resolve().parent)
ENV_PATH = BASE_DIR / ".env"
SESSION_DIR = BASE_DIR / "session"
SESSION_PATH = SESSION_DIR / "userbot"
QR_PNG_PATH = BASE_DIR / "cache" / "login_qr.png"


async def qr_login(client: TelegramClient) -> None:
    qr = await client.qr_login()
    while True:
        q = qrcode.QRCode(border=1)
        q.add_data(qr.url)
        q.make(fit=True)
        q.print_ascii(invert=True)
        QR_PNG_PATH.parent.mkdir(parents=True, exist_ok=True)
        img = q.make_image(fill_color="black", back_color="white")
        img.save(QR_PNG_PATH)
        print(f"\n请使用小号 Telegram 扫描上方二维码（设置 -> 设备 -> 连接桌面设备）。\n二维码图片也已保存到: {QR_PNG_PATH}")
        try:
            await qr.wait(timeout=45)
            break
        except asyncio.TimeoutError:
            print("二维码已刷新，请重新扫描...")
            await qr.recreate()
        except SessionPasswordNeededError:
            import getpass

            pwd = getpass.getpass("请输入两步验证 (2FA) 密码: ")
            await client.sign_in(password=pwd)
            break


async def main() -> None:
    parser = argparse.ArgumentParser(description="Login alt Telegram account for tg-forwarder")
    parser.add_argument("--qr", action="store_true", help="Login via QR code instead of phone number")
    args = parser.parse_args()

    load_env_file(ENV_PATH)
    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(SESSION_DIR, 0o700)

    client = TelegramClient(str(SESSION_PATH), api_id, api_hash)
    await client.connect()
    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"已经登录过啦: {me.first_name} (@{me.username}, id={me.id})")
        await client.disconnect()
        return

    if args.qr:
        await qr_login(client)
    else:
        await client.start()

    session_file = Path(f"{SESSION_PATH}.session")
    if session_file.exists():
        os.chmod(session_file, 0o600)
    if QR_PNG_PATH.exists():
        QR_PNG_PATH.unlink(missing_ok=True)

    me = await client.get_me()
    print(f"登录成功: {me.first_name} (@{me.username}, id={me.id})")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
