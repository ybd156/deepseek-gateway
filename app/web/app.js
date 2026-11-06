/* 飞牛 AI 网关 —— 前端（纯原生，无依赖） */
'use strict';

// ── 基础 ────────────────────────────────────────────────
const BASE = (function () {
  const seg = location.pathname.split('/').filter(Boolean);
  let cut = seg.length;
  for (let i = seg.length - 1; i >= 0; i--) {
    const s = seg[i];
    if (s === 'admin' || s === 'settings' || s === 'logs' || s === 'static') { cut = i; }
  }
  return cut > 0 ? ('/' + seg.slice(0, cut).join('/')) : '';
})();

function tokStore(k, v) {
  if (v === undefined) return localStorage.getItem(k) || '';
  localStorage.setItem(k, v);
}
const tok = () => tokStore('gw_token');
const authHeaders = () => ({
  'Authorization': 'Bearer ' + tok(),
  'Content-Type': 'application/json'
});

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/"/g, '&quot;')
    .replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function msg(tab, text) {
  const el = document.getElementById('msg' + tab);
  if (el) el.textContent = text || '';
}

async function api(path, opts) {
  const o = Object.assign({ headers: authHeaders() }, opts || {});
  const r = await fetch(BASE + path, o);
  if (r.status === 401) {
    throw new Error('未登录：请先在「设置」里填访问口令');
  }
  const text = await r.text();
  try { return JSON.parse(text); } catch (e) { return { ok: false, error: text.slice(0, 200) }; }
}

async function act(path, body, okText, tab) {
  try {
    const d = await api(path, { method: 'POST', body: JSON.stringify(body || {}) });
    msg(tab || 'Providers', (d.ok === false ? '失败：' + (d.error || '') : (okText || '完成')));
    return d;
  } catch (e) {
    msg(tab || 'Providers', '失败：' + e.message);
    return { ok: false };
  }
}

function copyText(txt, label) {
  const done = () => toast('已复制' + (label ? '：' + label : ''));
  const fail = () => toast('复制失败，请手动选中');
  const fallback = () => {
    try {
      const ta = document.createElement('textarea');
      ta.value = txt;
      ta.style.position = 'fixed'; ta.style.top = '-1000px';
      document.body.appendChild(ta); ta.select();
      const ok = document.execCommand('copy');
      document.body.removeChild(ta);
      ok ? done() : fail();
    } catch (e) { fail(); }
  };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(txt).then(done).catch(fallback);
  } else { fallback(); }
}

function copyVal(id, label) {
  const el = document.getElementById(id);
  const v = el ? el.value : '';
  if (!v) { toast('这一行还没有内容'); return; }
  copyText(v, label);
}

let toastTimer = null;
function toast(text) {
  let el = document.getElementById('toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'toast';
    el.style.cssText = 'position:fixed;right:20px;bottom:20px;background:#111827;color:#fff;'
      + 'padding:10px 16px;border-radius:10px;font-size:13px;opacity:0;'
      + 'transition:opacity .2s;z-index:99';
    document.body.appendChild(el);
  }
  el.textContent = text;
  el.style.opacity = '1';
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.style.opacity = '0'; }, 2200);
}

// ── 标签页 ──────────────────────────────────────────────
function currentTab() {
  let p = location.pathname;
  while (p.length > 1 && p.endsWith('/')) p = p.slice(0, -1);
  if (p.indexOf('/logs') >= 0) return 'logs';
  if (p.indexOf('/advanced') >= 0) return 'advanced';
  if (p.indexOf('/models') >= 0) return 'models';
  if (p.indexOf('/stats') >= 0) return 'stats';
  if (p.indexOf('/settings') >= 0) return 'settings';
  return 'overview';
}

function showTab(name) {
  document.querySelectorAll('.tab').forEach(el => {
    el.classList.toggle('on', el.id === 'tab-' + name);
  });
  document.querySelectorAll('#nav a').forEach(a => {
    a.classList.toggle('on', a.dataset.tab === name);
  });
  if (name === 'logs') { loadUsage(); loadErrors(); loadGwLog(); }
  if (name === 'stats') loadStats();
  if (name === 'models') { loadCfg().then(loadCaps); }
  if (name === 'advanced') loadCfg();
  if (name === 'tokens') loadTokens();
  if (name === 'providers') loadCfg();
  if (name === 'settings') { loadCfg(); ocRefresh(); }
}

document.getElementById('nav').addEventListener('click', e => {
  const a = e.target.closest('a[data-tab]');
  if (!a) return;
  const tab = a.dataset.tab;
  const url = tab === 'overview' ? (BASE + '/admin') : (BASE + '/settings');
  history.pushState({}, '', url + (tab === 'overview' ? '' : '#'));
  showTab(tab);
});
window.addEventListener('popstate', () => showTab(currentTab()));

// ── 概览 ────────────────────────────────────────────────
let STATUS = null;
let epInited = false;

async function loadStatus() {
  try {
    const d = await api('/admin/api/status');
    STATUS = d;
    document.getElementById('sReqs').textContent = d.total_requests_24h;
    document.getElementById('sCost').textContent = '$' + (d.total_cost_24h || 0).toFixed(4);
    const live = d.providers.filter(p => p.enabled && p.has_key && !p.cooldown && !p.exhausted);
    document.getElementById('sLive').textContent = live.length + ' / ' + d.providers.length;
    document.getElementById('sStrat').textContent =
      d.strategy === 'cheapest' ? '省钱优先' : '稳定优先';

    document.getElementById('provBody').innerHTML = d.providers.map(p => {
      let st, cls;
      if (!p.enabled) { st = '已停用'; cls = 'd'; }
      else if (!p.has_key) { st = '缺密钥'; cls = 'd'; }
      else if (p.exhausted) { st = '额度用尽'; cls = 'r'; }
      else if (p.cooldown > 0) { st = '冷却 ' + p.cooldown + 's'; cls = 'r'; }
      else if (p.fails > 0) { st = '重试中'; cls = 'y'; }
      else { st = '正常'; cls = 'g'; }

      let quota;
      if (!p.free_only) quota = '<span class="sub">付费</span>';
      else if (p.free_quota_tokens > 0) {
        const pct = Math.min(100, p.used_tokens / p.free_quota_tokens * 100);
        const col = p.exhausted ? 'var(--err)' : (pct > 80 ? 'var(--warn)' : 'var(--ok)');
        quota = '<span style="color:' + col + '">' + p.used_tokens.toLocaleString() + '</span>'
          + '<span class="sub"> / ' + p.free_quota_tokens.toLocaleString() + '</span>';
      } else quota = '<span class="sub">免费不限</span>';

      const tag = p.type === 'opencode'
        ? ' <span class="tag d">opencode' + (p.opencode_mode === 'cli' ? '·cli' : '') + '</span>'
        : (p.protocol ? ' <span class="tag d">' + esc(p.protocol) + '</span>' : '');

      const btn = p.enabled
        ? '<button data-n="' + esc(p.name) + '" data-e="0" onclick="toggleProv(this)">停用</button>'
        : '<button data-n="' + esc(p.name) + '" data-e="1" onclick="toggleProv(this)">启用</button>';

      return '<tr>'
        + '<td><b>' + esc(p.name) + '</b>' + tag
        + '<div class="sub">' + esc(p.note || '') + '</div></td>'
        + '<td><span class="tag ' + cls + '">' + st + '</span></td>'
        + '<td class="mono clip" title="' + esc(p.model) + '">' + esc(p.model) + '</td>'
        + '<td class="nowrap">' + quota + '</td>'
        + '<td class="mono nowrap">' + p.price_in.toFixed(3) + ' / ' + p.price_out.toFixed(3) + '</td>'
        + '<td class="nowrap">' + p.requests_24h + '</td>'
        + '<td class="mono nowrap">$' + p.cost_24h.toFixed(4) + '</td>'
        + '<td class="nowrap">' + btn
        + ' <button data-n="' + esc(p.name) + '" onclick="probeProv(this)">自检</button></td>'
        + '<td><span class="err" title="' + esc(p.last_error) + '">'
        + (esc(p.last_error) || '-') + '</span></td>'
        + '</tr>';
    }).join('');

    if (!epInited) { epInited = true; loadEndpoint(d.port); }
    document.getElementById('sideFoot').textContent = '端口 ' + d.port;
  } catch (e) {
    document.getElementById('sReqs').textContent = '—';
  }
}

