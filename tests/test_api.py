"""採点API(grader/api.py)のテスト。FastAPI TestClientで実データCSVなしに検証。"""
import asyncio
import io
import inspect
import json
import fitz
import pandas as pd
import pytest
import re
import subprocess
import zipfile
from urllib.parse import parse_qs, urlparse
from fastapi.testclient import TestClient
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from grader import api, settings_presets
from grader.audit import AuditLog
from grader.config import Config
from grader.coursework_actions import CourseworkActionStore
from grader.device_pairing import DevicePairingStore
from grader.draft_input_jobs import DraftInputJobStore
from grader.external_grading import ExternalProposalStore
from grader.jobs import JobService, JobStore
from grader.mcp_tokens import McpTokenStore
from grader.mcp_server import McpPrincipal
from grader.google_auth import GoogleIdentity, PendingOAuth
from grader.mcp_oauth import McpOAuthProvider
from grader.ranking import build_ranking
from grader.session_auth import COOKIE_NAME
from grader.teacher_review import DraftBatchStore, TeacherReviewStore


@pytest.fixture
def client(tmp_path, monkeypatch):
    # data_dir を一時ディレクトリに差し替え、report CSV を1件用意
    report_dir = tmp_path / "report"
    report_dir.mkdir(parents=True)
    frame = pd.DataFrame([
        {"student_id": "111", "name": "テスト太郎", "content_score": 2,
         "category": "auto_2", "tier": "", "judge_score": 2,
         "score_after_late": 2, "flags": "", "evidence": "x"},
        {"student_id": "222", "name": "テスト花子", "content_score": 3,
         "category": "candidate_3", "tier": "strong", "judge_score": 3,
         "score_after_late": 3, "flags": "", "evidence": "y"},
    ])
    frame.to_csv(report_dir / "100000000001.csv", index=False)
    scoped_report = (tmp_path / "courses" / "200000000001" / "courseworks" /
                     "100000000001" / "report.csv")
    scoped_report.parent.mkdir(parents=True)
    frame.to_csv(scoped_report, index=False)

    monkeypatch.setattr(type(api._cfg), "data_dir", property(lambda self: tmp_path))
    monkeypatch.setattr(api._cfg, "raw", {"integration": {"token": "integration-secret"}}, raising=False)
    store = JobStore(tmp_path / "jobs")
    monkeypatch.setattr(api, "_job_store", store)
    monkeypatch.setattr(api, "_audit", AuditLog(Config({"paths": {"data_dir": str(tmp_path)}})))
    monkeypatch.setattr(api, "_teacher_reviews", TeacherReviewStore(api._cfg))
    monkeypatch.setattr(api, "_draft_batches", DraftBatchStore())
    monkeypatch.setattr(api, "_device_pairings", DevicePairingStore(tmp_path / "devices"))
    monkeypatch.setattr(api, "_draft_input_jobs", DraftInputJobStore(
        tmp_path / "draft_input_jobs"))
    monkeypatch.setattr(api, "_coursework_actions", CourseworkActionStore(
        tmp_path / "mcp_coursework_actions"))
    monkeypatch.setattr(api, "_announcement_actions", CourseworkActionStore(
        tmp_path / "mcp_announcement_actions",
        subject="お知らせ", id_field="announcement_id"))
    monkeypatch.setattr(api, "_mcp_tokens", McpTokenStore(tmp_path / "mcp_tokens.json"))
    monkeypatch.setattr(api, "_external_proposals", ExternalProposalStore(
        api._cfg, secret=b"e" * 32))
    monkeypatch.setattr(api, "_job_service", JobService(
        store, runner=lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "ok", ""),
        token_resolver=lambda ref: tmp_path / "oauth_tokens" / f"{ref}.json",
    ))
    api._ranking_cache.clear()  # テスト間でランキングキャッシュを共有しない
    monkeypatch.setattr(api, "_require_teacher_course", lambda *_args: {"id": "200000000001"})
    monkeypatch.setattr(api, "_require_coursework", lambda *_args: {
        "id": "100000000001", "maxPoints": 10,
    })
    return TestClient(api.app)


def login(client, *, sub="google-subject", email="teacher@example.edu"):
    token, identity = api._sessions.create(sub=sub, email=email)
    client.cookies.set(COOKIE_NAME, token, domain="testserver.local", path="/")
    return identity.csrf_token


