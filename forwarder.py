import asyncio
import logging
import os
import shutil
import signal
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any, Callable, Optional

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
    find_matched_regex_branches,
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

ALBUM_FIRST_READ_SECONDS = 4
RECOVERY_CHECKPOINT_SECONDS = 30
RECOVERY_OVERLAP_SECONDS = 120
RECOVERY_MAX_LOOKBACK_SECONDS = 30 * 60
RECOVERY_MESSAGE_LIMIT = 200


@dataclass
class PendingAlbum:
    anchors: set[int]
    started_at: float
    last_update_at: float
    quiet_seconds: int
    max_wait_seconds: int


class ForwarderTelegramClient(TelegramClient):
    def __init__(self, *args, recovery_callback: Callable[[], None], **kwargs) -> None:
        self._recovery_callback = recovery_callback
        super().__init__(*args, **kwargs)

    async def _handle_auto_reconnect(self) -> None:
        self._recovery_callback()
        await super()._handle_auto_reconnect()

    def transport_available(self) -> bool:
        return self.is_connected() and self._sender._transport_connected()


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
        self._reupload_sem = asyncio.Semaphore(2)
        self._inflight_deliveries: set[tuple[int, int, int]] = set()
        self._bg_tasks: set[asyncio.Task[Any]] = set()
        self._pending_albums: dict[tuple[int, int], PendingAlbum] = {}
        self._handlers_attached = False
        self.listening = False
        self._listen_task: Optional[asyncio.Task[None]] = None
        self._recovery_started_at = int(time.time())
        self._source_activated_at: dict[int, int] = {}
        self._recovery_task: Optional[asyncio.Task[None]] = None
        self._checkpoint_task: Optional[asyncio.Task[None]] = None
        self._recovery_requested_at: int | None = None
        self._recovery_failed_sources: set[int] = set()

    def _build_client(self) -> TelegramClient:
        client = ForwarderTelegramClient(
            str(self.session_prefix), self.api_id, self.api_hash, recovery_callback=self._request_recovery,
        )
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
            # Telethon's is_user_authorized() collapses transient RPC failures
            # into False. get_me() distinguishes these from an expired session.
            ok = await self.client.get_me() is not None
            if ok:
                self.client._authorized = True
        else:
            ok = await self.client.is_user_authorized()
        if not ok:
            self.mark_unauthorized()
        return ok

    async def _run_listener(self) -> None:
        retry_delay = 2
        try:
            while self.enabled:
                try:
                    await self.client.run_until_disconnected()
                except UnauthorizedError:
                    log.warning("[%s] Telegram session revoked; please re-login via Web UI", self.account_name)
                    self.mark_unauthorized()
                    return
                except Exception as exc:
                    log.warning("[%s] Listener disconnected: %s", self.account_name, exc)
                self.listening = False
                while self.enabled:
                    await asyncio.sleep(retry_delay)
                    try:
                        if not await self.ensure_authorized(verify_live=True):
                            return
                        await self.refresh_routes(self.manager.cfg)
                    except UnauthorizedError:
                        self.mark_unauthorized()
                        return
                    except Exception as exc:
                        log.warning("[%s] Telegram reconnect failed: %s", self.account_name, exc)
                        retry_delay = min(60, retry_delay * 2)
                        continue
                    self.listening = self.enabled
                    retry_delay = 2
                    log.info("[%s] Telegram listener reconnected", self.account_name)
                    self._request_recovery()
                    break
        finally:
            self.listening = False

    async def stop(self) -> None:
        self.listening = False
        pending: list[asyncio.Task[Any]] = []
        for t in list(self._bg_tasks):
            if not t.done():
                t.cancel()
                pending.append(t)
        self._bg_tasks.clear()
        self._pending_albums.clear()
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
        self._pending_albums.clear()
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
            if not self.enabled:
                return
            me = await self.client.get_me()
            if me is None:
                self.mark_unauthorized()
                log.warning("[%s] Session not authorized (get_me returned None)", self.account_name)
                return
            log.info("[%s] Logged in as %s (id=%s)", self.account_name, me.username or me.first_name, me.id)
            if not self._handlers_attached:
                @self.client.on(events.NewMessage())
                async def _msg_handler(event: events.NewMessage.Event) -> None:
                    await self.process_single(event)

                self._handlers_attached = True

            await self.refresh_routes(self.manager.cfg)
            self.listening = self.enabled
            if self.enabled and (self._listen_task is None or self._listen_task.done()):
                self._listen_task = asyncio.create_task(self._run_listener())
                self._request_recovery()
            if self.enabled and (self._checkpoint_task is None or self._checkpoint_task.done()):
                self._checkpoint_task = asyncio.create_task(self._run_recovery_checkpoints())
                self._bg_tasks.add(self._checkpoint_task)
                self._checkpoint_task.add_done_callback(self._bg_tasks.discard)
            log.info("[%s] Listening for updates...", self.account_name)

    def _request_recovery(self) -> None:
        if not self.enabled or not self.listening:
            return
        self._recovery_requested_at = int(time.time())
        if self._recovery_task is None or self._recovery_task.done():
            self._recovery_task = asyncio.create_task(self._run_recovery())
            self._bg_tasks.add(self._recovery_task)
            self._recovery_task.add_done_callback(self._bg_tasks.discard)

    def _recovery_settle_seconds(self, rules: list[CompiledRule]) -> int:
        comment_delay = max(
            (rule.comment_wait_seconds + self.manager.cfg.comment_max_extra_seconds for rule in rules if rule.collect_comments),
            default=0,
        )
        return self.manager.cfg.album_max_wait_seconds + comment_delay

    def _checkpoint_recovery(self, now: int) -> None:
        floors = self.manager.dedup.recovery_floors(self.account_id)
        updates = {}
        recovering = self._recovery_task is not None and not self._recovery_task.done()
        for source_id, rules in self.source_map.items():
            floor = floors.get(source_id)
            if floor is None:
                updates[source_id] = now
            elif not recovering and source_id not in self._recovery_failed_sources:
                cutoff = now - self._recovery_settle_seconds(rules) - RECOVERY_OVERLAP_SECONDS
                if cutoff > floor:
                    updates[source_id] = cutoff
        if updates:
            self.manager.dedup.set_recovery_floors(self.account_id, updates)

    async def _run_recovery_checkpoints(self) -> None:
        while self.enabled:
            if self.listening and self.client.transport_available():
                self._checkpoint_recovery(int(time.time()))
            await asyncio.sleep(RECOVERY_CHECKPOINT_SECONDS)

    async def _run_recovery(self) -> None:
        while self.enabled and self.listening and self._recovery_requested_at is not None:
            until = self._recovery_requested_at
            self._recovery_requested_at = None
            floors = self.manager.dedup.recovery_floors(self.account_id)
            sources = sorted(self.source_map, key=lambda source: self._recovery_settle_seconds(self.source_map[source]))
            activated = getattr(self, "_source_activated_at", {})
            new_floors = {
                source_id: activated.get(source_id, self._recovery_started_at)
                for source_id in sources if source_id not in floors
            }
            if new_floors:
                self.manager.dedup.set_recovery_floors(self.account_id, new_floors)
                floors.update(new_floors)
            for source_id in sources:
                since = max(floors[source_id], until - RECOVERY_MAX_LOOKBACK_SECONDS)
                if since >= until or source_id not in self.source_map:
                    continue
                settle = self._recovery_settle_seconds(self.source_map[source_id])
                await asyncio.sleep(max(0, until + settle - time.time()))
                if not self.enabled or not self.listening or not self.client.is_connected():
                    return
                try:
                    await self._recover_source(source_id, since, until)
                except (RPCError, OSError, TimeoutError, ValueError):
                    self._recovery_failed_sources.add(source_id)
                    log.warning("[%s] Could not check missed posts for source %s", self.account_name,
                                source_id, exc_info=True)
                else:
                    self._recovery_failed_sources.discard(source_id)

    async def _recover_source(self, source_id: int, since: int, until: int) -> None:
        chat = await self.client.get_entity(source_id)
        batches: dict[tuple[str, int], list[Any]] = {}
        async for msg in self.client.iter_messages(chat, limit=RECOVERY_MESSAGE_LIMIT):
            if not getattr(msg, "date", None):
                continue
            if msg.date.timestamp() < since - self.manager.cfg.album_max_wait_seconds:
                break
            if getattr(msg, "action", None) is not None:
                continue
            grouped_id = getattr(msg, "grouped_id", None)
            key = ("album", grouped_id) if grouped_id is not None else ("single", msg.id)
            batches.setdefault(key, []).append(msg)
        else:
            if sum(len(batch) for batch in batches.values()) >= RECOVERY_MESSAGE_LIMIT:
                log.warning("[%s] Recovery hit the %d message cap for source %s", self.account_name,
                            RECOVERY_MESSAGE_LIMIT, source_id)
        for (kind, group_id), batch in reversed(list(batches.items())):
            if not self.enabled or not self.listening:
                return
            batch.sort(key=lambda msg: msg.id)
            if batch[0].date.timestamp() > until or batch[-1].date.timestamp() < since:
                continue
            if kind == "album" and (source_id, group_id) in self._pending_albums:
                continue
            lead_id = batch[0].id
            skipped = self.manager.history.skipped_rule_names(self.account_id, source_id, lead_id)
            pending_rules = []
            for rule in self._rules_for_chat(source_id):
                if rule.name in skipped:
                    continue
                targets = [utils.get_peer_id(self.target_cache[target]) for target in rule.targets
                           if target in self.target_cache]
                if any(not self.manager.dedup.seen(source_id, msg.id, target, self.account_id)
                       for target in targets for msg in batch):
                    pending_rules.append(rule)
            if not pending_rules:
                continue
            log.info("[%s] Recovering missed post %s/%s (%d items)", self.account_name, source_id, lead_id, len(batch))
            if kind == "album":
                await self._process_album_messages(chat, source_id, batch, recovering_rules=pending_rules)
            else:
                await self._process_single_message(chat, source_id, batch[0], pending_rules, recovering=True)

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
        activated = getattr(self, "_source_activated_at", None)
        if activated is None:
            activated = self._source_activated_at = {}
        now = int(time.time())
        for peer_id in new_source_map:
            activated.setdefault(peer_id, now)
        for old_id in list(activated):
            if old_id not in new_source_map:
                activated.pop(old_id, None)
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

    def _get_reupload_sem(self) -> asyncio.Semaphore:
        sem = getattr(self, "_reupload_sem", None)
        if sem is None:
            sem = self._reupload_sem = asyncio.Semaphore(2)
        return sem

    def _get_inflight_deliveries(self) -> set[tuple[int, int, int]]:
        inflight = getattr(self, "_inflight_deliveries", None)
        if inflight is None:
            inflight = self._inflight_deliveries = set()
        return inflight

    async def _preupload_file(
        self,
        file_path: str,
        *,
        is_video: bool = False,
        video_attrs: Optional[list[Any]] = None,
        thumb_path: Optional[str] = None,
    ) -> tuple[Any, bool]:
        file_to_media = getattr(self.client, "_file_to_media", None)
        if not callable(file_to_media) or not os.path.isfile(file_path):
            return file_path, False
        result = await self._call_with_flood_retry(
            lambda: file_to_media(
                file_path,
                attributes=video_attrs,
                supports_streaming=is_video,
                thumb=thumb_path,
            )
        )
        media = result[1] if isinstance(result, tuple) and len(result) >= 2 else None
        return (media, True) if media is not None else (file_path, False)

    async def _reupload_single(
        self,
        target_ent: Any,
        msg: Any,
        text: str,
        entities: Optional[list[Any]],
        rule: CompiledRule,
        *,
        lock_send: bool = False,
    ) -> bool:
        cfg = self.manager.cfg
        if not self._is_real_media(msg):
            if not (text or "").strip():
                log.warning("Skip empty text-only restricted msg %s", msg.id)
                return False

            async def _send_text() -> None:
                await self._call_with_flood_retry(
                    lambda: self.client.send_message(
                        target_ent,
                        text,
                        formatting_entities=entities,
                        link_preview=(not rule.disable_link_preview) and isinstance(msg.media, MessageMediaWebPage),
                    )
                )

            if lock_send:
                async with self._send_lock:
                    await _send_text()
            else:
                await _send_text()
            return True

        size_mb = self._file_size_mb(msg)
        if size_mb > cfg.max_download_mb:
            log.warning("Skip restricted msg %s: size %.1fMB > max_download_mb %dMB", msg.id, size_mb, cfg.max_download_mb)
            return False

        cap, ents = truncate_text_with_entities(text, entities, 1024)
        work_dir = cfg.temp_dir / f"job_{uuid.uuid4().hex[:12]}"
        work_dir.mkdir(parents=True, exist_ok=True)
        try:
            async with self._get_reupload_sem():
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

                prepared, preuploaded = await self._preupload_file(
                    file_path,
                    is_video=is_video,
                    video_attrs=video_attrs,
                    thumb_path=thumb_path,
                )

            async def _send_media() -> None:
                if preuploaded:
                    await self._call_with_flood_retry(
                        lambda: self.client.send_file(
                            target_ent,
                            prepared,
                            caption=cap,
                            formatting_entities=ents,
                        )
                    )
                else:
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

            if lock_send:
                async with self._send_lock:
                    await _send_media()
            else:
                await _send_media()
            return True
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    async def _reupload_album(
        self,
        target_ent: Any,
        msgs: list[Any],
        captions: list[str],
        entities_list: list[list[Any]],
        *,
        lock_send: bool = False,
    ) -> set[int]:
        cfg = self.manager.cfg
        total_mb = sum(self._file_size_mb(m) for m in msgs)
        if total_mb > cfg.max_download_mb:
            log.warning("Skip restricted album (%d items): total %.1fMB > %dMB", len(msgs), total_mb, cfg.max_download_mb)
            return set()

        work_dir = cfg.temp_dir / f"album_{uuid.uuid4().hex[:12]}"
        work_dir.mkdir(parents=True, exist_ok=True)
        try:
            items: list[tuple[Any, str, list[Any], int]] = []
            dropped_caption: Optional[tuple[str, list[Any]]] = None
            async with self._get_reupload_sem():
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
                        items.append((media_obj, cap, ents or [], idx))
                    else:
                        prepared, _ = await self._preupload_file(p, is_video=is_video, video_attrs=video_attrs)
                        items.append((prepared, cap, ents or [], idx))

            if not items:
                return set()
            if dropped_caption is not None and not any(item[1] for item in items):
                m0, _, _, original_index = items[0]
                items[0] = (m0, dropped_caption[0], dropped_caption[1], original_index)

            async def _send_album_items() -> set[int]:
                if len(items) == 1:
                    media0, cap0, ent0, original_index = items[0]
                    sent = await self._call_with_flood_retry(
                        lambda: self.client.send_file(
                            target_ent,
                            media0,
                            caption=cap0,
                            formatting_entities=ent0 or None,
                            supports_streaming=True,
                        )
                    )
                    return {original_index} if sent is not None else set()

                files_out = [x[0] for x in items]
                caps_out = [x[1] for x in items]
                ents_out = [x[2] for x in items]
                sent = await self._call_with_flood_retry(
                    lambda: self.client.send_file(
                        target_ent,
                        files_out,
                        caption=caps_out,
                        formatting_entities=ents_out,
                        supports_streaming=True,
                    )
                )
                return {item[3] for item, result in zip(items, sent or []) if result is not None}

            if lock_send:
                async with self._send_lock:
                    return await _send_album_items()
            return await _send_album_items()
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

    async def _find_album_messages(self, chat: Any, group_id: int, anchors: set[int]) -> list[Any]:
        # Albums contain up to ten messages. Match neighbors by grouped_id.
        ids = sorted({mid for anchor in anchors for mid in range(max(1, anchor - 9), anchor + 10)})
        messages = await self.client.get_messages(chat, ids=ids)
        matches = [msg for msg in (messages or []) if getattr(msg, "grouped_id", None) == group_id]
        return sorted(matches, key=lambda msg: msg.id)

    def _queue_album(self, event: events.NewMessage.Event) -> None:
        key = (event.chat_id, event.message.grouped_id)
        pending = self._pending_albums.get(key)
        if pending is not None:
            if event.message.id not in pending.anchors:
                pending.anchors.add(event.message.id)
                pending.last_update_at = monotonic()
            return
        now = monotonic()
        pending = PendingAlbum(
            anchors={event.message.id},
            started_at=now,
            last_update_at=now,
            quiet_seconds=self.manager.cfg.album_quiet_seconds,
            max_wait_seconds=self.manager.cfg.album_max_wait_seconds,
        )
        self._pending_albums[key] = pending
        task = asyncio.create_task(self._assemble_album(event, key, pending))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _assemble_album(self, event: events.NewMessage.Event, key: tuple[int, int], pending: PendingAlbum) -> None:
        source_id, group_id = key
        deadline = pending.started_at + pending.max_wait_seconds
        last_snapshot_change = pending.started_at
        previous: set[int] | None = None
        messages: list[Any] = []
        try:
            await asyncio.sleep(min(ALBUM_FIRST_READ_SECONDS, pending.quiet_seconds))
            if not self.enabled or not self._rules_for_chat(source_id):
                return
            chat = await event.get_chat()
            if chat is None:
                chat = await self.client.get_entity(source_id)
            while self.enabled and self._rules_for_chat(source_id):
                anchors = pending.anchors.copy()
                messages = await self._find_album_messages(chat, group_id, anchors)
                current = {msg.id for msg in messages}
                now = monotonic()
                baseline = anchors if previous is None else previous
                if current != baseline:
                    last_snapshot_change = now
                quiet_until = max(last_snapshot_change, pending.last_update_at) + pending.quiet_seconds
                all_known_visible = bool(current) and pending.anchors <= current
                if now >= deadline or (all_known_visible and (len(current) == 10 or now >= quiet_until)):
                    break
                previous = current
                next_read = quiet_until if all_known_visible else now + ALBUM_FIRST_READ_SECONDS
                await asyncio.sleep(max(0, min(deadline, next_read) - now))
        except (RPCError, OSError, TimeoutError, ValueError):
            log.warning("[%s] Could not assemble album %s/%s", self.account_name, source_id, group_id, exc_info=True)
            return
        finally:
            if self._pending_albums.get(key) is pending:
                self._pending_albums.pop(key, None)
        if not messages:
            log.warning("[%s] No source messages found for album %s/%s", self.account_name, source_id, group_id)
        elif self.enabled:
            log.info("[%s] Assembled album %s/%s: %d items from %d live updates", self.account_name,
                     source_id, group_id, len(messages), len(pending.anchors))
            await self._process_album_messages(chat, source_id, messages)

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
            async for cm in self.client.iter_messages(source_ent, reply_to=thread_msg_id, reverse=True, limit=400):
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
        recovering: bool = False,
        target_override: Optional[tuple[Any, int, str]] = None,
        detail_prefix: str = "",
        delivery_key: str = "comments",
        history_msg_id: int | None = None,
    ) -> tuple[bool, str]:
        if not self.enabled or not main_msgs:
            return False, "账号未启用或消息为空"
        lead_id = main_msgs[0].id if history_msg_id is None else history_msg_id
        thread_msg_id = next((m.id for m in main_msgs if getattr(m, "replies", None)), main_msgs[0].id)
        mode = "comments_batch" if rule.comment_merge_mode == "keep_batches" else "comments_pack"
        album_id = getattr(main_msgs[0], "grouped_id", None)
        group_id = str(album_id) if album_id is not None else ""

        def media_key(msg: Any) -> str:
            return f"{'main' if id(msg) in main_objects else 'reply'}:{msg.id}"

        def resolve_active_rule() -> Optional[CompiledRule]:
            if not self.enabled:
                return None
            if skip_wait and not recovering:
                return rule
            active_rules = self._rules_for_chat(source_id)
            if rule in active_rules and rule in self.manager.cfg.rules:
                return rule
            if rule.id:
                for candidate in active_rules:
                    if (
                        candidate.id == rule.id
                        and candidate.enabled
                        and candidate.collect_comments
                        and candidate in self.manager.cfg.rules
                    ):
                        return candidate
            return None

        def is_active() -> bool:
            nonlocal rule, mode
            current = resolve_active_rule()
            if current is None:
                log.info("[%s/%s] Aborted queued post %s: rule disabled or source removed", self.account_name, rule.name, lead_id)
                return False
            if current is not rule:
                rule = current
                mode = "comments_batch" if rule.comment_merge_mode == "keep_batches" else "comments_pack"
            return True

        def targets() -> list[tuple[Any, int, str]]:
            if target_override is not None:
                return [target_override]
            out = {}
            for target in rule.targets:
                peer = self.target_cache.get(target)
                if peer is None:
                    continue
                target_id = utils.get_peer_id(peer)
                if not all(self.manager.dedup.seen(source_id, msg.id, target_id, account_id=self.account_id)
                           for msg in main_msgs):
                    out[target_id] = (peer, target_id, self.chat_titles.get(target_id, str(target)))
            return list(out.values())

        try:
            if not skip_wait:
                wait_s = max(5, rule.comment_wait_seconds)
                silence_s = max(5, self.manager.cfg.comment_silence_seconds)
                extra_s = max(0, self.manager.cfg.comment_max_extra_seconds)
                start_ts = time.time()
                max_deadline = start_ts + wait_s + extra_s

                await asyncio.sleep(wait_s)
                if not is_active():
                    return False, "规则已停用或修改"

                comment_batches, fb_text, fb_entities, latest_ts = await self._fetch_channel_comment_batches(
                    chat, source_id, thread_msg_id
                )
                while extra_s > 0 and latest_ts > 0 and (time.time() - latest_ts) < silence_s and time.time() < max_deadline:
                    await asyncio.sleep(5)
                    if not is_active():
                        return False, "规则已停用或修改"
                    comment_batches, fb_text, fb_entities, latest_ts = await self._fetch_channel_comment_batches(
                        chat, source_id, thread_msg_id
                    )
                if album_id is not None:
                    refreshed = await self._find_album_messages(chat, album_id, {msg.id for msg in main_msgs})
                    if not refreshed:
                        raise ValueError("源频道相册已不可读")
                    main_msgs = refreshed
                    updated_thread = next((msg.id for msg in main_msgs if getattr(msg, "replies", None)), main_msgs[0].id)
                    if updated_thread != thread_msg_id:
                        comment_batches, fb_text, fb_entities, _ = await self._fetch_channel_comment_batches(
                            chat, source_id, updated_thread
                        )
            else:
                comment_batches, fb_text, fb_entities, _ = await self._fetch_channel_comment_batches(
                    chat, source_id, thread_msg_id
                )

        except (RPCError, OSError, TimeoutError, ValueError) as exc:
            async with self._send_lock:
                if is_active():
                    for _, target_id, target_title in targets():
                        self.manager.history.record(
                            account_id=self.account_id, account_name=self.account_name, rule_name=rule.name,
                            source_id=source_id, source_title=source_title, target_id=target_id,
                            target_title=target_title, msg_id=lead_id, item_count=len(main_msgs),
                            mode=mode, status="error", detail=f"主帖或评论读取失败: {exc}",
                            delivery_key=delivery_key, rule_id=rule.id, album_group_id=group_id,
                        )
            return False, f"主帖或评论读取失败: {exc}"

        main_objects = {id(msg) for msg in main_msgs}
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
            if not is_active():
                return False, "规则已停用或修改"
            if target_override is None:
                filter_texts = [message.message or "" for message in main_msgs] if main_text_msg else [chosen_text]
                skip_hit = next(((text, hit) for text in filter_texts
                                 if (hit := should_skip_text(text, rule)) is not None), None)
                if skip_hit is not None:
                    matched_text, matched_skip = skip_hit
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
                        matched_terms=find_matched_regex_branches(matched_text, matched_skip),
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

            if album_id is not None and main_media:
                chunks = build_media_chunks([main_media], "keep_batches", self._media_bucket)
                chunks += build_media_chunks(comment_batches, rule.comment_merge_mode, self._media_bucket)
            else:
                chunks = build_media_chunks(all_batches, rule.comment_merge_mode, self._media_bucket)
            clean_preview = cleaned_text or (f"[评论区合并 {total_media} 项]" if comment_batches else f"[媒体 {total_media} 项]")

            target_list = targets()

            last_ok = False
            last_msg = "无可用目标频道"
            for target_ent, target_id, target_title in target_list:
                if not is_active():
                    return False, "规则已停用或修改"
                key = (self.account_id, source_id, lead_id, target_id, delivery_key)
                delivered = self.manager.dedup.delivery_parts(key)
                try:
                    has_standalone_text = not total_media or utf16_len(cleaned_text) > 1024
                    if has_standalone_text and "text" not in delivered:
                        if not rule.fallback_reupload and self._is_noforwards(chat, main_msgs):
                            raise ValueError("源频道禁止转发且未启用重传")
                        sent = await self._call_with_flood_retry(
                            lambda: self.client.send_message(
                                target_ent, cleaned_text, formatting_entities=cleaned_entities,
                                link_preview=(not rule.disable_link_preview)
                                and isinstance(main_msgs[0].media, MessageMediaWebPage),
                            )
                        )
                        if sent is None:
                            raise RuntimeError("Telegram 未确认文本发送")
                        self.manager.dedup.mark_delivery_parts(key, ["text"])
                        delivered.add("text")
                        if total_media:
                            await asyncio.sleep(min(1.5, max(0.5, self.manager.cfg.delay_seconds or 1.0)))

                    sent_chunks = 0
                    for chunk in chunks:
                        pending = [(m, media_key(m)) for m in chunk if media_key(m) not in delivered]
                        if not pending:
                            continue
                        if sent_chunks:
                            await asyncio.sleep(min(2.0, max(0.8, self.manager.cfg.delay_seconds or 1.0)))
                        if not is_active():
                            return False, "规则已停用或修改"
                        cap, ents = "", None
                        if not has_standalone_text and "caption" not in delivered:
                            cap, ents = truncate_text_with_entities(cleaned_text, cleaned_entities, 1024)
                        sent_indexes, caption_sent = await self._deliver_comment_chunk(
                            chat, target_ent, [m for m, _ in pending], cap, ents, rule,
                        )
                        parts = {pending[idx][1] for idx in sent_indexes}
                        if caption_sent:
                            parts.add("caption")
                        self.manager.dedup.mark_delivery_parts(key, parts)
                        delivered.update(parts)
                        if len(sent_indexes) != len(pending):
                            raise RuntimeError(f"评论组发送不完整：本次确认发送 {len(sent_indexes)}/{len(pending)} 项")
                        sent_chunks += 1
                    self.manager.dedup.mark_many(
                        source_id, {lead_id, *(msg.id for msg in main_msgs)}, target_id, account_id=self.account_id,
                    )
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
                        delivery_key=delivery_key, rule_id=rule.id, album_group_id=group_id,
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
                        delivery_key=delivery_key, rule_id=rule.id, album_group_id=group_id,
                        preview=clean_preview,
                        detail=str(exc),
                    )
                    last_ok, last_msg = False, str(exc)
                if self.manager.cfg.delay_seconds > 0:
                    await asyncio.sleep(self.manager.cfg.delay_seconds)
            return last_ok, last_msg

    async def _deliver_comment_chunk(
        self,
        chat: Any,
        target: Any,
        chunk: list[Any],
        caption: str,
        entities: Optional[list[Any]],
        rule: CompiledRule,
    ) -> tuple[set[int], bool]:
        if not self._is_noforwards(chat, chunk):
            try:
                if len(chunk) == 1:
                    result = await self._call_with_flood_retry(
                        lambda: self.client.send_file(target, chunk[0].media, caption=caption, formatting_entities=entities)
                    )
                    indexes = {0} if result is not None else set()
                else:
                    result = await self._call_with_flood_retry(
                        lambda: self.client.send_file(
                            target, [msg.media for msg in chunk],
                            caption=[caption] + [""] * (len(chunk) - 1),
                            formatting_entities=[entities or []] + [[] for _ in chunk[1:]],
                        )
                    )
                    indexes = {idx for idx, (_, msg) in enumerate(zip(chunk, result or [])) if msg is not None}
                return indexes, bool(caption and 0 in indexes)
            except ChatForwardsRestrictedError:
                pass
        if not rule.fallback_reupload:
            raise ValueError("源频道禁止转发且未启用重传")
        if len(chunk) == 1:
            uploaded = await self._reupload_single(target, chunk[0], caption, entities, rule)
            return ({0} if uploaded else set()), bool(caption and uploaded)
        sent = await self._reupload_album(
            target, chunk, [caption] + [""] * (len(chunk) - 1), [entities or []] + [[] for _ in chunk[1:]],
        )
        return sent, bool(caption and sent)

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
        *,
        lock_send: bool = False,
    ) -> tuple[bool, str]:
        mode = "copy"
        inflight = self._get_inflight_deliveries()
        inflight_key = (source_id, msg.id, target_id)
        if lock_send and inflight_key in inflight:
            return False, "正在转发中"
        if lock_send:
            inflight.add(inflight_key)
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
                    async def _do_copy() -> None:
                        await self._call_with_flood_retry(
                            lambda: self.client.forward_messages(
                                target_ent,
                                msg.id,
                                source_id,
                                drop_author=rule.drop_author,
                                drop_media_captions=clear_caption,
                            )
                        )

                    if lock_send:
                        async with self._send_lock:
                            await _do_copy()
                            if self.manager.cfg.delay_seconds > 0:
                                await asyncio.sleep(self.manager.cfg.delay_seconds)
                    else:
                        await _do_copy()
                    mode = "cleaned" if clear_caption and changed else "copy"
                except ChatForwardsRestrictedError:
                    restricted = True
            elif not restricted:
                try:
                    mode = "cleaned"
                    if has_real_media:
                        safe_cap, safe_ents = truncate_text_with_entities(cleaned_text, cleaned_entities, 1024)

                        async def _do_send_media() -> None:
                            await self._call_with_flood_retry(
                                lambda: self.client.send_file(
                                    target_ent,
                                    msg.media,
                                    caption=safe_cap,
                                    formatting_entities=safe_ents,
                                )
                            )

                        if lock_send:
                            async with self._send_lock:
                                await _do_send_media()
                                if self.manager.cfg.delay_seconds > 0:
                                    await asyncio.sleep(self.manager.cfg.delay_seconds)
                        else:
                            await _do_send_media()
                    else:
                        if not cleaned_text.strip():
                            return False, "清洗后为空文本，无法发送"

                        async def _do_send_text() -> None:
                            await self._call_with_flood_retry(
                                lambda: self.client.send_message(
                                    target_ent,
                                    cleaned_text,
                                    formatting_entities=cleaned_entities,
                                    link_preview=(not rule.disable_link_preview)
                                    and isinstance(msg.media, MessageMediaWebPage),
                                )
                            )

                        if lock_send:
                            async with self._send_lock:
                                await _do_send_text()
                                if self.manager.cfg.delay_seconds > 0:
                                    await asyncio.sleep(self.manager.cfg.delay_seconds)
                        else:
                            await _do_send_text()
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
                uploaded = await self._reupload_single(
                    target_ent, msg, cleaned_text, cleaned_entities, rule, lock_send=lock_send
                )
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
                if lock_send and self.manager.cfg.delay_seconds > 0:
                    async with self._send_lock:
                        await asyncio.sleep(self.manager.cfg.delay_seconds)

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
        finally:
            if lock_send:
                inflight.discard(inflight_key)

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
        delivery_key: str = "album",
        history_msg_id: int | None = None,
        *,
        lock_send: bool = False,
    ) -> tuple[bool, str]:
        lead_id = history_msg_id if history_msg_id is not None else msgs[0].id
        group_id = str(getattr(msgs[0], "grouped_id", None) or "")
        original_ids = [msg.id for msg in msgs]
        inflight = self._get_inflight_deliveries()
        inflight_key = (source_id, lead_id, target_id)
        if lock_send and inflight_key in inflight:
            return False, "正在转发中"
        if lock_send:
            inflight.add(inflight_key)
        key = (self.account_id, source_id, lead_id, target_id, delivery_key)
        delivered = self.manager.dedup.delivery_parts(key)
        pending = [idx for idx, msg in enumerate(msgs) if f"main:{msg.id}" not in delivered]
        msgs = [msgs[idx] for idx in pending]
        captions = [captions[idx] for idx in pending]
        entities_list = [entities_list[idx] for idx in pending]
        mode = "copy"
        try:
            if not msgs:
                self.manager.dedup.mark_many(source_id, original_ids, target_id, account_id=self.account_id)
                return True, "already_delivered"
            sent_ids: set[int] = set()
            restricted = self._is_noforwards(chat, msgs)
            can_server_forward = (
                not restricted
                and ((not any_changed and not clear_caption) or (clear_caption and rule.drop_author))
            )
            if can_server_forward:
                try:
                    async def _do_album_copy() -> Any:
                        return await self._call_with_flood_retry(
                            lambda: self.client.forward_messages(
                                target_ent,
                                [m.id for m in msgs],
                                source_id,
                                drop_author=rule.drop_author,
                                drop_media_captions=clear_caption,
                            )
                        )

                    if lock_send:
                        async with self._send_lock:
                            sent = await _do_album_copy()
                            if self.manager.cfg.delay_seconds > 0:
                                await asyncio.sleep(self.manager.cfg.delay_seconds)
                    else:
                        sent = await _do_album_copy()
                    sent_ids = {msg.id for msg, result in zip(msgs, sent or []) if result is not None}
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

                        async def _do_album_cleaned() -> Any:
                            return await self._call_with_flood_retry(
                                lambda: self.client.send_file(
                                    target_ent,
                                    media_list,
                                    caption=safe_caps,
                                    formatting_entities=safe_ents,
                                )
                            )

                        if lock_send:
                            async with self._send_lock:
                                sent = await _do_album_cleaned()
                                if self.manager.cfg.delay_seconds > 0:
                                    await asyncio.sleep(self.manager.cfg.delay_seconds)
                        else:
                            sent = await _do_album_cleaned()
                        sent_ids = {msg.id for msg, result in zip(msgs, sent or []) if result is not None}
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
                        delivery_key=delivery_key,
                        rule_id=rule.id,
                        album_group_id=group_id,
                        preview=clean_preview,
                        detail="源频道禁止转发且未启用重传",
                    )
                    return False, "源频道禁止转发且未启用重传"
                mode = "reupload"
                sent_indexes = await self._reupload_album(
                    target_ent, msgs, captions, entities_list, lock_send=lock_send
                )
                sent_ids = {msgs[idx].id for idx in sent_indexes}
                if lock_send and sent_ids and self.manager.cfg.delay_seconds > 0:
                    async with self._send_lock:
                        await asyncio.sleep(self.manager.cfg.delay_seconds)

            self.manager.dedup.mark_delivery_parts(key, (f"main:{msg_id}" for msg_id in sent_ids))
            if len(sent_ids) != len(msgs):
                raise RuntimeError(f"相册发送不完整：本次确认发送 {len(sent_ids)}/{len(msgs)} 项，可从记录补发未送达项")
            self.manager.dedup.mark_many(source_id, original_ids, target_id, account_id=self.account_id)
            self.manager.history.record(
                account_id=self.account_id,
                account_name=self.account_name,
                rule_name=rule.name,
                source_id=source_id,
                source_title=source_title,
                target_id=target_id,
                target_title=target_title,
                msg_id=lead_id,
                item_count=len(original_ids),
                mode=mode,
                status="success",
                delivery_key=delivery_key,
                rule_id=rule.id,
                album_group_id=group_id,
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
                item_count=len(original_ids),
                mode=mode,
                status="error",
                delivery_key=delivery_key,
                rule_id=rule.id,
                album_group_id=group_id,
                preview=clean_preview,
                detail=str(exc),
            )
            return False, str(exc)
        finally:
            if lock_send:
                inflight.discard(inflight_key)

    def _find_rule_for_history(self, item: dict[str, Any], source_id: int) -> CompiledRule:
        rule_name = str(item.get("rule_name") or "")
        rule_id = str(item.get("rule_id") or "")
        for r in self.manager.cfg.rules:
            if rule_id and r.id == rule_id and r.account_id == self.account_id:
                return r
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

        try:
            lead_msg = await self.client.get_messages(source_ent, ids=msg_id)
            main_msgs: list[Any] = [lead_msg] if lead_msg is not None else []
            gid = getattr(lead_msg, "grouped_id", None) or (int(item["album_group_id"]) if item.get("album_group_id") else None)
            if gid is not None:
                grouped = await self._find_album_messages(source_ent, gid, {msg_id})
                if grouped:
                    main_msgs = grouped
        except (RPCError, OSError, TimeoutError) as exc:
            return False, f"暂时无法读取源消息: {exc}"
        if not main_msgs:
            return False, "源频道原消息已被删除或无法读取"

        rule = self._find_rule_for_history(item, source_id)
        hist_mode = str(item.get("mode") or "")
        is_broadcast = bool(getattr(source_ent, "broadcast", False))
        saved_key = str(item.get("delivery_key") or "")
        was_album = saved_key.startswith("album")
        was_comments = saved_key.startswith("comments") or hist_mode in ("comments_pack", "comments_batch")
        new_attempt = item.get("status") == "success"
        if is_broadcast and (was_comments or (not was_album and rule.collect_comments)):
            delivery_key = f"comments:{uuid.uuid4().hex}" if new_attempt else (saved_key or "comments")
            return await self._process_with_comments(
                source_ent,
                source_id,
                source_title,
                main_msgs,
                rule,
                skip_wait=True,
                target_override=(target_ent, target_id, target_title),
                detail_prefix="手动重发",
                delivery_key=delivery_key,
                history_msg_id=msg_id,
            )

        if not self.enabled:
            return False, "账号已停止"
        if gid is not None or len(main_msgs) > 1:
            delivery_key = f"album:{uuid.uuid4().hex}" if new_attempt else (saved_key or "album")
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
            return await self._deliver_album_to_target(
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
                delivery_key=delivery_key,
                history_msg_id=msg_id,
                lock_send=True,
            )

        msg = main_msgs[0]
        raw_text = msg.message or ""
        orig_entities = list(msg.entities) if msg.entities else None
        has_real_media = self._is_real_media(msg)
        clear_caption = rule.strip_all_text or (should_strip_all_text(raw_text, rule) is not None)
        if clear_caption:
            cleaned_text, cleaned_entities, changed = "", None, bool(raw_text)
        else:
            cleaned_text, cleaned_entities, changed = clean_message_text(raw_text, orig_entities, rule)
        return await self._deliver_single_to_target(
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
            lock_send=True,
        )

    async def process_single(self, event: events.NewMessage.Event) -> None:
        if not self.enabled:
            return
        msg = event.message
        await self.manager.refresh_if_changed()
        source_id = event.chat_id
        rules = self._rules_for_chat(source_id)
        if not rules:
            return
        if msg.grouped_id is not None:
            self._queue_album(event)
            return

        chat = await event.get_chat()
        await self._process_single_message(chat, source_id, msg, rules)

    async def _process_single_message(
        self, chat: Any, source_id: int, msg: Any, rules: list[CompiledRule], *, recovering: bool = False,
    ) -> None:
        source_title = self._entity_title(chat, source_id)
        self.chat_titles[source_id] = source_title
        raw_text = msg.message or ""
        orig_entities = list(msg.entities) if msg.entities else None
        has_real_media = self._is_real_media(msg)

        is_broadcast = bool(getattr(chat, "broadcast", False))
        comment_rules = [r for r in rules if r.collect_comments and is_broadcast]
        normal_rules = [r for r in rules if not (r.collect_comments and is_broadcast)]
        for rule in comment_rules:
            if recovering:
                await self._process_with_comments(chat, source_id, source_title, [msg], rule,
                                                  skip_wait=True, recovering=True, detail_prefix="回查补发")
            else:
                self._schedule_comment_forward(chat, source_id, source_title, [msg], rule)
        if not normal_rules:
            return

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
                    matched_terms=find_matched_regex_branches(raw_text, matched_skip),
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
                    lock_send=True,
                )

    async def _process_album_messages(
        self, chat: Any, source_id: int, msgs: list[Any], *, recovering_rules: list[CompiledRule] | None = None,
    ) -> None:
        rules = self._rules_for_chat(source_id) if recovering_rules is None else recovering_rules
        if not rules:
            return

        source_title = self._entity_title(chat, source_id)
        self.chat_titles[source_id] = source_title
        lead_id = msgs[0].id
        raw_preview = next((m.message for m in msgs if m.message), "") or f"[相册 {len(msgs)} 项]"

        is_broadcast = bool(getattr(chat, "broadcast", False))
        comment_rules = [r for r in rules if r.collect_comments and is_broadcast]
        normal_rules = [r for r in rules if not (r.collect_comments and is_broadcast)]
        for rule in comment_rules:
            if recovering_rules is not None:
                await self._process_with_comments(chat, source_id, source_title, msgs, rule,
                                                  skip_wait=True, recovering=True, detail_prefix="回查补发")
            else:
                self._schedule_comment_forward(chat, source_id, source_title, msgs, rule)
        if not normal_rules:
            return

        for rule in normal_rules:
            skip_hit = None
            skip_source_text = ""
            for m in msgs:
                skip_hit = should_skip_text(m.message or "", rule)
                if skip_hit is not None:
                    skip_source_text = m.message or ""
                    break
            if skip_hit is not None:
                log.info("[%s/%s] Skip album %s (matched %r)", self.account_name, rule.name, lead_id, skip_hit)
                first_txt = next((m.message for m in msgs if m.message), "[相册]")
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
                    preview=first_txt,
                    detail=f"命中跳过正则: {skip_hit}",
                    matched_terms=find_matched_regex_branches(skip_source_text, skip_hit),
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
                if all(self.manager.dedup.seen(source_id, msg.id, target_id, account_id=self.account_id)
                       for msg in msgs):
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
                    lock_send=True,
                )


class ForwarderManager:
    def __init__(self, api_id: int, api_hash: str) -> None:
        self.api_id = api_id
        self.api_hash = api_hash
        self.cfg: AppConfig = load_config(CONFIG_PATH)
        self.cfg_mtime: float = CONFIG_PATH.stat().st_mtime if CONFIG_PATH.exists() else 0.0
        self.dedup = DedupStore(STATE_DB_PATH)
        self.dedup.prune(self.cfg.dedup_ttl_hours)
        self.dedup.prune_delivery_parts(self.cfg.log_retention_days)
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
            self.dedup.prune_delivery_parts(new_cfg.log_retention_days)

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
        log.warning("Set the web admin password in an interactive terminal with: python common.py reset-web-password")
    log.info("Starting web server on %s:%d", host, port)
    log.info("Web UI available at http://%s:%d/", host, port)

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
