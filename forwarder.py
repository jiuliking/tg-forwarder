import asyncio
import logging
import os
import shutil
import signal
import uuid
from pathlib import Path
from typing import Any, Optional

from aiohttp import web
from telethon import TelegramClient, events, utils
from telethon.errors import ChatForwardsRestrictedError, FloodWaitError
from telethon.tl.types import DocumentAttributeVideo, MessageMediaWebPage

from common import (
    AccountConfig,
    AppConfig,
    AuthStore,
    CompiledRule,
    DedupStore,
    HistoryStore,
    clean_message_text,
    load_config,
    load_env_file,
    load_raw_config,
    resolve_tg_api,
    session_prefix_for,
    should_skip_text,
    should_strip_all_text,
)
from web_server import WebConsole

BASE_DIR = Path(os.environ.get("TG_FWD_DIR") or Path(__file__).resolve().parent)
ENV_PATH = BASE_DIR / ".env"
CONFIG_PATH = BASE_DIR / "config.yaml"
SESSION_DIR = BASE_DIR / "session"
STATE_DB_PATH = BASE_DIR / "state.db"
UI_DIR = BASE_DIR / "ui"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("tg-forwarder")


class AccountRunner:
    def __init__(
        self,
        *,
        acct: AccountConfig,
        api_id: int,
        api_hash: str,
        manager: "ForwarderManager",
    ) -> None:
        self.account_id = acct.id
        self.account_name = acct.name
        self.enabled = acct.enabled
        self.manager = manager
        self.session_prefix = session_prefix_for(SESSION_DIR, acct.id)
        self.session_file = Path(f"{self.session_prefix}.session")
        self.client = TelegramClient(str(self.session_prefix), api_id, api_hash)

        self.source_map: dict[int, list[CompiledRule]] = {}
        self.target_cache: dict[int | str, Any] = {}
        self.chat_titles: dict[int, str] = {}
        self.chats_cache: list[dict[str, Any]] = []
        self._send_lock = asyncio.Lock()
        self._handlers_attached = False
        self.listening = False
        self._listen_task: Optional[asyncio.Task[None]] = None

    async def start(self) -> None:
        if not self.client.is_connected():
            await self.client.connect()
        if await self.client.is_user_authorized():
            await self.on_user_authorized()
        else:
            log.warning("[%s] Session not yet authorized; complete login via Web UI", self.account_name)

    async def stop(self) -> None:
        self.listening = False
        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
        if self.client.is_connected():
            await self.client.disconnect()

    async def on_user_authorized(self) -> None:
        me = await self.client.get_me()
        log.info("[%s] Logged in as %s (id=%s)", self.account_name, me.username or me.first_name, me.id)
        if not self._handlers_attached:
            @self.client.on(events.Album())
            async def _album_handler(event: events.Album.Event) -> None:
                await self.process_album(event)

            @self.client.on(events.NewMessage())
            async def _msg_handler(event: events.NewMessage.Event) -> None:
                await self.process_single(event)

            self._handlers_attached = True

        await self.refresh_routes(self.manager.cfg)
        self.listening = True
        if self._listen_task is None or self._listen_task.done():
            self._listen_task = asyncio.create_task(self.client.run_until_disconnected())
        log.info("[%s] Listening for updates...", self.account_name)

    @staticmethod
    def _entity_title(ent: Any, fallback: Any) -> str:
        t = getattr(ent, "title", None)
        if t:
            return str(t)
        u = getattr(ent, "username", None)
        if u:
            return f"@{u}"
        fn = " ".join(p for p in [getattr(ent, "first_name", None), getattr(ent, "last_name", None)] if p)
        return fn or str(fallback)

    async def refresh_routes(self, cfg: AppConfig) -> None:
        new_source_map: dict[int, list[CompiledRule]] = {}
        new_target_cache: dict[int | str, Any] = {}

        if self.enabled and await self.client.is_user_authorized():
            for rule in cfg.rules:
                if not rule.enabled or rule.account_id != self.account_id:
                    continue
                for src in rule.sources:
                    try:
                        ent = await self.client.get_entity(src)
                        peer_id = utils.get_peer_id(ent)
                        self.chat_titles[peer_id] = self._entity_title(ent, src)
                        new_source_map.setdefault(peer_id, []).append(rule)
                    except Exception as exc:
                        log.warning("[%s] Cannot resolve source %r in rule %r: %s", self.account_name, src, rule.name, exc)
                for tgt in rule.targets:
                    if tgt not in new_target_cache:
                        try:
                            ent = await self.client.get_entity(tgt)
                            peer_id = utils.get_peer_id(ent)
                            self.chat_titles[peer_id] = self._entity_title(ent, tgt)
                            new_target_cache[tgt] = await self.client.get_input_entity(ent)
                        except Exception as exc:
                            log.warning("[%s] Cannot resolve target %r in rule %r: %s", self.account_name, tgt, rule.name, exc)

        self.source_map = new_source_map
        self.target_cache = new_target_cache
        log.info(
            "[%s] Loaded %d active sources for account",
            self.account_name,
            len(self.source_map),
        )

    def _rules_for_chat(self, chat_id: int) -> list[CompiledRule]:
        return self.source_map.get(chat_id, [])

    async def _call_with_flood_retry(self, coro_factory):
        try:
            return await coro_factory()
        except FloodWaitError as exc:
            wait_s = exc.seconds + 1
            log.warning("[%s] FloodWait %ss; sleeping before retry", self.account_name, wait_s)
            await asyncio.sleep(wait_s)
            return await coro_factory()

    @staticmethod
    def _is_noforwards(chat: Any, msgs: list[Any]) -> bool:
        if getattr(chat, "noforwards", False):
            return True
        return any(getattr(m, "noforwards", False) for m in msgs)

    @staticmethod
    def _is_real_media(msg: Any) -> bool:
        return bool(msg.media) and not isinstance(msg.media, MessageMediaWebPage)

    @staticmethod
    def _file_size_mb(msg: Any) -> float:
        doc = getattr(msg, "document", None)
        if doc and getattr(doc, "size", None):
            return doc.size / (1024 * 1024)
        return 0.0

    @staticmethod
    def _extract_video_attrs(msg: Any) -> tuple[bool, Optional[list[Any]]]:
        doc = getattr(msg, "document", None)
        if not doc or not getattr(doc, "attributes", None):
            return False, None
        for attr in doc.attributes:
            if isinstance(attr, DocumentAttributeVideo):
                return True, [
                    DocumentAttributeVideo(
                        duration=attr.duration,
                        w=attr.w,
                        h=attr.h,
                        round_message=attr.round_message,
                        supports_streaming=True,
                        nosound=attr.nosound,
                    )
                ]
        return False, None

    async def _reupload_single(
        self, target_ent: Any, msg: Any, text: str, entities: Optional[list[Any]], rule: CompiledRule
    ) -> bool:
        cfg = self.manager.cfg
        if not self._is_real_media(msg):
            await self._call_with_flood_retry(
                lambda: self.client.send_message(
                    target_ent,
                    text,
                    formatting_entities=entities,
                    link_preview=(not rule.disable_link_preview) and isinstance(msg.media, MessageMediaWebPage),
                )
            )
            return True

        size_mb = self._file_size_mb(msg)
        if size_mb > cfg.max_download_mb:
            log.warning("Skip restricted msg %s: size %.1fMB > max_download_mb %dMB", msg.id, size_mb, cfg.max_download_mb)
            return False

        work_dir = cfg.temp_dir / f"job_{uuid.uuid4().hex[:12]}"
        work_dir.mkdir(parents=True, exist_ok=True)
        try:
            file_path = await self._call_with_flood_retry(
                lambda: self.client.download_media(msg, file=str(work_dir) + "/")
            )
            if not file_path:
                log.warning("Download returned empty for restricted msg %s", msg.id)
                return False

            is_video, video_attrs = self._extract_video_attrs(msg)
            thumb_path = None
            if is_video and getattr(msg.document, "thumbs", None):
                thumb_path = await self.client.download_media(
                    msg, thumb=-1, file=str(work_dir / "thumb.jpg")
                )

            await self._call_with_flood_retry(
                lambda: self.client.send_file(
                    target_ent,
                    file_path,
                    caption=text,
                    formatting_entities=entities,
                    supports_streaming=is_video,
                    attributes=video_attrs,
                    thumb=thumb_path,
                )
            )
            return True
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    async def _reupload_album(
        self,
        target_ent: Any,
        msgs: list[Any],
        captions: list[str],
        entities_list: list[list[Any]],
    ) -> bool:
        cfg = self.manager.cfg
        total_mb = sum(self._file_size_mb(m) for m in msgs)
        if total_mb > cfg.max_download_mb:
            log.warning("Skip restricted album (%d items): total %.1fMB > %dMB", len(msgs), total_mb, cfg.max_download_mb)
            return False

        work_dir = cfg.temp_dir / f"album_{uuid.uuid4().hex[:12]}"
        work_dir.mkdir(parents=True, exist_ok=True)
        try:
            paths: list[str] = []
            for m in msgs:
                p = await self._call_with_flood_retry(
                    lambda msg_obj=m: self.client.download_media(msg_obj, file=str(work_dir) + "/")
                )
                if p:
                    paths.append(p)
            if not paths:
                return False
            await self._call_with_flood_retry(
                lambda: self.client.send_file(
                    target_ent,
                    paths,
                    caption=captions[: len(paths)],
                    formatting_entities=entities_list[: len(paths)],
                    supports_streaming=True,
                )
            )
            return True
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    async def process_single(self, event: events.NewMessage.Event) -> None:
        if not self.enabled:
            return
        msg = event.message
        if msg.grouped_id is not None:
            return
        await self.manager.refresh_if_changed()
        source_id = event.chat_id
        rules = self._rules_for_chat(source_id)
        if not rules:
            return

        chat = await event.get_chat()
        source_title = self._entity_title(chat, source_id)
        self.chat_titles[source_id] = source_title
        raw_text = msg.message or ""
        orig_entities = list(msg.entities) if msg.entities else None
        has_real_media = self._is_real_media(msg)

        async with self._send_lock:
            for rule in rules:
                matched_skip = should_skip_text(raw_text, rule)
                if matched_skip:
                    log.info("[%s/%s] Skip msg %s (matched %r)", self.account_name, rule.name, msg.id, matched_skip)
                    self.manager.history.record(
                        account_id=self.account_id,
                        account_name=self.account_name,
                        rule_name=rule.name,
                        source_id=source_id,
                        source_title=source_title,
                        target_id=0,
                        target_title="-",
                        msg_id=msg.id,
                        item_count=1,
                        mode="skip_regex",
                        status="skipped",
                        preview=raw_text or "[媒体消息]",
                        detail=f"命中跳过正则: {matched_skip}",
                    )
                    continue

                if not has_real_media and rule.skip_text_only:
                    log.info("[%s/%s] Skip text-only msg %s", self.account_name, rule.name, msg.id)
                    self.manager.history.record(
                        account_id=self.account_id,
                        account_name=self.account_name,
                        rule_name=rule.name,
                        source_id=source_id,
                        source_title=source_title,
                        target_id=0,
                        target_title="-",
                        msg_id=msg.id,
                        item_count=1,
                        mode="skip_text_only",
                        status="skipped",
                        preview=raw_text or "[纯文字消息]",
                        detail="已开启跳过纯文字消息",
                    )
                    continue

                clear_caption = rule.strip_all_text or (should_strip_all_text(raw_text, rule) is not None)
                if clear_caption:
                    cleaned_text, cleaned_entities, changed = "", None, bool(raw_text)
                else:
                    cleaned_text, cleaned_entities, changed = clean_message_text(raw_text, orig_entities, rule)

                if not has_real_media and not cleaned_text.strip() and (rule.skip_empty_text_only or clear_caption):
                    log.info("[%s/%s] Skip empty/text-stripped msg %s", self.account_name, rule.name, msg.id)
                    self.manager.history.record(
                        account_id=self.account_id,
                        account_name=self.account_name,
                        rule_name=rule.name,
                        source_id=source_id,
                        source_title=source_title,
                        target_id=0,
                        target_title="-",
                        msg_id=msg.id,
                        item_count=1,
                        mode="skip_empty",
                        status="skipped",
                        preview=raw_text,
                        detail="去除文本后无媒体" if clear_caption else "清洗后为空文本",
                    )
                    continue

                for tgt in rule.targets:
                    target_ent = self.target_cache.get(tgt)
                    if target_ent is None:
                        continue
                    target_id = utils.get_peer_id(target_ent)
                    target_title = self.chat_titles.get(target_id, str(tgt))
                    if self.manager.dedup.seen(source_id, msg.id, target_id, account_id=self.account_id):
                        continue

                    mode = "copy"
                    try:
                        restricted = self._is_noforwards(chat, [msg])
                        if not restricted and (not changed or clear_caption) and rule.drop_author:
                            try:
                                await self._call_with_flood_retry(
                                    lambda: self.client.forward_messages(
                                        target_ent,
                                        msg.id,
                                        source_id,
                                        drop_author=True,
                                        drop_media_captions=clear_caption,
                                    )
                                )
                                mode = "cleaned" if clear_caption and changed else "copy"
                            except ChatForwardsRestrictedError:
                                restricted = True
                        elif not restricted:
                            try:
                                mode = "cleaned"
                                if has_real_media:
                                    await self._call_with_flood_retry(
                                        lambda: self.client.send_file(
                                            target_ent,
                                            msg.media,
                                            caption=cleaned_text,
                                            formatting_entities=cleaned_entities,
                                        )
                                    )
                                else:
                                    await self._call_with_flood_retry(
                                        lambda: self.client.send_message(
                                            target_ent,
                                            cleaned_text,
                                            formatting_entities=cleaned_entities,
                                            link_preview=(not rule.disable_link_preview)
                                            and isinstance(msg.media, MessageMediaWebPage),
                                        )
                                    )
                            except ChatForwardsRestrictedError:
                                restricted = True

                        if restricted:
                            if not rule.fallback_reupload:
                                self.manager.history.record(
                                    account_id=self.account_id,
                                    account_name=self.account_name,
                                    rule_name=rule.name,
                                    source_id=source_id,
                                    source_title=source_title,
                                    target_id=target_id,
                                    target_title=target_title,
                                    msg_id=msg.id,
                                    item_count=1,
                                    mode="skip_restricted",
                                    status="skipped",
                                    preview=cleaned_text or "[媒体消息]",
                                    detail="源频道禁止转发且未启用重传",
                                )
                                continue
                            mode = "reupload"
                            uploaded = await self._reupload_single(target_ent, msg, cleaned_text, cleaned_entities, rule)
                            if not uploaded:
                                self.manager.history.record(
                                    account_id=self.account_id,
                                    account_name=self.account_name,
                                    rule_name=rule.name,
                                    source_id=source_id,
                                    source_title=source_title,
                                    target_id=target_id,
                                    target_title=target_title,
                                    msg_id=msg.id,
                                    item_count=1,
                                    mode="skip_size",
                                    status="skipped",
                                    preview=cleaned_text or "[媒体消息]",
                                    detail="超出最大下载大小或下载为空",
                                )
                                continue

                        self.manager.dedup.mark(source_id, msg.id, target_id, account_id=self.account_id)
                        self.manager.history.record(
                            account_id=self.account_id,
                            account_name=self.account_name,
                            rule_name=rule.name,
                            source_id=source_id,
                            source_title=source_title,
                            target_id=target_id,
                            target_title=target_title,
                            msg_id=msg.id,
                            item_count=1,
                            mode=mode,
                            status="success",
                            preview=cleaned_text or "[媒体消息]",
                        )
                        log.info("[%s/%s] Forwarded msg %s (%s -> %s, mode=%s)", self.account_name, rule.name, msg.id, source_id, target_id, mode)
                    except Exception as exc:
                        log.exception("[%s/%s] Failed msg %s -> %s: %s", self.account_name, rule.name, msg.id, tgt, exc)
                        self.manager.history.record(
                            account_id=self.account_id,
                            account_name=self.account_name,
                            rule_name=rule.name,
                            source_id=source_id,
                            source_title=source_title,
                            target_id=target_id,
                            target_title=target_title,
                            msg_id=msg.id,
                            item_count=1,
                            mode=mode,
                            status="error",
                            preview=cleaned_text or raw_text or "[媒体消息]",
                            detail=str(exc),
                        )

                    if self.manager.cfg.delay_seconds > 0:
                        await asyncio.sleep(self.manager.cfg.delay_seconds)

    async def process_album(self, event: events.Album.Event) -> None:
        if not self.enabled:
            return
        msgs = list(event.messages)
        if not msgs:
            return
        await self.manager.refresh_if_changed()
        source_id = event.chat_id
        rules = self._rules_for_chat(source_id)
        if not rules:
            return

        chat = await event.get_chat()
        source_title = self._entity_title(chat, source_id)
        self.chat_titles[source_id] = source_title
        lead_id = msgs[0].id
        raw_preview = next((m.message for m in msgs if m.message), "") or f"[相册 {len(msgs)} 项]"

        async with self._send_lock:
            for rule in rules:
                skip_hit = None
                for m in msgs:
                    skip_hit = should_skip_text(m.message or "", rule)
                    if skip_hit:
                        break
                if skip_hit:
                    log.info("[%s/%s] Skip album %s (matched %r)", self.account_name, rule.name, lead_id, skip_hit)
                    self.manager.history.record(
                        account_id=self.account_id,
                        account_name=self.account_name,
                        rule_name=rule.name,
                        source_id=source_id,
                        source_title=source_title,
                        target_id=0,
                        target_title="-",
                        msg_id=lead_id,
                        item_count=len(msgs),
                        mode="skip_regex",
                        status="skipped",
                        preview=raw_preview,
                        detail=f"命中跳过正则: {skip_hit}",
                    )
                    continue

                clear_caption = rule.strip_all_text or any(
                    should_strip_all_text(m.message or "", rule) is not None for m in msgs
                )
                captions: list[str] = []
                entities_list: list[list[Any]] = []
                any_changed = False
                for m in msgs:
                    if clear_caption:
                        captions.append("")
                        entities_list.append([])
                        if m.message:
                            any_changed = True
                    else:
                        ct, ce, ch = clean_message_text(m.message or "", m.entities, rule)
                        captions.append(ct)
                        entities_list.append(ce or [])
                        if ch:
                            any_changed = True
                clean_preview = next((c for c in captions if c), "") or f"[相册 {len(msgs)} 项]"

                for tgt in rule.targets:
                    target_ent = self.target_cache.get(tgt)
                    if target_ent is None:
                        continue
                    target_id = utils.get_peer_id(target_ent)
                    target_title = self.chat_titles.get(target_id, str(tgt))
                    if self.manager.dedup.seen(source_id, lead_id, target_id, account_id=self.account_id):
                        continue

                    mode = "copy"
                    try:
                        restricted = self._is_noforwards(chat, msgs)
                        if not restricted and (not any_changed or clear_caption) and rule.drop_author:
                            try:
                                await self._call_with_flood_retry(
                                    lambda: self.client.forward_messages(
                                        target_ent,
                                        [m.id for m in msgs],
                                        source_id,
                                        drop_author=True,
                                        drop_media_captions=clear_caption,
                                        as_album=True,
                                    )
                                )
                                mode = "cleaned" if clear_caption and any_changed else "copy"
                            except ChatForwardsRestrictedError:
                                restricted = True
                        elif not restricted:
                            try:
                                mode = "cleaned"
                                media_list = [m.media for m in msgs if self._is_real_media(m)]
                                if len(media_list) == len(msgs):
                                    await self._call_with_flood_retry(
                                        lambda: self.client.send_file(
                                            target_ent,
                                            media_list,
                                            caption=captions,
                                            formatting_entities=entities_list,
                                        )
                                    )
                                else:
                                    restricted = True
                            except ChatForwardsRestrictedError:
                                restricted = True

                        if restricted:
                            if not rule.fallback_reupload:
                                self.manager.history.record(
                                    account_id=self.account_id,
                                    account_name=self.account_name,
                                    rule_name=rule.name,
                                    source_id=source_id,
                                    source_title=source_title,
                                    target_id=target_id,
                                    target_title=target_title,
                                    msg_id=lead_id,
                                    item_count=len(msgs),
                                    mode="skip_restricted",
                                    status="skipped",
                                    preview=clean_preview,
                                    detail="源频道禁止转发且未启用重传",
                                )
                                continue
                            mode = "reupload"
                            uploaded = await self._reupload_album(target_ent, msgs, captions, entities_list)
                            if not uploaded:
                                self.manager.history.record(
                                    account_id=self.account_id,
                                    account_name=self.account_name,
                                    rule_name=rule.name,
                                    source_id=source_id,
                                    source_title=source_title,
                                    target_id=target_id,
                                    target_title=target_title,
                                    msg_id=lead_id,
                                    item_count=len(msgs),
                                    mode="skip_size",
                                    status="skipped",
                                    preview=clean_preview,
                                    detail="超出最大下载大小或下载为空",
                                )
                                continue

                        for m in msgs:
                            self.manager.dedup.mark(source_id, m.id, target_id, account_id=self.account_id)
                        self.manager.history.record(
                            account_id=self.account_id,
                            account_name=self.account_name,
                            rule_name=rule.name,
                            source_id=source_id,
                            source_title=source_title,
                            target_id=target_id,
                            target_title=target_title,
                            msg_id=lead_id,
                            item_count=len(msgs),
                            mode=mode,
                            status="success",
                            preview=clean_preview,
                        )
                        log.info(
                            "[%s/%s] Forwarded album %s (%d items, %s -> %s, mode=%s)",
                            self.account_name,
                            rule.name,
                            lead_id,
                            len(msgs),
                            source_id,
                            target_id,
                            mode,
                        )
                    except Exception as exc:
                        log.exception("[%s/%s] Failed album %s -> %s: %s", self.account_name, rule.name, lead_id, tgt, exc)
                        self.manager.history.record(
                            account_id=self.account_id,
                            account_name=self.account_name,
                            rule_name=rule.name,
                            source_id=source_id,
                            source_title=source_title,
                            target_id=target_id,
                            target_title=target_title,
                            msg_id=lead_id,
                            item_count=len(msgs),
                            mode=mode,
                            status="error",
                            preview=clean_preview,
                            detail=str(exc),
                        )

                    if self.manager.cfg.delay_seconds > 0:
                        await asyncio.sleep(self.manager.cfg.delay_seconds)


