#!/usr/bin/env python3
"""
ArduScope - Web Interface

Launch a local web server for uploading .BIN logs and generating analysis reports.

Usage:
    python3 web_app.py
    python3 web_app.py --port 9090
    python3 web_app.py --no-open
    python3 web_app.py --report-dir ./reports
"""

from __future__ import annotations

import argparse
import html
import ipaddress
import json
import math
import os
import re
import socket
import sys
import threading
import time
import uuid
import webbrowser
from urllib.parse import quote, urlsplit

# ---------------------------------------------------------------------------
# Path setup (same as flight_report.py)
# ---------------------------------------------------------------------------
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.dirname(_SCRIPT_DIR)
if _PARENT_DIR not in sys.path:
    sys.path.insert(0, _PARENT_DIR)
# Embedded Python (._pth) may strip the script directory from sys.path
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

# ---------------------------------------------------------------------------
# Third-party imports
# ---------------------------------------------------------------------------
try:
    import bottle  # noqa: F401 — used for version check and must be importable
    from bottle import (Bottle, HTTPResponse, abort, redirect, request,
                        response, static_file)
except ImportError:
    print("Error: bottle is required. Install with: pip install bottle",
          file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Local imports
# ---------------------------------------------------------------------------
from lib.i18n import available_locales, get_locale, set_locale, t
from lib import fleet_history
from lib.html_report import HtmlReportGenerator
from lib.ai_diagnosis import generate_diagnosis
from lib.agent import analyze_with_agent
from lib.agent.config import ConfigError, load_config, update_config
from lib.agent.config import CONFIG_PATH  # noqa: F401 - re-exported for tests
from lib.agent.providers import NoProviderPackage, test_connection
from lib.pipeline import run_analysis
from lib.templates import get_template
from lib.web_limits import (  # noqa: F401 - two re-exported for tests
    ANALYSIS_BASE_BYTES, ANALYSIS_PEAK_PER_LOG_BYTE,
    _MemoryBudget, _clean_upload_name, _estimated_peak_bytes)
from lib.web_pages import (_build_fleet_shell,
                           build_airframe_html as _build_airframe_html,
                           build_fleet_html, build_index_html,
                           build_settings_html as _build_settings_html)

from multi_report import extract_metrics, generate_html as generate_multi_html

# No MEMFILE_MAX needed — uploads use raw binary body, not multipart.

# ---------------------------------------------------------------------------
# App and global state
# ---------------------------------------------------------------------------
app = Bottle()

# Default UI language, from --lang. A browser that picked one keeps it in a
# cookie, so one user's choice does not change the page for anyone else.
# i18n keeps the locale in a threading.local, so it is applied on every
# request (hook below) and handed to every analysis worker.
_UI_LOCALE = "zh"
_LANG_COOKIE_MAX_AGE = 365 * 24 * 3600


@app.hook('before_request')
def _apply_ui_locale():
    chosen = request.get_cookie('lang')
    set_locale(chosen if chosen in available_locales() else _UI_LOCALE)


# Host names this server answers to. Binding 127.0.0.1 does not keep other
# sites out: any page the user opens can send requests here, and can point
# its own DNS name at 127.0.0.1 so the browser treats them as same-origin.
# Such a request still names the attacker's site in its Host header.
_ALLOWED_HOST_NAMES: set[str] = {"localhost"}
# Bound to every interface, the server cannot know its addresses, but an IP
# literal in Host cannot be the product of DNS rebinding.
_ACCEPT_ANY_IP = False

# The settings page writes a key to this machine, so it is served only
# when the server binds a loopback address. A LAN listener would hand the
# page to anyone on the network.
_ALLOW_SETTINGS = True


def _configure_allowed_hosts(bind_host: str, extra_names: list[str]) -> None:
    """Derive the accepted Host names from the address the server binds."""
    global _ALLOWED_HOST_NAMES, _ACCEPT_ANY_IP
    names = {"localhost", bind_host.lower()}
    names.update(name.lower() for name in extra_names)
    if not _is_loopback(bind_host):
        names.update((socket.gethostname().lower(), socket.getfqdn().lower()))
    _ALLOWED_HOST_NAMES = names
    _ACCEPT_ANY_IP = bind_host in ("0.0.0.0", "::")


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _host_allowed(host_header: str) -> bool:
    try:
        name = urlsplit("//" + host_header).hostname
    except ValueError:
        return False
    if not name:
        return False
    if name in _ALLOWED_HOST_NAMES or _is_loopback(name):
        return True
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return _ACCEPT_ANY_IP


def _same_origin(origin: str, host_header: str) -> bool:
    try:
        return urlsplit(origin).netloc.lower() == host_header.lower()
    except ValueError:
        return False


@app.hook('before_request')
def _refuse_foreign_requests():
    """Stop DNS rebinding (Host) and cross-site requests (Origin).

    Browsers put Origin on every cross-site POST, and on reads through
    fetch. A request without it comes from a link or from a tool such as
    curl, which CSRF cannot use.
    """
    host = request.environ.get('HTTP_HOST', '')
    origin = request.environ.get('HTTP_ORIGIN')
    if not _host_allowed(host):
        reason = t("web.host_refused", host=host)
    elif origin is not None and not _same_origin(origin, host):
        reason = t("web.origin_refused", origin=origin)
    else:
        return
    raise HTTPResponse(json.dumps({'error': reason}), status=403,
                       headers={'Content-Type': 'application/json'})


# These pages are built on inline scripts and styles, so those stay allowed.
# What the policy adds is that nothing can be loaded from, or sent to, any
# other origin.
_APP_CSP = ("default-src 'none'; script-src 'unsafe-inline'; "
            "style-src 'unsafe-inline'; img-src data:; connect-src 'self'; "
            "form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
# A report holds text taken from a log, which can come from anyone. The
# sandbox gives it an origin of its own: a script that got past escaping
# could not reach this API or read another report. Map tiles are the only
# thing it fetches.
_REPORT_CSP = ("sandbox allow-scripts allow-modals allow-downloads; "
               "default-src 'none'; script-src 'unsafe-inline'; "
               "style-src 'unsafe-inline'; "
               "img-src data: blob: https://*.tile.openstreetmap.org; "
               "base-uri 'none'; form-action 'none'; "
               "frame-ancestors 'none'")


@app.hook('after_request')
def _set_security_headers():
    response.set_header('X-Content-Type-Options', 'nosniff')
    response.set_header('X-Frame-Options', 'DENY')
    response.set_header('Content-Security-Policy',
                        _REPORT_CSP if request.path.startswith('/reports/')
                        else _APP_CSP)


JOBS: dict[str, dict] = {}          # job_id -> job info dict
_JOBS_LOCK = threading.Lock()       # protect JOBS dict against concurrent access
# The limit follows peak memory: a log of this size has to fit the budget
# below. It stood at 500 MB while the largest corpus log, 813 MB, peaked at
# 8.9 GB. The file was being read again for each message type nobody had
# declared, and pymavlink kept the index of the read before. The same log
# now peaks at 3.0 GB in a new server and 4.1 GB in one that has run it.
MAX_UPLOAD_BYTES = 1024 * 1024 * 1024
MAX_JOBS = 200                      # evict oldest done/error jobs beyond this
# An upload holds a thread and its file on disk until its analysis ends.
# 64 covers a long field day dropped in as one folder, while at the upload
# limit it still bounds the disk taken by waiting files.
MAX_PENDING_ANALYSES = 64
_PENDING_SLOTS = threading.BoundedSemaphore(MAX_PENDING_ANALYSES)
# What running analyses may use together: leaves room for the system and a
# browser on a 16 GB field laptop.
ANALYSIS_MEMORY_BUDGET_BYTES = 8 * 1024 * 1024 * 1024
# 0 keeps every report: they are the user's work, and only --keep-reports
# may delete them. It has to stay above MAX_JOBS, or the job list would
# link to deleted pages.
MAX_KEPT_REPORTS = 0
_GENERATED_REPORT = re.compile(
    r'(?:[0-9a-f]{8}_.*_report|compare_[0-9a-f]{8})\.html')
# The largest JSON body a route reads: the compare routes take a list of
# at most MAX_JOBS ids of 8 characters.
MAX_JSON_BYTES = 64 * 1024

_ANALYSIS_BUDGET = _MemoryBudget(ANALYSIS_MEMORY_BUDGET_BYTES)


def _finite(value):
    """Replace NaN and infinities, which JSON has no way to write, with null.

    json.dumps emits them as bare NaN, the browser's parser rejects the
    whole response, and the job or history list goes blank. A log can put
    them anywhere a metric or a parameter value goes.
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(v) for v in value]
    return value


def _evict_finished_jobs():
    """Make room for one job. Caller holds _JOBS_LOCK."""
    if len(JOBS) >= MAX_JOBS:
        done = [k for k, v in JOBS.items()
                if v.get('status') in ('done', 'error')]
        done.sort(key=lambda k: JOBS[k].get('start_time', 0))
        for k in done[:len(JOBS) - MAX_JOBS + 1]:
            del JOBS[k]


def _update_job(job_id: str, **fields):
    """Atomically update multiple fields on a job entry."""
    with _JOBS_LOCK:
        job = JOBS.get(job_id)
        if job:
            job.update(fields)
REPORT_DIR: str = ""                # set in main()
HISTORY: list[dict] = []            # persistent history of completed analyses
HISTORY_FILE: str = ""              # path to history.json
_HISTORY_LOCK = threading.Lock()

# =========================================================================
# Routes
# =========================================================================


@app.error(500)
def error500(err):
    """Return JSON instead of HTML for server errors on API routes."""
    # Bottle replaces the response headers when a route raises.
    _set_security_headers()
    response.content_type = 'application/json'
    return json.dumps({'error': t("web.error_500", detail=err.body)})


@app.route('/')
def index():
    """Serve the main page."""
    return _build_index_html()


@app.route('/api/set-lang', method='POST')
def set_lang():
    """Switch this browser's UI language and return success."""
    data = _json_object()
    if data is None:
        return _json_error(400, t("web.invalid_json"))
    lang = data.get("lang", "zh")
    if not isinstance(lang, str) or lang not in available_locales():
        lang = "zh"
    response.set_cookie('lang', lang, path='/', httponly=True,
                        samesite='strict', max_age=_LANG_COOKIE_MAX_AGE)
    response.content_type = 'application/json'
    return json.dumps({"ok": True, "lang": lang})


def _json_error(status: int, message: str) -> str:
    response.status = status
    response.content_type = 'application/json'
    return json.dumps({'error': message})


def _json_object() -> dict | None:
    """The request body as a JSON object, None for anything else.

    Read only when Content-Length says it is small: request.body takes
    the whole body into memory, whatever its size.
    """
    if not 0 <= request.content_length <= MAX_JSON_BYTES:
        return None
    try:
        data = json.loads(request.body.read())
    except (ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def _refuse_upload_length() -> str | None:
    """Error reply for a body that must not be read, else None.

    Decided from the header alone, because reading is what costs: bottle
    spools a body to a temp file, so a size check made while copying only
    runs once the disk has already taken all of it.
    """
    if request.content_length < 0:
        return _json_error(411, t("web.length_required"))
    if request.content_length > MAX_UPLOAD_BYTES:
        return _json_error(413, t(
            "web.file_too_large", max_mb=MAX_UPLOAD_BYTES // 1024 // 1024))
    return None


def _save_body(path: str, length: int) -> int:
    """Stream the raw request body into path, returning the bytes received.

    Reads wsgi.input itself, since request.body would spool a second copy.
    It must not read past Content-Length: on the dev server that blocks
    until the client disconnects.
    """
    stream = request.environ['wsgi.input']
    written = 0
    with open(path, 'wb') as dst:
        while written < length:
            chunk = stream.read(min(1024 * 1024, length - written))
            if not chunk:
                break
            dst.write(chunk)
            written += len(chunk)
    return written


def _discard(path: str) -> None:
    try:
        os.remove(path)
    except (OSError, ValueError):
        pass


def _prune_reports() -> None:
    """Delete the oldest reports this server wrote, past MAX_KEPT_REPORTS.

    Off unless --keep-reports asks for it. Anything else in the directory
    is left alone: --report-dir can point at a folder that also holds
    reports from the command line.
    """
    if MAX_KEPT_REPORTS <= 0:
        return
    try:
        with os.scandir(REPORT_DIR) as entries:
            reports = sorted((e.stat().st_mtime, e.path) for e in entries
                             if _GENERATED_REPORT.fullmatch(e.name))
    except OSError:
        return
    for _mtime, path in reports[:-MAX_KEPT_REPORTS]:
        _discard(path)


def _receive_body(path: str) -> tuple[int, str | None]:
    """Save the body to path: (bytes written, error reply or None)."""
    try:
        written = _save_body(path, request.content_length)
    except Exception as e:
        _discard(path)
        return 0, _json_error(500, t("web.file_save_failed", detail=e))
    if written == 0:
        _discard(path)
        return 0, _json_error(400, t("web.empty_upload"))
    if written < request.content_length:
        _discard(path)
        return 0, _json_error(400, t("web.upload_incomplete"))
    return written, None


def _analyze_then_free_slot(*args) -> None:
    try:
        _run_analysis(*args)
    finally:
        _PENDING_SLOTS.release()


def _start_job(job_id: str, save_path: str, filename: str, written: int,
               template_name: str = "", use_agent: bool = False) -> None:
    """List a saved upload as a job and hand it to an analysis thread.

    The caller holds a pending slot, and the thread gives it back. If the
    thread cannot start nothing ever will, so the job and its file go.
    """
    with _JOBS_LOCK:
        _evict_finished_jobs()
        JOBS[job_id] = {
            'id': job_id,
            'filename': filename,
            'status': 'queued',
            'progress': 0,
            'message': t("web.status_queued"),
            'result_url': None,
            'report_size': None,
            'vehicle_info': None,
            'analyzer_results': None,
            'metrics': None,
            'start_time': time.time(),
            'file_size': written,
            'agent': use_agent,
            'agent_calls': 0,
            'agent_fallback': '',
        }
    worker = threading.Thread(
        target=_analyze_then_free_slot,
        args=(job_id, save_path, filename, template_name, get_locale(),
              use_agent),
        daemon=True,
    )
    try:
        worker.start()
    except BaseException:
        with _JOBS_LOCK:
            JOBS.pop(job_id, None)
        _discard(save_path)
        raise


def _agent_ready() -> bool:
    """Whether the agent has a provider, a model and a key."""
    try:
        return load_config().is_configured()
    except ConfigError:
        return False


@app.route('/api/upload', method='POST')
def upload():
    """
    Handle single .BIN file upload (raw binary body).
    Filename passed as query parameter: /api/upload?filename=xxx.BIN
    This avoids multipart parsing which fails for large files.
    """
    # Bottle decodes query params as Latin-1 by default; use parse_qs for UTF-8
    from urllib.parse import parse_qs
    qs = parse_qs(request.query_string)
    filename = qs.get('filename', [''])[0]
    if not filename:
        response.status = 400
        response.content_type = 'application/json'
        return json.dumps({'error': t("web.missing_filename")})

    filename = _clean_upload_name(filename)
    if not filename.upper().endswith('.BIN'):
        response.status = 400
        response.content_type = 'application/json'
        return json.dumps({'error': t("web.not_bin_file", filename=filename)})

    # Optional template from query string. A name that matches nothing
    # would run the default thresholds under the template's name.
    template_name = qs.get('template', [''])[0]
    if template_name and get_template(template_name) is None:
        return _json_error(400, t("web.unknown_template", name=template_name))

    use_agent = qs.get('agent', [''])[0].lower() in ('1', 'true', 'on')
    if use_agent and not _agent_ready():
        return _json_error(400, t("web.agent_not_configured"))

    refused = _refuse_upload_length()
    if refused:
        return refused
    if not _PENDING_SLOTS.acquire(blocking=False):
        return _json_error(429, t("web.queue_full"))

    job_id = uuid.uuid4().hex[:8]
    save_path = os.path.join(REPORT_DIR, f"upload_{job_id}_{filename}")
    handed_over = False
    try:
        written, error = _receive_body(save_path)
        if error:
            return error
        print(f"  [upload] {filename} ({written / 1024 / 1024:.0f} MB) -> "
              f"{job_id}"
              + (f" [template: {template_name}]" if template_name else ""))
        _start_job(job_id, save_path, filename, written, template_name,
                   use_agent)
        handed_over = True
    finally:
        # Whatever went wrong, the slot goes back: each one that leaked
        # was gone until the server restarted, and there are 64.
        if not handed_over:
            _PENDING_SLOTS.release()

    response.content_type = 'application/json'
    return json.dumps({'job_id': job_id})


@app.route('/api/upload-batch', method='POST')
def upload_batch():
    """
    Handle multiple .BIN file uploads via multipart/form-data.
    Returns a list of job_ids, one per file. The whole request has to fit
    the upload limit, which keeps what the multipart parser spools bounded.
    """
    refused = _refuse_upload_length()
    if refused:
        return refused

    job_ids = []
    errors = []

    for upload_file in request.files.getall('files'):
        # raw_filename: bottle's .filename keeps ASCII only, which turned
        # 飞行.BIN into "BIN" and refused it as not being a log.
        filename = _clean_upload_name(
            upload_file.raw_filename or "unknown.BIN")
        if not filename.upper().endswith('.BIN'):
            errors.append({"filename": filename, "error": t("web.not_bin_file", filename=filename)})
            continue
        if not _PENDING_SLOTS.acquire(blocking=False):
            errors.append({"filename": filename, "error": t("web.queue_full")})
            continue

        job_id = uuid.uuid4().hex[:8]
        save_path = os.path.join(REPORT_DIR, f"upload_{job_id}_{filename}")
        handed_over = False
        try:
            upload_file.save(save_path, overwrite=True)
            written = os.path.getsize(save_path)
            print(f"  [batch] {filename} ({written / 1024 / 1024:.1f} MB) -> "
                  f"{job_id}")
            _start_job(job_id, save_path, filename, written)
            handed_over = True
        except Exception as e:
            _discard(save_path)
            errors.append({"filename": filename, "error": str(e)})
            continue
        finally:
            if not handed_over:
                _PENDING_SLOTS.release()
        job_ids.append({"job_id": job_id, "filename": filename})

    response.content_type = 'application/json'
    return json.dumps({"jobs": job_ids, "errors": errors})


@app.route('/api/jobs')
def list_jobs():
    """List all analysis jobs."""
    response.content_type = 'application/json'
    # Snapshot each job inside the lock — list(JOBS.values()) only captures
    # references, so concurrent _update_job() can mutate dicts mid-serialize,
    # raising RuntimeError or producing torn reads. Same race as get_job.
    with _JOBS_LOCK:
        jobs = [dict(j) for j in JOBS.values()]
    return json.dumps(_finite({'jobs': jobs}))


@app.route('/api/jobs/<job_id>')
def get_job(job_id):
    """Get single job status."""
    response.content_type = 'application/json'
    with _JOBS_LOCK:
        raw = JOBS.get(job_id)
        job = dict(raw) if raw is not None else None
    if job is None:
        response.status = 404
        return json.dumps({'error': t("web.job_not_found")})
    return json.dumps(_finite(job))


@app.route('/api/compare', method='POST')
def compare():
    """Generate multi-log comparison report from completed jobs."""
    data = _json_object()
    if data is None:
        return _json_error(400, t("web.invalid_json"))
    job_ids = data.get('job_ids')
    if not isinstance(job_ids, list):
        job_ids = []

    log_names: list[str] = []
    all_metrics: list[dict] = []
    all_params: list[dict] = []
    with _JOBS_LOCK:
        for jid in job_ids:
            job = JOBS.get(jid) if isinstance(jid, str) else None
            if job and job['status'] == 'done' and job.get('metrics'):
                log_names.append(job['filename'])
                all_metrics.append(job['metrics'])
                all_params.append(job.get('params') or {})

    if len(log_names) < 2:
        response.status = 400
        response.content_type = 'application/json'
        return json.dumps({'error': t("web.compare_min_two")})

    compare_id = uuid.uuid4().hex[:8]
    output_name = f"compare_{compare_id}.html"
    output_path = os.path.join(REPORT_DIR, output_name)

    html_content = generate_multi_html(log_names, all_metrics, all_params)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html_content)
    _prune_reports()

    response.content_type = 'application/json'
    return json.dumps({'url': f'/reports/{output_name}'})


@app.route('/api/compare-history', method='POST')
def compare_history():
    """Generate comparison report from history entries by id."""
    data = _json_object()
    if data is None:
        return _json_error(400, t("web.invalid_json"))

    ids = data.get('ids', [])
    if not isinstance(ids, list) or len(ids) < 2:
        response.status = 400
        response.content_type = 'application/json'
        return json.dumps({'error': t("web.compare_min_two")})

    wanted = {i for i in ids if isinstance(i, str)}
    log_names: list[str] = []
    all_metrics: list[dict] = []
    all_params: list[dict] = []
    with _HISTORY_LOCK:
        # Archive order: the page posts newest first, and a log with no
        # GPS time has nothing else to be placed by.
        for entry in HISTORY:
            if entry.get('id') in wanted and entry.get('metrics'):
                log_names.append(entry.get('filename', ''))
                all_metrics.append(entry['metrics'])
                all_params.append({})

    if len(log_names) < 2:
        response.status = 400
        response.content_type = 'application/json'
        return json.dumps({'error': t("web.compare_min_two")})

    compare_id = uuid.uuid4().hex[:8]
    output_name = f"compare_{compare_id}.html"
    output_path = os.path.join(REPORT_DIR, output_name)

    html_content = generate_multi_html(log_names, all_metrics, all_params)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html_content)
    _prune_reports()

    response.content_type = 'application/json'
    return json.dumps({'url': f'/reports/{output_name}'})


def _settings_allowed() -> bool:
    return _ALLOW_SETTINGS


def _settings_same_origin() -> bool:
    """Refuse a POST another site sent from the browser.

    No Origin header is a local tool (curl, a script); the settings server
    only listens on loopback, so those are the owner's own requests.
    """
    origin = request.headers.get("Origin", "")
    if not origin:
        return True
    try:
        return urlsplit(origin).hostname == request.headers.get("Host", "").split(":")[0]
    except ValueError:
        return False


@app.route('/settings')
def settings_page():
    if not _settings_allowed():
        abort(404, t("web.settings_only_local"))
    try:
        config = load_config()
    except ConfigError as e:
        config = None
        return _build_settings_html(_empty_agent_config(), error=str(e))
    return _build_settings_html(config)


def _empty_agent_config():
    from lib.agent.config import AgentConfig
    return AgentConfig()


@app.route('/api/settings')
def settings_get():
    if not _settings_allowed():
        abort(404, t("web.settings_only_local"))
    response.content_type = 'application/json'
    try:
        return json.dumps(load_config().public_dict(), ensure_ascii=False)
    except ConfigError as e:
        return _json_error(500, str(e))


@app.route('/api/settings', method='POST')
def settings_post():
    if not _settings_allowed() or not _settings_same_origin():
        abort(404, t("web.settings_only_local"))
    data = _json_object()
    if data is None:
        return _json_error(400, t("web.invalid_json"))
    provider = str(data.get("provider") or "")
    model = str(data.get("model") or "").strip()
    base_url = str(data.get("base_url") or "").strip()
    if provider not in ("anthropic", "openai"):
        return _json_error(400, t("web.settings_provider"))
    if not model:
        return _json_error(400, t("web.settings_model"))
    if base_url and not base_url.startswith(("http://", "https://")):
        return _json_error(400, t("web.settings_base_url"))
    try:
        config = load_config()
        config = update_config(config, data)
    except (ConfigError, OSError) as e:
        return _json_error(500, t("web.settings_error", error=e))
    response.content_type = 'application/json'
    return json.dumps(config.public_dict(), ensure_ascii=False)


@app.route('/api/settings/test', method='POST')
def settings_test():
    if not _settings_allowed() or not _settings_same_origin():
        abort(404, t("web.settings_only_local"))
    response.content_type = 'application/json'
    data = _json_object()
    if data is None:
        return _json_error(400, t("web.invalid_json"))
    try:
        saved = load_config()
    except ConfigError as e:
        return _json_error(500, t("web.settings_error", error=e))
    from dataclasses import replace
    candidate = replace(
        saved,
        provider=str(data.get("provider") or saved.provider),
        base_url=str(data.get("base_url", saved.base_url) or "").strip(),
        model=str(data.get("model", saved.model) or "").strip(),
        api_key=(str(data["api_key"]).strip()
                 if str(data.get("api_key") or "").strip()
                 else saved.api_key),
    )
    if not candidate.is_configured():
        return json.dumps({"ok": False,
                           "error": t("web.agent_not_configured")})
    try:
        ok, message = test_connection(candidate)
    except NoProviderPackage as e:
        return json.dumps({"ok": False, "error": str(e)})
    return json.dumps({"ok": ok, "error": "" if ok else message})


@app.route('/reports/<filepath:path>')
def serve_report(filepath):
    """Serve report pages, and nothing else kept in the directory.

    Uploads in progress, fleet.db and history.json live beside them, and
    static_file labels what it does not recognise as text/html, so text
    from a log stored in fleet.db would run as a page of this origin.
    """
    if os.path.basename(filepath) != filepath or not filepath.endswith('.html'):
        abort(404, t("web.report_not_found"))
    return static_file(filepath, root=REPORT_DIR)


def _fleet_db_path() -> str:
    return os.path.join(REPORT_DIR, "fleet.db")


@app.route('/fleet')
def fleet_page():
    """Fleet archive overview: every airframe in this report dir's fleet.db."""
    return _build_fleet_html()


@app.route('/fleet/<key>')
def fleet_detail(key):
    """Per-airframe drilldown: trend figure + flight table."""
    # Bottle decodes path segments as Latin-1; recover UTF-8 aliases
    try:
        key = key.encode('latin-1').decode('utf-8')
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    db_path = _fleet_db_path()
    serial = fleet_history.resolve_serial(db_path, key)
    if serial is None:
        response.status = 404
        msg = html.escape(t("fleet.web_not_found", key=key))
        return _build_fleet_shell(
            t("fleet.web_title"),
            f'<div class="card"><p class="empty">{msg}</p></div>')
    return _build_airframe_html(db_path, serial)


@app.route('/fleet/<key>/alias', method='POST')
def fleet_set_alias(key):
    """Set (or clear, when empty) an airframe alias from the detail page."""
    # Bottle decodes path segments as Latin-1; recover UTF-8 aliases
    try:
        key = key.encode('latin-1').decode('utf-8')
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    db_path = _fleet_db_path()
    serial = fleet_history.resolve_serial(db_path, key)
    if serial is None:
        response.status = 404
        msg = html.escape(t("fleet.web_not_found", key=key))
        return _build_fleet_shell(
            t("fleet.web_title"),
            f'<div class="card"><p class="empty">{msg}</p></div>')
    alias = (request.forms.getunicode('alias') or '').strip()
    try:
        fleet_history.set_alias(db_path, serial, alias)
    except fleet_history.AliasTaken as e:
        response.status = 409
        return _build_fleet_shell(
            t("fleet.web_title"),
            f'<div class="card"><p class="empty">{html.escape(str(e))}</p>'
            f'</div>',
            back_href=f'/fleet/{serial}')
    redirect(f'/fleet/{serial}')


@app.route('/api/history')
def get_history():
    """Return persistent analysis history."""
    response.content_type = 'application/json'
    with _HISTORY_LOCK:
        snapshot = list(HISTORY)
    return json.dumps(_finite({'history': snapshot}))


@app.route('/api/history', method='DELETE')
def clear_history():
    """Clear analysis history."""
    with _HISTORY_LOCK:
        HISTORY.clear()
        _save_history()
    response.content_type = 'application/json'
    return json.dumps({'ok': True})


def _load_history():
    """Load history from disk."""
    global HISTORY
    if HISTORY_FILE and os.path.isfile(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
            if not isinstance(loaded, list):
                raise ValueError(type(loaded).__name__)
            for entry in loaded:
                entry.setdefault('id', uuid.uuid4().hex[:8])
            HISTORY = loaded
        except Exception as e:
            # Set it aside: the next save would overwrite the only copy.
            aside = HISTORY_FILE + ".corrupt"
            try:
                os.replace(HISTORY_FILE, aside)
            except OSError:
                aside = HISTORY_FILE
            print(t("web.history_unreadable", path=aside, detail=e),
                  file=sys.stderr)
            HISTORY = []


def _save_history():
    """Save history to disk.

    Written beside the file and swapped in. Rewriting in place truncates
    first, so a kill or a full disk mid-write lost all of it.
    """
    if not HISTORY_FILE:
        return
    temp = f"{HISTORY_FILE}.{os.getpid()}.tmp"
    try:
        with open(temp, 'w', encoding='utf-8') as f:
            json.dump(_finite(HISTORY), f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, HISTORY_FILE)
    except (OSError, TypeError, ValueError) as e:
        print(t("web.history_save_failed", detail=e), file=sys.stderr)
        try:
            os.remove(temp)
        except OSError:
            pass


def _append_history(job: dict):
    """Add a completed job to history."""
    from datetime import datetime
    entry = {
        # Comparing goes by id: positions shift when the list is trimmed,
        # and a page left open would then compare the wrong flights.
        'id': job.get('id') or uuid.uuid4().hex[:8],
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'filename': job.get('filename', ''),
        'vehicle_info': job.get('vehicle_info'),
        'health_score': job.get('health_score'),
        'metrics': job.get('metrics'),
        'analyzer_results': job.get('analyzer_results'),
    }
    with _HISTORY_LOCK:
        HISTORY.append(entry)
        if len(HISTORY) > 100:
            HISTORY[:] = HISTORY[-100:]
        _save_history()


# =========================================================================
# Analysis worker (runs in background thread)
# =========================================================================

# ---------------------------------------------------------------------------
# Needed message types (collected from all analyzers)
# ---------------------------------------------------------------------------


def _run_analysis(job_id: str, filepath: str, original_name: str,
                   template_name: str = "", locale: str = "",
                   use_agent: bool = False):
    """Run the full analysis pipeline for a single .BIN file."""
    # The worker runs in its own thread; the before_request hook does not
    # fire here, and the i18n locale is thread-local, so re-apply the UI
    # locale or the whole report renders in the default language.
    if locale:
        from lib.i18n import set_locale
        set_locale(locale)
    _update_job(job_id, message=t("web.status_queued"), progress=0)
    memory = 0
    try:
        memory = _estimated_peak_bytes(os.path.getsize(filepath))
        _ANALYSIS_BUDGET.acquire(memory)
        _update_job(job_id, status='running',
                    message=t("web.reading_log"), progress=5)

        template = get_template(template_name) if template_name else None

        def _on_progress(pct, msg):
            _update_job(job_id, progress=pct, message=msg)

        pipeline = run_analysis(
            filepath,
            template=template,
            on_progress=_on_progress,
        )

        reader = pipeline.reader
        vehicle_info = pipeline.vehicle_info
        results = pipeline.results
        results_by_key = pipeline.results_by_key
        health_score = pipeline.health_score
        gps_track = pipeline.gps_track

        _update_job(job_id, vehicle_info={
            'type': vehicle_info.vehicle_type,
            'board': vehicle_info.board_name,
            'motors': vehicle_info.motor_count,
            'firmware': vehicle_info.firmware_version,
        })

        # ----- Extract metrics for multi-log comparison -----
        _update_job(job_id, message=t("web.extracting_metrics"), progress=80)
        try:
            metrics = extract_metrics(reader, pipeline.thresholds)
            _update_job(job_id, metrics=metrics, params=dict(reader.params))
        except Exception:
            _update_job(job_id, metrics={})

        # ----- AI diagnosis -----
        _update_job(job_id, message=t("web.ai_diagnosis"), progress=85)
        if results_by_key:
            try:
                if use_agent:
                    try:
                        agent_config = load_config()
                    except ConfigError as e:
                        agent_config = None
                        _update_job(job_id, agent_fallback=str(e))
                    if agent_config is not None and agent_config.is_configured():
                        _update_job(job_id,
                                    message=t("web.agent_analyzing"),
                                    progress=85)

                        def _on_agent(event):
                            if event.get("stage") == "tool":
                                _update_job(
                                    job_id,
                                    message=t("web.agent_tool",
                                              tool=event.get("tool", ""),
                                              calls=event.get("calls", 0)),
                                    agent_calls=event.get("calls", 0))
                            elif event.get("stage") == "fallback":
                                _update_job(job_id,
                                            agent_fallback=event.get("reason", ""))

                        diagnosis = analyze_with_agent(
                            pipeline, agent_config, on_event=_on_agent)
                    else:
                        diagnosis = generate_diagnosis(
                            results_by_key, vehicle_info, health_score,
                            reader.get_flight_duration(),
                        )
                        diagnosis.details.insert(
                            0, html.escape(t("web.agent_not_configured")))
                else:
                    diagnosis = generate_diagnosis(
                        results_by_key, vehicle_info, health_score,
                        reader.get_flight_duration(),
                    )
                results.insert(0, diagnosis)
            except Exception:
                pass

        # ----- Fleet history (archive + trend card) -----
        # Default on; failures never block the report.
        try:
            fleet_db = _fleet_db_path()
            serial, _n = fleet_history.record(
                fleet_db, pipeline, filepath, log_name=original_name)
            hist = fleet_history.build_history_result(
                fleet_history.query_history(fleet_db, serial), serial)
            if hist is not None:
                results.append(hist)
        except Exception as e:
            warning = t("fleet.record_failed", error=e)
            print(f"  [warn] {warning}", file=sys.stderr)
            _update_job(job_id, archive_warning=warning)

        # ----- Generate HTML report -----
        _update_job(job_id, message=t("web.generating_report"), progress=92)

        base = os.path.splitext(original_name)[0]
        output_name = f"{job_id}_{base}_report.html"
        output_path = os.path.join(REPORT_DIR, output_name)

        generator = HtmlReportGenerator(
            vehicle_info=vehicle_info,
            results=results,
            log_filename=original_name,
            flight_duration=reader.get_flight_duration(),
            health_score=health_score,
            gps_track=gps_track,
        )
        generator.write(output_path)
        _prune_reports()

        # ----- Build summary for UI -----
        analyzer_summary = []
        for r in results:
            analyzer_summary.append({
                'title': r.title,
                'status': r.status,
                'summary': r.summary,
            })

        with _JOBS_LOCK:
            job = JOBS.get(job_id)
            start_time = job.get('start_time', 0) if job else 0
        elapsed = round(time.time() - start_time, 1)

        _update_job(job_id, status='done', progress=100,
                    message=t("web.analysis_complete"), elapsed=elapsed,
                    result_url=f'/reports/{quote(output_name)}',
                    report_size=os.path.getsize(output_path),
                    analyzer_results=analyzer_summary,
                    health_score=health_score)

        # Read job snapshot under lock, then append history outside lock
        # (history write does file I/O, shouldn't hold _JOBS_LOCK).
        with _JOBS_LOCK:
            job_snapshot = JOBS.get(job_id)
        if job_snapshot:
            _append_history(job_snapshot)

    except Exception as e:
        _update_job(job_id, status='error',
                    message=t("web.analysis_failed", detail=e))

    finally:
        _ANALYSIS_BUDGET.release(memory)
        try:
            os.remove(filepath)
        except FileNotFoundError:
            pass
        except OSError as e:
            print(f"  [warn] could not delete upload {filepath}: {e}",
                  file=sys.stderr)


# =========================================================================
# Inline HTML / CSS / JS
# =========================================================================

def _build_index_html() -> str:
    """Render the upload page with this module's current limits."""
    return build_index_html(MAX_UPLOAD_BYTES, _ALLOW_SETTINGS)


def _build_fleet_html() -> str:
    return build_fleet_html(_fleet_db_path())


def main():
    # Force UTF-8 stdout on Windows (chcp 65001 alone isn't enough)
    if sys.platform == 'win32':
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

    parser = argparse.ArgumentParser(
        description="ArduScope - Web Interface",
    )
    parser.add_argument(
        "--port", type=int, default=8080,
        help="Web server port (default: 8080)",
    )
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="Listen address (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--allow-host", action="append", default=[], metavar="NAME",
        help="Also accept this Host name, for a DNS name or reverse proxy "
             "in front of the server (repeatable)",
    )
    parser.add_argument(
        "--no-open", action="store_true",
        help="Do not auto-open browser",
    )
    parser.add_argument(
        "--report-dir",
        help="Report output directory (default: ~/.arduscope/reports)",
    )
    parser.add_argument(
        "--lang",
        choices=["zh", "en"],
        default="zh",
        help="UI language (default: zh)",
    )
    parser.add_argument(
        "--server",
        choices=["dev", "waitress"],
        default="dev",
        help="WSGI server backend (default: dev; waitress for production)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Number of worker threads for waitress (default: 4)",
    )
    parser.add_argument(
        "--keep-reports",
        type=int,
        default=0,
        metavar="N",
        help="Delete the oldest generated reports past N (default: keep all)",
    )
    args = parser.parse_args()
    if 0 < args.keep_reports <= MAX_JOBS:
        parser.error(f"--keep-reports must be 0 or above {MAX_JOBS}, "
                     f"or the job list would link to deleted reports")
    global MAX_KEPT_REPORTS
    MAX_KEPT_REPORTS = args.keep_reports

    from lib.i18n import set_locale
    global _UI_LOCALE, REPORT_DIR, HISTORY_FILE, _ALLOW_SETTINGS
    _UI_LOCALE = args.lang
    _ALLOW_SETTINGS = _is_loopback(args.host)
    set_locale(args.lang)
    # The fleet archive and analysis history live here, so the default has
    # to be the same directory on every start, from any working directory.
    REPORT_DIR = os.path.abspath(args.report_dir or os.path.join(
        os.path.expanduser("~"), ".arduscope", "reports"))
    os.makedirs(REPORT_DIR, exist_ok=True)

    HISTORY_FILE = os.path.join(REPORT_DIR, "history.json")
    _load_history()
    _configure_allowed_hosts(args.host, args.allow_host)
    _warn_if_exposed(args.host)

    if args.server == "waitress":
        _run_waitress(args)
    else:
        _run_dev_server(args)


def _warn_if_exposed(host: str) -> None:
    """There is no login, so leaving loopback must not pass quietly."""
    if _is_loopback(host):
        return
    rule = "!" * 72
    print(f"\n{rule}\n{t('web.warn_exposed', host=host)}\n{rule}\n",
          file=sys.stderr)


def _run_waitress(args):
    """Production server using Waitress (works on Windows + Linux)."""
    try:
        import waitress
    except ImportError:
        print("Error: waitress not installed. Install with: pip install waitress",
              file=sys.stderr)
        sys.exit(1)

    url = f"http://{args.host}:{args.port}"
    print(f"\nArduScope (waitress, {args.workers} threads)")
    print(f"Web UI: {url}")
    print(f"Report dir: {REPORT_DIR}")
    print(f"Press Ctrl+C to quit\n")

    if not args.no_open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    waitress.serve(app, host=args.host, port=args.port,
                   threads=args.workers,
                   max_request_body_size=MAX_UPLOAD_BYTES, _quiet=False)


def _run_dev_server(args):
    """Development server with auto-retry port binding."""
    from wsgiref.simple_server import make_server, WSGIServer
    from socketserver import ThreadingMixIn

    class ThreadedWSGIServer(ThreadingMixIn, WSGIServer):
        daemon_threads = True

    port = args.port
    max_retries = 10
    server = None

    for attempt in range(max_retries):
        try:
            server = make_server(args.host, port, app,
                                 server_class=ThreadedWSGIServer)
            break
        except OSError as e:
            _winerr = getattr(e, 'winerror', None)
            _is_port_error = (
                _winerr in (10013, 10048)
                or e.errno in (98, 13)
                or "10013" in str(e)
                or "10048" in str(e)
                or "address already in use" in str(e).lower()
                or "permission" in str(e).lower()
            )
            if _is_port_error:
                if attempt == 0 and port == args.port:
                    print(f"\nPort {port} is in use or restricted, trying other ports...")
                port += 1
                if attempt == max_retries - 1:
                    print(f"\n{'='*60}")
                    print(f"  Startup failed! No available port found")
                    print(f"{'='*60}\n")
                    print(f"Tried port range: {args.port} - {port - 1}")
                    print(f"\nCommon causes:")
                    print(f"  1. Port reserved by Hyper-V / WSL / Docker (WinError 10013)")
                    print(f"  2. Blocked by firewall or antivirus")
                    print(f"  3. All candidate ports occupied by other programs\n")
                    print(f"Solutions:\n")
                    print(f"[Option 1] Specify a higher port number (recommended):")
                    print(f"  start.bat --port 19090\n")
                    print(f"[Option 2] Check Hyper-V reserved port ranges:")
                    print(f"  netsh int ipv4 show excludedportrange protocol=tcp\n")
                    print(f"[Option 3] Use waitress production server:")
                    print(f"  arduscope-web --server waitress --port 19090\n")
                    sys.exit(1)
            else:
                raise

    if server is None:
        print("Startup failed: unknown error")
        sys.exit(1)

    url = f"http://{args.host}:{port}"
    print(f"\nArduScope (dev)")
    print(f"Web UI: {url}")
    if port != args.port:
        print(f"Note: port {args.port} was in use, auto-switched to port {port}")
    print(f"Report dir: {REPORT_DIR}")
    print(f"Press Ctrl+C to quit\n")

    if not args.no_open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped")
        server.server_close()


if __name__ == "__main__":
    main()
