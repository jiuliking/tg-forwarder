import copy
import hashlib
import hmac
import logging
import os
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

import yaml

log = logging.getLogger("tg-forwarder")


def load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def normalize_peer(val: Any) -> int | str:
    if isinstance(val, int):
        return val
    s = str(val).strip()
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    return s


def session_prefix_for(session_dir: Path, account_id: str) -> Path:
    if account_id == "default":
        return session_dir / "userbot"
    return session_dir / f"acct_{account_id}"


@dataclass
class AccountConfig:
    id: str
    name: str
    enabled: bool = True


@dataclass
class CompiledReplace:
    regex: re.Pattern[str]
    repl: str


@dataclass
class CompiledRule:
    name: str
    enabled: bool
    account_id: str
    sources: list[int | str]
    targets: list[int | str]
    drop_author: bool = True
    fallback_reupload: bool = True
    skip_text_only: bool = False
    strip_all_text: bool = False
    skip_empty_text_only: bool = True
    disable_link_preview: bool = False
    skip_patterns: list[re.Pattern[str]] = field(default_factory=list)
    strip_text_patterns: list[re.Pattern[str]] = field(default_factory=list)
    strip_patterns: list[re.Pattern[str]] = field(default_factory=list)
    replace_patterns: list[CompiledReplace] = field(default_factory=list)


@dataclass
class AppConfig:
    delay_seconds: float
    temp_dir: Path
    max_download_mb: int
    dedup_ttl_hours: int
    accounts: list[AccountConfig]
    rules: list[CompiledRule]


def _compile_patterns(items: Sequence[str] | None, field_name: str) -> list[re.Pattern[str]]:
    compiled: list[re.Pattern[str]] = []
    for pat in items or []:
        if not str(pat):
            continue
        try:
            compiled.append(re.compile(pat, re.MULTILINE))
        except re.error as exc:
            raise ValueError(f"Invalid regex in {field_name}: {pat!r} ({exc})") from exc
    return compiled


def compile_rule_dict(r: dict[str, Any], idx: int = 0) -> CompiledRule:
    name = str(r.get("name") or f"rule-{idx + 1}")
    account_id = str(r.get("account_id") or "default").strip() or "default"
    replaces: list[CompiledReplace] = []
    for rep in r.get("replace_patterns") or []:
        pat = str(rep.get("pattern") or "")
        if not pat:
            continue
        repl = str(rep.get("repl", ""))
        try:
            replaces.append(CompiledReplace(re.compile(pat, re.MULTILINE), repl))
        except re.error as exc:
            raise ValueError(f"Invalid replace regex in {name}: {pat!r} ({exc})") from exc
    return CompiledRule(
        name=name,
        enabled=bool(r.get("enabled", True)),
        account_id=account_id,
        sources=[normalize_peer(x) for x in (r.get("sources") or []) if str(x).strip()],
        targets=[normalize_peer(x) for x in (r.get("targets") or []) if str(x).strip()],
        drop_author=bool(r.get("drop_author", True)),
        fallback_reupload=bool(r.get("fallback_reupload", True)),
        skip_text_only=bool(r.get("skip_text_only", False)),
        strip_all_text=bool(r.get("strip_all_text", False)),
        skip_empty_text_only=bool(r.get("skip_empty_text_only", True)),
        disable_link_preview=bool(r.get("disable_link_preview", False)),
        skip_patterns=_compile_patterns(r.get("skip_patterns"), f"{name}.skip_patterns"),
        strip_text_patterns=_compile_patterns(r.get("strip_text_patterns"), f"{name}.strip_text_patterns"),
        strip_patterns=_compile_patterns(r.get("strip_patterns"), f"{name}.strip_patterns"),
        replace_patterns=replaces,
    )


def load_raw_config(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        raw: dict[str, Any] = {}
    else:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    raw.setdefault("global", {})
    if not raw.get("accounts"):
        raw["accounts"] = [{"id": "default", "name": "默认账号", "enabled": True}]
    rules = raw.get("rules") or []
    for r in rules:
        if not r.get("account_id"):
            r["account_id"] = raw["accounts"][0]["id"]
    raw["rules"] = rules
    return raw


def save_raw_config(config_path: Path, data: dict[str, Any]) -> None:
    for idx, r in enumerate(data.get("rules") or []):
        compile_rule_dict(r, idx)
    tmp_path = config_path.with_suffix(".yaml.tmp")
    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, indent=2)
    tmp_path.write_text(text, encoding="utf-8")
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, config_path)


