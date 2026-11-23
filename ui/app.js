(function () {
  const state = {
    authed: false,
    route: location.hash.replace(/^#/, '') || '/',
    sidebarOpen: false,
    theme: localStorage.getItem('tgfwd_theme') || 'system',
    overview: null,
    logs: { items: [], rules: [], accounts: [], status: '', rule: '', account_id: '' },
    rules: [],
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
    settings: null,
    modalRule: null, // null | { idx, data, chats }
    modalPassword: false,
    modalAddAccount: false,
    testInput: '🔥频道推荐内容\n这是一段正文，包含重要信息\n\n关注备用频道 @example_ad_channel',
    testOutput: null,
    regexGen: {
      mode: 'strip_line',
      words: '关注频道\n备用频道\n投稿联系',
      ignoreCase: true,
      out: '(?im)^.*(?:关注频道|备用频道|投稿联系).*$',
      sample: '🔥精品分享\n这是一段正文内容\n关注备用频道 @example_ad\n广告合作请联系 @someone',
      skip: '(?i)#ad\\b|广告合作|商务联系',
      stripAll: '',
      strip: '(?im)^.*(?:关注频道|备用频道|投稿联系).*$',
      replace: '',
      result: null,
    },
  };

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
      skip_regex: '<span class="badge muted">广告拦截</span>',
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
          limit: '100',
          status: state.logs.status,
          rule: state.logs.rule,
          account_id: state.logs.account_id,
        });
        const d = await api('/api/logs?' + qs.toString());
        state.logs.items = d.items || [];
        state.logs.rules = d.rules || [];
        state.logs.accounts = d.accounts || [];
      } else if (path === '/rules') {
        const d = await api('/api/rules');
        state.rules = d.rules || [];
        state.accounts = d.accounts || [];
        state.chatTitles = d.chat_titles || {};
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
        <p class="login-help">也可直接打开日志中的 <code>?token=...</code> 直达链接登录。若忘记密码，可在服务器查看 <code>journalctl -u tg-forwarder</code>。</p>
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
                  <span class="badge ${r.strip_all_text ? 'warning' : r.drop_author ? 'success' : 'muted'}">${r.strip_all_text ? '仅媒体' : r.drop_author ? '无来源' : '带来源'}</span>
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

  function renderLogs() {
    const items = state.logs.items || [];
    return `
      <div class="page-head">
        <div>
          <h1>转发记录</h1>
          <p class="page-sub">展示最近的转发、广告跳过与受限重传记录</p>
        </div>
        <div class="page-actions">
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
          <thead><tr><th>时间</th><th>账号 / 规则</th><th>方式</th><th>源频道 → 目标频道</th><th>内容摘要</th></tr></thead>
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
                      ${it.detail ? `<div class="hint" style="color:var(--danger)">${esc(it.detail)}</div>` : ''}
                    </td>
                  </tr>`
                    )
                    .join('')
                : '<tr><td colspan="5" class="empty">暂无符合条件的记录</td></tr>'
            }
          </tbody>
        </table>
      </div>`;
  }

  function renderRules() {
    const allRules = state.rules || [];
    const rules = allRules
      .map((r, idx) => ({ r, idx }))
      .filter(({ r }) => !state.ruleFilterAccount || (r.account_id || 'default') === state.ruleFilterAccount);

    return `
      <div class="page-head">
        <div>
          <h1>转发规则</h1>
          <p class="page-sub">按账号分别配置多对多频道映射、无来源转发、去文本与广告过滤规则</p>
        </div>
        <div class="page-actions">
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
            <article class="rule-card">
              <div class="rule-card-head">
                <div>
                  <div class="rule-card-title">${esc(r.name)}</div>
                  <div class="row" style="margin-top:6px;gap:6px">
                    <span class="badge ${r.enabled ? 'success status' : 'muted'}">${r.enabled ? '已启用' : '已停用'}</span>
                    <span class="badge info">账号: ${esc(accountName(r.account_id || 'default'))}</span>
                    ${r.drop_author !== false ? '<span class="badge info">无来源</span>' : ''}
                    ${r.fallback_reupload !== false ? '<span class="badge warning">受限重传</span>' : ''}
                    ${r.strip_all_text ? '<span class="badge warning">仅媒体</span>' : ''}
                  </div>
                </div>
                <div class="actions">
                  <button class="btn small" data-toggle-rule="${idx}">${r.enabled ? '停用' : '启用'}</button>
                  <button class="btn small" data-edit-rule="${idx}">编辑</button>
                  <button class="btn small danger" data-del-rule="${idx}">删除</button>
                </div>
              </div>

              <div class="rule-peers">
                <div><span class="muted">源频道：</span>${(r.sources || []).map(peerLabel).join('、') || '<span class="muted">未设置</span>'}</div>
                <div><span class="muted">目标频道：</span>${(r.targets || []).map(peerLabel).join('、') || '<span class="muted">未设置</span>'}</div>
              </div>

              <div class="hint">
                跳过正则 ${(r.skip_patterns || []).length} 条 · 去除正则 ${(r.strip_patterns || []).length} 条 · 替换 ${(r.replace_patterns || []).length} 条
              </div>
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
              <div>
                <div class="rule-card-title">${esc(a.name)}</div>
                <div class="row" style="margin-top:6px;gap:6px">
                  ${
                    !a.enabled
                      ? '<span class="badge muted">已停用</span>'
                      : a.authorized
                      ? `<span class="badge success status">${a.listening ? '监听中' : '已连接'}</span>`
                      : '<span class="badge warning status">等待登录</span>'
                  }
                  <span class="badge muted">绑定规则 ${a.rules_count} 条</span>
                </div>
              </div>
              <div class="actions">
                ${
                  a.enabled && !a.authorized
                    ? `<button class="btn small primary" data-acct-login="${esc(a.id)}">登录 / 扫码</button>`
                    : ''
                }
                <button class="btn small" data-acct-rename="${esc(a.id)}">重命名</button>
                <button class="btn small" data-acct-toggle="${esc(a.id)}" data-acct-enabled="${a.enabled ? '1' : '0'}">${a.enabled ? '停用' : '启用'}</button>
              </div>
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
                       <label>Telegram 登录验证码</label>
                       <input id="tg-otp" type="text" placeholder="输入收到的 5 位验证码" />
                     </div>`
                  : ''
              }
              ${
                state.phoneStep === 'need_2fa'
                  ? `<div class="field">
                       <label>两步验证 (2FA) 密码</label>
                       <input id="tg-2fa" type="password" placeholder="输入 2FA 云密码" />
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
    const tokenUrl = `${location.origin}/?token=${s.access_token}`;
    return `
      <div class="page-head">
        <div>
          <h1>全局设置</h1>
          <p class="page-sub">控制转发限速、受限下载上限、去重周期与面板访问 Token</p>
        </div>
      </div>

      <section class="panel">
        <div class="panel-head"><h2 class="panel-title">运行参数</h2></div>
        <form id="settings-form" class="form-grid">
          <div class="field">
            <label>转发间隔（秒）</label>
            <input id="set-delay" type="number" step="0.1" min="0" value="${esc(s.delay_seconds)}" />
            <span class="hint">每次发帖后停顿时间，建议 ≥ 1.0 防止 TG 限流</span>
          </div>
          <div class="field">
            <label>受限频道单次下载上限（MB）</label>
            <input id="set-maxmb" type="number" min="1" value="${esc(s.max_download_mb)}" />
            <span class="hint">仅针对禁止转发频道的兜底重传生效</span>
          </div>
          <div class="field">
            <label>消息去重保留时长（小时）</label>
            <input id="set-ttl" type="number" min="1" value="${esc(s.dedup_ttl_hours)}" />
          </div>
          <div class="span-full actions">
            <button class="btn primary" type="submit">保存设置</button>
          </div>
        </form>
      </section>

      <section class="panel">
        <div class="panel-head">
          <h2 class="panel-title">Telegram API 凭据（MTProto）</h2>
          <span class="badge ${s.api_custom ? 'info' : 'muted'}">${s.api_custom ? '自定义凭据' : '系统默认 (.env)'}</span>
        </div>
        <form id="api-settings-form" class="form-grid">
          <div class="field">
            <label>API ID</label>
            <input id="set-api-id" type="number" min="1" class="mono" value="${esc(s.api_id || '')}" required />
          </div>
          <div class="field">
            <label>API Hash</label>
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
          <span class="hint">包含访问 Token，可直接打开登录（等同于 SaveAny-Bot 的 /?token=...）</span>
        </div>
        <div class="actions" style="margin-top:14px">
          <button class="btn" data-copy="${esc(tokenUrl)}">复制直达链接</button>
          <button class="btn danger" id="btn-rotate-token">重置 Token</button>
        </div>
      </section>`;
  }

  function renderRuleModal() {
    if (!state.modalRule) return '';
    const { idx, data, chats } = state.modalRule;
    const isNew = idx === null;
    const chatList = chats || [];

    return `
      <div class="modal-overlay" id="rule-modal-overlay">
        <div class="modal">
          <div class="modal-head">
            <h3 class="modal-title">${isNew ? '新建转发规则' : '编辑转发规则'}</h3>
            <button class="btn ghost icon small" id="btn-close-rule-modal">${icon('close', 17)}</button>
          </div>

          <div class="stack">
            <div class="form-grid">
              <div class="field">
                <label>规则名称</label>
                <input id="rm-name" type="text" value="${esc(data.name)}" placeholder="例如：资源频道搬运" />
              </div>
              <div class="field">
                <label>执行账号</label>
                <select id="rm-account">
                  ${(state.accounts || []).map((a) => `<option value="${esc(a.id)}" ${(data.account_id || 'default') === a.id ? 'selected' : ''}>${esc(a.name)}</option>`).join('')}
                </select>
              </div>
            </div>

            <label class="checkbox"><input id="rm-enabled" type="checkbox" ${data.enabled ? 'checked' : ''} /> <span>启用此规则</span></label>

            <div class="field">
              <label>源频道列表（接收新消息的频道）</label>
              <div class="peer-chip-list">
                ${(data.sources || []).map((s, i) => `<span class="peer-chip">${peerLabel(s)} <button type="button" data-rm-del-src="${i}">×</button></span>`).join('') || '<span class="hint">暂无源频道</span>'}
              </div>
              <div class="row" style="margin-top:6px">
                <input id="rm-add-src-input" type="text" class="grow" placeholder="输入频道 ID（如 -100123456）或 @username" />
                <button type="button" class="btn small" id="btn-rm-add-src">添加源</button>
              </div>
            </div>

            <div class="field">
              <label>目标频道列表（搬运发送到这里）</label>
              <div class="peer-chip-list">
                ${(data.targets || []).map((t, i) => `<span class="peer-chip">${peerLabel(t)} <button type="button" data-rm-del-tgt="${i}">×</button></span>`).join('') || '<span class="hint">暂无目标频道</span>'}
              </div>
              <div class="row" style="margin-top:6px">
                <input id="rm-add-tgt-input" type="text" class="grow" placeholder="输入目标频道 ID（如 -100987654）或 @username" />
                <button type="button" class="btn small" id="btn-rm-add-tgt">添加目标</button>
              </div>
            </div>

            ${
              chatList.length
                ? `<div class="field">
                     <label>从「${esc(accountName(data.account_id || 'default'))}」已加入频道快速勾选</label>
                     <div class="picker-box">
                       ${chatList
                         .map(
                           (c) => `
                         <div class="picker-item">
                           <span>${esc(c.title)} <span class="badge muted">${c.kind === 'channel' ? '频道' : c.kind === 'supergroup' ? '超级群' : '群组'}</span> <span class="mono muted">${esc(c.id)}</span> ${c.noforwards ? '<span class="badge warning">禁转发</span>' : ''}</span>
                           <span class="actions">
                             <button type="button" class="btn small" data-pick-src="${esc(c.id)}">+ 源</button>
                             <button type="button" class="btn small" data-pick-tgt="${esc(c.id)}">+ 目标</button>
                           </span>
                         </div>`
                         )
                         .join('')}
                     </div>
                   </div>`
                : '<div class="hint">该账号暂无已加入频道或尚未登录，也可直接手动输入频道 ID / @username</div>'
            }

            <div class="form-grid">
              <label class="checkbox"><input id="rm-drop-author" type="checkbox" ${data.drop_author !== false ? 'checked' : ''} /> <span>无来源转发（去除“转发自”）</span></label>
              <label class="checkbox"><input id="rm-fallback" type="checkbox" ${data.fallback_reupload !== false ? 'checked' : ''} /> <span>禁止转发频道自动下载重传</span></label>
              <label class="checkbox"><input id="rm-strip-all-text" type="checkbox" ${data.strip_all_text ? 'checked' : ''} /> <span>去除全部文本（清空配文，仅发媒体）</span></label>
              <label class="checkbox"><input id="rm-skip-empty" type="checkbox" ${data.skip_empty_text_only !== false ? 'checked' : ''} /> <span>清洗后为空的纯文本跳过</span></label>
              <label class="checkbox"><input id="rm-no-preview" type="checkbox" ${data.disable_link_preview ? 'checked' : ''} /> <span>禁用链接网页预览</span></label>
            </div>

            <div class="field">
              <label>整条跳过正则（skip_patterns，每行一条）</label>
              <textarea id="rm-skip" placeholder="(?i)#ad\\b|推广合作">${esc((data.skip_patterns || []).join('\n'))}</textarea>
              <span class="hint">即使开启「去除全部文本」，仍会先按原配文检查此处正则，拦截广告图片/视频</span>
            </div>

            <div class="field">
              <label>命中即清空全部文本正则（strip_text_patterns，每行一条）</label>
              <textarea id="rm-strip-all-on-match" placeholder="(?i)推广文案|点击看更多">${esc((data.strip_text_patterns || []).join('\n'))}</textarea>
              <span class="hint">未命中「整条跳过」但命中此处正则时，清空整条消息配文，仅发媒体（纯文字贴自动跳过）</span>
            </div>

            <div class="field">
              <label>删除广告文本正则（strip_patterns，每行一条）</label>
              <textarea id="rm-strip" placeholder="(?m)^.*(?:关注频道|备用频道).*$">${esc((data.strip_patterns || []).join('\n'))}</textarea>
              <span class="hint">未开启「去除全部文本」时生效；匹配片段会被删除，同时保留正文加粗、超链接与剧透格式</span>
            </div>

            <div class="field">
              <label>正则替换（replace_patterns，每行一条：查找正则 => 替换为）</label>
              <textarea id="rm-replace" placeholder="@old_channel => @my_channel">${esc((data.replace_patterns || []).map((r) => `${r.pattern} => ${r.repl || ''}`).join('\n'))}</textarea>
            </div>

            <div class="panel" style="padding:14px;background:var(--paper)">
              <div class="row" style="justify-content:space-between;margin-bottom:8px">
                <strong style="font-size:13px">在线测试正则清洗效果</strong>
                <button type="button" class="btn small" id="btn-test-regex">运行测试</button>
              </div>
              <div class="field">
                <textarea id="rm-test-sample" style="min-height:68px">${esc(state.testInput)}</textarea>
              </div>
              ${
                state.testOutput
                  ? `<div class="test-result" style="margin-top:8px">${
                      state.testOutput.skipped
                        ? `<span class="badge warning">已拦截跳过</span> 命中规则：<code>${esc(state.testOutput.skip_reason)}</code>（即使开启「去除全部文本」也会被拦截）`
                        : `<div><span class="badge ${state.testOutput.changed ? 'info' : 'muted'}">${state.testOutput.changed ? '已清洗' : '无变化'}</span></div><div style="margin-top:6px">${esc(state.testOutput.cleaned || '(配文已清空，仅发送媒体)')}</div>`
                    }</div>`
                  : ''
              }
            </div>
          </div>

          <div class="modal-actions">
            <button type="button" class="btn ghost" id="btn-cancel-rule-modal">取消</button>
            <button type="button" class="btn primary" id="btn-save-rule">保存规则</button>
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
            <button class="btn ghost icon small" id="btn-close-add-acct">${icon('close', 17)}</button>
          </div>
          <form id="add-acct-form" class="stack">
            <div class="field">
              <label>账号备注名称</label>
              <input id="add-acct-name" type="text" placeholder="例如：搬运小号2 / 美区小号" required />
              <span class="hint">添加后即可使用网页扫码或手机号验证码登录</span>
            </div>
            <div class="modal-actions">
              <button type="button" class="btn ghost" id="btn-cancel-add-acct">取消</button>
              <button type="submit" class="btn primary">创建并去登录</button>
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
            <strong>3. 五个规则开关说明</strong>
            <ul class="guide-list">
              <li><b>无来源转发</b>：不显示「转发自 xxx」；普通频道零下载秒转，保留原排版与相册。</li>
              <li><b>禁止转发频道自动下载重传</b>：遇到保护内容频道时，自动缓存下载后再上传，传完清理缓存。</li>
              <li><b>去除全部文本（清空配文，仅发媒体）</b>：清空图片/视频/文件配文只发媒体本身（纯文字贴自动跳过）。<b>注意：仍会先按原配文跑一遍「整条跳过正则」拦截广告图文，再清空文本发送！</b></li>
              <li><b>清洗后为空的纯文本跳过</b>：纯文字广告被正则删空后不发空白消息。</li>
              <li><b>禁用链接网页预览</b>：隐藏消息底部的链接大图摘要卡片。</li>
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
              <li><b>不会写正则？</b>去左侧「正则生成」页输入关键词一键生成并测试效果，再粘进规则保存（保存即热生效）。</li>
            </ul>
          </div>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head">
          <h2 class="panel-title">二、其他页面简略说明</h2>
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
            <p class="hint" style="margin:6px 0 0">按账号/状态/规则筛选搬运记录：无来源秒转、无下载清洗、下载重传或广告拦截。</p>
          </div>
          <div class="guide-item">
            <strong>全局设置 & 正则生成</strong>
            <p class="hint" style="margin:6px 0 0">调节发帖间隔、下载上限与免密 Token；正则生成页提供关键词一键转正则和实时测试。</p>
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
    if (mode === 'skip_words') return `${ignoreCase ? '(?i)' : ''}${joined}`;
    if (mode === 'strip_all_on_match') return `${ignoreCase ? '(?i)' : ''}${joined}`;
    if (mode === 'strip_line') return `${ignoreCase ? '(?im)' : '(?m)'}^.*${group}.*$`;
    if (mode === 'strip_words') return `${ignoreCase ? '(?i)' : ''}${joined}`;
    if (mode === 'replace_words') {
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

  function renderRegexHelper() {
    const g = state.regexGen;
    const templates = [
      { title: '拦截常见广告/推广贴', target: 'skip', desc: '整条消息命中即跳过', pat: '(?i)#ad\\b|广告合作|商务联系|推广' },
      { title: '拦截带外部网址的帖子', target: 'skip', desc: '含 http 链接但放行 t.me', pat: '(?i)https?://(?!t\\.me/)' },
      { title: '删掉含关键词的整行广告', target: 'strip', desc: '把整行“关注频道/备用频道”删掉', pat: '(?im)^.*(?:关注频道|备用频道|投稿联系).*$' },
      { title: '删掉所有 @用户名', target: 'strip', desc: '清空文内或末尾的 @xxx 提及', pat: '@\\w+' },
      { title: '删掉所有 t.me 频道/群链接', target: 'strip', desc: '移除 https://t.me/xxx 尾巴', pat: '(?i)https?://t\\.me/\\S+' },
      { title: '删掉所有 #话题标签', target: 'strip', desc: '移除 #标签1 #标签2', pat: '#[\\w\\u4e00-\\u9fff]+' },
    ];
    return `
      <div class="page-head">
        <div>
          <h1>正则生成</h1>
          <p class="page-sub">不会写正则也没关系：输入想过滤的关键词自动生成，复制后即可用到转发规则里</p>
        </div>
        <div class="page-actions">
          <button class="btn primary" data-nav="/rules">去配规则</button>
        </div>
      </div>

      <section class="panel">
        <div class="panel-head">
          <h2 class="panel-title">一、关键词一键生成正则</h2>
          <span class="badge info">自动转义特殊符号</span>
        </div>
        <div class="form-grid">
          <div class="field">
            <label>想要的效果</label>
            <select id="rg-mode">
              <option value="skip_words" ${g.mode === 'skip_words' ? 'selected' : ''}>整条跳过（含任意关键词就整条不转发）</option>
              <option value="strip_all_on_match" ${g.mode === 'strip_all_on_match' ? 'selected' : ''}>命中就去除文本发送（含关键词就清空全部配文，仅发媒体）</option>
              <option value="strip_line" ${g.mode === 'strip_line' ? 'selected' : ''}>删掉整行（某一行含关键词就把整行删掉）</option>
              <option value="strip_words" ${g.mode === 'strip_words' ? 'selected' : ''}>只删词本身（保留同一行里的其他文字）</option>
              <option value="replace_words" ${g.mode === 'replace_words' ? 'selected' : ''}>查找替换（每行填：旧词 => 新词）</option>
            </select>
          </div>
          <div class="field" style="justify-content:flex-end">
            <label class="checkbox"><input id="rg-icase" type="checkbox" ${g.ignoreCase ? 'checked' : ''} /> <span>忽略英文字母大小写</span></label>
          </div>
          <div class="field span-full">
            <label>${g.mode === 'replace_words' ? '输入替换内容（每行一条：旧词 => 新词）' : '输入关键词（每行一个，或用逗号隔开）'}</label>
            <textarea id="rg-words" style="min-height:84px" placeholder="${g.mode === 'replace_words' ? '@原频道 => @我的频道' : '关注频道\n备用频道\n广告合作'}">${esc(g.words)}</textarea>
          </div>
          <div class="field span-full">
            <label>生成结果（${g.mode === 'skip_words' ? '填到规则的「整条跳过正则」' : g.mode === 'strip_all_on_match' ? '填到规则的「命中即清空全部文本正则」' : g.mode === 'replace_words' ? '填到规则的「正则替换」' : '填到规则的「删除广告文本正则」'}）</label>
            <textarea id="rg-out" readonly style="min-height:60px">${esc(g.out)}</textarea>
          </div>
          <div class="span-full actions">
            <button type="button" class="btn primary" id="btn-rg-copy">复制生成结果</button>
            <button type="button" class="btn" id="btn-rg-fill">填入下方测试</button>
          </div>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head"><h2 class="panel-title">二、常用现成模板（一键复制）</h2></div>
        <div class="form-grid">
          ${templates
            .map(
              (t, i) => `
            <div class="guide-item">
              <div class="row" style="justify-content:space-between;margin-bottom:6px">
                <strong>${esc(t.title)}</strong>
                <span class="badge ${t.target === 'skip' ? 'warning' : 'info'}">${t.target === 'skip' ? '跳过规则' : '删除规则'}</span>
              </div>
              <div class="hint" style="margin-bottom:8px">${esc(t.desc)}</div>
              <div class="code-snip" style="width:100%;word-break:break-all;margin-bottom:8px">${esc(t.pat)}</div>
              <div class="actions">
                <button type="button" class="btn small" data-copy="${esc(t.pat)}">复制</button>
                <button type="button" class="btn small" data-tpl-fill="${i}">试用</button>
              </div>
            </div>`
            )
            .join('')}
        </div>
      </section>

      <section class="panel">
        <div class="panel-head">
          <h2 class="panel-title">三、在线测试清洗效果</h2>
          <button type="button" class="btn primary small" id="btn-rg-test">运行测试</button>
        </div>
        <div class="form-grid">
          <div class="field span-full">
            <label>测试消息原文</label>
            <textarea id="rg-sample" style="min-height:84px">${esc(g.sample)}</textarea>
          </div>
          <div class="field">
            <label>整条跳过正则（skip_patterns）</label>
            <textarea id="rg-test-skip" style="min-height:70px">${esc(g.skip)}</textarea>
          </div>
          <div class="field">
            <label>命中即清空文本正则（strip_text_patterns）</label>
            <textarea id="rg-test-strip-all" style="min-height:70px">${esc(g.stripAll || '')}</textarea>
          </div>
          <div class="field">
            <label>删除文本正则（strip_patterns）</label>
            <textarea id="rg-test-strip" style="min-height:70px">${esc(g.strip)}</textarea>
          </div>
          <div class="field">
            <label>替换规则（每行：查找 => 替换）</label>
            <textarea id="rg-test-replace" style="min-height:70px">${esc(g.replace)}</textarea>
          </div>
        </div>
        ${
          g.result
            ? `<div class="test-result" style="margin-top:12px">${
                g.result.skipped
                  ? `<span class="badge warning">已拦截跳过</span> 命中规则：<code>${esc(g.result.skip_reason)}</code>`
                  : g.result.cleared_all_text
                  ? `<span class="badge warning">已清空全部文本</span> 命中：<code>${esc(g.result.clear_reason)}</code>（将仅发送媒体）`
                  : `<div><span class="badge ${g.result.changed ? 'info' : 'muted'}">${g.result.changed ? '已清洗' : '无变化'}</span></div><div style="margin-top:6px">${esc(g.result.cleaned || '(空)')}</div>`
              }</div>`
            : ''
        }
      </section>

      <section class="panel">
        <div class="panel-head"><h2 class="panel-title">四、30 秒看懂常用符号</h2></div>
        <div class="form-grid">
          <div class="guide-item"><span class="code-snip">|</span> <span class="hint" style="margin-left:6px">“或者”的意思，例如 <span class="code-snip">广告|推广</span></span></div>
          <div class="guide-item"><span class="code-snip">(?i)</span> <span class="hint" style="margin-left:6px">放在开头表示忽略英文大小写</span></div>
          <div class="guide-item"><span class="code-snip">(?m)^.*词.*$</span> <span class="hint" style="margin-left:6px">匹配包含“词”的<b>一整行</b></span></div>
          <div class="guide-item"><span class="code-snip">\\.</span> <span class="hint" style="margin-left:6px">匹配真实的小数点，如 <span class="code-snip">t\\.me</span></span></div>
        </div>
      </section>`;
  }

  function renderPasswordModal() {
    if (!state.modalPassword) return '';
    return `
      <div class="modal-overlay" id="pwd-modal-overlay">
        <div class="modal" style="max-width:460px">
          <div class="modal-head">
            <h3 class="modal-title">修改管理员密码</h3>
            <button class="btn ghost icon small" id="btn-close-pwd">${icon('close', 17)}</button>
          </div>
          <form id="pwd-form" class="stack">
            <div class="field"><label>当前密码</label><input id="pwd-cur" type="password" required /></div>
            <div class="field"><label>新密码（至少 8 位）</label><input id="pwd-new" type="password" minlength="8" required /></div>
            <div class="field"><label>确认新密码</label><input id="pwd-confirm" type="password" minlength="8" required /></div>
            <div class="modal-actions">
              <button type="button" class="btn ghost" id="btn-cancel-pwd">取消</button>
              <button type="submit" class="btn primary">保存新密码</button>
            </div>
          </form>
        </div>
      </div>`;
  }

  function renderApp() {
    const navGroups = [
      { label: '工作台', items: [['/', '概览', 'dashboard'], ['/rules', '转发规则', 'rules'], ['/chats', '频道目录', 'chats']] },
      { label: '连接与记录', items: [['/telegram', 'Telegram 账号', 'telegram'], ['/logs', '转发记录', 'logs']] },
      { label: '系统与帮助', items: [['/settings', '全局设置', 'settings'], ['/regex', '正则生成', 'regex'], ['/guide', '使用说明', 'guide']] },
    ];
    const authCnt = (state.accounts || []).filter((a) => a.authorized).length;
    const totalCnt = (state.accounts || []).length;

    let bodyHtml = '';
    if (state.route === '/') bodyHtml = renderOverview();
    else if (state.route === '/regex') bodyHtml = renderRegexHelper();
    else if (state.route === '/guide') bodyHtml = renderGuide();
    else if (state.route === '/logs') bodyHtml = renderLogs();
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
    d.strip_all_text = g('rm-strip-all-text').checked;
    d.skip_empty_text_only = g('rm-skip-empty').checked;
    d.disable_link_preview = g('rm-no-preview').checked;
    d.skip_patterns = g('rm-skip').value.split('\n').map((s) => s.trim()).filter(Boolean);
    d.strip_text_patterns = g('rm-strip-all-on-match').value.split('\n').map((s) => s.trim()).filter(Boolean);
    d.strip_patterns = g('rm-strip').value.split('\n').map((s) => s.trim()).filter(Boolean);
    d.replace_patterns = g('rm-replace')
      .value.split('\n')
      .map((l) => l.trim())
      .filter(Boolean)
      .map((l) => {
        const idx = l.indexOf('=>');
        if (idx === -1) return { pattern: l, repl: '' };
        return { pattern: l.slice(0, idx).trim(), repl: l.slice(idx + 2).trim() };
      });
    state.testInput = g('rm-test-sample').value;
  }

  async function loadModalChatsForAccount(acctId) {
    if (!state.modalRule) return;
    try {
      const d = await api('/api/chats?account_id=' + encodeURIComponent(acctId));
      if (state.modalRule && state.modalRule.data.account_id === acctId) {
        state.modalRule.chats = d.authorized ? d.items || [] : [];
        render();
      }
    } catch {
      if (state.modalRule) { state.modalRule.chats = []; render(); }
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
      el.onclick = async () => {
        await navigator.clipboard.writeText(el.dataset.copy);
        toast('已复制到剪贴板', 'success');
      };
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
        await api('/api/auth/logout', { method: 'POST' });
        state.authed = false;
        render();
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

    const logAcct = document.getElementById('log-filter-account');
    if (logAcct) logAcct.onchange = () => { state.logs.account_id = logAcct.value; loadRouteData('/logs'); };
    const logSt = document.getElementById('log-filter-status');
    if (logSt) logSt.onchange = () => { state.logs.status = logSt.value; loadRouteData('/logs'); };
    const logRl = document.getElementById('log-filter-rule');
    if (logRl) logRl.onchange = () => { state.logs.rule = logRl.value; loadRouteData('/logs'); };

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
        const d = await api('/api/chats?account_id=' + encodeURIComponent(state.chatsAccountId) + '&refresh=1');
        state.chatsAuthorized = d.authorized;
        state.chats = d.items || [];
        toast('频道目录已刷新', 'success');
        render();
      };
    }

    // Rules page
    const rulesFilter = document.getElementById('rules-acct-filter');
    if (rulesFilter) rulesFilter.onchange = () => { state.ruleFilterAccount = rulesFilter.value; render(); };

    const openRuleModal = async (idx, data) => {
      state.testOutput = null;
      const cloned = JSON.parse(JSON.stringify(data));
      if (!cloned.account_id) cloned.account_id = state.accounts[0]?.id || 'default';
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
          strip_all_text: false,
          skip_empty_text_only: true,
          disable_link_preview: false,
          skip_patterns: [],
          strip_text_patterns: [],
          strip_patterns: [],
          replace_patterns: [],
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
          await api(`/api/rules/${idx}/toggle`, { method: 'POST' });
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
          await api(`/api/rules/${idx}`, { method: 'DELETE' });
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

      document.getElementById('btn-rm-add-src').onclick = () => {
        syncRuleModalFromDom();
        const v = document.getElementById('rm-add-src-input').value.trim();
        if (v && !state.modalRule.data.sources.map(String).includes(v)) {
          state.modalRule.data.sources.push(/^-?\d+$/.test(v) ? Number(v) : v);
        }
        render();
      };
      document.getElementById('btn-rm-add-tgt').onclick = () => {
        syncRuleModalFromDom();
        const v = document.getElementById('rm-add-tgt-input').value.trim();
        if (v && !state.modalRule.data.targets.map(String).includes(v)) {
          state.modalRule.data.targets.push(/^-?\d+$/.test(v) ? Number(v) : v);
        }
        render();
      };
      document.querySelectorAll('[data-rm-del-src]').forEach((b) => {
        b.onclick = () => { syncRuleModalFromDom(); state.modalRule.data.sources.splice(Number(b.dataset.rmDelSrc), 1); render(); };
      });
      document.querySelectorAll('[data-rm-del-tgt]').forEach((b) => {
        b.onclick = () => { syncRuleModalFromDom(); state.modalRule.data.targets.splice(Number(b.dataset.rmDelTgt), 1); render(); };
      });
      document.querySelectorAll('[data-pick-src]').forEach((b) => {
        b.onclick = () => {
          syncRuleModalFromDom();
          const v = Number(b.dataset.pickSrc);
          if (!state.modalRule.data.sources.map(String).includes(String(v))) state.modalRule.data.sources.push(v);
          render();
        };
      });
      document.querySelectorAll('[data-pick-tgt]').forEach((b) => {
        b.onclick = () => {
          syncRuleModalFromDom();
          const v = Number(b.dataset.pickTgt);
          if (!state.modalRule.data.targets.map(String).includes(String(v))) state.modalRule.data.targets.push(v);
          render();
        };
      });

      document.getElementById('btn-test-regex').onclick = async () => {
        syncRuleModalFromDom();
        const d = state.modalRule.data;
        try {
          state.testOutput = await api('/api/rules/test', {
            method: 'POST',
            body: JSON.stringify({
              text: state.testInput,
              strip_all_text: d.strip_all_text,
              skip_patterns: d.skip_patterns,
              strip_text_patterns: d.strip_text_patterns,
              strip_patterns: d.strip_patterns,
              replace_patterns: d.replace_patterns,
            }),
          });
          render();
        } catch (err) {
          toast(err.message, 'danger');
        }
      };

      document.getElementById('btn-save-rule').onclick = async () => {
        syncRuleModalFromDom();
        const { idx, data } = state.modalRule;
        try {
          if (idx === null) {
            await api('/api/rules', { method: 'POST', body: JSON.stringify(data) });
          } else {
            await api(`/api/rules/${idx}`, { method: 'PUT', body: JSON.stringify(data) });
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
        const code = document.getElementById('tg-otp')?.value.trim() || '';
        const password = document.getElementById('tg-2fa')?.value || '';
        try {
          const res = await api('/api/tg/phone/verify', {
            method: 'POST',
            body: JSON.stringify({ account_id: acctId, code, password }),
          });
          if (res.need_2fa) {
            state.phoneStep = 'need_2fa';
            toast(res.error, 'info');
            render();
            return;
          }
          state.phoneStep = 'input';
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
        try {
          await api('/api/settings', {
            method: 'PUT',
            body: JSON.stringify({
              delay_seconds: Number(document.getElementById('set-delay').value),
              max_download_mb: Number(document.getElementById('set-maxmb').value),
              dedup_ttl_hours: Number(document.getElementById('set-ttl').value),
            }),
          });
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
        if (!confirm('确定要重置访问 Token 吗？旧的直达链接将失效。')) return;
        const r = await api('/api/settings/token/rotate', { method: 'POST' });
        state.settings.access_token = r.access_token;
        toast('访问 Token 已重置', 'success');
        render();
      };
    }

    // Regex helper controls
    const rgMode = document.getElementById('rg-mode');
    if (rgMode) {
      const updateGen = () => {
        const g = state.regexGen;
        g.mode = document.getElementById('rg-mode').value;
        g.ignoreCase = document.getElementById('rg-icase').checked;
        g.words = document.getElementById('rg-words').value;
        g.out = buildGeneratedRegex(g.mode, g.words, g.ignoreCase);
        const outEl = document.getElementById('rg-out');
        if (outEl) outEl.value = g.out;
      };
      rgMode.onchange = () => { updateGen(); render(); };
      document.getElementById('rg-icase').onchange = updateGen;
      document.getElementById('rg-words').oninput = updateGen;
      document.getElementById('btn-rg-copy').onclick = async () => {
        updateGen();
        if (!state.regexGen.out) return toast('请先输入关键词', 'danger');
        await navigator.clipboard.writeText(state.regexGen.out);
        toast('已复制生成的正则', 'success');
      };
      document.getElementById('btn-rg-fill').onclick = () => {
        updateGen();
        const g = state.regexGen;
        if (g.mode === 'skip_words') g.skip = g.out;
        else if (g.mode === 'strip_all_on_match') g.stripAll = g.out;
        else if (g.mode === 'replace_words') g.replace = g.out;
        else g.strip = g.out;
        toast('已填入下方测试区', 'success');
        render();
      };
      const tplList = [
        { target: 'skip', pat: '(?i)#ad\\b|广告合作|商务联系|推广' },
        { target: 'skip', pat: '(?i)https?://(?!t\\.me/)' },
        { target: 'strip', pat: '(?im)^.*(?:关注频道|备用频道|投稿联系).*$' },
        { target: 'strip', pat: '@\\w+' },
        { target: 'strip', pat: '(?i)https?://t\\.me/\\S+' },
        { target: 'strip', pat: '#[\\w\\u4e00-\\u9fff]+' },
      ];
      document.querySelectorAll('[data-tpl-fill]').forEach((b) => {
        b.onclick = () => {
          const item = tplList[Number(b.dataset.tplFill)];
          if (!item) return;
          if (item.target === 'skip') state.regexGen.skip = item.pat;
          else state.regexGen.strip = item.pat;
          toast('已填入下方测试区', 'success');
          render();
        };
      });
      document.getElementById('btn-rg-test').onclick = async () => {
        const g = state.regexGen;
        g.sample = document.getElementById('rg-sample').value;
        g.skip = document.getElementById('rg-test-skip').value;
        g.stripAll = document.getElementById('rg-test-strip-all').value;
        g.strip = document.getElementById('rg-test-strip').value;
        g.replace = document.getElementById('rg-test-replace').value;
        try {
          g.result = await api('/api/rules/test', {
            method: 'POST',
            body: JSON.stringify({
              text: g.sample,
              skip_patterns: g.skip.split('\n').map((s) => s.trim()).filter(Boolean),
              strip_text_patterns: g.stripAll.split('\n').map((s) => s.trim()).filter(Boolean),
              strip_patterns: g.strip.split('\n').map((s) => s.trim()).filter(Boolean),
              replace_patterns: g.replace.split('\n').map((l) => l.trim()).filter(Boolean).map((l) => {
                const i = l.indexOf('=>');
                return i === -1 ? { pattern: l, repl: '' } : { pattern: l.slice(0, i).trim(), repl: l.slice(i + 2).trim() };
              }),
            }),
          });
          render();
        } catch (err) {
          toast(err.message, 'danger');
        }
      };
    }
  }

  function render() {
    document.getElementById('app').innerHTML = state.authed ? renderApp() : renderLogin();
    bindEvents();
  }

  async function boot() {
    window.addEventListener('hashchange', () => {
      const next = location.hash.replace(/^#/, '') || '/';
      if (next !== state.route) {
        state.route = next;
        loadRouteData(next);
        render();
      }
    });
    try {
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