async function loadEndpoint(port) {
  const host = location.hostname;
  const pt = port || 18787;
  const direct = 'http://' + host + ':' + pt + '/v1';
  document.getElementById('epDirect').value = direct;
  document.getElementById('epLocal').value = 'http://127.0.0.1:' + pt + '/v1';
  document.getElementById('epGateway').value = BASE
    ? (location.origin + BASE + '/v1') : '（当前就是直连，无需网关）';
  try {
    const r = await fetch(BASE + '/v1/models', { headers: authHeaders() });
    const d = await r.json();
    const ids = (d.data || []).map(m => m.id);
    document.getElementById('epModels').value = ids.length ? ids.join(', ') : '（还没配置上游）';
    const first = ids[0] || 'deepseek-chat';
    document.getElementById('epCurl').value =
      'curl ' + direct + '/chat/completions -H "Content-Type: application/json" '
      + '-H "Authorization: Bearer <你的Key>" -d '
      + String.fromCharCode(39) + '{"model":"' + first
      + '","messages":[{"role":"user","content":"hi"}]}' + String.fromCharCode(39);
  } catch (e) { /* 忽略 */ }
}

async function toggleProv(el) {
  const name = el.dataset.n;
  const enabled = el.dataset.e === '1';
  await act('/admin/api/toggle', { name: name, enabled: enabled },
            (enabled ? '已启用 ' : '已停用 ') + name, 'Overview');
  loadStatus();
}

async function probeProv(el) {
  const name = el.dataset.n;
  msg('Overview', '正在自检 ' + name + ' …');
  const t0 = Date.now();
  try {
    const d = await api('/admin/api/probe', { method: 'POST', body: JSON.stringify({ name: name }) });
    const sec = ((Date.now() - t0) / 1000).toFixed(1);
    msg('Overview', d.ok
      ? '[通] ' + name + '  ' + sec + 's  ' + String(d.sample || '').slice(0, 60)
      : '[失败] ' + name + '  ' + sec + 's  ' + (d.error || ''));
  } catch (e) {
    msg('Overview', '[失败] ' + name + '  ' + e.message);
  }
  loadStatus();
}

function refreshAll() { loadStatus(); }

// ── 上游配置 ────────────────────────────────────────────
let CFG = { providers: [] };

const PRESETS = {
  openai: { name: 'openai-compatible', type: 'http', enabled: false, priority: 50,
    base_url: 'https://api.example.com/v1', model: '', no_auth: false,
    free_only: false, free_quota_tokens: 0, price_in: 0, price_out: 0,
    api_key_env: 'OPENAI_API_KEY',
    note: '任意 OpenAI 兼容接口：填地址 / 模型 / Key 即可' },
  openrouter: { name: 'openrouter', type: 'http', enabled: false, priority: 55,
    base_url: 'https://openrouter.ai/api/v1', model: '', no_auth: false,
    free_only: false, free_quota_tokens: 0, price_in: 0, price_out: 0,
    api_key_env: 'OPENROUTER_API_KEY', console_url: 'https://openrouter.ai/keys',
    note: 'OpenRouter：一个 Key 打通多家 provider' },
  ollama: { name: 'ollama', type: 'http', enabled: false, priority: 20,
    base_url: 'http://127.0.0.1:11434/v1', model: 'deepseek-r1:8b', no_auth: true,
    free_only: true, free_quota_tokens: 0, price_in: 0, price_out: 0,
    api_key_env: '', console_url: 'https://ollama.com/library',
    note: '本地 Ollama / vLLM / LM Studio：不需要 Key' },
  blank: { name: 'new-provider', type: 'http', enabled: false, priority: 100,
    base_url: 'https://api.example.com/v1', model: '', no_auth: false,
    free_only: true, free_quota_tokens: 0, price_in: 0, price_out: 0,
    api_key_env: 'NEW_PROVIDER_KEY', note: '' }
};

const CONSOLE_MAP = [
  ['openrouter.ai', 'https://openrouter.ai/keys'],
  ['api.deepseek.com', 'https://platform.deepseek.com/api_keys'],
  ['ark.cn-beijing.volces.com', 'https://console.volcengine.com/ark'],
  ['api.siliconflow.cn', 'https://cloud.siliconflow.cn/account/ak'],
  ['tokenhub.tencentmaas.com', 'https://console.cloud.tencent.com/tokenhub'],
  ['qianfan.baidubce.com', 'https://console.bce.baidu.com/qianfan/'],
  ['build.nvidia.com', 'https://build.nvidia.com/settings/api-keys'],
  ['modelarts-maas.com', 'https://console.huaweicloud.com/modelarts/'],
  ['11434', 'https://ollama.com/library'],
  ['opencode.ai', 'https://opencode.ai/console']
];

function guessConsole(p) {
  if (p.console_url) return p.console_url;
  const u = String(p.base_url || '');
  for (const [k, v] of CONSOLE_MAP) if (u.indexOf(k) >= 0) return v;
  return '';
}

function openUrl(u) {
  if (!u) { toast('没有可识别的控制台地址'); return; }
  window.open(u, '_blank', 'noopener');
}

async function loadCfg() {
  try {
    CFG = await api('/admin/api/config');
    renderProviders();
    renderSettings();
  } catch (e) {
    msg('Providers', '载入失败：' + e.message);
  }
}

