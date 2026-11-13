"""HTML pages served by web_app.

The pages are pure functions of their arguments; the stateful HTTP layer
(uploads, jobs, history, settings storage) stays in web_app.py.
"""

from __future__ import annotations

import html
import json

from lib import fleet_history
from lib.agent.config import CONFIG_PATH
from lib.i18n import get_locale, t


def build_settings_html(config, error: str = "") -> str:
    data = config.public_dict()
    lang_code = "zh-CN" if get_locale() == "zh" else "en"
    key_hint = (t("web.settings_key_hint", hint=data["key_hint"])
                if data["has_key"] else "")
    checked = lambda value: " checked" if value else ""
    options = "".join(
        f'<option value="{p}"'
        + (' selected' if p == data.get("provider") else "")
        + f'>{p}</option>'
        for p in ("anthropic", "openai"))
    return f"""<!DOCTYPE html>
<html lang="{lang_code}">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ArduScope - {html.escape(t('web.settings_title'))}</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
background:#1b1f24;color:#e6e6e6;margin:0;padding:24px}}
.card{{max-width:640px;margin:0 auto;background:#24292f;border-radius:10px;
padding:24px}}
h1{{font-size:20px;margin:0 0 18px}}
label{{display:block;margin:12px 0 4px;font-size:13px;color:#b8c0c8}}
input[type=text],input[type=password],select{{width:100%;box-sizing:border-box;
padding:8px;border-radius:6px;border:1px solid #3d444d;background:#0d1117;
color:#e6e6e6;font-size:14px}}
.row{{display:flex;gap:12px;margin-top:18px;align-items:center;flex-wrap:wrap}}
button{{padding:8px 14px;border-radius:6px;border:0;cursor:pointer;font-size:14px}}
.primary{{background:#2f81f7;color:#fff}} .secondary{{background:#30363d;color:#e6e6e6}}
.msg{{margin-top:14px;font-size:13px;min-height:18px}}
.err{{color:#f85149}} .ok{{color:#3fb950}} .hint{{font-size:12px;color:#8b949e;margin-top:6px}}
.check{{display:flex;align-items:center;gap:8px;margin:8px 0;font-size:13px}}
a{{color:#58a6ff}}
</style>
</head>
<body>
<div class="card">
<h1>{html.escape(t('web.settings_title'))}</h1>
<p class="hint">{html.escape(t('web.settings_hint', path=CONFIG_PATH))}</p>
<label for="provider">{html.escape(t('web.settings_provider'))}</label>
<select id="provider">{options}</select>
<div class="hint">{html.escape(t('web.settings_provider_hint'))}</div>
<label for="base_url">{html.escape(t('web.settings_base_url'))}</label>
<input type="text" id="base_url" value="{html.escape(data.get('base_url', ''))}">
<label for="model">{html.escape(t('web.settings_model'))}</label>
<input type="text" id="model" value="{html.escape(data.get('model', ''))}">
<label for="api_key">{html.escape(t('web.settings_key'))}</label>
<input type="password" id="api_key" placeholder="{html.escape(key_hint)}">
<div class="check"><input type="checkbox" id="allow_gps"{checked(data['allow_gps'])}>
<label for="allow_gps" style="margin:0">{html.escape(t('web.settings_allow_gps'))}</label></div>
<div class="check"><input type="checkbox" id="allow_serial"{checked(data['allow_serial'])}>
<label for="allow_serial" style="margin:0">{html.escape(t('web.settings_allow_serial'))}</label></div>
<div class="check"><input type="checkbox" id="allow_text"{checked(data['allow_text'])}>
<label for="allow_text" style="margin:0">{html.escape(t('web.settings_allow_text'))}</label></div>
<div class="check"><input type="checkbox" id="clear_key">
<label for="clear_key" style="margin:0">{html.escape(t('web.settings_clear_key'))}</label></div>
<div class="row">
<button class="primary" onclick="saveSettings()">{html.escape(t('web.settings_save'))}</button>
<button class="secondary" onclick="testSettings()">{html.escape(t('web.settings_test'))}</button>
<a href="/">{html.escape(t('web.settings_back'))}</a>
</div>
<div class="msg" id="msg">{html.escape(error)}</div>
</div>
<script>
const I18N = {json.dumps({
    "saved": t("web.settings_saved"),
    "test_ok": t("web.settings_test_ok"),
    "test_failed": t("web.settings_test_failed", error="{error}"),
    "error": t("web.settings_error", error="{error}"),
    "testing": t("web.settings_testing"),
}, ensure_ascii=False)};
function values() {{
  return {{
    provider: document.getElementById('provider').value,
    base_url: document.getElementById('base_url').value.trim(),
    model: document.getElementById('model').value.trim(),
    api_key: document.getElementById('api_key').value,
    allow_gps: document.getElementById('allow_gps').checked,
    allow_serial: document.getElementById('allow_serial').checked,
    allow_text: document.getElementById('allow_text').checked,
    egress_ack: true,
    clear_key: document.getElementById('clear_key').checked,
  }};
}}
function show(text, ok) {{
  const el = document.getElementById('msg');
  el.textContent = text; el.className = 'msg ' + (ok ? 'ok' : 'err');
}}
async function post(url) {{
  const r = await fetch(url, {{method:'POST', headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify(values())}});
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || r.status);
  return data;
}}
async function saveSettings() {{
  try {{
    await post('/api/settings');
    document.getElementById('api_key').value = '';
    show(I18N.saved, true);
  }}
  catch (e) {{ show(I18N.error.replace('{error}', e.message), false); }}
}}
async function testSettings() {{
  show(I18N.testing, true);
  try {{
    const data = await post('/api/settings/test');
    show(data.ok ? I18N.test_ok : I18N.test_failed.replace('{error}', data.error || ''), data.ok);
  }} catch (e) {{ show(I18N.test_failed.replace('{error}', e.message), false); }}
}}
</script>
</body>
</html>"""


