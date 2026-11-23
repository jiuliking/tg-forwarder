import tempfile
from pathlib import Path

from telethon.tl.types import MessageEntityBold, MessageEntityTextUrl

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
)


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
        cleaned, ents, changed = clean_message_text("正常内容", [MessageEntityBold(offset=0, length=2)], rule)
        assert changed is False
        assert cleaned == "正常内容"
        assert len(ents) == 1


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
                "global": {"delay_seconds": 1.5},
                "accounts": [{"id": "default", "name": "A"}, {"id": "acct_2", "name": "B"}],
                "rules": [{"name": "x", "account_id": "acct_2", "sources": [-1001], "targets": [-1002]}],
            },
        )
        loaded = load_config(cfg_path)
        assert loaded.delay_seconds == 1.5
        assert len(loaded.accounts) == 2
        assert loaded.rules[0].account_id == "acct_2"

        # Test comment chunking modes: pack vs keep_batches
        batches = [["p1"], ["p2", "p3", "p4", "p5", "p6", "p7"], ["p8", "p9", "p10", "p11", "p12", "p13"]]
        packed = build_media_chunks(batches, "pack", lambda _x: "pv", max_album=10)
        assert [len(c) for c in packed] == [10, 3]
        kept = build_media_chunks(batches, "keep_batches", lambda _x: "pv", max_album=10)
        assert [len(c) for c in kept] == [1, 6, 6]
        mixed = build_media_chunks([["p1", "d1", "d2", "p2"]], "pack", lambda x: "pv" if x.startswith("p") else "doc", max_album=10)
        assert mixed == [["p1"], ["d1", "d2"], ["p2"]]


if __name__ == "__main__":
    test_clean_preserves_utf16_entities()
    test_strip_all_text_still_runs_skip_filter()
    test_unchanged_text_returns_false()
    test_stores_and_config_roundtrip()
    print("ALL TESTS PASSED")
