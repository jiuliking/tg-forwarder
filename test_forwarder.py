import asyncio
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from aiohttp.test_utils import TestClient, TestServer
from telethon.errors import AuthKeyUnregisteredError, RPCError
from telethon.tl.types import InputPeerChannel, MessageMediaPhoto
from common import AuthStore, DedupStore, HistoryStore, compile_rule_dict, load_raw_config
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
        runner.chats_cache = []
        runner.chat_titles = {}
        runner.target_cache = {TARGET: TARGET_PEER}
        runner.source_map = {}
        runner.manager = NS(cfg=NS(delay_seconds=0, max_download_mb=100, temp_dir=self.tmp),
                            dedup=DedupStore(self.tmp / 'db'), history=NS(record=Mock()))
        self.addCleanup(runner.manager.dedup.conn.close)
        runner.client = NS(send_file=AsyncMock(), send_message=AsyncMock(), forward_messages=AsyncMock(),
                           run_until_disconnected=AsyncMock(), disconnect=AsyncMock(), _authorized=True)
        runner._fetch_channel_comment_batches = AsyncMock(return_value=([], '', None, 0))

    def record(self):
        return self.runner.manager.history.record.call_args.kwargs

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