def build_index_html(max_upload_bytes: int, allow_settings: bool) -> str:
    """Build the index page HTML with i18n strings."""
    from lib.i18n import get_locale
    lang_code = "zh-CN" if get_locale() == "zh" else "en"
    return (r"""<!DOCTYPE html>
<html lang="""
    + f'"{lang_code}"'
    + r""">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ArduScope</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>&#x1f4ca;</text></svg>">
<style>
/* ---- Theme Variables ---- */
:root{
    --bg:#f0f2f5;
    --card-bg:#fff;
    --text:#333;
    --text-secondary:#606266;
    --text-muted:#909399;
    --border:#dcdfe6;
    --shadow:rgba(0,0,0,.08);
    --header-bg:linear-gradient(135deg,#1a1a2e 0%,#16213e 100%);
    --tag-bg:#f0f2f5;
    --tag-text:#666;
    --track-bg:#ebeef5;
    --hover-bg:#ecf5ff;
    --dashed-border:#c0c4cc;
    --btn-secondary-bg:#f0f2f5;
    --btn-secondary-hover:#e4e7ed;
    --divider:#f0f2f5;
    --input-bg:#fff;
}
[data-theme="dark"]{
    --bg:#1a1a2e;
    --card-bg:#252540;
    --text:#e0e0e0;
    --text-secondary:#a0a0b0;
    --text-muted:#707080;
    --border:#3a3a50;
    --shadow:rgba(0,0,0,.3);
    --header-bg:linear-gradient(135deg,#0d0d1a 0%,#0f1525 100%);
    --tag-bg:#2a2a45;
    --tag-text:#a0a0b0;
    --track-bg:#2a2a45;
    --hover-bg:#2a2a50;
    --dashed-border:#3a3a50;
    --btn-secondary-bg:#2a2a45;
    --btn-secondary-hover:#35355a;
    --divider:#2a2a45;
    --input-bg:#1e1e35;
}

/* ---- Reset & Base ---- */
*{margin:0;padding:0;box-sizing:border-box}
body{
    font-family:"PingFang SC","Microsoft YaHei","Noto Sans SC",
                -apple-system,BlinkMacSystemFont,sans-serif;
    background:var(--bg);color:var(--text);min-height:100vh;
}

/* ---- Header ---- */
.header{
    background:var(--header-bg);
    color:#fff;padding:20px 0;text-align:center;position:relative;
}
.header h1{font-size:22px}
.header p{font-size:13px;color:rgba(255,255,255,.6);margin-top:4px}
.lang-switch{
    position:absolute;top:16px;right:20px;
    display:flex;gap:4px;
}
.lang-switch button{
    background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.25);
    color:rgba(255,255,255,.7);padding:4px 10px;border-radius:4px;
    font-size:12px;cursor:pointer;transition:all .2s;
}
.lang-switch button:hover{background:rgba(255,255,255,.2)}
.lang-switch button.active{
    background:rgba(255,255,255,.25);color:#fff;
    border-color:rgba(255,255,255,.5);font-weight:600;
}
.lang-switch a.nav-fleet{
    background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.25);
    color:rgba(255,255,255,.7);padding:4px 10px;border-radius:4px;
    font-size:12px;text-decoration:none;transition:all .2s;
}
.lang-switch a.nav-fleet:hover{background:rgba(255,255,255,.2);color:#fff}

/* ---- Layout ---- */
.main{max-width:900px;margin:24px auto;padding:0 16px}

/* ---- Template bar ---- */
.template-bar{
    display:flex;align-items:center;gap:10px;
    margin-bottom:12px;padding:10px 16px;
    background:var(--card-bg);border-radius:8px;box-shadow:0 1px 3px var(--shadow);
    font-size:13px;color:var(--text-secondary);
}
.template-bar select{
    flex:1;max-width:320px;padding:6px 10px;border:1px solid var(--border);
    border-radius:4px;font-size:13px;color:var(--text);background:var(--input-bg);
    cursor:pointer;
}
.template-bar select:focus{outline:none;border-color:#409eff}
.template-bar .agent-check{
    display:flex;align-items:center;gap:6px;white-space:nowrap;
    cursor:pointer;color:var(--text-secondary);font-size:13px;
}
.template-bar .agent-check input{cursor:pointer}

/* ---- Upload zone ---- */
.upload-zone{
    border:2px dashed var(--dashed-border);border-radius:12px;
    padding:48px 24px;text-align:center;
    background:var(--card-bg);cursor:pointer;transition:all .2s;
    margin-bottom:24px;
}
.upload-zone:hover,.upload-zone.dragover{border-color:#409eff;background:var(--hover-bg)}
.upload-zone .icon{font-size:48px;margin-bottom:12px}
.upload-zone .hint{color:var(--text-muted);font-size:14px}
.upload-zone .hint2{color:var(--dashed-border);font-size:12px;margin-top:8px}
.upload-zone input{display:none}

/* ---- Job cards ---- */
.job-card{
    background:var(--card-bg);border-radius:8px;padding:16px 20px;
    margin-bottom:12px;box-shadow:0 1px 3px var(--shadow);
    border-left:4px solid var(--dashed-border);transition:border-color .3s;
}
.job-card.status-running{border-left-color:#409eff}
.job-card.status-done{border-left-color:#67c23a}
.job-card.status-error{border-left-color:#f56c6c}
.job-header{display:flex;align-items:center;gap:10px;margin-bottom:6px}
.job-filename{font-weight:600;font-size:15px}
.vehicle-tag{
    font-size:11px;background:var(--tag-bg);padding:2px 8px;
    border-radius:3px;color:var(--tag-text);
}

/* ---- Progress bar ---- */
.progress-track{
    height:6px;background:var(--track-bg);border-radius:3px;
    overflow:hidden;margin:8px 0 4px;
}
.progress-fill{
    height:100%;background:#409eff;border-radius:3px;
    transition:width .4s ease;
}
.progress-fill.active{
    background:linear-gradient(90deg,#409eff,#66b1ff,#409eff);
    background-size:200% 100%;animation:shimmer 1.5s infinite;
}
@keyframes shimmer{
    0%{background-position:200% 0}100%{background-position:-200% 0}
}
.progress-text{font-size:12px;color:var(--text-muted)}

/* ---- Analyzer dots ---- */
.analyzer-dots{display:flex;gap:5px;margin:8px 0;flex-wrap:wrap}
.dot{
    width:10px;height:10px;border-radius:50%;
    display:inline-block;cursor:help;
}

/* ---- Error ---- */
.error-msg{color:#f56c6c;font-size:13px;margin-top:4px}

/* ---- Actions ---- */
.job-actions{display:flex;align-items:center;gap:10px;margin-top:10px}
.btn{
    display:inline-block;padding:6px 16px;border-radius:4px;
    font-size:13px;text-decoration:none;cursor:pointer;
    border:none;transition:background .2s;
}
.btn-primary{background:#409eff;color:#fff}
.btn-primary:hover{background:#337ecc}
.btn-secondary{background:var(--btn-secondary-bg);color:var(--text-secondary)}
.btn-secondary:hover{background:var(--btn-secondary-hover)}
.btn-success{background:#67c23a;color:#fff}
.btn-success:hover{background:#529b2e}
.btn:disabled{opacity:.5;cursor:not-allowed}
.compare-check{
    display:flex;align-items:center;gap:4px;
    font-size:13px;color:var(--text-muted);cursor:pointer;
}

/* ---- Compare section ---- */
.compare-section{
    display:none;text-align:center;margin-top:20px;padding:16px;
    background:var(--card-bg);border-radius:8px;box-shadow:0 1px 3px var(--shadow);
}
.compare-section .hint{font-size:13px;color:var(--text-muted);margin-bottom:10px}

/* ---- Empty state ---- */
.empty-hint{text-align:center;color:var(--dashed-border);font-size:14px;padding:20px}

/* ---- Footer ---- */
.footer{text-align:center;padding:16px;color:var(--dashed-border);font-size:11px}

/* ---- History section ---- */
.history-section{
    margin-top:20px;padding:16px;
    background:var(--card-bg);border-radius:8px;box-shadow:0 1px 3px var(--shadow);
}
.history-item{
    display:flex;align-items:center;gap:8px;
    padding:6px 0;border-bottom:1px solid var(--divider);font-size:13px;
}
.history-item:last-child{border-bottom:none}
.history-score{
    font-weight:600;width:40px;text-align:center;font-size:14px;
}
.history-name{flex:1;color:var(--text)}
.history-time{color:var(--dashed-border);font-size:11px}

/* ---- History search ---- */
.history-search{
    width:100%;padding:6px 10px;border:1px solid var(--border);
    border-radius:4px;font-size:13px;color:var(--text);background:var(--input-bg);
    margin-bottom:10px;
}
.history-search:focus{outline:none;border-color:#409eff}

/* ---- History date group ---- */
.history-date-header{
    font-size:12px;color:var(--text-muted);font-weight:600;
    padding:8px 0 4px;margin-top:6px;
    border-top:1px solid var(--divider);
}
.history-date-header:first-child{border-top:none;margin-top:0}

/* ---- History checkbox ---- */
.history-check{
    display:flex;align-items:center;margin-right:4px;
}
.history-check input{cursor:pointer}

/* ---- History compare bar ---- */
.history-compare-bar{
    display:none;text-align:center;padding:10px 0;margin-top:8px;
}

/* ---- History no results ---- */
.history-empty{
    text-align:center;color:var(--dashed-border);font-size:13px;padding:12px 0;
}

/* ---- Toast notifications ---- */
.toast-container{position:fixed;top:16px;right:16px;z-index:9999;display:flex;flex-direction:column;gap:8px}
.toast{padding:12px 20px;border-radius:8px;color:#fff;font-size:13px;box-shadow:0 4px 12px rgba(0,0,0,.15);animation:slideIn .3s ease;max-width:360px}
.toast-error{background:#f56c6c}
.toast-success{background:#67c23a}
.toast-info{background:#409eff}
@keyframes slideIn{from{transform:translateX(100%);opacity:0}to{transform:translateX(0);opacity:1}}

/* ---- Responsive ---- */
@media(max-width:600px){
    .main{padding:0 8px}
    .upload-zone{padding:32px 16px}
    .job-card{padding:12px}
}
</style>
</head>
<body>

<header class="header">
    <h1>ArduScope</h1>
    <p>"""
    + t("web.subtitle")
    + r"""</p>
    <div class="lang-switch">
        <a class="nav-fleet" href="/fleet">"""
    + t("web.nav_fleet")
    + r"""</a>
        """
    + (f'<a class="nav-fleet" href="/settings">'
       f'{html.escape(t("web.nav_settings"))}</a>'
       if allow_settings else "")
    + r"""
        <button onclick="switchLang('zh')" """
    + ('class="active"' if get_locale() == "zh" else '')
    + r""">中文</button>
        <button onclick="switchLang('en')" """
    + ('class="active"' if get_locale() == "en" else '')
    + r""">EN</button>
        <button id="theme-toggle" onclick="toggleTheme()" title="Toggle dark/light mode" style="font-size:16px;line-height:1;padding:4px 8px"></button>
    </div>
</header>

<div class="main">
    <div class="template-bar">
        <label for="template-select">"""
    + t("web.template_label")
    + r"""</label>
        <select id="template-select">
            <option value="">"""
    + t("web.template_default")
    + r"""</option>
            <option value="quadplane-accept">"""
    + t("web.template_quadplane")
    + r"""</option>
            <option value="tuning-compare">"""
    + t("web.template_tuning")
    + r"""</option>
            <option value="crash-diagnosis">"""
    + t("web.template_crash")
    + r"""</option>
            <option value="battery-aging">"""
    + t("web.template_battery")
    + r"""</option>
            <option value="copter-factory">"""
    + t("web.template_copter_factory")
    + r"""</option>
        </select>
        <label class="agent-check">
            <input type="checkbox" id="agent-check"> """
    + t("web.agent_checkbox")
    + r"""
        </label>
    </div>
    <div class="upload-zone" id="upload-zone"
         onclick="document.getElementById('file-input').click()">
        <div class="icon">&#x1F4C1;</div>
        <div class="hint">"""
    + t("web.upload_hint")
    + r"""</div>
        <div class="hint2">"""
    + t("web.upload_hint2")
    + r"""</div>
        <input type="file" id="file-input" accept=".bin,.BIN" multiple>
    </div>

    <div id="jobs"></div>

    <div class="compare-section" id="compare-section">
        <div class="hint">"""
    + t("web.compare_hint_prefix")
    + r""" <span id="done-count">0</span> """
    + t("web.compare_hint_suffix")
    + r"""
        </div>
        <button class="btn btn-success" id="compare-btn"
                onclick="compareSelected()">"""
    + t("web.compare_btn")
    + r"""
        </button>
    </div>

    <div class="history-section" id="history-section" style="display:none">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
            <h3 style="font-size:15px;color:var(--text-secondary)">"""
    + t("web.history_title")
    + r"""</h3>
            <button class="btn btn-secondary" onclick="clearHistory()" style="font-size:12px">"""
    + t("web.history_clear")
    + r"""</button>
        </div>
        <input type="text" class="history-search" id="history-search"
               placeholder="""
    + '"' + t("history.search_placeholder") + '"'
    + r""" onkeyup="filterHistory()">
        <div id="history-list"></div>
        <div class="history-compare-bar" id="history-compare-bar">
            <button class="btn btn-success" onclick="compareHistorySelected()">"""
    + t("history.compare_selected")
    + r"""</button>
        </div>
    </div>
</div>

<div class="footer">ArduScope</div>
<div class="toast-container" id="toast-container"></div>

<script>
/* ---- i18n strings for JS ---- */
const I18N = """
    + json.dumps({
        "js.select_bin": t("web.js_select_bin"),
        "js.upload_failed": t("web.js_upload_failed"),
        "js.elapsed": t("web.js_elapsed"),
        "js.waiting": t("web.js_waiting"),
        "js.view_report": t("web.js_view_report"),
        "js.download": t("web.js_download"),
        "js.add_compare": t("web.js_add_compare"),
        "js.compare_min_select": t("web.js_compare_min_select"),
        "js.comparing": t("web.js_comparing"),
        "js.compare_failed": t("web.js_compare_failed"),
        "js.compare_btn": t("web.compare_btn"),
        "js.confirm_clear_history": t("web.js_confirm_clear_history"),
        "js.history_no_results": t("history.no_results"),
        "js.history_compare_selected": t("history.compare_selected"),
        "js.history_select_min_two": t("history.select_min_two"),
        "js.history_date_group": t("history.date_group"),
        "js.file_too_large": t("web.file_too_large",
                               max_mb=max_upload_bytes // 1024 // 1024),
        "js.agent_confirm": t("web.agent_egress_confirm"),
        "js.agent_tag": t("web.agent_tag"),
    }, ensure_ascii=False)
    + f";\nconst MAX_UPLOAD_BYTES = {max_upload_bytes};"
    + r"""

/* =================== Dark Mode =================== */
(function initTheme() {
    const saved = localStorage.getItem('theme');
    if (saved) {
        document.documentElement.dataset.theme = saved;
    } else if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) {
        document.documentElement.dataset.theme = 'dark';
    }
    updateThemeIcon();
})();
function toggleTheme() {
    const current = document.documentElement.dataset.theme;
    const next = current === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    localStorage.setItem('theme', next);
    updateThemeIcon();
}
function updateThemeIcon() {
    const btn = document.getElementById('theme-toggle');
    if (btn) btn.textContent = document.documentElement.dataset.theme === 'dark' ? '☀️' : '🌙';
}

/* =================== Toast Notifications =================== */
function showToast(message, type, duration) {
    type = type || 'info';
    duration = duration || 4000;
    const container = document.getElementById('toast-container');
    const toast = document.createElement('div');
    toast.className = 'toast toast-' + type;
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(function() {
        toast.style.opacity = '0';
        toast.style.transform = 'translateX(100%)';
        toast.style.transition = 'opacity .3s, transform .3s';
        setTimeout(function() { toast.remove(); }, 300);
    }, duration);
}

/* =================== Language =================== */
async function switchLang(lang) {
    await fetch('/api/set-lang', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({lang}),
    });
    location.reload();
}

/* =================== State =================== */
let pollTimer = null;

/* The agent choice survives a reload. The first tick asks what leaves
   the machine; saying no leaves the choice off. */
const agentCheck = document.getElementById('agent-check');
agentCheck.checked = localStorage.getItem('arduscope_agent') === '1';
agentCheck.addEventListener('change', () => {
    if (agentCheck.checked && localStorage.getItem('arduscope_agent_ack') !== '1') {
        if (!confirm(I18N['js.agent_confirm'])) { agentCheck.checked = false; return; }
        localStorage.setItem('arduscope_agent_ack', '1');
    }
    localStorage.setItem('arduscope_agent', agentCheck.checked ? '1' : '0');
});

/* =================== Upload =================== */
const zone = document.getElementById('upload-zone');
const fileInput = document.getElementById('file-input');

zone.addEventListener('dragover', e => {
    e.preventDefault();
    zone.classList.add('dragover');
});
zone.addEventListener('dragleave', () => zone.classList.remove('dragover'));
zone.addEventListener('drop', e => {
    e.preventDefault();
    zone.classList.remove('dragover');
    uploadFiles(e.dataTransfer.files);
});
fileInput.addEventListener('change', () => {
    uploadFiles(fileInput.files);
    fileInput.value = '';
});

async function uploadFiles(files) {
    const binFiles = Array.from(files).filter(
        f => f.name.toUpperCase().endsWith('.BIN')
    );
    if (binFiles.length === 0) { showToast(I18N['js.select_bin'], 'error'); return; }

    // Fire all uploads in parallel — each file is a separate raw binary POST
    for (const f of binFiles) {
        uploadOneFile(f);  // no await — fire and forget
    }
}

async function uploadOneFile(file) {
    /* The server refuses from the header alone and closes the connection,
       which a browser still sending the body can report as a network error. */
    if (file.size > MAX_UPLOAD_BYTES) {
        showToast(file.name + ' - ' + I18N['js.file_too_large'], 'error');
        return;
    }
    try {
        const tpl = document.getElementById('template-select').value;
        let url = '/api/upload?filename=' + encodeURIComponent(file.name);
        if (tpl) url += '&template=' + encodeURIComponent(tpl);
        if (document.getElementById('agent-check').checked) url += '&agent=1';
        const resp = await fetch(url, {
            method: 'POST',
            body: file,
            headers: {'Content-Type': 'application/octet-stream'},
        });
        if (!resp.ok) {
            let msg = '';
            try { msg = (await resp.json()).error || ''; } catch(_) {}
            showToast(I18N['js.upload_failed'] + file.name + (msg ? ' - ' + msg : ''), 'error');
        } else {
            // Job created on server — NOW start polling
            startPolling();
        }
    } catch (e) {
        showToast(I18N['js.upload_failed'] + file.name + ' - ' + e.message, 'error');
    }
}

/* =================== Polling =================== */
function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(pollStatus, 800);
    pollStatus();
}

async function pollStatus() {
    try {
        const resp = await fetch('/api/jobs');
        const data = await resp.json();
        renderJobs(data.jobs);
        const hasActive = data.jobs.some(
            j => j.status === 'running' || j.status === 'queued'
        );
        if (!hasActive && pollTimer) {
            clearInterval(pollTimer);
            pollTimer = null;
            loadHistory();
        }
    } catch (_) { /* ignore transient errors */ }
}

/* =================== Rendering =================== */
function renderJobs(jobs) {
    const container = document.getElementById('jobs');
    if (jobs.length === 0) {
        container.innerHTML = '<p class="empty-hint">"""
    + t("web.js_waiting")
    + r"""</p>';
        document.getElementById('compare-section').style.display = 'none';
        return;
    }
    container.innerHTML = jobs.map(renderJobCard).join('');

    const doneCount = jobs.filter(j => j.status === 'done').length;
    const sec = document.getElementById('compare-section');
    sec.style.display = doneCount >= 2 ? 'block' : 'none';
    document.getElementById('done-count').textContent = doneCount;
}

function renderJobCard(job) {
    const cls = 'status-' + job.status;

    /* file size */
    let sizeTag = '';
    if (job.file_size) {
        const mb = (job.file_size / 1024 / 1024).toFixed(0);
        sizeTag = `<span class="vehicle-tag">${mb} MB</span>`;
    }

    /* progress */
    let prog = '';
    if (job.status === 'running' || job.status === 'queued') {
        const active = job.status === 'running' ? ' active' : '';
        const elapsed = job.start_time ? Math.round((Date.now()/1000) - job.start_time) : 0;
        const elapsedStr = elapsed > 0 ? ` | ${I18N['js.elapsed']} ${elapsed}s` : '';
        prog = `<div class="progress-track">
            <div class="progress-fill${active}" style="width:${job.progress}%"></div>
        </div>
        <span class="progress-text">${job.progress}% - ${esc(job.message)}${elapsedStr}</span>`;
    }

    /* vehicle info */
    let veh = '';
    if (job.vehicle_info) {
        veh = `<span class="vehicle-tag">${esc(job.vehicle_info.type)} | ${esc(job.vehicle_info.board)}</span>`;
    }

    /* health score */
    let score = '';
    if (job.health_score !== undefined && job.health_score !== null && job.status === 'done') {
        const sc = job.health_score;
        const scColor = sc >= 80 ? '#67c23a' : sc >= 50 ? '#e6a23c' : '#f56c6c';
        score = `<span class="vehicle-tag" style="color:${scColor};font-weight:600">${sc}/100</span>`;
    }

    /* elapsed time for completed jobs */
    let elapsedTag = '';
    if (job.elapsed && job.status === 'done') {
        elapsedTag = `<span class="vehicle-tag">${job.elapsed}s</span>`;
    }

    /* online analysis: shown while it runs and after it finishes */
    let agentTag = '';
    if (job.agent) {
        const calls = job.agent_calls ? ' ' + job.agent_calls : '';
        agentTag = `<span class="vehicle-tag">${I18N['js.agent_tag']}${calls}</span>`;
    }

    /* analyzer dots */
    let dots = '';
    if (job.analyzer_results) {
        const cm = {good:'#67c23a', warning:'#e6a23c', critical:'#f56c6c', info:'#909399'};
        dots = '<div class="analyzer-dots">' +
            job.analyzer_results.map(r =>
                `<span class="dot" style="background:${cm[r.status]||'#ccc'}" title="${esc(r.title)}: ${esc(r.summary)}"></span>`
            ).join('') + '</div>';
    }

    /* error */
    let err = '';
    if (job.status === 'error') {
        err = `<div class="error-msg">${esc(job.message)}</div>`;
    } else if (job.agent_fallback) {
        err = `<div class="error-msg">${esc(job.agent_fallback)}</div>`;
    } else if (job.archive_warning) {
        err = `<div class="error-msg">${esc(job.archive_warning)}</div>`;
    }

    /* actions */
    let act = '';
    if (job.status === 'done') {
        const kb = job.report_size ? Math.round(job.report_size / 1024) : '?';
        act = `<div class="job-actions">
            <a href="${esc(job.result_url)}" target="_blank" class="btn btn-primary">${I18N['js.view_report']}</a>
            <a href="${esc(job.result_url)}" download class="btn btn-secondary">${I18N['js.download']} (${kb} KB)</a>
            <label class="compare-check">
                <input type="checkbox" value="${job.id}" checked> ${I18N['js.add_compare']}
            </label>
        </div>`;
    }

    return `<div class="job-card ${cls}">
        <div class="job-header">
            <span class="job-filename">${esc(job.filename)}</span>${sizeTag}${veh}${score}${agentTag}${elapsedTag}
        </div>
        ${prog}${dots}${err}${act}
    </div>`;
}

function esc(s) {
    if (s === null || s === undefined) return '';
    const map = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
    return String(s).replace(/[&<>"']/g, c => map[c]);
}

/* =================== Compare =================== */
async function compareSelected() {
    const checks = document.querySelectorAll('.compare-check input:checked');
    const ids = Array.from(checks).map(c => c.value);
    if (ids.length < 2) { showToast(I18N['js.compare_min_select'], 'error'); return; }

    const btn = document.getElementById('compare-btn');
    btn.disabled = true;
    btn.textContent = I18N['js.comparing'];

    try {
        const resp = await fetch('/api/compare', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({job_ids: ids}),
        });
        const data = await resp.json();
        if (data.url) window.open(data.url, '_blank');
        else if (data.error) showToast(data.error, 'error');
    } catch (e) {
        showToast(I18N['js.compare_failed'] + e.message, 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = I18N['js.compare_btn'];
    }
}

/* =================== History =================== */
let _historyData = [];

async function loadHistory() {
    try {
        const resp = await fetch('/api/history');
        const data = await resp.json();
        const sec = document.getElementById('history-section');
        if (data.history && data.history.length > 0) {
            _historyData = data.history;
            sec.style.display = 'block';
            renderHistory(_historyData);
        } else {
            _historyData = [];
            sec.style.display = 'none';
        }
    } catch(_) {}
}

function renderHistory(items) {
    const list = document.getElementById('history-list');
    const query = (document.getElementById('history-search').value || '').toLowerCase();

    /* Reverse to show newest first, limit to 50 */
    let display = items.slice().reverse().slice(0, 50);

    /* Apply search filter */
    if (query) {
        display = display.filter(h => (h.filename || '').toLowerCase().indexOf(query) !== -1);
    }

    if (display.length === 0) {
        list.innerHTML = '<div class="history-empty">' + I18N['js.history_no_results'] + '</div>';
        updateHistoryCompareBar();
        return;
    }

    /* Group by date */
    const groups = {};
    const groupOrder = [];
    display.forEach(h => {
        const date = (h.timestamp || '').substring(0, 10) || '—';
        if (!groups[date]) {
            groups[date] = [];
            groupOrder.push(date);
        }
        groups[date].push(h);
    });

    let html = '';
    groupOrder.forEach(date => {
        const label = I18N['js.history_date_group'].replace('{date}', date);
        html += '<div class="history-date-header">' + esc(label) + '</div>';
        groups[date].forEach(h => {
            const sc = h.health_score;
            const scColor = sc >= 80 ? '#67c23a' : sc >= 50 ? '#e6a23c' : '#f56c6c';
            const veh = h.vehicle_info ? h.vehicle_info.type : '';
            const hasMetrics = h.metrics && Object.keys(h.metrics).length > 0;
            html += '<div class="history-item">'
                + (hasMetrics ? '<label class="history-check"><input type="checkbox" data-hist-id="' + esc(h.id) + '" onchange="updateHistoryCompareBar()"></label>' : '')
                + '<span class="history-score" style="color:' + scColor + '">' + (sc !== null && sc !== undefined ? sc : '-') + '</span>'
                + '<span class="history-name">' + esc(h.filename) + (veh ? ' <small style="color:#999">' + esc(veh) + '</small>' : '') + '</span>'
                + '<span class="history-time">' + esc(h.timestamp) + '</span>'
                + '</div>';
        });
    });

    list.innerHTML = html;
    updateHistoryCompareBar();
}

function filterHistory() {
    renderHistory(_historyData);
}

function updateHistoryCompareBar() {
    const checks = document.querySelectorAll('#history-list input[data-hist-id]:checked');
    const bar = document.getElementById('history-compare-bar');
    bar.style.display = checks.length >= 2 ? 'block' : 'none';
}

async function compareHistorySelected() {
    const checks = document.querySelectorAll('#history-list input[data-hist-id]:checked');
    const ids = Array.from(checks).map(c => c.dataset.histId);
    if (ids.length < 2) { showToast(I18N['js.history_select_min_two'], 'error'); return; }

    const btn = document.querySelector('#history-compare-bar button');
    btn.disabled = true;
    btn.textContent = I18N['js.comparing'];

    try {
        const resp = await fetch('/api/compare-history', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ids}),
        });
        const data = await resp.json();
        if (data.url) window.open(data.url, '_blank');
        else if (data.error) showToast(data.error, 'error');
    } catch (e) {
        showToast(I18N['js.compare_failed'] + e.message, 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = I18N['js.history_compare_selected'];
    }
}

async function clearHistory() {
    if (!confirm(I18N['js.confirm_clear_history'])) return;
    await fetch('/api/history', {method: 'DELETE'});
    loadHistory();
}

// Load history on page load
loadHistory();
</script>
</body>
</html>"""
    )


# =========================================================================
# Fleet pages (Phase 2 of docs/fleet_history_design.md)
# =========================================================================



# =========================================================================
# Fleet pages (Phase 2 of docs/fleet_history_design.md)
# =========================================================================

def _score_badge(score) -> str:
    """Colored score span; '-' when the score is unknown (<0 or None)."""
    if score is None or score < 0:
        return '<span class="score">-</span>'
    color = ('#67c23a' if score >= 80
             else '#e6a23c' if score >= 50 else '#f56c6c')
    return f'<span class="score" style="color:{color}">{score}</span>'


def _build_fleet_shell(title_text: str, body: str,
                       back_href: str = "/",
                       back_label: str = "") -> str:
    """Minimal themed page frame shared by the fleet list/detail pages."""
    from lib.i18n import get_locale
    lang_code = "zh-CN" if get_locale() == "zh" else "en"
    back = back_label or t("web.nav_home")
    return f"""<!DOCTYPE html>
<html lang="{lang_code}">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(title_text)} - ArduScope</title>
<style>
:root{{--bg:#f0f2f5;--card-bg:#fff;--text:#333;--text-muted:#909399;
--border:#dcdfe6;--shadow:rgba(0,0,0,.08);
--header-bg:linear-gradient(135deg,#1a1a2e 0%,#16213e 100%)}}
[data-theme="dark"]{{--bg:#1a1a2e;--card-bg:#252540;--text:#e0e0e0;
--text-muted:#707080;--border:#3a3a50;--shadow:rgba(0,0,0,.3);
--header-bg:linear-gradient(135deg,#0d0d1a 0%,#0f1525 100%)}}
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:"PingFang SC","Microsoft YaHei","Noto Sans SC",
-apple-system,BlinkMacSystemFont,sans-serif;
background:var(--bg);color:var(--text);min-height:100vh}}
.header{{background:var(--header-bg);color:#fff;padding:16px 0;
text-align:center}}
.header h1{{font-size:20px}}
.header a{{color:rgba(255,255,255,.7);font-size:13px;
text-decoration:none}}
.header a:hover{{color:#fff}}
.main{{max-width:1100px;margin:24px auto;padding:0 16px}}
.card{{background:var(--card-bg);border-radius:8px;padding:20px;
box-shadow:0 1px 3px var(--shadow);margin-bottom:16px;overflow-x:auto}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{padding:8px 10px;text-align:left;
border-bottom:1px solid var(--border)}}
th{{color:var(--text-muted);font-weight:600;white-space:nowrap}}
td code{{font-size:12px}}
a.serial{{color:#409eff;text-decoration:none}}
a.serial:hover{{text-decoration:underline}}
.score{{font-weight:600}}
.empty{{text-align:center;color:var(--text-muted);padding:24px 0}}
.meta{{color:var(--text-muted);font-size:13px;margin:0 0 14px}}
.alias-form{{display:flex;gap:8px;margin:0 0 14px}}
.alias-form input{{flex:0 1 260px;padding:6px 10px;font-size:13px;
border:1px solid var(--border);border-radius:4px;
background:var(--card-bg);color:var(--text)}}
.alias-form button{{padding:6px 14px;font-size:13px;border:none;
border-radius:4px;background:#409eff;color:#fff;cursor:pointer}}
.alias-form button:hover{{background:#66b1ff}}
</style>
</head>
<body>
<script>
(function(){{const s=localStorage.getItem('theme');
if(s){{document.documentElement.dataset.theme=s}}
else if(window.matchMedia&&window.matchMedia(
'(prefers-color-scheme: dark)').matches)
{{document.documentElement.dataset.theme='dark'}}}})();
</script>
<header class="header"><h1>{html.escape(title_text)}</h1>
<a href="{back_href}">{html.escape(back)}</a></header>
<div class="main">{body}</div>
</body></html>"""


def build_fleet_html(db_path: str) -> str:
    """Airframe list page."""
    frames = fleet_history.list_airframes(db_path)
    if not frames:
        body = (f'<div class="card">'
                f'<p class="empty">{t("fleet.web_empty")}</p></div>')
        return _build_fleet_shell(t("fleet.web_title"), body)
    rows = []
    for f in frames:
        rows.append(
            '<tr>'
            f'<td><a class="serial" href="/fleet/{f["serial"]}">'
            f'<code>{html.escape(f["serial"])}</code></a></td>'
            f'<td>{html.escape(f["alias"]) or "-"}</td>'
            f'<td>{html.escape(f["board"]) or "-"}</td>'
            f'<td>{f["n_flights"]}</td>'
            f'<td>{html.escape(f["last_date"]) or "-"}</td>'
            f'<td>{_score_badge(f["last_score"])}</td>'
            '</tr>')
    body = (
        '<div class="card"><table><thead><tr>'
        f'<th>{t("fleet.th_serial")}</th>'
        f'<th>{t("fleet.th_alias")}</th>'
        f'<th>{t("fleet.th_board")}</th>'
        f'<th>{t("fleet.th_flights")}</th>'
        f'<th>{t("fleet.th_last")}</th>'
        f'<th>{t("fleet.th_score")}</th>'
        '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>')
    return _build_fleet_shell(t("fleet.web_title"), body)


def build_airframe_html(db_path: str, serial: str) -> str:
    """Single-airframe page: metadata line, trend figure, flight table."""
    info = next((f for f in fleet_history.list_airframes(db_path)
                 if f["serial"] == serial), {})
    rows = fleet_history.query_history(db_path, serial, limit=50)

    heading = info.get("alias") or serial
    meta_bits = [serial]
    if info.get("board"):
        meta_bits.append(info["board"])
    summary = t("fleet.summary", n=len(rows),
                first=(rows[0]["flight_date"] or "-") if rows else "-",
                last=(rows[-1]["flight_date"] or "-") if rows else "-")

    hist = fleet_history.build_history_result(rows, serial)
    if hist is not None:
        fig_html = ('<div class="card">'
                    + hist.figures[0].to_html(full_html=False,
                                              include_plotlyjs="inline")
                    + '</div>')
    else:
        fig_html = (f'<div class="card">'
                    f'<p class="empty">{t("fleet.web_too_few")}</p></div>')

    trs = []
    for r in rows:
        mets = r["metrics"]

        def _m(key, suffix=""):
            v = mets.get(key)
            return f"{v}{suffix}" if v is not None else "-"

        vibe = " / ".join(_m(f"vibe_max_{ax}") for ax in ("x", "y", "z"))
        trs.append(
            '<tr>'
            f'<td>{html.escape(r["flight_date"] or "-")}</td>'
            f'<td><code>{html.escape(r["log_name"])}</code></td>'
            f'<td>{_score_badge(r["health_score"])}</td>'
            f'<td>{(r["duration_s"] or 0) / 60:.1f}</td>'
            f'<td>{vibe}</td>'
            f'<td>{_m("batt_v_min", "V")}</td>'
            f'<td>{_m("vcc_sag", "V")}</td>'
            f'<td>{html.escape(r["fw_version"] or "-")}</td>'
            '</tr>')

    alias_form = (
        f'<form class="alias-form" method="post"'
        f' action="/fleet/{serial}/alias">'
        f'<input name="alias"'
        f' value="{html.escape(info.get("alias") or "")}"'
        f' placeholder="{html.escape(t("fleet.web_alias_ph"))}">'
        f'<button type="submit">{t("fleet.web_alias_save")}</button>'
        f'</form>')

    body = (
        f'<p class="meta">{html.escape(" | ".join(meta_bits))}'
        f' — {html.escape(summary)}</p>'
        + alias_form
        + fig_html
        + '<div class="card"><table><thead><tr>'
        f'<th>{t("fleet.th_date")}</th>'
        f'<th>{t("fleet.th_log")}</th>'
        f'<th>{t("fleet.th_score")}</th>'
        f'<th>{t("fleet.th_dur")}</th>'
        f'<th>{t("fleet.th_vibe")}</th>'
        f'<th>{t("fleet.th_vmin")}</th>'
        f'<th>{t("fleet.th_sag")}</th>'
        f'<th>{t("fleet.th_fw")}</th>'
        '</tr></thead><tbody>' + ''.join(trs) + '</tbody></table></div>')
    return _build_fleet_shell(heading, body, back_href="/fleet",
                              back_label=t("fleet.web_back_list"))