function provCard(p, i) {
  const isOc = p.type === 'opencode';
  const cUrl = guessConsole(p);
  const badge = '<span class="tag d">'
    + (isOc ? ('opencode' + (p.opencode_mode === 'cli' ? '·cli' : ''))
      : (p.protocol || 'openai')) + '</span>';
  const link = cUrl
    ? '<a href="' + esc(cUrl) + '" target="_blank" rel="noopener">去获取 Key ↗</a>' : '';

  let h = '<div class="provcard" data-i="' + i + '">';
  h += '<div class="phd"><input data-f="name" value="' + esc(p.name)
    + '" style="font-weight:700"><span class="sub">' + badge + ' ' + link + '</span></div>';

  h += '<div class="field"><label>接口地址</label>'
    + '<input data-f="base_url" value="' + esc(p.base_url) + '">'
    + '<div class="hint">' + (isOc ? '本地 opencode server 地址，一般不用改'
      : 'OpenAI 兼容根地址，结尾不要带斜杠') + '</div></div>';

  h += '<div class="f2">'
    + '<div class="field"><label>默认模型</label>'
    + '<input data-f="model" value="' + esc(p.model) + '">'
    + '<div class="hint">客户端不点名时用这个</div></div>'
    + '<div class="field"><label>优先级</label>'
    + '<input type="number" data-f="priority" value="' + (p.priority || 100) + '">'
    + '<div class="hint">越小越先试</div></div></div>';

  h += '<div class="field"><label>更多模型（可选）</label>'
    + '<input data-f="models" value="' + esc((p.models || []).join(', ')) + '"'
    + ' placeholder="逗号分隔，客户端可以直接点名">'
    + '<div class="hint"><button type="button" onclick="discover(' + i + ')">'
    + '探测这个上游有哪些模型</button> <span id="dsc_' + i + '"></span></div>'
    + '<label class="chk" style="margin-top:8px">'
    + '<input type="checkbox" data-f="model_passthrough"'
    + (p.model_passthrough ? ' checked' : '') + '> 允许任意模型名（透传，不校验）</label>'
    + '<div class="hint">不勾时：客户端点了列表里没有的模型，网关会直接报错，'
    + '不会偷偷换成默认模型。</div></div>';

  h += '<div class="f2"><div class="field"><label>类型</label>'
    + '<select data-f="type" onchange="switchType(this)">'
    + '<option value="http"' + (!isOc ? ' selected' : '') + '>http（HTTP 上游）</option>'
    + '<option value="opencode"' + (isOc ? ' selected' : '') + '>opencode（本地）</option>'
    + '</select></div>';

  if (isOc) {
    h += '<div class="field"><label>调用方式</label>'
      + '<select data-f="opencode_mode">'
      + '<option value="cli"' + (p.opencode_mode === 'cli' ? ' selected' : '')
      + '>命令行（run，推荐）</option>'
      + '<option value="server"' + (p.opencode_mode !== 'cli' ? ' selected' : '')
      + '>服务端 API（serve）</option>'
      + '</select><div class="hint">命令行走客户端本体，免费模型只认这条路</div></div>';
  } else {
    h += '<div class="field"><label>协议</label>'
      + '<select data-f="protocol">'
      + '<option value=""' + (!p.protocol ? ' selected' : '') + '>OpenAI Chat</option>'
      + '<option value="responses"' + (p.protocol === 'responses' ? ' selected' : '') + '>OpenAI Responses</option>'
      + '<option value="anthropic"' + (p.protocol === 'anthropic' ? ' selected' : '') + '>Anthropic</option>'
      + '<option value="gemini"' + (p.protocol === 'gemini' ? ' selected' : '') + '>Gemini</option>'
      + '</select><div class="hint">网关按它转换请求与响应</div></div>';
  }
  h += '</div>';

  if (isOc) {
    h += '<div class="f2">'
      + '<div class="field"><label>providerID</label>'
      + '<input data-f="opencode_provider" value="' + esc(p.opencode_provider || 'opencode') + '"></div>'
      + '<div class="field"><label>agent</label>'
      + '<input data-f="opencode_agent" value="' + esc(p.opencode_agent || '') + '" placeholder="留空 = 默认">'
      + '<div class="hint">建议留空</div></div></div>';
  } else {
    h += '<div class="f2">'
      + '<div class="field"><label>密钥</label>'
      + '<input type="password" class="kval" data-env="' + esc(p.api_key_env) + '"'
      + ' placeholder="' + esc(p._key_source || '无') + '">'
      + '<div class="hint">变量名 <span class="mono">' + esc(p.api_key_env || '（未设置）')
      + '</span>　留空保存 = 不改动</div></div>'
      + '<div class="field"><label>量化</label>'
      + '<input data-f="quant" value="' + esc(p.quant || '') + '" placeholder="fp8 / fp4"></div></div>';
  }

  h += '<div class="field"><label>控制台地址</label><div class="inline">'
    + '<input data-f="console_url" value="' + esc(p.console_url) + '"'
    + ' placeholder="' + esc(cUrl || '留空则按接口自动识别') + '">'
    + '<button type="button" data-u="' + esc(cUrl) + '" onclick="openUrl(this.dataset.u)">打开</button>'
    + '</div></div>';

  h += '<div class="field"><label>免费额度（tokens）</label><div class="inline">'
    + '<input type="number" data-f="free_quota_tokens" value="'
    + (p.free_quota_tokens || 0) + '" style="max-width:180px">'
    + '<label class="chk"><input type="checkbox" data-f="free_only"'
    + (p.free_only ? ' checked' : '') + '> 免费上游</label>'
    + '</div><div class="hint">0 = 不限。填了额度，用尽后自动停用，不会转成付费。</div></div>';

  h += '<div class="f2">'
    + '<div class="field"><label>输入价 $/百万</label>'
    + '<input type="number" step="0.0001" data-f="price_in" value="' + (p.price_in || 0) + '"></div>'
    + '<div class="field"><label>输出价 $/百万</label>'
    + '<input type="number" step="0.0001" data-f="price_out" value="' + (p.price_out || 0) + '"></div></div>';

  h += '<div class="f2">'
    + '<div class="field"><label>模型名校验</label>'
    + '<input data-f="model_contains" value="' + esc(p.model_contains || '') + '" placeholder="留空 = 不校验">'
    + '<div class="hint">响应里的模型名必须包含它，防上游偷换</div></div>'
    + '<div class="field"><label>单独代理</label>'
    + '<input data-f="proxy" value="' + esc(p.proxy || '') + '" placeholder="留空 = 用全局">'
    + '<div class="hint">可填 socks5://host:port</div></div></div>';

  h += '<div class="field"><label>备注</label>'
    + '<input data-f="note" value="' + esc(p.note || '') + '"></div>';

  h += '<div class="field" style="margin-bottom:0;display:flex;gap:8px;align-items:center;flex-wrap:wrap">'
    + '<label class="chk"><input type="checkbox" data-f="enabled"' + (p.enabled ? ' checked' : '') + '> 启用</label>'
    + '<label class="chk"><input type="checkbox" data-f="no_auth"' + (p.no_auth ? ' checked' : '') + '> 免密钥</label>'
    + '<span style="flex:1"></span>'
    + '<button class="pri" onclick="saveOne(' + i + ')">保存这张</button>'
    + '<button data-n="' + esc(p.name) + '" onclick="probeByName(this.dataset.n)">自检</button>'
    + '<button class="danger" onclick="delProv(' + i + ')">删除</button>'
    + '</div>';

  h += '</div>';
  return h;
}

