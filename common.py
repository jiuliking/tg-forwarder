import copy
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

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
    collect_comments: bool = False
    comment_wait_seconds: int = 30
    comment_merge_mode: str = "pack"
    skip_patterns: list[re.Pattern[str]] = field(default_factory=list)
    strip_text_patterns: list[re.Pattern[str]] = field(default_factory=list)
    strip_patterns: list[re.Pattern[str]] = field(default_factory=list)
    replace_patterns: list[CompiledReplace] = field(default_factory=list)
    id: str = ""


DEFAULT_ALBUM_QUIET_SECONDS = 15
DEFAULT_ALBUM_MAX_WAIT_SECONDS = 45


@dataclass
class AppConfig:
    delay_seconds: float
    temp_dir: Path
    max_download_mb: int
    dedup_ttl_hours: int
    accounts: list[AccountConfig]
    rules: list[CompiledRule]
    log_retention_days: int = 30
    log_max_rows: int = 5000
    album_quiet_seconds: int = DEFAULT_ALBUM_QUIET_SECONDS
    album_max_wait_seconds: int = DEFAULT_ALBUM_MAX_WAIT_SECONDS


REGEX_KINDS = ("skip", "strip_text", "strip", "replace")


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


def _validate_replace_template(rx: re.Pattern[str], repl: str) -> None:
    parser_mod = getattr(re, "_parser", None)
    parse_tpl = getattr(parser_mod, "parse_template", None) if parser_mod else None
    if parse_tpl is not None:
        try:
            parse_tpl(repl, rx)
        except (re.error, IndexError) as exc:
            raise ValueError(f"Invalid replacement template {repl!r}: {exc}") from exc