def resolve_tg_api(raw: dict[str, Any]) -> tuple[int, str, bool]:
    g = raw.get("global") or {}
    custom_id = g.get("api_id")
    custom_hash = str(g.get("api_hash") or "").strip()
    env_id = int(os.environ.get("TELEGRAM_API_ID") or 0)
    env_hash = str(os.environ.get("TELEGRAM_API_HASH") or "").strip()
    is_custom = bool(custom_id) or bool(custom_hash)
    api_id = int(custom_id) if custom_id else env_id
    api_hash = custom_hash if custom_hash else env_hash
    return api_id, api_hash, is_custom


def load_config(config_path: Path) -> AppConfig:
    raw = load_raw_config(config_path)
    g = raw.get("global") or {}
    accounts_out = [
        AccountConfig(
            id=str(a.get("id") or "default").strip(),
            name=str(a.get("name") or a.get("id") or "默认账号").strip(),
            enabled=bool(a.get("enabled", True)),
        )
        for a in (raw.get("accounts") or [])
    ]
    rules_out = [compile_rule_dict(r, idx) for idx, r in enumerate(raw.get("rules") or [])]
    return AppConfig(
        delay_seconds=float(g.get("delay_seconds", 1.0)),
        temp_dir=Path(g.get("temp_dir") or (Path(os.environ.get("TG_FWD_DIR") or Path(__file__).resolve().parent) / "cache")),
        max_download_mb=int(g.get("max_download_mb", 2000)),
        dedup_ttl_hours=int(g.get("dedup_ttl_hours", 48)),
        accounts=accounts_out,
        rules=rules_out,
    )


def _utf16_to_cp_map(text: str) -> list[int]:
    mapping: list[int] = []
    for cp_idx, ch in enumerate(text):
        units = len(ch.encode("utf-16-le")) // 2
        mapping.extend([cp_idx] * units)
    mapping.append(len(text))
    return mapping


def _cp_to_utf16_map(text: str) -> list[int]:
    mapping: list[int] = [0] * (len(text) + 1)
    u = 0
    for cp_idx, ch in enumerate(text):
        mapping[cp_idx] = u
        u += len(ch.encode("utf-16-le")) // 2
    mapping[len(text)] = u
    return mapping


def _apply_regex_with_spans(
    text: str,
    regex: re.Pattern[str],
    repl_fn,
    spans: list[tuple[int, int, Any]],
) -> tuple[str, list[tuple[int, int, Any]]]:
    matches = list(regex.finditer(text))
    if not matches:
        return text, spans

    new_text = text
    new_spans = list(spans)
    for m in reversed(matches):
        m_start, m_end = m.span()
        repl_text = repl_fn(m)
        delta = len(repl_text) - (m_end - m_start)
        new_text = new_text[:m_start] + repl_text + new_text[m_end:]

        updated: list[tuple[int, int, Any]] = []
        for e_start, e_end, ent in new_spans:
            if e_end <= m_start:
                updated.append((e_start, e_end, ent))
            elif e_start >= m_end:
                updated.append((e_start + delta, e_end + delta, ent))
            elif e_start >= m_start and e_end <= m_end:
                continue
            elif e_start < m_start and e_end > m_end:
                updated.append((e_start, e_end + delta, ent))
            elif e_start < m_start < e_end <= m_end:
                if m_start > e_start:
                    updated.append((e_start, m_start, ent))
            else:
                ns = m_start + len(repl_text)
                ne = e_end + delta
                if ne > ns:
                    updated.append((ns, ne, ent))
        new_spans = updated
    return new_text, new_spans


def should_skip_text(text: str, rule: CompiledRule) -> Optional[str]:
    if not text:
        return None
    for pat in rule.skip_patterns:
        if pat.search(text):
            return pat.pattern
    return None


def should_strip_all_text(text: str, rule: CompiledRule) -> Optional[str]:
    if not text:
        return None
    for pat in rule.strip_text_patterns:
        if pat.search(text):
            return pat.pattern
    return None


