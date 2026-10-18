from copy import copy
from time import time

from ..helper.ext_utils.bot_utils import new_task
from ..core.config_manager import Config
from ..helper.ext_utils.mirror_deep_link import (
    MirrorLookupError,
    fetch_release_magnet,
    mirror_deep_link_release_id,
)
from ..helper.telegram_helper.bot_commands import BotCommands
from ..helper.telegram_helper.button_build import ButtonMaker
from ..helper.telegram_helper.filters import CustomFilters
from ..helper.telegram_helper.message_utils import edit_message, send_file, send_message
from .mirror_leech import mirror


@new_task
async def start(_, message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2 and parts[1].startswith("mirror_"):
        # /start is public, unlike /mirror: explicitly apply the same filter
        # before handing a deep link to the existing download pipeline.
        if not await CustomFilters.authorized(_, message):
            await send_message(message, "You are not authorized to start downloads.")
            return
        try:
            release_id = mirror_deep_link_release_id(parts[1])
        except ValueError:
            await send_message(message, "Invalid mirror download link.")
            return
        try:
            magnet = await fetch_release_magnet(Config.AT_API_URL, release_id)
        except MirrorLookupError as error:
            await send_message(message, str(error))
            return
        command = f"/{BotCommands.MirrorCommand[0]} {magnet}"
        # Preserve the requesting user, chat, message ID and client. Do not send
        # a command as the bot (which would change task ownership/permissions).
        mirror_message = copy(message)
        mirror_message.text = command
        mirror_message.command = command[1:].split()
        mirror_message.entities = None
        await mirror(_, mirror_message)
        return

    buttons = ButtonMaker()
    buttons.url_button(
        "Repo", "https://www.github.com/anasty17/mirror-leech-telegram-bot"
    )
    buttons.url_button("Code Owner", "https://t.me/anas_tayyar")
    reply_markup = buttons.build_menu(2)
    if await CustomFilters.authorized(_, message):
        start_string = f"""
This bot can mirror from links|tgfiles|torrents|nzb|rclone-cloud to any rclone cloud, Google Drive or to telegram.
Type /{BotCommands.HelpCommand} to get a list of available commands
"""
        await send_message(message, start_string, reply_markup)
    else:
        await send_message(
            message,
            "This bot can mirror from links|tgfiles|torrents|nzb|rclone-cloud to any rclone cloud, Google Drive or to telegram.\n\n⚠️ You Are not authorized user! Deploy your own mirror-leech bot",
            reply_markup,
        )


@new_task
async def ping(_, message):
    start_time = int(round(time() * 1000))
    reply = await send_message(message, "Starting Ping")
    end_time = int(round(time() * 1000))
    await edit_message(reply, f"{end_time - start_time} ms")


@new_task
async def log(_, message):
    await send_file(message, "log.txt")