class ForwarderManager:
    def __init__(self, api_id: int, api_hash: str) -> None:
        self.api_id = api_id
        self.api_hash = api_hash
        self.cfg: AppConfig = load_config(CONFIG_PATH)
        self.cfg_mtime: float = CONFIG_PATH.stat().st_mtime if CONFIG_PATH.exists() else 0.0
        self.dedup = DedupStore(STATE_DB_PATH)
        self.dedup.prune(self.cfg.dedup_ttl_hours)
        self.history = HistoryStore(
            STATE_DB_PATH,
            retention_days=self.cfg.log_retention_days,
            max_rows=self.cfg.log_max_rows,
        )
        self.history.prune()
        self.runners: dict[str, AccountRunner] = {}
        self._reload_lock = asyncio.Lock()

    async def reload_all(self, force: bool = False) -> None:
        async with self._reload_lock:
            try:
                mtime = CONFIG_PATH.stat().st_mtime
            except FileNotFoundError:
                return
            if not force and mtime == self.cfg_mtime and self.runners:
                return

            raw = load_raw_config(CONFIG_PATH)
            new_api_id, new_api_hash, _ = resolve_tg_api(raw)
            api_changed = (new_api_id != self.api_id) or (new_api_hash != self.api_hash)
            if api_changed and new_api_id and new_api_hash:
                log.info("Telegram API credentials changed; recreating account runners")
                for runner in list(self.runners.values()):
                    await runner.stop()
                self.runners.clear()
                self.api_id = new_api_id
                self.api_hash = new_api_hash

            new_cfg = load_config(CONFIG_PATH)
            new_cfg.temp_dir.mkdir(parents=True, exist_ok=True)
            self.cfg = new_cfg
            self.cfg_mtime = mtime
            self.history.retention_days = new_cfg.log_retention_days
            self.history.max_rows = new_cfg.log_max_rows
            self.history.prune()
            self.dedup.prune(new_cfg.dedup_ttl_hours)

            desired = {a.id: a for a in new_cfg.accounts}
            for old_id in list(self.runners.keys()):
                if old_id not in desired:
                    runner = self.runners.pop(old_id)
                    await runner.stop()

            for acct_id, acct in desired.items():
                runner = self.runners.get(acct_id)
                if runner is None:
                    runner = AccountRunner(
                        acct=acct,
                        api_id=self.api_id,
                        api_hash=self.api_hash,
                        manager=self,
                    )
                    self.runners[acct_id] = runner
                    if acct.enabled:
                        await runner.start()
                else:
                    was_enabled = runner.enabled
                    runner.account_name = acct.name
                    runner.enabled = acct.enabled
                    if acct.enabled and not was_enabled:
                        await runner.start()
                    elif not acct.enabled and was_enabled:
                        await runner.stop()
                    elif acct.enabled:
                        await runner.refresh_routes(self.cfg)

    async def refresh_if_changed(self) -> None:
        try:
            mtime = CONFIG_PATH.stat().st_mtime
        except FileNotFoundError:
            return
        if mtime != self.cfg_mtime:
            await self.reload_all(force=True)

    def get_runner(self, account_id: str) -> Optional[AccountRunner]:
        return self.runners.get(account_id)

    def all_chat_titles(self) -> dict[str, str]:
        merged: dict[str, str] = {}
        for r in self.runners.values():
            for k, v in r.chat_titles.items():
                merged[str(k)] = v
        return merged

    async def shutdown(self) -> None:
        for runner in list(self.runners.values()):
            await runner.stop()


