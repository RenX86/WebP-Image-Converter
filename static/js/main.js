document.addEventListener('DOMContentLoaded', () => {
    const qualitySlider = document.getElementById('quality');
    const qualityVal = document.getElementById('quality-val');
    const workersSlider = document.getElementById('workers');
    const workersVal = document.getElementById('workers-val');
    const startBtn = document.getElementById('start-btn');
    const folderPath = document.getElementById('folder-path');
    const keepOriginals = document.getElementById('keep-originals');
    const lossless = document.getElementById('lossless');
    const logsContainer = document.getElementById('logs');
    const progressContainer = document.getElementById('progress-container');
    const progressText = document.getElementById('progress-text');
    const progressFill = document.getElementById('progress-fill');
    const progressFile = document.getElementById('progress-file');

    let pollTimer = null;
    let logSeq = 0;  // last log sequence number received from the server

    // UI Updates
    qualitySlider.addEventListener('input', (e) => {
        qualityVal.textContent = e.target.value;
    });

    workersSlider.addEventListener('input', (e) => {
        workersVal.textContent = e.target.value;
    });

    lossless.addEventListener('change', () => {
        qualitySlider.disabled = lossless.checked;
        qualitySlider.closest('.range-group').classList.toggle('disabled', lossless.checked);
    });

    keepOriginals.addEventListener('change', () => {
        if (!keepOriginals.checked &&
            !confirm('Originals will be permanently deleted after each successful conversion. Continue?')) {
            keepOriginals.checked = true;
        }
    });

    function classify(log) {
        // Prefer the logging level prefix so file names containing "error" etc. don't mislead
        if (log.startsWith('ERROR')) return 'error';
        if (log.startsWith('WARNING')) return 'warning';
        if (log.startsWith('INFO - Successfully')) return 'success';
        if (log.startsWith('INFO') || log.startsWith('DEBUG')) return 'system';
        const l = log.toLowerCase();
        if (l.includes('error') || l.includes('failed')) return 'error';
        if (l.includes('success')) return 'success';
        return 'system';
    }

    // Logging helper: only auto-scroll if the user is already at the bottom
    function appendLogs(messages) {
        if (!messages.length) return;
        const atBottom = logsContainer.scrollHeight - logsContainer.scrollTop - logsContainer.clientHeight < 30;
        const frag = document.createDocumentFragment();
        messages.forEach(message => {
            const div = document.createElement('div');
            div.className = `log-entry ${classify(message)}`;
            div.textContent = message;
            frag.appendChild(div);
        });
        logsContainer.appendChild(frag);
        if (atBottom) logsContainer.scrollTop = logsContainer.scrollHeight;
    }

    function clearLogs() {
        logsContainer.innerHTML = '';
    }

    function setBusy(busy) {
        startBtn.disabled = busy;
        startBtn.textContent = busy ? 'Processing...' : 'Start Conversion';
    }

    function renderProgress(data) {
        if (data.status === 'idle') return;
        progressContainer.style.display = 'block';
        progressContainer.classList.toggle('has-errors',
            data.status === 'error' || data.status === 'completed_with_errors');
        progressText.textContent = `${data.progress || 0}%`;
        progressFill.style.width = `${data.progress || 0}%`;

        let text = data.current_file || 'Preparing...';
        if (data.status === 'completed') text = 'Conversion finished successfully!';
        else if (data.status === 'completed_with_errors') text = 'Finished with errors (see log)';
        else if (data.status === 'error') text = 'Conversion failed (see log)';
        progressFile.textContent = text;
        progressFile.title = text;
    }

    async function checkStatus() {
        try {
            const res = await fetch(`/api/status?since=${logSeq}`);
            const data = await res.json();

            if (data.reset) clearLogs();
            appendLogs(data.logs);
            logSeq = data.log_seq;
            renderProgress(data);

            if (data.status === 'running') {
                setBusy(true);
                pollTimer = setTimeout(checkStatus, 500);
            } else {
                setBusy(false);
                pollTimer = null;
            }
        } catch (e) {
            console.error("Failed to fetch status", e);
            pollTimer = setTimeout(checkStatus, 2000);
        }
    }

    function startPolling() {
        if (pollTimer) clearTimeout(pollTimer);
        checkStatus();
    }

    startBtn.addEventListener('click', async () => {
        const path = folderPath.value.trim();
        if (!path) {
            alert('Please enter a folder path.');
            return;
        }

        setBusy(true);
        clearLogs();
        progressContainer.style.display = 'none';

        try {
            const res = await fetch('/api/convert', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    folder_path: path,
                    quality: parseInt(qualitySlider.value, 10),
                    workers: parseInt(workersSlider.value, 10),
                    keep_originals: keepOriginals.checked,
                    lossless: lossless.checked
                })
            });

            const data = await res.json();

            if (!res.ok) {
                appendLogs([data.error || 'Failed to start']);
                setBusy(false);
                return;
            }

            startPolling();

        } catch (e) {
            appendLogs(['Network error occurred.']);
            setBusy(false);
        }
    });

    // Resume showing an in-progress (or the last) run after a page reload
    fetch('/api/status').then(r => r.json()).then(data => {
        if (data.status === 'idle') return;
        clearLogs();
        appendLogs(data.logs);
        logSeq = data.log_seq;
        renderProgress(data);
        if (data.status === 'running') startPolling();
    }).catch(() => {});
});