def clean_message_text(
    text: Optional[str],
    entities: Optional[Sequence[Any]],
    rule: CompiledRule,
) -> tuple[str, Optional[list[Any]], bool]:
    orig = text or ""
    if not orig or (not rule.strip_patterns and not rule.replace_patterns):
        return orig, (list(entities) if entities else None), False

    u2c = _utf16_to_cp_map(orig)
    max_u = len(u2c) - 1
    spans: list[tuple[int, int, Any]] = []
    for ent in entities or []:
        u_start = max(0, min(ent.offset, max_u))
        u_end = max(u_start, min(ent.offset + ent.length, max_u))
        c_start = u2c[u_start]
        c_end = u2c[u_end]
        if c_end > c_start:
            spans.append((c_start, c_end, copy.copy(ent)))

    cur_text = orig
    cur_spans = spans

    for pat in rule.strip_patterns:
        cur_text, cur_spans = _apply_regex_with_spans(cur_text, pat, lambda _m: "", cur_spans)

    for rep in rule.replace_patterns:
        cur_text, cur_spans = _apply_regex_with_spans(
            cur_text, rep.regex, lambda m, r=rep.repl: m.expand(r), cur_spans
        )

    cur_text, cur_spans = _apply_regex_with_spans(
        cur_text, re.compile(r"\n{3,}"), lambda _m: "\n\n", cur_spans
    )
    cur_text, cur_spans = _apply_regex_with_spans(
        cur_text, re.compile(r"^\s+"), lambda _m: "", cur_spans
    )
    cur_text, cur_spans = _apply_regex_with_spans(
        cur_text, re.compile(r"\s+$"), lambda _m: "", cur_spans
    )

    if cur_text == orig:
        return orig, (list(entities) if entities else None), False

    c2u = _cp_to_utf16_map(cur_text)
    out_entities: list[Any] = []
    for c_start, c_end, ent in cur_spans:
        c_start = max(0, min(c_start, len(cur_text)))
        c_end = max(c_start, min(c_end, len(cur_text)))
        if c_end <= c_start:
            continue
        ent.offset = c2u[c_start]
        ent.length = c2u[c_end] - c2u[c_start]
        if ent.length > 0:
            out_entities.append(ent)

    return cur_text, (out_entities or None), True


class DedupStore:
    def __init__(self, db_path: Path) -> None:
        self.conn = sqlite3.connect(db_path)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS forwarded_v2 (
                account_id TEXT NOT NULL,
                source_id INTEGER NOT NULL,
                msg_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (account_id, source_id, msg_id, target_id)
            )
            """
        )
        old_exists = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='forwarded'"
        ).fetchone()
        if old_exists:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO forwarded_v2 (account_id, source_id, msg_id, target_id, created_at)
                SELECT 'default', source_id, msg_id, target_id, created_at FROM forwarded
                """
            )
            self.conn.execute("DROP TABLE forwarded")
        self.conn.commit()

    def seen(self, source_id: int, msg_id: int, target_id: int, account_id: str = "default") -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM forwarded_v2 WHERE account_id=? AND source_id=? AND msg_id=? AND target_id=?",
            (account_id, source_id, msg_id, target_id),
        ).fetchone()
        return row is not None

    def mark(self, source_id: int, msg_id: int, target_id: int, account_id: str = "default") -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO forwarded_v2 (account_id, source_id, msg_id, target_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (account_id, source_id, msg_id, target_id, int(time.time())),
        )
        self.conn.commit()

    def prune(self, ttl_hours: int) -> None:
        cutoff = int(time.time()) - max(1, ttl_hours) * 3600
        self.conn.execute("DELETE FROM forwarded_v2 WHERE created_at < ?", (cutoff,))
        self.conn.commit()


class HistoryStore:
    def __init__(self, db_path: Path) -> None:
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                account_id TEXT NOT NULL DEFAULT 'default',
                account_name TEXT NOT NULL DEFAULT '默认账号',
                rule_name TEXT NOT NULL,
                source_id INTEGER NOT NULL,
                source_title TEXT NOT NULL,
                target_id INTEGER NOT NULL,
                target_title TEXT NOT NULL,
                msg_id INTEGER NOT NULL,
                item_count INTEGER NOT NULL DEFAULT 1,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                preview TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT ''
            )
            """
        )
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(history)").fetchall()}
        if "account_id" not in cols:
            self.conn.execute("ALTER TABLE history ADD COLUMN account_id TEXT NOT NULL DEFAULT 'default'")
        if "account_name" not in cols:
            self.conn.execute("ALTER TABLE history ADD COLUMN account_name TEXT NOT NULL DEFAULT '默认账号'")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_history_ts ON history(ts DESC)")
        self.conn.commit()

    def record(
        self,
        *,
        account_id: str = "default",
        account_name: str = "默认账号",
        rule_name: str,
        source_id: int,
        source_title: str,
        target_id: int,
        target_title: str,
        msg_id: int,
        item_count: int,
        mode: str,
        status: str,
        preview: str = "",
        detail: str = "",
    ) -> None:
        snippet = (preview or "").replace("\n", " ").strip()[:160]
        self.conn.execute(
            """
            INSERT INTO history
            (ts, account_id, account_name, rule_name, source_id, source_title, target_id, target_title, msg_id, item_count, mode, status, preview, detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(time.time()),
                account_id,
                account_name or account_id,
                rule_name,
                int(source_id),
                source_title or str(source_id),
                int(target_id),
                target_title or str(target_id),
                int(msg_id),
                int(item_count),
                mode,
                status,
                snippet,
                (detail or "")[:400],
            ),
        )
        self.conn.execute(
            "DELETE FROM history WHERE id NOT IN (SELECT id FROM history ORDER BY id DESC LIMIT 5000)"
        )
        self.conn.commit()

    def recent(self, limit: int = 50, status: str = "", rule: str = "", account_id: str = "") -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if rule:
            clauses.append("rule_name = ?")
            params.append(rule)
        if account_id:
            clauses.append("account_id = ?")
            params.append(account_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(1, min(limit, 500)))
        rows = self.conn.execute(
            f"SELECT * FROM history {where} ORDER BY id DESC LIMIT ?", params
        ).fetchall()
        return [dict(r) for r in rows]

    def stats_and_activity(self) -> dict[str, Any]:
        now = int(time.time())
        lt = time.localtime(now)
        today_start = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))

        totals = self.conn.execute(
            """
            SELECT
                SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS total_success,
                SUM(CASE WHEN status='skipped' THEN 1 ELSE 0 END) AS total_skipped,
                SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS total_error,
                SUM(CASE WHEN status='success' AND ts >= ? THEN 1 ELSE 0 END) AS today_success,
                SUM(CASE WHEN status='skipped' AND ts >= ? THEN 1 ELSE 0 END) AS today_skipped
            FROM history
            """,
            (today_start, today_start),
        ).fetchone()

        days: list[dict[str, Any]] = []
        for offset in range(6, -1, -1):
            d_start = today_start - offset * 86400
            d_end = d_start + 86400
            cnt = self.conn.execute(
                "SELECT COUNT(*) FROM history WHERE status='success' AND ts >= ? AND ts < ?",
                (d_start, d_end),
            ).fetchone()[0]
            label = time.strftime("%m-%d", time.localtime(d_start))
            days.append({"label": label, "count": cnt, "current": offset == 0})

        return {
            "total_success": totals["total_success"] or 0,
            "total_skipped": totals["total_skipped"] or 0,
            "total_error": totals["total_error"] or 0,
            "today_success": totals["today_success"] or 0,
            "today_skipped": totals["today_skipped"] or 0,
            "activity_7d": days,
        }


