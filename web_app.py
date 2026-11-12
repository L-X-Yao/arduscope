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
from collections import deque
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
from lib.agent.config import (CONFIG_PATH, ConfigError, load_config,
                              update_config)
from lib.agent.providers import NoProviderPackage, test_connection
from lib.pipeline import run_analysis
from lib.templates import get_template

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
# Peak memory of one analysis, fitted above the peak of a server that had
# analysed the same log four times running: 0.23 GB for a 5 MB log, 0.43 GB
# for 49 MB, 0.97 GB for 229 MB, 3.30 GB for 663 MB and 4.13 GB for 813 MB.
# Past the base that is up to 4.8 bytes for each byte of log. The first
# analysis in a new process peaks a tenth to a third lower, and the Copter
# and Plane logs measured take less for each byte than these QuadPlane logs.
ANALYSIS_BASE_BYTES = 200 * 1024 * 1024
ANALYSIS_PEAK_PER_LOG_BYTE = 6
# What running analyses may use together: leaves room for the system and a
# browser on a 16 GB field laptop.
ANALYSIS_MEMORY_BUDGET_BYTES = 8 * 1024 * 1024 * 1024
# 0 keeps every report: they are the user's work, and only --keep-reports
# may delete them. It has to stay above MAX_JOBS, or the job list would
# link to deleted pages.
MAX_KEPT_REPORTS = 0
_GENERATED_REPORT = re.compile(
    r'(?:[0-9a-f]{8}_.*_report|compare_[0-9a-f]{8})\.html')
# Characters a Windows file name cannot hold, and control characters.
_UNSAFE_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
# upload_<id>_ goes before an upload's name and <id>_..._report.html around
# its stem, and a file name holds 255 bytes at most.
MAX_NAME_BYTES = 200
# The largest JSON body a route reads: the compare routes take a list of
# at most MAX_JOBS ids of 8 characters.
MAX_JSON_BYTES = 64 * 1024


def _clean_upload_name(raw: str) -> str:
    """The name an upload is stored, listed and reported under.

    It comes from the request and goes into two file names. A NUL in it
    raised past the code that frees the upload's slot, and a colon made
    Windows write the log into a stream of an empty file that stayed
    behind.
    """
    raw = raw.encode('utf-8', 'ignore').decode('utf-8')
    name = _UNSAFE_NAME_CHARS.sub('_', os.path.basename(raw.replace('\\', '/')))
    stem, ext = os.path.splitext(name)
    room = max(MAX_NAME_BYTES - len(ext.encode('utf-8')), 0)
    return stem.encode('utf-8')[:room].decode('utf-8', 'ignore') + ext


def _estimated_peak_bytes(log_bytes: int) -> int:
    return ANALYSIS_BASE_BYTES + ANALYSIS_PEAK_PER_LOG_BYTE * log_bytes


class _MemoryBudget:
    """Admit analyses in arrival order while their memory fits.

    Memory follows log size, so counting analyses says nothing: three
    logs at the upload limit are put at 19 GB, three small ones under 1 GB.
    Arrival order keeps a stream of small logs from starving a large one.
    An analysis over the whole budget still runs, once it is alone.
    """

    def __init__(self, limit: int):
        self._limit = limit
        self._used = 0
        self._waiting: deque[object] = deque()
        self._changed = threading.Condition()

    def acquire(self, cost: int) -> None:
        ticket = object()
        with self._changed:
            self._waiting.append(ticket)
            while (self._waiting[0] is not ticket
                   or (self._used and self._used + cost > self._limit)):
                self._changed.wait()
            self._waiting.popleft()
            self._used += cost
            self._changed.notify_all()

    def release(self, cost: int) -> None:
        with self._changed:
            self._used -= cost
            self._changed.notify_all()


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


def _build_settings_html(config, error: str = "") -> str:
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
       if _ALLOW_SETTINGS else "")
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
                               max_mb=MAX_UPLOAD_BYTES // 1024 // 1024),
        "js.agent_confirm": t("web.agent_egress_confirm"),
        "js.agent_tag": t("web.agent_tag"),
    }, ensure_ascii=False)
    + f";\nconst MAX_UPLOAD_BYTES = {MAX_UPLOAD_BYTES};"
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


def _build_fleet_html() -> str:
    """Airframe list page."""
    frames = fleet_history.list_airframes(_fleet_db_path())
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


def _build_airframe_html(db_path: str, serial: str) -> str:
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


# =========================================================================
# Main
# =========================================================================

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