function renderProviders() {
  const list = CFG.providers || [];
  document.getElementById('provGrid').innerHTML = list.map(provCard).join('');
}

function renderSettings() {
  document.getElementById('setStrat').value = CFG.strategy || 'priority';
  document.getElementById('setCd').value = CFG.cooldown_seconds || 60;
  const pr = parseProxy(CFG.proxy || '');
  document.getElementById('setProxyScheme').value = pr.scheme;
  document.getElementById('setProxyAddr').value = pr.addr;
  const pm = document.getElementById('proxyMsg');
  if (pm) {
    pm.textContent = CFG.proxy_env
      ? ('⚠ 服务进程里还设着环境变量代理 ' + CFG.proxy_env + '，面板留空时会用它。要完全不用代理，填一个无效值也不行——请改 run.sh。')
      : '只有需要代理的上游才走它；国内直连的平台建议单独留空。';
  }
  document.getElementById('setTok').placeholder =
    CFG.auth_enabled ? '已设置（留空不改）' : '留空 = 免口令';
  const oc = CFG.opencode || {};
  document.getElementById('ocPort').value = oc.port || 4096;
  document.getElementById('ocBin').value = oc.bin || '';
  const ws = document.getElementById('ocWs');
  if (oc.workspace && oc.workspace !== oc.workspace_default) ws.value = oc.workspace;
  else { ws.value = ''; ws.placeholder = '默认：' + (oc.workspace_default || '数据目录下'); }
  document.getElementById('ocAuto').checked = oc.autostart !== false;
  const k = document.getElementById('ocKey');
  if (k) k.placeholder = oc.key_set ? '已设置（留空不改）' : '留空 = 未设置';
  document.getElementById('advShort').value = CFG.timeout_short || 120;
  document.getElementById('advFirst').value = CFG.timeout_first_byte || 120;
  document.getElementById('advIdle').value = CFG.timeout_stream_idle || 300;
  document.getElementById('advUA').value = CFG.user_agent || '';
  document.getElementById('advBlack').value = CFG.model_blacklist || '';
  document.getElementById('advWhite').value = CFG.model_whitelist || '';
  document.getElementById('advMap').value = CFG.model_mapping || '';
  document.getElementById('advSysMode').value = CFG.system_mode || 'passthrough';
  document.getElementById('advSysFile').value = CFG.system_file || '';
  document.getElementById('advSysText').value = CFG.system_text || '';
  document.getElementById('advRounds').value = CFG.keep_rounds || 0;
  document.getElementById('advLogSource').checked = CFG.log_source !== false;
  document.getElementById('effDefault').value = CFG.effort_default || 'max';
  document.getElementById('effMode').value = CFG.effort_mode || 'client';
  document.getElementById('sanOn').checked = !!CFG.sanitize;
  document.getElementById('sanScope').value = CFG.sanitize_scope || 'all';
  document.getElementById('sanWords').value = CFG.sanitize_words || '';
  document.getElementById('sanRepl').value = CFG.sanitize_replacement || '该服务';
}

function parseProxy(s) {
  s = String(s || '').trim();
  if (!s) return { scheme: '', addr: '' };
  const i = s.indexOf('://');
  if (i > 0) return { scheme: s.slice(0, i).toLowerCase(), addr: s.slice(i + 3) };
  return { scheme: 'http', addr: s };
}

function readCard(el) {
  const idx = Number(el.dataset.i);
  const src = (CFG.providers || [])[idx] || {};
  const o = Object.assign({}, src);
  delete o._has_key; delete o._key_source;
  el.querySelectorAll('[data-f]').forEach(inp => {
    const f = inp.dataset.f;
    if (inp.type === 'checkbox') o[f] = inp.checked;
    else if (f === 'models') {
      o[f] = String(inp.value || '').split(',').map(x => x.trim()).filter(Boolean);
    } else if (inp.type === 'number') o[f] = inp.value === '' ? 0 : Number(inp.value);
    else o[f] = inp.value;
  });
  o._source = src._source || 'main';
  return o;
}

function cardSecrets(el) {
  const out = {};
  el.querySelectorAll('.kval').forEach(inp => {
    const v = (inp.value || '').trim();
    if (v && inp.dataset.env) out[inp.dataset.env] = v;
  });
  return out;
}

async function saveOne(idx) {
  const el = document.querySelector('.provcard[data-i="' + idx + '"]');
  if (!el) return;
  const o = readCard(el);
  if (!String(o.name || '').trim()) { msg('Providers', '名称不能为空'); return; }
  msg('Providers', '保存中：' + o.name + ' …');
  try {
    const d = await api('/admin/api/save-provider', {
      method: 'POST',
      body: JSON.stringify({ provider: o, secrets: cardSecrets(el) })
    });
    if (d.ok) { CFG = d; renderProviders(); msg('Providers', '已保存：' + o.name); loadStatus(); }
    else msg('Providers', '保存失败：' + (d.error || ''));
  } catch (e) { msg('Providers', '保存失败：' + e.message); }
}

function collectAll() {
  const cards = [...document.querySelectorAll('.provcard')];
  const names = cards.map(el => {
    const inp = el.querySelector('[data-f="name"]');
    return inp ? inp.value.trim() : '';
  });
  if (names.some(n => !n)) {
    msg('Providers', '有卡片名称为空，先填上（第 ' + (names.indexOf('') + 1) + ' 张）');
    return null;
  }
  const dup = names.filter((n, i) => names.indexOf(n) !== i);
  if (dup.length) { msg('Providers', '名称重复：' + dup.join(', ')); return null; }

  const providers = cards.map(readCard);
  const secrets = {};
  cards.forEach(el => Object.assign(secrets, cardSecrets(el)));
  const sc = document.getElementById('setProxyScheme').value;
  const ad = (document.getElementById('setProxyAddr').value || '').trim();
  const body = {
    strategy: document.getElementById('setStrat').value,
    cooldown_seconds: Number(document.getElementById('setCd').value || 60),
    proxy: (sc && ad) ? (sc + '://' + ad) : '',
    providers: providers,
    secrets: secrets
  };
  const t = (document.getElementById('setTok').value || '').trim();
  if (t) body.gateway_token = t;
  return body;
}

async function saveAll() {
  const body = collectAll();
  if (!body) return;
  msg('Providers', '保存中…');
  try {
    const d = await api('/admin/api/config', { method: 'POST', body: JSON.stringify(body) });
    if (d.ok || d.providers) {
      CFG = d; renderProviders(); renderSettings();
      document.getElementById('setTok').value = '';
      const od = d.on_disk || {};
      msg('Providers', '已保存（磁盘：代理=' + (od.proxy || '空')
        + '，策略=' + (od.strategy || '-') + '）');
      loadStatus();
    } else msg('Providers', '保存失败：' + (d.error || ''));
  } catch (e) { msg('Providers', '保存失败：' + e.message); }
}