def stage_external_answers(tmp_path, principal, page_counts, *, extra_rows=None):
    course_id, coursework_id = "200000000001", "100000000001"
    api._mcp_set_policy(
        principal, course_id, coursework_id, "rubric",
        {str(i): f"level {i}" for i in range(4)},
        {"0": 0, "1": 3, "2": 7, "3": 10}, 0, True)
    root = tmp_path / "courses" / course_id / "courseworks" / coursework_id
    pdf_dir = root / "pdf"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for index, count in enumerate(page_counts, start=1):
        student_id = str(1000 + index)
        rows.append({"student_id": student_id, "state": "TURNED_IN", "late": index % 2 == 0})
        document = fitz.open()
        for page_number in range(count):
            page = document.new_page(width=300, height=300)
            page.insert_text((20, 30), f"untrusted answer {index} page {page_number + 1}")
        document.save(pdf_dir / f"{student_id}.pdf")
        document.close()
    rows.extend(extra_rows or [])
    (root / "meta.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return rows


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_mcp_policy_preview_does_not_save_and_confirm_does(client, tmp_path):
    principal = McpPrincipal("a" * 64, "grader")
    levels = {str(i): f"level {i}" for i in range(4)}
    mapping = {"0": 0, "1": 3, "2": 7, "3": 10}
    preview = api._mcp_set_policy(
        principal, "200000000001", "100000000001", "notes",
        levels, mapping, 0, False)
    settings_path = (tmp_path / "courses" / "200000000001" / "courseworks" /
                     "100000000001" / "settings.json")
    assert preview["saved"] is False and not settings_path.exists()
    saved = api._mcp_set_policy(
        principal, "200000000001", "100000000001", "notes",
        levels, mapping, 0, True)
    assert saved["saved"] is True and settings_path.exists()


class _FakeGoogleRequest:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return dict(self.value)


class _FakeCourseWorkService:
    def __init__(self):
        self.create_calls = []
        self.patch_calls = []
        self.submissions = _FakeStudentSubmissionsService()
        self.current = {
            "id": "300000000001", "courseId": "200000000001", "title": "Draft assignment",
            "state": "DRAFT", "maxPoints": 10, "associatedWithDeveloper": True,
        }

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        return _FakeGoogleRequest({
            "id": "300000000001", "courseId": kwargs["courseId"],
            "title": kwargs["body"]["title"], "state": kwargs["body"]["state"],
            "associatedWithDeveloper": True,
        })

    def get(self, **_kwargs):
        return _FakeGoogleRequest(self.current)

    def patch(self, **kwargs):
        self.patch_calls.append(kwargs)
        self.current = {
            **self.current, "state": "PUBLISHED",
            "alternateLink": "https://classroom.google.com/c/mock/a/mock",
        }
        return _FakeGoogleRequest(self.current)

    def studentSubmissions(self):
        return self.submissions


class _FakeStudentSubmissionsService:
    def __init__(self):
        self.patch_calls = []
        self.grade_on_get = False
        self.values = {
            "submission-1": {
                "id": "400000000001", "userId": "1001", "state": "TURNED_IN",
                "associatedWithDeveloper": True,
            },
            "submission-2": {
                "id": "400000000002", "userId": "1002", "state": "TURNED_IN",
                "draftGrade": 5, "associatedWithDeveloper": True,
            },
        }

    def list(self, **_kwargs):
        return _FakeGoogleRequest({"studentSubmissions": list(self.values.values())})

    def get(self, **kwargs):
        value = next(item for item in self.values.values() if item["id"] == kwargs["id"])
        if self.grade_on_get:
            value["draftGrade"] = 6
        return _FakeGoogleRequest(value)

    def patch(self, **kwargs):
        self.patch_calls.append(kwargs)
        value = next(item for item in self.values.values() if item["id"] == kwargs["id"])
        value.update(kwargs["body"])
        return _FakeGoogleRequest(value)


class _FakeAnnouncementService:
    def __init__(self):
        self.create_calls = []
        self.patch_calls = []
        self.current = {
            "id": "500000000001", "courseId": "200000000001", "text": "休講のお知らせ",
            "state": "DRAFT",
        }

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        return _FakeGoogleRequest({
            "id": "500000000001", "courseId": kwargs["courseId"],
            "text": kwargs["body"]["text"], "state": kwargs["body"]["state"],
        })

    def get(self, **_kwargs):
        return _FakeGoogleRequest(self.current)

    def patch(self, **kwargs):
        self.patch_calls.append(kwargs)
        self.current = {
            **self.current, "state": "PUBLISHED",
            "alternateLink": "https://classroom.google.com/c/mock/p/mock",
        }
        return _FakeGoogleRequest(self.current)


class _FakeGoogleService:
    def __init__(self):
        self.coursework = _FakeCourseWorkService()
        self.announcement = _FakeAnnouncementService()

    def courses(self):
        return self

    def courseWork(self):
        return self.coursework

    def announcements(self):
        return self.announcement


def test_mcp_assignment_preview_create_is_draft_and_idempotent(
    client, monkeypatch,
):
    principal = McpPrincipal("a" * 64, "grader")
    google = _FakeGoogleService()
    monkeypatch.setattr(api, "_classroom_for", lambda _identity: google)
    preview = api._mcp_preview_classroom_assignment(
        principal, "200000000001", "Draft assignment", "Description", 10,
        "2026-08-01", "23:59")
    assert preview["preview"] is True and preview["classroom_written"] is False
    assert preview["assignment"] == {
        "title": "Draft assignment", "description": "Description", "maxPoints": 10,
        "workType": "ASSIGNMENT", "state": "DRAFT", "assigneeMode": "ALL_STUDENTS",
        "dueDate": {"year": 2026, "month": 8, "day": 1},
        "dueTime": {"hours": 23, "minutes": 59},
    }
    created = api._mcp_create_classroom_assignment_draft(
        principal, "200000000001", "Draft assignment", "Description", 10,
        "2026-08-01", "23:59", "assignment_test_001")
    assert created["state"] == "DRAFT" and created["classroom_written"] is True
    assert google.coursework.create_calls[0]["body"]["state"] == "DRAFT"
    repeated = api._mcp_create_classroom_assignment_draft(
        principal, "200000000001", "Draft assignment", "Description", 10,
        "2026-08-01", "23:59", "assignment_test_001")
    assert repeated["replayed"] is True and len(google.coursework.create_calls) == 1
    with pytest.raises(api.HTTPException) as mismatch:
        api._mcp_create_classroom_assignment_draft(
            principal, "200000000001", "Different assignment", "Description", 10,
            "2026-08-01", "23:59", "assignment_test_001")
    assert mismatch.value.status_code == 409


def test_mcp_publish_requires_exact_title_and_developer_owned_draft(client, monkeypatch):
    principal = McpPrincipal("a" * 64, "grader")
    google = _FakeGoogleService()
    monkeypatch.setattr(api, "_classroom_for", lambda _identity: google)
    with pytest.raises(api.HTTPException) as mismatch:
        api._mcp_publish_classroom_assignment(
            principal, "200000000001", "300000000001", "Wrong title")
    assert mismatch.value.status_code == 409 and google.coursework.patch_calls == []
    google.coursework.current["associatedWithDeveloper"] = False
    with pytest.raises(api.HTTPException) as foreign:
        api._mcp_publish_classroom_assignment(
            principal, "200000000001", "300000000001", "Draft assignment")
    assert foreign.value.status_code == 409 and google.coursework.patch_calls == []
    google.coursework.current["associatedWithDeveloper"] = True
    published = api._mcp_publish_classroom_assignment(
        principal, "200000000001", "300000000001", "Draft assignment")
    assert published["state"] == "PUBLISHED" and published["classroom_written"] is True
    assert google.coursework.patch_calls[0]["updateMask"] == "state"
    assert google.coursework.patch_calls[0]["body"] == {"state": "PUBLISHED"}
    repeated = api._mcp_publish_classroom_assignment(
        principal, "200000000001", "300000000001", "Draft assignment")
    assert repeated["already_published"] is True
    assert len(google.coursework.patch_calls) == 1


def _stage_direct_grade_proposals(tmp_path, principal, coursework_id="300000000001"):
    course_id = "200000000001"
    api._mcp_set_policy(
        principal, course_id, coursework_id, "rubric",
        {str(i): f"level {i}" for i in range(4)},
        {"0": 0, "1": 3, "2": 7, "3": 10}, 0, True)
    root = tmp_path / "courses" / course_id / "courseworks" / coursework_id
    root.mkdir(parents=True, exist_ok=True)
    rows = [
        {"student_id": "1001", "state": "TURNED_IN", "late": False},
        {"student_id": "1002", "state": "TURNED_IN", "late": False},
    ]
    (root / "meta.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    settings = api.load_settings(api._cfg, course_id, coursework_id)
    for student_id, score in (("1001", 3), ("1002", 2)):
        ref = api._external_proposals.submission_ref(
            principal.owner_ref, course_id, coursework_id, student_id)
        api._external_proposals.save(
            principal.owner_ref, course_id, coursework_id, ref,
            internal_score=score, confidence=0.9, reason="reason", evidence="evidence",
            model="test", settings=settings, late=False)


def test_mcp_direct_draft_grades_are_owner_created_blank_only_and_idempotent(
    client, tmp_path, monkeypatch,
):
    principal = McpPrincipal("a" * 64, "grader")
    google = _FakeGoogleService()
    monkeypatch.setattr(api, "_classroom_for", lambda _identity: google)
    created = api._mcp_create_classroom_assignment_draft(
        principal, "200000000001", "Draft assignment", "Description", 10,
        None, None, "assignment_direct_001")
    assert created["coursework_id"] == "300000000001"
    google.coursework.current["state"] = "PUBLISHED"
    _stage_direct_grade_proposals(tmp_path, principal)

    preview = api._mcp_preview_classroom_draft_grades(
        principal, "200000000001", "300000000001")
    assert preview["writable_count"] == 1
    assert preview["score_distribution"] == {"10": 1}
    assert preview["skipped_counts"]["existing_grade"] == 1
    assert preview["classroom_written"] is False

    with pytest.raises(api.HTTPException) as changed:
        api._mcp_write_classroom_draft_grades(
            principal, "200000000001", "300000000001", "Draft assignment", 2,
            "direct_grade_key_001")
    assert changed.value.status_code == 409
    result = api._mcp_write_classroom_draft_grades(
        principal, "200000000001", "300000000001", "Draft assignment", 1,
        "direct_grade_key_001")
    assert result["status"] == "succeeded" and result["written_count"] == 1
    assert result["assigned_grades_written"] == 0 and result["submissions_returned"] == 0
    call = google.coursework.submissions.patch_calls[0]
    assert call["updateMask"] == "draftGrade" and call["body"] == {"draftGrade": 10.0}
    assert "assignedGrade" not in call["body"]
    repeated = api._mcp_write_classroom_draft_grades(
        principal, "200000000001", "300000000001", "Draft assignment", 1,
        "direct_grade_key_001")
    assert repeated["replayed"] is True
    assert len(google.coursework.submissions.patch_calls) == 1

    with pytest.raises(api.HTTPException) as other_owner:
        api._mcp_preview_classroom_draft_grades(
            McpPrincipal("b" * 64, "grader"), "200000000001", "300000000001")
    assert other_owner.value.status_code == 409


def test_mcp_direct_draft_grades_rechecks_live_grade_before_each_patch(
    client, tmp_path, monkeypatch,
):
    principal = McpPrincipal("a" * 64, "grader")
    google = _FakeGoogleService()
    monkeypatch.setattr(api, "_classroom_for", lambda _identity: google)
    api._mcp_create_classroom_assignment_draft(
        principal, "200000000001", "Draft assignment", "", 10,
        None, None, "assignment_direct_002")
    google.coursework.current["state"] = "PUBLISHED"
    _stage_direct_grade_proposals(tmp_path, principal)
    google.coursework.submissions.grade_on_get = True
    result = api._mcp_write_classroom_draft_grades(
        principal, "200000000001", "300000000001", "Draft assignment", 1,
        "direct_grade_race_001")
    assert result["written_count"] == 0 and result["race_skipped_count"] == 1
    assert google.coursework.submissions.patch_calls == []


def test_assignment_draft_validation_rejects_ambiguous_due_time(client):
    with pytest.raises(api.HTTPException):
        api._assignment_draft_body("Draft", "", 10, None, "23:59")
    with pytest.raises(api.HTTPException):
        api._assignment_draft_body("Draft", "", True, None, None)


def test_classroom_private_network_preflight(client):
    r = client.options(
        "/grades/100000000001",
        headers={
            "Origin": "https://classroom.google.com",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Private-Network": "true",
        },
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-private-network"] == "true"


def test_get_grades(client):
    assert client.get("/grades/100000000001",
                      headers={"X-API-Key": "integration-secret"}).status_code == 410
    r = client.get("/grades/200000000001/100000000001",
                   headers={"X-API-Key": "integration-secret"})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert body["category_counts"] == {"auto_2": 1, "candidate_3": 1}
    strong = [g for g in body["grades"] if g["tier"] == "strong"][0]
    assert strong["name"] == "テスト花子" and strong["content_score"] == 3


def test_get_grades_missing(client):
    r = client.get("/grades/200000000001/999999",
                   headers={"X-API-Key": "integration-secret"})
    assert r.status_code == 404


def test_empty_report_csv_is_safe_conflict(client, tmp_path):
    path = (tmp_path / "courses" / "200000000001" / "courseworks" /
            "100000000001" / "report.csv")
    path.write_text("")
    response = client.get(
        "/grades/200000000001/100000000001",
        headers={"X-API-Key": "integration-secret"},
    )
    assert response.status_code == 409
    assert "再集計" in response.json()["detail"]


def test_integration_token_is_only_for_external_grades(client, monkeypatch):
    monkeypatch.setattr(api._cfg, "raw", {"integration": {"token": "secret"}}, raising=False)
    assert client.get("/grades/200000000001/100000000001").status_code == 401
    ok = client.get("/grades/200000000001/100000000001",
                    headers={"X-API-Key": "secret"})
    assert ok.status_code == 200
    assert client.get("/api/v1/jobs", headers={"X-API-Key": "secret"}).status_code == 401


def test_create_job_validates_phase(client):
    csrf = login(client)
    r = client.post("/jobs", headers={"X-CSRF-Token": csrf},
                    json={"course_id": "200000000001", "coursework_id": "100000000001",
                          "phase": "bogus"})
    assert r.status_code == 400


def test_ui_is_served(client):
    root = client.get("/", follow_redirects=False)
    assert root.status_code == 307 and root.headers["location"] == "/ui/"
    r = client.get("/ui/")
    assert r.status_code == 200 and "Classroom Grading Automation" in r.text
    assert "採点の厳しさ" in r.text and "甘め" in r.text  # 2026-07-20 UI日本語化に追従
    assert "Googleでログイン" in r.text
    assert 'id="course-select"' in r.text and "担当コース" in r.text
    assert 'id="settings-dialog"' in r.text and "Classroom実点数" in r.text
    assert 'id="ranking-refresh"' in r.text and "Google Sheetsへ出力" in r.text
    assert 'id="google-reconnect"' in r.text
    assert "API token" not in r.text and 'id="token"' not in r.text
    script = client.get("/ui/static/app.js").text
    assert 'api("/api/v1/courses")' in script
    assert "course_id:selectedCourseId" in script
    assert "cga-selected-course" in script
    assert "readiness" in script and "cancelJob" in script and "session.role" in script
    assert "/ranking/sheets" in script and "updated_cells" in script
    assert "settings-templates" in script and "template-apply" in r.text
    assert 'id="mcp-section"' in r.text and "bearer_token_env_var" in script
    assert "通常はtokenの発行は不要" in r.text and "Add custom connector" in r.text
    assert "上級者向け: 手動Bearer token" in r.text
    normal_example = script.split('$("mcp-codex-example")', 1)[1].split(
        '$("mcp-token-codex-example")', 1)[0]
    assert "bearer_token_env_var" not in normal_example
    assert "localStorage.setItem(\"CGA_MCP_TOKEN\"" not in script
    assert "Chrome拡張機能セットアップ" in r.text
    assert "chrome://extensions" in r.text and "manifest.json" in r.text
    assert "初回接続コード（端末ペアリング）" in r.text
    assert 'id="device-code-copy"' in r.text
    assert "旧式互換用の短期下書きコード" in script
    assert 'id="draft-transfer"' in r.text and "CGA_EXTENSION_TRANSFER_V1" in script
    assert "Claudeで採点 → 課題一覧からClassroomへ下書き入力" in r.text
    assert "バージョン0.5.4" in r.text
    assert "Classroomへ下書き入力" in script and "transferDraftFor(courseId,c.id" in script
    assert "CGA_EXTENSION_BRIDGE_PROBE_V1" in script
    assert "extensionBridgeReady();return new Promise" in script
    assert "旧方式: サーバーバッチを作成" in r.text
    flow = script.split("async function loadCourseworks()", 1)[1].split(
        "let settingsCoursework", 1)[0]
    assert "/overview" in flow and "Promise.all" not in flow
    assert "/settings" not in flow and "/readiness" not in flow


def test_mcp_token_lifecycle_is_csrf_owner_scoped_and_one_time(client):
    csrf = login(client, sub="owner-a")
    assert client.post("/api/v1/mcp/tokens", json={"expires_days": 30}).status_code == 403
    created = client.post(
        "/api/v1/mcp/tokens", headers={"X-CSRF-Token": csrf}, json={"expires_days": 30},
    )
    assert created.status_code == 200
    raw = created.json()["token"]
    token_id = created.json()["metadata"]["id"]
    listed = client.get("/api/v1/mcp/tokens").json()["tokens"]
    assert [item["id"] for item in listed] == [token_id]
    assert all("token" not in key for key in listed[0])
    assert raw not in (api._cfg.data_dir / "mcp_tokens.json").read_text(encoding="utf-8")

    other_csrf = login(client, sub="owner-b")
    assert client.get("/api/v1/mcp/tokens").json() == {"tokens": []}
    assert client.delete(
        f"/api/v1/mcp/tokens/{token_id}", headers={"X-CSRF-Token": other_csrf},
    ).status_code == 404
    owner_csrf = login(client, sub="owner-a")
    revoked = client.delete(
        f"/api/v1/mcp/tokens/{token_id}", headers={"X-CSRF-Token": owner_csrf},
    )
    assert revoked.status_code == 200 and revoked.json() == {"revoked": True}


def test_mcp_work_packet_packs_three_six_pages_and_batch_advances_atomically(
    client, tmp_path,
):
    principal = McpPrincipal("a" * 64, "grader")
    rows = stage_external_answers(
        tmp_path, principal, [2, 2, 2, 1],
        extra_rows=[
            {"student_id": "1998", "state": "TURNED_IN"},
            {"student_id": "1999", "state": "TURNED_IN", "draft_grade": 0},
        ])
    packet = api._mcp_get_grading_work_packet(
        principal, "200000000001", "100000000001", 3)
    assert packet["status"] == "ready" and packet["returned"] == 3
    assert packet["page_count"] == 6
    assert packet["eligible_remaining"] == 5
    assert [item["page_count"] for item in packet["items"]] == [2, 2, 2]
    assert all(item["page_count"] == item["available_pages"] for item in packet["items"])
    assert all(page["image"]["mime_type"] == "image/jpeg"
               for item in packet["items"] for page in item["pages"])
    serialized = json.dumps(packet)
    assert not any(row["student_id"] in serialized for row in rows)
    proposals = [{"submission_ref": item["submission_ref"], "internal_score": index,
                  "confidence": .8, "reason": f"reason {index}", "evidence": "page",
                  "model": "claude"} for index, item in enumerate(packet["items"])]
    bad = [proposals[0], {**proposals[1], "internal_score": 9}, proposals[2]]
    with pytest.raises(Exception) as rejected:
        api._mcp_submit_proposals_batch(
            principal, "200000000001", "100000000001", bad)
    assert getattr(rejected.value, "status_code", None) == 400
    assert api._external_proposals.load(
        principal.owner_ref, "200000000001", "100000000001") == {}
    saved = api._mcp_submit_proposals_batch(
        principal, "200000000001", "100000000001", proposals)
    assert saved["saved_count"] == 3 and saved["classroom_written"] is False
    assert api._mcp_submit_proposals_batch(
        principal, "200000000001", "100000000001", proposals)["saved_count"] == 3
    following = api._mcp_get_grading_work_packet(
        principal, "200000000001", "100000000001", 3)
    assert following["returned"] == 1 and following["items"][0]["page_count"] == 1
    assert following["eligible_remaining"] == 2
    assert following["skipped_not_ready"] == 1
    with pytest.raises(Exception):
        api._mcp_submit_proposals_batch(
            principal, "200000000001", "100000000001", [proposals[0], proposals[0]])
    with pytest.raises(Exception):
        api._mcp_submit_proposals_batch(
            McpPrincipal("b" * 64, "grader"), "200000000001", "100000000001",
            [proposals[0]])


def test_mcp_work_packet_allows_single_eight_pages_and_bounds_oversize(
    client, tmp_path, monkeypatch,
):
    principal = McpPrincipal("a" * 64, "grader")
    stage_external_answers(tmp_path, principal, [8, 1])
    monkeypatch.setattr(api, "submission_text", lambda _paths, _row: {
        "status": "visual_required", "available_pages": 8,
        "total_pages": 8, "text_chars": 0, "visual_elements": 1,
    })
    packet = api._mcp_get_grading_work_packet(
        principal, "200000000001", "100000000001", 3)
    assert packet["returned"] == 1 and packet["page_count"] == 8
    assert packet["items"][0]["available_pages"] == 8
    assert packet["eligible_remaining"] == 2
    original = api.submission_page

    def oversized(paths, row, page):
        value = original(paths, row, page)
        if value.get("status") == "ready":
            value["image"]["data_base64"] = "A" * (2 * 1024 * 1024 + 1)
        return value

    monkeypatch.setattr(api, "submission_page", oversized)
    bounded = api._mcp_get_grading_work_packet(
        principal, "200000000001", "100000000001", 1)
    assert bounded["status"] == "oversized" and bounded["items"] == []
    assert bounded["fallback_tool"] == "get_submission_for_grading"
    assert "A" * 100 not in json.dumps(bounded)


def test_mcp_text_packet_and_atomic_submit_accept_thirty_answers(
    client, tmp_path, monkeypatch,
):
    principal = McpPrincipal("a" * 64, "grader")
    stage_external_answers(tmp_path, principal, [1] * 30)

    def extracted(_paths, row):
        answer = f"[[page 1]]\nanswer {row['student_id']} " + "detail " * 45
        return {
            "status": "ready", "content_mode": "text", "answer_text": answer,
            "text_chars": len(answer), "page_count": 1, "available_pages": 1,
            "total_pages": 1, "untrusted_content": True, "warning": "warning",
        }

    monkeypatch.setattr(api, "submission_text", extracted)
    packet = api._mcp_get_grading_work_packet(
        principal, "200000000001", "100000000001", 30)
    assert packet["status"] == "ready" and packet["returned"] == 30
    assert packet["image_base64_bytes"] == 0 and packet["text_chars"] > 0
    assert all(item["content_mode"] == "text" and "pages" not in item
               for item in packet["items"])
    proposals = [
        {"submission_ref": item["submission_ref"], "internal_score": index % 4,
         "confidence": .8, "reason": "reason", "evidence": "text",
         "model": "claude"}
        for index, item in enumerate(packet["items"])
    ]
    saved = api._mcp_submit_proposals_batch(
        principal, "200000000001", "100000000001", proposals)
    assert saved["saved_count"] == 30 and saved["classroom_written"] is False


def test_mcp_batch_rejects_fingerprint_change_before_saving(client, tmp_path, monkeypatch):
    principal = McpPrincipal("a" * 64, "grader")
    rows = stage_external_answers(tmp_path, principal, [1])
    first = api._confirmed_external_context(
        principal, "200000000001", "100000000001")
    changed_settings = {**first[3], "notes": "changed"}
    from grader.course_settings import settings_fingerprint
    second = (first[0], first[1], first[2], changed_settings,
              settings_fingerprint(changed_settings), first[5], rows)
    contexts = iter([first, second])
    monkeypatch.setattr(api, "_confirmed_external_context", lambda *_args: next(contexts))
    ref = api._external_proposals.submission_ref(
        principal.owner_ref, "200000000001", "100000000001", rows[0]["student_id"])
    proposal = {"submission_ref": ref, "internal_score": 2, "confidence": .8,
                "reason": "reason", "evidence": "page", "model": "claude"}
    with pytest.raises(api.HTTPException) as rejected:
        api._mcp_submit_proposals_batch(
            principal, "200000000001", "100000000001", [proposal])
    assert getattr(rejected.value, "status_code", None) == 409
    assert api._external_proposals.load(
        principal.owner_ref, "200000000001", "100000000001") == {}


def test_extension_zip_contains_only_fixed_mv3_files(client):
    response = client.get("/ui/classroom-grading-extension.zip")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "attachment" in response.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert set(archive.namelist()) == set(api._EXTENSION_FILES)
        assert "tests/" not in "\n".join(archive.namelist())
        manifest = archive.read("manifest.json").decode("utf-8")
        options = archive.read("options.js").decode("utf-8")
        options_html = archive.read("options.html").decode("utf-8")
        background = archive.read("background.js").decode("utf-8")
        content = archive.read("content.js").decode("utf-8")
        bridge = archive.read("bridge.js").decode("utf-8")
    assert '"manifest_version": 3' in manifest
    assert '"version": "0.5.4"' in manifest
    public_origin = "https://classroom-grader-1.tail80e540.ts.net"
    assert public_origin in manifest and public_origin in options and public_origin in background
    assert "旧方式向け" in options_html and "接続状態を再確認" in options_html
    assert "通常はWeb UIの「拡張機能へ転送」を使う" in options_html
    assert "既存点の上書き、返却、確定、送信は行いません" in options_html
    assert "削除は同じ画面で拡張機能が入力" in options_html
    assert "初回接続コード（端末ペアリング）" in content
    assert "event.source === window" in bridge
    assert "https://classroom-grader-1.tail80e540.ts.net" in bridge
    assert "chrome.runtime.getManifest().version" in bridge
    assert "CGA_EXTENSION_BRIDGE_READY_V1" in bridge
    assert "verifiedDeviceStatus" in options and "verifiedDeviceStatus" in background
    assert "/api/v1/extension/devices/status" in background


def test_v1_status_and_jobs(client):
    login(client)
    status = client.get("/api/v1/status")
    assert status.status_code == 200
    assert "web_user_token_present" in status.json() and "token_present" not in status.json()
    assert status.json()["system_busy"] is False
    assert "sheets_scope_granted" in status.json()["classroom_oauth"]
    assert client.get("/api/v1/jobs").json() == {"jobs": []}


def test_legacy_courseworks_requires_course_selection(client):
    login(client)
    r = client.get("/api/v1/courseworks")
    assert r.status_code == 410 and "コース" in r.json()["detail"]


def test_oauth_start_is_public(client, monkeypatch):
    monkeypatch.setattr(api._google_oauth, "begin", lambda: "https://accounts.example/auth?state=x")
    r = client.post("/api/v1/auth/google/start")
    assert r.status_code == 200 and "state=" in r.json()["authorization_url"]


def test_oauth_callback_rejects_invalid_state_without_api_token(client, monkeypatch):
    from grader.google_auth import OAuthStateError

    def reject(**_kwargs):
        raise OAuthStateError("認証状態が一致しません。")

    monkeypatch.setattr(api._google_oauth, "exchange", reject)
    r = client.get("/oauth2callback?state=bad&code=x", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/ui/?oauth=error")
    assert "%E8%AA%8D%E8%A8%BC" in r.headers["location"]


def test_oauth_callback_sets_http_only_session_cookie(client, monkeypatch):
    monkeypatch.setattr(
        api._google_oauth, "exchange",
        lambda **_kwargs: GoogleIdentity(sub="verified-sub", email="teacher@example.edu"),
    )
    r = client.get("/oauth2callback?state=valid&code=x", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui/?oauth=success"
    cookie = r.headers["set-cookie"]
    assert f"{COOKIE_NAME}=" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert client.get("/api/v1/auth/session").json()["logged_in"] is True


def test_oauth_callback_rejects_account_outside_allowlist(client, monkeypatch):
    monkeypatch.setattr(api._sessions, "allowed_emails", {"allowed@example.edu"})
    monkeypatch.setattr(api._sessions, "allowed_domains", set())

    def exchange(**kwargs):
        identity = GoogleIdentity(sub="outsider-sub", email="outsider@example.net")
        kwargs["authorize_identity"](identity)
        return identity

    monkeypatch.setattr(api._google_oauth, "exchange", exchange)
    r = client.get("/oauth2callback?state=valid&code=x", follow_redirects=False)
    assert r.status_code == 403
    assert COOKIE_NAME not in r.headers.get("set-cookie", "")


def _pending_mcp_request(provider, *, client_name="Claude Desktop", state="client-state"):
    async def setup():
        oauth_client = OAuthClientInformationFull(
            client_id="consent-client", client_name=client_name,
            redirect_uris=[AnyUrl("http://127.0.0.1:8765/callback")],
            token_endpoint_auth_method="none", scope="mcp:tools",
        )
        await provider.register_client(oauth_client)
        return await provider.authorize(oauth_client, AuthorizationParams(
            state=state, scopes=["mcp:tools"], code_challenge="x" * 43,
            redirect_uri=AnyUrl("http://127.0.0.1:8765/callback"),
            redirect_uri_provided_explicitly=True,
            resource="https://grader.example/mcp",
        ))
    location = asyncio.run(setup())
    return parse_qs(urlparse(location).query)["request_id"][0]


def test_mcp_consent_escapes_client_and_requires_csrf_once(client, monkeypatch, tmp_path):
    provider = McpOAuthProvider(
        tmp_path / "oauth-consent.json", public_origin="https://grader.example",
        legacy_tokens=api._mcp_tokens,
    )
    monkeypatch.setattr(api, "_mcp_oauth", provider)
    request_id = _pending_mcp_request(
        provider, client_name="<img src=x onerror=alert(1)>", state="kept")
    csrf = login(client, sub="consent-owner")
    page = client.get(f"/oauth/mcp/authorize?request_id={request_id}")
    assert page.status_code == 200
    assert "<img src=x" not in page.text
    assert "&lt;img src=x onerror=alert(1)&gt;" in page.text
    assert page.headers["cache-control"] == "no-store"
    nonce = re.search(r"name='nonce' value='([^']+)'", page.text).group(1)
    form = {"request_id": request_id, "nonce": nonce, "decision": "allow"}
    denied = client.post("/oauth/mcp/authorize", data={**form, "csrf_token": "wrong"})
    assert denied.status_code == 403
    allowed = client.post(
        "/oauth/mcp/authorize", data={**form, "csrf_token": csrf},
        follow_redirects=False,
    )
    assert allowed.status_code == 303
    query = parse_qs(urlparse(allowed.headers["location"]).query)
    assert query["state"] == ["kept"] and len(query["code"][0]) >= 20
    replay = client.post(
        "/oauth/mcp/authorize", data={**form, "csrf_token": csrf},
        follow_redirects=False,
    )
    assert replay.status_code == 400


def test_google_cancel_returns_access_denied_to_mcp_client(client, monkeypatch, tmp_path):
    provider = McpOAuthProvider(
        tmp_path / "oauth-cancel.json", public_origin="https://grader.example",
        legacy_tokens=api._mcp_tokens,
    )
    monkeypatch.setattr(api, "_mcp_oauth", provider)
    request_id = _pending_mcp_request(provider, state="preserved-state")
    return_to = f"/oauth/mcp/authorize?request_id={request_id}"
    pending_google = PendingOAuth(
        created_at=1, code_verifier=None,
        redirect_uri="https://grader.example/oauth2callback", return_to=return_to,
    )
    monkeypatch.setattr(api._google_oauth, "return_to", lambda _state: return_to)
    monkeypatch.setattr(api._google_oauth, "consume_state", lambda _state: pending_google)
    response = client.get(
        "/oauth2callback?state=google-state&error=access_denied",
        follow_redirects=False,
    )
    assert response.status_code == 303
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query == {"error": ["access_denied"], "state": ["preserved-state"]}
    assert provider.authorization_request(request_id, owner_ref="a" * 64) is None


def test_business_api_requires_login_and_csrf(client):
    assert client.get("/api/v1/jobs").status_code == 401
    csrf = login(client)
    body = {"course_id": "200000000001", "coursework_id": "100000000001",
            "phase": "run"}
    assert client.post("/api/v1/jobs", json=body).status_code == 403
    assert client.post("/api/v1/jobs", json=body,
                       headers={"X-CSRF-Token": csrf}).status_code == 200


def test_logout_requires_csrf_and_clears_cookie(client):
    csrf = login(client)
    assert client.post("/api/v1/auth/logout").status_code == 403
    r = client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200 and r.json() == {"logged_out": True}
    assert client.get("/api/v1/auth/session").json()["logged_in"] is False


def test_v1_rejects_bad_coursework_id(client):
    csrf = login(client)
    r = client.post("/api/v1/jobs", headers={"X-CSRF-Token": csrf},
                    json={"course_id": "200000000001", "coursework_id": "not-an-id",
                          "phase": "report"})
    assert r.status_code == 400


def test_teacher_courses_and_selected_courseworks(client, monkeypatch):
    login(client)
    fake_service = object()
    monkeypatch.setattr(api, "_classroom_for", lambda _identity: fake_service)
    monkeypatch.setattr("grader.fetch.list_teacher_courses", lambda service: [
        {"id": "200000000001", "name": "テストコース", "section": "A",
         "courseState": "ACTIVE"},
    ] if service is fake_service else [])
    coursework_calls = []
    def list_work(*_args, **kwargs):
        coursework_calls.append(kwargs["course_id"])
        return [
            {"id": "100000000001", "title": "課題", "maxPoints": 100},
        ] if kwargs["course_id"] == "200000000001" and kwargs["classroom"] is fake_service else []
    monkeypatch.setattr("grader.fetch.list_courseworks", list_work)
    courses = client.get("/api/v1/courses")
    assert courses.status_code == 200
    assert courses.json()["courses"][0] == {
        "id": "200000000001", "name": "テストコース", "section": "A",
        "course_state": "ACTIVE",
    }
    work = client.get("/api/v1/courses/200000000001/courseworks")
    assert work.status_code == 200 and work.json()["course_id"] == "200000000001"
    settings_calls, readiness_calls = [], []
    monkeypatch.setattr("grader.course_overview._load_settings", lambda _cfg, course, cw: (
        settings_calls.append((course, cw)) or {"confirmed": True}))
    monkeypatch.setattr("grader.course_overview._load_readiness", lambda _cfg, course, cw: (
        readiness_calls.append((course, cw)) or {
            "ready": True, "total": 3, "human_graded": 1, "system_graded": 2,
        }))
    overview = client.get("/api/v1/courses/200000000001/overview")
    assert overview.status_code == 200
    row = overview.json()["courseworks"][0]
    assert row["settings"] == {"confirmed": True} and row["readiness"]["total"] == 3
    assert row["configured"] is True
    assert coursework_calls == ["200000000001", "200000000001"]
    assert settings_calls == [("200000000001", "100000000001")]
    assert readiness_calls == settings_calls


def test_coursework_settings_require_csrf_validate_and_save(client):
    csrf = login(client)
    path = "/api/v1/courses/200000000001/courseworks/100000000001/settings"
    body = {
        "notes": "教師備考", "levels": {str(i): f"{i}点条件" for i in range(4)},
        "score_mapping": {"0": 0, "1": 5, "2": 8, "3": 10},
        "late_penalty": 1, "confirmed": True,
    }
    assert client.put(path, json=body).status_code == 403
    saved = client.put(path, json=body, headers={"X-CSRF-Token": csrf})
    assert saved.status_code == 200
    value = client.get(path).json()["settings"]
    assert value["score_mapping"]["2"] == 8
    body["score_mapping"]["2"] = 4
    assert client.put(path, json=body, headers={"X-CSRF-Token": csrf}).status_code == 400


def test_course_template_crud_apply_scales_and_never_confirms(client, monkeypatch):
    csrf = login(client)

    def coursework(_identity, _course, coursework_id):
        return {"id": coursework_id, "maxPoints": 100 if coursework_id.endswith("1") else 10}

    monkeypatch.setattr(api, "_require_coursework", coursework)
    base = "/api/v1/courses/200000000001/settings-templates"
    settings = {
        "notes": "再利用する注意", "levels": {str(i): f"{i}点条件" for i in range(4)},
        "score_mapping": {"0": 0, "1": 80, "2": 90, "3": 100},
        "late_penalty": 10, "confirmed": True,
    }
    created = client.post(
        base, headers={"X-CSRF-Token": csrf},
        json={"name": "非線形100点", "coursework_id": "100000000001", "settings": settings},
    )
    assert created.status_code == 200
    template = created.json()["template"]
    assert template["score_mapping"] == {"0": 0, "1": 0.8, "2": 0.9, "3": 1.0}

    listed = client.get(base).json()
    assert listed["can_edit"] is True
    assert [item["id"] for item in listed["templates"]] == [template["id"]]
    assert client.get(f"{base}/{template['id']}").json()["template"]["name"] == "非線形100点"

    applied = client.post(
        f"{base}/{template['id']}/apply", headers={"X-CSRF-Token": csrf},
        json={"coursework_id": "100000000002"},
    )
    assert applied.status_code == 200 and applied.json()["saved"] is False
    assert applied.json()["settings"]["score_mapping"] == {
        "0": 0, "1": 8, "2": 9, "3": 10,
    }
    assert applied.json()["settings"]["late_penalty"] == 1
    assert applied.json()["settings"]["confirmed"] is False

    renamed = client.put(
        f"{base}/{template['id']}", headers={"X-CSRF-Token": csrf},
        json={"name": "名前変更後"},
    )
    assert renamed.status_code == 200 and renamed.json()["template"]["name"] == "名前変更後"
    assert client.delete(f"{base}/{template['id']}",
                         headers={"X-CSRF-Token": csrf}).json() == {"deleted": True}
    events = api._audit.read(limit=20)
    assert all("非線形100点" not in str(event) and "再利用する注意" not in str(event)
               for event in events)


def test_template_viewer_can_read_but_not_change(client, monkeypatch):
    monkeypatch.setattr(api._sessions, "roles", {
        "admin": (set(), set()), "grader": (set(), set()),
        "viewer": ({"viewer@example.edu"}, set()),
    })
    monkeypatch.setattr(api._sessions, "_explicit_roles", True)
    csrf = login(client, sub="viewer", email="viewer@example.edu")
    base = "/api/v1/courses/200000000001/settings-templates"
    listed = client.get(base)
    assert listed.status_code == 200 and listed.json()["can_edit"] is False
    response = client.post(base, headers={"X-CSRF-Token": csrf}, json={
        "name": "不可", "coursework_id": "100000000001",
        "settings": {"notes": "", "levels": {str(i): "x" for i in range(4)},
                     "score_mapping": {str(i): i for i in range(4)},
                     "late_penalty": 0, "confirmed": False},
    })
    assert response.status_code == 403


def test_unconfirmed_course_settings_block_job_start(client):
    from grader.course_settings import save_settings

    csrf = login(client)
    save_settings(
        api._cfg, "200000000001", "100000000001",
        {"notes": "", "levels": {str(i): "x" for i in range(4)},
         "score_mapping": {str(i): i for i in range(4)}, "late_penalty": 0,
         "confirmed": False},
        max_points=10, actor_ref="anonymous",
    )
    response = client.post("/api/v1/jobs", headers={"X-CSRF-Token": csrf}, json={
        "course_id": "200000000001", "coursework_id": "100000000001", "phase": "run",
    })
    assert response.status_code == 409 and "未確認" in response.json()["detail"]


def test_viewer_cannot_change_settings_or_create_job(client, monkeypatch):
    monkeypatch.setattr(api._sessions, "roles", {
        "admin": (set(), set()), "grader": (set(), set()),
        "viewer": ({"viewer@example.edu"}, set()),
    })
    monkeypatch.setattr(api._sessions, "_explicit_roles", True)
    csrf = login(client, sub="viewer-sub", email="viewer@example.edu")
    settings_path = "/api/v1/courses/200000000001/courseworks/100000000001/settings"
    body = {"notes": "", "levels": {str(i): "x" for i in range(4)},
            "score_mapping": {str(i): i for i in range(4)}, "confirmed": True}
    assert client.put(settings_path, json=body,
                      headers={"X-CSRF-Token": csrf}).status_code == 403
    assert client.post("/api/v1/jobs", headers={"X-CSRF-Token": csrf}, json={
        "course_id": "200000000001", "coursework_id": "100000000001", "phase": "run",
    }).status_code == 403


def test_audit_endpoint_is_admin_only(client, monkeypatch):
    monkeypatch.setattr(api._sessions, "roles", {
        "admin": ({"admin@example.edu"}, set()), "grader": ({"grader@example.edu"}, set()),
        "viewer": (set(), set()),
    })
    monkeypatch.setattr(api._sessions, "_explicit_roles", True)
    login(client, sub="grader", email="grader@example.edu")
    assert client.get("/api/v1/audit").status_code == 403
    login(client, sub="admin", email="admin@example.edu")
    response = client.get("/api/v1/audit?limit=10")
    assert response.status_code == 200 and isinstance(response.json()["events"], list)


def test_job_response_never_exposes_token_reference(client, monkeypatch):
    monkeypatch.setattr(api._job_service, "_execute", lambda _job_id, **_kwargs: None)
    csrf = login(client)
    body = {"course_id": "200000000001", "coursework_id": "100000000001",
            "phase": "run"}
    created = client.post("/api/v1/jobs", json=body, headers={"X-CSRF-Token": csrf})
    assert created.status_code == 200
    listed = client.get("/api/v1/jobs").json()["jobs"]
    assert listed and "token_ref" not in listed[0]
    active = client.get("/api/v1/status").json()["active_jobs"]
    assert active and "token_ref" not in active[0]


def test_jobs_are_private_between_google_users_and_cli(client):
    from grader.google_auth import token_reference

    def save(job_id, token_ref, status, log):
        api._job_store.save({
            "id": job_id, "status": status, "created_at": job_id,
            "token_ref": token_ref, "course_id": "200000000001",
            "coursework_id": "100000000001", "log": log,
        })

    ref_a = token_reference("teacher-a")
    ref_b = token_reference("teacher-b")
    save("job-a", ref_a, "queued", "private-a")
    save("job-b", ref_b, "succeeded", "private-b")
    save("job-cli", None, "succeeded", "private-cli")

    login(client, sub="teacher-a", email="a@example.edu")
    listed_a = client.get("/api/v1/jobs").json()["jobs"]
    assert [job["id"] for job in listed_a] == ["job-a"]
    assert listed_a[0]["log"] == "private-a" and "token_ref" not in listed_a[0]
    assert client.get("/api/v1/jobs/job-b").status_code == 404
    assert client.get("/jobs/job-b").status_code == 404
    status_a = client.get("/api/v1/status").json()
    assert [job["id"] for job in status_a["active_jobs"]] == ["job-a"]
    assert status_a["system_busy"] is True

    login(client, sub="teacher-b", email="b@example.edu")
    listed_b = client.get("/api/v1/jobs").json()["jobs"]
    assert [job["id"] for job in listed_b] == ["job-b"]
    assert listed_b[0]["log"] == "private-b"
    assert client.get("/api/v1/jobs/job-a").status_code == 404
    status_b = client.get("/api/v1/status").json()
    assert status_b["active_jobs"] == []
    assert status_b["system_busy"] is True


def test_classroom_http_error_is_normalized_without_raw_url(client, monkeypatch):
    login(client)
    monkeypatch.setattr(api, "_classroom_for", lambda _identity: object())

    class Response:
        status = 404

    class GoogleFailure(Exception):
        resp = Response()

    def fail(_service):
        raise GoogleFailure("https://classroom.googleapis.com/private?id=secret")

    monkeypatch.setattr("grader.fetch.list_teacher_courses", fail)
    response = client.get("/api/v1/courses")
    assert response.status_code == 404
    assert "classroom.googleapis.com" not in response.json()["detail"]
    assert "secret" not in response.json()["detail"]


def test_teacher_confirmation_preview_and_one_time_batch(client):
    csrf = login(client)
    base = "/api/v1/courses/200000000001/courseworks/100000000001"
    results = client.get(f"{base}/results").json()["grades"]
    assert all(row["teacher_status"] == "proposal" for row in results)
    unavailable = client.post(f"{base}/extension-transfer", headers={"X-CSRF-Token": csrf})
    assert unavailable.status_code == 409
    assert "安全条件" in unavailable.json()["detail"]
    assert "確認済みAI採点案なし" in unavailable.json()["detail"]
    assert "111" not in unavailable.json()["detail"] and "222" not in unavailable.json()["detail"]
    assert unavailable.headers["cache-control"] == "no-store"

    saved = client.put(
        f"{base}/reviews/111", headers={"X-CSRF-Token": csrf},
        json={"score": 8, "confirmed": True},
    )
    assert saved.status_code == 200 and saved.json()["teacher_status"] == "confirmed"
    assert client.put(f"{base}/reviews/222", headers={"X-CSRF-Token": csrf},
                      json={"score": 11, "confirmed": True}).status_code == 400

    preview = client.get(f"{base}/draft-batches/preview").json()
    assert preview["eligible"] == [{"student_id": "111", "score": 8.0}]
    assert client.post(f"{base}/extension-transfer").status_code == 403
    transfer = client.post(f"{base}/extension-transfer", headers={"X-CSRF-Token": csrf})
    assert transfer.status_code == 200 and transfer.headers["cache-control"] == "no-store"
    assert transfer.json() == {
        "course_id": "200000000001", "coursework_id": "100000000001",
        "max_points": 10.0, "items": [{"student_id": "111",
                                         "student_name": "テスト太郎", "score": 8.0}],
    }
    assert not any(term in transfer.text for term in ("reason", "evidence", "answer"))
    created = client.post(
        "/api/v1/draft-batches", headers={"X-CSRF-Token": csrf},
        json={"course_id": "200000000001", "coursework_id": "100000000001"},
    )
    assert created.status_code == 200 and created.json()["count"] == 1
    code = created.json()["pairing_code"]
    claim_body = {"pairing_code": code, "course_id": "200000000001",
                  "coursework_id": "100000000001"}
    claimed = client.post("/api/v1/extension/pairings/claim", json=claim_body)
    assert claimed.status_code == 200
    capability = claimed.json()
    auth = {"Authorization": f"Bearer {capability['access_token']}"}
    delivered = client.get(f"/api/v1/extension/batches/{capability['batch_id']}", headers=auth)
    assert delivered.json()["items"][0]["student_id"] == "111"
    assert delivered.json()["max_points"] == 10
    assert client.post("/api/v1/extension/pairings/claim", json=claim_body).status_code == 404
    assert client.post(f"/api/v1/extension/batches/{capability['batch_id']}/consume",
                       headers=auth, json={"attempted": 1, "filled": 1,
                                           "skipped": 0, "failed": 0}).json() == {"consumed": True}
    assert client.get(f"/api/v1/extension/batches/{capability['batch_id']}",
                      headers=auth).status_code == 404


def test_persistent_device_claims_owner_ready_batch_and_summary_is_strict(client):
    csrf = login(client, sub="device-owner", email="device@example.edu")
    base = "/api/v1/courses/200000000001/courseworks/100000000001"
    assert client.put(f"{base}/reviews/111", headers={"X-CSRF-Token": csrf},
                      json={"score": 8, "confirmed": True}).status_code == 200
    assert client.post("/api/v1/draft-batches", headers={"X-CSRF-Token": csrf},
                       json={"course_id": "200000000001",
                             "coursework_id": "100000000001"}).status_code == 200
    assert client.post("/api/v1/extension/devices/pairing-codes",
                       headers={"X-CSRF-Token": csrf},
                       json={"label": "PC", "expires_days": 90,
                             "confirm": False}).status_code == 400
    pairing = client.post("/api/v1/extension/devices/pairing-codes",
                          headers={"X-CSRF-Token": csrf},
                          json={"label": "PC", "expires_days": 90,
                                "confirm": True}).json()
    device = client.post("/api/v1/extension/devices/claim",
                         json={"pairing_code": pairing["pairing_code"]}).json()
    device_auth = {"Authorization": f"Bearer {device['device_token']}"}
    assert client.get("/api/v1/extension/devices").json()["devices"][0]["last_used_at"] is None
    device_status = client.get("/api/v1/extension/devices/status", headers=device_auth)
    assert device_status.status_code == 200 and device_status.json() == {"valid": True}
    assert device_status.headers["cache-control"] == "no-store"
    assert client.get("/api/v1/extension/devices").json()["devices"][0]["last_used_at"] is None
    wrong = client.post("/api/v1/extension/device-batches/claim", headers=device_auth,
                        json={"course_id": "200000000001", "coursework_id": "999"})
    assert wrong.status_code == 404
    claimed = client.post("/api/v1/extension/device-batches/claim", headers=device_auth,
                          json={"course_id": "200000000001",
                                "coursework_id": "100000000001"})
    assert claimed.status_code == 200
    capability = claimed.json()
    batch_auth = {"Authorization": f"Bearer {capability['access_token']}"}
    assert client.get(f"/api/v1/extension/batches/{capability['batch_id']}",
                      headers=batch_auth).status_code == 200
    invalid = client.post(f"/api/v1/extension/batches/{capability['batch_id']}/consume",
                          headers=batch_auth,
                          json={"attempted": 1, "filled": 1, "skipped": 1, "failed": 0})
    assert invalid.status_code == 400
    assert client.post(f"/api/v1/extension/batches/{capability['batch_id']}/consume",
                       headers=batch_auth,
                       json={"attempted": True, "filled": 1, "skipped": 0,
                             "failed": 0}).status_code == 422
    assert client.post(f"/api/v1/extension/batches/{capability['batch_id']}/consume",
                       headers=batch_auth,
                       json={"attempted": 1, "filled": 1, "skipped": 0,
                             "failed": 0}).status_code == 200
    assert client.post("/api/v1/extension/device-batches/claim", headers=device_auth,
                       json={"course_id": "200000000001",
                             "coursework_id": "100000000001"}).status_code == 404
    assert client.delete(
        f"/api/v1/extension/devices/{device['device_id']}",
        headers={"X-CSRF-Token": csrf},
    ).json() == {"revoked": True}
    revoked_status = client.get("/api/v1/extension/devices/status", headers=device_auth)
    assert revoked_status.status_code == 404
    assert revoked_status.headers["cache-control"] == "no-store"


def test_mcp_draft_input_job_is_owner_context_and_fingerprint_scoped(client):
    sub = "automatic-device-owner"
    csrf = login(client, sub=sub, email="automatic@example.edu")
    owner_ref = api.token_reference(sub)
    principal = McpPrincipal(owner_ref, "grader")
    course_id, coursework_id = "200000000001", "100000000001"
    api._mcp_set_policy(
        principal, course_id, coursework_id, "rubric",
        {str(i): f"level {i}" for i in range(4)},
        {"0": 0, "1": 3, "2": 7, "3": 10}, 0, True)
    base = f"/api/v1/courses/{course_id}/courseworks/{coursework_id}"
    assert client.put(
        f"{base}/reviews/111", headers={"X-CSRF-Token": csrf},
        json={"score": 8, "confirmed": True}).status_code == 200

    created = api._mcp_create_draft_input_job(principal, course_id, coursework_id)
    repeated = api._mcp_create_draft_input_job(principal, course_id, coursework_id)
    assert created["created"] is True and repeated["created"] is False
    assert repeated["id"] == created["id"] and created["status"] == "queued"
    assert "items" not in created and "student_id" not in json.dumps(created)

    pairing = client.post(
        "/api/v1/extension/devices/pairing-codes",
        headers={"X-CSRF-Token": csrf},
        json={"label": "Automatic PC", "expires_days": 90, "confirm": True},
    ).json()
    device = client.post(
        "/api/v1/extension/devices/claim",
        json={"pairing_code": pairing["pairing_code"]}).json()
    auth = {"Authorization": f"Bearer {device['device_token']}"}
    endpoint = "/api/v1/extension/draft-input-jobs/pending"
    assert client.get(endpoint, headers=auth, params={
        "course_id": course_id, "coursework_id": "999"}).status_code == 404
    pending = client.get(endpoint, headers=auth, params={
        "course_id": course_id, "coursework_id": coursework_id})
    assert pending.status_code == 200 and pending.headers["cache-control"] == "no-store"
    body = pending.json()
    assert set(body) == {"job_id", "course_id", "coursework_id", "max_points", "items", "expires_at"}
    assert body["job_id"] == created["id"]
    assert body["items"] == [{"student_id": "111", "student_name": "テスト太郎",
                               "score": 8.0}]

    reported = client.post(
        f"/api/v1/extension/draft-input-jobs/{created['id']}/progress",
        headers=auth, json={"results": [{"student_id": "111", "outcome": "existing"}]})
    assert reported.status_code == 200 and reported.headers["cache-control"] == "no-store"
    progress = reported.json()
    assert progress["status"] == "succeeded"
    assert progress["counts"] == {"pending": 0, "filled": 0, "existing": 1, "failed": 0}
    assert "student_id" not in json.dumps(progress) and "score" not in json.dumps(progress)


def test_teacher_confirmations_are_private_between_users(client):
    csrf = login(client, sub="owner-a", email="a@example.edu")
    base = "/api/v1/courses/200000000001/courseworks/100000000001"
    assert client.put(f"{base}/reviews/111", headers={"X-CSRF-Token": csrf},
                      json={"score": 8, "confirmed": True}).status_code == 200
    login(client, sub="owner-b", email="b@example.edu")
    rows = client.get(f"{base}/results").json()["grades"]
    assert next(row for row in rows if row["student_id"] == "111")["teacher_status"] == "proposal"
    assert client.get(f"{base}/draft-batches/preview").json()["eligible_count"] == 0


def test_v1_report_download(client):
    login(client)
    assert client.get("/api/v1/courseworks/100000000001/report.csv").status_code == 410
    r = client.get(
        "/api/v1/courses/200000000001/courseworks/100000000001/report.csv"
    )
    assert r.status_code == 200 and "text/csv" in r.headers["content-type"]
    assert client.get(
        "/api/v1/courses/200000000001/courseworks/999999/report.csv"
    ).status_code == 404


def test_legacy_results_cannot_read_cross_course_report(client):
    login(client)
    assert client.get("/api/v1/courseworks/100000000001/results").status_code == 410


def test_course_ranking_uses_only_confirmed_or_human_scores(client, tmp_path, monkeypatch):
    login(client)
    monkeypatch.setattr(api, "_classroom_for", lambda _identity: object())
    # このテストはローカル集計だけを検証する。Classroom確定点の取り込みは別テスト。
    monkeypatch.setattr(api, "_live_confirmed_grades", lambda *_a, **_k: {})
    monkeypatch.setattr("grader.fetch.list_courseworks", lambda *_args, **_kwargs: [
        {"id": "100000000001", "title": "課題1"},
        {"id": "100000000002", "title": "課題2"},
    ])
    second = (tmp_path / "courses" / "200000000001" / "courseworks" /
              "100000000002" / "report.csv")
    second.parent.mkdir(parents=True)
    pd.DataFrame([
        {"student_id": "111", "name": "テスト太郎", "source": "system", "mapped_score": 9},
        {"student_id": "222", "name": "テスト花子", "source": "human", "mapped_score": 7},
        {"student_id": "333", "name": "未確認", "source": "system", "mapped_score": 10},
    ]).to_csv(second, index=False)
    api._teacher_reviews.save(
        "google-subject", "200000000001", "100000000001", "111",
        score=8, confirmed=True,
    )

    response = client.get("/api/v1/courses/200000000001/ranking")
    assert response.status_code == 200
    body = response.json()
    assert body["rank_style"] == "competition"
    assert [item["coursework_id"] for item in body["courseworks"]] == [
        "100000000001", "100000000002",
    ]
    # 順位は満点で正規化した平均点で決まる。どちらも自分の課題で唯一の確定点
    # (=その課題の最高点)なので平均1.0で同率1位になり、確定点合計は参考値。
    assert [(row["student_id"], row["rank"], row["total"], row["confirmed_count"])
            for row in body["rows"]] == [("111", 1, 8.0, 1), ("222", 1, 7.0, 1)]
    assert all(row["average_rate"] == 1.0 for row in body["rows"])
    assert body["rows"][0]["scores"] == {
        "100000000001": 8.0, "100000000002": None,
    }


class _SheetsCall:
    def __init__(self, result=None):
        self.result = result or {}

    def execute(self):
        return self.result


class _SheetsValues:
    def __init__(self):
        self.calls = []

    def clear(self, **kwargs):
        self.calls.append(("clear", kwargs))
        return _SheetsCall()

    def update(self, **kwargs):
        self.calls.append(("update", kwargs))
        return _SheetsCall({"updatedCells": 10})


class _SheetsService:
    def __init__(self):
        self.values_service = _SheetsValues()

    def spreadsheets(self):
        return self

    def values(self):
        return self.values_service


def test_ranking_sheets_uses_current_user_credentials_and_explicit_confirmation_api(
    client, monkeypatch,
):
    csrf = login(client)
    table = build_ranking([{"coursework_id": "100000000001", "title": "課題", "rows": [
        {"student_id": "111", "name": "先生確認済み", "source": "human", "mapped_score": 8},
    ]}])
    monkeypatch.setattr(api, "_course_ranking_table", lambda identity, course: table)
    credential = object()
    seen_subs = []
    monkeypatch.setattr(api._google_oauth, "user_credentials",
                        lambda sub: seen_subs.append(sub) or credential)
    service = _SheetsService()
    import googleapiclient.discovery
    monkeypatch.setattr(
        googleapiclient.discovery, "build",
        lambda name, version, **kwargs: (
            service if (name, version, kwargs.get("credentials")) ==
            ("sheets", "v4", credential) else pytest.fail("unexpected Sheets build")
        ),
    )
    response = client.post(
        "/api/v1/courses/200000000001/ranking/sheets",
        headers={"X-CSRF-Token": csrf},
        json={"spreadsheet": "a_valid_spreadsheet_id_12345",
              "sheet_name": "ランキング", "range": "A1:J20"},
    )
    assert response.status_code == 200
    assert response.json()["updated_cells"] == 10
    assert seen_subs == ["google-subject"]
    assert [name for name, _ in service.values_service.calls] == ["clear", "update"]
    assert service.values_service.calls[1][1]["valueInputOption"] == "RAW"


def test_ranking_sheets_rejects_destination_before_external_call(client, monkeypatch):
    csrf = login(client)
    monkeypatch.setattr(api._google_oauth, "user_credentials",
                        lambda _sub: pytest.fail("credentials must not be loaded"))
    response = client.post(
        "/api/v1/courses/200000000001/ranking/sheets",
        headers={"X-CSRF-Token": csrf},
        json={"spreadsheet": "https://evil.example/sheet", "sheet_name": "ランキング",
              "range": "A1:Z1000"},
    )
    assert response.status_code == 400
    assert "evil.example" not in response.text


def test_ranking_sheets_maps_disabled_api_without_leaking_google_response(client, monkeypatch):
    csrf = login(client)
    table = build_ranking([{"coursework_id": "1", "rows": [
        {"student_id": "1", "source": "human", "mapped_score": 1},
    ]}])
    monkeypatch.setattr(api, "_course_ranking_table", lambda *_args: table)
    monkeypatch.setattr(api._google_oauth, "user_credentials", lambda _sub: object())

    class Response:
        status = 403

    class Disabled(Exception):
        resp = Response()
        content = b'{"error":{"errors":[{"reason":"accessNotConfigured"}]}}'

    monkeypatch.setattr(api, "write_values", lambda *_args, **_kwargs: (_ for _ in ()).throw(Disabled("private")))
    response = client.post(
        "/api/v1/courses/200000000001/ranking/sheets",
        headers={"X-CSRF-Token": csrf},
        json={"spreadsheet": "a_valid_spreadsheet_id_12345",
              "sheet_name": "ランキング", "range": "A1:F20"},
    )
    assert response.status_code == 409
    assert "Sheets API" in response.json()["detail"]
    assert "private" not in response.text


def test_bulk_review_confirm_is_admin_only_and_disabled_by_default(monkeypatch):
    """一括確認は標準運用では無効。個別の人間確認を伴わないため既定で塞ぐ。"""
    from grader import api as api_module

    monkeypatch.delenv("CGA_ALLOW_BULK_REVIEW_CONFIRM", raising=False)
    assert api_module.bulk_review_confirm_enabled() is False
    monkeypatch.setenv("CGA_ALLOW_BULK_REVIEW_CONFIRM", "0")
    assert api_module.bulk_review_confirm_enabled() is False
    monkeypatch.setenv("CGA_ALLOW_BULK_REVIEW_CONFIRM", "1")
    assert api_module.bulk_review_confirm_enabled() is True

    # 管理者権限を要求している(require_grader=採点者一般では通らない)
    route = next(r for r in api_module.app.routes
                 if getattr(r, "path", "").endswith("/reviews-confirm-all"))
    dependency_names = [
        getattr(d.call, "__name__", "") for d in route.dependant.dependencies]
    assert "require_admin" in dependency_names
    assert "require_grader" not in dependency_names

    # 明示確認・期待件数・fingerprint・監査理由をリクエストで要求する
    fields = set(api_module.BulkConfirmRequest.model_fields)
    assert fields == {"confirm", "expected_count", "settings_fingerprint", "reason"}
    assert api_module.BulkConfirmRequest().confirm is False


def test_public_gateway_does_not_allow_bulk_review_confirm():
    """公開gateway経由では一括確認を通さない。"""
    import pathlib

    # gatewayは別デプロイ構成のため、このイメージに含まれない場合はスキップする。
    path = pathlib.Path("gateway/app.py")
    if not path.exists():
        pytest.skip("gateway/app.py is not part of this image")
    source = path.read_text(encoding="utf-8")
    assert "reviews-confirm-all" not in source
    # 個別レビューは引き続き許可されている
    assert "reviews/{_SEGMENT}$" in source


def test_web_ui_excludes_visual_info_badges_from_risk_scoring():
    """has_visual_material と evidence_not_verified_visual をリスクスコアへ入れない。

    画像答案では常に立つため、含めると全画像答案が「疑問答案」になる。
    情報バッジ(表示のみ)としては残す。
    """
    import pathlib

    source = pathlib.Path("grader/web/app.js").read_text(encoding="utf-8")
    weights = source[source.index("const RISK_WEIGHTS={"):source.index("// スコアに寄与しない")]
    assert "has_visual_material" not in weights
    assert "evidence_not_verified_visual" not in weights
    # 実質的な視覚警告は重み付けされている
    assert "visual_review_required" in weights and "ocr_quality_low" in weights
    # 情報バッジ集合として明示的に除外している
    assert 'INFO_BADGES=new Set(["has_visual_material","evidence_not_verified_visual"])' in source
    assert "const riskReasons=r=>[...allSignals(r)].filter(name=>!INFO_BADGES.has(name))" in source
    # ラベルは残す(表示用)
    assert "has_visual_material:" in source and "evidence_not_verified_visual:" in source


def test_visual_max_score_signal_is_display_only_and_visual_scoped():
    """画像答案の最高点提案シグナルは表示・優先度専用で、適格性へ影響させない。"""
    import pathlib

    source = pathlib.Path("grader/web/app.js").read_text(encoding="utf-8")
    # 条件: Qwen2.5 / 最高点 / 未確認 / has_visual_material
    # 判定は必ずprimary_resultから行う(統合後のcontent_scoreは使わない)
    assert "/Qwen2\\.5/.test(String(primary.model||\"\"))" in source
    assert "Number(primary.internal_score)===TOP_INTERNAL_SCORE" in source
    assert "Number(r.content_score)===TOP_INTERNAL_SCORE" not in source
    assert 'r.teacher_status!=="confirmed"' in source
    assert '.includes("has_visual_material")' in source
    # 重み: low_confidence(12)より高く、独立検証失敗(25以上)より低い
    assert "q25_visual_max_score:20," in source
    # automatic_eligible / proposal_state / ルーティングへは影響させない
    # (コメント行は除外し、実コードだけを検査する)
    scoring = source[source.index("const isQ25VisualMaxScore"):source.index("function riskScore")]
    code_only = "\n".join(line for line in scoring.splitlines()
                          if not line.strip().startswith("//"))
    assert "automatic_eligible" not in code_only
    assert "proposal_state" not in code_only
    # has_visual_material単独は情報バッジのまま
    assert 'INFO_BADGES=new Set(["has_visual_material","evidence_not_verified_visual"])' in source


def test_visual_max_score_saturation_disables_row_priority_only():
    """フラグが飽和した課題では行の優先度重みを0にする(表示は残す)。

    自動減点・自動ルーティング・automatic_eligible・proposal_stateには
    影響させない。
    """
    import pathlib

    source = pathlib.Path("grader/web/app.js").read_text(encoding="utf-8")
    assert "const FLAG_SATURATION_RATE=0.90;" in source
    # 小規模課題を過剰に無効化しないため、件数条件は必ずフラグ率と組み合わせる
    assert "const MIN_VISUAL_FOR_SATURATION=10;" in source
    assert "const SECONDARY_SATURATION_RATE=0.80;" in source
    assert "const SECONDARY_MIN_UNFLAGGED=3;" in source
    assert "const MIN_UNFLAGGED=5;" not in source
    # 飽和判定は未確認の画像答案だけを母数にする
    saturation = source[source.index("function evaluateVisualMaxSaturation"):
                        source.index("const allSignals=r=>{")]
    assert 'r.teacher_status!=="confirmed"' in saturation
    assert '.includes("has_visual_material")' in saturation
    assert "pending.length>=MIN_VISUAL_FOR_SATURATION&&rate>=FLAG_SATURATION_RATE" in saturation
    assert "rate>=SECONDARY_SATURATION_RATE&&unflagged<SECONDARY_MIN_UNFLAGGED" in saturation
    # 飽和時はriskScoreへ加算しない
    assert 'if(name==="q25_visual_max_score"&&visualMaxSaturated)return total;' in source
    # 適格性・状態・ルーティングへは触れない(コメント行は除外)
    saturation_code = "\n".join(line for line in saturation.splitlines()
                                if not line.strip().startswith("//"))
    assert "automatic_eligible" not in saturation_code
    assert "proposal_state" not in saturation_code


def test_review_timer_starts_on_row_activation_not_on_list_render():
    """確認所要時間は一覧描画時に全行へ一括開始しない。

    行をアクティブ化(点数欄フォーカス/行クリック)した時点で開始し、
    同時に進行するタイマーは1件だけとする。
    """
    import pathlib

    source = pathlib.Path("grader/web/app.js").read_text(encoding="utf-8")
    # renderRows内で全行へ開始時刻を設定していないこと
    render = source[source.index("function renderRows(){"):]
    assert "reviewOpenedAt.set(" not in render
    # アクティブ化で開始し、直前の行は破棄する(同時進行は1件)
    assert "function activateReview(studentId){" in source
    assert "if(activeReviewStudentId===studentId)return;" in source
    assert "if(activeReviewStudentId!==null)reviewOpenedAt.delete(activeReviewStudentId);" in source
    assert 'input.addEventListener("focus",()=>activateReview(r.student_id));' in source
    assert 'tr.addEventListener("click",()=>activateReview(r.student_id));' in source


def _login_as(client, monkeypatch, role, *, email="admin@example.edu"):
    """指定ロールでログインする(session_authのロール解決を差し替える)。"""
    monkeypatch.setattr(api._sessions, "resolve_role", lambda _email: role)
    monkeypatch.setattr(api._sessions, "authorize", lambda _email: None)
    return login(client, sub=f"subject-{role}", email=email)


def test_bulk_confirm_requires_admin_role(client, monkeypatch):
    """一般graderは403。管理者だけが到達できる。"""
    csrf = _login_as(client, monkeypatch, "grader", email="grader@example.edu")
    monkeypatch.setenv("CGA_ALLOW_BULK_REVIEW_CONFIRM", "1")
    response = client.post(
        "/api/v1/courses/200000000001/courseworks/100000000001/reviews-confirm-all",
        headers={"X-CSRF-Token": csrf},
        json={"confirm": True, "expected_count": 1,
              "settings_fingerprint": "x", "reason": "監査用の理由テキスト"})
    assert response.status_code == 403


def test_bulk_confirm_is_disabled_by_default_even_for_admin(client, monkeypatch):
    """既定(環境変数なし)では管理者でも409で拒否する。"""
    csrf = _login_as(client, monkeypatch, "admin")
    monkeypatch.delenv("CGA_ALLOW_BULK_REVIEW_CONFIRM", raising=False)
    response = client.post(
        "/api/v1/courses/200000000001/courseworks/100000000001/reviews-confirm-all",
        headers={"X-CSRF-Token": csrf},
        json={"confirm": True, "expected_count": 1,
              "settings_fingerprint": "x", "reason": "監査用の理由テキスト"})
    assert response.status_code == 409
    assert "無効" in response.json()["detail"]


def test_bulk_confirm_validates_confirm_reason_fingerprint_and_count(client, monkeypatch, tmp_path):
    """confirm・reason・fingerprint・expected_countの全てを検証し、監査へ残す。"""
    csrf = _login_as(client, monkeypatch, "admin")
    monkeypatch.setenv("CGA_ALLOW_BULK_REVIEW_CONFIRM", "1")
    url = "/api/v1/courses/200000000001/courseworks/100000000001/reviews-confirm-all"
    headers = {"X-CSRF-Token": csrf}
    base = {"confirm": True, "expected_count": 2,
            "settings_fingerprint": "x", "reason": "監査用の理由テキスト"}

    # confirm=false は400
    assert client.post(url, headers=headers, json={**base, "confirm": False}).status_code == 400
    # reasonが短すぎると400
    assert client.post(url, headers=headers, json={**base, "reason": "短い"}).status_code == 400
    # fingerprint不一致は409(この課題には確認済み設定がないため必ず不一致)
    mismatch = client.post(url, headers=headers, json=base)
    assert mismatch.status_code == 409
    assert "fingerprint" in mismatch.json()["detail"]

    # 確認済み設定を用意してfingerprintを一致させ、件数不一致を検証する
    from grader.course_settings import save_settings, settings_fingerprint

    settings = {
        "notes": "rubric", "levels": {str(i): f"level {i}" for i in range(4)},
        "score_mapping": {"0": 0, "1": 3, "2": 7, "3": 10},
        "late_penalty": 0, "confirmed": True, "max_points": 10,
    }
    saved = save_settings(api._cfg, "200000000001", "100000000001", settings, max_points=10.0,
                          actor_ref="a" * 64)
    fingerprint = settings_fingerprint(saved)
    wrong_count = client.post(url, headers=headers, json={
        **base, "settings_fingerprint": fingerprint, "expected_count": 999})
    assert wrong_count.status_code == 409
    assert "expected_count" in wrong_count.json()["detail"]

    # 監査ログへ拒否理由が残る(答案本文・学生名は含まない)
    audit_lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    outcomes = [json.loads(line)["outcome"] for line in audit_lines
                if json.loads(line)["action"] == "review.confirm_all"]
    assert "rejected:fingerprint" in outcomes and "rejected:count" in outcomes


def test_gateway_allowlist_blocks_bulk_confirm_and_allows_single_review():
    """公開gateway経由では一括確認を通さず、個別レビューは通す。

    gatewayソースがimageに含まれない環境ではスキップする。
    """
    try:
        import gateway.app as gateway_app
    except ModuleNotFoundError:
        pytest.skip("gateway module is not part of this image")
    source = inspect.getsource(gateway_app)
    assert "reviews-confirm-all" not in source
    assert "reviews/{_SEGMENT}$" in source


def test_web_ui_separates_mcp_and_extension_into_tabs():
    """MCP連携とChrome拡張機能を採点タブから分離する。"""
    import pathlib

    html = pathlib.Path("grader/web/index.html").read_text(encoding="utf-8")
    js = pathlib.Path("grader/web/app.js").read_text(encoding="utf-8")
    for tab in ("tab-grading", "tab-mcp", "tab-extension"):
        assert f'id="{tab}"' in html
    for panel in ("panel-grading", "panel-mcp", "panel-extension"):
        assert f'id="{panel}"' in html
    # 既定は採点タブ。MCP/拡張は初期非表示。
    assert 'id="panel-mcp" role="tabpanel" aria-labelledby="tab-mcp" hidden' in html
    assert 'id="panel-extension" role="tabpanel" aria-labelledby="tab-extension" hidden' in html
    # 採点パネルは2ブロックに分かれるため両方を切り替える
    assert 'grading:["panel-grading","panel-grading-2","panel-grading-3"]' in js


def test_web_ui_offers_top_level_coursework_fetch():
    """答案取得を各課題カードではなく一番上のクイック操作から行える。"""
    import pathlib

    html = pathlib.Path("grader/web/index.html").read_text(encoding="utf-8")
    js = pathlib.Path("grader/web/app.js").read_text(encoding="utf-8")
    assert 'id="quick-coursework"' in html and 'id="quick-prepare"' in html
    assert 'id="quick-full"' in html and 'id="courses-refresh"' in html
    assert "function runQuickJob(phase)" in js
    assert "function renderQuickCourseworkOptions()" in js
    # 課題カードからは答案準備ボタンを外している(上部へ集約)
    cards = js[js.index("const acts=node(\"div\",undefined,\"course-actions\")"):]
    assert 'startJob(c,"prepare")' not in cards


def test_web_ui_has_jobs_tab_and_bulk_preset_apply():
    """ジョブは別タブ。採点基準は最上部でまとめて適用できる。"""
    import pathlib

    html = pathlib.Path("grader/web/index.html").read_text(encoding="utf-8")
    js = pathlib.Path("grader/web/app.js").read_text(encoding="utf-8")
    assert 'id="tab-jobs"' in html and 'id="panel-jobs"' in html
    assert 'id="preset-select"' in html and 'id="preset-apply"' in html
    assert "async function applyPreset()" in js
    assert 'jobs:["panel-jobs"]' in js
    # 採点パネルは3ブロックに分かれる
    assert 'grading:["panel-grading","panel-grading-2","panel-grading-3"]' in js


def test_preset_apply_endpoint_does_not_auto_confirm(client, monkeypatch):
    """一括適用しただけでは確認済みにせず、確認済み課題は上書きしない。"""
    from grader.course_settings import load_settings, save_settings

    csrf = login(client)
    url = "/api/v1/courses/200000000001/settings-presets/apply"
    headers = {"X-CSRF-Token": csrf}
    response = client.post(url, headers=headers, json={
        "preset_id": "kansou", "coursework_ids": ["100000000001"]})
    assert response.status_code == 200
    body = response.json()
    assert len(body["applied"]) == 1 and body["applied"][0]["preset_id"] == "kansou"
    saved = load_settings(api._cfg, "200000000001", "100000000001")
    assert saved["confirmed"] is False
    # maxPoints=10 なので確認済み基準と同じ配分になる
    assert saved["score_mapping"] == {"0": 0.0, "1": 8.0, "2": 9.0, "3": 10.0}

    # 確認済みの課題は上書きしない
    save_settings(api._cfg, "200000000001", "100000000001",
                  {**saved, "confirmed": True}, max_points=10.0, actor_ref="a" * 64)
    again = client.post(url, headers=headers, json={
        "preset_id": "experiment", "coursework_ids": ["100000000001"]}).json()
    assert again["applied"] == []
    assert again["skipped"][0]["reason"] == "already_confirmed"
    assert load_settings(api._cfg, "200000000001", "100000000001")["score_mapping"] == {
        "0": 0.0, "1": 8.0, "2": 9.0, "3": 10.0}

    # 対象未指定は400
    assert client.post(url, headers=headers, json={"preset_id": "experiment"}).status_code == 400


def test_web_ui_disables_actions_that_cannot_run():
    """実行できない操作はボタンを無効化し、理由をtitleで示す。"""
    import pathlib

    js = pathlib.Path("grader/web/app.js").read_text(encoding="utf-8")
    css = pathlib.Path("grader/web/style.css").read_text(encoding="utf-8")
    # AI採点前はClassroom入力・結果確認を押せない
    assert 'transfer.disabled=session.role==="viewer"||!graded;' in js
    assert "const view=node(\"button\",\"結果を確認\");view.disabled=!graded;" in js
    # 採点基準が未確認なら採点を開始できない(理由も表示)
    assert 'if(!configured)full.title="採点基準を確認済みにするまで採点を開始できません。";' in js
    # クイック操作も選択状態に応じて同期する
    assert "function syncQuickButtons()" in js
    # 無効なボタンは視覚的にも薄くする
    assert "button:disabled" in css and "opacity: 0.45" in css


def test_ranking_table_is_hidden_when_no_confirmed_scores():
    """確定済みが無いときに列見出しだけの空表を描画しない。"""
    import pathlib

    js = pathlib.Path("grader/web/app.js").read_text(encoding="utf-8")
    html = pathlib.Path("grader/web/index.html").read_text(encoding="utf-8")
    assert 'id="ranking-wrap" class="table-wrap" hidden' in html
    assert 'if(!d.rows.length){' in js
    assert '$("ranking-wrap").hidden=true;' in js
    assert '$("ranking-wrap").hidden=false;' in js


def test_settings_dialog_can_load_the_three_presets(client, monkeypatch):
    """個別課題の設定ダイアログからも3種の既定テンプレを選べる。"""
    import pathlib

    csrf = login(client)
    listing = client.get("/api/v1/settings-presets").json()
    assert [p["id"] for p in listing["presets"]] == ["kansou", "research", "experiment"]

    # 満点に合わせて展開し、確認済みにはしない
    detail = client.get("/api/v1/settings-presets/kansou?max_points=10").json()
    assert detail["settings"]["score_mapping"] == {"0": 0.0, "1": 8.0, "2": 9.0, "3": 10.0}
    assert detail["settings"]["confirmed"] is False
    assert client.get("/api/v1/settings-presets/bogus").status_code == 400

    html = pathlib.Path("grader/web/index.html").read_text(encoding="utf-8")
    js = pathlib.Path("grader/web/app.js").read_text(encoding="utf-8")
    assert 'id="dialog-preset-select"' in html and 'id="dialog-preset-load"' in html
    assert "async function loadDialogPreset()" in js
    # 読み込んだだけでは確認済みにしない
    assert "fillSettingsForm({...d.settings,confirmed:false});" in js
    assert csrf


def test_ranking_reads_confirmed_grades_from_classroom(client, monkeypatch, tmp_path):
    """ランキング更新時にClassroomの確定点を取得してから集計する。"""
    csrf = login(client)

    calls = []

    def fake_live(_identity, course_id, coursework_id):
        calls.append(coursework_id)
        # 集計CSVに無い学生(999)も確定点があれば集計対象にする
        return {"111": 9.0, "999": 7.0}

    monkeypatch.setattr(api, "_classroom_for", lambda _identity: object())
    monkeypatch.setattr(api, "_live_confirmed_grades", fake_live)
    monkeypatch.setattr("grader.fetch.list_courseworks",
                        lambda *a, **k: [{"id": "100000000001", "title": "テスト課題"}])
    response = client.get("/api/v1/courses/200000000001/ranking")
    assert response.status_code == 200, response.json()
    assert calls == ["100000000001"]
    body = response.json()
    ids = {row["student_id"] for row in body["rows"]}
    assert "111" in ids and "999" in ids
    scores = {row["student_id"]: row["total"] for row in body["rows"]}
    # Classroomのassigned_gradeが正本として反映される
    assert scores["111"] == 9.0 and scores["999"] == 7.0
    assert body["cached"] is False
    assert csrf


def test_ranking_ui_separates_summary_from_per_coursework_scores():
    """集計表と課題ごとの内訳を分け、見出しの二重描画を防ぐ。"""
    import pathlib

    html = pathlib.Path("grader/web/index.html").read_text(encoding="utf-8")
    js = pathlib.Path("grader/web/app.js").read_text(encoding="utf-8")
    # ランキング表示は4つのサブタブで切り替える
    assert 'id="ranking-detail"' in html and 'id="ranking-detail-head"' in html
    for name in ("ranking", "top", "zero", "breakdown"):
        assert f'id="subtab-{name}"' in html and f'id="subpanel-{name}"' in html
    # 集計表の列
    for label in ("課題の平均点", "最高点回数", "提出数", "未提出数", "未確定数"):
        assert label in js
    # 連続実行で見出しが重複しないよう、最新要求だけを描画する
    assert "let rankingRequestId=0;" in js
    assert "if(requestId!==rankingRequestId)return;" in js
    # DOM書き換えはawaitの後にまとめて行う
    ranking = js[js.index("async function loadRanking("):js.index("async function exportRanking(")]
    assert ranking.index("await api(") < ranking.index("head.replaceChildren()")


def test_ranking_is_cached_and_refresh_bypasses_the_cache(client, monkeypatch):
    """数分間は再集計せずキャッシュを返し、更新指定で破棄する。"""
    calls = []
    monkeypatch.setattr(api, "_classroom_for", lambda _identity: object())
    monkeypatch.setattr(api, "_live_confirmed_grades",
                        lambda *_a, **_k: calls.append(1) or {"111": 5.0})
    monkeypatch.setattr("grader.fetch.list_courseworks",
                        lambda *a, **k: [{"id": "100000000001", "title": "課題", "maxPoints": 10}])
    csrf = login(client)

    first = client.get("/api/v1/courses/200000000001/ranking").json()
    assert first["cached"] is False and len(calls) == 1
    # 2回目はキャッシュを返し、Classroomへ問い合わせない
    second = client.get("/api/v1/courses/200000000001/ranking").json()
    assert second["cached"] is True and len(calls) == 1
    assert second["rows"] == first["rows"]
    # refresh=trueなら再集計する
    third = client.get("/api/v1/courses/200000000001/ranking?refresh=true").json()
    assert third["cached"] is False and len(calls) == 2
    # 教員が点数を確定するとキャッシュを破棄する
    client.put("/api/v1/courses/200000000001/courseworks/100000000001/reviews/111",
               headers={"X-CSRF-Token": csrf}, json={"score": 9, "confirmed": True})
    after = client.get("/api/v1/courses/200000000001/ranking").json()
    assert after["cached"] is False and len(calls) == 3


def test_top_scorers_endpoint_and_answer_view(client, monkeypatch, tmp_path):
    """最高点取得者APIと、その答案を確認するAPI。"""
    monkeypatch.setattr(api, "_classroom_for", lambda _identity: object())
    monkeypatch.setattr(api, "_live_confirmed_grades", lambda *_a, **_k: {"111": 10.0})
    monkeypatch.setattr("grader.fetch.list_courseworks",
                        lambda *a, **k: [{"id": "100000000001", "title": "課題",
                                          "maxPoints": 10}])
    login(client)
    body = client.get("/api/v1/courses/200000000001/top-scorers").json()
    entry = body["courseworks"][0]
    assert entry["top_score"] == 10.0 and entry["max_points"] == 10
    assert [s["student_id"] for s in entry["scorers"]] == ["111"]
    assert body["cached"] is False

    # 答案表示: メタが無ければ404
    assert client.get(
        "/api/v1/courses/200000000001/courseworks/100000000001"
        "/submissions/111/answer").status_code == 404
    # student_idの検証
    assert client.get(
        "/api/v1/courses/200000000001/courseworks/100000000001"
        "/submissions/abc/answer").status_code == 400

    # テキスト答案を返せること
    from grader.course_data import CoursePaths

    paths = CoursePaths(api._cfg, "200000000001", "100000000001")
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.meta.write_text(json.dumps({"student_id": "111", "state": "TURNED_IN"}),
                          encoding="utf-8")
    monkeypatch.setattr(api, "submission_text", lambda *_a: {
        "status": "ready", "answer_text": "回答本文", "page_count": 1})
    answer = client.get(
        "/api/v1/courses/200000000001/courseworks/100000000001"
        "/submissions/111/answer").json()
    assert answer["content_mode"] == "text" and answer["answer_text"] == "回答本文"
    # 答案は信頼できない入力である旨を必ず添える
    assert answer["untrusted_content"] is True and answer["warning"]


def test_mcp_exposes_ranking_top_scorers_and_guarded_sheets_export():
    """MCPからランキング・最高点取得者を参照でき、Sheets出力はconfirm必須。"""
    import inspect

    from grader import mcp_server

    source = inspect.getsource(mcp_server)
    assert "def get_ranking(" in source
    assert "def get_course_top_scorers(" in source
    assert "def export_ranking_to_sheets(" in source
    # 出力は明示確認がないと実行しない
    export = source[source.index("def export_ranking_to_sheets("):
                    source.index("def start_full_grading(")]
    assert "if confirm is not True:" in export
    assert "READ" in source[source.index("def get_course_top_scorers(") - 60:
                            source.index("def get_course_top_scorers(")]


def test_web_ui_collapses_coursework_list_and_shows_top_scorers():
    """課題一覧を折りたためること、最高点取得者と答案表示があること。"""
    import pathlib

    html = pathlib.Path("grader/web/index.html").read_text(encoding="utf-8")
    js = pathlib.Path("grader/web/app.js").read_text(encoding="utf-8")
    # 課題一覧は既定で閉じる(openを付けない)
    assert '<details id="courses-details">' in html
    assert 'id="courses-summary"' in html
    assert 'id="top-scorers"' in html and 'id="answer-dialog"' in html
    # 点数内訳と最高点者はタブで切り替える(既定は内訳)
    assert "function selectRankingSubtab(name)" in js
    assert 'ranking:"subpanel-ranking"' in js and 'zero:"subpanel-zero"' in js
    # 既定はランキング
    assert 'id="subtab-ranking" class="subtab active"' in html
    assert 'localStorage.getItem("cga-ranking-subtab-v2")||"ranking"' in js
    # 0点・未提出はランキング取得結果から組み立てる(追加のAPI呼び出しをしない)
    assert "function renderZeroAndMissing()" in js
    assert 'row.not_submitted?.[c.coursework_id]' in js
    assert "row.scores[c.coursework_id]===0" in js
    # 最高点者タブを開いたときに初回だけ取得する
    assert 'if(name==="top"&&!topScorersLoaded&&selectedCourseId)loadTopScorers();' in js
    assert "async function loadTopScorers(" in js
    assert "async function openAnswer(" in js
    # 折りたたんでも件数が分かる
    assert "課題一覧（${total}件" in js


def test_mcp_announcement_preview_create_is_draft_and_idempotent(client, monkeypatch):
    principal = McpPrincipal("a" * 64, "grader")
    google = _FakeGoogleService()
    monkeypatch.setattr(api, "_classroom_for", lambda _identity: google)
    preview = api._mcp_preview_classroom_announcement(
        principal, "200000000001", "休講のお知らせ")
    assert preview["preview"] is True and preview["classroom_written"] is False
    # 素材(添付・リンク)と個別配信は扱わない
    assert preview["announcement"] == {
        "text": "休講のお知らせ", "state": "DRAFT", "assigneeMode": "ALL_STUDENTS"}
    assert google.announcement.create_calls == []
    created = api._mcp_create_classroom_announcement_draft(
        principal, "200000000001", "休講のお知らせ", "announcement_test_001")
    assert created["state"] == "DRAFT" and created["classroom_written"] is True
    assert created["announcement_id"] == "500000000001"
    assert google.announcement.create_calls[0]["body"]["state"] == "DRAFT"
    repeated = api._mcp_create_classroom_announcement_draft(
        principal, "200000000001", "休講のお知らせ", "announcement_test_001")
    assert repeated["replayed"] is True and len(google.announcement.create_calls) == 1
    with pytest.raises(api.HTTPException) as mismatch:
        api._mcp_create_classroom_announcement_draft(
            principal, "200000000001", "別のお知らせ", "announcement_test_001")
    assert mismatch.value.status_code == 409
    with pytest.raises(api.HTTPException) as empty:
        api._mcp_create_classroom_announcement_draft(
            principal, "200000000001", "   ", "announcement_test_002")
    assert empty.value.status_code == 400


def test_mcp_announcement_publish_requires_own_draft_and_exact_text(client, monkeypatch):
    principal = McpPrincipal("a" * 64, "grader")
    other = McpPrincipal("b" * 64, "grader")
    google = _FakeGoogleService()
    monkeypatch.setattr(api, "_classroom_for", lambda _identity: google)
    # 作成記録が無いお知らせは、本文が一致しても公開しない
    with pytest.raises(api.HTTPException) as unknown:
        api._mcp_publish_classroom_announcement(
            principal, "200000000001", "500000000001", "休講のお知らせ")
    assert unknown.value.status_code == 409 and google.announcement.patch_calls == []
    api._mcp_create_classroom_announcement_draft(
        principal, "200000000001", "休講のお知らせ", "announcement_publish_001")
    # 別のMCP利用者は公開できない
    with pytest.raises(api.HTTPException) as foreign:
        api._mcp_publish_classroom_announcement(
            other, "200000000001", "500000000001", "休講のお知らせ")
    assert foreign.value.status_code == 409 and google.announcement.patch_calls == []
    with pytest.raises(api.HTTPException) as mismatch:
        api._mcp_publish_classroom_announcement(
            principal, "200000000001", "500000000001", "違う本文")
    assert mismatch.value.status_code == 409 and google.announcement.patch_calls == []
    published = api._mcp_publish_classroom_announcement(
        principal, "200000000001", "500000000001", "休講のお知らせ")
    assert published["state"] == "PUBLISHED" and published["classroom_written"] is True
    assert google.announcement.patch_calls[0]["updateMask"] == "state"
    assert google.announcement.patch_calls[0]["body"] == {"state": "PUBLISHED"}
    repeated = api._mcp_publish_classroom_announcement(
        principal, "200000000001", "500000000001", "休講のお知らせ")
    assert repeated["already_published"] is True
    assert len(google.announcement.patch_calls) == 1


def test_oauth_scopes_include_announcements_and_require_reconsent():
    from grader import google_auth

    assert google_auth.ANNOUNCEMENTS_SCOPE in google_auth.OAUTH_SCOPES
    # CLI用SCOPESへは追加しない(お知らせはMCP/Web経由の明示操作だけ)
    from grader.fetch import SCOPES

    assert google_auth.ANNOUNCEMENTS_SCOPE not in SCOPES
    assert set(google_auth.ADDED_SCOPES) == {
        google_auth.SHEETS_SCOPE, google_auth.ANNOUNCEMENTS_SCOPE}


def test_mcp_system_overview_explains_policy_without_touching_classroom(monkeypatch):
    from grader import mcp_guide

    principal = McpPrincipal("a" * 64, "grader")
    # Classroom/data参照が無いことを、接続を壊した状態で確認する
    monkeypatch.setattr(api, "_classroom_for", lambda _identity: pytest.fail(
        "get_system_overviewはClassroomを参照してはならない"))
    index = api._mcp_get_system_overview(principal, None)
    assert {t["id"] for t in index["topics"]} == set(mcp_guide.topic_ids())
    assert "採点案" in index["summary"]
    assert "Qwen2.5" in index["standard_operation"]
    # 採点基準テンプレートはsettings_presetsを唯一の出所とする
    assert {p["id"] for p in index["grading_presets"]} == set(
        settings_presets.PRESETS)
    for topic in mcp_guide.topic_ids():
        value = api._mcp_get_system_overview(principal, topic)
        assert value["topic"] == topic and value["body"]
    # 禁止事項と未提出ペナルティが説明に含まれる
    assert any("相対評価" in line for line in
               api._mcp_get_system_overview(principal, "policy")["body"])
    assert any("-1/3" in line for line in
               api._mcp_get_system_overview(principal, "ranking")["body"])
    assert any("assignedGrade" in line for line in
               api._mcp_get_system_overview(principal, "glossary")["body"])
    with pytest.raises(api.HTTPException) as bad:
        api._mcp_get_system_overview(principal, "../secrets")
    assert bad.value.status_code == 400
    with pytest.raises(api.HTTPException):
        api._mcp_get_system_overview(principal, "x" * 65)
