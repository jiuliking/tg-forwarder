import asyncio
import os
import stat
import tempfile
from pathlib import Path
from types import SimpleNamespace

from telethon.tl.types import (
    DocumentAttributeAnimated,
    DocumentAttributeAudio,
    DocumentAttributeSticker,
    DocumentAttributeVideo,
    MessageEntityBold,
    MessageEntityTextUrl,
    MessageMediaDocument,
    MessageMediaPhoto,
)

from common import (
    AuthStore,
    DedupStore,
    HistoryStore,
    build_media_chunks,
    clean_message_text,
    compile_rule_dict,
    load_config,
    save_raw_config,
    should_skip_text,
    should_strip_all_text,
    truncate_text_with_entities,
    utf16_len,
)
from forwarder import AccountRunner


def test_clean_preserves_utf16_entities() -> None:
    text = "🔥标题加粗\n正文看这里\n关注频道 @ad_chan"
    e_bold = MessageEntityBold(offset=2, length=4)
    e_link = MessageEntityTextUrl(offset=10, length=2, url="https://example.com")
    e_ad = MessageEntityBold(offset=13, length=13)

    cfg_yaml = """
accounts:
  - id: default
    name: 主小号
    enabled: true
rules:
  - name: t1
    enabled: true
    account_id: default
    sources: [-1001]
    targets: [-1002]
    skip_patterns:
      - "(?i)#sponsored"
    strip_patterns:
      - "(?m)^.*关注频道.*$"
    replace_patterns:
      - pattern: "正文"
        repl: "详情内容"
"""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "cfg.yaml"
        p.write_text(cfg_yaml, encoding="utf-8")
        cfg = load_config(p)
        rule = cfg.rules[0]

        assert should_skip_text("hello #SPONSORED", rule) is not None
        assert should_skip_text(text, rule) is None

        cleaned, ents, changed = clean_message_text(text, [e_bold, e_link, e_ad], rule)
        assert changed is True
        assert cleaned == "🔥标题加粗\n详情内容看这里"
        assert ents is not None
        b = [e for e in ents if isinstance(e, MessageEntityBold)]
        assert len(b) == 1
        assert cleaned.encode("utf-16-le")[b[0].offset * 2 : (b[0].offset + b[0].length) * 2].decode("utf-16-le") == "标题加粗"
        lnk = [e for e in ents if isinstance(e, MessageEntityTextUrl)]
        assert len(lnk) == 1
        assert cleaned.encode("utf-16-le")[lnk[0].offset * 2 : (lnk[0].offset + lnk[0].length) * 2].decode("utf-16-le") == "这里"


def test_strip_all_text_still_runs_skip_filter() -> None:
    rule = compile_rule_dict(
        {
            "name": "ad_img",
            "account_id": "acct_2",
            "skip_text_only": True,
            "strip_all_text": True,
            "skip_patterns": ["(?i)广告|推广"],
            "strip_text_patterns": ["(?i)仅发图"],
        }
    )
    assert rule.skip_text_only is True
    assert rule.strip_all_text is True
    assert rule.account_id == "acct_2"
    assert should_skip_text("独家推广图片，点击购买", rule) is not None
    assert should_strip_all_text("今天仅发图不留字", rule) is not None
    assert should_strip_all_text("普通内容", rule) is None


def test_unchanged_text_returns_false() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "cfg.yaml"
        p.write_text("rules:\n  - name: t2\n    sources: [-1001]\n    targets: [-1002]\n    strip_patterns: ['广告']\n", encoding="utf-8")
        rule = load_config(p).rules[0]
        cleaned, ents, changed = clean_message_text("正常内容\n\n\n保留空行", [MessageEntityBold(offset=0, length=2)], rule)
        assert changed is False
        assert cleaned == "正常内容\n\n\n保留空行"
        assert len(ents) == 1


def test_invalid_replace_template_rejected() -> None:
    try:
        compile_rule_dict(
            {
                "name": "bad_repl",
                "replace_patterns": [{"pattern": "foo", "repl": r"\1"}],
            }
        )
        raise AssertionError("Expected ValueError for invalid backreference")
    except ValueError:
        pass


def test_truncate_text_with_entities_utf16() -> None:
    text = "🔥" * 10 + "你好世界ABCDEF"
    ent_keep = MessageEntityBold(offset=0, length=4)
    ent_clip = MessageEntityBold(offset=18, length=6)
    ent_drop = MessageEntityBold(offset=25, length=3)
    out, ents = truncate_text_with_entities(text, [ent_keep, ent_clip, ent_drop], max_utf16=22)
    assert utf16_len(out) <= 22
    assert out.endswith("…")
    assert ents is not None and len(ents) == 2
    assert ents[0].offset == 0 and ents[0].length == 4
    assert ents[1].offset == 18 and ents[1].offset + ents[1].length <= utf16_len(out) - 1


