from flask import Flask, render_template, request, jsonify
import threading
import subprocess
import os

app = Flask(__name__)

# Global state to track background task
task_state = {
    "status": "idle",  # idle, running, completed, error
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
    task_state['logs'] = ["Starting conversion process..."]

    def run_script():
        global task_state
        try:
            cmd = ['python', 'WebP-Image-Converter.py', folder_path, '-q', str(quality), '-w', str(workers)]
            if keep_originals:
                cmd.append('--keep-originals')

            # We use Popen to stream the output
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            
            for line in iter(process.stdout.readline, ''):
                task_state['logs'].append(line.strip())
            
            process.stdout.close()
            process.wait()
            
            if process.returncode == 0:
                task_state['status'] = 'completed'
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
        "logs": task_state["logs"][-100:]  # Return last 100 logs to avoid overwhelming the client
    })

if __name__ == '__main__':
    app.run(debug=True, port=5000)
