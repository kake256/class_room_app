import importlib.util
import pathlib

from fastapi.testclient import TestClient


def load_controller(monkeypatch):
    monkeypatch.setenv("CGA_MODEL_CONTROLLER_SECRET", "controller-test-secret")
    monkeypatch.setenv("VLLM_BASE_URL", "http://host.docker.internal:8000")
    path = pathlib.Path("scripts/model-controller.py")
    spec = importlib.util.spec_from_file_location("model_controller", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def test_controller_rejects_auth_profile_and_parallel_switch(monkeypatch):
    module = load_controller(monkeypatch)
    client = TestClient(module.app)
    assert client.post("/switch", json={"profile": "q25-7b"}).status_code == 401
    headers = {"Authorization": "Bearer controller-test-secret"}
    assert client.post("/switch", json={"profile": "arbitrary"}, headers=headers).status_code == 400
    module.SWITCH_LOCK.acquire()
    try:
        assert client.post("/switch", json={"profile": "q25-7b"}, headers=headers).status_code == 409
    finally:
        module.SWITCH_LOCK.release()


def test_controller_executes_only_fixed_script_and_profile(monkeypatch):
    module = load_controller(monkeypatch)
    called, urls = [], []

    def unavailable(url, **_kwargs):
        urls.append(url)
        raise OSError

    monkeypatch.setattr(module.urllib.request, "urlopen", unavailable)
    monkeypatch.setattr(module.subprocess, "run", lambda command, **kwargs: called.append((command, kwargs)))
    response = TestClient(module.app).post(
        "/switch", json={"profile": "q3-8b"},
        headers={"Authorization": "Bearer controller-test-secret"},
    )
    assert response.status_code == 200
    assert urls == ["http://host.docker.internal:8000/v1/models"]
    assert called[0][0] == [str(module.SCRIPT), "q3-8b"]
    assert called[0][1]["timeout"] == 1500


def test_vllm_script_requires_explicit_host_cache_for_controller():
    source = pathlib.Path("scripts/vllm-server.sh").read_text(encoding="utf-8")
    assert "CGA_HOST_HF_HOME" in source
    assert "VLLM_BASE_URL" in source
    assert '-v "$CACHE:/root/.cache/huggingface"' in source


def test_controller_allows_minicpm_profile_and_maps_expected_model(monkeypatch):
    module = load_controller(monkeypatch)
    assert module.ALLOWED == {"q25-7b", "q3-8b", "minicpm-v45"}
    assert module.EXPECTED["minicpm-v45"] == "openbmb/MiniCPM-V-4_5"
    called = []
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *_a, **_k: (_ for _ in ()).throw(OSError))
    monkeypatch.setattr(module.subprocess, "run", lambda command, **kwargs: called.append((command, kwargs)))
    response = TestClient(module.app).post(
        "/switch", json={"profile": "minicpm-v45"},
        headers={"Authorization": "Bearer controller-test-secret"},
    )
    assert response.status_code == 200
    assert called[0][0] == [str(module.SCRIPT), "minicpm-v45"]


def test_vllm_script_has_minicpm_profile_with_required_flags():
    source = pathlib.Path("scripts/vllm-server.sh").read_text(encoding="utf-8")
    assert "minicpm-v45)" in source
    assert "openbmb/MiniCPM-V-4_5" in source
    assert "vllm/vllm-openai:v0.11.0" in source
    assert "--trust-remote-code" in source
    assert "--max-model-len 16384" in source
    assert "--gpu-memory-utilization 0.85" in source
    assert "--max-num-seqs 4" in source