def validate_regex_rule_dict(rx: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(rx, dict):
        raise ValueError("正则条目格式无效")
    rx_id = str(rx.get("id") or "").strip() or ("rx_" + secrets.token_hex(4))
    name = str(rx.get("name") or "").strip()
    if not name:
        raise ValueError("请填写正则名称")
    kind = str(rx.get("kind") or "").strip()
    if kind not in REGEX_KINDS:
        raise ValueError(f"无效的正则类型: {kind!r}")
    note = str(rx.get("note") or "").strip()

    if kind == "replace":
        replaces: list[dict[str, str]] = []
        for rep in rx.get("replace_patterns") or []:
            if not isinstance(rep, dict):
                continue
            pat = str(rep.get("pattern") or "").strip()
            if not pat:
                continue
            repl = str(rep.get("repl", ""))
            try:
                compiled = re.compile(pat, re.MULTILINE)
            except re.error as exc:
                raise ValueError(f"Invalid replace regex in {name}: {pat!r} ({exc})") from exc
            _validate_replace_template(compiled, repl)
            replaces.append({"pattern": pat, "repl": repl})
        if not replaces:
            raise ValueError(f"正则「{name}」至少需要一条替换规则")
        out: dict[str, Any] = {
            "id": rx_id,
            "name": name,
            "kind": kind,
            "replace_patterns": replaces,
        }
        if note:
            out["note"] = note
        return out

    patterns = [str(p).strip() for p in (rx.get("patterns") or []) if str(p).strip()]
    if not patterns:
        raise ValueError(f"正则「{name}」至少需要一条正则表达式")
    _compile_patterns(patterns, f"regex.{name}")
    out = {
        "id": rx_id,
        "name": name,
        "kind": kind,
        "patterns": patterns,
    }
    if note:
        out["note"] = note
    return out


def build_regex_map(regex_rules: Sequence[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for rx in regex_rules or []:
        if isinstance(rx, dict) and rx.get("id"):
            out[str(rx["id"]).strip()] = rx
    return out


def resolve_rule_patterns(
    r: dict[str, Any],
    regex_map: Optional[dict[str, dict[str, Any]]] = None,
) -> tuple[list[str], list[str], list[str], list[dict[str, str]]]:
    skip_pats: list[str] = []
    strip_text_pats: list[str] = []
    strip_pats: list[str] = []
    replace_pats: list[dict[str, str]] = []

    if regex_map:
        seen_ids: set[str] = set()
        for raw_id in r.get("regex_ids") or []:
            rx_id = str(raw_id).strip()
            if not rx_id or rx_id in seen_ids:
                continue
            seen_ids.add(rx_id)
            rx = regex_map.get(rx_id)
            if not rx:
                continue
            kind = str(rx.get("kind") or "").strip()
            if kind == "skip":
                skip_pats.extend(str(p) for p in (rx.get("patterns") or []) if str(p).strip())
            elif kind == "strip_text":
                strip_text_pats.extend(str(p) for p in (rx.get("patterns") or []) if str(p).strip())
            elif kind == "strip":
                strip_pats.extend(str(p) for p in (rx.get("patterns") or []) if str(p).strip())
            elif kind == "replace":
                for rep in rx.get("replace_patterns") or []:
                    if isinstance(rep, dict) and str(rep.get("pattern") or "").strip():
                        replace_pats.append({"pattern": str(rep["pattern"]), "repl": str(rep.get("repl", ""))})

    skip_pats.extend(str(p) for p in (r.get("skip_patterns") or []) if str(p).strip())
    strip_text_pats.extend(str(p) for p in (r.get("strip_text_patterns") or []) if str(p).strip())
    strip_pats.extend(str(p) for p in (r.get("strip_patterns") or []) if str(p).strip())
    for rep in r.get("replace_patterns") or []:
        if isinstance(rep, dict) and str(rep.get("pattern") or "").strip():
            replace_pats.append({"pattern": str(rep["pattern"]), "repl": str(rep.get("repl", ""))})

    return skip_pats, strip_text_pats, strip_pats, replace_pats


def compile_rule_dict(
    r: dict[str, Any],
    idx: int = 0,
    default_comment_wait: int = 30,
    default_comment_mode: str = "pack",
    regex_map: Optional[dict[str, dict[str, Any]]] = None,
) -> CompiledRule:
    name = str(r.get("name") or f"rule-{idx + 1}")
    account_id = str(r.get("account_id") or "default").strip() or "default"
    skip_pats, strip_text_pats, strip_pats, raw_replaces = resolve_rule_patterns(r, regex_map=regex_map)
    replaces: list[CompiledReplace] = []
    for rep in raw_replaces:
        pat = str(rep.get("pattern") or "")
        if not pat:
            continue
        repl = str(rep.get("repl", ""))
        try:
            rx = re.compile(pat, re.MULTILINE)
        except re.error as exc:
            raise ValueError(f"Invalid replace regex in {name}: {pat!r} ({exc})") from exc
        _validate_replace_template(rx, repl)
        replaces.append(CompiledReplace(rx, repl))
    raw_cw = r.get("comment_wait_seconds")
    cw = default_comment_wait if raw_cw in (None, "") else max(5, min(300, int(raw_cw)))
    raw_cm = str(r.get("comment_merge_mode") or "").strip()
    cm = raw_cm if raw_cm in ("pack", "keep_batches") else (
        "keep_batches" if default_comment_mode == "keep_batches" else "pack"
    )
    return CompiledRule(
        name=name,
        id=str(r.get("id") or ""),
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
        collect_comments=bool(r.get("collect_comments", False)),
        comment_wait_seconds=cw,
        comment_merge_mode=cm,
        skip_patterns=_compile_patterns(skip_pats, f"{name}.skip_patterns"),
        strip_text_patterns=_compile_patterns(strip_text_pats, f"{name}.strip_text_patterns"),
        strip_patterns=_compile_patterns(strip_pats, f"{name}.strip_patterns"),
        replace_patterns=replaces,
    )


def load_raw_config(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        raw: dict[str, Any] = {}
    else:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    raw.setdefault("global", {})
    raw.setdefault("regex_rules", [])
    if not raw.get("accounts"):
        raw["accounts"] = [{"id": "default", "name": "默认账号", "enabled": True}]
    rules = raw.get("rules") or []
    for idx, r in enumerate(rules):
        if not r.get("account_id"):
            r["account_id"] = raw["accounts"][0]["id"]
        if not r.get("id"):
            # Stable before the first save, when the IDs are persisted with the rules.
            initial = json.dumps([idx, r], sort_keys=True, ensure_ascii=False, default=str)
            r["id"] = "rule_" + hashlib.sha256(initial.encode()).hexdigest()[:24]
    raw["rules"] = rules
    return raw


def parse_album_wait_settings(settings: dict[str, Any]) -> tuple[int, int]:
    quiet = settings.get("album_quiet_seconds", DEFAULT_ALBUM_QUIET_SECONDS)
    maximum = settings.get("album_max_wait_seconds", DEFAULT_ALBUM_MAX_WAIT_SECONDS)
    for value, label in ((quiet, "相册静默等待"), (maximum, "相册最长收集时间")):
        if type(value) is not int or not 1 <= value <= 300:
            raise ValueError(f"{label}必须是 1 到 300 秒的整数")
    if maximum < quiet:
        raise ValueError("相册最长收集时间必须大于或等于静默等待时间")
    return quiet, maximum


def save_raw_config(config_path: Path, data: dict[str, Any]) -> None:
    parse_album_wait_settings(data.get("global") or {})
    regex_rules = data.get("regex_rules") or []
    validated_regex = [validate_regex_rule_dict(rx) for rx in regex_rules]
    if "regex_rules" in data or validated_regex:
        data["regex_rules"] = validated_regex
    regex_map = build_regex_map(validated_regex)
    for idx, r in enumerate(data.get("rules") or []):
        compile_rule_dict(r, idx, regex_map=regex_map)
    tmp_path = config_path.with_name(f".{config_path.name}.{secrets.token_hex(4)}.tmp")
    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, indent=2)
    try:
        tmp_path.write_text(text, encoding="utf-8")
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, config_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


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
    album_quiet, album_max_wait = parse_album_wait_settings(g)
    accounts_out = [
        AccountConfig(
            id=str(a.get("id") or "default").strip(),
            name=str(a.get("name") or a.get("id") or "默认账号").strip(),
            enabled=bool(a.get("enabled", True)),
        )
        for a in (raw.get("accounts") or [])
    ]
    default_cw = max(5, min(300, int(g.get("comment_wait_seconds", 30) or 30)))
    default_cm = "keep_batches" if str(g.get("comment_merge_mode") or "").strip() == "keep_batches" else "pack"
    regex_map = build_regex_map(raw.get("regex_rules") or [])
    rules_out = [
        compile_rule_dict(r, idx, default_comment_wait=default_cw, default_comment_mode=default_cm, regex_map=regex_map)
        for idx, r in enumerate(raw.get("rules") or [])
    ]
    return AppConfig(
        delay_seconds=float(g.get("delay_seconds", 1.0)),
        temp_dir=Path(g.get("temp_dir") or (Path(os.environ.get("TG_FWD_DIR") or Path(__file__).resolve().parent) / "cache")),
        max_download_mb=int(g.get("max_download_mb", 2000)),
        dedup_ttl_hours=int(g.get("dedup_ttl_hours", 48)),
        accounts=accounts_out,
        rules=rules_out,
        log_retention_days=max(0, int(g.get("log_retention_days", 30))),
        log_max_rows=max(100, int(g.get("log_max_rows", 5000))),
        album_quiet_seconds=album_quiet,
        album_max_wait_seconds=album_max_wait,
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


def _split_top_level_alternation(body: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    escaped = False
    in_class = False
    depth = 0
    for ch in body:
        if escaped:
            buf.append(ch)
            escaped = False
            continue
        if ch == "\\":
            buf.append(ch)
            escaped = True
            continue
        if in_class:
            buf.append(ch)
            if ch == "]":
                in_class = False
            continue
        if ch == "[":
            in_class = True
            buf.append(ch)
        elif ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch == "|" and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def _unwrap_single_group(body: str) -> tuple[str, str, str]:
    if not (body.startswith("(") and body.endswith(")")):
        return "", body, ""
    escaped = False
    in_class = False
    depth = 0
    for i, ch in enumerate(body):
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if in_class:
            if ch == "]":
                in_class = False
            continue
        if ch == "[":
            in_class = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0 and i < len(body) - 1:
                return "", body, ""
    if body.startswith("(?:"):
        return "(?:", body[3:-1], ")"
    if body.startswith("(?"):
        return "", body, ""
    return "(", body[1:-1], ")"


def find_matched_regex_branches(text: Optional[str], pattern: str) -> list[str]:
    if not text or not pattern:
        return []
    flags = re.MULTILINE
    body = pattern
    m_flag = re.match(r"^\(\?([aiLmsux]+)\)", body)
    if m_flag:
        fl = m_flag.group(1)
        if "i" in fl:
            flags |= re.IGNORECASE
        if "s" in fl:
            flags |= re.DOTALL
        if "x" in fl:
            flags |= re.VERBOSE
        if "a" in fl:
            flags |= re.ASCII
        body = body[m_flag.end() :]

    parts = _split_top_level_alternation(body)
    if len(parts) == 1:
        _, inner, _ = _unwrap_single_group(body)
        if inner != body:
            parts = _split_top_level_alternation(inner)

    hits: list[str] = []
    seen: set[str] = set()
    for part in parts:
        if not part or part in seen:
            continue
        try:
            if re.search(part, text, flags):
                seen.add(part)
                hits.append(part)
        except re.error:
            continue
    return hits


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
        try:
            cur_text, cur_spans = _apply_regex_with_spans(
                cur_text, rep.regex, lambda m, r=rep.repl: m.expand(r), cur_spans
            )
        except (re.error, IndexError) as exc:
            raise ValueError(f"Invalid replacement template {rep.repl!r}: {exc}") from exc

    if cur_text == orig:
        return orig, (list(entities) if entities else None), False

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


def utf16_len(text: Optional[str]) -> int:
    if not text:
        return 0
    return len(text.encode("utf-16-le")) // 2


def truncate_text_with_entities(
    text: Optional[str],
    entities: Optional[Sequence[Any]],
    max_utf16: int = 1024,
    ellipsis: str = "…",
) -> tuple[str, Optional[list[Any]]]:
    orig = text or ""
    if not orig or utf16_len(orig) <= max_utf16:
        return orig, (list(entities) if entities else None)

    ell_u = utf16_len(ellipsis)
    budget = max(1, max_utf16 - ell_u)
    u = 0
    cut_cp = 0
    for idx, ch in enumerate(orig):
        ch_u = len(ch.encode("utf-16-le")) // 2
        if u + ch_u > budget:
            break
        u += ch_u
        cut_cp = idx + 1

    clipped = orig[:cut_cp].rstrip()
    cut_u = utf16_len(clipped)
    out_text = clipped + ellipsis

    out_ents: list[Any] = []
    for ent in entities or []:
        e_start = int(getattr(ent, "offset", 0))
        e_end = e_start + int(getattr(ent, "length", 0))
        if e_start >= cut_u:
            continue
        ne_end = min(e_end, cut_u)
        if ne_end <= e_start:
            continue
        ne = copy.copy(ent)
        ne.offset = e_start
        ne.length = ne_end - e_start
        out_ents.append(ne)
    return out_text, (out_ents or None)


def build_media_chunks(
    batches: Sequence[Sequence[Any]],
    mode: str,
    bucket_fn,
    max_album: int = 10,
) -> list[list[Any]]:
    def _split_compatible(items: Sequence[Any]) -> list[list[Any]]:
        out: list[list[Any]] = []
        cur: list[Any] = []
        cur_bucket: Optional[str] = None
        for item in items:
            b = bucket_fn(item)
            if b == "single":
                if cur:
                    out.append(cur)
                    cur = []
                    cur_bucket = None
                out.append([item])
                continue
            if cur and (cur_bucket != b or len(cur) >= max_album):
                out.append(cur)
                cur = []
            cur.append(item)
            cur_bucket = b
        if cur:
            out.append(cur)
        return out

    if mode == "keep_batches":
        chunks: list[list[Any]] = []
        for batch in batches:
            if batch:
                chunks.extend(_split_compatible(batch))
        return chunks

    flat: list[Any] = []
    for batch in batches:
        flat.extend(batch)
    return _split_compatible(flat)


def _open_sqlite(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=15.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
    except sqlite3.DatabaseError:
        pass
    try:
        if Path(db_path).exists():
            os.chmod(db_path, 0o600)
    except OSError:
        pass
    return conn


class DedupStore:
    def __init__(self, db_path: Path) -> None:
        self.conn = _open_sqlite(db_path)
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
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS delivery_parts (
                account_id TEXT NOT NULL,
                source_id INTEGER NOT NULL,
                msg_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                delivery_key TEXT NOT NULL,
                part TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (account_id, source_id, msg_id, target_id, delivery_key, part)
            )
        """)
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_delivery_parts_ts ON delivery_parts(created_at)")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS source_recovery (
                account_id TEXT NOT NULL,
                source_id INTEGER NOT NULL,
                resume_from INTEGER NOT NULL,
                PRIMARY KEY (account_id, source_id)
            )
        """)
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
        self.mark_many(source_id, (msg_id,), target_id, account_id)

    def mark_many(self, source_id: int, msg_ids: Iterable[int], target_id: int, account_id: str = "default") -> None:
        now = int(time.time())
        self.conn.executemany(
            "INSERT OR REPLACE INTO forwarded_v2 (account_id, source_id, msg_id, target_id, created_at) VALUES (?, ?, ?, ?, ?)",
            ((account_id, source_id, msg_id, target_id, now) for msg_id in msg_ids),
        )
        self.conn.commit()

    def prune(self, ttl_hours: int) -> None:
        cutoff = int(time.time()) - max(1, ttl_hours) * 3600
        self.conn.execute("DELETE FROM forwarded_v2 WHERE created_at < ?", (cutoff,))
        self.conn.commit()

    def delivery_parts(self, key: tuple[str, int, int, int, str]) -> set[str]:
        rows = self.conn.execute(
            "SELECT part FROM delivery_parts WHERE account_id=? AND source_id=? AND msg_id=? AND target_id=? AND delivery_key=?",
            key,
        )
        return {row[0] for row in rows}

    def recovery_floors(self, account_id: str) -> dict[int, int]:
        return dict(self.conn.execute(
            "SELECT source_id, resume_from FROM source_recovery WHERE account_id=?", (account_id,),
        ))

    def set_recovery_floors(self, account_id: str, floors: dict[int, int]) -> None:
        with self.conn:
            self.conn.executemany(
                """INSERT INTO source_recovery (account_id, source_id, resume_from) VALUES (?, ?, ?)
                   ON CONFLICT(account_id, source_id) DO UPDATE SET resume_from=excluded.resume_from""",
                ((account_id, source_id, resume_from) for source_id, resume_from in floors.items()),
            )

    def mark_delivery_parts(self, key: tuple[str, int, int, int, str], parts: Iterable[str]) -> None:
        now = int(time.time())
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO delivery_parts VALUES (?, ?, ?, ?, ?, ?, ?)",
                ((*key, part, now) for part in parts),
            )

    def prune_delivery_parts(self, retention_days: int) -> None:
        if retention_days > 0:
            cutoff = int(time.time()) - retention_days * 86400
            with self.conn:
                self.conn.execute("DELETE FROM delivery_parts WHERE created_at < ?", (cutoff,))


class HistoryStore:
    def __init__(self, db_path: Path, retention_days: int = 30, max_rows: int = 5000) -> None:
        self.db_path = Path(db_path)
        self.retention_days = max(0, int(retention_days))
        self.max_rows = max(100, int(max_rows))
        self.conn = _open_sqlite(db_path)
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
                detail TEXT NOT NULL DEFAULT '',
                matched_terms TEXT NOT NULL DEFAULT ''
            )
            """
        )
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(history)").fetchall()}
        if "account_id" not in cols:
            self.conn.execute("ALTER TABLE history ADD COLUMN account_id TEXT NOT NULL DEFAULT 'default'")
        if "account_name" not in cols:
            self.conn.execute("ALTER TABLE history ADD COLUMN account_name TEXT NOT NULL DEFAULT '默认账号'")
        if "matched_terms" not in cols:
            self.conn.execute("ALTER TABLE history ADD COLUMN matched_terms TEXT NOT NULL DEFAULT ''")
        for column in ("delivery_key", "rule_id", "album_group_id"):
            if column not in cols:
                self.conn.execute(f"ALTER TABLE history ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_history_ts ON history(ts DESC)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_history_source_msg ON history(account_id, source_id, msg_id)")
        self.conn.commit()

    def skipped_rule_names(self, account_id: str, source_id: int, msg_id: int) -> set[str]:
        rows = self.conn.execute(
            "SELECT rule_name FROM history WHERE account_id=? AND source_id=? AND msg_id=? AND status='skipped' AND target_id=0",
            (account_id, source_id, msg_id),
        )
        return {row[0] for row in rows}

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
        matched_terms: Sequence[str] = (),
        delivery_key: str = "",
        rule_id: str = "",
        album_group_id: str = "",
    ) -> None:
        snippet = (preview or "").replace("\n", " ").strip()[:160]
        terms_json = json.dumps(list(matched_terms), ensure_ascii=False) if matched_terms else ""
        self.conn.execute(
            """
            INSERT INTO history
            (ts, account_id, account_name, rule_name, source_id, source_title, target_id, target_title, msg_id, item_count, mode, status, preview, detail, matched_terms, delivery_key, rule_id, album_group_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                terms_json[:500],
                delivery_key,
                rule_id,
                album_group_id,
            ),
        )
        self._apply_prune_sql(self.retention_days, self.max_rows)
        self.conn.commit()

    def _apply_prune_sql(self, retention_days: int, max_rows: int) -> int:
        deleted = 0
        if retention_days > 0:
            cutoff = int(time.time()) - retention_days * 86400
            cur = self.conn.execute("DELETE FROM history WHERE ts < ?", (cutoff,))
            deleted += cur.rowcount or 0
        if max_rows > 0:
            cur = self.conn.execute(
                "DELETE FROM history WHERE id NOT IN (SELECT id FROM history ORDER BY id DESC LIMIT ?)",
                (max_rows,),
            )
            deleted += cur.rowcount or 0
        return deleted

    def prune(self, retention_days: Optional[int] = None, max_rows: Optional[int] = None) -> int:
        days = self.retention_days if retention_days is None else max(0, int(retention_days))
        rows = self.max_rows if max_rows is None else max(100, int(max_rows))
        deleted = self._apply_prune_sql(days, rows)
        self.conn.commit()
        return deleted

    def clear(self, status: str = "") -> int:
        st = str(status or "").strip()
        if st in ("success", "skipped", "error"):
            cur = self.conn.execute("DELETE FROM history WHERE status = ?", (st,))
        else:
            cur = self.conn.execute("DELETE FROM history")
        deleted = cur.rowcount or 0
        self.conn.commit()
        return deleted

    def get_by_id(self, row_id: int) -> Optional[dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM history WHERE id = ?", (int(row_id),)).fetchone()
        return dict(row) if row else None

    def stats_detail(self) -> dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT
                COUNT(*) AS total_count,
                COALESCE(SUM(CASE WHEN status='success' THEN 1 ELSE 0 END), 0) AS success_count,
                COALESCE(SUM(CASE WHEN status='skipped' THEN 1 ELSE 0 END), 0) AS skipped_count,
                COALESCE(SUM(CASE WHEN status='error' THEN 1 ELSE 0 END), 0) AS error_count,
                MIN(ts) AS oldest_ts,
                MAX(ts) AS newest_ts
            FROM history
            """
        ).fetchone()
        try:
            db_size = self.db_path.stat().st_size
        except OSError:
            db_size = 0
        return {
            "total_count": int(row["total_count"] or 0),
            "success_count": int(row["success_count"] or 0),
            "skipped_count": int(row["skipped_count"] or 0),
            "error_count": int(row["error_count"] or 0),
            "oldest_ts": int(row["oldest_ts"]) if row["oldest_ts"] is not None else None,
            "newest_ts": int(row["newest_ts"]) if row["newest_ts"] is not None else None,
            "db_size_bytes": db_size,
        }

    def recent(
        self,
        limit: int = 50,
        status: str = "",
        rule: str = "",
        account_id: str = "",
        offset: int = 0,
    ) -> list[dict[str, Any]]:
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
        lim = max(1, min(limit, 500))
        off = max(0, int(offset))
        rows = self.conn.execute(
            f"SELECT * FROM history {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            [*params, lim, off],
        ).fetchall()
        return [dict(r) for r in rows]

    def query_page(
        self,
        page: int = 1,
        page_size: int = 30,
        status: str = "",
        rule: str = "",
        account_id: str = "",
    ) -> dict[str, Any]:
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
        total = int(self.conn.execute(f"SELECT COUNT(*) FROM history {where}", params).fetchone()[0] or 0)
        size = max(1, min(int(page_size), 200))
        total_pages = max(1, (total + size - 1) // size)
        cur_page = max(1, min(int(page), total_pages))
        offset = (cur_page - 1) * size
        rows = self.conn.execute(
            f"SELECT * FROM history {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            [*params, size, offset],
        ).fetchall()
        return {
            "items": [dict(r) for r in rows],
            "total": total,
            "page": cur_page,
            "page_size": size,
            "total_pages": total_pages,
        }

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
        self.conn = _open_sqlite(db_path)
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
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO auth_kv (key, value) VALUES ('access_token', ?)", (tok,))
            self.conn.execute("DELETE FROM web_sessions")
        return tok

    def verify_password(self, password: str) -> bool:
        stored = self._get("password_hash") or ""
        return self._verify_hash(password, stored)

    def verify_access_token(self, token: str) -> bool:
        expected = self.get_access_token()
        return bool(token) and bool(expected) and token.isascii() and hmac.compare_digest(token, expected)

    def change_password(self, current_password: str, new_password: str, keep_session: str = "") -> bool:
        if not self.verify_password(current_password):
            return False
        self._set("password_hash", self._hash_password(new_password))
        if keep_session:
            self.conn.execute("DELETE FROM web_sessions WHERE token != ?", (keep_session,))
        else:
            self.conn.execute("DELETE FROM web_sessions")
        self.conn.commit()
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


if __name__ == "__main__":
    import argparse
    import getpass
    import sys

    parser = argparse.ArgumentParser(description="Manage the local Web Console password")
    parser.add_argument("command", choices=["reset-web-password"])
    parser.parse_args()
    if not sys.stdin.isatty():
        parser.error("Run this command in an interactive terminal")
    password = getpass.getpass("New Web Console password: ")
    if len(password) < 8 or password != getpass.getpass("Confirm password: "):
        parser.error("Use at least 8 characters and enter the same password twice")
    auth = AuthStore(Path(os.environ.get("TG_FWD_DIR") or Path(__file__).resolve().parent) / "state.db")
    with auth.conn:
        auth.conn.execute("INSERT OR REPLACE INTO auth_kv (key, value) VALUES ('password_hash', ?)", (auth._hash_password(password),))
        auth.conn.execute("DELETE FROM web_sessions")
    print("Web Console password updated. Existing web sessions revoked.")