function addPreset(kind) {
  const t = PRESETS[kind] || PRESETS.blank;
  const p = JSON.parse(JSON.stringify(t));
  if (kind === 'blank') p.name = 'new-provider-' + ((CFG.providers || []).length + 1);
  p._source = 'main';
  p._key_source = p.no_auth ? '免密钥' : '无';
  CFG.providers = (CFG.providers || []).concat([p]);
  renderProviders();
  msg('Providers', '已添加模板，改完点「保存这张」');
}

function delProv(i) {
  CFG.providers.splice(i, 1);
  renderProviders();
  msg('Providers', '已从列表移除，点「保存全部」才真正写盘');
}

function switchType(el) {
  const card = el.closest('.provcard');
  if (!card) return;
  const idx = Number(card.dataset.i);
  CFG.providers[idx] = readCard(card);
  card.outerHTML = provCard(CFG.providers[idx], idx);
  msg('Providers', '已切换类型，记得保存');
}

async function discover(i) {
  const p = (CFG.providers || [])[i];
  const el = document.getElementById('dsc_' + i);
  if (!p || !el) return;
  el.textContent = '探测中…';
  try {
    const d = await api('/admin/api/discover', { method: 'POST', body: JSON.stringify({ name: p.name }) });
    if (!d.ok) { el.textContent = '失败：' + (d.error || ''); return; }
    const free = (d.models || []).filter(m => m.free).map(m => m.id);
    window._freeCache = window._freeCache || {};
    window._freeCache[i] = free;
    el.innerHTML = '共 ' + d.count + ' 个，免费 ' + free.length + ' 个 '
      + (free.length ? '<button type="button" onclick="fillFree(' + i + ')">填入免费模型</button>' : '');
  } catch (e) { el.textContent = '失败：' + e.message; }
}

function fillFree(i) {
  const free = (window._freeCache || {})[i] || [];
  const card = document.querySelector('.provcard[data-i="' + i + '"]');
  const inp = card ? card.querySelector('[data-f="models"]') : null;
  if (!inp) return;
  inp.value = free.join(', ');
  msg('Providers', '已填入 ' + free.length + ' 个免费模型，记得保存');
}

async function probeByName(name) {
  msg('Providers', '正在自检 ' + name + ' …');
  try {
    const d = await api('/admin/api/probe', { method: 'POST', body: JSON.stringify({ name: name }) });
    msg('Providers', d.ok ? '[通] ' + name + '  ' + (d.sample || '').slice(0, 50)
      : '[失败] ' + name + '  ' + (d.error || ''));
  } catch (e) { msg('Providers', '[失败] ' + e.message); }
}

async function testProxy() {
  const el = document.getElementById('proxyMsg');
  const sc = document.getElementById('setProxyScheme').value;
  const ad = (document.getElementById('setProxyAddr').value || '').trim();
  const proxy = (sc && ad) ? (sc + '://' + ad) : '';
  if (!proxy) { el.textContent = '先填代理地址'; return; }
  el.textContent = '测试中…';
  try {
    const d = await api('/admin/api/proxy-test', { method: 'POST', body: JSON.stringify({ proxy: proxy }) });
    el.textContent = d.ok
      ? ('通：HTTP ' + d.status + ' · ' + d.ms + 'ms · ' + (d.note || '') + '　别忘了点保存')
      : ('失败：' + (d.error || ''));
  } catch (e) { el.textContent = '失败：' + e.message; }
}

async function clearTok() {
  await act('/admin/api/set-settings', { gateway_token: '' }, '已清除口令', 'Providers');
  tokStore('gw_token', '');
  loadCfg();
}

async function saveOc() {
  const body = {
    opencode_bin: document.getElementById('ocBin').value,
    opencode_port: Number(document.getElementById('ocPort').value || 4096),
    opencode_workspace: document.getElementById('ocWs').value,
    opencode_autostart: document.getElementById('ocAuto').checked
  };
  const key = (document.getElementById('ocKey').value || '').trim();
  if (key) body.opencode_api_key = key;
  await act('/admin/api/config', body, 'OpenCode 设置已保存', 'Providers');
  loadCfg();
}

// ── OpenCode 运行时 ─────────────────────────────────────
async function ocRefresh() {
  try {
    const d = await api('/admin/api/opencode');
    ocPaint(d);
  } catch (e) { /* 忽略 */ }
}

function ocPaint(d) {
  const st = d.login_state || {};
  const cli = !!d.cli_mode;
  const setTag = (id, text, cls) => {
    const e = document.getElementById(id);
    if (e) { e.textContent = text; e.className = 'tag ' + cls; }
  };
  setTag('ocTagInstall', '安装 · ' + (d.installed ? (d.bin_version || '已装') : '未装'),
    d.installed ? 'g' : 'd');
  if (cli) {
    setTag('ocTagLogin', '登录 · 不需要', 'g');
    setTag('ocTagRun', '运行 · 不需要', 'g');
  } else {
    setTag('ocTagLogin', '登录 · ' + (st.state === 'done' ? '已登录'
      : (st.state === 'idle' ? '未登录' : '进行中')), st.state === 'done' ? 'g' : 'y');
    setTag('ocTagRun', '运行 · ' + (d.running ? ('pid ' + d.pid) : '未运行'),
      d.running ? 'g' : 'd');
  }

  const nodeTxt = d.node_version || (d.node ? '?' : '');
  document.getElementById('ocEnv').textContent =
    (nodeTxt ? ('Node ' + nodeTxt + ' · npm ' + (d.npm_version || '—') + ' · ')
      : (d.installed ? 'opencode 自带运行时（不需要 Node）· ' : 'Node 未找到 · '))
    + 'opencode ' + (d.bin ? ((d.bin_version || '?') + '　' + d.bin) : '未安装')
    + (d.mode_note ? ('　—　' + d.mode_note) : '');

  document.getElementById('ocLog').textContent =
    (d.last_error ? ('[错误] ' + d.last_error + ' | ') : '') + (d.log || '（暂无日志）');
  document.getElementById('ocLogin').textContent = d.login_log || '（暂无）';
  document.getElementById('ocOwnLog').textContent = d.oc_logs || '（暂无）';

  const gd = document.getElementById('ocGuide');
  if (gd && st.title && !cli) {
    let h = '<div class="card" style="padding:14px">';
    h += '<div style="font-weight:700">' + esc(st.title) + '</div>';
    h += '<div class="sub" style="margin-top:6px">' + esc(st.hint || '') + '</div>';
    if (st.url) {
      h += '<div class="inline" style="margin-top:10px">'
        + '<input id="ocUrl" readonly value="' + esc(st.url) + '">'
        + '<button data-c="ocUrl" data-l="授权链接" onclick="copyVal(this.dataset.c,this.dataset.l)">复制链接</button></div>';
    }
    const acts = st.actions || [];
    if (acts.length) {
      h += '<div style="margin-top:10px">';
      acts.forEach(a => {
        h += '<button class="pri" data-k="' + esc(a.id) + '" '
          + 'onclick="ocGuideAct(this.dataset.k)" style="margin:0 8px 8px 0">'
          + esc(a.label) + '</button>';
      });
      h += '</div>';
    }
    h += '</div>';
    gd.innerHTML = h;
  } else if (gd) {
    gd.innerHTML = cli
      ? '<div class="card" style="padding:14px"><div style="font-weight:700">'
        + '命令行模式：装好就能用</div>'
        + '<div class="sub" style="margin-top:6px">不启动服务、不登录也能跑免费模型。'
        + '上面「试一句」通了就说明可用。</div></div>'
      : '';
  }
}

