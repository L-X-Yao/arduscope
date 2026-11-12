"""Upload logs to a packaged web server and report what stays behind.

Runs on Windows under the package's own python.exe, so it uses the standard
library only. tools/win_package_check.py copies it there and reads its report.

Usage: win_package_driver.py <package> <reports> <inputs> <port> <out.jsonl>
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

package, reports, inputs, port, out = sys.argv[1:6]
base = f"http://127.0.0.1:{port}"
sys.stdout.reconfigure(encoding="utf-8")
os.makedirs(reports, exist_ok=True)
server_log = open(out + ".server.log", "ab")
results = open(out, "w", encoding="utf-8")


def say(**fields):
    line = json.dumps(fields, ensure_ascii=False)
    print(line, flush=True)
    results.write(line + "\n")
    results.flush()


def port_answers():
    try:
        urllib.request.urlopen(base + "/", timeout=2).read()
        return True
    except Exception:
        return False


def start():
    server = subprocess.Popen(
        [os.path.join(package, "python", "python.exe"),
         os.path.join("log_analysis", "web_app.pyc"),
         "--report-dir", reports, "--no-open", "--port", str(port)],
        cwd=package, stdout=server_log, stderr=server_log)
    for _ in range(240):
        if server.poll() is not None:
            say(event="server_exit_at_start", code=server.returncode)
            return server
        if port_answers():
            say(event="server_up", pid=server.pid)
            return server
        time.sleep(0.5)
    say(event="server_no_answer")
    return server


# Names no file on Windows can have, sent with the first log's bytes. The
# server has to store each under a name it can: a colon wrote the log into
# a stream of an empty file, which stayed behind.
ODD_NAMES = {
    "odd name, colon": "flight:1.BIN",
    "odd name, NUL": "a\x00b.BIN",
    "odd name, reserved characters": 'c<d>|e?.BIN',
    "odd name, 80 Chinese characters": "\u957f" * 80 + ".BIN",
}


def upload(name, source=None, label=None):
    """Job id, or None with the failure reported."""
    with open(os.path.join(inputs, source or name), "rb") as f:
        body = f.read()
    url = f"{base}/api/upload?filename={urllib.parse.quote(name)}"
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/octet-stream"})
    try:
        return json.load(urllib.request.urlopen(request, timeout=120))["job_id"]
    except urllib.error.HTTPError as e:
        say(case=label or name, status=f"http {e.code}",
            server_alive=proc.poll() is None,
            message=e.read().decode("utf-8", "replace")[:160])
    except Exception as e:
        say(case=label or name, status="upload failed", message=repr(e)[:160],
            server_alive=proc.poll() is None)
    return None


def wait_for(job):
    status, message = "?", ""
    for _ in range(1800):
        if proc.poll() is not None:
            return "server_died", message
        try:
            info = json.load(
                urllib.request.urlopen(f"{base}/api/jobs/{job}", timeout=10))
        except Exception:
            time.sleep(0.5)
            continue
        status, message = info.get("status"), info.get("message", "")
        if status in ("done", "error"):
            break
        time.sleep(0.5)
    return status, message


# A server left over from an earlier run would answer in place of this one.
if port_answers():
    say(event="port_busy")
    sys.exit(1)

proc = start()
try:
    page = urllib.request.urlopen(base + "/settings", timeout=10)
    body = page.read().decode("utf-8", "replace")
    say(event="settings_page", ok=page.status == 200 and len(body) > 0)
except Exception as e:
    say(event="settings_page", ok=False, error=repr(e)[:160])
files = sorted(os.listdir(inputs))
sent = [(name, name, None) for name in files]
sent += [(case, name, "normal.BIN") for case, name in ODD_NAMES.items()]
for case, sent_name, source in sent:
    if proc.poll() is not None:
        proc = start()
    t0 = time.time()
    job_id = upload(sent_name, source, case)
    if job_id is None:
        continue
    job_status, job_message = wait_for(job_id)
    time.sleep(1.0)
    left = sorted(f for f in os.listdir(reports) if f.startswith("upload_"))
    say(case=case, job=job_id, status=job_status, message=str(job_message)[:110],
        secs=round(time.time() - t0, 1), uploads_left=left,
        server_alive=proc.poll() is None)

say(event="all_files", files=sorted(os.listdir(reports)))
proc.terminate()
try:
    proc.wait(timeout=10)
except subprocess.TimeoutExpired:
    proc.kill()
say(event="stopped", code=proc.returncode)
