import asyncio
import logging
import os
import shutil
import signal
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from aiohttp import web
from telethon import TelegramClient, events, utils
from telethon.errors import (
    ChannelPrivateError,
    ChatForwardsRestrictedError,
    ChatInvalidError,
    FloodWaitError,
    MsgIdInvalidError,
    RPCError,
    UnauthorizedError,
)
from telethon.tl.types import (
    DocumentAttributeAnimated,
    DocumentAttributeAudio,
    DocumentAttributeSticker,
    DocumentAttributeVideo,
    InputMediaUploadedDocument,
    MessageMediaDocument,
    MessageMediaPhoto,
    MessageMediaWebPage,
)

from common import (
    AccountConfig,
    AppConfig,
    AuthStore,
    CompiledRule,
    DedupStore,
    HistoryStore,
    build_media_chunks,
    clean_message_text,
    load_config,
    load_env_file,
    load_raw_config,
    resolve_tg_api,
    session_prefix_for,
    should_skip_text,
    should_strip_all_text,
    truncate_text_with_entities,
    utf16_len,
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
        self.api_id = api_id
        self.api_hash = api_hash
        self.manager = manager
        self.session_prefix = session_prefix_for(SESSION_DIR, acct.id)
        self.session_file = Path(f"{self.session_prefix}.session")
        self.client = self._build_client()

        self.source_map: dict[int, list[CompiledRule]] = {}
        self.target_cache: dict[int | str, Any] = {}
        self.chat_titles: dict[int, str] = {}
        self.chats_cache: list[dict[str, Any]] = []
        self._send_lock = asyncio.Lock()
        self._auth_lock = asyncio.Lock()
        self._bg_tasks: set[asyncio.Task[Any]] = set()
        self._handlers_attached = False
        self.listening = False
        self._listen_task: Optional[asyncio.Task[None]] = None

    def _build_client(self) -> TelegramClient:
        client = TelegramClient(str(self.session_prefix), self.api_id, self.api_hash)
        client.parse_mode = None
        return client

    async def start(self) -> None:
        if not self.client.is_connected():
            await self.client.connect()
        if await self.ensure_authorized(verify_live=True):
            await self.on_user_authorized()
        else:
            log.warning("[%s] Session not yet authorized; complete login via Web UI", self.account_name)

    def mark_unauthorized(self) -> None:
        self.listening = False
        self.source_map = {}
        self.target_cache = {}
        self.chats_cache = []
        try:
            self.client._authorized = False
        except Exception:
            pass

    async def ensure_authorized(self, verify_live: bool = False) -> bool:
        if not self.client.is_connected():
            await self.client.connect()
        if verify_live:
            try:
                self.client._authorized = None
            except Exception:
                pass
        ok = await self.client.is_user_authorized()
        if not ok:
            self.mark_unauthorized()
            return False
        if verify_live:
            me = await self.client.get_me()
            if me is None:
                self.mark_unauthorized()
                return False
        return True

    async def _run_listener(self) -> None:
        try:
            await self.client.run_until_disconnected()
        except asyncio.CancelledError:
            raise
        except UnauthorizedError as exc:
            log.error("[%s] Telegram session revoked/unauthorized (%s); please re-login via Web UI", self.account_name, exc)
            self.mark_unauthorized()
        except Exception as exc:
            log.warning("[%s] Listener stopped: %s", self.account_name, exc)
            self.listening = False
        else:
            self.listening = False

    async def stop(self) -> None:
        self.listening = False
        pending: list[asyncio.Task[Any]] = []
        for t in list(self._bg_tasks):
            if not t.done():
                t.cancel()
                pending.append(t)
        self._bg_tasks.clear()
        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
            pending.append(self._listen_task)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._listen_task = None
        if self.client and self.client.is_connected():
            await self.client.disconnect()

    async def logout(self) -> None:
        self.listening = False
        pending: list[asyncio.Task[Any]] = []
        for t in list(self._bg_tasks):
            if not t.done():
                t.cancel()
                pending.append(t)
        self._bg_tasks.clear()
        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
            pending.append(self._listen_task)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._listen_task = None

        try:
            if self.client and getattr(self.client, "session", None) is not None:
                if not self.client.is_connected():
                    await self.client.connect()
                if await self.client.is_user_authorized():
                    await self.client.log_out()
                elif self.client.is_connected():
                    await self.client.disconnect()
        except Exception as exc:
            log.warning("[%s] Logout warning: %s", self.account_name, exc)
            try:
                if self.client and self.client.is_connected():
                    await self.client.disconnect()
            except Exception:
                pass

        self.mark_unauthorized()
        for suffix in (".session", ".session-journal", ".session-wal", ".session-shm"):
            Path(f"{self.session_prefix}{suffix}").unlink(missing_ok=True)
        self._handlers_attached = False
        self.client = self._build_client()
        if self.enabled:
            try:
                await self.client.connect()
            except Exception as exc:
                log.warning("[%s] Reconnect after logout warning: %s", self.account_name, exc)

    async def on_user_authorized(self) -> None:
        async with self._auth_lock:
            me = await self.client.get_me()
            if me is None:
                self.mark_unauthorized()
                log.warning("[%s] Session not authorized (get_me returned None)", self.account_name)
                return
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
                self._listen_task = asyncio.create_task(self._run_listener())
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

        if self.enabled and await self.ensure_authorized(verify_live=False):
            for rule in cfg.rules:
                if not rule.enabled or rule.account_id != self.account_id:
                    continue
                for src in rule.sources:
                    try:
                        ent = await self.client.get_entity(src)
                        peer_id = utils.get_peer_id(ent)
                        self.chat_titles[peer_id] = self._entity_title(ent, src)
                        lst = new_source_map.setdefault(peer_id, [])
                        if rule not in lst:
                            lst.append(rule)
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

    async def _call_with_flood_retry(self, coro_factory, max_attempts: int = 3):
        for attempt in range(max_attempts):
            try:
                return await coro_factory()
            except FloodWaitError as exc:
                if attempt + 1 >= max_attempts:
                    raise
                wait_s = exc.seconds + 1
                log.warning("[%s] FloodWait %ss; sleeping before retry (%d/%d)", self.account_name, wait_s, attempt + 1, max_attempts)
                await asyncio.sleep(wait_s)

    @staticmethod
    def _is_noforwards(chat: Any, msgs: list[Any]) -> bool:
        if getattr(chat, "noforwards", False):
            return True
        return any(getattr(m, "noforwards", False) for m in msgs)

    @staticmethod
    def _is_real_media(msg: Any) -> bool:
        return bool(msg.media) and not isinstance(msg.media, MessageMediaWebPage)

    @staticmethod
    def _media_bucket(msg: Any) -> str:
        media = getattr(msg, "media", None)
        if isinstance(media, MessageMediaPhoto):
            return "pv"
        if isinstance(media, MessageMediaDocument):
            doc = getattr(media, "document", None)
            mime = str(getattr(doc, "mime_type", "") or "").lower()
            attrs = getattr(doc, "attributes", None) or []
            if any(
                isinstance(a, (DocumentAttributeSticker, DocumentAttributeAnimated))
                or (isinstance(a, DocumentAttributeAudio) and getattr(a, "voice", False))
                or (isinstance(a, DocumentAttributeVideo) and getattr(a, "round_message", False))
                for a in attrs
            ):
                return "single"
            if any(isinstance(a, DocumentAttributeAudio) for a in attrs):
                return "audio"
            if mime.startswith("video/") or any(isinstance(a, DocumentAttributeVideo) for a in attrs):
                return "pv"
            return "doc"
        return "single"

    @staticmethod
    def _is_channel_sender(msg: Any, source_id: int) -> bool:
        if getattr(msg, "action", None) is not None:
            return False
        sender_id = getattr(msg, "sender_id", None)
        if sender_id == source_id:
            return True
        from_id = getattr(msg, "from_id", None)
        if from_id is not None:
            try:
                pid = utils.get_peer_id(from_id)
                if pid == source_id:
                    return True
                peer_id = getattr(msg, "peer_id", None)
                if peer_id is not None and pid == utils.get_peer_id(peer_id):
                    return True
                return False
            except Exception:
                return False
        return sender_id is None

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
            if not (text or "").strip():
                log.warning("Skip empty text-only restricted msg %s", msg.id)
                return False
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

        cap, ents = truncate_text_with_entities(text, entities, 1024)
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
                    caption=cap,
                    formatting_entities=ents,
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
            items: list[tuple[Any, str, list[Any]]] = []
            dropped_caption: Optional[tuple[str, list[Any]]] = None
            for idx, m in enumerate(msgs):
                raw_cap = captions[idx] if idx < len(captions) else ""
                raw_ents = entities_list[idx] if idx < len(entities_list) else []
                cap, ents = truncate_text_with_entities(raw_cap, raw_ents, 1024)
                p = await self._call_with_flood_retry(
                    lambda msg_obj=m: self.client.download_media(msg_obj, file=str(work_dir) + "/")
                )
                if not p:
                    if cap and dropped_caption is None:
                        dropped_caption = (cap, ents or [])
                    continue
                is_video, video_attrs = self._extract_video_attrs(m)
                if is_video and video_attrs:
                    uploaded = await self._call_with_flood_retry(
                        lambda fp=p: self.client.upload_file(fp)
                    )
                    uploaded_thumb = None
                    if getattr(m.document, "thumbs", None):
                        tpath = await self.client.download_media(
                            m, thumb=-1, file=str(work_dir / f"thumb_{idx}.jpg")
                        )
                        if tpath:
                            uploaded_thumb = await self._call_with_flood_retry(
                                lambda tp=tpath: self.client.upload_file(tp)
                            )
                    mime = str(getattr(m.document, "mime_type", "") or "video/mp4")
                    media_obj: Any = InputMediaUploadedDocument(
                        file=uploaded,
                        thumb=uploaded_thumb,
                        mime_type=mime,
                        attributes=video_attrs,
                    )
                    items.append((media_obj, cap, ents or []))
                else:
                    items.append((p, cap, ents or []))

            if not items:
                return False
            if dropped_caption is not None and not any(c for _, c, _ in items):
                m0, _, _ = items[0]
                items[0] = (m0, dropped_caption[0], dropped_caption[1])

            if len(items) == 1:
                media0, cap0, ent0 = items[0]
                await self._call_with_flood_retry(
                    lambda: self.client.send_file(
                        target_ent,
                        media0,
                        caption=cap0,
                        formatting_entities=ent0 or None,
                        supports_streaming=True,
                    )
                )
                return True

            files_out = [x[0] for x in items]
            caps_out = [x[1] for x in items]
            ents_out = [x[2] for x in items]
            await self._call_with_flood_retry(
                lambda: self.client.send_file(
                    target_ent,
                    files_out,
                    caption=caps_out,
                    formatting_entities=ents_out,
                    supports_streaming=True,
                )
            )
            return True
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    def _schedule_comment_forward(
        self,
        chat: Any,
        source_id: int,
        source_title: str,
        main_msgs: list[Any],
        rule: CompiledRule,
    ) -> None:
        task = asyncio.create_task(
            self._process_with_comments(chat, source_id, source_title, main_msgs, rule)
        )
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _fetch_channel_comment_batches(
        self,
        source_ent: Any,
        source_id: int,
        thread_msg_id: int,
    ) -> tuple[list[list[Any]], str, Optional[list[Any]], float]:
        batches: list[list[Any]] = []
        group_idx: dict[int, int] = {}
        first_text = ""
        first_entities: Optional[list[Any]] = None
        latest_ts = 0.0

        try:
            async for cm in self.client.iter_messages(source_ent, reply_to=thread_msg_id, reverse=True, limit=200):
                if cm.id == thread_msg_id:
                    continue
                if not self._is_channel_sender(cm, source_id):
                    continue
                msg_ts = cm.date.timestamp() if getattr(cm, "date", None) else 0.0
                if msg_ts > latest_ts:
                    latest_ts = msg_ts
                if not first_text and (cm.message or "").strip():
                    first_text = cm.message
                    first_entities = list(cm.entities) if cm.entities else None
                if not self._is_real_media(cm):
                    continue
                gid = getattr(cm, "grouped_id", None)
                if gid is not None:
                    if gid in group_idx:
                        batches[group_idx[gid]].append(cm)
                    else:
                        group_idx[gid] = len(batches)
                        batches.append([cm])
                else:
                    batches.append([cm])
        except (MsgIdInvalidError, ChatInvalidError, ChannelPrivateError) as exc:
            log.debug("[%s] No discussion replies for %s/%s: %s", self.account_name, source_id, thread_msg_id, exc)
        except RPCError as exc:
            log.warning("[%s] Failed fetching comments for %s/%s: %s", self.account_name, source_id, thread_msg_id, exc)

        return batches, first_text, first_entities, latest_ts

    async def _process_with_comments(
        self,
        chat: Any,
        source_id: int,
        source_title: str,
        main_msgs: list[Any],
        rule: CompiledRule,
        *,
        skip_wait: bool = False,
        target_override: Optional[tuple[Any, int, str]] = None,
        detail_prefix: str = "",
    ) -> tuple[bool, str]:
        if not self.enabled or not main_msgs:
            return False, "账号未启用或消息为空"
        lead_id = main_msgs[0].id
        thread_msg_id = next((m.id for m in main_msgs if getattr(m, "replies", None)), lead_id)

        if not skip_wait:
            wait_s = max(5, rule.comment_wait_seconds)
            start_ts = time.time()
            max_deadline = start_ts + wait_s + 60.0

            await asyncio.sleep(wait_s)
            if not self.enabled:
                return False, "账号已停止"

            comment_batches, fb_text, fb_entities, latest_ts = await self._fetch_channel_comment_batches(
                chat, source_id, thread_msg_id
            )
            while latest_ts > 0 and (time.time() - latest_ts) < 10.0 and time.time() < max_deadline:
                await asyncio.sleep(5)
                if not self.enabled:
                    return False, "账号已停止"
                comment_batches, fb_text, fb_entities, latest_ts = await self._fetch_channel_comment_batches(
                    chat, source_id, thread_msg_id
                )
        else:
            comment_batches, fb_text, fb_entities, _ = await self._fetch_channel_comment_batches(
                chat, source_id, thread_msg_id
            )

        main_text_msg = next((m for m in main_msgs if (m.message or "").strip()), None)
        if main_text_msg is not None:
            chosen_text = main_text_msg.message or ""
            chosen_entities = list(main_text_msg.entities) if main_text_msg.entities else None
        elif fb_text:
            chosen_text = fb_text
            chosen_entities = fb_entities
        else:
            chosen_text = ""
            chosen_entities = None

        main_media = [m for m in main_msgs if self._is_real_media(m)]
        all_batches = ([main_media] if main_media else []) + comment_batches
        total_media = sum(len(b) for b in all_batches)
        raw_preview = chosen_text or (f"[媒体 {total_media} 项]" if total_media else "[纯文字消息]")

        async with self._send_lock:
            if target_override is None:
                matched_skip = should_skip_text(chosen_text, rule)
                if matched_skip:
                    log.info("[%s/%s] Skip post+comments %s (matched %r)", self.account_name, rule.name, lead_id, matched_skip)
                    self.manager.history.record(
                        account_id=self.account_id,
                        account_name=self.account_name,
                        rule_name=rule.name,
                        source_id=source_id,
                        source_title=source_title,
                        target_id=0,
                        target_title="-",
                        msg_id=lead_id,
                        item_count=max(1, total_media),
                        mode="skip_regex",
                        status="skipped",
                        preview=raw_preview,
                        detail=f"命中跳过正则: {matched_skip}",
                    )
                    return False, f"命中跳过正则: {matched_skip}"

                if total_media == 0 and rule.skip_text_only:
                    log.info("[%s/%s] Skip text-only msg %s (no media in comments)", self.account_name, rule.name, lead_id)
                    self.manager.history.record(
                        account_id=self.account_id,
                        account_name=self.account_name,
                        rule_name=rule.name,
                        source_id=source_id,
                        source_title=source_title,
                        target_id=0,
                        target_title="-",
                        msg_id=lead_id,
                        item_count=1,
                        mode="skip_text_only",
                        status="skipped",
                        preview=raw_preview,
                        detail="主帖与评论区均无媒体，已跳过纯文字消息",
                    )
                    return False, "主帖与评论区均无媒体"

            clear_caption = rule.strip_all_text or (should_strip_all_text(chosen_text, rule) is not None)
            if clear_caption:
                cleaned_text, cleaned_entities = "", None
            else:
                cleaned_text, cleaned_entities, _ = clean_message_text(chosen_text, chosen_entities, rule)

            if total_media == 0 and not cleaned_text.strip():
                if target_override is not None:
                    return False, "清洗后为空文本，无法发送"
                if rule.skip_empty_text_only or clear_caption:
                    self.manager.history.record(
                        account_id=self.account_id,
                        account_name=self.account_name,
                        rule_name=rule.name,
                        source_id=source_id,
                        source_title=source_title,
                        target_id=0,
                        target_title="-",
                        msg_id=lead_id,
                        item_count=1,
                        mode="skip_empty",
                        status="skipped",
                        preview=raw_preview,
                        detail="清洗后为空文本",
                    )
                    return False, "清洗后为空文本"
                return False, "清洗后为空文本，无法发送"

            chunks = build_media_chunks(all_batches, rule.comment_merge_mode, self._media_bucket, max_album=10)
            clean_preview = cleaned_text or (f"[评论区合并 {total_media} 项]" if comment_batches else f"[媒体 {total_media} 项]")

            target_list: list[tuple[Any, int, str]] = []
            if target_override is not None:
                target_list.append(target_override)
            else:
                for tgt in rule.targets:
                    te = self.target_cache.get(tgt)
                    if te is None:
                        continue
                    tid = utils.get_peer_id(te)
                    ttitle = self.chat_titles.get(tid, str(tgt))
                    if self.manager.dedup.seen(source_id, lead_id, tid, account_id=self.account_id):
                        continue
                    target_list.append((te, tid, ttitle))

            last_ok = False
            last_msg = "无可用目标频道"
            for target_ent, target_id, target_title in target_list:
                mode = "comments_batch" if rule.comment_merge_mode == "keep_batches" else "comments_pack"
                try:
                    if total_media == 0:
                        lead_msg = main_msgs[0]
                        if self._is_noforwards(chat, [lead_msg]):
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
                                    item_count=1,
                                    mode="skip_restricted",
                                    status="skipped",
                                    preview=clean_preview,
                                    detail="源频道禁止转发且未启用重传",
                                )
                                last_ok, last_msg = False, "源频道禁止转发且未启用重传"
                                continue
                        await self._call_with_flood_retry(
                            lambda: self.client.send_message(
                                target_ent,
                                cleaned_text,
                                formatting_entities=cleaned_entities,
                                link_preview=(not rule.disable_link_preview)
                                and isinstance(lead_msg.media, MessageMediaWebPage),
                            )
                        )
                        mode = "cleaned"
                    else:
                        any_reuploaded = False
                        aborted = False
                        split_long_text = utf16_len(cleaned_text) > 1024
                        if split_long_text:
                            lead_msg = main_msgs[0]
                            await self._call_with_flood_retry(
                                lambda: self.client.send_message(
                                    target_ent,
                                    cleaned_text,
                                    formatting_entities=cleaned_entities,
                                    link_preview=(not rule.disable_link_preview)
                                    and isinstance(lead_msg.media, MessageMediaWebPage),
                                )
                            )
                            await asyncio.sleep(min(1.5, max(0.5, self.manager.cfg.delay_seconds or 1.0)))
                        for c_idx, chunk in enumerate(chunks):
                            if c_idx > 0:
                                await asyncio.sleep(min(2.0, max(0.8, self.manager.cfg.delay_seconds or 1.0)))
                            if c_idx == 0 and not split_long_text:
                                cap, ents = truncate_text_with_entities(cleaned_text, cleaned_entities, 1024)
                            else:
                                cap, ents = "", None
                            restricted = self._is_noforwards(chat, chunk)
                            if not restricted:
                                try:
                                    if len(chunk) == 1:
                                        await self._call_with_flood_retry(
                                            lambda m0=chunk[0], cp=cap, en=ents: self.client.send_file(
                                                target_ent,
                                                m0.media,
                                                caption=cp,
                                                formatting_entities=en,
                                            )
                                        )
                                    else:
                                        caps = [cap] + [""] * (len(chunk) - 1)
                                        ents_l = [ents or []] + [[] for _ in chunk[1:]]
                                        medias = [m.media for m in chunk]
                                        await self._call_with_flood_retry(
                                            lambda ml=medias, cps=caps, el=ents_l: self.client.send_file(
                                                target_ent,
                                                ml,
                                                caption=cps,
                                                formatting_entities=el,
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
                                        msg_id=lead_id,
                                        item_count=total_media,
                                        mode="skip_restricted",
                                        status="skipped",
                                        preview=clean_preview,
                                        detail="源频道禁止转发且未启用重传",
                                    )
                                    last_ok, last_msg = False, "源频道禁止转发且未启用重传"
                                    aborted = True
                                    break
                                any_reuploaded = True
                                if len(chunk) == 1:
                                    ok = await self._reupload_single(target_ent, chunk[0], cap, ents, rule)
                                else:
                                    caps = [cap] + [""] * (len(chunk) - 1)
                                    ents_l = [ents or []] + [[] for _ in chunk[1:]]
                                    ok = await self._reupload_album(target_ent, chunk, caps, ents_l)
                                if not ok:
                                    self.manager.history.record(
                                        account_id=self.account_id,
                                        account_name=self.account_name,
                                        rule_name=rule.name,
                                        source_id=source_id,
                                        source_title=source_title,
                                        target_id=target_id,
                                        target_title=target_title,
                                        msg_id=lead_id,
                                        item_count=total_media,
                                        mode="skip_size",
                                        status="skipped",
                                        preview=clean_preview,
                                        detail="超出最大下载大小或下载为空",
                                    )
                                    last_ok, last_msg = False, "超出最大下载大小或下载为空"
                                    aborted = True
                                    break
                        if aborted:
                            continue
                        if any_reuploaded:
                            mode = "reupload"

                    for m in main_msgs:
                        self.manager.dedup.mark(source_id, m.id, target_id, account_id=self.account_id)
                    cmt_note = f"含评论区媒体 {sum(len(b) for b in comment_batches)} 项（{len(chunks)} 组）" if comment_batches else ""
                    detail_txt = " · ".join(p for p in [detail_prefix, cmt_note] if p)
                    self.manager.history.record(
                        account_id=self.account_id,
                        account_name=self.account_name,
                        rule_name=rule.name,
                        source_id=source_id,
                        source_title=source_title,
                        target_id=target_id,
                        target_title=target_title,
                        msg_id=lead_id,
                        item_count=max(1, total_media),
                        mode=mode,
                        status="success",
                        preview=clean_preview,
                        detail=detail_txt,
                    )
                    log.info(
                        "[%s/%s] Forwarded post %s (+%d comment items in %d chunks, %s -> %s, mode=%s)",
                        self.account_name,
                        rule.name,
                        lead_id,
                        sum(len(b) for b in comment_batches),
                        len(chunks),
                        source_id,
                        target_id,
                        mode,
                    )
                    last_ok, last_msg = True, mode
                except Exception as exc:
                    log.exception("[%s/%s] Failed post+comments %s -> %s: %s", self.account_name, rule.name, lead_id, target_id, exc)
                    self.manager.history.record(
                        account_id=self.account_id,
                        account_name=self.account_name,
                        rule_name=rule.name,
                        source_id=source_id,
                        source_title=source_title,
                        target_id=target_id,
                        target_title=target_title,
                        msg_id=lead_id,
                        item_count=max(1, total_media),
                        mode=mode,
                        status="error",
                        preview=clean_preview,
                        detail=str(exc),
                    )
                    last_ok, last_msg = False, str(exc)
                if self.manager.cfg.delay_seconds > 0:
                    await asyncio.sleep(self.manager.cfg.delay_seconds)
            return last_ok, last_msg

    async def _deliver_single_to_target(
        self,
        chat: Any,
        source_id: int,
        source_title: str,
        msg: Any,
        rule: CompiledRule,
        target_ent: Any,
        target_id: int,
        target_title: str,
        cleaned_text: str,
        cleaned_entities: Optional[list[Any]],
        changed: bool,
        clear_caption: bool,
        has_real_media: bool,
        detail_note: str = "",
    ) -> tuple[bool, str]:
        mode = "copy"
        try:
            restricted = self._is_noforwards(chat, [msg])
            needs_strip_preview = (not has_real_media) and rule.disable_link_preview and isinstance(msg.media, MessageMediaWebPage)
            can_server_forward = (
                not restricted
                and not needs_strip_preview
                and ((not changed and not clear_caption) or (clear_caption and rule.drop_author))
            )
            if can_server_forward:
                try:
                    await self._call_with_flood_retry(
                        lambda: self.client.forward_messages(
                            target_ent,
                            msg.id,
                            source_id,
                            drop_author=rule.drop_author,
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
                        safe_cap, safe_ents = truncate_text_with_entities(cleaned_text, cleaned_entities, 1024)
                        await self._call_with_flood_retry(
                            lambda: self.client.send_file(
                                target_ent,
                                msg.media,
                                caption=safe_cap,
                                formatting_entities=safe_ents,
                            )
                        )
                    else:
                        if not cleaned_text.strip():
                            return False, "清洗后为空文本，无法发送"
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
                    return False, "源频道禁止转发且未启用重传"
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
                    return False, "超出最大下载大小或下载为空"

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
                detail=detail_note,
            )
            log.info("[%s/%s] Forwarded msg %s (%s -> %s, mode=%s)", self.account_name, rule.name, msg.id, source_id, target_id, mode)
            return True, mode
        except Exception as exc:
            log.exception("[%s/%s] Failed msg %s -> %s: %s", self.account_name, rule.name, msg.id, target_id, exc)
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
                preview=cleaned_text or (msg.message or "") or "[媒体消息]",
                detail=str(exc),
            )
            return False, str(exc)

    async def _deliver_album_to_target(
        self,
        chat: Any,
        source_id: int,
        source_title: str,
        msgs: list[Any],
        rule: CompiledRule,
        target_ent: Any,
        target_id: int,
        target_title: str,
        captions: list[str],
        entities_list: list[list[Any]],
        any_changed: bool,
        clear_caption: bool,
        clean_preview: str,
        detail_note: str = "",
    ) -> tuple[bool, str]:
        lead_id = msgs[0].id
        mode = "copy"
        try:
            restricted = self._is_noforwards(chat, msgs)
            can_server_forward = (
                not restricted
                and ((not any_changed and not clear_caption) or (clear_caption and rule.drop_author))
            )
            if can_server_forward:
                try:
                    await self._call_with_flood_retry(
                        lambda: self.client.forward_messages(
                            target_ent,
                            [m.id for m in msgs],
                            source_id,
                            drop_author=rule.drop_author,
                            drop_media_captions=clear_caption,
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
                        safe_caps: list[str] = []
                        safe_ents: list[list[Any]] = []
                        for c_txt, c_ent in zip(captions, entities_list):
                            sc, se = truncate_text_with_entities(c_txt, c_ent, 1024)
                            safe_caps.append(sc)
                            safe_ents.append(se or [])
                        await self._call_with_flood_retry(
                            lambda: self.client.send_file(
                                target_ent,
                                media_list,
                                caption=safe_caps,
                                formatting_entities=safe_ents,
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
                    return False, "源频道禁止转发且未启用重传"
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
                    return False, "超出最大下载大小或下载为空"

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
                detail=detail_note,
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
            return True, mode
        except Exception as exc:
            log.exception("[%s/%s] Failed album %s -> %s: %s", self.account_name, rule.name, lead_id, target_id, exc)
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
            return False, str(exc)

    def _find_rule_for_history(self, item: dict[str, Any], source_id: int) -> CompiledRule:
        rule_name = str(item.get("rule_name") or "")
        for r in self.manager.cfg.rules:
            if r.account_id == self.account_id and r.name == rule_name:
                return r
        for r in self.manager.cfg.rules:
            if r.account_id == self.account_id and source_id in r.sources:
                return r
        mapped = self.source_map.get(source_id) or []
        if mapped:
            return mapped[0]
        mode = str(item.get("mode") or "")
        return CompiledRule(
            name=rule_name or "手动重发",
            enabled=True,
            account_id=self.account_id,
            sources=[source_id],
            targets=[int(item["target_id"])],
            drop_author=True,
            fallback_reupload=True,
            collect_comments=mode in ("comments_pack", "comments_batch"),
            comment_wait_seconds=self.manager.cfg.rules[0].comment_wait_seconds if self.manager.cfg.rules else 30,
            comment_merge_mode="keep_batches" if mode == "comments_batch" else "pack",
        )

    async def resend_history_item(self, item: dict[str, Any]) -> tuple[bool, str]:
        source_id = int(item["source_id"])
        target_id = int(item["target_id"])
        msg_id = int(item["msg_id"])
        if target_id == 0:
            return False, "该记录没有目标频道，无法重发"
        if not await self.ensure_authorized(verify_live=True):
            return False, "对应账号尚未登录 Telegram（或会话已失效，请重新登录）"

        try:
            source_ent = await self.client.get_entity(source_id)
            target_ent = self.target_cache.get(target_id) or await self.client.get_entity(target_id)
        except Exception as exc:
            return False, f"无法访问源/目标频道: {exc}"

        source_title = self._entity_title(source_ent, item.get("source_title") or source_id)
        target_title = self._entity_title(target_ent, item.get("target_title") or target_id)

        lead_msg = await self.client.get_messages(source_ent, ids=msg_id)
        if lead_msg is None:
            return False, "源频道原消息已被删除或无法读取"

        main_msgs: list[Any] = [lead_msg]
        gid = getattr(lead_msg, "grouped_id", None)
        if gid is not None:
            cand_ids = list(range(max(1, msg_id - 9), msg_id + 10))
            cands = await self.client.get_messages(source_ent, ids=cand_ids)
            grouped = [m for m in (cands or []) if m is not None and getattr(m, "grouped_id", None) == gid]
            if grouped:
                grouped.sort(key=lambda m: m.id)
                main_msgs = grouped

        rule = self._find_rule_for_history(item, source_id)
        hist_mode = str(item.get("mode") or "")
        is_broadcast = bool(getattr(source_ent, "broadcast", False))
        if (rule.collect_comments and is_broadcast) or (hist_mode in ("comments_pack", "comments_batch") and is_broadcast):
            return await self._process_with_comments(
                source_ent,
                source_id,
                source_title,
                main_msgs,
                rule,
                skip_wait=True,
                target_override=(target_ent, target_id, target_title),
                detail_prefix="手动重发",
            )

        async with self._send_lock:
            if len(main_msgs) > 1:
                clear_caption = rule.strip_all_text or any(
                    should_strip_all_text(m.message or "", rule) is not None for m in main_msgs
                )
                captions: list[str] = []
                entities_list: list[list[Any]] = []
                any_changed = False
                for m in main_msgs:
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
                clean_preview = next((c for c in captions if c), "") or f"[相册 {len(main_msgs)} 项]"
                res = await self._deliver_album_to_target(
                    source_ent,
                    source_id,
                    source_title,
                    main_msgs,
                    rule,
                    target_ent,
                    target_id,
                    target_title,
                    captions,
                    entities_list,
                    any_changed,
                    clear_caption,
                    clean_preview,
                    detail_note="手动重发",
                )
                if self.manager.cfg.delay_seconds > 0:
                    await asyncio.sleep(self.manager.cfg.delay_seconds)
                return res

            msg = main_msgs[0]
            raw_text = msg.message or ""
            orig_entities = list(msg.entities) if msg.entities else None
            has_real_media = self._is_real_media(msg)
            clear_caption = rule.strip_all_text or (should_strip_all_text(raw_text, rule) is not None)
            if clear_caption:
                cleaned_text, cleaned_entities, changed = "", None, bool(raw_text)
            else:
                cleaned_text, cleaned_entities, changed = clean_message_text(raw_text, orig_entities, rule)
            res = await self._deliver_single_to_target(
                source_ent,
                source_id,
                source_title,
                msg,
                rule,
                target_ent,
                target_id,
                target_title,
                cleaned_text,
                cleaned_entities,
                changed,
                clear_caption,
                has_real_media,
                detail_note="手动重发",
            )
            if self.manager.cfg.delay_seconds > 0:
                await asyncio.sleep(self.manager.cfg.delay_seconds)
            return res

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

        is_broadcast = bool(getattr(chat, "broadcast", False))
        comment_rules = [r for r in rules if r.collect_comments and is_broadcast]
        normal_rules = [r for r in rules if not (r.collect_comments and is_broadcast)]
        for rule in comment_rules:
            self._schedule_comment_forward(chat, source_id, source_title, [msg], rule)
        if not normal_rules:
            return

        async with self._send_lock:
            for rule in normal_rules:
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

                    await self._deliver_single_to_target(
                        chat,
                        source_id,
                        source_title,
                        msg,
                        rule,
                        target_ent,
                        target_id,
                        target_title,
                        cleaned_text,
                        cleaned_entities,
                        changed,
                        clear_caption,
                        has_real_media,
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

        is_broadcast = bool(getattr(chat, "broadcast", False))
        comment_rules = [r for r in rules if r.collect_comments and is_broadcast]
        normal_rules = [r for r in rules if not (r.collect_comments and is_broadcast)]
        for rule in comment_rules:
            self._schedule_comment_forward(chat, source_id, source_title, msgs, rule)
        if not normal_rules:
            return

        async with self._send_lock:
            for rule in normal_rules:
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

                    await self._deliver_album_to_target(
                        chat,
                        source_id,
                        source_title,
                        msgs,
                        rule,
                        target_ent,
                        target_id,
                        target_title,
                        captions,
                        entities_list,
                        any_changed,
                        clear_caption,
                        clean_preview,
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
