from flask import Flask, render_template, request, jsonify, abort
from collections import deque
import io
import os
import re
import subprocess
import sys
import threading

app = Flask(__name__)

HOST = os.environ.get("WEBP_HOST", "127.0.0.1")
PORT = int(os.environ.get("WEBP_PORT", "5000"))
DEBUG = os.environ.get("WEBP_DEBUG", "").lower() in ("1", "true", "yes")
MAX_LOG_LINES = 2000

# Exit codes returned by WebP-Image-Converter.py
EXIT_OK = 0
EXIT_PARTIAL_FAILURE = 1

PROGRESS_RE = re.compile(r'Converting images:\s*(\d+)%.*?(\d+)/(\d+)')

# Lock for thread-safe access to task_state
task_lock = threading.Lock()

# Global state to track background task
task_state = {
    "status": "idle",  # idle, running, completed, completed_with_errors, error
    "progress": 0,
    "total": 0,
    "current_file": "",
    "logs": deque(maxlen=MAX_LOG_LINES),  # bounded so long runs don't grow memory forever
    "log_seq": 0,  # total number of log lines ever appended (lets the client fetch only new lines)
}


def append_log(line):
    """Append a log line. Caller must hold task_lock."""
    task_state['logs'].append(line)
    task_state['log_seq'] += 1


@app.before_request
def reject_foreign_hosts():
    """
    This server can delete files, so only answer requests addressed to localhost.
    Blocks DNS-rebinding attacks (foreign Host header) and cross-site POSTs (foreign Origin).
    """
    allowed = {f"127.0.0.1:{PORT}", f"localhost:{PORT}", f"[::1]:{PORT}"}
    if HOST not in ("127.0.0.1", "localhost", "::1"):
        allowed.add(f"{HOST}:{PORT}")
    if request.host not in allowed:
        abort(403)
    origin = request.headers.get("Origin")
    if origin and origin.split("://", 1)[-1] not in allowed:
        abort(403)


@app.route('/')
def index():
    return render_template('index.html')


def _int_in_range(value, default, lo, hi):
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def run_script(cmd):
    try:
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)

        # Decode as UTF-8 stream (multi-byte chars like non-ASCII file names stay intact).
        # newline='' keeps '\r' so tqdm's in-place progress updates can be split on.
        stream = io.TextIOWrapper(process.stdout, encoding='utf-8', errors='replace', newline='')
        buffer = ""
        while True:
            chunk = stream.read(1)
            if not chunk:
                break
            if chunk not in ('\r', '\n'):
                buffer += chunk
                continue

            line = buffer.strip()
            buffer = ""
            if not line:
                continue

            # Parse progress
            prog_match = PROGRESS_RE.search(line)
            if prog_match:
                with task_lock:
                    task_state['progress'] = int(prog_match.group(1))
                    task_state['total'] = int(prog_match.group(3))
                continue  # progress bar redraws are not log lines

            with task_lock:
                # Parse current file
                if 'INFO - Processing:' in line:
                    task_state['current_file'] = line.split('INFO - Processing: ', 1)[-1].strip()
                append_log(line)

        process.wait()

        with task_lock:
            if process.returncode == EXIT_OK:
                task_state['status'] = 'completed'
                task_state['progress'] = 100
                task_state['current_file'] = "Done"
                append_log("Conversion finished successfully!")
            elif process.returncode == EXIT_PARTIAL_FAILURE:
                task_state['status'] = 'completed_with_errors'
                task_state['progress'] = 100
                task_state['current_file'] = "Finished with errors"
                append_log("Conversion finished, but some files failed or were skipped. See the log above.")
            else:
                task_state['status'] = 'error'
                append_log(f"Conversion failed (exit code {process.returncode}).")

    except Exception as e:
        with task_lock:
            task_state['status'] = 'error'
            append_log(f"Internal error: {e}")


@app.route('/api/convert', methods=['POST'])
def start_conversion():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object."}), 400

    folder_path = str(data.get('folder_path') or '').strip().strip('"\'')
    quality = _int_in_range(data.get('quality'), 100, 0, 100)
    workers = _int_in_range(data.get('workers'), 4, 1, 64)
    keep_originals = bool(data.get('keep_originals', True))
    lossless = bool(data.get('lossless', False))

    if not folder_path or not os.path.isdir(folder_path):
        return jsonify({"error": "Invalid or missing folder path."}), 400

    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'WebP-Image-Converter.py')
    cmd = [sys.executable, script_path, folder_path, '-q', str(quality), '-w', str(workers),
           '--keep-originals' if keep_originals else '--delete-originals',
           '--lossless' if lossless else '--lossy']

    # Check-and-set under one lock acquisition so two requests can't both start a task
    with task_lock:
        if task_state['status'] == 'running':
            return jsonify({"error": "A conversion task is already running."}), 409
        task_state['status'] = 'running'
        task_state['progress'] = 0
        task_state['total'] = 0
        task_state['current_file'] = ""
        task_state['logs'].clear()
        append_log("Starting conversion process...")

    # Start task in background
    threading.Thread(target=run_script, args=(cmd,), daemon=True).start()

    return jsonify({"message": "Conversion started."})


@app.route('/api/status', methods=['GET'])
def get_status():
    # ?since=N returns only log lines after sequence number N
    since = _int_in_range(request.args.get('since'), 0, 0, sys.maxsize)
    with task_lock:
        seq = task_state['log_seq']
        logs = list(task_state['logs'])
        new_count = max(0, seq - since)
        reset = new_count > len(logs)  # client fell behind the bounded buffer
        return jsonify({
            "status": task_state["status"],
            "progress": task_state["progress"],
            "total": task_state["total"],
            "current_file": task_state["current_file"],
            "logs": logs if reset else logs[len(logs) - new_count:],
            "log_seq": seq,
            "reset": reset,
        })


if __name__ == '__main__':
    print(f"WebP Converter UI: http://{'localhost' if HOST == '127.0.0.1' else HOST}:{PORT}")
    app.run(host=HOST, port=PORT, debug=DEBUG)
