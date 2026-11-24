(function () {
  const state = {
    authed: false,
    route: location.hash.replace(/^#/, '') || '/',
    sidebarOpen: false,
    theme: localStorage.getItem('tgfwd_theme') || 'system',
    overview: null,
    logs: { items: [], rules: [], accounts: [], status: '', rule: '', account_id: '', page: 1, pageSize: 30, total: 0, totalPages: 1 },
    rules: [],
    regexRules: [],
    regexFilterKind: '',
    regexSearch: '',
    expandedRuleSources: {},
    accounts: [],
    ruleFilterAccount: '',
    chatTitles: {},
    chatsAccountId: '',
    chats: [],
    chatsAuthorized: false,
    chatsQuery: '',
    loginAccountId: '',
    qrDataUrl: '',
    qrPollTimer: null,
    phoneStep: 'input',
    phoneNum: '',
    phoneCode: '',
    settings: null,
    ruleGlobalDefaults: { comment_wait_seconds: 30, comment_merge_mode: 'pack' },
    logManage: null,
    resendingIds: {},
    modalRule: null, // null | { idx, data, chats }
    modalRuleTestOpen: false,
    modalRuleRxFilter: '',
    modalPickerQuery: '',
    modalRegex: null, // null | { id, data, genOpen, genStripMode, genWords, genIgnoreCase }
    modalPassword: false,
    modalAddAccount: false,
    testInput: '🔥频道推荐内容\n这是一段正文，包含重要信息\n\n关注备用频道 @example_ad_channel',
    testOutput: null,
    regexTest: {
      selectedIds: [],
      sample: '🔥精品分享\n这是一段正文内容\n关注备用频道 @example_ad\n广告合作请联系 @someone',
      result: null,
    },
  };

  const REGEX_KIND_META = {
    skip: { label: '整条跳过', short: '跳过', badge: 'warning', desc: '命中即拦截整条消息（含媒体）' },
    strip_text: { label: '命中清文', short: '清文', badge: 'violet', desc: '命中即清空全部配文，仅发媒体' },
    strip: { label: '删除文本', short: '删除', badge: 'info', desc: '删除匹配词或整行，保留正文格式' },
    replace: { label: '查找替换', short: '替换', badge: 'success', desc: '按 查找正则 => 替换为 执行替换' },
  };
  const REGEX_KIND_ORDER = ['skip', 'strip_text', 'strip', 'replace'];

  const REGEX_TEMPLATES = [
    { name: '拦截常见广告与推广', kind: 'skip', desc: '整条消息命中关键词即跳过', patterns: ['(?i)#ad\\b|广告合作|商务联系|推广'] },
    { name: '拦截外部网址帖', kind: 'skip', desc: '含 http 链接但放行 t.me', patterns: ['(?i)https?://(?!t\\.me/)'] },
    { name: '删除含关键词的宣传整行', kind: 'strip', desc: '删掉含“关注频道/备用频道/投稿联系”的整行', patterns: ['(?im)^.*(?:关注频道|备用频道|投稿联系).*$'] },
    { name: '删除所有 @用户名', kind: 'strip', desc: '清空文内或末尾的 @xxx 提及', patterns: ['@\\w+'] },
    { name: '删除 t.me 频道链接', kind: 'strip', desc: '移除 https://t.me/xxx 尾巴', patterns: ['(?i)https?://t\\.me/\\S+'] },
    { name: '删除所有 #话题标签', kind: 'strip', desc: '移除 #标签1 #标签2', patterns: ['#[\\w\\u4e00-\\u9fff]+'] },
  ];

  function esc(v) {
    return String(v == null ? '' : v)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function fmtTime(ts) {
    if (!ts) return '-';
    const d = new Date(ts * 1000);
    return d.toLocaleString('zh-CN', { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }

  function toast(msg, kind = 'info') {
    const stack = document.getElementById('toast-stack');
    const el = document.createElement('div');
    el.className = 'toast ' + kind;
    el.textContent = msg;
    stack.appendChild(el);
    setTimeout(() => el.remove(), 3400);
  }

  async function copyText(text, successMsg = '已复制到剪贴板') {
    const val = String(text ?? '');
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(val);
      } else {
        const ta = document.createElement('textarea');
        ta.value = val;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        const ok = document.execCommand('copy');
        ta.remove();
        if (!ok) throw new Error('浏览器拒绝复制');
      }
      toast(successMsg, 'success');
    } catch (err) {
      toast('复制失败：' + (err?.message || '请手动复制'), 'danger');
    }
  }

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
      ...opts,
    });
    const data = await res.json().catch(() => ({}));
    if (res.status === 401 && path !== '/api/auth/login' && path !== '/api/auth/status') {
      state.authed = false;
      render();
      throw new Error('登录已过期');
    }
    if (!res.ok) throw new Error(data.error || ('请求失败 (' + res.status + ')'));
    return data;
  }

  function applyTheme(t) {
    state.theme = t;
    localStorage.setItem('tgfwd_theme', t);
    const dark = t === 'dark' || (t === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches);
    if (dark) document.documentElement.setAttribute('data-theme', 'dark');
    else document.documentElement.removeAttribute('data-theme');
    render();
  }

  function icon(name, size = 16) {
    const icons = {
      brand: `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2L11 13"/><path d="M22 2l-7 20-4-9-9-4 20-7z"/></svg>`,
      dashboard: `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/></svg>`,
      guide: `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/></svg>`,
      logs: `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 6h16M4 12h16M4 18h10"/></svg>`,
      logManage: `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/><path d="M3 12c0 1.66 4 3 9 3s9-1.34 9-3"/></svg>`,
      rules: `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M16 3h5v5M4 20L21 3M21 16v5h-5M15 15l6 6M4 4l5 5"/></svg>`,
      chats: `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>`,
      telegram: `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z"/></svg>`,
      settings: `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/></svg>`,
      regex: `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>`,
      arrow: `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M5 12h14M13 6l6 6-6 6"/></svg>`,
      menu: `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 6h16M4 12h16M4 18h16"/></svg>`,
      close: `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M18 6L6 18M6 6l12 12"/></svg>`,
      key: `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="7.5" cy="15.5" r="4.5"/><path d="m21 2-9.6 9.6M15.5 7.5l3 3"/></svg>`,
      logout: `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9"/></svg>`,
    };
    return icons[name] || '';
  }

  function modeBadge(mode, status) {
    if (status === 'error') return '<span class="badge danger status">转发失败</span>';
    const map = {
      copy: '<span class="badge success status">无来源秒转</span>',
      cleaned: '<span class="badge info status">无下载清洗</span>',
      reupload: '<span class="badge warning status">下载重传</span>',
      comments_pack: '<span class="badge info status">评论合并</span>',
      comments_batch: '<span class="badge info status">评论分批</span>',
      skip_regex: '<span class="badge muted">广告拦截</span>',
      skip_text_only: '<span class="badge muted">纯文字跳过</span>',
      skip_empty: '<span class="badge muted">空内容跳过</span>',
      skip_restricted: '<span class="badge warning">受限跳过</span>',
      skip_size: '<span class="badge warning">超限跳过</span>',
    };
    return map[mode] || `<span class="badge muted">${esc(mode)}</span>`;
  }

  function themeSwitchHtml() {
    const opts = [
      ['light', '亮'],
      ['dark', '暗'],
      ['system', '自动'],
    ];
    return `<div class="theme-switch">${opts
      .map(([k, l]) => `<button type="button" class="${state.theme === k ? 'active' : ''}" data-theme-set="${k}">${l}</button>`)
      .join('')}</div>`;
  }

  function accountName(acctId) {
    const a = (state.accounts || []).find((x) => x.id === acctId);
    return a ? a.name : acctId || 'default';
  }

  function peerLabel(id) {
    const s = String(id);
    const t = state.chatTitles[s];
    return t ? `${esc(t)} <span class="mono muted">(${esc(s)})</span>` : `<span class="mono">${esc(s)}</span>`;
  }

  function peerShortName(id) {
    const s = String(id);
    return state.chatTitles[s] || s;
  }

  function peerTooltip(id) {
    const s = String(id);
    const t = state.chatTitles[s];
    return t ? `${t} (${s})` : s;
  }

  function regexContentText(rx) {
    if (!rx) return '';
    if (rx.kind === 'replace') {
      return (rx.replace_patterns || []).map((p) => `${p.pattern} => ${p.repl || ''}`).join('\n');
    }
    return (rx.patterns || []).join('\n');
  }

  function regexPreviewLine(rx) {
    const lines = regexContentText(rx).split('\n').filter(Boolean);
    if (!lines.length) return '';
    return lines.length > 1 ? `${lines[0]} (+${lines.length - 1}行)` : lines[0];
  }

  function getRegexById(id) {
    return (state.regexRules || []).find((x) => x.id === id) || null;
  }

  function navigate(path) {
    state.route = path;
    state.sidebarOpen = false;
    location.hash = path;
    stopQrPoll();
    loadRouteData(path);
    render();
  }

  function stopQrPoll() {
    if (state.qrPollTimer) {
      clearInterval(state.qrPollTimer);
      state.qrPollTimer = null;
    }
  }

  async function loadRouteData(path) {
    if (!state.authed) return;
    try {
      if (path === '/') {
        state.overview = await api('/api/overview');
        state.accounts = state.overview.accounts || [];
        state.chatTitles = state.overview.chat_titles || {};
      } else if (path === '/logs') {
        const qs = new URLSearchParams({
          page: String(state.logs.page || 1),
          page_size: String(state.logs.pageSize || 30),
          status: state.logs.status,
          rule: state.logs.rule,
          account_id: state.logs.account_id,
        });
        const d = await api('/api/logs?' + qs.toString());
        state.logs.items = d.items || [];
        state.logs.total = Number(d.total ?? state.logs.items.length);
        state.logs.page = Number(d.page || 1);
        state.logs.pageSize = Number(d.page_size || 30);
        state.logs.totalPages = Number(d.total_pages || 1);
        state.logs.rules = d.rules || [];
        state.logs.accounts = d.accounts || [];
      } else if (path === '/rules') {
        const d = await api('/api/rules');
        state.rules = d.rules || [];
        state.regexRules = d.regex_rules || [];
        state.accounts = d.accounts || [];
        state.chatTitles = d.chat_titles || {};
        if (d.global_defaults) state.ruleGlobalDefaults = d.global_defaults;
      } else if (path === '/regex') {
        const d = await api('/api/regex');
        state.regexRules = d.items || [];
        const validSet = new Set(state.regexRules.map((x) => x.id));
        state.regexTest.selectedIds = (state.regexTest.selectedIds || []).filter((id) => validSet.has(id));
        if (!state.regexTest.selectedIds.length && state.regexRules.length) {
          state.regexTest.selectedIds = state.regexRules.slice(0, 2).map((x) => x.id);
        }
      } else if (path === '/chats') {
        const accts = await api('/api/accounts');
        state.accounts = accts.items || [];
        if (!state.chatsAccountId || !state.accounts.some((a) => a.id === state.chatsAccountId)) {
          const firstAuth = state.accounts.find((a) => a.authorized) || state.accounts[0];
          state.chatsAccountId = firstAuth ? firstAuth.id : 'default';
        }
        const d = await api('/api/chats?account_id=' + encodeURIComponent(state.chatsAccountId));
        state.chatsAuthorized = d.authorized;
        state.chats = d.items || [];
      } else if (path === '/telegram') {
        const accts = await api('/api/accounts');
        state.accounts = accts.items || [];
        if (state.loginAccountId) {
          const cur = state.accounts.find((a) => a.id === state.loginAccountId);
          if (!cur || cur.authorized) state.loginAccountId = '';
        }
      } else if (path === '/settings') {
        state.settings = await api('/api/settings');
      } else if (path === '/log-manage') {
        state.logManage = await api('/api/logs/manage');
      }
      if (!state.accounts.length && !['/', '/rules', '/chats', '/telegram'].includes(path)) {
        const accts = await api('/api/accounts');
        state.accounts = accts.items || [];
      }
      render();
    } catch (e) {
      toast(e.message, 'danger');
    }
  }

  function renderLogin() {
    return `
    <div class="login-page">
      <div class="login-preferences">${themeSwitchHtml()}</div>
      <svg class="login-trace" viewBox="0 0 1200 720" preserveAspectRatio="none">
        <path d="M-40 240 C 260 240, 320 140, 560 140 S 880 340, 1240 340" fill="none" stroke="currentColor" stroke-width="1.2"/>
        <path d="M-40 500 C 220 500, 380 410, 620 410 S 940 560, 1240 560" fill="none" stroke="currentColor" stroke-width="1.2"/>
      </svg>
      <div class="login-card">
        <div class="login-brand">
          <span class="brand-mark">${icon('brand', 20)}</span>
          <span>TG-Forwarder</span>
        </div>
        <div class="login-heading">
          <h1>登录控制台</h1>
          <p class="page-sub">多账号频道无痕搬运 · UserBot 管理面板</p>
        </div>
        <form id="login-form" class="login-form">
          <div class="field">
            <label for="login-pwd">管理员密码</label>
            <input id="login-pwd" type="password" autocomplete="current-password" placeholder="输入首次启动日志中的密码" required />
          </div>
          <button class="btn primary" type="submit">进入控制台</button>
        </form>
        <p class="login-help">首次登录或忘记密码，请在服务器运行 <code>.venv/bin/python common.py reset-web-password</code>。</p>
      </div>
    </div>`;
  }

  function renderOverview() {
    const o = state.overview;
    if (!o) return '<div class="panel empty">加载概览中...</div>';
    const tgOk = o.accounts_authorized > 0;
    const ruleOk = o.rules_enabled > 0;
    const doneSteps = (tgOk ? 1 : 0) + (ruleOk ? 1 : 0);
    const maxCount = Math.max(1, ...(o.stats.activity_7d || []).map((d) => d.count));

    return `
      <div class="page-head">
        <div>
          <h1>概览</h1>
          <p class="page-sub">多账号频道监听、广告过滤清洗与无来源搬运状态</p>
        </div>
        <div class="page-actions">
          <button class="btn" data-nav="/rules">管理规则</button>
          <button class="btn primary" id="btn-refresh-overview">刷新状态</button>
        </div>
      </div>

      ${doneSteps < 2 ? `
      <section class="setup-callout">
        <div>
          <div class="setup-eyebrow">初始化清单</div>
          <h2>完成以下 ${2 - doneSteps} 项即可开始自动无痕搬运</h2>
          <p>支持登录多个 TG 小号，并在转发规则里为每条规则单独绑定使用哪个账号。</p>
          <div class="setup-checks">
            <span class="setup-check ${tgOk ? 'done' : ''}">${tgOk ? '✓' : '1.'} 登录至少 1 个 Telegram 小号</span>
            <span class="setup-check ${ruleOk ? 'done' : ''}">${ruleOk ? '✓' : '2.'} 启用至少 1 条转发规则</span>
          </div>
        </div>
        <div class="setup-callout-side">
          <strong>${doneSteps}<span> / 2</span></strong>
          <button class="btn primary small" data-nav="${tgOk ? '/rules' : '/telegram'}">${tgOk ? '去配规则' : '去登录小号'}</button>
        </div>
      </section>` : ''}

      <section class="relay-board">
        <div class="relay-status">
          <span class="live-dot ${o.accounts_listening > 0 ? 'running' : 'warning'}"></span>
          <span>${o.accounts_listening > 0 ? `已连接 ${o.accounts_authorized} / ${o.accounts_total} 个账号，${o.accounts_listening} 个正在实时监听` : '等待登录 Telegram 小号'}</span>
        </div>
        <div class="relay-flow">
          <div class="relay-node">
            <div class="relay-node-label">${icon('chats', 15)} 源频道监听</div>
            <strong>${o.resolved_sources} 个活跃源</strong>
            <small>${o.accounts_authorized} 个账号在线 · 支持普通与防保存频道</small>
          </div>
          <div class="relay-link ${ruleOk ? 'active' : ''}">${icon('arrow', 20)}</div>
          <div class="relay-node">
            <div class="relay-node-label">${icon('rules', 15)} 规则与清洗</div>
            <strong>${o.rules_enabled} / ${o.rules_total} 条启用</strong>
            <small>无来源秒转 · 广告先滤后清 · 多账号隔离</small>
          </div>
          <div class="relay-link ${ruleOk ? 'active' : ''}">${icon('arrow', 20)}</div>
          <div class="relay-node">
            <div class="relay-node-label">${icon('telegram', 15)} 目标频道投递</div>
            <strong>${o.targets_count} 个目标</strong>
            <small>相册合并 · 受限频道自动缓存重传</small>
          </div>
        </div>
        <div class="relay-metrics">
          <div class="relay-metric"><span>今日已转发 / 拦截</span><strong>${o.stats.today_success} / ${o.stats.today_skipped}</strong></div>
          <div class="relay-metric"><span>累计成功转发</span><strong>${o.stats.total_success}</strong></div>
          <div class="relay-metric"><span>累计拦截 / 失败</span><strong>${o.stats.total_skipped} / ${o.stats.total_error}</strong></div>
        </div>
      </section>

      <div class="dashboard-split">
        <section class="panel">
          <div class="panel-head">
            <h2 class="panel-title">近 7 天搬运趋势</h2>
            <span class="muted">今日 ${o.stats.today_success} 条</span>
          </div>
          <div class="activity-figure"><strong>${(o.stats.activity_7d || []).reduce((a, b) => a + b.count, 0)}</strong><span class="muted">近 7 天合计成功</span></div>
          <div class="activity-chart">
            ${(o.stats.activity_7d || [])
              .map((d) => {
                const h = d.count ? Math.max(8, Math.round((d.count / maxCount) * 100)) : 2;
                return `<div class="chart-col ${d.current ? 'current' : ''}"><span class="chart-count">${d.count || ''}</span><div class="chart-bar" style="height:${h}%"></div><span class="chart-label">${esc(d.label)}</span></div>`;
              })
              .join('')}
          </div>
        </section>

        <section class="panel">
          <div class="panel-head">
            <h2 class="panel-title">已启用规则</h2>
            <a href="#/rules" class="muted" style="font-size:13px">全部 →</a>
          </div>
          <div class="storage-mini-list">
            ${
              o.enabled_rules_preview.length
                ? o.enabled_rules_preview
                    .map(
                      (r) => `
                <div class="storage-mini">
                  <span class="storage-mini-icon">${icon('rules', 15)}</span>
                  <div class="storage-mini-main">
                    <strong>${esc(r.name)}</strong>
                    <span>[${esc(r.account_name)}] ${r.sources.length} 源 → ${r.targets.length} 目标 · ${r.filters_count} 过滤</span>
                  </div>
                  <span class="badge ${r.strip_all_text ? 'warning' : r.skip_text_only ? 'info' : r.drop_author ? 'success' : 'muted'}">${r.strip_all_text ? '仅媒体' : r.skip_text_only ? '跳过纯文字' : r.drop_author ? '无来源' : '带来源'}</span>
                </div>`
                    )
                    .join('')
                : '<div class="empty">暂无已启用规则</div>'
            }
          </div>
        </section>
      </div>

      <section class="panel">
        <div class="panel-head">
          <h2 class="panel-title">最近转发动态</h2>
          <a href="#/logs" class="muted" style="font-size:13px">完整记录 →</a>
        </div>
        <div class="recent-list">
          ${
            o.recent.length
              ? o.recent
                  .map(
                    (it) => `
              <div class="recent-task">
                <div class="recent-task-main">
                  <div class="recent-task-title">${esc(it.preview || '[媒体消息]')}</div>
                  <div class="recent-task-meta">
                    <span>[${esc(it.account_name || it.account_id || '默认')}] ${esc(it.rule_name)}</span>
                    <span>·</span>
                    <span>${esc(it.source_title)} → ${esc(it.target_title)}</span>
                    <span>·</span>
                    <span>${fmtTime(it.ts)}</span>
                  </div>
                </div>
                ${modeBadge(it.mode, it.status)}
              </div>`
                  )
                  .join('')
              : '<div class="empty">还没有转发记录，等待源频道新消息中...</div>'
          }
        </div>
      </section>`;
  }

  function buildPageNumbers(cur, total) {
    if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1);
    const out = [1];
    const left = Math.max(2, cur - 1);
    const right = Math.min(total - 1, cur + 1);
    if (left > 2) out.push('...');
    for (let p = left; p <= right; p += 1) out.push(p);
    if (right < total - 1) out.push('...');
    out.push(total);
    return out;
  }

  function splitTopLevelAlternation(body) {
    const parts = [];
    let buf = '';
    let escaped = false;
    let inClass = false;
    let depth = 0;
    for (let i = 0; i < body.length; i++) {
      const ch = body[i];
      if (escaped) {
        buf += ch;
        escaped = false;
        continue;
      }
      if (ch === '\\') {
        buf += ch;
        escaped = true;
        continue;
      }
      if (inClass) {
        buf += ch;
        if (ch === ']') inClass = false;
        continue;
      }
      if (ch === '[') {
        inClass = true;
        buf += ch;
      } else if (ch === '(') {
        depth += 1;
        buf += ch;
      } else if (ch === ')') {
        depth = Math.max(0, depth - 1);
        buf += ch;
      } else if (ch === '|' && depth === 0) {
        parts.push(buf);
        buf = '';
      } else {
        buf += ch;
      }
    }
    parts.push(buf);
    return parts;
  }

  function unwrapSingleGroup(body) {
    if (!body.startsWith('(') || !body.endsWith(')')) return ['', body, ''];
    let escaped = false;
    let inClass = false;
    let depth = 0;
    for (let i = 0; i < body.length; i++) {
      const ch = body[i];
      if (escaped) {
        escaped = false;
        continue;
      }
      if (ch === '\\') {
        escaped = true;
        continue;
      }
      if (inClass) {
        if (ch === ']') inClass = false;
        continue;
      }
      if (ch === '[') inClass = true;
      else if (ch === '(') depth += 1;
      else if (ch === ')') {
        depth -= 1;
        if (depth === 0 && i < body.length - 1) return ['', body, ''];
      }
    }
    if (body.startsWith('(?:')) return ['(?:', body.slice(3, -1), ')'];
    if (body.startsWith('(?')) return ['', body, ''];
    return ['(', body.slice(1, -1), ')'];
  }

  function renderLogDetail(it) {
    const detail = String(it.detail || '');
    const m = detail.match(/^(命中跳过正则[:：]\s*)(.+)$/);
    if (!m) return esc(detail);

    const prefix = m[1];
    let body = m[2];
    let flagPrefix = '';
    let jsFlags = 'm';
    const fm = body.match(/^\(\?([aiLmsux]+)\)/);
    if (fm) {
      flagPrefix = fm[0];
      const fl = fm[1];
      if (fl.includes('i')) jsFlags += 'i';
      if (fl.includes('s')) jsFlags += 's';
      body = body.slice(fm[0].length);
    }

    const savedHits = new Set();
    if (it.matched_terms) {
      try {
        const arr = typeof it.matched_terms === 'string' ? JSON.parse(it.matched_terms) : it.matched_terms;
        if (Array.isArray(arr)) arr.forEach((t) => savedHits.add(String(t)));
      } catch {}
    }

    let openWrap = '';
    let closeWrap = '';
    let parts = splitTopLevelAlternation(body);
    if (parts.length === 1) {
      const [ow, inner, cw] = unwrapSingleGroup(body);
      if (inner !== body) {
        openWrap = ow;
        closeWrap = cw;
        parts = splitTopLevelAlternation(inner);
      }
    }

    const previewText = String(it.preview || '');
    const partsHtml = parts.map((part) => {
      if (!part) return '';
      let hit = savedHits.has(part);
      if (!hit && previewText) {
        try {
          hit = new RegExp(part, jsFlags).test(previewText);
        } catch {}
      }
      return hit ? `<mark class="regex-hit">${esc(part)}</mark>` : esc(part);
    });

    return `${esc(prefix)}${esc(flagPrefix)}${esc(openWrap)}${partsHtml.join('|')}${esc(closeWrap)}`;
  }

  function renderLogs() {
    const items = state.logs.items || [];
    const total = Number(state.logs.total || 0);
    const page = Number(state.logs.page || 1);
    const pageSize = Number(state.logs.pageSize || 30);
    const totalPages = Math.max(1, Number(state.logs.totalPages || 1));
    const startIdx = total ? (page - 1) * pageSize + 1 : 0;
    const endIdx = total ? Math.min(total, startIdx + items.length - 1) : 0;
    const pageNums = buildPageNumbers(page, totalPages);
    return `
      <div class="page-head">
        <div>
          <h1>转发记录</h1>
          <p class="page-sub">展示最近的转发、广告跳过与受限重传记录</p>
        </div>
        <div class="page-actions">
          <button class="btn" data-nav="/log-manage">日志管理</button>
          <button class="btn primary" id="btn-refresh-logs">刷新</button>
        </div>
      </div>

      <div class="filter-bar">
        <div class="filter-field">
          <label>账号</label>
          <select id="log-filter-account">
            <option value="">全部账号</option>
            ${(state.logs.accounts || []).map((a) => `<option value="${esc(a.id)}" ${state.logs.account_id === a.id ? 'selected' : ''}>${esc(a.name)}</option>`).join('')}
          </select>
        </div>
        <div class="filter-field">
          <label>状态</label>
          <select id="log-filter-status">
            <option value="" ${state.logs.status === '' ? 'selected' : ''}>全部</option>
            <option value="success" ${state.logs.status === 'success' ? 'selected' : ''}>成功</option>
            <option value="skipped" ${state.logs.status === 'skipped' ? 'selected' : ''}>已拦截/跳过</option>
            <option value="error" ${state.logs.status === 'error' ? 'selected' : ''}>失败</option>
          </select>
        </div>
        <div class="filter-field">
          <label>规则</label>
          <select id="log-filter-rule">
            <option value="">全部规则</option>
            ${(state.logs.rules || []).map((r) => `<option value="${esc(r)}" ${state.logs.rule === r ? 'selected' : ''}>${esc(r)}</option>`).join('')}
          </select>
        </div>
      </div>

      <div class="table-wrap">
        <table>
          <thead><tr><th>时间</th><th>账号 / 规则</th><th>方式</th><th>源频道 → 目标频道</th><th>内容摘要</th><th>操作</th></tr></thead>
          <tbody>
            ${
              items.length
                ? items
                    .map(
                      (it) => `
                  <tr>
                    <td class="mono" style="white-space:nowrap">${fmtTime(it.ts)}</td>
                    <td>
                      <div style="font-weight:500">${esc(it.rule_name)}</div>
                      <div class="hint">${esc(it.account_name || it.account_id || '默认账号')}</div>
                    </td>
                    <td>${modeBadge(it.mode, it.status)}</td>
                    <td>${esc(it.source_title)} → ${esc(it.target_title)}</td>
                    <td>
                      <div>${esc(it.preview || '[媒体消息]')}</div>
                      ${it.detail ? `<div class="hint" style="color:${it.status === 'error' ? 'var(--danger)' : 'var(--text-3)'}">${renderLogDetail(it)}</div>` : ''}
                    </td>
                    <td style="white-space:nowrap">
                      ${
                        it.target_id && Number(it.target_id) !== 0
                          ? `<button type="button" class="btn small" data-resend-log="${it.id}" ${state.resendingIds[it.id] ? 'disabled' : ''}>${state.resendingIds[it.id] ? '重发中...' : '重发'}</button>`
                          : '<span class="muted">-</span>'
                      }
                    </td>
                  </tr>`
                    )
                    .join('')
                : '<tr><td colspan="6" class="empty">暂无符合条件的记录</td></tr>'
            }
          </tbody>
        </table>
      </div>
      <div class="pagination-bar">
        <div class="pagination-info">
          ${total ? `第 ${startIdx}–${endIdx} 条 · 共 ${total} 条（第 ${page} / ${totalPages} 页）` : '共 0 条记录'}
        </div>
        <div class="pagination-controls">
          <button type="button" class="btn small" data-log-page="${page - 1}" ${page <= 1 ? 'disabled' : ''}>上一页</button>
          ${pageNums
            .map((p) =>
              p === '...'
                ? '<span class="pagination-ellipsis">…</span>'
                : `<button type="button" class="btn small ${p === page ? 'primary' : ''}" data-log-page="${p}" ${p === page ? 'aria-current="page"' : ''}>${p}</button>`
            )
            .join('')}
          <button type="button" class="btn small" data-log-page="${page + 1}" ${page >= totalPages ? 'disabled' : ''}>下一页</button>
        </div>
      </div>`;
  }

  function renderRules() {
    const allRules = state.rules || [];
    const rules = allRules
      .map((r, idx) => ({ r, idx }))
      .filter(({ r }) => !state.ruleFilterAccount || (r.account_id || 'default') === state.ruleFilterAccount);

    const renderSourcePills = (sources, idx) => {
      const list = sources || [];
      if (!list.length) return '<span class="hint">未设置源频道</span>';
      const limit = 6;
      const expanded = !!state.expandedRuleSources[idx];
      const visible = expanded || list.length <= limit ? list : list.slice(0, limit);
      const pills = visible
        .map(
          (id) =>
            `<span class="channel-pill" title="${esc(peerTooltip(id))}"><span class="channel-pill-text">${esc(peerShortName(id))}</span></span>`
        )
        .join('');
      const more =
        list.length > limit
          ? `<button type="button" class="link-toggle-btn" data-toggle-sources="${idx}">${expanded ? '收起' : `+${list.length - limit} 更多`}</button>`
          : '';
      return `<div class="channel-pill-list">${pills}</div>${more ? `<div style="margin-top:2px">${more}</div>` : ''}`;
    };

    const renderTargetPills = (targets) => {
      const list = targets || [];
      if (!list.length) return '<span class="hint">未设置目标频道</span>';
      return `<div class="channel-pill-list">${list
        .map(
          (id) =>
            `<span class="channel-pill is-target" title="${esc(peerTooltip(id))}"><span class="channel-pill-text">${esc(peerShortName(id))}</span></span>`
        )
        .join('')}</div>`;
    };

    const renderRuleRegexRefs = (r) => {
      const rxList = (r.regex_ids || []).map(getRegexById).filter(Boolean);
      const legacyCount =
        (r.skip_patterns || []).length +
        (r.strip_text_patterns || []).length +
        (r.strip_patterns || []).length +
        (r.replace_patterns || []).length;
      if (!rxList.length && !legacyCount) {
        return `<div class="rule-card-regex"><div class="rule-card-regex-head"><span>正则过滤</span><span class="muted">未引用</span></div></div>`;
      }
      const chips = rxList
        .map((rx) => {
          const meta = REGEX_KIND_META[rx.kind] || { short: rx.kind };
          return `<button type="button" class="rx-ref-chip kind-${esc(rx.kind)}" data-jump-regex="${esc(rx.id)}" title="${esc(regexContentText(rx))}"><span>${esc(meta.short)}</span><span>·</span><span>${esc(rx.name)}</span></button>`;
        })
        .join('');
      const legacyChip = legacyCount
        ? `<span class="rx-ref-chip" title="历史内联正则，可在正则管理中统一维护">内联正则 ${legacyCount} 条</span>`
        : '';
      return `
        <div class="rule-card-regex">
          <div class="rule-card-regex-head">
            <span>引用正则（${rxList.length}${legacyCount ? ` + ${legacyCount} 内联` : ''}）</span>
            <a href="#/regex" data-nav="/regex" class="link-toggle-btn">管理</a>
          </div>
          <div class="rx-ref-list">${chips}${legacyChip}</div>
        </div>`;
    };

    const renderRuleFlags = (r) => {
      const flags = [];
      if (r.drop_author !== false) flags.push('<span class="flag-tag">无来源</span>');
      if (r.fallback_reupload !== false) flags.push('<span class="flag-tag warn">受限重传</span>');
      if (r.skip_text_only) flags.push('<span class="flag-tag">跳过纯文字</span>');
      if (r.strip_all_text) flags.push('<span class="flag-tag warn">仅发媒体</span>');
      if (r.collect_comments) {
        const mode = (r.comment_merge_mode || state.ruleGlobalDefaults?.comment_merge_mode || 'pack') === 'keep_batches' ? '按原批' : '凑满';
        flags.push(`<span class="flag-tag accent">抓评论区 · ${mode}</span>`);
      }
      if (r.disable_link_preview) flags.push('<span class="flag-tag">禁链接预览</span>');
      return flags.length ? `<div class="rule-card-flags">${flags.join('')}</div>` : '';
    };

    return `
      <div class="page-head">
        <div>
          <h1>转发规则</h1>
          <p class="page-sub">按账号配置多对多频道映射与转发策略，正则过滤统一在「正则管理」维护并引用</p>
        </div>
        <div class="page-actions">
          <button class="btn" data-nav="/regex">正则管理</button>
          <button class="btn primary" id="btn-new-rule">+ 新建规则</button>
        </div>
      </div>

      ${
        state.accounts.length > 1
          ? `<div class="filter-bar">
               <div class="filter-field">
                 <label>按账号筛选</label>
                 <select id="rules-acct-filter">
                   <option value="">全部账号 (${allRules.length})</option>
                   ${state.accounts.map((a) => `<option value="${esc(a.id)}" ${state.ruleFilterAccount === a.id ? 'selected' : ''}>${esc(a.name)}</option>`).join('')}
                 </select>
               </div>
             </div>`
          : ''
      }

      ${
        rules.length
          ? `<div class="rule-grid">
          ${rules
            .map(
              ({ r, idx }) => `
            <article class="rule-card ${r.enabled ? '' : 'is-disabled'}">
              <div class="rule-card-head">
                <div class="rule-card-title-group">
                  <div class="rule-card-title-row">
                    <span class="rule-status-dot ${r.enabled ? 'is-on' : ''}"></span>
                    <div class="rule-card-title" title="${esc(r.name)}">${esc(r.name)}</div>
                  </div>
                  <div class="rule-card-sub">
                    <span class="rule-acct-pill">${esc(accountName(r.account_id || 'default'))}</span>
                    <span>${(r.sources || []).length} 源 → ${(r.targets || []).length} 目标</span>
                  </div>
                </div>
                <div class="rule-card-actions">
                  <button type="button" class="rule-switch ${r.enabled ? 'is-on' : ''}" data-toggle-rule="${idx}" title="${r.enabled ? '点击停用' : '点击启用'}">
                    <span class="rule-switch-track"></span>
                    <span>${r.enabled ? '已启用' : '已停用'}</span>
                  </button>
                  <button type="button" class="btn small" data-edit-rule="${idx}">编辑</button>
                  <button type="button" class="btn small ghost-danger" data-del-rule="${idx}">删除</button>
                </div>
              </div>

              ${renderRuleFlags(r)}

              <div class="rule-flow-box">
                <div class="rule-flow-section">
                  <div class="rule-flow-head">
                    <strong>源频道 <span class="count-pill">${(r.sources || []).length}</span></strong>
                    <span class="muted">悬停查看 ID</span>
                  </div>
                  ${renderSourcePills(r.sources, idx)}
                </div>
                <div class="rule-flow-divider"></div>
                <div class="rule-flow-section">
                  <div class="rule-flow-head">
                    <strong>→ 目标频道 <span class="count-pill">${(r.targets || []).length}</span></strong>
                  </div>
                  ${renderTargetPills(r.targets)}
                </div>
              </div>

              ${renderRuleRegexRefs(r)}
            </article>`
            )
            .join('')}
        </div>`
          : '<div class="panel empty">暂无转发规则，点击右上角「+ 新建规则」开始配置～</div>'
      }`;
  }

  function renderChats() {
    const q = state.chatsQuery.trim().toLowerCase();
    const list = (state.chats || []).filter(
      (c) => !q || c.title.toLowerCase().includes(q) || String(c.id).includes(q) || c.username.toLowerCase().includes(q)
    );
    return `
      <div class="page-head">
        <div>
          <h1>频道目录</h1>
          <p class="page-sub">查看各账号已加入的频道和群组 ID，以及是否开启禁止转发限制</p>
        </div>
        <div class="page-actions">
          <button class="btn primary" id="btn-refresh-chats">刷新列表</button>
        </div>
      </div>

      <div class="filter-bar">
        <div class="filter-field">
          <label>查看账号</label>
          <select id="chats-acct-select">
            ${(state.accounts || []).map((a) => `<option value="${esc(a.id)}" ${state.chatsAccountId === a.id ? 'selected' : ''}>${esc(a.name)}${a.authorized ? '' : ' (未登录)'}</option>`).join('')}
          </select>
        </div>
        ${
          state.chatsAuthorized
            ? `<div class="filter-field grow">
                 <label>搜索名称 / @用户名 / ID</label>
                 <input id="chats-search" type="text" value="${esc(state.chatsQuery)}" placeholder="输入关键词过滤..." />
               </div>`
            : ''
        }
      </div>

      ${
        !state.chatsAuthorized
          ? `<div class="panel empty">当前账号尚未登录，请先到 <a href="#/telegram" style="color:var(--accent-strong);text-decoration:underline">Telegram 账号</a> 页面扫码或手机登录。</div>`
          : `
        <div class="table-wrap">
          <table>
            <thead><tr><th>名称</th><th>类型</th><th>ID（点击复制）</th><th>转发限制</th></tr></thead>
            <tbody>
              ${
                list.length
                  ? list
                      .map(
                        (c) => `
                  <tr>
                    <td>
                      <div style="font-weight:500">${esc(c.title)}</div>
                      ${c.username ? `<div class="hint">@${esc(c.username)}</div>` : ''}
                    </td>
                    <td><span class="badge muted">${c.kind === 'channel' ? '频道' : c.kind === 'supergroup' ? '超级群' : '群组'}</span></td>
                    <td><button class="btn small mono" data-copy="${esc(c.id)}">${esc(c.id)}</button></td>
                    <td>${c.noforwards ? '<span class="badge warning">禁止转发 (需重传)</span>' : '<span class="badge success">允许秒转</span>'}</td>
                  </tr>`
                      )
                      .join('')
                  : '<tr><td colspan="4" class="empty">没有匹配的频道或群组</td></tr>'
              }
            </tbody>
          </table>
        </div>`
      }`;
  }

  function renderTelegram() {
    const accounts = state.accounts || [];
    const loginAcct = accounts.find((a) => a.id === state.loginAccountId);

    return `
      <div class="page-head">
        <div>
          <h1>Telegram 账号</h1>
          <p class="page-sub">管理多个 UserBot 小号，可在转发规则中为每个账号分别配置搬运任务</p>
        </div>
        <div class="page-actions">
          <button class="btn primary" id="btn-add-account">+ 添加账号</button>
        </div>
      </div>

      <div class="rule-grid">
        ${accounts
          .map(
            (a) => `
          <article class="rule-card">
            <div class="rule-card-head">
              <div class="rule-card-title" title="${esc(a.name)}">${esc(a.name)}</div>
              <div class="actions rule-card-actions">
                ${
                  a.enabled && !a.authorized
                    ? `<button class="btn small primary" data-acct-login="${esc(a.id)}">登录 / 扫码</button>`
                    : ''
                }
                <button class="btn small" data-acct-rename="${esc(a.id)}">重命名</button>
                <button class="btn small" data-acct-toggle="${esc(a.id)}" data-acct-enabled="${a.enabled ? '1' : '0'}">${a.enabled ? '停用' : '启用'}</button>
              </div>
            </div>

            <div class="rule-card-badges">
              ${
                !a.enabled
                  ? '<span class="badge muted">已停用</span>'
                  : a.authorized
                  ? `<span class="badge success status">${a.listening ? '监听中' : '已连接'}</span>`
                  : '<span class="badge warning status">等待登录</span>'
              }
              <span class="badge muted">绑定规则 ${a.rules_count} 条</span>
            </div>

            ${
              a.authorized && a.me
                ? `<div class="rule-peers">
                     <div><span class="muted">TG 账号：</span>${esc(a.me.name)} ${a.me.username ? `<span class="mono">@${esc(a.me.username)}</span>` : ''}</div>
                     <div><span class="muted">ID / 手机：</span><span class="mono">${esc(a.me.id)}</span> · <span class="mono">${esc(a.me.phone || '-')}</span></div>
                   </div>`
                : `<div class="hint">${a.enabled ? '点击「登录 / 扫码」完成该小号会话授权' : '账号已停用，不会保持 TG 连接'}</div>`
            }

            <div class="actions">
              ${a.authorized ? `<button class="btn small" data-acct-chats="${esc(a.id)}">频道目录</button>` : ''}
              ${a.authorized ? `<button class="btn small danger" data-acct-logout="${esc(a.id)}">退出登录</button>` : ''}
              ${accounts.length > 1 ? `<button class="btn small danger" data-acct-del="${esc(a.id)}">删除账号</button>` : ''}
            </div>
          </article>`
          )
          .join('')}
      </div>

      ${
        loginAcct && loginAcct.enabled && !loginAcct.authorized
          ? `
        <section class="panel">
          <div class="panel-head">
            <h2 class="panel-title">登录账号：${esc(loginAcct.name)}</h2>
            <button class="btn ghost small" id="btn-close-login-panel">收起</button>
          </div>
          <div class="tg-grid">
            <div class="qr-box">
              <strong>方式一：扫码登录（推荐）</strong>
              ${
                state.qrDataUrl && loginAcct.qr?.status === 'waiting'
                  ? `<img src="${state.qrDataUrl}" alt="Telegram Login QR" />
                     <div class="hint">请用目标小号 Telegram App → 设置 → 设备 → 连接桌面设备 扫码</div>`
                  : loginAcct.qr?.status === 'need_2fa'
                  ? `<div class="field" style="width:100%;text-align:left">
                       <label>该账号开启了两步验证 (2FA)</label>
                       <input id="qr-2fa-pwd" type="password" placeholder="输入 2FA 云密码" />
                     </div>
                     <button class="btn primary" id="btn-qr-2fa">提交 2FA 密码</button>`
                  : `<div class="hint">${esc(loginAcct.qr?.error || '点击下方按钮生成登录二维码')}</div>`
              }
              <button class="btn ${loginAcct.qr?.status === 'waiting' ? '' : 'primary'}" id="btn-qr-start">
                ${loginAcct.qr?.status === 'waiting' ? '刷新二维码' : '生成登录二维码'}
              </button>
            </div>

            <div class="stack" style="padding:10px 4px">
              <strong>方式二：手机号验证码登录</strong>
              <div class="field">
                <label>手机号（含国际区号）</label>
                <input id="tg-phone" type="text" value="${esc(state.phoneNum)}" placeholder="+8613800138000" />
              </div>
              ${
                state.phoneStep !== 'input'
                  ? `<div class="field">
                       <label>Telegram 登录验证码${state.phoneStep === 'need_2fa' ? '（已验证 ✓）' : ''}</label>
                       <input id="tg-otp" type="text" value="${esc(state.phoneCode)}" placeholder="输入收到的 5 位验证码" ${state.phoneStep === 'need_2fa' ? 'disabled' : ''} />
                     </div>`
                  : ''
              }
              ${
                state.phoneStep === 'need_2fa'
                  ? `<div class="field">
                       <label>两步验证 (2FA) 固定云密码</label>
                       <input id="tg-2fa" type="password" placeholder="输入你设置的固定 2FA 云密码" />
                     </div>`
                  : ''
              }
              <div class="actions">
                <button class="btn" id="btn-phone-send">发送验证码</button>
                ${state.phoneStep !== 'input' ? '<button class="btn primary" id="btn-phone-verify">验证并登录</button>' : ''}
              </div>
            </div>
          </div>
        </section>`
          : ''
      }`;
  }

  function renderSettings() {
    const s = state.settings;
    if (!s) return '<div class="panel empty">加载设置中...</div>';
    const tokenUrl = `${location.origin}/#access-token=${encodeURIComponent(s.access_token)}`;
    return `
      <div class="page-head">
        <div>
          <h1>全局设置</h1>
          <p class="page-sub">设置转发间隔、相册与评论收集时间、去重周期和面板访问 Token</p>
        </div>
      </div>

      <section class="panel">
        <div class="panel-head"><h2 class="panel-title">运行参数</h2></div>
        <form id="settings-form" class="settings-form">
          <div class="settings-group">
            <div class="settings-group-head">
              <span class="settings-group-title">基础转发与去重</span>
              <span class="settings-group-sub">控制全局限速、受限频道兜底体积与消息去重窗口</span>
            </div>
            <div class="settings-grid-3">
              <div class="field">
                <label for="set-delay">转发间隔（秒）</label>
                <input id="set-delay" type="number" step="0.1" min="0" required value="${esc(s.delay_seconds)}" />
                <span class="hint">每次发帖后停顿时间，建议 ≥ 1.0 防止 TG 限流</span>
              </div>
              <div class="field">
                <label for="set-maxmb">受限频道单次下载上限（MB）</label>
                <input id="set-maxmb" type="number" min="1" step="1" required value="${esc(s.max_download_mb)}" />
                <span class="hint">仅针对禁止转发频道的兜底重传生效</span>
              </div>
              <div class="field">
                <label for="set-ttl">消息去重保留时长（小时）</label>
                <input id="set-ttl" type="number" min="1" step="1" required value="${esc(s.dedup_ttl_hours)}" />
                <span class="hint">已转发记录在此时长内不重复处理，过期后自动清理</span>
              </div>
            </div>
          </div>

          <div class="settings-group">
            <div class="settings-group-head">
              <span class="settings-group-title">相册收集</span>
              <span class="settings-group-sub">控制多图/多视频相册分批到达时的聚合等待时间</span>
            </div>
            <div class="settings-grid-2">
              <div class="field">
                <label for="set-album-quiet">相册静默等待（秒）</label>
                <input id="set-album-quiet" type="number" min="1" max="300" step="1" required value="${esc(s.album_quiet_seconds ?? 15)}" />
                <span class="hint">同组没有新增媒体达到此时长后发出，新增媒体会重新计时</span>
              </div>
              <div class="field">
                <label for="set-album-max-wait">相册最长收集时间（秒）</label>
                <input id="set-album-max-wait" type="number" min="1" max="300" step="1" required value="${esc(s.album_max_wait_seconds ?? 45)}" />
                <span class="hint">从第一项开始计时，必须大于或等于静默等待时间；凑满 10 项可提前发送</span>
              </div>
            </div>
          </div>

          <div class="settings-group">
            <div class="settings-group-head">
              <span class="settings-group-title">评论区默认参数</span>
              <span class="settings-group-sub">控制评论区初次等待、持续顺延机制与默认分组方式</span>
            </div>
            <div class="settings-grid-2">
              <div class="field">
                <label for="set-comment-wait">评论区默认等待收集窗口（秒）</label>
                <input id="set-comment-wait" type="number" min="5" max="300" step="1" required value="${esc(s.comment_wait_seconds ?? 30)}" />
                <span class="hint">主帖发出后先等待该时长再首次抓取评论；规则未单独覆盖时默认使用</span>
              </div>
              <div class="field">
                <label for="set-comment-mode">评论区默认媒体分组方式</label>
                <select id="set-comment-mode">
                  <option value="pack" ${(s.comment_merge_mode || 'pack') === 'pack' ? 'selected' : ''}>自动凑满（保留主帖原相册，每组最多 10 项）</option>
                  <option value="keep_batches" ${s.comment_merge_mode === 'keep_batches' ? 'selected' : ''}>按原批次发送（保持主帖与每条评论各自数量）</option>
                </select>
                <span class="hint">决定主帖与作者评论媒体合并时的相册拆分方式（可在单条规则覆盖）</span>
              </div>
              <div class="field">
                <label for="set-comment-silence">评论区静默顺延（秒）</label>
                <input id="set-comment-silence" type="number" min="5" max="300" step="1" required value="${esc(s.comment_silence_seconds ?? 30)}" />
                <span class="hint">抓取时若最后一条作者评论距今未满此时长，继续每 5 秒轮询等下一批补图；建议 ≥ 25 覆盖多组上传间隔</span>
              </div>
              <div class="field">
                <label for="set-comment-max-extra">评论区额外顺延上限（秒）</label>
                <input id="set-comment-max-extra" type="number" min="0" max="600" step="1" required value="${esc(s.comment_max_extra_seconds ?? 180)}" />
                <span class="hint">在等待窗口之外最多再顺延的总时长（设 0 不顺延）；若作者提前停止传图会立即发出，不会白白等满</span>
              </div>
            </div>
          </div>

          <div class="actions settings-actions">
            <button class="btn primary" type="submit">保存设置</button>
          </div>
        </form>
      </section>

      <section class="panel">
        <div class="panel-head"><h2 class="panel-title">Telegram API 凭据（MTProto）</h2>
          <span class="badge ${s.api_custom ? 'info' : 'muted'}">${s.api_custom ? '自定义凭据' : '系统默认 (.env)'}</span>
        </div>
        <form id="api-settings-form" class="settings-grid-2">
          <div class="field">
            <label for="set-api-id">API ID</label>
            <input id="set-api-id" type="number" min="1" class="mono" value="${esc(s.api_id || '')}" required />
          </div>
          <div class="field">
            <label for="set-api-hash">API Hash</label>
            <input id="set-api-hash" type="password" class="mono" placeholder="${s.api_hash_masked ? '已配置 (' + esc(s.api_hash_masked) + ')，不改请留空' : '输入 API Hash'}" />
          </div>
          <div class="span-full hint">
            可在 <a href="https://my.telegram.org" target="_blank" rel="noopener" style="color:var(--accent-strong);text-decoration:underline">my.telegram.org</a> 申请专属 API ID / Hash。保存后会自动重连所有账号；留空 API Hash 则保留当前值。
          </div>
          <div class="span-full actions">
            <button class="btn primary" type="submit">保存 API 凭据</button>
            ${s.api_custom ? '<button class="btn" type="button" id="btn-reset-api">恢复系统默认</button>' : ''}
          </div>
        </form>
      </section>

      <section class="panel">
        <div class="panel-head"><h2 class="panel-title">免密直达链接（Access Token）</h2></div>
        <div class="field">
          <label>直达登录链接</label>
          <input type="text" readonly class="mono" value="${esc(tokenUrl)}" />
          <span class="hint">包含访问 Token，请妥善保管。</span>
        </div>
        <div class="actions" style="margin-top:14px">
          <button class="btn" data-copy="${esc(tokenUrl)}">复制直达链接</button>
          <button class="btn danger" id="btn-rotate-token">重置 Token</button>
        </div>
      </section>`;
  }

  function renderLogManage() {
    const lm = state.logManage;
    if (!lm) return '<div class="panel empty">加载日志统计中...</div>';
    const st = lm.stats || {};
    const dbKb = ((st.db_size_bytes || 0) / 1024).toFixed(1);
    const oldest = st.oldest_ts ? fmtTime(st.oldest_ts) : '暂无';
    return `
      <div class="page-head">
        <div>
          <h1>日志管理</h1>
          <p class="page-sub">设置转发与拦截记录的保留时长、条数上限，或按需手动清理历史日志</p>
        </div>
        <div class="page-actions">
          <button class="btn" data-nav="/logs">查看转发记录</button>
          <button class="btn primary" id="btn-refresh-log-manage">刷新统计</button>
        </div>
      </div>

      <section class="metrics-row">
        <div class="metric-card">
          <span>总记录数</span>
          <strong>${st.total_count || 0}</strong>
          <small>数据库 ${dbKb} KB</small>
        </div>
        <div class="metric-card">
          <span>成功转发</span>
          <strong>${st.success_count || 0}</strong>
          <small>正常发出记录</small>
        </div>
        <div class="metric-card">
          <span>已拦截 / 跳过</span>
          <strong>${st.skipped_count || 0}</strong>
          <small>广告、纯文字或受限跳过</small>
        </div>
        <div class="metric-card">
          <span>失败 / 最早记录</span>
          <strong>${st.error_count || 0}</strong>
          <small>最早：${esc(oldest)}</small>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head"><h2 class="panel-title">日志保留策略</h2></div>
        <form id="log-manage-form" class="form-grid">
          <div class="field">
            <label>日志保留时长（天）</label>
            <input id="lm-days" type="number" min="0" value="${esc(lm.log_retention_days)}" />
            <span class="hint">填 0 表示不按天数过期；新写入日志及服务重载时会自动清理超出天数的旧记录</span>
          </div>
          <div class="field">
            <label>最多保留条数</label>
            <input id="lm-maxrows" type="number" min="100" step="100" value="${esc(lm.log_max_rows)}" />
            <span class="hint">按时间顺序只保留最新 N 条（最低 100 条），防止日志表无限膨胀</span>
          </div>
          <div class="span-full actions">
            <button type="button" class="btn small" data-lm-preset-days="7">7 天</button>
            <button type="button" class="btn small" data-lm-preset-days="30">30 天</button>
            <button type="button" class="btn small" data-lm-preset-days="90">90 天</button>
            <button type="button" class="btn small" data-lm-preset-days="0">永久保留 (0)</button>
            <button class="btn primary" type="submit" style="margin-left:auto">保存保留策略并应用</button>
          </div>
        </form>
      </section>

      <section class="panel">
        <div class="panel-head"><h2 class="panel-title">手动清理日志</h2></div>
        <p class="hint" style="margin:0 0 14px">清理仅删除历史记录（转发记录/拦截记录），不会影响去重记录、账号登录状态或转发规则。</p>
        <div class="actions">
          <button type="button" class="btn" id="btn-prune-logs">按当前策略清理过期日志</button>
          <button type="button" class="btn" data-clear-logs="skipped">仅清空已拦截/跳过记录</button>
          <button type="button" class="btn" data-clear-logs="error">仅清空失败记录</button>
          <button type="button" class="btn danger" data-clear-logs="">清空全部日志</button>
        </div>
      </section>`;
  }

  function isPeerSelected(list, chat) {
    const idStr = String(chat.id);
    const uname = chat.username ? '@' + chat.username.toLowerCase() : '';
    return (list || []).some((x) => {
      const s = String(x).trim();
      return s === idStr || (uname && s.toLowerCase() === uname);
    });
  }

  function getOtherRulesUsage(chat, modalIdx, accountId) {
    const acct = accountId || 'default';
    const srcRules = [];
    const tgtRules = [];
    (state.rules || []).forEach((r, i) => {
      if (i === modalIdx) return;
      if ((r.account_id || 'default') !== acct) return;
      const rName = r.name || `规则#${i + 1}`;
      if (isPeerSelected(r.sources, chat)) srcRules.push(rName);
      if (isPeerSelected(r.targets, chat)) tgtRules.push(rName);
    });
    return { srcRules, tgtRules };
  }

  function fmtRuleNamesBadge(prefix, names) {
    if (!names.length) return '';
    const shortText = names.length === 1 ? `${prefix}: ${names[0]}` : `${prefix}: ${names[0]} 等${names.length}条`;
    return { text: shortText, title: `${prefix}（${names.join('、')}）` };
  }

  function renderPickerItemsHtml(chatList, data, query, modalIdx = null) {
    const q = String(query || '').trim().toLowerCase();
    const filtered = q
      ? chatList.filter((c) => {
          const t = `${c.title || ''} ${c.id || ''} ${c.username || ''}`.toLowerCase();
          return t.includes(q);
        })
      : chatList;
    if (!filtered.length) return '<div class="hint" style="padding:12px 4px">没有匹配的频道或群组</div>';
    const acctId = data.account_id || 'default';
    return filtered
      .map((c) => {
        const isSrc = isPeerSelected(data.sources, c);
        const isTgt = isPeerSelected(data.targets, c);
        const other = getOtherRulesUsage(c, modalIdx, acctId);
        const usedByOther = other.srcRules.length > 0 || other.tgtRules.length > 0;
        const otherSrcBadge = fmtRuleNamesBadge('其他规则源', other.srcRules);
        const otherTgtBadge = fmtRuleNamesBadge('其他规则目标', other.tgtRules);
        const kindLabel = c.kind === 'channel' ? '频道' : c.kind === 'supergroup' ? '超级群' : '群组';
        const rowCls = isSrc || isTgt ? 'is-selected' : usedByOther ? 'is-other-selected' : '';
        return `
          <div class="picker-item ${rowCls}">
            <div class="picker-item-meta">
              <span>${esc(c.title)}</span>
              <span class="badge muted">${kindLabel}</span>
              <span class="mono muted">${esc(c.id)}</span>
              ${c.noforwards ? '<span class="badge warning">禁转发</span>' : ''}
              ${isSrc ? '<span class="badge info">✓ 已选为源</span>' : ''}
              ${isTgt ? '<span class="badge success">✓ 已选为目标</span>' : ''}
              ${otherSrcBadge ? `<span class="badge other-src" title="${esc(otherSrcBadge.title)}">${esc(otherSrcBadge.text)}</span>` : ''}
              ${otherTgtBadge ? `<span class="badge other-tgt" title="${esc(otherTgtBadge.title)}">${esc(otherTgtBadge.text)}</span>` : ''}
            </div>
            <span class="actions" style="flex-shrink:0">
              <button type="button" class="btn small ${isSrc ? 'picked-src' : ''}" data-pick-src="${esc(c.id)}">${isSrc ? '✓ 源' : '+ 源'}</button>
              <button type="button" class="btn small ${isTgt ? 'picked-tgt' : ''}" data-pick-tgt="${esc(c.id)}">${isTgt ? '✓ 目标' : '+ 目标'}</button>
            </span>
          </div>`;
      })
      .join('');
  }

  function renderRuleModal() {
    if (!state.modalRule) return '';
    const { idx, data, chats } = state.modalRule;
    const isNew = idx === null;
    const chatList = chats || [];
    const selectedRxIds = new Set(data.regex_ids || []);
    const allRegex = state.regexRules || [];
    const rxFilter = state.modalRuleRxFilter || '';
    const visibleRegex = rxFilter ? allRegex.filter((rx) => rx.kind === rxFilter) : allRegex;
    const sortedRegex = [...visibleRegex].sort((a, b) => {
      const oa = REGEX_KIND_ORDER.indexOf(a.kind);
      const ob = REGEX_KIND_ORDER.indexOf(b.kind);
      return (oa === -1 ? 9 : oa) - (ob === -1 ? 9 : ob);
    });

    const optCard = (id, checked, title, desc, extraCls = '') => `
      <label class="option-card ${checked ? 'is-checked' : ''} ${extraCls}">
        <input id="${id}" type="checkbox" ${checked ? 'checked' : ''} />
        <div class="option-card-body">
          <strong>${title}</strong>
          <span>${desc}</span>
        </div>
      </label>`;

    return `
      <div class="modal-overlay" id="rule-modal-overlay">
        <div class="modal modal-lg">
          <div class="modal-head">
            <div class="modal-title-wrap">
              <h3 class="modal-title">${isNew ? '新建转发规则' : `编辑规则 · ${esc(data.name || '未命名规则')}`}</h3>
              <span class="modal-sub">顶部与底部操作栏固定常驻，按 Esc 可随时关闭</span>
            </div>
            <button type="button" class="btn ghost icon small" id="btn-close-rule-modal" title="关闭 (Esc)">${icon('close', 18)}</button>
          </div>

          <div class="modal-body" id="rule-modal-body">
            <section class="modal-section">
              <div class="modal-section-head">
                <span class="modal-section-title"><span class="section-step">1</span>基本信息</span>
                <label class="checkbox" style="font-weight:500;color:var(--text)">
                  <input id="rm-enabled" type="checkbox" ${data.enabled ? 'checked' : ''} />
                  <span>启用此规则</span>
                </label>
              </div>
              <div class="form-grid">
                <div class="field">
                  <label>规则名称</label>
                  <input id="rm-name" type="text" value="${esc(data.name)}" placeholder="例如：写真 / 舞蹈 / 资讯搬运" />
                </div>
                <div class="field">
                  <label>执行账号</label>
                  <select id="rm-account">
                    ${(state.accounts || []).map((a) => `<option value="${esc(a.id)}" ${(data.account_id || 'default') === a.id ? 'selected' : ''}>${esc(a.name)}</option>`).join('')}
                  </select>
                </div>
              </div>
            </section>

            <section class="modal-section">
              <div class="modal-section-head">
                <span class="modal-section-title"><span class="section-step">2</span>频道映射</span>
                <span class="hint">已选 ${(data.sources || []).length} 个源频道 → ${(data.targets || []).length} 个目标频道</span>
              </div>

              <div class="mapping-cols">
                <div class="mapping-col">
                  <div class="mapping-col-head">
                    <span>源频道（监听新消息）</span>
                    <span class="count-pill">${(data.sources || []).length}</span>
                  </div>
                  <div class="peer-chip-list">
                    ${
                      (data.sources || []).length
                        ? (data.sources || [])
                            .map(
                              (s, i) =>
                                `<span class="peer-chip" title="${esc(peerTooltip(s))}"><span class="peer-chip-name">${esc(peerShortName(s))}</span><button type="button" data-rm-del-src="${i}" title="移除">×</button></span>`
                            )
                            .join('')
                        : '<span class="hint">暂未选择源频道</span>'
                    }
                  </div>
                  <div class="mapping-add-row">
                    <input id="rm-add-src-input" type="text" placeholder="手动输入 -100... 或 @username" />
                    <button type="button" class="btn small" id="btn-rm-add-src">添加</button>
                  </div>
                </div>

                <div class="mapping-col">
                  <div class="mapping-col-head">
                    <span>目标频道（搬运到这里）</span>
                    <span class="count-pill">${(data.targets || []).length}</span>
                  </div>
                  <div class="peer-chip-list">
                    ${
                      (data.targets || []).length
                        ? (data.targets || [])
                            .map(
                              (t, i) =>
                                `<span class="peer-chip is-target" title="${esc(peerTooltip(t))}"><span class="peer-chip-name">${esc(peerShortName(t))}</span><button type="button" data-rm-del-tgt="${i}" title="移除">×</button></span>`
                            )
                            .join('')
                        : '<span class="hint">暂未选择目标频道</span>'
                    }
                  </div>
                  <div class="mapping-add-row">
                    <input id="rm-add-tgt-input" type="text" placeholder="手动输入 -100... 或 @username" />
                    <button type="button" class="btn small" id="btn-rm-add-tgt">添加</button>
                  </div>
                </div>
              </div>

              ${
                chatList.length
                  ? `<div class="field">
                       <div class="row" style="justify-content:space-between;gap:8px">
                         <label style="color:var(--text-2);font-size:13px;font-weight:500">从「${esc(accountName(data.account_id || 'default'))}」快速勾选（共 ${chatList.length} 个）</label>
                         <span class="hint">蓝 = 当前规则已选 · 琥珀 = 其他规则已选</span>
                       </div>
                       <div class="picker-toolbar">
                         <input id="rm-chat-filter" type="text" value="${esc(state.modalPickerQuery)}" placeholder="搜索频道名称、ID 或 @username..." />
                       </div>
                       <div class="picker-box" id="rm-picker-box">
                         ${renderPickerItemsHtml(chatList, data, state.modalPickerQuery, idx)}
                       </div>
                     </div>`
                  : '<div class="hint">该账号暂无已加入频道或尚未登录，可直接在上方手动输入频道 ID / @username</div>'
              }
            </section>

            <section class="modal-section">
              <div class="modal-section-head">
                <span class="modal-section-title"><span class="section-step">3</span>转发策略</span>
              </div>
              <div class="option-grid">
                ${optCard('rm-drop-author', data.drop_author !== false, '无来源转发', '去除“转发自”标签，普通频道零下载秒转')}
                ${optCard('rm-fallback', data.fallback_reupload !== false, '受限频道自动重传', '遇到禁止转发频道时自动下载缓存后重传')}
                ${optCard('rm-skip-text-only', !!data.skip_text_only, '跳过纯文字消息', '丢弃无媒体的纯文字贴，保留媒体配文')}
                ${optCard('rm-strip-all-text', !!data.strip_all_text, '去除全部文本（仅发媒体）', '清空所有配文，仍会先按原配文跑跳过正则')}
                ${optCard('rm-skip-empty', data.skip_empty_text_only !== false, '跳过清洗后空文本', '纯文字广告被删空后不发空白消息')}
                ${optCard('rm-no-preview', !!data.disable_link_preview, '禁用链接网页预览', '隐藏消息底部的链接大图卡片')}
                ${optCard('rm-collect-comments', !!data.collect_comments, '抓取评论区频道马甲媒体', '收集主帖评论区里频道自身发的补图/视频；主帖无字时取首条作者评论', 'span-full')}
              </div>

              ${
                data.collect_comments
                  ? `<div class="form-grid" style="padding:12px 14px;background:var(--surface-muted);border:1px solid var(--line);border-radius:10px">
                       <div class="field">
                         <label>评论收集等待窗口（秒，可选覆盖）</label>
                         <input id="rm-comment-wait" type="number" min="5" max="300" placeholder="跟随全局默认 (${esc(state.ruleGlobalDefaults?.comment_wait_seconds ?? 30)} 秒)" value="${data.comment_wait_seconds != null ? esc(data.comment_wait_seconds) : ''}" />
                         <span class="hint">留空继承「全局设置」；作者持续补图会自动顺延</span>
                       </div>
                       <div class="field">
                         <label>媒体分组发送方式（可选覆盖）</label>
                         <select id="rm-comment-mode">
                           <option value="" ${!data.comment_merge_mode ? 'selected' : ''}>跟随全局默认（${(state.ruleGlobalDefaults?.comment_merge_mode || 'pack') === 'keep_batches' ? '按原批次' : '自动凑满'}）</option>
                           <option value="pack" ${data.comment_merge_mode === 'pack' ? 'selected' : ''}>单独指定：自动凑满（保留主帖原相册）</option>
                           <option value="keep_batches" ${data.comment_merge_mode === 'keep_batches' ? 'selected' : ''}>单独指定：按原批次发送</option>
                         </select>
                       </div>
                     </div>`
                  : ''
              }
            </section>

            <section class="modal-section">
              <div class="modal-section-head">
                <span class="modal-section-title"><span class="section-step">4</span>引用正则规则 <span class="count-pill">${selectedRxIds.size}</span></span>
                <button type="button" class="btn small" id="btn-rm-goto-regex">前往「正则管理」 ↗</button>
              </div>
              <div class="hint" style="margin-top:-6px">正则统一在左侧「正则管理」中创建和维护，此处直接勾选引用即可（执行顺序：整条跳过 → 命中清文 → 删除文本 → 查找替换）。</div>

              ${
                allRegex.length
                  ? `
                  <div class="rx-tabs">
                    <button type="button" class="rx-tab ${!rxFilter ? 'is-active' : ''}" data-rm-rx-filter="">全部 (${allRegex.length})</button>
                    ${REGEX_KIND_ORDER.map((k) => {
                      const cnt = allRegex.filter((x) => x.kind === k).length;
                      if (!cnt) return '';
                      return `<button type="button" class="rx-tab ${rxFilter === k ? 'is-active' : ''}" data-rm-rx-filter="${k}">${REGEX_KIND_META[k].label} (${cnt})</button>`;
                    }).join('')}
                  </div>
                  <div class="rx-select-list">
                    ${sortedRegex
                      .map((rx) => {
                        const checked = selectedRxIds.has(rx.id);
                        const meta = REGEX_KIND_META[rx.kind] || { label: rx.kind, badge: 'muted' };
                        return `
                          <label class="rx-select-item ${checked ? 'is-selected' : ''}">
                            <input type="checkbox" data-rm-toggle-rx="${esc(rx.id)}" ${checked ? 'checked' : ''} />
                            <div class="rx-select-main">
                              <div class="rx-select-top">
                                <span class="badge ${meta.badge}">${meta.label}</span>
                                <strong>${esc(rx.name)}</strong>
                                ${rx.note ? `<span class="hint">${esc(rx.note)}</span>` : ''}
                              </div>
                              <div class="rx-select-code" title="${esc(regexContentText(rx))}">${esc(regexPreviewLine(rx))}</div>
                            </div>
                          </label>`;
                      })
                      .join('')}
                  </div>`
                  : `<div class="empty" style="padding:20px;background:var(--surface-muted);border-radius:10px">
                       正则库还是空的，先去 <button type="button" class="link-toggle-btn" id="btn-rm-goto-regex-empty">正则管理</button> 添加几条正则再来引用吧～
                     </div>`
              }

              <div class="collapsible-box">
                <button type="button" class="collapsible-head" id="btn-toggle-rm-test">
                  <span>在线测试当前已选正则效果</span>
                  <span class="hint">${state.modalRuleTestOpen ? '收起 ▲' : '展开 ▼'}</span>
                </button>
                ${
                  state.modalRuleTestOpen
                    ? `<div class="collapsible-body">
                         <div class="field">
                           <textarea id="rm-test-sample" style="min-height:72px">${esc(state.testInput)}</textarea>
                         </div>
                         <div class="actions">
                           <button type="button" class="btn small primary" id="btn-test-regex">运行测试</button>
                           <span class="hint">按当前已勾选的 ${selectedRxIds.size} 条正则 +「去除全部文本」开关模拟清洗</span>
                         </div>
                         ${
                           state.testOutput
                             ? `<div class="test-result">${
                                 state.testOutput.skipped
                                   ? `<span class="badge warning">已拦截跳过</span> 命中规则：<code>${esc(state.testOutput.skip_reason)}</code>`
                                   : state.testOutput.cleared_all_text
                                   ? `<span class="badge warning">已清空全部文本</span> 命中：<code>${esc(state.testOutput.clear_reason)}</code>（将仅发送媒体）`
                                   : `<div><span class="badge ${state.testOutput.changed ? 'info' : 'muted'}">${state.testOutput.changed ? '已清洗' : '无变化'}</span></div><div style="margin-top:6px">${esc(state.testOutput.cleaned || '(配文已清空，仅发送媒体)')}</div>`
                               }</div>`
                             : ''
                         }
                       </div>`
                    : ''
                }
              </div>
            </section>
          </div>

          <div class="modal-actions">
            <span class="modal-foot-summary">已选 ${(data.sources || []).length} 源 → ${(data.targets || []).length} 目标 · 引用 ${selectedRxIds.size} 条正则</span>
            <div class="modal-actions-right">
              <button type="button" class="btn ghost" id="btn-cancel-rule-modal">取消</button>
              <button type="button" class="btn primary" id="btn-save-rule">保存规则</button>
            </div>
          </div>
        </div>
      </div>`;
  }

  function renderAddAccountModal() {
    if (!state.modalAddAccount) return '';
    return `
      <div class="modal-overlay" id="add-acct-overlay">
        <div class="modal" style="max-width:440px">
          <div class="modal-head">
            <h3 class="modal-title">添加 Telegram 账号</h3>
            <button type="button" class="btn ghost icon small" id="btn-close-add-acct">${icon('close', 17)}</button>
          </div>
          <form id="add-acct-form" class="modal-form">
            <div class="modal-body plain">
              <div class="field">
                <label>账号备注名称</label>
                <input id="add-acct-name" type="text" placeholder="例如：搬运小号2 / 美区小号" required />
                <span class="hint">添加后即可使用网页扫码或手机号验证码登录</span>
              </div>
            </div>
            <div class="modal-actions">
              <div class="modal-actions-right">
                <button type="button" class="btn ghost" id="btn-cancel-add-acct">取消</button>
                <button type="submit" class="btn primary">创建并去登录</button>
              </div>
            </div>
          </form>
        </div>
      </div>`;
  }

  function renderGuide() {
    return `
      <div class="page-head">
        <div>
          <h1>使用说明</h1>
          <p class="page-sub">转发规则配置详解与多账号功能速查</p>
        </div>
        <div class="page-actions">
          <button class="btn" data-nav="/settings">去全局设置</button>
          <button class="btn primary" data-nav="/rules">去配置规则</button>
        </div>
      </div>

      <section class="panel">
        <div class="panel-head">
          <h2 class="panel-title">一、转发规则配置详解（核心）</h2>
          <span class="badge info">保存即热生效 · 仅监控新消息</span>
        </div>

        <div class="stack">
          <div class="guide-item">
            <strong>1. 多账号绑定与监听时机</strong>
            <ul class="guide-list">
              <li><b>多账号分工</b>：每条规则都可以指定由哪个已登录的 TG 账号执行，互不干扰。</li>
              <li><b>支持范围</b>：支持频道、超级群、普通群；规则启用后只自动搬运<b>之后新发布</b>的消息，不会刷历史旧帖。</li>
              <li><b>权限要求</b>：源频道只需该账号加入；目标频道需把该账号设为<b>管理员</b>并开启「发布消息」权限。</li>
            </ul>
          </div>

          <div class="guide-item">
            <strong>2. 源频道与目标频道（支持多对多）</strong>
            <ul class="guide-list">
              <li>在规则弹窗顶部选好「执行账号」后，下方「快速勾选」会自动列出该账号已加入的频道和群组，直接点 <b>+ 源</b> / <b>+ 目标</b> 即可。</li>
              <li>也可手动填数字 ID（频道/超级群为 <span class="code-snip">-100</span> 开头）或 <span class="code-snip">@username</span>。</li>
            </ul>
          </div>

          <div class="guide-item">
            <strong>3. 七个规则开关说明</strong>
            <ul class="guide-list">
              <li><b>无来源转发</b>：不显示「转发自 xxx」；普通频道零下载秒转，保留原排版与相册。</li>
              <li><b>禁止转发频道自动下载重传</b>：遇到保护内容频道时，自动缓存下载后再上传，传完清理缓存。</li>
              <li><b>跳过纯文字消息（保留媒体配文）</b>：直接过滤掉没有图片/视频/文件的纯文字贴（含仅带网页预览的文字贴），同时<b>正常保留媒体帖的配文</b>（仍会经过正则清洗）。</li>
              <li><b>去除全部文本（清空配文，仅发媒体）</b>：清空图片/视频/文件配文只发媒体本身（纯文字贴自动跳过）。<b>注意：仍会先按原配文跑一遍「整条跳过正则」拦截广告图文，再清空文本发送！</b></li>
              <li><b>清洗后为空的纯文本跳过</b>：纯文字广告被正则删空后不发空白消息。</li>
              <li><b>禁用链接网页预览</b>：隐藏消息底部的链接大图摘要卡片。</li>
              <li><b>抓取评论区频道马甲媒体</b>：开启后等待设定窗口，仅抓取评论区里以「频道自身身份」发的图片或视频。主帖原相册会保留，评论媒体可自动凑组（每组最多 10 项）或按原批次发送。文字优先用主帖，主帖无字时取首条带文字的频道马甲评论。</li>
            </ul>
          </div>

          <div class="guide-item">
            <strong>4. 正则规则生效的先后顺序（从高到低）</strong>
            <ul class="guide-list">
              <li><b>第 1 步 · 整条跳过（skip_patterns）［最高优先级］</b>：先用原消息正文/配文匹配。一旦命中，<b>整条消息（连带图片/视频）直接拦截丢弃</b>，后续规则都不再执行。</li>
              <li><b>第 2 步 · 清空全部文本（strip_text_patterns /「去除全部文本」开关）</b>：未触发跳过时，如果勾选了「去除全部文本」或原配文命中了「命中即清空全部文本正则」，<b>直接清空所有配文只转发媒体</b>（纯文字贴跳过），不再走第 3、4 步。</li>
              <li><b>第 3 步 · 删除广告文本（strip_patterns）</b>：未清空全部文本时，按从上到下的顺序逐条删除匹配的字句或整行（保留正文的加粗、超链接与剧透格式）。</li>
              <li><b>第 4 步 · 查找替换（replace_patterns）</b>：在第 3 步删完广告后的文本基础上，再按顺序做关键词替换。</li>
              <li><b>第 5 步 · 空文本兜底</b>：经过清洗后如果既无媒体、文字又变成空白，且开启了「清洗后为空的纯文本跳过」，则跳过不发。</li>
            </ul>
          </div>

          <div class="guide-item">
            <strong>5. 常用写法示例（每行一条）</strong>
            <ul class="guide-list">
              <li><b>整条跳过（skip_patterns）</b>：<span class="code-snip">(?i)#ad\\b|广告合作|商务联系</span> 或中文直接 <span class="code-snip">付费广告|推广</span></li>
              <li><b>命中即清空全部文本（strip_text_patterns）</b>：<span class="code-snip">点击查看|原频道推广</span>（命中后只发图/视频，不带文字）</li>
              <li><b>删除广告片段（strip_patterns）</b>：<br/>
                • 删含关键词整行：<span class="code-snip">(?m)^.*(?:关注频道|备用频道|投稿联系).*$</span><br/>
                • 删所有 @提及：<span class="code-snip">@\\w+</span><br/>
                • 删固定宣传语：直接写字面文本即可，如 <span class="code-snip">点击加入群组</span>
              </li>
              <li><b>查找替换（replace_patterns）</b>：格式为 <span class="code-snip">查找内容 =&gt; 替换为</span>，如 <span class="code-snip">@old =&gt; @new</span>。</li>
              <li><b>不会写正则？</b>先去左侧「正则管理」统一创建（内置关键词一键生成与常用模板），再到转发规则里直接勾选引用；之后修改正则库会自动同步热生效到所有引用该正则的规则。</li>
            </ul>
          </div>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head">
          <h2 class="panel-title">二、相册与评论区等待时间详解（全局设置）</h2>
          <span class="badge muted">防漏图 · 防截断</span>
        </div>

        <div class="stack">
          <div class="guide-item">
            <strong>1. 相册收集：为什么有「静默等待」和「最长收集时间」？</strong>
            <ul class="guide-list">
              <li><b>工作原理</b>：Telegram 发送一组多图/视频相册时，客户端往往分几批推送消息。收到第一项后，系统先等待约 4 秒并去源频道回查同组消息；只要发现有新成员加入，就会重置「静默计时」。</li>
              <li><b>相册静默等待（秒，默认 15）</b>：同组消息连续这么久没有新增任何媒体，就认为主帖相册已经收齐，立刻开始转发或进入评论等待。</li>
              <li><b>相册最长收集时间（秒，默认 45）</b>：从收到第一项开始算起的总兜底上限（必须 ≥ 静默等待）。即使某组媒体一直断断续续到达，到了该上限也会强制收口发出；<b>如果已经凑满 Telegram 单组上限 10 项，会直接提前发出</b>，不用死等。</li>
            </ul>
          </div>

          <div class="guide-item">
            <strong>2. 评论区抓取：三个时间参数如何协同工作？</strong>
            <ul class="guide-list">
              <li><b>第 1 阶段 · 等待收集窗口（默认 30 秒，单条规则可覆盖）</b>：主帖相册集齐后，先休眠该时长再第一次去评论区抓取「频道马甲」发的补图/视频。适合留出作者在评论区开传第一批图的时间（搬运套图/写真类频道建议在规则里设成 <span class="code-snip">45～60</span> 秒）。</li>
              <li><b>第 2 阶段 · 静默顺延（默认 30 秒，范围 5～300）</b>：等待窗口到期去抓评论时，如果<b>最后一条作者评论距今未满该时长</b>，说明作者很可能还在继续传下一批，系统会每 5 秒轮询一次，每次抓到新评论就按最新一条重新计算静默，直到连续满「静默顺延」秒都没有新图为止。<br/>
                <i>经验值</i>：作者每传 1 批 10 张图通常需要 15～25 秒，因此静默顺延建议设 <b>25～40 秒</b>；若设得太小（如 10 秒），容易在两批图之间被提前截断。</li>
              <li><b>第 3 阶段 · 额外顺延上限（默认 180 秒，范围 0～600）</b>：在「等待收集窗口」之外，最多允许继续顺延的总时长（设 <span class="code-snip">0</span> 表示到点即发、不启用顺延）。<br/>
                <i>不会白白等满</i>：如果主帖根本没有评论、或作者早就传完，系统在窗口到期（或静默满阈值）后立刻就发，完全不会拖到上限。</li>
              <li><b>举个例子</b>：某套图主帖 10 张 + 评论区 5 批共 50 张，每批间隔 20 秒。配置为「收集窗口 60 秒 + 静默顺延 30 秒 + 额外上限 180 秒」时：第 60 秒抓到第 1 批（距今 11 秒 &lt; 30 秒）→ 自动每 5 秒轮询接住第 2～5 批 → 第 5 批结束后再静默确认 30 秒无新图 → 整套 60 张一次完整发出。</li>
            </ul>
          </div>

          <div class="guide-item">
            <strong>3. 基础转发参数速查</strong>
            <ul class="guide-list">
              <li><b>转发间隔（秒）</b>：每次向目标频道发帖后的停顿时间，建议 ≥ 1.0 秒避免触发 Telegram FloodWait 限流。</li>
              <li><b>受限频道单次下载上限（MB）</b>：仅针对开启了「禁止转发」的频道兜底下载重传生效，超出该体积的单个文件会跳过。</li>
              <li><b>消息去重保留时长（小时）</b>：已转发消息 ID 在此时长内不会重复搬运（包括自动回查补发），过期后自动清理旧记录。</li>
            </ul>
          </div>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head">
          <h2 class="panel-title">三、其他页面简略说明</h2>
        </div>
        <div class="form-grid">
          <div class="guide-item">
            <strong>Telegram 账号</strong>
            <p class="hint" style="margin:6px 0 0">支持添加多个小号，网页直接扫码或手机号验证码登录，可随时停用、退出或删除。</p>
          </div>
          <div class="guide-item">
            <strong>频道目录</strong>
            <p class="hint" style="margin:6px 0 0">切换不同账号查看其加入的频道/群组，点击复制 ID，标注是否禁止转发。</p>
          </div>
          <div class="guide-item">
            <strong>转发记录</strong>
            <p class="hint" style="margin:6px 0 0">按账号/状态/规则筛选搬运记录：无来源秒转、无下载清洗、下载重传、评论合并或广告拦截，支持单条手动重发。</p>
          </div>
          <div class="guide-item">
            <strong>全局设置 & 正则管理</strong>
            <p class="hint" style="margin:6px 0 0">调节发帖间隔、相册/评论等待与顺延时间、下载上限与免密 Token；正则管理统一维护可复用的过滤正则库，内置关键词生成与实时测试。</p>
          </div>
        </div>
      </section>`;
  }

  function buildGeneratedRegex(mode, wordsRaw, ignoreCase) {
    const items = String(wordsRaw || '')
      .split(/[\n,，]+/)
      .map((s) => s.trim())
      .filter(Boolean);
    if (!items.length) return '';
    const escRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const joined = items.map(escRe).join('|');
    const group = items.length > 1 ? `(?:${joined})` : joined;
    if (mode === 'skip' || mode === 'skip_words') return `${ignoreCase ? '(?i)' : ''}${joined}`;
    if (mode === 'strip_text' || mode === 'strip_all_on_match') return `${ignoreCase ? '(?i)' : ''}${joined}`;
    if (mode === 'strip_line') return `${ignoreCase ? '(?im)' : '(?m)'}^.*${group}.*$`;
    if (mode === 'strip_words') return `${ignoreCase ? '(?i)' : ''}${joined}`;
    if (mode === 'replace' || mode === 'replace_words') {
      return String(wordsRaw || '')
        .split('\n')
        .map((l) => l.trim())
        .filter(Boolean)
        .map((l) => {
          const i = l.indexOf('=>');
          if (i === -1) return `${escRe(l)} => `;
          return `${escRe(l.slice(0, i).trim())} => ${l.slice(i + 2).trim()}`;
        })
        .join('\n');
    }
    return joined;
  }

  function renderRegexManager() {
    const all = state.regexRules || [];
    const fk = state.regexFilterKind || '';
    const q = (state.regexSearch || '').trim().toLowerCase();
    const filtered = all.filter((rx) => {
      if (fk && rx.kind !== fk) return false;
      if (!q) return true;
      const hay = `${rx.name || ''} ${rx.note || ''} ${regexContentText(rx)}`.toLowerCase();
      return hay.includes(q);
    });
    const rt = state.regexTest;
    const selTestSet = new Set(rt.selectedIds || []);

    return `
      <div class="page-head">
        <div>
          <h1>正则管理</h1>
          <p class="page-sub">集中管理跳过、清文、去尾与替换正则库，转发规则直接勾选引用；修改后自动同步热生效</p>
        </div>
        <div class="page-actions">
          <button class="btn" data-nav="/rules">转发规则</button>
          <button class="btn primary" id="btn-new-regex">+ 新建正则</button>
        </div>
      </div>

      <section class="panel">
        <div class="panel-head" style="margin-bottom:14px">
          <h2 class="panel-title">正则库（${all.length} 条）</h2>
          <div class="filter-field" style="min-width:220px">
            <input id="rx-search" type="text" value="${esc(state.regexSearch)}" placeholder="搜索名称、备注或正则..." />
          </div>
        </div>

        <div class="rx-tabs" style="margin-bottom:16px">
          <button type="button" class="rx-tab ${!fk ? 'is-active' : ''}" data-rx-filter="">全部 (${all.length})</button>
          ${REGEX_KIND_ORDER.map((k) => {
            const cnt = all.filter((x) => x.kind === k).length;
            return `<button type="button" class="rx-tab ${fk === k ? 'is-active' : ''}" data-rx-filter="${k}">${REGEX_KIND_META[k].label} (${cnt})</button>`;
          }).join('')}
        </div>

        ${
          filtered.length
            ? `<div class="rx-grid">
                ${filtered
                  .map((rx) => {
                    const meta = REGEX_KIND_META[rx.kind] || { label: rx.kind, badge: 'muted' };
                    const usedBy = rx.used_by || [];
                    const codeText = regexContentText(rx);
                    return `
                  <article class="rx-card">
                    <div class="rx-card-head">
                      <div class="rx-card-title-wrap">
                        <div class="row" style="gap:7px">
                          <span class="badge ${meta.badge}">${meta.label}</span>
                          <div class="rx-card-title" title="${esc(rx.name)}">${esc(rx.name)}</div>
                        </div>
                        ${rx.note ? `<div class="hint">${esc(rx.note)}</div>` : ''}
                      </div>
                      <div class="actions" style="flex-shrink:0;gap:4px">
                        <button type="button" class="btn small" data-edit-regex="${esc(rx.id)}">编辑</button>
                        <button type="button" class="btn small ghost-danger" data-del-regex="${esc(rx.id)}">删除</button>
                      </div>
                    </div>
                    <div class="rx-code-box">${esc(codeText)}</div>
                    <div class="rx-card-foot">
                      <span>${usedBy.length ? `被引用：${usedBy.map((n) => `<b>${esc(n)}</b>`).join('、')}` : '暂未被转发规则引用'}</span>
                      <div class="actions" style="gap:6px">
                        <button type="button" class="link-toggle-btn" data-copy="${esc(codeText)}">复制</button>
                        <button type="button" class="link-toggle-btn" data-rx-try="${esc(rx.id)}">去测试</button>
                      </div>
                    </div>
                  </article>`;
                  })
                  .join('')}
              </div>`
            : `<div class="empty">${all.length ? '没有匹配筛选条件的正则规则' : '正则库还是空的，点击右上角「+ 新建正则」或从下方模板一键添加吧～'}</div>`
        }
      </section>

      <section class="panel">
        <div class="panel-head">
          <h2 class="panel-title">常用现成模板（一键加入正则库）</h2>
          <span class="hint">添加后可随时编辑关键词或直接被转发规则引用</span>
        </div>
        <div class="tpl-grid">
          ${REGEX_TEMPLATES.map((t, i) => {
            const meta = REGEX_KIND_META[t.kind] || { label: t.kind, badge: 'muted' };
            return `
            <div class="tpl-card">
              <div class="tpl-card-head">
                <strong title="${esc(t.name)}">${esc(t.name)}</strong>
                <span class="badge ${meta.badge}">${meta.label}</span>
              </div>
              <div class="hint">${esc(t.desc)}</div>
              <div class="code-snip" style="width:100%;word-break:break-all;padding:5px 8px">${esc(t.patterns.join('\n'))}</div>
              <div class="actions">
                <button type="button" class="btn small primary" data-tpl-add="${i}">+ 加入正则库</button>
                <button type="button" class="btn small" data-copy="${esc(t.patterns.join('\n'))}">复制</button>
              </div>
            </div>`;
          }).join('')}
        </div>
      </section>

      <section class="panel" id="rx-test-section">
        <div class="panel-head">
          <h2 class="panel-title">在线测试正则库清洗效果</h2>
          <button type="button" class="btn primary small" id="btn-rg-test">运行测试</button>
        </div>
        <div class="stack" style="gap:14px">
          <div class="field">
            <label>勾选参与测试的正则（已选 ${selTestSet.size} 条）</label>
            ${
              all.length
                ? `<div class="rx-test-pills">
                    ${all
                      .map((rx) => {
                        const checked = selTestSet.has(rx.id);
                        const meta = REGEX_KIND_META[rx.kind] || { short: rx.kind };
                        return `
                          <label class="rx-test-pill ${checked ? 'is-selected' : ''}">
                            <input type="checkbox" data-rx-test-toggle="${esc(rx.id)}" ${checked ? 'checked' : ''} />
                            <span>[${meta.short}] ${esc(rx.name)}</span>
                          </label>`;
                      })
                      .join('')}
                  </div>`
                : '<span class="hint">请先在上方添加至少一条正则</span>'
            }
          </div>
          <div class="field">
            <label>测试消息原文</label>
            <textarea id="rg-sample" style="min-height:84px">${esc(rt.sample)}</textarea>
          </div>
          ${
            rt.result
              ? `<div class="test-result">${
                  rt.result.skipped
                    ? `<span class="badge warning">已拦截跳过</span> 命中规则：<code>${esc(rt.result.skip_reason)}</code>`
                    : rt.result.cleared_all_text
                    ? `<span class="badge warning">已清空全部文本</span> 命中：<code>${esc(rt.result.clear_reason)}</code>（将仅发送媒体）`
                    : `<div><span class="badge ${rt.result.changed ? 'info' : 'muted'}">${rt.result.changed ? '已清洗' : '无变化'}</span></div><div style="margin-top:6px">${esc(rt.result.cleaned || '(空)')}</div>`
                }</div>`
              : ''
          }
        </div>
      </section>

      <section class="panel">
        <div class="panel-head"><h2 class="panel-title">30 秒看懂常用正则符号</h2></div>
        <div class="form-grid">
          <div class="guide-item"><span class="code-snip">|</span> <span class="hint" style="margin-left:6px">“或者”的意思，例如 <span class="code-snip">广告|推广</span></span></div>
          <div class="guide-item"><span class="code-snip">(?i)</span> <span class="hint" style="margin-left:6px">放在开头表示忽略英文大小写</span></div>
          <div class="guide-item"><span class="code-snip">(?m)^.*词.*$</span> <span class="hint" style="margin-left:6px">匹配包含“词”的<b>一整行</b></span></div>
          <div class="guide-item"><span class="code-snip">\\.</span> <span class="hint" style="margin-left:6px">匹配真实的小数点，如 <span class="code-snip">t\\.me</span></span></div>
        </div>
      </section>`;
  }

  function renderRegexModal() {
    if (!state.modalRegex) return '';
    const { id, data, genOpen, genStripMode, genWords, genIgnoreCase } = state.modalRegex;
    const isNew = !id;
    const kind = data.kind || 'skip';
    const meta = REGEX_KIND_META[kind] || REGEX_KIND_META.skip;
    const contentPlaceholder =
      kind === 'replace'
        ? '@old_channel => @my_channel\n旧广告词 => 新文字'
        : kind === 'strip'
        ? '(?im)^.*(?:关注频道|备用频道|投稿联系).*$'
        : '(?i)担保|广告|商务|互推|推广';

    return `
      <div class="modal-overlay" id="regex-modal-overlay">
        <div class="modal" style="max-width:680px">
          <div class="modal-head">
            <div class="modal-title-wrap">
              <h3 class="modal-title">${isNew ? '新建正则规则' : `编辑正则 · ${esc(data.name || '')}`}</h3>
              <span class="modal-sub">${esc(meta.desc)}</span>
            </div>
            <button type="button" class="btn ghost icon small" id="btn-close-regex-modal" title="关闭 (Esc)">${icon('close', 18)}</button>
          </div>

          <form id="regex-modal-form" class="modal-form">
            <div class="modal-body plain">
              <div class="form-grid">
                <div class="field">
                  <label>正则名称</label>
                  <input id="rxm-name" type="text" value="${esc(data.name || '')}" placeholder="例如：通用广告拦截 / 去除频道尾巴" required />
                </div>
                <div class="field">
                  <label>作用类型</label>
                  <select id="rxm-kind">
                    ${REGEX_KIND_ORDER.map(
                      (k) => `<option value="${k}" ${kind === k ? 'selected' : ''}>${REGEX_KIND_META[k].label}（${REGEX_KIND_META[k].short}）</option>`
                    ).join('')}
                  </select>
                </div>
              </div>

              <div class="collapsible-box">
                <button type="button" class="collapsible-head" id="btn-toggle-rxm-gen">
                  <span>✨ 不会写正则？用关键词一键生成</span>
                  <span class="hint">${genOpen ? '收起 ▲' : '展开 ▼'}</span>
                </button>
                ${
                  genOpen
                    ? `<div class="collapsible-body">
                         ${
                           kind === 'strip'
                             ? `<div class="row" style="gap:12px">
                                  <label class="checkbox"><input type="radio" name="rxm-strip-mode" value="strip_line" ${genStripMode !== 'strip_words' ? 'checked' : ''} /> <span>删掉含关键词的整行</span></label>
                                  <label class="checkbox"><input type="radio" name="rxm-strip-mode" value="strip_words" ${genStripMode === 'strip_words' ? 'checked' : ''} /> <span>只删关键词本身</span></label>
                                </div>`
                             : ''
                         }
                         <div class="field">
                           <label>${kind === 'replace' ? '输入替换对（每行一条：旧词 => 新词）' : '输入关键词（每行一个，或用逗号隔开）'}</label>
                           <textarea id="rxm-gen-words" style="min-height:68px" placeholder="${kind === 'replace' ? '@原频道 => @我的频道' : '担保\n广告\n商务合作'}">${esc(genWords || '')}</textarea>
                         </div>
                         <div class="row" style="justify-content:space-between">
                           <label class="checkbox"><input id="rxm-gen-icase" type="checkbox" ${genIgnoreCase !== false ? 'checked' : ''} /> <span>忽略英文大小写</span></label>
                           <div class="actions">
                             <button type="button" class="btn small" id="btn-rxm-gen-append">追加到下方</button>
                             <button type="button" class="btn small primary" id="btn-rxm-gen-replace">生成并填入下方</button>
                           </div>
                         </div>
                       </div>`
                    : ''
                }
              </div>

              <div class="field">
                <label>${kind === 'replace' ? '替换规则（每行一条：查找正则 => 替换为）' : '正则表达式（每行一条，自动开启多行匹配）'}</label>
                <textarea id="rxm-content" style="min-height:110px" placeholder="${esc(contentPlaceholder)}" required>${esc(data.content || '')}</textarea>
                <span class="hint">${kind === 'replace' ? '格式示例：@old_channel => @my_channel（留空右侧表示替换为空）' : '保存时会自动校验正则语法，修改后会自动同步到所有引用它的转发规则'}</span>
              </div>

              <div class="field">
                <label>备注说明（可选）</label>
                <input id="rxm-note" type="text" value="${esc(data.note || '')}" placeholder="例如：适用于写真频道拦截博彩与商务推广" />
              </div>
            </div>

            <div class="modal-actions">
              <div class="modal-actions-right">
                <button type="button" class="btn ghost" id="btn-cancel-regex-modal">取消</button>
                <button type="submit" class="btn primary">${isNew ? '创建正则' : '保存修改'}</button>
              </div>
            </div>
          </form>
        </div>
      </div>`;
  }

  function renderPasswordModal() {
    if (!state.modalPassword) return '';
    return `
      <div class="modal-overlay" id="pwd-modal-overlay">
        <div class="modal" style="max-width:460px">
          <div class="modal-head">
            <h3 class="modal-title">修改管理员密码</h3>
            <button type="button" class="btn ghost icon small" id="btn-close-pwd">${icon('close', 17)}</button>
          </div>
          <form id="pwd-form" class="modal-form">
            <div class="modal-body plain">
              <div class="field"><label>当前密码</label><input id="pwd-cur" type="password" required /></div>
              <div class="field"><label>新密码（至少 8 位）</label><input id="pwd-new" type="password" minlength="8" required /></div>
              <div class="field"><label>确认新密码</label><input id="pwd-confirm" type="password" minlength="8" required /></div>
            </div>
            <div class="modal-actions">
              <div class="modal-actions-right">
                <button type="button" class="btn ghost" id="btn-cancel-pwd">取消</button>
                <button type="submit" class="btn primary">保存新密码</button>
              </div>
            </div>
          </form>
        </div>
      </div>`;
  }

  function renderApp() {
    const navGroups = [
      { label: '工作台', items: [['/', '概览', 'dashboard'], ['/rules', '转发规则', 'rules'], ['/chats', '频道目录', 'chats']] },
      { label: '连接与记录', items: [['/telegram', 'Telegram 账号', 'telegram'], ['/logs', '转发记录', 'logs']] },
      { label: '系统与帮助', items: [['/settings', '全局设置', 'settings'], ['/log-manage', '日志管理', 'logManage'], ['/regex', '正则管理', 'regex'], ['/guide', '使用说明', 'guide']] },
    ];
    const authCnt = (state.accounts || []).filter((a) => a.authorized).length;
    const totalCnt = (state.accounts || []).length;

    let bodyHtml = '';
    if (state.route === '/') bodyHtml = renderOverview();
    else if (state.route === '/regex') bodyHtml = renderRegexManager();
    else if (state.route === '/guide') bodyHtml = renderGuide();
    else if (state.route === '/logs') bodyHtml = renderLogs();
    else if (state.route === '/log-manage') bodyHtml = renderLogManage();
    else if (state.route === '/rules') bodyHtml = renderRules();
    else if (state.route === '/chats') bodyHtml = renderChats();
    else if (state.route === '/telegram') bodyHtml = renderTelegram();
    else if (state.route === '/settings') bodyHtml = renderSettings();
    else bodyHtml = renderOverview();

    return `
    <div class="app-shell">
      <div class="sidebar-backdrop ${state.sidebarOpen ? 'open' : ''}" id="sidebar-backdrop"></div>
      <div class="layout">
        <aside class="sidebar ${state.sidebarOpen ? 'open' : ''}">
          <div class="brand">
            <span class="brand-mark">${icon('brand', 20)}</span>
            <div class="brand-copy">
              <strong>TG-Forwarder</strong>
              <span>频道无痕搬运</span>
            </div>
            <button class="sidebar-close" type="button" id="btn-close-sidebar">${icon('close', 18)}</button>
          </div>
          <nav class="nav">
            ${navGroups
              .map(
                (g) => `
              <div class="nav-group">
                <div class="nav-label">${g.label}</div>
                ${g.items
                  .map(
                    ([p, lbl, ic]) =>
                      `<a href="#${p}" data-nav="${p}" class="${state.route === p ? 'is-active' : ''}">${icon(ic, 17)}<span>${lbl}</span></a>`
                  )
                  .join('')}
              </div>`
              )
              .join('')}
          </nav>
          <div class="sidebar-foot">
            <div class="sidebar-status">
              <span class="live-dot ${authCnt > 0 ? 'running' : 'warning'}"></span>
              <span>${totalCnt ? (authCnt > 0 ? `已连接 ${authCnt} / ${totalCnt} 个账号` : '等待登录小号') : '加载中...'}</span>
            </div>
            <div class="theme-control">
              <span class="theme-control-label">外观</span>
              ${themeSwitchHtml()}
            </div>
            <div class="sidebar-account">
              <button class="sidebar-action" type="button" id="btn-open-pwd">${icon('key', 14)} 修改密码</button>
              <button class="sidebar-action" type="button" id="btn-logout">${icon('logout', 14)} 退出</button>
            </div>
          </div>
        </aside>

        <div class="main">
          <header class="topbar">
            <button class="btn ghost icon" type="button" id="btn-open-sidebar">${icon('menu', 19)}</button>
            <span style="font-weight:500">TG-Forwarder</span>
          </header>
          <main class="content">${bodyHtml}</main>
        </div>
      </div>
      ${renderRuleModal()}
      ${renderRegexModal()}
      ${renderAddAccountModal()}
      ${renderPasswordModal()}
    </div>`;
  }

  function syncRuleModalFromDom() {
    if (!state.modalRule) return;
    const d = state.modalRule.data;
    const g = (id) => document.getElementById(id);
    if (!g('rm-name')) return;
    d.name = g('rm-name').value;
    d.account_id = g('rm-account').value;
    d.enabled = g('rm-enabled').checked;
    d.drop_author = g('rm-drop-author').checked;
    d.fallback_reupload = g('rm-fallback').checked;
    d.skip_text_only = g('rm-skip-text-only').checked;
    d.strip_all_text = g('rm-strip-all-text').checked;
    d.skip_empty_text_only = g('rm-skip-empty').checked;
    d.disable_link_preview = g('rm-no-preview').checked;
    d.collect_comments = g('rm-collect-comments').checked;
    if (g('rm-comment-wait')) {
      const wv = g('rm-comment-wait').value.trim();
      d.comment_wait_seconds = wv ? Math.max(5, Math.min(300, Number(wv))) : null;
    }
    if (g('rm-comment-mode')) d.comment_merge_mode = g('rm-comment-mode').value || '';
    if (g('rm-test-sample')) state.testInput = g('rm-test-sample').value;
  }

  function syncRegexModalFromDom() {
    if (!state.modalRegex) return;
    const m = state.modalRegex;
    const g = (id) => document.getElementById(id);
    if (g('rxm-name')) m.data.name = g('rxm-name').value;
    if (g('rxm-kind')) m.data.kind = g('rxm-kind').value;
    if (g('rxm-content')) m.data.content = g('rxm-content').value;
    if (g('rxm-note')) m.data.note = g('rxm-note').value;
    if (g('rxm-gen-words')) m.genWords = g('rxm-gen-words').value;
    if (g('rxm-gen-icase')) m.genIgnoreCase = g('rxm-gen-icase').checked;
    const stripRadio = document.querySelector('input[name="rxm-strip-mode"]:checked');
    if (stripRadio) m.genStripMode = stripRadio.value;
  }

  function openRegexModal(rx = null, prefill = null) {
    if (rx) {
      state.modalRegex = {
        id: rx.id,
        data: {
          name: rx.name || '',
          kind: rx.kind || 'skip',
          content: regexContentText(rx),
          note: rx.note || '',
        },
        genOpen: false,
        genStripMode: 'strip_line',
        genWords: '',
        genIgnoreCase: true,
      };
    } else {
      state.modalRegex = {
        id: null,
        data: {
          name: prefill?.name || '',
          kind: prefill?.kind || 'skip',
          content: prefill?.content || '',
          note: prefill?.note || '',
        },
        genOpen: !prefill?.content,
        genStripMode: 'strip_line',
        genWords: '',
        genIgnoreCase: true,
      };
    }
    render();
  }

  function parseRegexModalPayload(m) {
    const kind = m.data.kind || 'skip';
    const lines = String(m.data.content || '')
      .split('\n')
      .map((s) => s.trim())
      .filter(Boolean);
    const payload = {
      name: String(m.data.name || '').trim(),
      kind,
      note: String(m.data.note || '').trim(),
    };
    if (kind === 'replace') {
      payload.replace_patterns = lines.map((l) => {
        const i = l.indexOf('=>');
        if (i === -1) return { pattern: l, repl: '' };
        return { pattern: l.slice(0, i).trim(), repl: l.slice(i + 2).trim() };
      });
    } else {
      payload.patterns = lines;
    }
    return payload;
  }

  async function loadModalChatsForAccount(acctId) {
    if (!state.modalRule) return;
    try {
      const d = await api('/api/chats?account_id=' + encodeURIComponent(acctId));
      if (state.modalRule && state.modalRule.data.account_id === acctId) {
        syncRuleModalFromDom();
        state.modalRule.chats = d.authorized ? d.items || [] : [];
        render();
      }
    } catch {
      if (state.modalRule) {
        syncRuleModalFromDom();
        state.modalRule.chats = [];
        render();
      }
    }
  }

  function bindEvents() {
    document.querySelectorAll('[data-theme-set]').forEach((el) => {
      el.onclick = () => applyTheme(el.dataset.themeSet);
    });
    document.querySelectorAll('[data-nav]').forEach((el) => {
      el.onclick = (e) => {
        e.preventDefault();
        navigate(el.dataset.nav);
      };
    });
    document.querySelectorAll('[data-copy]').forEach((el) => {
      el.onclick = () => copyText(el.dataset.copy);
    });

    const loginForm = document.getElementById('login-form');
    if (loginForm) {
      loginForm.onsubmit = async (e) => {
        e.preventDefault();
        try {
          await api('/api/auth/login', {
            method: 'POST',
            body: JSON.stringify({ password: document.getElementById('login-pwd').value }),
          });
          state.authed = true;
          await loadRouteData(state.route);
          render();
        } catch (err) {
          toast(err.message, 'danger');
        }
      };
      return;
    }

    const openSb = document.getElementById('btn-open-sidebar');
    if (openSb) openSb.onclick = () => { state.sidebarOpen = true; render(); };
    const closeSb = document.getElementById('btn-close-sidebar');
    if (closeSb) closeSb.onclick = () => { state.sidebarOpen = false; render(); };
    const bd = document.getElementById('sidebar-backdrop');
    if (bd) bd.onclick = () => { state.sidebarOpen = false; render(); };

    const btnLogout = document.getElementById('btn-logout');
    if (btnLogout) {
      btnLogout.onclick = async () => {
        try {
          await api('/api/auth/logout', { method: 'POST' });
          state.authed = false;
          render();
        } catch (err) {
          toast(err.message, 'danger');
        }
      };
    }

    const btnOpenPwd = document.getElementById('btn-open-pwd');
    if (btnOpenPwd) btnOpenPwd.onclick = () => { state.modalPassword = true; render(); };
    const closePwd = document.getElementById('btn-close-pwd');
    if (closePwd) closePwd.onclick = () => { state.modalPassword = false; render(); };
    const cancelPwd = document.getElementById('btn-cancel-pwd');
    if (cancelPwd) cancelPwd.onclick = () => { state.modalPassword = false; render(); };
    const pwdForm = document.getElementById('pwd-form');
    if (pwdForm) {
      pwdForm.onsubmit = async (e) => {
        e.preventDefault();
        const cur = document.getElementById('pwd-cur').value;
        const np = document.getElementById('pwd-new').value;
        const cf = document.getElementById('pwd-confirm').value;
        if (np !== cf) return toast('两次输入的新密码不一致', 'danger');
        try {
          await api('/api/auth/password', {
            method: 'POST',
            body: JSON.stringify({ current_password: cur, new_password: np }),
          });
          state.modalPassword = false;
          toast('管理员密码已更新', 'success');
          render();
        } catch (err) {
          toast(err.message, 'danger');
        }
      };
    }

    const btnRefreshOv = document.getElementById('btn-refresh-overview');
    if (btnRefreshOv) btnRefreshOv.onclick = () => loadRouteData('/');
    const btnRefreshLogs = document.getElementById('btn-refresh-logs');
    if (btnRefreshLogs) btnRefreshLogs.onclick = () => loadRouteData('/logs');
    document.querySelectorAll('[data-resend-log]').forEach((b) => {
      b.onclick = async () => {
        const id = b.dataset.resendLog;
        if (state.resendingIds[id]) return;
        state.resendingIds[id] = true;
        b.disabled = true;
        b.textContent = '重发中...';
        try {
          await api(`/api/logs/${encodeURIComponent(id)}/resend`, { method: 'POST' });
          delete state.resendingIds[id];
          toast('已重新发送到目标频道', 'success');
          await loadRouteData('/logs');
        } catch (err) {
          delete state.resendingIds[id];
          toast(err.message, 'danger');
          render();
        }
      };
    });

    const logAcct = document.getElementById('log-filter-account');
    if (logAcct) logAcct.onchange = () => { state.logs.account_id = logAcct.value; state.logs.page = 1; loadRouteData('/logs'); };
    const logSt = document.getElementById('log-filter-status');
    if (logSt) logSt.onchange = () => { state.logs.status = logSt.value; state.logs.page = 1; loadRouteData('/logs'); };
    const logRl = document.getElementById('log-filter-rule');
    if (logRl) logRl.onchange = () => { state.logs.rule = logRl.value; state.logs.page = 1; loadRouteData('/logs'); };
    document.querySelectorAll('[data-log-page]').forEach((b) => {
      b.onclick = () => {
        const p = Number(b.dataset.logPage);
        if (!Number.isFinite(p) || p < 1 || p === state.logs.page || p > state.logs.totalPages) return;
        state.logs.page = p;
        loadRouteData('/logs');
      };
    });

    const chatsAcct = document.getElementById('chats-acct-select');
    if (chatsAcct) {
      chatsAcct.onchange = () => {
        state.chatsAccountId = chatsAcct.value;
        loadRouteData('/chats');
      };
    }
    const chatsSearch = document.getElementById('chats-search');
    if (chatsSearch) {
      chatsSearch.oninput = () => {
        const pos = chatsSearch.selectionStart;
        state.chatsQuery = chatsSearch.value;
        render();
        const el = document.getElementById('chats-search');
        if (el) { el.focus(); el.setSelectionRange(pos, pos); }
      };
    }
    const btnRefChats = document.getElementById('btn-refresh-chats');
    if (btnRefChats) {
      btnRefChats.onclick = async () => {
        try {
          const d = await api('/api/chats?account_id=' + encodeURIComponent(state.chatsAccountId) + '&refresh=1');
          state.chatsAuthorized = d.authorized;
          state.chats = d.items || [];
          toast('频道目录已刷新', 'success');
          render();
        } catch (err) {
          toast(err.message, 'danger');
        }
      };
    }

    // Rules page
    const rulesFilter = document.getElementById('rules-acct-filter');
    if (rulesFilter) rulesFilter.onchange = () => { state.ruleFilterAccount = rulesFilter.value; render(); };

    document.querySelectorAll('[data-toggle-sources]').forEach((b) => {
      b.onclick = () => {
        const idx = b.dataset.toggleSources;
        state.expandedRuleSources[idx] = !state.expandedRuleSources[idx];
        render();
      };
    });

    document.querySelectorAll('[data-jump-regex]').forEach((b) => {
      b.onclick = () => {
        const rx = getRegexById(b.dataset.jumpRegex);
        navigate('/regex');
        if (rx) {
          setTimeout(() => {
            const latest = getRegexById(rx.id) || rx;
            openRegexModal(latest);
          }, 50);
        }
      };
    });

    const openRuleModal = async (idx, data) => {
      state.testOutput = null;
      state.modalRuleTestOpen = false;
      state.modalRuleRxFilter = '';
      state.modalPickerQuery = '';
      const cloned = JSON.parse(JSON.stringify(data));
      if (!cloned.account_id) cloned.account_id = state.accounts[0]?.id || 'default';
      if (!Array.isArray(cloned.regex_ids)) cloned.regex_ids = [];
      state.modalRule = { idx, data: cloned, chats: [] };
      render();
      loadModalChatsForAccount(cloned.account_id);
    };
    const btnNewRule = document.getElementById('btn-new-rule');
    if (btnNewRule) {
      btnNewRule.onclick = () =>
        openRuleModal(null, {
          name: '',
          enabled: true,
          account_id: state.ruleFilterAccount || state.accounts[0]?.id || 'default',
          sources: [],
          targets: [],
          drop_author: true,
          fallback_reupload: true,
          skip_text_only: false,
          strip_all_text: false,
          skip_empty_text_only: true,
          disable_link_preview: false,
          collect_comments: false,
          comment_wait_seconds: null,
          comment_merge_mode: '',
          regex_ids: [],
        });
    }
    document.querySelectorAll('[data-edit-rule]').forEach((el) => {
      el.onclick = () => {
        const idx = Number(el.dataset.editRule);
        openRuleModal(idx, state.rules[idx]);
      };
    });
    document.querySelectorAll('[data-toggle-rule]').forEach((el) => {
      el.onclick = async () => {
        const idx = Number(el.dataset.toggleRule);
        try {
          await api(`/api/rules/${encodeURIComponent(state.rules[idx].id)}/toggle`, { method: 'POST' });
          await loadRouteData('/rules');
          toast('规则状态已切换', 'success');
        } catch (err) {
          toast(err.message, 'danger');
        }
      };
    });
    document.querySelectorAll('[data-del-rule]').forEach((el) => {
      el.onclick = async () => {
        const idx = Number(el.dataset.delRule);
        if (!confirm(`确定要删除规则「${state.rules[idx]?.name}」吗？`)) return;
        try {
          await api(`/api/rules/${encodeURIComponent(state.rules[idx].id)}`, { method: 'DELETE' });
          await loadRouteData('/rules');
          toast('规则已删除', 'success');
        } catch (err) {
          toast(err.message, 'danger');
        }
      };
    });

    // Rule modal controls
    if (state.modalRule) {
      const closeModal = () => { state.modalRule = null; render(); };
      document.getElementById('btn-close-rule-modal').onclick = closeModal;
      document.getElementById('btn-cancel-rule-modal').onclick = closeModal;

      document.getElementById('rm-account').onchange = (e) => {
        syncRuleModalFromDom();
        loadModalChatsForAccount(e.target.value);
      };

      const addSrc = () => {
        syncRuleModalFromDom();
        const v = document.getElementById('rm-add-src-input').value.trim();
        if (v && !state.modalRule.data.sources.map(String).includes(v)) {
          state.modalRule.data.sources.push(/^-?\d+$/.test(v) ? Number(v) : v);
        }
        render();
      };
      const addTgt = () => {
        syncRuleModalFromDom();
        const v = document.getElementById('rm-add-tgt-input').value.trim();
        if (v && !state.modalRule.data.targets.map(String).includes(v)) {
          state.modalRule.data.targets.push(/^-?\d+$/.test(v) ? Number(v) : v);
        }
        render();
      };
      document.getElementById('btn-rm-add-src').onclick = addSrc;
      document.getElementById('btn-rm-add-tgt').onclick = addTgt;
      document.getElementById('rm-add-src-input').onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); addSrc(); } };
      document.getElementById('rm-add-tgt-input').onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); addTgt(); } };
      document.querySelectorAll('[data-rm-del-src]').forEach((b) => {
        b.onclick = () => { syncRuleModalFromDom(); state.modalRule.data.sources.splice(Number(b.dataset.rmDelSrc), 1); render(); };
      });
      document.querySelectorAll('[data-rm-del-tgt]').forEach((b) => {
        b.onclick = () => { syncRuleModalFromDom(); state.modalRule.data.targets.splice(Number(b.dataset.rmDelTgt), 1); render(); };
      });

      ['rm-drop-author', 'rm-fallback', 'rm-skip-text-only', 'rm-strip-all-text', 'rm-skip-empty', 'rm-no-preview', 'rm-collect-comments'].forEach((id) => {
        const el = document.getElementById(id);
        if (el) {
          el.onchange = () => {
            syncRuleModalFromDom();
            render();
          };
        }
      });

      const gotoRegex = () => {
        state.modalRule = null;
        navigate('/regex');
      };
      const btnGotoRx = document.getElementById('btn-rm-goto-regex');
      if (btnGotoRx) btnGotoRx.onclick = gotoRegex;
      const btnGotoRxEmpty = document.getElementById('btn-rm-goto-regex-empty');
      if (btnGotoRxEmpty) btnGotoRxEmpty.onclick = gotoRegex;

      document.querySelectorAll('[data-rm-rx-filter]').forEach((b) => {
        b.onclick = () => {
          syncRuleModalFromDom();
          state.modalRuleRxFilter = b.dataset.rmRxFilter || '';
          render();
        };
      });

      document.querySelectorAll('[data-rm-toggle-rx]').forEach((cb) => {
        cb.onchange = () => {
          syncRuleModalFromDom();
          const rid = cb.dataset.rmToggleRx;
          const cur = state.modalRule.data.regex_ids || [];
          const i = cur.indexOf(rid);
          if (cb.checked && i === -1) cur.push(rid);
          else if (!cb.checked && i >= 0) cur.splice(i, 1);
          state.modalRule.data.regex_ids = cur;
          render();
        };
      });

      const btnToggleTest = document.getElementById('btn-toggle-rm-test');
      if (btnToggleTest) {
        btnToggleTest.onclick = () => {
          syncRuleModalFromDom();
          state.modalRuleTestOpen = !state.modalRuleTestOpen;
          render();
        };
      }

      const bindPickerButtons = () => {
        document.querySelectorAll('[data-pick-src]').forEach((b) => {
          b.onclick = () => {
            syncRuleModalFromDom();
            const idStr = String(b.dataset.pickSrc);
            const chat = (state.modalRule.chats || []).find((c) => String(c.id) === idStr);
            const uname = chat && chat.username ? '@' + chat.username.toLowerCase() : '';
            const cur = state.modalRule.data.sources || [];
            const idx = cur.findIndex((x) => {
              const s = String(x).trim();
              return s === idStr || (uname && s.toLowerCase() === uname);
            });
            if (idx >= 0) cur.splice(idx, 1);
            else cur.push(Number(idStr));
            render();
          };
        });
        document.querySelectorAll('[data-pick-tgt]').forEach((b) => {
          b.onclick = () => {
            syncRuleModalFromDom();
            const idStr = String(b.dataset.pickTgt);
            const chat = (state.modalRule.chats || []).find((c) => String(c.id) === idStr);
            const uname = chat && chat.username ? '@' + chat.username.toLowerCase() : '';
            const cur = state.modalRule.data.targets || [];
            const idx = cur.findIndex((x) => {
              const s = String(x).trim();
              return s === idStr || (uname && s.toLowerCase() === uname);
            });
            if (idx >= 0) cur.splice(idx, 1);
            else cur.push(Number(idStr));
            render();
          };
        });
      };
      bindPickerButtons();
      const chatFilterInput = document.getElementById('rm-chat-filter');
      if (chatFilterInput) {
        chatFilterInput.oninput = () => {
          state.modalPickerQuery = chatFilterInput.value;
          const box = document.getElementById('rm-picker-box');
          if (box && state.modalRule) {
            box.innerHTML = renderPickerItemsHtml(state.modalRule.chats || [], state.modalRule.data, state.modalPickerQuery, state.modalRule.idx);
            bindPickerButtons();
          }
        };
      }

      const btnTestRegex = document.getElementById('btn-test-regex');
      if (btnTestRegex) {
        btnTestRegex.onclick = async () => {
          syncRuleModalFromDom();
          const d = state.modalRule.data;
          try {
            state.testOutput = await api('/api/rules/test', {
              method: 'POST',
              body: JSON.stringify({
                text: state.testInput,
                strip_all_text: d.strip_all_text,
                regex_ids: d.regex_ids || [],
              }),
            });
            render();
          } catch (err) {
            toast(err.message, 'danger');
          }
        };
      }

      document.getElementById('btn-save-rule').onclick = async () => {
        syncRuleModalFromDom();
        const { idx, data } = state.modalRule;
        try {
          if (idx === null) {
            await api('/api/rules', { method: 'POST', body: JSON.stringify(data) });
          } else {
            await api(`/api/rules/${encodeURIComponent(data.id)}`, { method: 'PUT', body: JSON.stringify(data) });
          }
          state.modalRule = null;
          await loadRouteData('/rules');
          toast('规则已保存并热加载生效', 'success');
        } catch (err) {
          toast(err.message, 'danger');
        }
      };
    }

    // Multi-account actions
    const btnAddAcct = document.getElementById('btn-add-account');
    if (btnAddAcct) btnAddAcct.onclick = () => { state.modalAddAccount = true; render(); };
    if (state.modalAddAccount) {
      document.getElementById('btn-close-add-acct').onclick = () => { state.modalAddAccount = false; render(); };
      document.getElementById('btn-cancel-add-acct').onclick = () => { state.modalAddAccount = false; render(); };
      document.getElementById('add-acct-form').onsubmit = async (e) => {
        e.preventDefault();
        try {
          const name = document.getElementById('add-acct-name').value.trim();
          const res = await api('/api/accounts', { method: 'POST', body: JSON.stringify({ name }) });
          state.modalAddAccount = false;
          state.accounts = res.items || [];
          state.loginAccountId = res.id;
          state.qrDataUrl = '';
          state.phoneStep = 'input';
          toast('账号已添加，请扫码或手机登录', 'success');
          render();
        } catch (err) {
          toast(err.message, 'danger');
        }
      };
    }

    document.querySelectorAll('[data-acct-login]').forEach((el) => {
      el.onclick = () => {
        stopQrPoll();
        state.loginAccountId = el.dataset.acctLogin;
        state.qrDataUrl = '';
        state.phoneStep = 'input';
        state.phoneNum = '';
        state.phoneCode = '';
        render();
      };
    });
    document.querySelectorAll('[data-acct-chats]').forEach((el) => {
      el.onclick = () => {
        state.chatsAccountId = el.dataset.acctChats;
        navigate('/chats');
      };
    });
    document.querySelectorAll('[data-acct-rename]').forEach((el) => {
      el.onclick = async () => {
        const id = el.dataset.acctRename;
        const cur = state.accounts.find((a) => a.id === id)?.name || '';
        const next = prompt('请输入新的账号名称：', cur);
        if (!next || !next.trim()) return;
        try {
          const r = await api(`/api/accounts/${encodeURIComponent(id)}`, {
            method: 'PUT',
            body: JSON.stringify({ name: next.trim() }),
          });
          state.accounts = r.items || [];
          toast('已重命名', 'success');
          render();
        } catch (err) {
          toast(err.message, 'danger');
        }
      };
    });
    document.querySelectorAll('[data-acct-toggle]').forEach((el) => {
      el.onclick = async () => {
        const id = el.dataset.acctToggle;
        const enabled = el.dataset.acctEnabled !== '1';
        try {
          const r = await api(`/api/accounts/${encodeURIComponent(id)}`, {
            method: 'PUT',
            body: JSON.stringify({ enabled }),
          });
          state.accounts = r.items || [];
          toast(enabled ? '账号已启用' : '账号已停用', 'success');
          render();
        } catch (err) {
          toast(err.message, 'danger');
        }
      };
    });
    document.querySelectorAll('[data-acct-logout]').forEach((el) => {
      el.onclick = async () => {
        const id = el.dataset.acctLogout;
        if (!confirm('确定要退出该账号的 Telegram 会话吗？')) return;
        try {
          await api('/api/tg/logout', { method: 'POST', body: JSON.stringify({ account_id: id }) });
          toast('已退出登录', 'success');
          await loadRouteData('/telegram');
        } catch (err) {
          toast(err.message, 'danger');
        }
      };
    });
    document.querySelectorAll('[data-acct-del]').forEach((el) => {
      el.onclick = async () => {
        const id = el.dataset.acctDel;
        const nm = accountName(id);
        if (!confirm(`确定要删除账号「${nm}」及其会话文件吗？`)) return;
        try {
          const r = await api(`/api/accounts/${encodeURIComponent(id)}`, { method: 'DELETE' });
          state.accounts = r.items || [];
          if (state.loginAccountId === id) state.loginAccountId = '';
          toast('账号已删除', 'success');
          render();
        } catch (err) {
          toast(err.message, 'danger');
        }
      };
    });

    const btnCloseLogin = document.getElementById('btn-close-login-panel');
    if (btnCloseLogin) btnCloseLogin.onclick = () => { stopQrPoll(); state.loginAccountId = ''; render(); };

    // Telegram login controls for state.loginAccountId
    const btnQrStart = document.getElementById('btn-qr-start');
    if (btnQrStart && state.loginAccountId) {
      btnQrStart.onclick = async () => {
        const acctId = state.loginAccountId;
        try {
          const res = await api('/api/tg/qr/start', { method: 'POST', body: JSON.stringify({ account_id: acctId }) });
          state.qrDataUrl = res.qr_data_url || '';
          const acct = state.accounts.find((a) => a.id === acctId);
          if (acct) acct.qr = { status: res.status, error: res.error || '' };
          stopQrPoll();
          if (res.status === 'waiting') {
            state.qrPollTimer = setInterval(async () => {
              try {
                const p = await api('/api/tg/qr/poll?account_id=' + encodeURIComponent(acctId));
                if (p.status !== 'waiting') {
                  stopQrPoll();
                  if (p.status === 'authorized') {
                    toast('Telegram 账号登录成功！', 'success');
                    state.loginAccountId = '';
                    await loadRouteData('/telegram');
                  } else {
                    const a2 = state.accounts.find((a) => a.id === acctId);
                    if (a2) a2.qr = p;
                    render();
                  }
                }
              } catch {}
            }, 2000);
          }
          render();
        } catch (err) {
          toast(err.message, 'danger');
        }
      };
    }

    const btnQr2fa = document.getElementById('btn-qr-2fa');
    if (btnQr2fa && state.loginAccountId) {
      btnQr2fa.onclick = async () => {
        const acctId = state.loginAccountId;
        try {
          await api('/api/tg/qr/2fa', {
            method: 'POST',
            body: JSON.stringify({ account_id: acctId, password: document.getElementById('qr-2fa-pwd').value }),
          });
          toast('两步验证通过，登录成功！', 'success');
          state.loginAccountId = '';
          await loadRouteData('/telegram');
        } catch (err) {
          toast(err.message, 'danger');
        }
      };
    }

    const btnPhoneSend = document.getElementById('btn-phone-send');
    if (btnPhoneSend && state.loginAccountId) {
      btnPhoneSend.onclick = async () => {
        const acctId = state.loginAccountId;
        state.phoneNum = document.getElementById('tg-phone').value.trim();
        try {
          await api('/api/tg/phone/send_code', {
            method: 'POST',
            body: JSON.stringify({ account_id: acctId, phone: state.phoneNum }),
          });
          state.phoneStep = 'verify';
          state.phoneCode = '';
          toast('验证码已发送到你的 Telegram 客户端', 'success');
          render();
        } catch (err) {
          toast(err.message, 'danger');
        }
      };
    }

    const btnPhoneVerify = document.getElementById('btn-phone-verify');
    if (btnPhoneVerify && state.loginAccountId) {
      btnPhoneVerify.onclick = async () => {
        const acctId = state.loginAccountId;
        const code = document.getElementById('tg-otp')?.value.trim() || state.phoneCode || '';
        const password = document.getElementById('tg-2fa')?.value || '';
        try {
          const res = await api('/api/tg/phone/verify', {
            method: 'POST',
            body: JSON.stringify({ account_id: acctId, code, password }),
          });
          if (res.need_2fa) {
            state.phoneCode = code;
            state.phoneStep = 'need_2fa';
            toast(res.error, 'info');
            render();
            return;
          }
          state.phoneStep = 'input';
          state.phoneCode = '';
          state.loginAccountId = '';
          toast('Telegram 账号登录成功！', 'success');
          await loadRouteData('/telegram');
        } catch (err) {
          toast(err.message, 'danger');
        }
      };
    }

    // Settings form
    const settingsForm = document.getElementById('settings-form');
    if (settingsForm) {
      settingsForm.onsubmit = async (e) => {
        e.preventDefault();
        const payload = {
          delay_seconds: Number(document.getElementById('set-delay').value),
          max_download_mb: Number(document.getElementById('set-maxmb').value),
          dedup_ttl_hours: Number(document.getElementById('set-ttl').value),
          album_quiet_seconds: Number(document.getElementById('set-album-quiet').value),
          album_max_wait_seconds: Number(document.getElementById('set-album-max-wait').value),
          comment_wait_seconds: Number(document.getElementById('set-comment-wait').value),
          comment_silence_seconds: Number(document.getElementById('set-comment-silence').value),
          comment_max_extra_seconds: Number(document.getElementById('set-comment-max-extra').value),
          comment_merge_mode: document.getElementById('set-comment-mode').value,
        };
        if (!Number.isInteger(payload.album_quiet_seconds) || payload.album_quiet_seconds < 1 || payload.album_quiet_seconds > 300) {
          toast('相册静默等待必须是 1 到 300 秒的整数', 'danger');
          return;
        }
        if (!Number.isInteger(payload.album_max_wait_seconds) || payload.album_max_wait_seconds < 1 || payload.album_max_wait_seconds > 300) {
          toast('相册最长收集时间必须是 1 到 300 秒的整数', 'danger');
          return;
        }
        if (payload.album_max_wait_seconds < payload.album_quiet_seconds) {
          toast('相册最长收集时间必须大于或等于静默等待时间', 'danger');
          return;
        }
        if (!Number.isInteger(payload.comment_silence_seconds) || payload.comment_silence_seconds < 5 || payload.comment_silence_seconds > 300) {
          toast('评论区静默顺延必须是 5 到 300 秒的整数', 'danger');
          return;
        }
        if (!Number.isInteger(payload.comment_max_extra_seconds) || payload.comment_max_extra_seconds < 0 || payload.comment_max_extra_seconds > 600) {
          toast('评论区额外顺延上限必须是 0 到 600 秒的整数', 'danger');
          return;
        }
        try {
          await api('/api/settings', {
            method: 'PUT',
            body: JSON.stringify(payload),
          });
          if (state.settings) Object.assign(state.settings, payload);
          state.ruleGlobalDefaults = {
            comment_wait_seconds: payload.comment_wait_seconds,
            comment_merge_mode: payload.comment_merge_mode,
          };
          toast('设置已保存并热生效', 'success');
        } catch (err) {
          toast(err.message, 'danger');
        }
      };
    }
    const apiSettingsForm = document.getElementById('api-settings-form');
    if (apiSettingsForm) {
      apiSettingsForm.onsubmit = async (e) => {
        e.preventDefault();
        try {
          await api('/api/settings', {
            method: 'PUT',
            body: JSON.stringify({
              api_id: document.getElementById('set-api-id').value.trim(),
              api_hash: document.getElementById('set-api-hash').value.trim(),
            }),
          });
          toast('Telegram API 凭据已保存并重连', 'success');
          await loadRouteData('/settings');
        } catch (err) {
          toast(err.message, 'danger');
        }
      };
    }
    const btnResetApi = document.getElementById('btn-reset-api');
    if (btnResetApi) {
      btnResetApi.onclick = async () => {
        if (!confirm('确定要恢复为系统默认 (.env) 的 API ID / Hash 吗？')) return;
        try {
          await api('/api/settings', { method: 'PUT', body: JSON.stringify({ reset_api: true }) });
          toast('已恢复系统默认 API 凭据并重连', 'success');
          await loadRouteData('/settings');
        } catch (err) {
          toast(err.message, 'danger');
        }
      };
    }
    const btnRotate = document.getElementById('btn-rotate-token');
    if (btnRotate) {
      btnRotate.onclick = async () => {
        if (!confirm('确定要重置访问 Token 吗？旧直达链接及所有网页会话将失效。')) return;
        try {
          await api('/api/settings/token/rotate', { method: 'POST' });
          state.settings = null;
          state.authed = false;
          toast('访问 Token 已重置，请重新登录', 'success');
          render();
        } catch (err) {
          toast(err.message, 'danger');
        }
      };
    }

    // Log management page
    const btnRefreshLm = document.getElementById('btn-refresh-log-manage');
    if (btnRefreshLm) btnRefreshLm.onclick = () => loadRouteData('/log-manage');
    document.querySelectorAll('[data-lm-preset-days]').forEach((b) => {
      b.onclick = () => {
        const el = document.getElementById('lm-days');
        if (el) el.value = b.dataset.lmPresetDays;
      };
    });
    const lmForm = document.getElementById('log-manage-form');
    if (lmForm) {
      lmForm.onsubmit = async (e) => {
        e.preventDefault();
        try {
          const r = await api('/api/logs/manage', {
            method: 'PUT',
            body: JSON.stringify({
              log_retention_days: Number(document.getElementById('lm-days').value),
              log_max_rows: Number(document.getElementById('lm-maxrows').value),
            }),
          });
          state.logManage = r;
          toast(`日志策略已保存${r.pruned ? `，并清理了 ${r.pruned} 条过期日志` : ''}`, 'success');
          render();
        } catch (err) {
          toast(err.message, 'danger');
        }
      };
    }
    const btnPruneLogs = document.getElementById('btn-prune-logs');
    if (btnPruneLogs) {
      btnPruneLogs.onclick = async () => {
        try {
          const r = await api('/api/logs/prune', { method: 'POST' });
          if (state.logManage) state.logManage.stats = r.stats;
          toast(`已清理 ${r.deleted || 0} 条过期/超限日志`, 'success');
          render();
        } catch (err) {
          toast(err.message, 'danger');
        }
      };
    }
    document.querySelectorAll('[data-clear-logs]').forEach((b) => {
      b.onclick = async () => {
        const st = b.dataset.clearLogs || '';
        const label = st === 'skipped' ? '所有已拦截/跳过记录' : st === 'error' ? '所有失败记录' : '全部历史日志';
        if (!confirm(`确定要清空${label}吗？此操作不可恢复。`)) return;
        try {
          const r = await api('/api/logs/clear', { method: 'POST', body: JSON.stringify({ status: st }) });
          if (state.logManage) state.logManage.stats = r.stats;
          toast(`已清空 ${r.deleted || 0} 条记录`, 'success');
          render();
        } catch (err) {
          toast(err.message, 'danger');
        }
      };
    });

    // Regex manager controls
    const btnNewRegex = document.getElementById('btn-new-regex');
    if (btnNewRegex) btnNewRegex.onclick = () => openRegexModal(null);

    document.querySelectorAll('[data-rx-filter]').forEach((b) => {
      b.onclick = () => {
        state.regexFilterKind = b.dataset.rxFilter || '';
        render();
      };
    });
    const rxSearch = document.getElementById('rx-search');
    if (rxSearch) {
      rxSearch.oninput = () => {
        const pos = rxSearch.selectionStart;
        state.regexSearch = rxSearch.value;
        render();
        const el = document.getElementById('rx-search');
        if (el) { el.focus(); el.setSelectionRange(pos, pos); }
      };
    }
    document.querySelectorAll('[data-edit-regex]').forEach((b) => {
      b.onclick = () => {
        const rx = getRegexById(b.dataset.editRegex);
        if (rx) openRegexModal(rx);
      };
    });
    document.querySelectorAll('[data-del-regex]').forEach((b) => {
      b.onclick = async () => {
        const rx = getRegexById(b.dataset.delRegex);
        if (!rx) return;
        const usedBy = rx.used_by || [];
        const msg = usedBy.length
          ? `正则「${rx.name}」正被规则「${usedBy.join('、')}」引用，删除后会同时从这些规则中移除引用，确定删除吗？`
          : `确定要删除正则「${rx.name}」吗？`;
        if (!confirm(msg)) return;
        try {
          const r = await api(`/api/regex/${encodeURIComponent(rx.id)}`, { method: 'DELETE' });
          state.regexRules = r.items || [];
          state.regexTest.selectedIds = (state.regexTest.selectedIds || []).filter((id) => id !== rx.id);
          toast('正则已删除并热重载', 'success');
          render();
        } catch (err) {
          toast(err.message, 'danger');
        }
      };
    });
    document.querySelectorAll('[data-rx-try]').forEach((b) => {
      b.onclick = () => {
        const rid = b.dataset.rxTry;
        if (!state.regexTest.selectedIds.includes(rid)) state.regexTest.selectedIds.push(rid);
        render();
        const sec = document.getElementById('rx-test-section');
        if (sec) sec.scrollIntoView({ behavior: 'smooth', block: 'start' });
      };
    });
    document.querySelectorAll('[data-tpl-add]').forEach((b) => {
      b.onclick = () => {
        const tpl = REGEX_TEMPLATES[Number(b.dataset.tplAdd)];
        if (!tpl) return;
        openRegexModal(null, {
          name: tpl.name,
          kind: tpl.kind,
          content: tpl.patterns.join('\n'),
          note: tpl.desc,
        });
      };
    });
    document.querySelectorAll('[data-rx-test-toggle]').forEach((cb) => {
      cb.onchange = () => {
        const rid = cb.dataset.rxTestToggle;
        const cur = state.regexTest.selectedIds || [];
        const i = cur.indexOf(rid);
        if (cb.checked && i === -1) cur.push(rid);
        else if (!cb.checked && i >= 0) cur.splice(i, 1);
        state.regexTest.selectedIds = cur;
        const sampleEl = document.getElementById('rg-sample');
        if (sampleEl) state.regexTest.sample = sampleEl.value;
        render();
      };
    });
    const btnRgTest = document.getElementById('btn-rg-test');
    if (btnRgTest) {
      btnRgTest.onclick = async () => {
        const sampleEl = document.getElementById('rg-sample');
        if (sampleEl) state.regexTest.sample = sampleEl.value;
        if (!(state.regexTest.selectedIds || []).length) {
          return toast('请先勾选至少一条参与测试的正则', 'danger');
        }
        try {
          state.regexTest.result = await api('/api/rules/test', {
            method: 'POST',
            body: JSON.stringify({
              text: state.regexTest.sample,
              regex_ids: state.regexTest.selectedIds,
            }),
          });
          render();
        } catch (err) {
          toast(err.message, 'danger');
        }
      };
    }

    // Regex modal controls
    if (state.modalRegex) {
      const closeRxModal = () => { state.modalRegex = null; render(); };
      document.getElementById('btn-close-regex-modal').onclick = closeRxModal;
      document.getElementById('btn-cancel-regex-modal').onclick = closeRxModal;
      document.getElementById('rxm-kind').onchange = () => {
        syncRegexModalFromDom();
        render();
      };
      document.getElementById('btn-toggle-rxm-gen').onclick = () => {
        syncRegexModalFromDom();
        state.modalRegex.genOpen = !state.modalRegex.genOpen;
        render();
      };
      const applyGenerated = (append) => {
        syncRegexModalFromDom();
        const m = state.modalRegex;
        const kind = m.data.kind || 'skip';
        const mode = kind === 'strip' ? (m.genStripMode || 'strip_line') : kind;
        const out = buildGeneratedRegex(mode, m.genWords, m.genIgnoreCase !== false);
        if (!out) return toast('请先输入关键词', 'danger');
        if (append && m.data.content && m.data.content.trim()) {
          m.data.content = m.data.content.trimEnd() + '\n' + out;
        } else {
          m.data.content = out;
        }
        toast('已生成并填入正则框', 'success');
        render();
      };
      const btnGenReplace = document.getElementById('btn-rxm-gen-replace');
      if (btnGenReplace) btnGenReplace.onclick = () => applyGenerated(false);
      const btnGenAppend = document.getElementById('btn-rxm-gen-append');
      if (btnGenAppend) btnGenAppend.onclick = () => applyGenerated(true);

      document.getElementById('regex-modal-form').onsubmit = async (e) => {
        e.preventDefault();
        syncRegexModalFromDom();
        const m = state.modalRegex;
        const payload = parseRegexModalPayload(m);
        try {
          let r;
          if (m.id) {
            r = await api(`/api/regex/${encodeURIComponent(m.id)}`, { method: 'PUT', body: JSON.stringify(payload) });
          } else {
            r = await api('/api/regex', { method: 'POST', body: JSON.stringify(payload) });
          }
          state.regexRules = r.items || [];
          if (!m.id && r.id && !state.regexTest.selectedIds.includes(r.id)) {
            state.regexTest.selectedIds.push(r.id);
          }
          state.modalRegex = null;
          toast(m.id ? '正则已更新，并已热同步到关联规则' : '正则已添加到正则库', 'success');
          render();
        } catch (err) {
          toast(err.message, 'danger');
        }
      };
    }
  }

  function render() {
    const ruleBody = document.querySelector('#rule-modal-overlay .modal-body');
    const rxBody = document.querySelector('#regex-modal-overlay .modal-body');
    const pickerEl = document.getElementById('rm-picker-box');
    const ruleScroll = ruleBody ? ruleBody.scrollTop : 0;
    const rxScroll = rxBody ? rxBody.scrollTop : 0;
    const pickerScroll = pickerEl ? pickerEl.scrollTop : 0;
    document.getElementById('app').innerHTML = state.authed ? renderApp() : renderLogin();
    bindEvents();
    const newRuleBody = document.querySelector('#rule-modal-overlay .modal-body');
    const newRxBody = document.querySelector('#regex-modal-overlay .modal-body');
    const newPickerEl = document.getElementById('rm-picker-box');
    if (newRuleBody && ruleScroll) newRuleBody.scrollTop = ruleScroll;
    if (newRxBody && rxScroll) newRxBody.scrollTop = rxScroll;
    if (newPickerEl && pickerScroll) newPickerEl.scrollTop = pickerScroll;
  }

  async function boot() {
    window.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape') return;
      if (state.modalRegex) { state.modalRegex = null; render(); }
      else if (state.modalRule) { state.modalRule = null; render(); }
      else if (state.modalAddAccount) { state.modalAddAccount = false; render(); }
      else if (state.modalPassword) { state.modalPassword = false; render(); }
    });
    window.addEventListener('hashchange', () => {
      const next = location.hash.replace(/^#/, '') || '/';
      if (next !== state.route) {
        stopQrPoll();
        state.route = next;
        loadRouteData(next);
        render();
      }
    });
    try {
      if (state.route.startsWith('access-token=')) {
        const token = new URLSearchParams(state.route).get('access-token');
        state.route = '/';
        history.replaceState(null, '', location.pathname + location.search);
        if (token) await api('/api/auth/login', { method: 'POST', body: JSON.stringify({ token }) });
      }
      const st = await api('/api/auth/status');
      state.authed = st.authenticated;
      if (state.authed) await loadRouteData(state.route);
    } catch {
      state.authed = false;
    }
    render();
  }

  boot();
})();
