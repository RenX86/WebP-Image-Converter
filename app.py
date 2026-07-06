from flask import Flask, render_template, request, jsonify
import threading
import subprocess
import os

app = Flask(__name__)

# Global state to track background task
task_state = {
    "status": "idle",  # idle, running, completed, error
    "progress": 0,
    "total": 0,
    "current_file": "",
    "logs": []
}

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/convert', methods=['POST'])
def start_conversion():
    global task_state
    
    if task_state['status'] == 'running':
        return jsonify({"error": "A conversion task is already running."}), 400

    data = request.json
    folder_path = data.get('folder_path')
    quality = data.get('quality', 100)
    workers = data.get('workers', 4)
    keep_originals = data.get('keep_originals', False)

    if not folder_path or not os.path.exists(folder_path):
        return jsonify({"error": "Invalid or missing folder path."}), 400

    task_state['status'] = 'running'
    task_state['progress'] = 0
    task_state['total'] = 0
    task_state['current_file'] = ""
    task_state['logs'] = ["Starting conversion process..."]

    def run_script():
        global task_state
        import re
        try:
            cmd = ['python', 'WebP-Image-Converter.py', folder_path, '-q', str(quality), '-w', str(workers)]
            if keep_originals:
                cmd.append('--keep-originals')

            # We use Popen without text=True to read char by char and handle \r
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            
            buffer = ""
            while True:
                char = process.stdout.read(1)
                if not char:
                    break
                
                try:
                    c = char.decode('utf-8')
                except UnicodeDecodeError:
                    continue
                    
                if c == '\r' or c == '\n':
                    line = buffer.strip()
                    buffer = ""
                    if not line:
                        continue
                    
                    # Parse progress
                    prog_match = re.search(r'Converting images:\s*(\d+)%.*?(\d+)/(\d+)', line)
                    if prog_match:
                        task_state['progress'] = int(prog_match.group(1))
                        task_state['total'] = int(prog_match.group(3))
                    
                    # Parse current file
                    if 'INFO - Processing:' in line:
                        file_path = line.split('INFO - Processing: ')[-1]
                        task_state['current_file'] = file_path.strip()
                    
                    # Keep clean logs
                    if 'Converting images:' not in line or 'INFO' in line:
                        if 'INFO - ' in line:
                            clean_log = 'INFO - ' + line.split('INFO - ')[1]
                            task_state['logs'].append(clean_log)
                        else:
                            task_state['logs'].append(line)
                else:
                    buffer += c
                    
            process.wait()
            
            if process.returncode == 0:
                task_state['status'] = 'completed'
                task_state['progress'] = 100
                task_state['current_file'] = "Done"
                task_state['logs'].append("Conversion finished successfully!")
            else:
                task_state['status'] = 'error'
                task_state['logs'].append(f"Process exited with code {process.returncode}")
                
        except Exception as e:
            task_state['status'] = 'error'
            task_state['logs'].append(f"Internal error: {str(e)}")

    # Start task in background
    thread = threading.Thread(target=run_script)
    thread.daemon = True
    thread.start()

    return jsonify({"message": "Conversion started."})

@app.route('/api/status', methods=['GET'])
def get_status():
    global task_state
    return jsonify({
        "status": task_state["status"],
        "progress": task_state["progress"],
        "total": task_state["total"],
        "current_file": task_state["current_file"],
        "logs": task_state["logs"][-100:]  # Return last 100 logs to avoid overwhelming the client
    })

if __name__ == '__main__':
    app.run(debug=True, port=5000)