class AuthStore:
    SESSION_TTL = 14 * 86400

    def __init__(self, db_path: Path) -> None:
        self.conn = sqlite3.connect(db_path)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS auth_kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS web_sessions (
                token TEXT PRIMARY KEY,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            )
            """
        )
        self.conn.commit()
        self.generated_password: Optional[str] = None
        self._ensure_initialized()

    @staticmethod
    def _hash_password(password: str, salt_hex: Optional[str] = None) -> str:
        salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 150000)
        return f"pbkdf2_sha256${salt.hex()}${dk.hex()}"

    @classmethod
    def _verify_hash(cls, password: str, stored: str) -> bool:
        try:
            algo, salt_hex, expected_hex = stored.split("$", 2)
            if algo != "pbkdf2_sha256":
                return False
            candidate = cls._hash_password(password, salt_hex)
            return hmac.compare_digest(candidate, stored)
        except Exception:
            return False

    def _get(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM auth_kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def _set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO auth_kv (key, value) VALUES (?, ?)", (key, value)
        )
        self.conn.commit()

    def _ensure_initialized(self) -> None:
        if not self._get("password_hash"):
            pwd = secrets.token_urlsafe(12)
            self._set("password_hash", self._hash_password(pwd))
            self.generated_password = pwd
        if not self._get("access_token"):
            self._set("access_token", secrets.token_urlsafe(24))

    def get_access_token(self) -> str:
        return self._get("access_token") or ""

    def rotate_access_token(self) -> str:
        tok = secrets.token_urlsafe(24)
        self._set("access_token", tok)
        return tok

    def verify_password(self, password: str) -> bool:
        stored = self._get("password_hash") or ""
        return self._verify_hash(password, stored)

    def verify_access_token(self, token: str) -> bool:
        expected = self.get_access_token()
        return bool(token) and bool(expected) and hmac.compare_digest(token, expected)

    def change_password(self, current_password: str, new_password: str) -> bool:
        if not self.verify_password(current_password):
            return False
        self._set("password_hash", self._hash_password(new_password))
        return True

    def create_session(self) -> str:
        tok = secrets.token_urlsafe(32)
        now = int(time.time())
        self.conn.execute("DELETE FROM web_sessions WHERE expires_at < ?", (now,))
        self.conn.execute(
            "INSERT INTO web_sessions (token, created_at, expires_at) VALUES (?, ?, ?)",
            (tok, now, now + self.SESSION_TTL),
        )
        self.conn.commit()
        return tok

    def verify_session(self, tok: str) -> bool:
        if not tok:
            return False
        now = int(time.time())
        row = self.conn.execute(
            "SELECT 1 FROM web_sessions WHERE token=? AND expires_at >= ?", (tok, now)
        ).fetchone()
        return row is not None

    def delete_session(self, tok: str) -> None:
        if not tok:
            return
        self.conn.execute("DELETE FROM web_sessions WHERE token=?", (tok,))
        self.conn.commit()
