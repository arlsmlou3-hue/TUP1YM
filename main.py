from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Literal

from highrise import AnchorPosition, BaseBot, Position, User
from highrise.__main__ import BotDefinition, main
from highrise.models import Error, RoomPermissions

logger = logging.getLogger("highrise-room-bot")

COMMANDS = {
    "جيب": "summon",
    "سحب": "summon",
    "summon": "summon",
    "bring": "summon",
    "كتم": "mute",
    "mute": "mute",
    "طرد": "kick",
    "kick": "kick",
    "ترقية": "promote",
    "منح_مشرف": "promote",
    "إزالة_مشرف": "demote",
    "ازالة_مشرف": "demote",
    "سحب_مشرف": "demote",
    "البوت": "bot_control",
    "بوت": "bot_control",
    "مساعدة": "help",
    "مساعده": "help",
    "help": "help",
}

DURATION_PATTERN = re.compile(
    r"^([0-9٠-٩۰-۹]+)\s*(s|sec|ث|m|min|د|h|hr|س|d|day|ي)?$",
    re.IGNORECASE,
)

DURATION_UNITS = {
    "": 60,
    "s": 1,
    "sec": 1,
    "ث": 1,
    "m": 60,
    "min": 60,
    "د": 60,
    "h": 3600,
    "hr": 3600,
    "س": 3600,
    "d": 86400,
    "day": 86400,
    "ي": 86400,
}

MAX_MUTE_SECONDS = 7 * 24 * 60 * 60
COMMAND_COOLDOWN_SECONDS = 1.0


@dataclass(frozen=True)
class ParsedCommand:
    action: Literal[
        "summon",
        "mute",
        "kick",
        "promote",
        "demote",
        "bot_control",
        "bot_on",
        "bot_off",
        "help",
    ]
    target: str = ""
    duration_seconds: int | None = None


def _ascii_digits(value: str) -> str:
    return value.translate(
        str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
    )


def parse_duration(value: str) -> int | None:
    match = DURATION_PATTERN.fullmatch(_ascii_digits(value.strip()))
    if not match:
        return None

    amount = int(match.group(1))
    unit = (match.group(2) or "").lower()
    seconds = amount * DURATION_UNITS[unit]

    if amount < 1 or seconds > MAX_MUTE_SECONDS:
        return None

    return seconds


def parse_command(message: str) -> ParsedCommand | None:
    stripped = message.strip()
    if not stripped.startswith(("!", "/")):
        return None

    parts = stripped[1:].split(maxsplit=1)
    if not parts:
        return None

    action = COMMANDS.get(parts[0].casefold())
    if action is None:
        return None

    if action == "help":
        return ParsedCommand(action="help")

    if action == "bot_control":
        if len(parts) < 2:
            return ParsedCommand(action="bot_control")

        control = parts[1].strip().casefold()

        if control in {"تشغيل", "شغل", "on", "start"}:
            return ParsedCommand(action="bot_on")

        if control in {"إيقاف", "ايقاف", "طفي", "off", "stop"}:
            return ParsedCommand(action="bot_off")

        return ParsedCommand(action="bot_control", target=parts[1].strip())

    if len(parts) < 2:
        return ParsedCommand(action=action)

    payload = parts[1].strip()
    duration_seconds = None

    if action == "mute":
        target_part, separator, duration_part = payload.rpartition(" ")

        if not separator:
            return ParsedCommand(action="mute", target=payload)

        duration_seconds = parse_duration(duration_part)
        if duration_seconds is None:
            return ParsedCommand(
                action="mute",
                target=target_part,
                duration_seconds=-1,
            )

        payload = target_part

    target = payload.strip().removeprefix("@").strip()

    return ParsedCommand(
        action=action,
        target=target,
        duration_seconds=duration_seconds,
    )