async def main() -> None:
    load_env_file(ENV_PATH)
    raw = load_raw_config(CONFIG_PATH)
    api_id, api_hash, _ = resolve_tg_api(raw)
    listen_raw = os.environ.get("WEB_LISTEN", "127.0.0.1:28081")
    host, port_str = listen_raw.rsplit(":", 1)
    port = int(port_str)

    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(SESSION_DIR, 0o700)

    manager = ForwarderManager(api_id=api_id, api_hash=api_hash)
    auth = AuthStore(STATE_DB_PATH)
    if auth.generated_password:
        log.warning("Generated web admin password; change it after login password=%s", auth.generated_password)
    log.info("Starting web server on %s:%d", host, port)
    log.info("Web UI available at http://%s:%d/?token=%s", host, port, auth.get_access_token())

    console = WebConsole(
        manager=manager,
        auth=auth,
        config_path=CONFIG_PATH,
        session_dir=SESSION_DIR,
        ui_dir=UI_DIR,
    )
    app = console.build_app()
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    await manager.reload_all(force=True)

    loop = asyncio.get_running_loop()

    def _on_sighup() -> None:
        log.info("Received SIGHUP; scheduling config reload")
        loop.create_task(manager.reload_all(force=True))

    try:
        loop.add_signal_handler(signal.SIGHUP, _on_sighup)
    except NotImplementedError:
        pass

    stop_event = asyncio.Event()

    def _on_stop() -> None:
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _on_stop)
        except NotImplementedError:
            pass

    await stop_event.wait()
    await runner.cleanup()
    await manager.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