let ocLast = '';
function ocGuideAct(id) {
  const ops = ['ping', 'diagnose', 'login', 'start', 'stop', 'install', 'login-log'];
  if (ops.indexOf(id) >= 0) ocAct(id);
  else ocSendKeys(id);
}

async function ocAct(action) {
  msg('Providers', '执行中：' + action + ' …');
  try {
    const d = await api('/admin/api/opencode', { method: 'POST', body: JSON.stringify({ action: action }) });
    ocLast = (d.ok ? '[成功] ' : '[失败] ') + (d.message || '');
    msg('Providers', ocLast);
    if (action === 'diagnose' && d.diag) {
      document.getElementById('ocOwnLog').textContent = JSON.stringify(d.diag, null, 2);
    }
    ocPaint(d);
  } catch (e) { msg('Providers', '失败：' + e.message); }
}

async function ocSendKeys(keys) {
  try {
    const d = await api('/admin/api/opencode', {
      method: 'POST',
      body: JSON.stringify({ action: 'login-keys', keys: keys })
    });
    msg('Providers', (d.ok ? '[成功] ' : '[失败] ') + (d.message || ''));
    ocPaint(d);
  } catch (e) { msg('Providers', '失败：' + e.message); }
}

// ── 令牌 ────────────────────────────────────────────────
async function loadTokens() {
  try {
    const d = await api('/admin/api/tokens');
    document.getElementById('tokHint').textContent = d.open_mode
      ? '⚠ 全开放模式：任何人能访问就能直接调 /v1。建议设口令或至少建一个令牌。'
      : (d.master ? '主口令可用；下面是给其他人用的独立令牌'
        : '当前只接受下面这些令牌');
    const rows = (d.tokens || []).map(t => {
      const q = t.quota_tokens > 0 ? t.quota_tokens.toLocaleString() : '不限';
      return '<tr>'
        + '<td>' + esc(t.name) + '</td>'
        + '<td class="mono"><span class="kv" data-id="' + esc(t.id) + '">'
        + esc(t.key_masked) + '</span> '
        + '<button data-id="' + esc(t.id) + '" onclick="tokReveal(this)">显示</button> '
        + '<button data-id="' + esc(t.id) + '" onclick="tokCopy(this)">复制</button></td>'
        + '<td class="mono">' + t.used_tokens.toLocaleString() + ' / ' + q + '</td>'
        + '<td><span class="tag ' + (t.enabled ? 'g' : 'd') + '">'
        + (t.enabled ? '启用' : '停用') + '</span></td>'
        + '<td><button data-id="' + esc(t.id) + '" data-on="' + (t.enabled ? '0' : '1')
        + '" onclick="tokToggle(this)">' + (t.enabled ? '停用' : '启用') + '</button> '
        + '<button class="danger" data-id="' + esc(t.id) + '" onclick="tokDelete(this)">删除</button></td>'
        + '</tr>';
    }).join('');
    document.getElementById('tokBody').innerHTML =
      rows || '<tr><td colspan="5" class="sub">还没有令牌</td></tr>';
  } catch (e) { /* 忽略 */ }
}

async function createTok() {
  const name = (document.getElementById('newTokName').value || '').trim() || '未命名';
  const quota = Number(document.getElementById('newTokQuota').value || 0);
  try {
    const d = await api('/admin/api/tokens', {
      method: 'POST',
      body: JSON.stringify({ action: 'create', name: name, quota_tokens: quota })
    });
    if (d.ok && d.token) {
      toast('已创建（只显示一次）：' + d.token.key);
      document.getElementById('newTokName').value = '';
      document.getElementById('newTokQuota').value = '';
    } else toast('创建失败：' + (d.error || ''));
  } catch (e) { toast('创建失败：' + e.message); }
  loadTokens();
}

async function tokToggle(el) {
  await api('/admin/api/tokens', {
    method: 'POST',
    body: JSON.stringify({ action: 'toggle', id: el.dataset.id, enabled: el.dataset.on === '1' })
  }).catch(() => {});
  loadTokens();
}

async function tokDelete(el) {
  await api('/admin/api/tokens', {
    method: 'POST',
    body: JSON.stringify({ action: 'delete', id: el.dataset.id })
  }).catch(() => {});
  loadTokens();
}

async function tokReveal(el) {
  const id = el.dataset.id;
  const span = document.querySelector('.kv[data-id="' + id + '"]');
  if (!span) return;
  if (span.dataset.shown === '1') {
    span.textContent = span.dataset.masked || '';
    span.dataset.shown = '0';
    el.textContent = '显示';
    return;
  }
  try {
    const d = await api('/admin/api/tokens', {
      method: 'POST', body: JSON.stringify({ action: 'reveal', id: id })
    });
    if (d.ok) {
      span.dataset.masked = span.textContent;
      span.textContent = d.key;
      span.dataset.shown = '1';
      el.textContent = '隐藏';
    } else toast('无法显示：' + (d.error || ''));
  } catch (e) { toast('失败：' + e.message); }
}

async function tokCopy(el) {
  try {
    const d = await api('/admin/api/tokens', {
      method: 'POST', body: JSON.stringify({ action: 'reveal', id: el.dataset.id })
    });
    if (d.ok) copyText(d.key, '完整 Key');
    else toast('无法复制：' + (d.error || ''));
  } catch (e) { toast('失败：' + e.message); }
}

async function verifyKey() {
  const el = document.getElementById('verifyMsg');
  const key = (document.getElementById('verifyKey').value || '').trim();
  if (!key) { el.textContent = '先粘贴一把 Key'; return; }
  try {
    const d = await api('/admin/api/verify-key', { method: 'POST', body: JSON.stringify({ key: key }) });
    el.textContent = d.ok
      ? ('可用：' + d.kind + (d.name ? '「' + d.name + '」' : '')
        + (d.quota_tokens ? ('（已用 ' + d.used_tokens + '/' + d.quota_tokens + '）') : '')
        + (d.note ? '　' + d.note : ''))
      : ('不可用：' + (d.error || ''));
  } catch (e) { el.textContent = '失败：' + e.message; }
}

