"""採点API(grader/api.py)のテスト。FastAPI TestClientで実データCSVなしに検証。"""
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from grader import api


@pytest.fixture
def client(tmp_path, monkeypatch):
    # data_dir を一時ディレクトリに差し替え、report CSV を1件用意
    report_dir = tmp_path / "report"
    report_dir.mkdir(parents=True)
    pd.DataFrame([
        {"student_id": "111", "name": "テスト太郎", "content_score": 2,
         "category": "auto_2", "tier": "", "judge_score": 2,
         "score_after_late": 2, "flags": "", "evidence": "x"},
        {"student_id": "222", "name": "テスト花子", "content_score": 3,
         "category": "candidate_3", "tier": "strong", "judge_score": 3,
         "score_after_late": 3, "flags": "", "evidence": "y"},
    ]).to_csv(report_dir / "100000000001.csv", index=False)

    monkeypatch.setattr(type(api._cfg), "data_dir", property(lambda self: tmp_path))
    monkeypatch.setattr(api._cfg, "raw", {"api": {"token": None}}, raising=False)
    return TestClient(api.app)


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_get_grades(client):
    r = client.get("/grades/100000000001")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert body["category_counts"] == {"auto_2": 1, "candidate_3": 1}
    strong = [g for g in body["grades"] if g["tier"] == "strong"][0]
    assert strong["name"] == "テスト花子" and strong["content_score"] == 3


def test_get_grades_missing(client):
    r = client.get("/grades/999999")
    assert r.status_code == 404


def test_token_auth(client, monkeypatch):
    monkeypatch.setattr(api._cfg, "raw", {"api": {"token": "secret"}}, raising=False)
    assert client.get("/grades/100000000001").status_code == 401
    ok = client.get("/grades/100000000001", headers={"X-API-Key": "secret"})
    assert ok.status_code == 200


def test_create_job_validates_phase(client):
    r = client.post("/jobs", json={"coursework_id": "100000000001", "phase": "bogus"})
    assert r.status_code == 400
