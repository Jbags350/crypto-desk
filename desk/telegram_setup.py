"""Telegram setup helper: run telegram_setup.bat

1. Checks the TELEGRAM_BOT_TOKEN in .env works.
2. Finds your chat ID from the message you sent your bot.
3. Saves it to .env (after asking you) and sends a test message.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from desk.config import DEFAULT_ENV_PATH, load_secrets
from desk.telegram import TelegramClient, TelegramError

BOTFATHER_STEPS = """How to create your bot (2 minutes):
  1. In Telegram, search for @BotFather (blue tick), open it, press Start.
  2. Send /newbot, give it a name, then a username ending in "bot".
  3. Copy the token it gives you (looks like 123456789:AAH...).
  4. Open .env in Notepad, paste it after TELEGRAM_BOT_TOKEN=, save.
  5. Open your new bot in Telegram, press Start and send it "hi".
  6. Run telegram_setup.bat again."""


def find_chats(updates: list[dict]) -> list[tuple[str, str]]:
    """[(chat id, who)] of private chats that messaged the bot, newest last, no duplicates."""
    seen: dict[str, str] = {}
    for u in updates:
        chat = (u.get("message") or {}).get("chat") or {}
        if chat.get("type") != "private" or "id" not in chat:
            continue
        who = " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x) or "unknown"
        if chat.get("username"):
            who += f" (@{chat['username']})"
        seen[str(chat["id"])] = who
    return list(seen.items())


def save_chat_id(env_path: Path, chat_id: str) -> None:
    text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    line = f"TELEGRAM_CHAT_ID={chat_id}"
    if re.search(r"^TELEGRAM_CHAT_ID=.*$", text, flags=re.MULTILINE):
        text = re.sub(r"^TELEGRAM_CHAT_ID=.*$", line, text, flags=re.MULTILINE)
    else:
        text = text.rstrip("\n") + "\n" + line + "\n"
    env_path.write_text(text, encoding="utf-8")


def run(env_path: Path = DEFAULT_ENV_PATH, client: TelegramClient | None = None, ask=input) -> int:
    s = load_secrets(env_path)
    if not s.telegram_bot_token:
        print("No TELEGRAM_BOT_TOKEN in .env yet.\n")
        print(BOTFATHER_STEPS)
        return 1
    client = client or TelegramClient(s.telegram_bot_token)
    try:
        me = client.get_me()
    except TelegramError as exc:
        print(f"Telegram didn't accept the token: {exc}")
        print("Check the TELEGRAM_BOT_TOKEN line in .env (no spaces or quotes), then try again.")
        return 1
    print(f"Token OK: your bot is @{me.get('username')} ({me.get('first_name')})")

    try:
        chats = find_chats(client.updates(None, timeout=0))
    except TelegramError as exc:
        print(f"Couldn't read the bot's messages: {exc}")
        return 1
    if s.telegram_chat_id:
        chosen = s.telegram_chat_id
        print(f"TELEGRAM_CHAT_ID is already set ({chosen}). (To change it, delete that line in .env and run this again.)")
    elif not chats:
        print(f"\nNo messages found yet. Open @{me.get('username')} in Telegram, press Start, send it \"hi\", "
              f"then run telegram_setup.bat again.")
        return 1
    else:
        if len(chats) == 1:
            chosen, who = chats[0]
            print(f"\nFound your chat: {who}, chat ID {chosen}")
        else:
            print("\nMore than one person has messaged this bot:")
            for i, (cid, who) in enumerate(chats, 1):
                print(f"  {i}. {who} (chat ID {cid})")
            pick = ask("Which one is you? Type the number: ").strip()
            if not pick.isdigit() or not 1 <= int(pick) <= len(chats):
                print("No change made.")
                return 1
            chosen, who = chats[int(pick) - 1]
        answer = ask(f"Save TELEGRAM_CHAT_ID={chosen} to .env? Only this chat will be able to control the bot. "
                     f"[y/n]: ").strip().lower()
        if answer not in ("y", "yes"):
            print("No change made.")
            return 1
        save_chat_id(env_path, chosen)
        print("Saved to .env.")

    try:
        client.send(chosen, "✅ Crypto Desk is connected. You'll get trade alerts, risk warnings and a daily "
                            "summary here. Send /help to see the commands (they work while paper.bat is running).")
    except TelegramError as exc:
        print(f"Couldn't send the test message: {exc}")
        return 1
    print("Test message sent. Check Telegram on your phone!")
    print("Restart paper.bat (Ctrl+C, then double-click it) so it picks up the new settings.")
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    sys.exit(main())