// ── 日志 ────────────────────────────────────────────────
let autoTimer = null;

let LOGROWS = [];

async function loadUsage() {
  const sel = document.getElementById('logLimit');
  const n = sel ? sel.value : 300;
  try {
    const d = await api('/admin/api/logs?kind=usage&limit=' + n);
    if (!d.ok) { document.getElementById('logMsg').textContent = '读取失败：' + (d.error || ''); return; }
    LOGROWS = d.rows || [];
    renderLog();
  } catch (e) { /* 忽略 */ }
}

function fmtTok(n) {
  if (!n) return '0';
  if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
  return String(n);
}

function renderLog() {
  const f = document.getElementById('logFilter').value;
  const q = (document.getElementById('logSearch').value || '').trim().toLowerCase();
  let rows = LOGROWS;
  if (f === 'ok') rows = rows.filter(r => r.ok);
  if (f === 'fail') rows = rows.filter(r => !r.ok);
  if (q) {
    rows = rows.filter(r => (r.client_ip + ' ' + r.user_agent + ' ' + r.model
      + ' ' + r.request_id + ' ' + r.provider + ' ' + r.token).toLowerCase().indexOf(q) >= 0);
  }

  // 顶部汇总
  const done = LOGROWS.length;
  const okN = LOGROWS.filter(r => r.ok).length;
  const okRate = done ? (okN / done * 100).toFixed(1) : '0.0';
  const avg = done ? (LOGROWS.reduce((a, r) => a + (r.ms || 0), 0) / done / 1000) : 0;
  const tok = LOGROWS.reduce((a, r) => a + (r.tokens_in || 0) + (r.tokens_out || 0), 0);
  const card = (k, v, sub) => '<div class="card stat"><div class="k">' + k
    + '</div><div class="v">' + v + '</div>'
    + (sub ? '<div class="sub">' + sub + '</div>' : '') + '</div>';
  document.getElementById('logCards').innerHTML =
    card('已完成', done) + card('成功率', okRate + '%')
    + card('平均耗时', avg.toFixed(2) + 's') + card('Token 合计', fmtTok(tok));

  document.getElementById('logBody').innerHTML = rows.map(r => {
    const cls = r.ok ? 'g' : 'r';
    const eff = r.effort ? ('→' + r.effort) : '';
    const cache = r.tokens_in
      ? ('<span style="color:' + (r.cache_rate >= 50 ? 'var(--ok)' : 'var(--dim)') + '">'
        + r.cache_rate + '%</span><div class="sub">命中 ' + fmtTok(r.cache_hit)
        + ' / 未命中 ' + fmtTok(r.cache_miss) + '</div>')
      : '-';
    const speed = r.tps ? r.tps + ' t/s' : '-';
    const ua = r.user_agent ? (r.user_agent.length > 26
      ? r.user_agent.slice(0, 26) + '…' : r.user_agent) : '-';
    return '<tr>'
      + '<td class="nowrap mono">' + r.time + '</td>'
      + '<td><span class="tag ' + cls + '">' + r.status + ' '
      + (r.ok ? '成功' : '失败') + '</span>'
      + (r.err ? ' <span class="err" title="' + esc(r.err) + '">!</span>' : '') + '</td>'
      + '<td class="mono clip" title="' + esc(r.model) + '">' + esc(r.model) + '</td>'
      + '<td class="sub clip">' + esc(r.token) + '</td>'
      + '<td class="sub nowrap">' + eff + '</td>'
      + '<td class="nowrap">' + (r.stream ? 'SSE' : 'JSON')
      + '<div class="sub">' + esc(r.provider) + '</div></td>'
      + '<td class="nowrap">' + (r.first_byte_ms ? (r.first_byte_ms / 1000).toFixed(1) + 's' : '-') + '</td>'
      + '<td class="nowrap">' + (r.ms / 1000).toFixed(2) + 's</td>'
      + '<td class="nowrap">' + speed + '</td>'
      + '<td class="nowrap">↑' + fmtTok(r.tokens_in) + '<br>↓' + fmtTok(r.tokens_out) + '</td>'
      + '<td class="nowrap">' + cache + '</td>'
      + '<td class="mono nowrap">' + esc(r.client_ip || '-') + '</td>'
      + '<td class="sub clip" title="' + esc(r.user_agent) + '">' + esc(ua) + '</td>'
      + '<td class="mono sub clip" title="' + esc(r.request_id) + '">'
      + esc(r.request_id || '-') + '</td>'
      + '</tr>';
  }).join('') || '<tr><td colspan="14" class="sub">还没有调用记录</td></tr>';

  document.getElementById('logMsg').textContent =
    '显示 ' + rows.length + ' / ' + done + ' 条';
}

async function loadErrors() {
  try {
    const d = await api('/admin/api/errors');
    if (!d.ok) { document.getElementById('errSummary').textContent = '读取失败'; return; }
    document.getElementById('errSummary').textContent =
      '共 ' + d.rows.length + ' 类，' + d.kinds + ' 种类型';
    document.getElementById('errBody').innerHTML = (d.rows || []).map(r =>
      '<tr><td>' + esc(r.provider) + '</td>'
      + '<td><span class="tag r">' + esc(r.kind) + '</span></td>'
      + '<td class="mono">' + r.status + '</td>'
      + '<td>' + r.count + '</td>'
      + '<td class="nowrap mono">' + r.last_time + '</td>'
      + '<td class="sub clip" title="' + esc(r.sample) + '">' + esc(r.sample) + '</td>'
      + '</tr>').join('')
      || '<tr><td colspan="6" class="sub">没有失败记录</td></tr>';
  } catch (e) { /* 忽略 */ }
}

async function loadGwLog() {
  try {
    const d = await api('/admin/api/logs?kind=gateway&limit=300');
    document.getElementById('gwLogPath').textContent = d.path || '';
    document.getElementById('gwLog').textContent = d.text || '（暂无）';
  } catch (e) { /* 忽略 */ }
}

function toggleAuto() {
  const on = document.getElementById('autoLog').checked;
  if (autoTimer) { clearInterval(autoTimer); autoTimer = null; }
  if (on) autoTimer = setInterval(() => { loadUsage(); loadErrors(); loadGwLog(); }, 5000);
}


