document.addEventListener('DOMContentLoaded', () => {
    const qualitySlider = document.getElementById('quality');
    const qualityVal = document.getElementById('quality-val');
    const workersSlider = document.getElementById('workers');
    const workersVal = document.getElementById('workers-val');
    const startBtn = document.getElementById('start-btn');
    const folderPath = document.getElementById('folder-path');
    const keepOriginals = document.getElementById('keep-originals');
    const logsContainer = document.getElementById('logs');
    
    let pollInterval = null;

    // UI Updates
    qualitySlider.addEventListener('input', (e) => {
        qualityVal.textContent = e.target.value;
    });

    workersSlider.addEventListener('input', (e) => {
        workersVal.textContent = e.target.value;
    });

    // Logging helper
    function appendLog(message, type = '') {
        const div = document.createElement('div');
        div.className = `log-entry ${type}`;
        div.textContent = message;
        logsContainer.appendChild(div);
        logsContainer.scrollTop = logsContainer.scrollHeight;
    }

    function clearLogs() {
        logsContainer.innerHTML = '';
    }

    async function checkStatus() {
        try {
            const res = await fetch('/api/status');
            const data = await res.json();
            
            clearLogs();
            data.logs.forEach(log => {
                let type = '';
                if (log.toLowerCase().includes('error') || log.toLowerCase().includes('failed')) type = 'error';
                else if (log.toLowerCase().includes('success')) type = 'success';
                else type = 'system';
                appendLog(log, type);
            });

            if (data.status === 'running' || data.status === 'completed') {
                document.getElementById('progress-container').style.display = 'block';
                document.getElementById('progress-text').textContent = `${data.progress || 0}%`;
                document.getElementById('progress-fill').style.width = `${data.progress || 0}%`;
                
                if (data.current_file) {
                    let text = data.current_file;
                    if (data.status === 'completed') text = 'Conversion finished successfully!';
                    document.getElementById('progress-file').textContent = text;
                    document.getElementById('progress-file').title = text;
                }
            }

            if (data.status === 'completed' || data.status === 'error') {
                clearInterval(pollInterval);
                startBtn.disabled = false;
                startBtn.textContent = 'Start Conversion';
            }
        } catch (e) {
            console.error("Failed to fetch status", e);
        }
    }

    startBtn.addEventListener('click', async () => {
        const path = folderPath.value.trim();
        if (!path) {
            alert('Please enter a folder path.');
            return;
        }

        startBtn.disabled = true;
        startBtn.textContent = 'Processing...';
        clearLogs();
        appendLog('Initializing...', 'system');

        try {
            const res = await fetch('/api/convert', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    folder_path: path,
                    quality: parseInt(qualitySlider.value),
                    workers: parseInt(workersSlider.value),
                    keep_originals: keepOriginals.checked
                })
            });

            const data = await res.json();
            
            if (!res.ok) {
                appendLog(data.error || 'Failed to start', 'error');
                startBtn.disabled = false;
                startBtn.textContent = 'Start Conversion';
                return;
            }

            // Start polling
            if (pollInterval) clearInterval(pollInterval);
            pollInterval = setInterval(checkStatus, 500);

        } catch (e) {
            appendLog('Network error occurred.', 'error');
            startBtn.disabled = false;
            startBtn.textContent = 'Start Conversion';
        }
    });
});
