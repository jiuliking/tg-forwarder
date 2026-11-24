import asyncio
import logging
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from aiohttp.test_utils import TestClient, TestServer
from telethon.errors import AuthKeyUnregisteredError, RPCError
from telethon.tl.types import InputPeerChannel, MessageMediaPhoto
from common import AuthStore, DedupStore, HistoryStore, compile_rule_dict, load_config, load_raw_config
from forwarder import AccountRunner
from web_server import WebConsole

SOURCE = -1000000000011
TARGET = -1000000000012
TARGET_PEER = InputPeerChannel(12, 1)
CHAT = NS(broadcast=True, noforwards=False)


def post(mid, text='', media=None):
    return NS(id=mid, message=text, entities=None, media=media, grouped_id=None)


def rule(**settings):
    return compile_rule_dict({'sources': [SOURCE], 'targets': [TARGET], **settings})


class Audit(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.addCleanup(logging.disable, logging.root.manager.disable)
        logging.disable(logging.CRITICAL)
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.tmp = Path(tmp)
        self.runner = runner = object.__new__(AccountRunner)
        runner.enabled = True
        runner.account_id = 'default'
        runner.account_name = 'test'
        runner.listening = True
        runner._listen_task = None
        runner._send_lock = asyncio.Lock()
        runner._bg_tasks = set()
        runner._pending_albums = {}
        runner.chats_cache = []
        runner.chat_titles = {}
        runner.target_cache = {TARGET: TARGET_PEER}
        runner.source_map = {}
        runner._source_activated_at = {}
        runner.manager = NS(cfg=NS(delay_seconds=0, max_download_mb=100, temp_dir=self.tmp,
                                   album_quiet_seconds=15, album_max_wait_seconds=45,
                                   comment_silence_seconds=30, comment_max_extra_seconds=180),
                            dedup=DedupStore(self.tmp / 'db'), history=NS(record=Mock(), skipped_rule_names=Mock(return_value=set())))
        self.addCleanup(runner.manager.dedup.conn.close)
        runner.client = NS(send_file=AsyncMock(), send_message=AsyncMock(), forward_messages=AsyncMock(),
                           run_until_disconnected=AsyncMock(), disconnect=AsyncMock(), _authorized=True)
        runner._fetch_channel_comment_batches = AsyncMock(return_value=([], '', None, 0))

    def record(self):
        return self.runner.manager.history.record.call_args.kwargs

    async def test_album_settings_round_trip_and_reject_invalid_patch(self):
        cfg = self.tmp / 'config.yaml'
        auth = AuthStore(self.tmp / 'auth.db')
        self.addCleanup(auth.conn.close)

        async def reload_config(*, force):
            self.runner.manager.cfg = load_config(cfg)

        self.runner.manager.reload_all = AsyncMock(side_effect=reload_config)
        console = WebConsole(self.runner.manager, auth, cfg, self.tmp, Path(__file__).parent / 'ui')
        headers = {'Authorization': 'Bearer ' + auth.get_access_token()}
        async with TestClient(TestServer(console.build_app())) as client:
            async with client.get('/api/settings', headers=headers) as response:
                body = await response.json()
                self.assertEqual((body.get('album_quiet_seconds'), body.get('album_max_wait_seconds')), (15, 45))
                self.assertEqual((body.get('comment_silence_seconds'), body.get('comment_max_extra_seconds')), (30, 180))
            async with client.put('/api/settings', headers=headers,
                                  json={'album_quiet_seconds': 25, 'album_max_wait_seconds': 65,
                                        'comment_silence_seconds': 40, 'comment_max_extra_seconds': 240}) as response:
                self.assertEqual(response.status, 200)
            self.assertEqual(self.runner.manager.cfg.album_quiet_seconds, 25)
            self.assertEqual(self.runner.manager.cfg.album_max_wait_seconds, 65)
            self.assertEqual(self.runner.manager.cfg.comment_silence_seconds, 40)
            self.assertEqual(self.runner.manager.cfg.comment_max_extra_seconds, 240)
            saved = cfg.read_bytes()
            for invalid in ({'album_max_wait_seconds': 20}, {'album_quiet_seconds': 1.5},
                            {'album_quiet_seconds': True}, {'album_max_wait_seconds': 301},
                            {'comment_silence_seconds': 4}, {'comment_silence_seconds': 301},
                            {'comment_max_extra_seconds': -1}, {'comment_max_extra_seconds': 601},
                            {'comment_silence_seconds': True}):
                async with client.put('/api/settings', headers=headers, json=invalid) as response:
                    self.assertEqual(response.status, 400)
                self.assertEqual(cfg.read_bytes(), saved)
            async with client.get('/api/settings', headers=headers) as response:
                body = await response.json()
                self.assertEqual((body['album_quiet_seconds'], body['album_max_wait_seconds']), (25, 65))
                self.assertEqual((body['comment_silence_seconds'], body['comment_max_extra_seconds']), (40, 240))
        self.runner.manager.reload_all.assert_awaited_once_with(force=True)

    async def test_new_album_uses_the_global_wait_values(self):
        runner = self.runner
        runner.manager.cfg.album_quiet_seconds = 22
        runner.manager.cfg.album_max_wait_seconds = 61
        runner.source_map = {SOURCE: [rule()]}
        runner.manager.refresh_if_changed = AsyncMock()
        photo = post(21, media=MessageMediaPhoto())
        photo.grouped_id = 301
        runner.client.get_messages = AsyncMock(return_value=[photo])
        runner.client.forward_messages.return_value = [post(31)]
        with self.album_clock() as clock:
            await runner.process_single(NS(chat_id=SOURCE, message=photo, get_chat=AsyncMock(return_value=CHAT)))
            await asyncio.gather(*runner._bg_tasks)
            self.assertEqual(clock.now, 22)
        runner.client.forward_messages.assert_awaited_once()

    async def test_restart_recovers_missed_albums_in_order_without_replaying_delivered_items(self):
        from datetime import datetime, timezone

        runner = self.runner
        runner.source_map = {SOURCE: [rule()]}
        runner.client.get_entity = AsyncMock(return_value=CHAT)
        photos = [post(i, media=MessageMediaPhoto()) for i in range(21, 27)]
        for photo, group in zip(photos, [301, 301, 302, 302, 303, 303]):
            photo.grouped_id = group
            photo.date = datetime.fromtimestamp(1000, timezone.utc)

        async def history(*args, **kwargs):
            for photo in reversed(photos):
                yield photo

        runner.client.iter_messages = history
        runner.manager.dedup.mark_many(SOURCE, [21, 22], TARGET)
        runner.client.forward_messages.side_effect = [[post(40), post(41)], [post(42), post(43)]]
        await runner._recover_source(SOURCE, 999, 1001)
        self.assertEqual([call.args[1] for call in runner.client.forward_messages.await_args_list], [[23, 24], [25, 26]])

    async def test_recovery_uses_the_checkpoint_saved_before_restart(self):
        runner = self.runner
        runner.source_map = {SOURCE: [rule()]}
        runner._recovery_started_at = 2000
        runner.manager.dedup.set_recovery_floors(runner.account_id, {SOURCE: 1800})
        runner.manager.dedup.conn.close()
        runner.manager.dedup = DedupStore(self.tmp / 'db')
        self.addCleanup(runner.manager.dedup.conn.close)
        runner.client.is_connected = Mock(return_value=True)
        runner._recover_source = AsyncMock()

        runner._recovery_requested_at = 2000
        runner._recovery_failed_sources = set()
        with patch('forwarder.time.time', return_value=2100), patch('forwarder.asyncio.sleep', new_callable=AsyncMock):
            await runner._run_recovery()
            await runner._run_recovery()
        runner._recover_source.assert_awaited_once_with(SOURCE, 1800, 2000)
        self.assertIsNone(runner._recovery_requested_at)

    async def test_healthy_checkpoint_ticks_do_not_query_telegram(self):
        runner = self.runner
        runner.source_map = {SOURCE: [rule()]}
        runner._recovery_task = None
        runner._recovery_failed_sources = set()
        runner.client.transport_available = Mock(return_value=True)
        runner.client.get_entity = AsyncMock()
        runner._recover_source = AsyncMock()
        runner.manager.dedup.set_recovery_floors(runner.account_id, {SOURCE: 1000})
        ticks = 0

        async def stop_after_three_ticks(_delay):
            nonlocal ticks
            ticks += 1
            if ticks == 3:
                runner.enabled = False

        with patch('forwarder.time.time', side_effect=[2000, 2030, 2060]), patch('forwarder.asyncio.sleep', new=stop_after_three_ticks):
            await runner._run_recovery_checkpoints()
        runner.client.get_entity.assert_not_awaited()
        runner._recover_source.assert_not_awaited()
        self.assertEqual(runner.manager.dedup.recovery_floors(runner.account_id), {SOURCE: 1895})

    async def test_comments_wait_through_20s_batch_gaps_and_extend_past_60s(self):
        runner = self.runner
        settings = rule(collect_comments=True, comment_wait_seconds=60, comment_merge_mode='keep_batches')
        runner.source_map = {SOURCE: [settings]}
        runner.manager.cfg.rules = [settings]
        main_photo = post(10, media=MessageMediaPhoto(), text='main')
        main_photo.grouped_id = 500
        batch1 = [post(101, media=MessageMediaPhoto()), post(102, media=MessageMediaPhoto())]
        for item in batch1:
            item.grouped_id = 601
        batch2 = [post(103, media=MessageMediaPhoto()), post(104, media=MessageMediaPhoto())]
        for item in batch2:
            item.grouped_id = 602
        runner._find_album_messages = AsyncMock(return_value=[main_photo])
        runner.client.send_file.side_effect = [post(201), [post(202), post(203)], [post(204), post(205)]]

        now = 1000.0
        async def advance(seconds):
            nonlocal now
            now += seconds

        async def fetch(_ent, _sid, _tid):
            if now < 1072.0:
                return [batch1], '', None, 1049.0  # 11s old at wake-up (1060), 23s gap before batch2
            return [batch1, batch2], '', None, 1072.0

        runner._fetch_channel_comment_batches = AsyncMock(side_effect=fetch)
        with patch('forwarder.time.time', lambda: now), patch('forwarder.asyncio.sleep', new=advance):
            ok, mode = await runner._process_with_comments(CHAT, SOURCE, 'Source', [main_photo], settings)
        self.assertTrue(ok)
        self.assertEqual(mode, 'comments_batch')
        self.assertEqual(runner.client.send_file.await_count, 3)
        self.assertGreaterEqual(now, 1102.0)

    async def test_new_source_added_later_seeds_activation_time_not_process_boot_time(self):
        runner = self.runner
        runner._recovery_started_at = 1000
        runner.client.get_entity = AsyncMock(side_effect=[InputPeerChannel(11, 1), TARGET_PEER])
        runner.client.get_input_entity = AsyncMock(return_value=TARGET_PEER)
        runner.client.is_connected = Mock(return_value=True)
        runner._recover_source = AsyncMock()
        runner.ensure_authorized = AsyncMock(return_value=True)
        cfg = NS(rules=[rule()])
        with patch('forwarder.time.time', return_value=2000):
            await runner.refresh_routes(cfg)
        runner._recovery_requested_at = 2000
        with patch('forwarder.time.time', return_value=2000), patch('forwarder.asyncio.sleep', new_callable=AsyncMock):
            await runner._run_recovery()
        self.assertEqual(runner.manager.dedup.recovery_floors(runner.account_id), {SOURCE: 2000})
        runner._recover_source.assert_not_awaited()

    @contextmanager
    def album_clock(self):
        clock = NS(now=0.0)

        async def advance(seconds):
            clock.now += seconds

        with patch('forwarder.monotonic', lambda: clock.now, create=True), patch('forwarder.asyncio.sleep', new=advance):
            yield clock

    async def test_album_looks_up_all_media_even_with_only_one_live_update(self):
        runner = self.runner
        settings = rule()
        runner.source_map = {SOURCE: [settings]}
        runner.manager.refresh_if_changed = AsyncMock()
        photos = [post(i, media=MessageMediaPhoto()) for i in range(21, 26)]
        for photo in photos:
            photo.grouped_id = 301
        adjacent = post(26, media=MessageMediaPhoto())
        adjacent.grouped_id = 302
        runner.client.get_messages = AsyncMock(return_value=[photos[2], adjacent, *photos[:2], *photos[3:]])
        runner.client.forward_messages.return_value = [post(i) for i in range(30, 35)]
        event = NS(chat_id=SOURCE, message=photos[0], get_chat=AsyncMock(return_value=CHAT))
        with self.album_clock():
            await runner.process_single(event)
            await asyncio.gather(*runner._bg_tasks)
        runner.client.forward_messages.assert_awaited_once()
        self.assertEqual(runner.client.forward_messages.await_args.args[1], [21, 22, 23, 24, 25])
        self.assertEqual(self.record()['item_count'], 5)

    async def test_late_album_fragment_sends_only_newly_visible_media(self):
        runner = self.runner
        settings = rule()
        runner.source_map = {SOURCE: [settings]}
        runner.manager.refresh_if_changed = AsyncMock()
        photos = [post(i, media=MessageMediaPhoto()) for i in range(21, 26)]
        for photo in photos:
            photo.grouped_id = 301
        runner.client.get_messages = AsyncMock(return_value=photos[:2])
        runner.client.forward_messages.side_effect = [[post(30), post(31)], [post(32), post(33), post(34)]]
        with self.album_clock():
            for photo, visible in ((photos[0], photos[:2]), (photos[2], photos)):
                runner.client.get_messages = AsyncMock(return_value=visible)
                await runner.process_single(NS(chat_id=SOURCE, message=photo, get_chat=AsyncMock(return_value=CHAT)))
                await asyncio.gather(*runner._bg_tasks)
        self.assertEqual([call.args[1] for call in runner.client.forward_messages.await_args_list], [[21, 22], [23, 24, 25]])

    async def test_album_waits_for_media_that_appears_ten_seconds_later(self):
        runner = self.runner
        runner.source_map = {SOURCE: [rule()]}
        runner.manager.refresh_if_changed = AsyncMock()
        photos = [post(i, media=MessageMediaPhoto()) for i in range(21, 26)]
        for photo in photos:
            photo.grouped_id = 301
        runner.client.forward_messages.return_value = [post(i) for i in range(30, 35)]
        event = NS(chat_id=SOURCE, message=photos[0], get_chat=AsyncMock(return_value=CHAT))

        async def lookup(chat, ids):
            return photos if clock.now >= 10 else photos[:2]

        with self.album_clock() as clock:
            runner.client.get_messages = AsyncMock(side_effect=lookup)
            await runner.process_single(event)
            await asyncio.gather(*runner._bg_tasks)
        runner.client.forward_messages.assert_awaited_once()
        self.assertEqual(runner.client.forward_messages.await_args.args[1], [21, 22, 23, 24, 25])

    async def test_album_wait_is_bounded_when_a_notified_item_is_unreadable(self):
        runner = self.runner
        runner.source_map = {SOURCE: [rule()]}
        runner.manager.refresh_if_changed = AsyncMock()
        first, missing = (post(i, media=MessageMediaPhoto()) for i in (21, 22))
        first.grouped_id = missing.grouped_id = 301
        runner.client.get_messages = AsyncMock(return_value=[first])
        runner.client.forward_messages.return_value = [post(31)]
        with self.album_clock() as clock:
            for photo in (first, missing):
                await runner.process_single(NS(chat_id=SOURCE, message=photo, get_chat=AsyncMock(return_value=CHAT)))
            await asyncio.gather(*runner._bg_tasks)
            self.assertEqual(clock.now, 45)
        runner.client.forward_messages.assert_awaited_once()
        self.assertEqual(runner.client.forward_messages.await_args.args[1], [21])

    async def test_comment_pack_preserves_the_original_album(self):
        runner = self.runner
        settings = rule(collect_comments=True, comment_merge_mode='pack')
        photos = [post(i, media=MessageMediaPhoto()) for i in range(21, 26)]
        for photo in photos:
            photo.grouped_id = 301
        reply = post(40, media=MessageMediaPhoto())
        runner._fetch_channel_comment_batches = AsyncMock(return_value=([[reply]], '', None, 0))
        runner.client.send_file.side_effect = [[post(i) for i in range(50, 55)], post(56)]
        with self.album_clock():
            await self.merged(photos, settings)
        self.assertEqual([call.args[1] for call in runner.client.send_file.await_args_list],
                         [[p.media for p in photos], reply.media])

    async def test_comment_wait_refreshes_main_album_before_sending(self):
        runner = self.runner
        settings = rule(collect_comments=True)
        runner.source_map = {SOURCE: [settings]}
        runner.manager.cfg.rules = [settings]
        photos = [post(i, media=MessageMediaPhoto()) for i in range(21, 26)]
        for photo in photos:
            photo.grouped_id = 301
        runner.client.get_messages = AsyncMock(return_value=photos)
        runner.client.send_file.return_value = [post(i) for i in range(30, 35)]
        with self.album_clock():
            ok, _ = await runner._process_with_comments(CHAT, SOURCE, 'source', photos[:2], settings)
        self.assertTrue(ok)
        runner.client.send_file.assert_awaited_once()
        self.assertEqual(runner.client.send_file.await_args.args[1], [photo.media for photo in photos])

    async def test_late_album_fragment_with_comments_keeps_existing_media(self):
        runner = self.runner
        settings = rule(collect_comments=True, comment_merge_mode='keep_batches')
        photos = [post(i, media=MessageMediaPhoto()) for i in range(21, 26)]
        for photo in photos:
            photo.grouped_id = 301
        runner.client.send_file.side_effect = [[post(30), post(31)], [post(32), post(33), post(34)]]
        await self.merged(photos[:2], settings)
        await self.merged(photos, settings)
        self.assertEqual([call.args[1] for call in runner.client.send_file.await_args_list],
                         [[p.media for p in photos[:2]], [p.media for p in photos[2:]]])

    async def test_token_rotation_revokes_existing_web_sessions(self):
        auth = AuthStore(self.tmp / 'auth.db')
        self.addCleanup(auth.conn.close)
        old_token = auth.get_access_token()
        session = auth.create_session()
        replacement = auth.rotate_access_token()
        self.assertTrue(auth.verify_access_token(replacement))
        self.assertFalse(auth.verify_access_token(old_token))
        self.assertFalse(auth.verify_session(session))

    async def test_query_link_no_longer_creates_session_but_token_login_works(self):
        auth = AuthStore(self.tmp / 'auth.db')
        self.addCleanup(auth.conn.close)
        console = WebConsole(NS(), auth, self.tmp / 'config.yaml', self.tmp, Path(__file__).parent / 'ui')
        async with TestClient(TestServer(console.build_app())) as client:
            async with client.get('/', params={'token': auth.get_access_token()}, allow_redirects=False) as response:
                self.assertEqual(response.status, 200)
                self.assertFalse(response.cookies)
            async with client.post('/api/auth/login', json={'token': auth.get_access_token()}) as response:
                self.assertEqual(response.status, 200)
                self.assertTrue(response.cookies)

    async def merged(self, posts, r, **kwargs):
        return await self.runner._process_with_comments(CHAT, SOURCE, 'source', posts, r, skip_wait=True, **kwargs)

    async def test_normal_forward_control(self):
        r = self.runner
        r.client.forward_messages.return_value = post(50)
        ok, mode = await r._deliver_single_to_target(CHAT, SOURCE, 'source', post(1, 'hello'), rule(),
                                                     TARGET_PEER, TARGET, 'target', 'hello', None, False, False, False)
        self.assertEqual((ok, mode), (True, 'copy'))
        self.assertTrue(r.manager.dedup.seen(SOURCE, 1, TARGET))

    async def test_album_partial_result_must_not_mark_missing_item_delivered(self):
        r = self.runner
        r.client.forward_messages.return_value = [post(50), None]
        media = MessageMediaPhoto()
        await r._deliver_album_to_target(CHAT, SOURCE, 'source', [post(1, media=media), post(2, media=media)],
                                         rule(), TARGET_PEER, TARGET, 'target', ['', ''], [[], []], False, False, '')
        self.assertFalse(r.manager.dedup.seen(SOURCE, 2, TARGET), f'missing item marked delivered, history={self.record()["status"]}')

    async def test_album_empty_download_must_not_mark_missing_item_delivered(self):
        r = self.runner
        r.client.download_media = AsyncMock(side_effect=['dummy-photo.jpg', None])
        media = MessageMediaPhoto()
        await r._deliver_album_to_target(NS(noforwards=True), SOURCE, 'source', [post(1, media=media), post(2, media=media)],
                                         rule(), TARGET_PEER, TARGET, 'target', ['', ''], [[], []], False, False, '')
        r.client.send_file.assert_awaited_once()
        self.assertFalse(r.manager.dedup.seen(SOURCE, 2, TARGET), f'missing item marked delivered, history={self.record()["status"]}')

    async def test_comment_retry_does_not_repeat_first_delivered_chunk(self):
        r = self.runner
        r.client.send_file.side_effect = [post(51), RuntimeError('second chunk failed'), post(52)]
        first, second = MessageMediaPhoto(), MessageMediaPhoto()
        # The linked discussion chat can have the same numeric message ID.
        r._fetch_channel_comment_batches = AsyncMock(return_value=([[post(1, media=second)]], '', None, 0))
        settings = rule(collect_comments=True, comment_merge_mode='keep_batches')
        r.manager.cfg.rules = [settings]
        r.manager.history = history = HistoryStore(self.tmp / 'db')
        self.addCleanup(history.conn.close)
        source_post = post(1, 'caption', first)
        r.ensure_authorized = AsyncMock(return_value=True)
        r.client.get_entity = AsyncMock(return_value=CHAT)
        r.client.get_messages = AsyncMock(return_value=source_post)
        with patch('forwarder.asyncio.sleep', new_callable=AsyncMock):
            await self.merged([source_post], settings)
            failure = history.recent(limit=1)[0]
            self.assertEqual(failure['status'], 'error')
            # Reopen the database to verify the retry can survive a process restart.
            r.manager.dedup.conn.close()
            r.manager.dedup = DedupStore(self.tmp / 'db')
            self.addCleanup(r.manager.dedup.conn.close)
            ok, _ = await r.resend_history_item(failure)
            self.assertTrue(ok)
        self.assertEqual(history.recent(limit=1)[0]['status'], 'success')
        sent_first = sum(call.args[1] is first for call in r.client.send_file.await_args_list)
        self.assertEqual(sent_first, 1, 'retry sent previously delivered first chunk again')
        self.assertEqual(r.client.send_file.await_args.kwargs['caption'], '')

    async def test_health_requires_telegram_listener_for_enabled_account(self):
        runner = self.runner
        manager = NS(cfg=NS(accounts=[NS(id='default', enabled=True)]), get_runner=lambda _: runner)
        auth = AuthStore(self.tmp / 'auth.db')
        self.addCleanup(auth.conn.close)
        console = WebConsole(manager, auth, self.tmp / 'config.yaml', self.tmp, Path(__file__).parent / 'ui')
        async with TestClient(TestServer(console.build_app())) as client:
            runner.listening = False
            async with client.get('/api/healthz') as response:
                self.assertEqual(response.status, 503)
                self.assertFalse((await response.json())['ok'])
            runner.listening = True
            async with client.get('/api/healthz') as response:
                self.assertEqual(response.status, 200)

    async def test_disabled_rule_stops_queued_comment_forward(self):
        r = self.runner
        entered, release = asyncio.Event(), asyncio.Event()
        settings = rule(collect_comments=True)
        r.source_map = {SOURCE: [settings]}

        async def wait_before_fetch(_seconds):
            entered.set()
            await release.wait()

        with patch('forwarder.asyncio.sleep', new=AsyncMock(side_effect=wait_before_fetch)):
            r._schedule_comment_forward(CHAT, SOURCE, 'source', [post(1, 'caption', MessageMediaPhoto())], settings)
            task = next(iter(r._bg_tasks))
            await asyncio.wait_for(entered.wait(), 2)
            r.source_map = {}
            release.set()
            await asyncio.wait_for(task, 2)
        self.assertEqual(r.client.send_file.await_count, 0, 'queued post still sent after rule removed from active routes')

    async def test_updating_regex_during_comment_wait_keeps_queued_post_alive(self):
        r = self.runner
        entered, release = asyncio.Event(), asyncio.Event()
        old_rule = rule(id='rule_photo_1', collect_comments=True, skip_patterns=['spam'])
        new_rule = rule(id='rule_photo_1', collect_comments=True, skip_patterns=['spam|promo'])
        r.source_map = {SOURCE: [old_rule]}
        r.manager.cfg.rules = [old_rule]
        r.client.send_file.return_value = post(55)

        async def wait_before_fetch(_seconds):
            entered.set()
            await release.wait()

        with patch('forwarder.asyncio.sleep', new=AsyncMock(side_effect=wait_before_fetch)):
            r._schedule_comment_forward(CHAT, SOURCE, 'source', [post(1, 'normal photo', MessageMediaPhoto())], old_rule)
            task = next(iter(r._bg_tasks))
            await asyncio.wait_for(entered.wait(), 2)
            # Simulate editing regex / saving settings while post is queued
            r.source_map = {SOURCE: [new_rule]}
            r.manager.cfg.rules = [new_rule]
            release.set()
            await asyncio.wait_for(task, 2)
        r.client.send_file.assert_awaited_once()

    async def test_slow_reupload_does_not_block_fast_copy_on_send_lock(self):
        r = self.runner
        dl_started, dl_release = asyncio.Event(), asyncio.Event()
        r.source_map = {SOURCE: [rule()]}
        r.client.forward_messages.return_value = post(99)
        r.client.send_file.return_value = post(100)

        async def slow_download(_msg, file=None, **_kw):
            dl_started.set()
            await dl_release.wait()
            return 'dummy-video.mp4'

        r.client.download_media = AsyncMock(side_effect=slow_download)

        restricted_chat = NS(broadcast=True, noforwards=True)
        slow_task = asyncio.create_task(
            r._process_single_message(restricted_chat, SOURCE, post(10, 'vid', MessageMediaPhoto()), [rule()])
        )
        await asyncio.wait_for(dl_started.wait(), 2)

        # While restricted video is still downloading, a fast copy must complete immediately without blocking!
        await asyncio.wait_for(
            r._process_single_message(CHAT, SOURCE, post(11, 'fast', MessageMediaPhoto()), [rule()]),
            timeout=1.0,
        )
        r.client.forward_messages.assert_awaited_once()
        dl_release.set()
        await asyncio.wait_for(slow_task, 2)
        r.client.send_file.assert_awaited_once()

    async def test_comment_mode_checks_every_main_album_caption(self):
        r = self.runner
        await self.merged([post(1, 'normal', MessageMediaPhoto()), post(2, 'advertisement', MessageMediaPhoto())],
                          rule(collect_comments=True, skip_patterns=['advertisement']))
        self.assertEqual(self.record()['status'], 'skipped', 'second caption matched filter but album was sent')

    async def test_comment_fetch_error_is_not_logged_as_complete_delivery(self):
        r = self.runner
        del r._fetch_channel_comment_batches

        async def unavailable(*args, **kwargs):
            raise RPCError(None, 'temporary fetch failure', 500)
            yield

        r.client.iter_messages = unavailable
        await self.merged([post(1, 'caption', MessageMediaPhoto())], rule(collect_comments=True))
        self.assertEqual(self.record()['status'], 'error', 'fetch failure recorded as full success')

    async def test_listener_restarts_after_terminal_disconnect(self):
        r = self.runner
        r.ensure_authorized = AsyncMock(return_value=True)
        r.refresh_routes = AsyncMock()
        r._request_recovery = Mock()

        async def listen():
            if r.client.run_until_disconnected.await_count == 1:
                raise OSError('terminal connection failure')
            self.assertTrue(r.listening)
            r.enabled = False

        r.client.run_until_disconnected.side_effect = listen
        with patch('forwarder.asyncio.sleep', new_callable=AsyncMock):
            await r._run_listener()
        self.assertEqual(r.client.run_until_disconnected.await_count, 2)
        r.refresh_routes.assert_awaited_once()
        r._request_recovery.assert_called_once_with()

    async def test_revoked_after_chats_auth_returns_a_user_facing_status(self):
        r = self.runner
        r.ensure_authorized = AsyncMock(return_value=True)

        async def revoked():
            raise AuthKeyUnregisteredError(None)
            yield

        r.client.iter_dialogs = revoked
        console = WebConsole(NS(get_runner=lambda _: r), NS(verify_session=lambda _: True),
                             self.tmp / 'config.yaml', self.tmp, Path(__file__).parent / 'ui')
        async with TestClient(TestServer(console.build_app())) as client:
            async with client.get('/api/chats?account_id=default') as response:
                self.assertEqual(response.status, 200)
                self.assertFalse((await response.json())['authorized'])
        self.assertFalse(r.listening)

    async def test_stale_rule_index_can_toggle_different_rule(self):
        cfg = self.tmp / 'config.yaml'
        cfg.write_text('rules:\n- name: Alpha\n- name: Beta\n- name: Gamma\n')
        console = WebConsole(NS(reload_all=AsyncMock()), NS(verify_session=lambda _: True),
                             cfg, self.tmp, Path(__file__).parent / 'ui')
        original = load_raw_config(cfg)['rules']
        alpha, beta = original[0]['id'], original[1]['id']
        async with TestClient(TestServer(console.build_app())) as client:
            async with client.delete(f'/api/rules/{alpha}') as response:
                self.assertEqual(response.status, 200)
            async with client.post('/api/rules/1/toggle') as response:
                self.assertEqual(response.status, 404)
            async with client.post(f'/api/rules/{beta}/toggle') as response:
                self.assertEqual(response.status, 200)
        gamma = next(x for x in load_raw_config(cfg)['rules'] if x['name'] == 'Gamma')
        self.assertTrue(gamma.get('enabled', True), 'stale index for Beta disabled Gamma')
        self.assertFalse(load_raw_config(cfg)['rules'][0]['enabled'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