// ── 统计 ────────────────────────────────────────────────
async function loadStats() {
  const days = document.getElementById('statDays').value || 7;
  try {
    const d = await api('/admin/api/stats?days=' + days);
    if (!d.ok) { msg('Stats', '读取失败：' + (d.error || '')); return; }
    const t = d.totals || {};
    const card = (k, v) => '<div class="card stat"><div class="k">' + k + '</div>'
      + '<div class="v">' + v + '</div></div>';
    document.getElementById('statCards').innerHTML =
      card('请求总数', t.requests || 0)
      + card('输入 tokens', (t.tokens_in || 0).toLocaleString())
      + card('输出 tokens', (t.tokens_out || 0).toLocaleString())
      + card('花费', '$' + (t.cost || 0).toFixed(4));

    document.getElementById('statDaily').innerHTML = (d.daily || []).map(r =>
      '<tr><td class="mono">' + r.day + '</td><td>' + r.requests + '</td>'
      + '<td>' + r.ok + '</td><td>' + r.tokens_in.toLocaleString() + '</td>'
      + '<td>' + r.tokens_out.toLocaleString() + '</td>'
      + '<td class="mono">$' + r.cost.toFixed(4) + '</td></tr>').join('')
      || '<tr><td colspan="6" class="sub">还没有数据</td></tr>';

    document.getElementById('statProv').innerHTML = (d.providers || []).map(r =>
      '<tr><td>' + esc(r.provider) + '</td><td>' + r.requests + '</td>'
      + '<td>' + (r.success_rate == null ? '-' : r.success_rate + '%') + '</td>'
      + '<td>' + r.tokens_in.toLocaleString() + '</td>'
      + '<td>' + r.tokens_out.toLocaleString() + '</td>'
      + '<td class="mono">$' + r.cost.toFixed(4) + '</td>'
      + '<td>' + r.avg_ms + 'ms</td></tr>').join('')
      || '<tr><td colspan="7" class="sub">还没有数据</td></tr>';
  } catch (e) { msg('Stats', '读取失败：' + e.message); }
}

// ── 脱敏 ────────────────────────────────────────────────
async function saveSanitize() {
  const body = {
    sanitize: document.getElementById('sanOn').checked,
    sanitize_scope: document.getElementById('sanScope').value,
    sanitize_words: document.getElementById('sanWords').value,
    sanitize_replacement: document.getElementById('sanRepl').value || '该服务'
  };
  try {
    const d = await api('/admin/api/config', { method: 'POST', body: JSON.stringify(body) });
    if (d.ok || d.providers) {
      CFG = d;
      msg('San', '已保存：' + (body.sanitize ? '开启' : '关闭')
        + '，作用域 ' + body.sanitize_scope);
    } else msg('San', '保存失败：' + (d.error || ''));
  } catch (e) { msg('San', '保存失败：' + e.message); }
}


// ── 模型能力 ────────────────────────────────────────────
let CAPS = [];

async function loadCaps() {
  try {
    const d = await api('/admin/api/caps');
    if (!d.ok) { msg('Models', '读取失败：' + (d.error || '')); return; }
    CAPS = d.models || [];
    document.getElementById('capsSummary').textContent =
      '共 ' + d.count + ' 个模型，' + d.vision_count + ' 个支持图片，'
      + d.with_reasoning + ' 个支持多档思考';

    const warn = document.getElementById('effWarn');
    const bad = d.unsupported_default || [];
    warn.textContent = bad.length
      ? ('⚠ 有 ' + bad.length + ' 个模型不支持「' + d.default_effort + '」，会自动落到：'
        + bad.slice(0, 6).map(x => x.model + '→' + (x.falls_to || 'off')).join('、')
        + (bad.length > 6 ? ' …' : '') + '（可在下表逐条指定）')
      : '';

    document.getElementById('capsBody').innerHTML = CAPS.map((m, i) => {
      const ctx = m.context ? (m.context >= 1000
        ? Math.round(m.context / 1000) + 'K' : m.context) : '-';
      const tiers = (m.reasoning || []).join(' / ') || 'off';
      const opts = ['off', 'low', 'medium', 'high', 'max'].map(t => {
        const sel = (m.override && m.override.effort === t) ? ' selected' : '';
        return '<option value="' + t + '"' + sel + '>' + t + '</option>';
      }).join('');
      return '<tr>'
        + '<td class="mono clip" title="' + esc(m.model) + '">' + esc(m.model)
        + (m.known ? '' : ' <span class="tag y">未知</span>') + '</td>'
        + '<td class="sub">' + esc(m.provider) + '</td>'
        + '<td><input class="capin" type="number" value="' + (m.context || '')
        + '" data-m="' + esc(m.model) + '" data-k="context"></td>'
        + '<td><input type="checkbox"' + (m.vision ? ' checked' : '')
        + ' data-m="' + esc(m.model) + '" data-k="vision"></td>'
        + '<td class="sub">' + esc(tiers) + '</td>'
        + '<td><select class="capin" data-m="' + esc(m.model) + '" data-k="effort">'
        + '<option value="">跟随默认</option>' + opts + '</select></td>'
        + '</tr>';
    }).join('') || '<tr><td colspan="6" class="sub">还没有模型</td></tr>';
  } catch (e) { msg('Models', '读取失败：' + e.message); }
}

function collectCaps() {
  const out = {};
  document.querySelectorAll('#capsBody [data-m]').forEach(el => {
    const m = el.dataset.m, k = el.dataset.k;
    out[m] = out[m] || {};
    if (k === 'vision') out[m].vision = el.checked;
    else if (k === 'context') out[m].context = Number(el.value || 0);
    else out[m].effort = el.value;
  });
  Object.keys(out).forEach(m => {
    if (out[m].context === 0) delete out[m].context;
    if (!out[m].effort) delete out[m].effort;
    if (!Object.keys(out[m]).length) delete out[m];
  });
  return out;
}

async function saveEffort() {
  const body = {
    effort_default: document.getElementById('effDefault').value,
    effort_mode: document.getElementById('effMode').value
  };
  try {
    const d = await api('/admin/api/config', { method: 'POST', body: JSON.stringify(body) });
    if (d.providers) { CFG = d; msg('Models', '已保存档位策略'); loadCaps(); }
    else msg('Models', '保存失败：' + (d.error || ''));
  } catch (e) { msg('Models', '保存失败：' + e.message); }
}

async function saveAdvanced() {
  const body = {
    timeout_short: Number(document.getElementById('advShort').value || 120),
    timeout_first_byte: Number(document.getElementById('advFirst').value || 120),
    timeout_stream_idle: Number(document.getElementById('advIdle').value || 300),
    user_agent: document.getElementById('advUA').value,
    model_blacklist: document.getElementById('advBlack').value,
    model_whitelist: document.getElementById('advWhite').value,
    model_mapping: document.getElementById('advMap').value,
    system_mode: document.getElementById('advSysMode').value,
    system_file: document.getElementById('advSysFile').value,
    system_text: document.getElementById('advSysText').value,
    keep_rounds: Number(document.getElementById('advRounds').value || 0),
    log_source: document.getElementById('advLogSource').checked,
    model_caps: collectCaps()
  };
  try {
    const d = await api('/admin/api/config', { method: 'POST', body: JSON.stringify(body) });
    if (d.providers) { CFG = d; msg('Advanced', '已保存'); }
    else msg('Advanced', '保存失败：' + (d.error || ''));
  } catch (e) { msg('Advanced', '保存失败：' + e.message); }
}

// ── 启动 ────────────────────────────────────────────────
(function boot() {
  showTab(currentTab());
  loadStatus();
  setInterval(() => { if (document.getElementById('tab-overview').classList.contains('on')) loadStatus(); }, 8000);
})();