def test_media_bucket_classification() -> None:
    def _doc_msg(mime: str, attrs: list):
        return SimpleNamespace(media=MessageMediaDocument(document=SimpleNamespace(mime_type=mime, attributes=attrs)))

    photo_msg = SimpleNamespace(media=MessageMediaPhoto())
    assert AccountRunner._media_bucket(photo_msg) == "pv"
    assert AccountRunner._media_bucket(_doc_msg("video/mp4", [DocumentAttributeVideo(duration=5, w=720, h=1280)])) == "pv"
    assert AccountRunner._media_bucket(_doc_msg("video/mp4", [DocumentAttributeVideo(duration=5, w=240, h=240, round_message=True)])) == "single"
    assert AccountRunner._media_bucket(_doc_msg("video/mp4", [DocumentAttributeVideo(duration=3, w=320, h=240), DocumentAttributeAnimated()])) == "single"
    assert AccountRunner._media_bucket(_doc_msg("image/webp", [DocumentAttributeSticker(alt="😀", stickerset=None)])) == "single"
    assert AccountRunner._media_bucket(_doc_msg("video/webm", [DocumentAttributeSticker(alt="😀", stickerset=None), DocumentAttributeVideo(duration=2, w=512, h=512)])) == "single"
    assert AccountRunner._media_bucket(_doc_msg("audio/ogg", [DocumentAttributeAudio(duration=4, voice=True)])) == "single"
    assert AccountRunner._media_bucket(_doc_msg("audio/mpeg", [DocumentAttributeAudio(duration=180, voice=False)])) == "audio"
    assert AccountRunner._media_bucket(_doc_msg("application/pdf", [])) == "doc"


def test_auth_change_password_revokes_other_sessions_and_chmod_600() -> None:
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "state.db"
        auth = AuthStore(db_path)
        mode = stat.S_IMODE(os.stat(db_path).st_mode)
        assert mode == 0o600, f"Expected 0600, got {oct(mode)}"
        s1 = auth.create_session()
        s2 = auth.create_session()
        assert auth.verify_session(s1) and auth.verify_session(s2)
        assert auth.change_password(auth.generated_password, "newpass456", keep_session=s1) is True
        assert auth.verify_session(s1) is True
        assert auth.verify_session(s2) is False


def test_phone_2fa_does_not_resubmit_otp() -> None:
    from telethon.errors import SessionPasswordNeededError
    from web_server import WebConsole

    calls = []

    class FakeClient:
        def is_connected(self):
            return True

        async def connect(self):
            pass

        async def send_code_request(self, phone):
            return SimpleNamespace(phone_code_hash="hash123")

        async def sign_in(self, **kwargs):
            calls.append(kwargs)
            if "phone" in kwargs:
                raise SessionPasswordNeededError(request=None)
            assert kwargs == {"password": "my-fixed-2fa-password"}

    class FakeRunner:
        enabled = True
        client = FakeClient()
        session_file = Path("/tmp/nonexistent_tgfwd_test.session")
        authorized_called = False

        async def on_user_authorized(self):
            self.authorized_called = True

    class FakeManager:
        runner = FakeRunner()

        def get_runner(self, acct_id):
            return self.runner

    class FakeRequest:
        def __init__(self, payload):
            self._payload = payload
            self.can_read_body = True

        async def json(self):
            return self._payload

    async def _run():
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            console = WebConsole(FakeManager(), AuthStore(p / "s.db"), p / "c.yaml", p, p)
            r0 = await console.handle_tg_phone_send(FakeRequest({"account_id": "default", "phone": "+1234567890"}))
            assert r0.status == 200
            r1 = await console.handle_tg_phone_verify(FakeRequest({"account_id": "default", "code": "12345"}))
            assert r1.status == 200 and '"need_2fa": true' in r1.text
            r2 = await console.handle_tg_phone_verify(
                FakeRequest({"account_id": "default", "code": "12345", "password": "my-fixed-2fa-password"})
            )
            assert r2.status == 200 and '"authorized"' in r2.text
            assert len(calls) == 2
            assert "phone" in calls[0] and "password" not in calls[0]
            assert calls[1] == {"password": "my-fixed-2fa-password"}
            assert console.manager.runner.authorized_called is True

    asyncio.run(_run())


