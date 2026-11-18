import os

import pytest


@pytest.fixture
def client(monkeypatch):
    import app as app_module
    with app_module.task_lock:
        app_module.task_state.update(status="idle", progress=0, total=0, current_file="", log_seq=0)
        app_module.task_state["logs"].clear()
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client(), app_module


HOST = {"base_url": "http://127.0.0.1:5000"}


def test_rejects_foreign_host(client):
    c, _ = client
    assert c.get("/api/status", base_url="http://evil.example:5000").status_code == 403


def test_rejects_foreign_origin(client):
    c, _ = client
    r = c.post("/api/convert", json={"folder_path": "."}, headers={"Origin": "http://evil.example"}, **HOST)
    assert r.status_code == 403


def test_non_json_body_is_400(client):
    c, _ = client
    r = c.post("/api/convert", data="hello", **HOST)
    assert r.status_code == 400


def test_invalid_folder_is_400(client):
    c, _ = client
    r = c.post("/api/convert", json={"folder_path": "Z:/definitely/missing"}, **HOST)
    assert r.status_code == 400


def test_second_start_while_running_is_409(client, monkeypatch, tmp_path):
    c, mod = client
    started = []
    monkeypatch.setattr(mod, "run_script", lambda cmd: started.append(cmd))
    assert c.post("/api/convert", json={"folder_path": str(tmp_path)}, **HOST).status_code == 200
    assert c.post("/api/convert", json={"folder_path": str(tmp_path)}, **HOST).status_code == 409
    cmd = started[0]
    assert "--keep-originals" in cmd, "keeping originals must be the default"


def test_status_incremental_logs(client):
    c, mod = client
    with mod.task_lock:
        for i in range(3):
            mod.append_log(f"line {i}")
    data = c.get("/api/status?since=1", **HOST).get_json()
    assert data["logs"] == ["line 1", "line 2"] and data["log_seq"] == 3


def test_real_run_reports_progress_and_unicode_logs(client, tmp_path):
    """Runs the actual converter subprocess through the Flask background thread."""
    import time
    from PIL import Image
    c, mod = client
    for i in range(3):
        Image.new("RGB", (8, 8), "red").save(tmp_path / f"画像_{i}.png")

    r = c.post("/api/convert", json={"folder_path": str(tmp_path), "workers": 2}, **HOST)
    assert r.status_code == 200
    deadline = time.time() + 60
    while time.time() < deadline:
        data = c.get("/api/status", **HOST).get_json()
        if data["status"] != "running":
            break
        time.sleep(0.2)

    if data["status"] == "error" and any("Missing required tools" in l for l in data["logs"]):
        pytest.skip("cwebp/exiftool not available")
    assert data["status"] == "completed", data["logs"]
    assert data["progress"] == 100 and data["total"] == 3
    assert any("画像_0.png" in l for l in data["logs"]), "non-ASCII file names must survive"
    assert len(list(tmp_path.glob("*.png"))) == 3, "originals kept by default"
    assert len(list(tmp_path.glob("*.webp"))) == 3
