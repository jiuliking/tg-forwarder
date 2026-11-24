import asyncio
import base64
import hashlib
import io
import logging
import os
import secrets
from pathlib import Path
from typing import Any

import qrcode
from aiohttp import web
from telethon import utils
from telethon.errors import PhoneCodeExpiredError, PhoneCodeInvalidError, RPCError, SessionPasswordNeededError, UnauthorizedError

from common import (
    AuthStore,
    build_regex_map,
    clean_message_text,
    compile_rule_dict,
    load_raw_config,
    parse_album_wait_settings,
    parse_comment_extend_settings,
    resolve_rule_patterns,
    resolve_tg_api,
    save_raw_config,
    session_prefix_for,
    should_skip_text,
    should_strip_all_text,
    validate_regex_rule_dict,
)

log = logging.getLogger("tg-forwarder.web")

SESSION_COOKIE = "tg_fwd_session"


class WebConsole:
    def __init__(self, manager: Any, auth: AuthStore, config_path: Path, session_dir: Path, ui_dir: Path) -> None:
        self.manager = manager
        self.auth = auth
        self.config_path = config_path
        self.session_dir = session_dir
        self.ui_dir = ui_dir

        self._qr_objs: dict[str, Any] = {}
        self._qr_tasks: dict[str, asyncio.Task[None]] = {}
        self._qr_states: dict[str, dict[str, Any]] = {}
        self._phone_states: dict[str, dict[str, Any]] = {}
        self._config_lock = asyncio.Lock()

    @staticmethod
    def _is_https(request: web.Request) -> bool:
        return request.scheme == "https" or request.headers.get("X-Forwarded-Proto", "").lower() == "https"

    @staticmethod
    async def _read_json(request: web.Request, allow_empty: bool = False) -> dict[str, Any]:
        if allow_empty and not request.can_read_body:
            return {}
        try:
            data = await request.json()
        except Exception:
            if allow_empty:
                return {}
            raise web.HTTPBadRequest(text='{"error":"请求体不是有效的 JSON"}', content_type="application/json")
        if not isinstance(data, dict):
            raise web.HTTPBadRequest(text='{"error":"请求体必须是 JSON 对象"}', content_type="application/json")
        return data

    @staticmethod
    def _parse_int(val: Any, field: str, default: int | None = None) -> int:
        if val is None or (isinstance(val, str) and not val.strip()):
            if default is not None:
                return default
            raise ValueError(f"{field} 必须是整数")
        if isinstance(val, bool):
            raise ValueError(f"{field} 必须是整数")
        try:
            return int(val)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} 必须是有效整数") from exc

    @staticmethod
    def _parse_float(val: Any, field: str, default: float | None = None) -> float:
        if val is None or (isinstance(val, str) and not val.strip()):
            if default is not None:
                return default
            raise ValueError(f"{field} 必须是数字")
        if isinstance(val, bool):
            raise ValueError(f"{field} 必须是数字")
        try:
            f = float(val)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} 必须是有效数字") from exc
        if f != f or f in (float("inf"), float("-inf")):
            raise ValueError(f"{field} 必须是有效数字")
        return f

    def _is_authed(self, request: web.Request) -> bool:
        sess = request.cookies.get(SESSION_COOKIE, "")
        if self.auth.verify_session(sess):
            return True
        auth_hdr = request.headers.get("Authorization", "")
        if auth_hdr.startswith("Bearer "):
            tok = auth_hdr[7:].strip()
            if self.auth.verify_access_token(tok):
                return True
        return False

    @web.middleware
    async def auth_middleware(self, request: web.Request, handler):
        path = request.path
        if path.startswith("/api/"):
            public_api = {"/api/auth/status", "/api/auth/login", "/api/auth/logout", "/api/healthz"}
            if path not in public_api and not self._is_authed(request):
                return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    def _asset_version(self, filename: str) -> str:
        p = self.ui_dir / filename
        try:
            return hashlib.sha256(p.read_bytes()).hexdigest()[:12]
        except OSError:
            return "0"

    async def handle_index(self, _request: web.Request) -> web.StreamResponse:
        html = (self.ui_dir / "index.html").read_text(encoding="utf-8")
        css_v = self._asset_version("styles.css")
        js_v = self._asset_version("app.js")
        svg_v = self._asset_version("favicon.svg")
        ico_v = self._asset_version("favicon.ico")
        png_v = self._asset_version("apple-touch-icon.png")
        html = html.replace("/assets/styles.css", f"/assets/styles.css?v={css_v}")
        html = html.replace("/assets/app.js", f"/assets/app.js?v={js_v}")
        html = html.replace("/assets/favicon.svg", f"/assets/favicon.svg?v={svg_v}")
        html = html.replace("/favicon.ico", f"/favicon.ico?v={ico_v}")
        html = html.replace("/assets/apple-touch-icon.png", f"/assets/apple-touch-icon.png?v={png_v}")
        return web.Response(
            text=html,
            content_type="text/html",
            charset="utf-8",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Pragma": "no-cache",
            },
        )

    async def handle_favicon(self, _request: web.Request) -> web.StreamResponse:
        ico = self.ui_dir / "favicon.ico"
        if ico.exists():
            return web.FileResponse(ico, headers={"Cache-Control": "public, max-age=86400"})
        svg = self.ui_dir / "favicon.svg"
        if svg.exists():
            return web.FileResponse(svg, headers={"Cache-Control": "public, max-age=86400"})
        raise web.HTTPNotFound()

    async def handle_healthz(self, _request: web.Request) -> web.Response:
        enabled = [account.id for account in self.manager.cfg.accounts if account.enabled]
        ok = all(
            (runner := self.manager.get_runner(account_id)) is not None and runner.listening
            for account_id in enabled
        )
        return web.json_response({"ok": ok}, status=200 if ok else 503)

    async def handle_auth_status(self, request: web.Request) -> web.Response:
        return web.json_response({"authenticated": self._is_authed(request)})

    async def handle_login(self, request: web.Request) -> web.Response:
        data = await self._read_json(request)
        password = str(data.get("password") or "")
        token = str(data.get("token") or "")
        ok = False
        if password:
            ok = self.auth.verify_password(password)
        elif token:
            ok = self.auth.verify_access_token(token)
        if not ok:
            return web.json_response({"error": "密码或访问 Token 不正确"}, status=401)
        sess = self.auth.create_session()
        resp = web.json_response({"ok": True})
        resp.set_cookie(
            SESSION_COOKIE,
            sess,
            max_age=self.auth.SESSION_TTL,
            httponly=True,
            secure=self._is_https(request),
            samesite="Lax",
            path="/",
        )
        return resp

    async def handle_logout(self, request: web.Request) -> web.Response:
        sess = request.cookies.get(SESSION_COOKIE, "")
        self.auth.delete_session(sess)
        resp = web.json_response({"ok": True})
        resp.del_cookie(SESSION_COOKIE, path="/")
        return resp

    async def handle_change_password(self, request: web.Request) -> web.Response:
        data = await self._read_json(request)
        cur = str(data.get("current_password") or "")
        new_p = str(data.get("new_password") or "")
        if len(new_p) < 8:
            return web.json_response({"error": "新密码至少需要 8 位字符"}, status=400)
        cur_sess = request.cookies.get(SESSION_COOKIE, "")
        if not self.auth.change_password(cur, new_p, keep_session=cur_sess):
            return web.json_response({"error": "当前密码不正确"}, status=400)
        return web.json_response({"ok": True})

    async def _runner_summary(self, acct_dict: dict[str, Any], rules: list[dict[str, Any]]) -> dict[str, Any]:
        acct_id = str(acct_dict["id"])
        runner = self.manager.get_runner(acct_id)
        authorized = False
        listening = False
        me_info = None
        if runner and acct_dict.get("enabled", True):
            try:
                authorized = await runner.ensure_authorized(verify_live=True)
                if authorized:
                    me = await runner.client.get_me()
                    if me is None:
                        runner.mark_unauthorized()
                        authorized = False
                        listening = False
                    else:
                        listening = bool(runner.listening)
                        phone = getattr(me, "phone", None) or ""
                        masked = (phone[:3] + "****" + phone[-4:]) if len(phone) >= 7 else ("已绑定" if phone else "")
                        me_info = {
                            "id": me.id,
                            "name": " ".join(p for p in [me.first_name, me.last_name] if p) or str(me.id),
                            "username": me.username or "",
                            "phone": masked,
                        }
            except Exception as exc:
                log.warning("Failed to inspect account %s: %s", acct_id, exc)
                authorized = False
                listening = False
        rules_count = sum(1 for r in rules if (r.get("account_id") or "default") == acct_id)
        qr_st = self._qr_states.get(acct_id) or {"status": "idle", "error": ""}
        return {
            "id": acct_id,
            "name": acct_dict.get("name") or acct_id,
            "enabled": bool(acct_dict.get("enabled", True)),
            "authorized": authorized,
            "listening": listening,
            "me": me_info,
            "rules_count": rules_count,
            "qr": {"status": qr_st.get("status", "idle"), "error": qr_st.get("error", "")},
        }

    async def _all_accounts_summary(self) -> list[dict[str, Any]]:
        raw = load_raw_config(self.config_path)
        rules = raw.get("rules") or []
        return [await self._runner_summary(a, rules) for a in (raw.get("accounts") or [])]

    async def handle_accounts_list(self, _request: web.Request) -> web.Response:
        return web.json_response({"items": await self._all_accounts_summary()})

    async def handle_account_create(self, request: web.Request) -> web.Response:
        data = await self._read_json(request)
        name = str(data.get("name") or "").strip()
        if not name:
            return web.json_response({"error": "请填写账号名称"}, status=400)
        async with self._config_lock:
            raw = load_raw_config(self.config_path)
            acct_id = "acct_" + secrets.token_hex(3)
            raw["accounts"].append({"id": acct_id, "name": name, "enabled": True})
            save_raw_config(self.config_path, raw)
            await self.manager.reload_all(force=True)
        return web.json_response({"ok": True, "id": acct_id, "items": await self._all_accounts_summary()})

    async def handle_account_update(self, request: web.Request) -> web.Response:
        acct_id = request.match_info["acct_id"]
        data = await self._read_json(request)
        async with self._config_lock:
            raw = load_raw_config(self.config_path)
            found = None
            for a in raw["accounts"]:
                if a["id"] == acct_id:
                    found = a
                    break
            if not found:
                return web.json_response({"error": "账号不存在"}, status=404)
            if "name" in data and str(data["name"]).strip():
                found["name"] = str(data["name"]).strip()
            if "enabled" in data:
                found["enabled"] = bool(data["enabled"])
            save_raw_config(self.config_path, raw)
            await self.manager.reload_all(force=True)
        return web.json_response({"ok": True, "items": await self._all_accounts_summary()})

    async def handle_account_delete(self, request: web.Request) -> web.Response:
        acct_id = request.match_info["acct_id"]
        async with self._config_lock:
            raw = load_raw_config(self.config_path)
            if len(raw["accounts"]) <= 1:
                return web.json_response({"error": "至少需保留 1 个账号"}, status=400)
            new_accts = [a for a in raw["accounts"] if a["id"] != acct_id]
            if len(new_accts) == len(raw["accounts"]):
                return web.json_response({"error": "账号不存在"}, status=404)
            if any((r.get("account_id") or "default") == acct_id for r in (raw.get("rules") or [])):
                return web.json_response({"error": "还有规则绑定在这个账号上，请先删除或改绑规则"}, status=400)

            task = self._qr_tasks.pop(acct_id, None)
            if task and not task.done():
                task.cancel()
            self._qr_objs.pop(acct_id, None)
            self._qr_states.pop(acct_id, None)
            self._phone_states.pop(acct_id, None)
            runner = self.manager.get_runner(acct_id)
            if runner:
                runner.enabled = False
                await runner.logout()
                await runner.stop()

            prefix = session_prefix_for(self.session_dir, acct_id)
            for suffix in (".session", ".session-journal", ".session-wal", ".session-shm"):
                Path(f"{prefix}{suffix}").unlink(missing_ok=True)

            raw["accounts"] = new_accts
            save_raw_config(self.config_path, raw)
            await self.manager.reload_all(force=True)
        return web.json_response({"ok": True, "items": await self._all_accounts_summary()})

    @staticmethod
    def _regex_rules_with_usage(raw: dict[str, Any]) -> list[dict[str, Any]]:
        rules = raw.get("rules") or []
        usage: dict[str, list[str]] = {}
        for idx, r in enumerate(rules):
            r_name = str(r.get("name") or f"rule-{idx + 1}")
            for raw_id in r.get("regex_ids") or []:
                rx_id = str(raw_id).strip()
                if rx_id:
                    usage.setdefault(rx_id, []).append(r_name)
        items: list[dict[str, Any]] = []
        for rx in raw.get("regex_rules") or []:
            if not isinstance(rx, dict):
                continue
            rx_id = str(rx.get("id") or "").strip()
            items.append({**rx, "used_by": usage.get(rx_id, [])})
        return items

    async def handle_regex_list(self, _request: web.Request) -> web.Response:
        raw = load_raw_config(self.config_path)
        return web.json_response({"items": self._regex_rules_with_usage(raw)})

    async def handle_regex_create(self, request: web.Request) -> web.Response:
        data = await self._read_json(request)
        async with self._config_lock:
            raw = load_raw_config(self.config_path)
            payload = dict(data)
            payload.pop("id", None)
            try:
                rx = validate_regex_rule_dict(payload)
                raw.setdefault("regex_rules", []).append(rx)
                save_raw_config(self.config_path, raw)
            except ValueError as exc:
                return web.json_response({"error": f"正则配置有误: {exc}"}, status=400)
            await self.manager.reload_all(force=True)
        return web.json_response({"ok": True, "id": rx["id"], "items": self._regex_rules_with_usage(raw)})

    async def handle_regex_update(self, request: web.Request) -> web.Response:
        rx_id = str(request.match_info["rx_id"] or "").strip()
        data = await self._read_json(request)
        async with self._config_lock:
            raw = load_raw_config(self.config_path)
            items = raw.setdefault("regex_rules", [])
            target_idx = next((i for i, x in enumerate(items) if str(x.get("id") or "") == rx_id), -1)
            if target_idx < 0:
                return web.json_response({"error": "正则规则不存在"}, status=404)
            payload = dict(data)
            payload["id"] = rx_id
            try:
                items[target_idx] = validate_regex_rule_dict(payload)
                save_raw_config(self.config_path, raw)
            except ValueError as exc:
                return web.json_response({"error": f"正则配置有误: {exc}"}, status=400)
            await self.manager.reload_all(force=True)
        return web.json_response({"ok": True, "items": self._regex_rules_with_usage(raw)})

    async def handle_regex_delete(self, request: web.Request) -> web.Response:
        rx_id = str(request.match_info["rx_id"] or "").strip()
        async with self._config_lock:
            raw = load_raw_config(self.config_path)
            items = raw.setdefault("regex_rules", [])
            new_items = [x for x in items if str(x.get("id") or "") != rx_id]
            if len(new_items) == len(items):
                return web.json_response({"error": "正则规则不存在"}, status=404)
            raw["regex_rules"] = new_items
            for r in raw.get("rules") or []:
                if r.get("regex_ids"):
                    r["regex_ids"] = [x for x in r["regex_ids"] if str(x).strip() != rx_id]
            save_raw_config(self.config_path, raw)
            await self.manager.reload_all(force=True)
        return web.json_response({"ok": True, "items": self._regex_rules_with_usage(raw)})

    async def handle_overview(self, _request: web.Request) -> web.Response:
        raw = load_raw_config(self.config_path)
        rules = raw.get("rules") or []
        enabled_rules = [r for r in rules if r.get("enabled", True)]
        regex_map = build_regex_map(raw.get("regex_rules") or [])
        accounts = await self._all_accounts_summary()
        acct_name_map = {a["id"]: a["name"] for a in accounts}

        stats = self.manager.history.stats_and_activity()
        recent = self.manager.history.recent(limit=8)

        unique_sources = set()
        unique_targets = set()
        for r in enabled_rules:
            for s in r.get("sources") or []:
                unique_sources.add(str(s))
            for t in r.get("targets") or []:
                unique_targets.add(str(t))

        resolved_sources = sum(len(r.source_map) for r in self.manager.runners.values())
        authorized_accounts = sum(1 for a in accounts if a["authorized"])
        listening_accounts = sum(1 for a in accounts if a["listening"])

        def _filters_count(r: dict[str, Any]) -> int:
            sp, stp, srp, rep = resolve_rule_patterns(r, regex_map=regex_map)
            return len(sp) + len(stp) + len(srp) + len(rep)

        return web.json_response(
            {
                "accounts": accounts,
                "accounts_total": len(accounts),
                "accounts_authorized": authorized_accounts,
                "accounts_listening": listening_accounts,
                "telegram": {
                    "authorized": authorized_accounts > 0,
                    "listening": listening_accounts > 0,
                    "me": next((a["me"] for a in accounts if a["me"]), None),
                },
                "rules_total": len(rules),
                "rules_enabled": len(enabled_rules),
                "sources_count": len(unique_sources),
                "targets_count": len(unique_targets),
                "resolved_sources": resolved_sources,
                "enabled_rules_preview": [
                    {
                        "name": r.get("name") or "未命名规则",
                        "account_id": r.get("account_id") or "default",
                        "account_name": acct_name_map.get(r.get("account_id") or "default", r.get("account_id") or "default"),
                        "sources": r.get("sources") or [],
                        "targets": r.get("targets") or [],
                        "drop_author": bool(r.get("drop_author", True)),
                        "skip_text_only": bool(r.get("skip_text_only", False)),
                        "strip_all_text": bool(r.get("strip_all_text", False)),
                        "fallback_reupload": bool(r.get("fallback_reupload", True)),
                        "filters_count": _filters_count(r),
                    }
                    for r in enabled_rules[:6]
                ],
                "stats": stats,
                "recent": recent,
                "chat_titles": self.manager.all_chat_titles(),
            }
        )

    async def handle_logs(self, request: web.Request) -> web.Response:
        try:
            page = int(request.query.get("page", "1"))
        except ValueError:
            page = 1
        raw_size = request.query.get("page_size") or request.query.get("limit") or "30"
        try:
            page_size = int(raw_size)
        except ValueError:
            page_size = 30
        status = request.query.get("status", "").strip()
        rule = request.query.get("rule", "").strip()
        account_id = request.query.get("account_id", "").strip()
        res = self.manager.history.query_page(
            page=page, page_size=page_size, status=status, rule=rule, account_id=account_id
        )
        raw = load_raw_config(self.config_path)
        rule_names = [str(r.get("name") or f"rule-{i+1}") for i, r in enumerate(raw.get("rules") or [])]
        accounts = [{"id": a["id"], "name": a["name"]} for a in (raw.get("accounts") or [])]
        return web.json_response({**res, "rules": rule_names, "accounts": accounts})

    async def handle_logs_manage_get(self, _request: web.Request) -> web.Response:
        raw = load_raw_config(self.config_path)
        g = raw.get("global") or {}
        retention_days = max(0, int(g.get("log_retention_days", 30)))
        max_rows = max(100, int(g.get("log_max_rows", 5000)))
        return web.json_response(
            {
                "log_retention_days": retention_days,
                "log_max_rows": max_rows,
                "stats": self.manager.history.stats_detail(),
            }
        )

    async def handle_logs_manage_put(self, request: web.Request) -> web.Response:
        data = await self._read_json(request)
        try:
            new_days = max(0, self._parse_int(data.get("log_retention_days"), "log_retention_days", default=30)) if "log_retention_days" in data else None
            new_rows = max(100, self._parse_int(data.get("log_max_rows"), "log_max_rows", default=5000)) if "log_max_rows" in data else None
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        async with self._config_lock:
            raw = load_raw_config(self.config_path)
            g = raw.setdefault("global", {})
            if new_days is not None:
                g["log_retention_days"] = new_days
            if new_rows is not None:
                g["log_max_rows"] = new_rows
            days_eff = max(0, int(g.get("log_retention_days", 30)))
            rows_eff = max(100, int(g.get("log_max_rows", 5000)))
            save_raw_config(self.config_path, raw)
            pruned = self.manager.history.prune(days_eff, rows_eff)
            await self.manager.reload_all(force=True)
        return web.json_response(
            {
                "ok": True,
                "pruned": pruned,
                "log_retention_days": days_eff,
                "log_max_rows": rows_eff,
                "stats": self.manager.history.stats_detail(),
            }
        )

    async def handle_logs_prune(self, _request: web.Request) -> web.Response:
        deleted = self.manager.history.prune()
        return web.json_response({"ok": True, "deleted": deleted, "stats": self.manager.history.stats_detail()})

    async def handle_logs_clear(self, request: web.Request) -> web.Response:
        data = await self._read_json(request, allow_empty=True)
        status = str(data.get("status") or "").strip()
        deleted = self.manager.history.clear(status)
        return web.json_response({"ok": True, "deleted": deleted, "stats": self.manager.history.stats_detail()})

    async def handle_log_resend(self, request: web.Request) -> web.Response:
        try:
            row_id = int(request.match_info["row_id"])
        except ValueError:
            return web.json_response({"error": "无效的记录 ID"}, status=400)
        item = self.manager.history.get_by_id(row_id)
        if not item:
            return web.json_response({"error": "记录不存在或已被清理"}, status=404)
        if int(item.get("target_id") or 0) == 0:
            return web.json_response({"error": "该记录未关联目标频道，无法重发"}, status=400)
        acct_id = str(item.get("account_id") or "default")
        runner = self.manager.get_runner(acct_id)
        if not runner or not runner.enabled:
            return web.json_response({"error": f"执行账号 {acct_id} 不存在或已停用"}, status=400)
        ok, info = await runner.resend_history_item(item)
        if not ok:
            return web.json_response({"error": info}, status=400)
        return web.json_response({"ok": True, "mode": info})

    async def handle_rules_list(self, _request: web.Request) -> web.Response:
        raw = load_raw_config(self.config_path)
        g = raw.get("global") or {}
        rules = raw.get("rules") or []
        accounts = [await self._runner_summary(a, rules) for a in (raw.get("accounts") or [])]
        return web.json_response(
            {
                "rules": rules,
                "regex_rules": self._regex_rules_with_usage(raw),
                "accounts": accounts,
                "chat_titles": self.manager.all_chat_titles(),
                "global_defaults": {
                    "comment_wait_seconds": max(5, min(300, int(g.get("comment_wait_seconds", 30) or 30))),
                    "comment_merge_mode": (
                        "keep_batches" if str(g.get("comment_merge_mode") or "").strip() == "keep_batches" else "pack"
                    ),
                },
            }
        )

    @staticmethod
    def _sanitize_rule_payload(
        data: dict[str, Any],
        default_acct: str = "default",
        valid_regex_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        def _clean_peers(items: Any) -> list[int | str]:
            out: list[int | str] = []
            for item in items or []:
                s = str(item).strip()
                if not s:
                    continue
                try:
                    out.append(int(s))
                except ValueError:
                    out.append(s)
            return out

        def _clean_patterns(items: Any) -> list[str]:
            return [str(x) for x in (items or []) if str(x).strip()]

        replaces = []
        for rep in data.get("replace_patterns") or []:
            if not isinstance(rep, dict):
                continue
            pat = str(rep.get("pattern") or "").strip()
            if pat:
                replaces.append({"pattern": pat, "repl": str(rep.get("repl") or "")})

        regex_ids: list[str] = []
        seen_rx: set[str] = set()
        for raw_id in data.get("regex_ids") or []:
            rid = str(raw_id).strip()
            if not rid or rid in seen_rx:
                continue
            if valid_regex_ids is not None and rid not in valid_regex_ids:
                continue
            seen_rx.add(rid)
            regex_ids.append(rid)

        rule_dict: dict[str, Any] = {
            "name": str(data.get("name") or "").strip() or "未命名规则",
            "enabled": bool(data.get("enabled", True)),
            "account_id": str(data.get("account_id") or default_acct).strip() or default_acct,
            "sources": _clean_peers(data.get("sources")),
            "targets": _clean_peers(data.get("targets")),
            "drop_author": bool(data.get("drop_author", True)),
            "fallback_reupload": bool(data.get("fallback_reupload", True)),
            "skip_text_only": bool(data.get("skip_text_only", False)),
            "strip_all_text": bool(data.get("strip_all_text", False)),
            "skip_empty_text_only": bool(data.get("skip_empty_text_only", True)),
            "disable_link_preview": bool(data.get("disable_link_preview", False)),
            "collect_comments": bool(data.get("collect_comments", False)),
            "regex_ids": regex_ids,
        }
        skip_p = _clean_patterns(data.get("skip_patterns"))
        strip_text_p = _clean_patterns(data.get("strip_text_patterns"))
        strip_p = _clean_patterns(data.get("strip_patterns"))
        if skip_p:
            rule_dict["skip_patterns"] = skip_p
        if strip_text_p:
            rule_dict["strip_text_patterns"] = strip_text_p
        if strip_p:
            rule_dict["strip_patterns"] = strip_p
        if replaces:
            rule_dict["replace_patterns"] = replaces

        raw_cw = data.get("comment_wait_seconds")
        if raw_cw not in (None, ""):
            rule_dict["comment_wait_seconds"] = max(5, min(300, WebConsole._parse_int(raw_cw, "comment_wait_seconds")))
        raw_cm = str(data.get("comment_merge_mode") or "").strip()
        if raw_cm in ("pack", "keep_batches"):
            rule_dict["comment_merge_mode"] = raw_cm
        return rule_dict

    async def handle_rule_create(self, request: web.Request) -> web.Response:
        data = await self._read_json(request)
        async with self._config_lock:
            raw = load_raw_config(self.config_path)
            default_acct = raw["accounts"][0]["id"] if raw.get("accounts") else "default"
            valid_rx = {str(x["id"]) for x in (raw.get("regex_rules") or []) if isinstance(x, dict) and x.get("id")}
            try:
                rule = self._sanitize_rule_payload(data, default_acct, valid_regex_ids=valid_rx)
                rule["id"] = "rule_" + secrets.token_hex(12)
                raw["rules"].append(rule)
                save_raw_config(self.config_path, raw)
            except ValueError as exc:
                return web.json_response({"error": f"规则参数或正则有误: {exc}"}, status=400)
            await self.manager.reload_all(force=True)
        return web.json_response({"ok": True, "rules": raw["rules"]})

    @staticmethod
    def _rule_index(raw: dict[str, Any], rule_id: str) -> int | None:
        return next((idx for idx, rule in enumerate(raw["rules"]) if rule["id"] == rule_id), None)

    async def handle_rule_update(self, request: web.Request) -> web.Response:
        rule_id = request.match_info["rule_id"]
        data = await self._read_json(request)
        async with self._config_lock:
            raw = load_raw_config(self.config_path)
            idx = self._rule_index(raw, rule_id)
            if idx is None:
                return web.json_response({"error": "规则不存在"}, status=404)
            default_acct = raw["accounts"][0]["id"] if raw.get("accounts") else "default"
            valid_rx = {str(x["id"]) for x in (raw.get("regex_rules") or []) if isinstance(x, dict) and x.get("id")}
            try:
                replacement = self._sanitize_rule_payload(data, default_acct, valid_regex_ids=valid_rx)
                replacement["id"] = rule_id
                raw["rules"][idx] = replacement
                save_raw_config(self.config_path, raw)
            except ValueError as exc:
                return web.json_response({"error": f"规则参数或正则有误: {exc}"}, status=400)
            await self.manager.reload_all(force=True)
        return web.json_response({"ok": True, "rules": raw["rules"]})

    async def handle_rule_delete(self, request: web.Request) -> web.Response:
        async with self._config_lock:
            raw = load_raw_config(self.config_path)
            idx = self._rule_index(raw, request.match_info["rule_id"])
            if idx is None:
                return web.json_response({"error": "规则不存在"}, status=404)
            raw["rules"].pop(idx)
            save_raw_config(self.config_path, raw)
            await self.manager.reload_all(force=True)
        return web.json_response({"ok": True, "rules": raw["rules"]})

    async def handle_rule_toggle(self, request: web.Request) -> web.Response:
        async with self._config_lock:
            raw = load_raw_config(self.config_path)
            idx = self._rule_index(raw, request.match_info["rule_id"])
            if idx is None:
                return web.json_response({"error": "规则不存在"}, status=404)
            raw["rules"][idx]["enabled"] = not bool(raw["rules"][idx].get("enabled", True))
            enabled = raw["rules"][idx]["enabled"]
            save_raw_config(self.config_path, raw)
            await self.manager.reload_all(force=True)
        return web.json_response({"ok": True, "enabled": enabled})

    async def handle_rule_test(self, request: web.Request) -> web.Response:
        data = await self._read_json(request)
        sample = str(data.get("text") or "")
        strip_all_text = bool(data.get("strip_all_text", False))
        raw = load_raw_config(self.config_path)
        regex_map = build_regex_map(raw.get("regex_rules") or [])
        try:
            compiled = compile_rule_dict(
                {
                    "name": "preview",
                    "strip_all_text": strip_all_text,
                    "regex_ids": data.get("regex_ids") or [],
                    "skip_patterns": data.get("skip_patterns") or [],
                    "strip_text_patterns": data.get("strip_text_patterns") or [],
                    "strip_patterns": data.get("strip_patterns") or [],
                    "replace_patterns": data.get("replace_patterns") or [],
                },
                regex_map=regex_map,
            )
        except ValueError as exc:
            return web.json_response({"error": f"正则表达式错误: {exc}"}, status=400)
        skip_reason = should_skip_text(sample, compiled)
        strip_all_hit = should_strip_all_text(sample, compiled)
        if strip_all_text or strip_all_hit is not None:
            return web.json_response(
                {
                    "skipped": skip_reason is not None,
                    "skip_reason": skip_reason or "",
                    "cleared_all_text": True,
                    "clear_reason": strip_all_hit or "规则已勾选去除全部文本",
                    "cleaned": "",
                    "changed": bool(sample),
                }
            )
        try:
            cleaned, _, changed = clean_message_text(sample, None, compiled)
        except ValueError as exc:
            return web.json_response({"error": f"替换模板错误: {exc}"}, status=400)
        return web.json_response(
            {
                "skipped": skip_reason is not None,
                "skip_reason": skip_reason or "",
                "cleaned": cleaned,
                "changed": changed,
            }
        )

    async def handle_settings_get(self, _request: web.Request) -> web.Response:
        raw = load_raw_config(self.config_path)
        g = raw.get("global") or {}
        album_quiet, album_max_wait = parse_album_wait_settings(g)
        comment_silence, comment_max_extra = parse_comment_extend_settings(g)
        api_id, api_hash, is_custom = resolve_tg_api(raw)
        masked_hash = (
            (api_hash[:4] + "****" + api_hash[-4:])
            if len(api_hash) >= 10
            else ("已配置" if api_hash else "")
        )
        return web.json_response(
            {
                "delay_seconds": float(g.get("delay_seconds", 1.0)),
                "max_download_mb": int(g.get("max_download_mb", 2000)),
                "dedup_ttl_hours": int(g.get("dedup_ttl_hours", 48)),
                "album_quiet_seconds": album_quiet,
                "album_max_wait_seconds": album_max_wait,
                "comment_wait_seconds": max(5, min(300, int(g.get("comment_wait_seconds", 30) or 30))),
                "comment_silence_seconds": comment_silence,
                "comment_max_extra_seconds": comment_max_extra,
                "comment_merge_mode": (
                    "keep_batches" if str(g.get("comment_merge_mode") or "").strip() == "keep_batches" else "pack"
                ),
                "temp_dir": str(g.get("temp_dir") or (self.config_path.resolve().parent / "cache")),
                "api_id": api_id,
                "api_hash_masked": masked_hash,
                "api_custom": is_custom,
                "access_token": self.auth.get_access_token(),
            },
            headers={"Cache-Control": "no-store"},
        )

    async def handle_settings_put(self, request: web.Request) -> web.Response:
        data = await self._read_json(request)
        try:
            delay_val = max(0.0, self._parse_float(data.get("delay_seconds"), "delay_seconds", default=1.0)) if "delay_seconds" in data else None
            max_mb_val = max(1, self._parse_int(data.get("max_download_mb"), "max_download_mb", default=2000)) if "max_download_mb" in data else None
            ttl_val = max(1, self._parse_int(data.get("dedup_ttl_hours"), "dedup_ttl_hours", default=48)) if "dedup_ttl_hours" in data else None
            cw_val = max(5, min(300, self._parse_int(data.get("comment_wait_seconds"), "comment_wait_seconds", default=30))) if "comment_wait_seconds" in data else None
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)

        async with self._config_lock:
            raw = load_raw_config(self.config_path)
            g = raw.setdefault("global", {})

            for key in ("album_quiet_seconds", "album_max_wait_seconds", "comment_silence_seconds", "comment_max_extra_seconds"):
                if key in data:
                    g[key] = data[key]
            try:
                parse_album_wait_settings(g)
                parse_comment_extend_settings(g)
            except ValueError as exc:
                return web.json_response({"error": str(exc)}, status=400)

            if delay_val is not None:
                g["delay_seconds"] = delay_val
            if max_mb_val is not None:
                g["max_download_mb"] = max_mb_val
            if ttl_val is not None:
                g["dedup_ttl_hours"] = ttl_val
            if cw_val is not None:
                g["comment_wait_seconds"] = cw_val
            if "comment_merge_mode" in data:
                g["comment_merge_mode"] = (
                    "keep_batches" if str(data.get("comment_merge_mode") or "").strip() == "keep_batches" else "pack"
                )

            if data.get("reset_api"):
                g.pop("api_id", None)
                g.pop("api_hash", None)
            else:
                if "api_id" in data and str(data["api_id"]).strip():
                    try:
                        aid = int(str(data["api_id"]).strip())
                        if aid <= 0:
                            raise ValueError("API ID 必须为正整数")
                        g["api_id"] = aid
                    except ValueError:
                        return web.json_response({"error": "API ID 必须是正整数"}, status=400)
                if "api_hash" in data and str(data["api_hash"]).strip():
                    ah = str(data["api_hash"]).strip()
                    if len(ah) < 16:
                        return web.json_response({"error": "API Hash 格式看起来过短，请核对"}, status=400)
                    g["api_hash"] = ah

            save_raw_config(self.config_path, raw)
            await self.manager.reload_all(force=True)
        return web.json_response({"ok": True})

    async def handle_token_rotate(self, _request: web.Request) -> web.Response:
        self.auth.rotate_access_token()
        response = web.json_response({"ok": True})
        response.del_cookie(SESSION_COOKIE, path="/")
        return response

    def _resolve_runner_or_404(self, acct_id: str) -> Any:
        runner = self.manager.get_runner(acct_id)
        if not runner:
            raise web.HTTPNotFound(text='{"error":"账号不存在或未启用"}', content_type="application/json")
        return runner

    async def handle_tg_status(self, request: web.Request) -> web.Response:
        acct_id = request.query.get("account_id", "default").strip() or "default"
        raw = load_raw_config(self.config_path)
        acct = next((a for a in raw["accounts"] if a["id"] == acct_id), None)
        if not acct:
            return web.json_response({"error": "账号不存在"}, status=404)
        return web.json_response(await self._runner_summary(acct, raw.get("rules") or []))

    @staticmethod
    def _qr_to_data_url(url: str) -> str:
        qr = qrcode.QRCode(border=2, box_size=8)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="#121418", back_color="#ffffff")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

    async def _run_qr_waiter(self, acct_id: str, runner: Any, qr_obj: Any) -> None:
        try:
            await qr_obj.wait(timeout=60)
            if runner.session_file.exists():
                os.chmod(runner.session_file, 0o600)
            self._qr_states[acct_id] = {"status": "authorized", "qr_data_url": "", "error": ""}
            await runner.on_user_authorized()
        except asyncio.TimeoutError:
            self._qr_states[acct_id] = {"status": "expired", "qr_data_url": "", "error": "二维码已过期，请点击刷新"}
        except SessionPasswordNeededError:
            self._qr_states[acct_id] = {"status": "need_2fa", "qr_data_url": "", "error": ""}
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._qr_states[acct_id] = {"status": "error", "qr_data_url": "", "error": str(exc)}

    async def handle_tg_qr_start(self, request: web.Request) -> web.Response:
        body = await self._read_json(request, allow_empty=True)
        acct_id = str(body.get("account_id") or request.query.get("account_id") or "default").strip() or "default"
        runner = self._resolve_runner_or_404(acct_id)
        if not runner.enabled:
            return web.json_response({"error": "该账号已停用，请先启用后再登录"}, status=400)
        if await runner.ensure_authorized(verify_live=True):
            return web.json_response({"status": "authorized"})
        old_task = self._qr_tasks.get(acct_id)
        if old_task and not old_task.done():
            old_task.cancel()
        try:
            qr_obj = await runner.client.qr_login()
        except Exception as exc:
            return web.json_response({"error": f"生成二维码失败: {exc}"}, status=400)
        self._qr_objs[acct_id] = qr_obj
        data_url = self._qr_to_data_url(qr_obj.url)
        st = {"status": "waiting", "qr_data_url": data_url, "error": ""}
        self._qr_states[acct_id] = st
        self._qr_tasks[acct_id] = asyncio.create_task(self._run_qr_waiter(acct_id, runner, qr_obj))
        return web.json_response(st)

    async def handle_tg_qr_poll(self, request: web.Request) -> web.Response:
        acct_id = request.query.get("account_id", "default").strip() or "default"
        runner = self._resolve_runner_or_404(acct_id)
        if await runner.ensure_authorized(verify_live=False):
            if not runner.listening:
                await runner.on_user_authorized()
            return web.json_response({"status": "authorized", "error": ""})
        st = self._qr_states.get(acct_id) or {"status": "idle", "error": ""}
        return web.json_response({"status": st.get("status", "idle"), "error": st.get("error", "")})

    async def handle_tg_qr_2fa(self, request: web.Request) -> web.Response:
        data = await self._read_json(request)
        acct_id = str(data.get("account_id") or "default").strip() or "default"
        pwd = str(data.get("password") or "")
        runner = self._resolve_runner_or_404(acct_id)
        if not pwd:
            return web.json_response({"error": "请输入两步验证（2FA）云密码"}, status=400)
        try:
            if not runner.client.is_connected():
                await runner.client.connect()
            await runner.client.sign_in(password=pwd)
            if runner.session_file.exists():
                os.chmod(runner.session_file, 0o600)
            self._qr_objs.pop(acct_id, None)
            self._qr_states[acct_id] = {"status": "authorized", "qr_data_url": "", "error": ""}
            await runner.on_user_authorized()
            return web.json_response({"status": "authorized"})
        except Exception as exc:
            return web.json_response({"error": f"两步验证失败: {exc}"}, status=400)

    async def handle_tg_phone_send(self, request: web.Request) -> web.Response:
        data = await self._read_json(request)
        acct_id = str(data.get("account_id") or "default").strip() or "default"
        phone = str(data.get("phone") or "").strip()
        runner = self._resolve_runner_or_404(acct_id)
        if not runner.enabled:
            return web.json_response({"error": "该账号已停用，请先启用后再登录"}, status=400)
        if not phone:
            return web.json_response({"error": "请填写带国际区号的手机号（如 +86...）"}, status=400)
        if not runner.client.is_connected():
            await runner.client.connect()
        try:
            res = await runner.client.send_code_request(phone)
            self._phone_states[acct_id] = {"phone": phone, "hash": res.phone_code_hash, "need_2fa": False}
            return web.json_response({"ok": True})
        except Exception as exc:
            return web.json_response({"error": f"发送验证码失败: {exc}"}, status=400)

    async def handle_tg_phone_verify(self, request: web.Request) -> web.Response:
        data = await self._read_json(request)
        acct_id = str(data.get("account_id") or "default").strip() or "default"
        code = str(data.get("code") or "").strip()
        password = str(data.get("password") or "")
        runner = self._resolve_runner_or_404(acct_id)
        phone_state = self._phone_states.get(acct_id)
        if not phone_state:
            return web.json_response({"error": "请先发送登录验证码"}, status=400)
        phone = phone_state["phone"]
        phone_code_hash = phone_state["hash"]
        try:
            if not runner.client.is_connected():
                await runner.client.connect()
            if phone_state.get("need_2fa"):
                if not password:
                    return web.json_response(
                        {"need_2fa": True, "error": "请输入该账号的两步验证（2FA）固定云密码"}, status=200
                    )
                await runner.client.sign_in(password=password)
            else:
                if not code:
                    return web.json_response({"error": "请输入 Telegram 收到的登录验证码"}, status=400)
                try:
                    await runner.client.sign_in(
                        phone=phone,
                        code=code,
                        phone_code_hash=phone_code_hash,
                    )
                except SessionPasswordNeededError:
                    phone_state["need_2fa"] = True
                    if not password:
                        return web.json_response(
                            {"need_2fa": True, "error": "验证码已通过，请输入两步验证（2FA）固定云密码"}, status=200
                        )
                    await runner.client.sign_in(password=password)
            self._phone_states.pop(acct_id, None)
            if runner.session_file.exists():
                os.chmod(runner.session_file, 0o600)
            await runner.on_user_authorized()
            return web.json_response({"ok": True, "status": "authorized"})
        except (PhoneCodeInvalidError, PhoneCodeExpiredError) as exc:
            return web.json_response({"error": f"登录验证码无效或已过期: {exc}"}, status=400)
        except Exception as exc:
            return web.json_response({"error": f"登录失败: {exc}"}, status=400)

    async def handle_tg_logout(self, request: web.Request) -> web.Response:
        body = await self._read_json(request, allow_empty=True)
        acct_id = str(body.get("account_id") or request.query.get("account_id") or "default").strip() or "default"
        runner = self._resolve_runner_or_404(acct_id)
        task = self._qr_tasks.pop(acct_id, None)
        if task and not task.done():
            task.cancel()
        self._qr_objs.pop(acct_id, None)
        self._phone_states.pop(acct_id, None)
        await runner.logout()
        self._qr_states[acct_id] = {"status": "idle", "qr_data_url": "", "error": ""}
        return web.json_response({"ok": True})

    async def handle_chats(self, request: web.Request) -> web.Response:
        acct_id = request.query.get("account_id", "").strip()
        raw = load_raw_config(self.config_path)
        if not acct_id and raw.get("accounts"):
            acct_id = raw["accounts"][0]["id"]
        runner = self.manager.get_runner(acct_id)
        if not runner or not runner.enabled:
            return web.json_response({"authorized": False, "account_id": acct_id, "items": []})
        refresh = request.query.get("refresh", "0") == "1"
        try:
            if not await runner.ensure_authorized(verify_live=True):
                return web.json_response({"authorized": False, "account_id": acct_id, "items": []})
            return await self._load_chats(runner, acct_id, refresh)
        except UnauthorizedError:
            runner.mark_unauthorized()
            return web.json_response({"authorized": False, "account_id": acct_id, "items": []})
        except (RPCError, OSError, TimeoutError):
            log.warning("Telegram directory refresh failed for %s", acct_id, exc_info=True)
            return web.json_response({"error": "暂时无法读取 Telegram 频道目录，请重试"}, status=503)

    async def _load_chats(self, runner: Any, acct_id: str, refresh: bool) -> web.Response:
        if refresh or not runner.chats_cache:
            items: list[dict[str, Any]] = []
            async for d in runner.client.iter_dialogs():
                if not (d.is_channel or d.is_group):
                    continue
                ent = d.entity
                if getattr(ent, "deactivated", False) or getattr(ent, "migrated_to", None):
                    continue
                pid = utils.get_peer_id(ent)
                kind = "channel" if getattr(ent, "broadcast", False) else ("supergroup" if d.is_channel else "group")
                nofwd = bool(getattr(ent, "noforwards", False))
                uname = getattr(ent, "username", None) or ""
                runner.chat_titles[pid] = d.name
                items.append(
                    {
                        "id": pid,
                        "title": d.name,
                        "username": uname,
                        "kind": kind,
                        "noforwards": nofwd,
                    }
                )
            runner.chats_cache = items
        return web.json_response({"authorized": True, "account_id": acct_id, "items": runner.chats_cache})

    async def _on_response_prepare(self, request: web.Request, response: web.StreamResponse) -> None:
        if request.path.startswith("/assets/") and "Cache-Control" not in response.headers:
            response.headers["Cache-Control"] = "no-cache, must-revalidate"

    def build_app(self) -> web.Application:
        app = web.Application(middlewares=[self.auth_middleware])
        app.on_response_prepare.append(self._on_response_prepare)
        app.router.add_get("/", self.handle_index)
        app.router.add_get("/favicon.ico", self.handle_favicon)
        app.router.add_get("/api/healthz", self.handle_healthz)
        app.router.add_get("/api/auth/status", self.handle_auth_status)
        app.router.add_post("/api/auth/login", self.handle_login)
        app.router.add_post("/api/auth/logout", self.handle_logout)
        app.router.add_post("/api/auth/password", self.handle_change_password)

        app.router.add_get("/api/overview", self.handle_overview)
        app.router.add_get("/api/logs", self.handle_logs)
        app.router.add_post("/api/logs/{row_id}/resend", self.handle_log_resend)
        app.router.add_get("/api/logs/manage", self.handle_logs_manage_get)
        app.router.add_put("/api/logs/manage", self.handle_logs_manage_put)
        app.router.add_post("/api/logs/prune", self.handle_logs_prune)
        app.router.add_post("/api/logs/clear", self.handle_logs_clear)
        app.router.add_get("/api/rules", self.handle_rules_list)
        app.router.add_post("/api/rules", self.handle_rule_create)
        app.router.add_put("/api/rules/{rule_id}", self.handle_rule_update)
        app.router.add_delete("/api/rules/{rule_id}", self.handle_rule_delete)
        app.router.add_post("/api/rules/{rule_id}/toggle", self.handle_rule_toggle)
        app.router.add_post("/api/rules/test", self.handle_rule_test)

        app.router.add_get("/api/regex", self.handle_regex_list)
        app.router.add_post("/api/regex", self.handle_regex_create)
        app.router.add_put("/api/regex/{rx_id}", self.handle_regex_update)
        app.router.add_delete("/api/regex/{rx_id}", self.handle_regex_delete)

        app.router.add_get("/api/settings", self.handle_settings_get)
        app.router.add_put("/api/settings", self.handle_settings_put)
        app.router.add_post("/api/settings/token/rotate", self.handle_token_rotate)

        app.router.add_get("/api/accounts", self.handle_accounts_list)
        app.router.add_post("/api/accounts", self.handle_account_create)
        app.router.add_put("/api/accounts/{acct_id}", self.handle_account_update)
        app.router.add_delete("/api/accounts/{acct_id}", self.handle_account_delete)

        app.router.add_get("/api/tg/status", self.handle_tg_status)
        app.router.add_post("/api/tg/qr/start", self.handle_tg_qr_start)
        app.router.add_get("/api/tg/qr/poll", self.handle_tg_qr_poll)
        app.router.add_post("/api/tg/qr/2fa", self.handle_tg_qr_2fa)
        app.router.add_post("/api/tg/phone/send_code", self.handle_tg_phone_send)
        app.router.add_post("/api/tg/phone/verify", self.handle_tg_phone_verify)
        app.router.add_post("/api/tg/logout", self.handle_tg_logout)

        app.router.add_get("/api/chats", self.handle_chats)
        app.router.add_static("/assets/", self.ui_dir)
        return app