def test_stores_and_config_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "state.db"
        dedup = DedupStore(db_path)
        dedup.mark(-1001, 42, -1002, account_id="default")
        assert dedup.seen(-1001, 42, -1002, account_id="default") is True
        assert dedup.seen(-1001, 42, -1002, account_id="acct_2") is False

        hist = HistoryStore(db_path)
        hist.record(
            account_id="acct_2",
            account_name="小号2",
            rule_name="r1",
            source_id=-1001,
            source_title="Src",
            target_id=-1002,
            target_title="Tgt",
            msg_id=42,
            item_count=1,
            mode="copy",
            status="success",
            preview="hello",
        )
        rows = hist.recent(10, account_id="acct_2")
        assert len(rows) == 1 and rows[0]["account_name"] == "小号2"
        assert len(hist.recent(10, account_id="default")) == 0
        st = hist.stats_and_activity()
        assert st["total_success"] == 1 and len(st["activity_7d"]) == 7

        hist.record(
            account_id="acct_2",
            account_name="小号2",
            rule_name="r1",
            source_id=-1001,
            source_title="Src",
            target_id=0,
            target_title="-",
            msg_id=43,
            item_count=1,
            mode="skip_regex",
            status="skipped",
            preview="ad",
            detail="命中跳过正则",
        )
        detail = hist.stats_detail()
        assert detail["total_count"] == 2 and detail["skipped_count"] == 1
        rows_all = hist.recent(10)
        assert hist.get_by_id(rows_all[0]["id"])["mode"] == rows_all[0]["mode"]
        assert hist.get_by_id(999999) is None
        assert hist.clear("skipped") == 1
        assert hist.stats_detail()["total_count"] == 1
        assert hist.clear("") == 1
        assert hist.stats_detail()["total_count"] == 0

        auth = AuthStore(db_path)
        assert auth.generated_password is not None
        assert auth.verify_password(auth.generated_password) is True
        assert auth.change_password(auth.generated_password, "newpass123") is True
        assert auth.verify_password("newpass123") is True
        sess = auth.create_session()
        assert auth.verify_session(sess) is True

        cfg_path = Path(td) / "config.yaml"
        save_raw_config(
            cfg_path,
            {
                "global": {"delay_seconds": 1.5, "comment_wait_seconds": 45, "comment_merge_mode": "keep_batches"},
                "accounts": [{"id": "default", "name": "A"}, {"id": "acct_2", "name": "B"}],
                "rules": [
                    {"name": "x", "account_id": "acct_2", "sources": [-1001], "targets": [-1002], "collect_comments": True},
                    {"name": "y", "account_id": "acct_2", "sources": [-1001], "targets": [-1002], "collect_comments": True, "comment_wait_seconds": 15, "comment_merge_mode": "pack"},
                ],
            },
        )
        loaded = load_config(cfg_path)
        assert loaded.delay_seconds == 1.5
        assert len(loaded.accounts) == 2
        assert loaded.rules[0].account_id == "acct_2"
        assert loaded.rules[0].comment_wait_seconds == 45 and loaded.rules[0].comment_merge_mode == "keep_batches"
        assert loaded.rules[1].comment_wait_seconds == 15 and loaded.rules[1].comment_merge_mode == "pack"

        # Test comment chunking modes: pack vs keep_batches
        batches = [["p1"], ["p2", "p3", "p4", "p5", "p6", "p7"], ["p8", "p9", "p10", "p11", "p12", "p13"]]
        packed = build_media_chunks(batches, "pack", lambda _x: "pv", max_album=10)
        assert [len(c) for c in packed] == [10, 3]
        kept = build_media_chunks(batches, "keep_batches", lambda _x: "pv", max_album=10)
        assert [len(c) for c in kept] == [1, 6, 6]
        mixed = build_media_chunks([["p1", "d1", "d2", "p2"]], "pack", lambda x: "pv" if x.startswith("p") else "doc", max_album=10)
        assert mixed == [["p1"], ["d1", "d2"], ["p2"]]


def test_history_pagination() -> None:
    with tempfile.TemporaryDirectory() as td:
        h = HistoryStore(Path(td) / "state.db")
        for i in range(1, 66):
            h.record(
                rule_name="r1" if i % 2 else "r2",
                source_id=-1001,
                source_title="S",
                target_id=-1002,
                target_title="T",
                msg_id=i,
                item_count=1,
                mode="copy",
                status="success" if i % 3 else "skipped",
                preview=f"p{i}",
            )
        p1 = h.query_page(page=1, page_size=30)
        assert p1["total"] == 65 and p1["total_pages"] == 3 and p1["page"] == 1
        assert len(p1["items"]) == 30 and p1["items"][0]["msg_id"] == 65 and p1["items"][-1]["msg_id"] == 36
        p3 = h.query_page(page=3, page_size=30)
        assert len(p3["items"]) == 5 and p3["items"][0]["msg_id"] == 5 and p3["items"][-1]["msg_id"] == 1
        clamped = h.query_page(page=99, page_size=30)
        assert clamped["page"] == 3 and len(clamped["items"]) == 5
        filtered = h.query_page(page=1, page_size=30, status="skipped", rule="r1")
        assert filtered["total"] == len([i for i in range(1, 66) if i % 2 and i % 3 == 0])


if __name__ == "__main__":
    test_clean_preserves_utf16_entities()
    test_strip_all_text_still_runs_skip_filter()
    test_unchanged_text_returns_false()
    test_invalid_replace_template_rejected()
    test_truncate_text_with_entities_utf16()
    test_media_bucket_classification()
    test_auth_change_password_revokes_other_sessions_and_chmod_600()
    test_phone_2fa_does_not_resubmit_otp()
    test_stores_and_config_roundtrip()
    test_history_pagination()
    print("ALL TESTS PASSED")