class RoomModeratorBot(BaseBot):
    def __init__(self) -> None:
        self._last_command_at: dict[str, float] = {}
        self._room_owner_id: str | None = None
        self._commands_enabled = True

    async def on_start(self, session_metadata) -> None:
        self._room_owner_id = session_metadata.room_info.owner_id

        logger.info(
            "Connected to Highrise room %s (%s)",
            session_metadata.room_info.room_name,
            session_metadata.room_info.owner_id,
        )

        await self.highrise.chat(
            "بوت الإدارة متصل. اكتبوا !مساعدة لعرض الأوامر."
        )

    async def on_chat(self, user: User, message: str) -> None:
        command = parse_command(message)
        if command is None:
            return

        now = time.monotonic()
        if (
            now - self._last_command_at.get(user.id, 0)
            < COMMAND_COOLDOWN_SECONDS
        ):
            return

        self._last_command_at[user.id] = now

        if command.action == "help":
            await self.highrise.send_whisper(user.id, self._help_text())
            return

        if command.action in {"bot_on", "bot_off", "bot_control"}:
            if not self._is_room_owner(user.id):
                await self.highrise.send_whisper(
                    user.id,
                    "مالك الغرفة فقط يقدر يشغل أو يوقف الأوامر.",
                )
                return

            if command.action == "bot_control":
                await self.highrise.send_whisper(
                    user.id,
                    "استخدم !البوت تشغيل أو !البوت إيقاف.",
                )
                return

            self._commands_enabled = command.action == "bot_on"

            response = (
                "تم تشغيل أوامر الإدارة."
                if self._commands_enabled
                else "تم إيقاف أوامر الإدارة. البوت ما زال متصلًا."
            )
            await self.highrise.send_whisper(user.id, response)
            return

        if command.action in {"promote", "demote"}:
            if not self._is_room_owner(user.id):
                await self.highrise.send_whisper(
                    user.id,
                    "مالك الغرفة فقط يقدر يضيف أو يزيل مشرفين.",
                )
                return

            await self._change_moderator(user, command)
            return

        if not self._commands_enabled:
            await self.highrise.send_whisper(
                user.id,
                "أوامر الإدارة متوقفة حاليًا من مالك الغرفة.",
            )
            return

        if not await self._has_moderation_rights(user.id):
            await self.highrise.send_whisper(
                user.id,
                "هذا الأمر لمالك الغرفة أو للمشرفين والمصممين فقط.",
            )
            return

        if not command.target:
            await self.highrise.send_whisper(
                user.id,
                self._usage(command.action),
            )
            return

        if command.action == "mute" and command.duration_seconds == -1:
            await self.highrise.send_whisper(
                user.id,
                "المدة غير صحيحة. مثال: !كتم @الاسم 5m",
            )
            return

        if command.action == "mute" and command.duration_seconds is None:
            await self.highrise.send_whisper(
                user.id,
                "اكتب مدة الكتم. مثال: !كتم @الاسم 5m",
            )
            return

        room_users = await self.highrise.get_room_users()
        if isinstance(room_users, Error):
            await self.highrise.send_whisper(
                user.id,
                "ما قدرت أقرأ الموجودين بالروم.",
            )
            logger.warning("Unable to read room users: %s", room_users.message)
            return

        matches = [
            (room_user, position)
            for room_user, position in room_users.content
            if room_user.username.casefold() == command.target.casefold()
        ]

        if not matches:
            await self.highrise.send_whisper(
                user.id,
                f"ما لقيت @{command.target} داخل الروم.",
            )
            return

        if len(matches) > 1:
            await self.highrise.send_whisper(
                user.id,
                "الاسم مو فريد. استخدم اسم المستخدم الكامل.",
            )
            return

        target, target_position = matches[0]

        if target.id == self.highrise.my_id:
            await self.highrise.send_whisper(
                user.id,
                "ما أقدر أطبق الأمر على نفسي.",
            )
            return

        if target.id == user.id and command.action != "summon":
            await self.highrise.send_whisper(
                user.id,
                "ما أقدر أطبق هذا الأمر عليك.",
            )
            return

        if await self._has_moderation_rights(target.id):
            await self.highrise.send_whisper(
                user.id,
                "ما أقدر أستهدف مشرفًا أو مصممًا آخر.",
            )
            return

        try:
            if command.action == "summon":
                await self._summon(user, target, target_position)

            elif command.action == "mute":
                await self.highrise.moderate_room(
                    target.id,
                    "mute",
                    command.duration_seconds,
                )
                await self.highrise.chat(
                    f"تم كتم @{target.username} لمدة "
                    f"{self._format_duration(command.duration_seconds or 0)}."
                )

            elif command.action == "kick":
                await self.highrise.moderate_room(target.id, "kick")
                await self.highrise.chat(
                    f"تم طرد @{target.username} من الروم."
                )

        except Exception:
            logger.exception("Highrise command failed: %s", command.action)
            await self.highrise.send_whisper(
                user.id,
                "ما اكتمل الأمر. تأكد من صلاحيات البوت وحاول مرة ثانية.",
            )

    async def _change_moderator(
        self,
        sender: User,
        command: ParsedCommand,
    ) -> None:
        if not command.target:
            usage = (
                "مثال: !ترقية @الاسم"
                if command.action == "promote"
                else "مثال: !إزالة_مشرف @الاسم"
            )
            await self.highrise.send_whisper(sender.id, usage)
            return

        room_users = await self.highrise.get_room_users()
        if isinstance(room_users, Error):
            await self.highrise.send_whisper(
                sender.id,
                "ما قدرت أقرأ الموجودين بالروم.",
            )
            logger.warning("Unable to read room users: %s", room_users.message)
            return

        matches = [
            room_user
            for room_user, _ in room_users.content
            if room_user.username.casefold() == command.target.casefold()
        ]

        if not matches:
            await self.highrise.send_whisper(
                sender.id,
                f"ما لقيت @{command.target} داخل الروم.",
            )
            return

        if len(matches) > 1:
            await self.highrise.send_whisper(
                sender.id,
                "الاسم مو فريد. استخدم اسم المستخدم الكامل.",
            )
            return

        target = matches[0]

        if target.id == self._room_owner_id:
            await self.highrise.send_whisper(
                sender.id,
                "ما أقدر أغيّر صلاحية مالك الغرفة.",
            )
            return

        if target.id == sender.id:
            await self.highrise.send_whisper(
                sender.id,
                "ما تقدر تغيّر رتبتك من خلال هذا الأمر.",
            )
            return

        privileges = await self.highrise.get_room_privilege(target.id)
        if isinstance(privileges, Error):
            await self.highrise.send_whisper(
                sender.id,
                "ما قدرت أتحقق من صلاحيات هذا المستخدم.",
            )
            logger.warning(
                "Privilege check failed: %s",
                privileges.message,
            )
            return

        try:
            if command.action == "promote":
                if privileges.moderator:
                    await self.highrise.send_whisper(
                        sender.id,
                        f"@{target.username} مشرف بالفعل.",
                    )
                    return

                await self.highrise.change_room_privilege(
                    target.id,
                    RoomPermissions(moderator=True),
                )
                await self.highrise.send_whisper(
                    sender.id,
                    f"تمت إضافة @{target.username} كمشرف.",
                )
                return

            if not privileges.moderator:
                await self.highrise.send_whisper(
                    sender.id,
                    f"@{target.username} ليس مشرفًا.",
                )
                return

            await self.highrise.change_room_privilege(
                target.id,
                RoomPermissions(moderator=False),
            )

            if privileges.designer:
                await self.highrise.send_whisper(
                    sender.id,
                    f"أزيلت صفة المشرف عن @{target.username}؛ "
                    "بقيت له صلاحية المصمم.",
                )
            else:
                await self.highrise.send_whisper(
                    sender.id,
                    f"تمت إزالة @{target.username} من المشرفين.",
                )

        except Exception:
            logger.exception(
                "Unable to change room moderator: %s",
                command.action,
            )
            await self.highrise.send_whisper(
                sender.id,
                "ما اكتمل تغيير الصلاحية. تأكد من صلاحيات البوت.",
            )

    async def _summon(
        self,
        sender: User,
        target: User,
        target_position: Position | AnchorPosition,
    ) -> None:
        room_users = await self.highrise.get_room_users()
        if isinstance(room_users, Error):
            await self.highrise.send_whisper(
                sender.id,
                "ما قدرت أقرأ موقعك داخل الروم.",
            )
            return

        sender_position = next(
            (
                position
                for room_user, position in room_users.content
                if room_user.id == sender.id
            ),
            None,
        )

        if not isinstance(sender_position, Position):
            await self.highrise.send_whisper(
                sender.id,
                "تحرك من مكانك الحالي أولًا، وبعدها جرّب السحب.",
            )
            return

        if not isinstance(target_position, Position):
            await self.highrise.send_whisper(
                sender.id,
                "ما أقدر أسحب هذا المستخدم من مكانه الحالي.",
            )
            return

        beside_sender = Position(
            x=sender_position.x + 0.75,
            y=sender_position.y,
            z=sender_position.z,
            facing=sender_position.facing,
        )

        await self.highrise.teleport(target.id, beside_sender)
        await self.highrise.chat(
            f"تم سحب @{target.username} بجانب المشرف."
        )

    async def _has_moderation_rights(self, user_id: str) -> bool:
        if self._is_room_owner(user_id):
            return True

        privileges = await self.highrise.get_room_privilege(user_id)
        if isinstance(privileges, Error):
            logger.warning(
                "Privilege check failed for a room user: %s",
                privileges.message,
            )
            return False

        return bool(privileges.moderator or privileges.designer)

    def _is_room_owner(self, user_id: str) -> bool:
        return user_id == self._room_owner_id

    @staticmethod
    def _format_duration(seconds: int) -> str:
        if seconds % 86400 == 0:
            return f"{seconds // 86400} يوم"
        if seconds % 3600 == 0:
            return f"{seconds // 3600} ساعة"
        if seconds % 60 == 0:
            return f"{seconds // 60} دقيقة"
        return f"{seconds} ثانية"

    @staticmethod
    def _usage(action: str) -> str:
        examples = {
            "summon": "مثال: !جيب @الاسم",
            "mute": "مثال: !كتم @الاسم 5m",
            "kick": "مثال: !طرد @الاسم",
            "promote": "مثال: !ترقية @الاسم",
            "demote": "مثال: !إزالة_مشرف @الاسم",
        }
        return examples[action]

    @staticmethod
    def _help_text() -> str:
        return (
            "أوامر الإدارة لمالك الغرفة والمشرفين والمصممين:\n"
            "!جيب @الاسم — يسحب المستخدم بجانبك\n"
            "!كتم @الاسم 5m — كتم لمدة محددة\n"
            "!طرد @الاسم — إخراج المستخدم من الروم\n"
            "مالك الغرفة فقط:\n"
            "!ترقية @الاسم — إضافة مشرف\n"
            "!إزالة_مشرف @الاسم — إزالة مشرف\n"
            "!البوت تشغيل / !البوت إيقاف — تشغيل أو إيقاف أوامر الإدارة\n"
            "المدة: s ثوانٍ، m أو د دقائق، h أو س ساعات، d أو ي أيام. "
            "أقصى مدة 7 أيام."
        )


def main_entry() -> None:
    token = os.environ.get("HIGHRISE_API_TOKEN")
    room_id = os.environ.get("HIGHRISE_ROOM_ID")

    if not token:
        raise SystemExit("Missing required secret: HIGHRISE_API_TOKEN")

    if not room_id:
        raise SystemExit("Missing required setting: HIGHRISE_ROOM_ID")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        asyncio.run(
            main(
                [
                    BotDefinition(
                        RoomModeratorBot(),
                        room_id,
                        token,
                    )
                ]
            )
        )
    except KeyboardInterrupt:
        logger.info("Bot stopped.")


if __name__ == "__main__":
    main_entry()
