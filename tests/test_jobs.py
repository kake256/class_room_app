import subprocess

import pytest

from grader.jobs import JobConflictError, JobOptions, JobService, JobStore


def test_store_recovers_running_job(tmp_path):
    store = JobStore(tmp_path)
    store.save({"id": "abc", "status": "running", "created_at": "1"})
    recovered = JobStore(tmp_path).get("abc")
    assert recovered["status"] == "interrupted"
    assert recovered["finished_at"]


def test_job_success_and_persistence(tmp_path, monkeypatch):
    store = JobStore(tmp_path)
    service = JobService(store, runner=lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "ok", ""))
    monkeypatch.setattr("grader.jobs.threading.Thread.start", lambda self: self.run())
    job = service.create("123", "report", JobOptions())
    saved = store.get(job["id"])
    assert saved["status"] == "succeeded" and saved["log"] == "ok"


def test_prepare_runs_fetch_only_without_model_guard_or_switch(tmp_path, monkeypatch):
    store = JobStore(tmp_path)
    commands, guards, switches = [], [], []
    service = JobService(
        store,
        runner=lambda command, **_kwargs: (
            commands.append(command) or subprocess.CompletedProcess(command, 0, "ok", "")),
        model_guard=lambda phase: (guards.append(phase) or True, ""),
        model_switcher=lambda phase: (switches.append(phase) or True, ""),
        token_resolver=lambda ref: tmp_path / f"{ref}.json",
    )
    monkeypatch.setattr("grader.jobs.threading.Thread.start", lambda self: self.run())
    job = service.create("456", "prepare", JobOptions(), course_id="123", token_ref="a" * 64)
    assert store.get(job["id"])["completed_steps"] == ["prepare"]
    assert commands[0][3] == "fetch"
    assert "run" not in commands[0] and "report" not in commands[0]
    assert guards == switches == []


def test_duplicate_active_owner_context_is_rejected(tmp_path, monkeypatch):
    store = JobStore(tmp_path)
    service = JobService(store)
    monkeypatch.setattr("grader.jobs.threading.Thread.start", lambda self: None)
    service.create("456", "prepare", JobOptions(), course_id="123", token_ref="a" * 64)
    with pytest.raises(JobConflictError):
        service.create("456", "prepare", JobOptions(), course_id="123", token_ref="a" * 64)


def test_job_failure(tmp_path, monkeypatch):
    store = JobStore(tmp_path)
    service = JobService(store, runner=lambda *a, **k: subprocess.CompletedProcess(a[0], 2, "", "bad"))
    monkeypatch.setattr("grader.jobs.threading.Thread.start", lambda self: self.run())
    job = service.create("123", "run", JobOptions(force=True))
    saved = store.get(job["id"])
    assert saved["status"] == "failed" and "終了コード2" in saved["error"]


def test_job_fifo_queue_and_queued_cancel(tmp_path, monkeypatch):
    store = JobStore(tmp_path)
    service = JobService(store)
    monkeypatch.setattr("grader.jobs.threading.Thread.start", lambda self: None)
    first = service.create("123", "run", JobOptions())
    second = service.create("456", "report", JobOptions())
    assert first["status"] == second["status"] == "queued"
    assert first["created_ns"] < second["created_ns"]
    canceled = service.cancel(second["id"])
    assert canceled["status"] == "canceled"
    with pytest.raises(ValueError):
        service.cancel(second["id"])


def test_model_mismatch_blocks_without_running_command(tmp_path, monkeypatch):
    store = JobStore(tmp_path)
    called = []
    service = JobService(
        store,
        runner=lambda *args, **kwargs: called.append(args) or subprocess.CompletedProcess([], 0),
        model_guard=lambda _phase: (False, "必要モデル不一致"),
    )
    monkeypatch.setattr("grader.jobs.threading.Thread.start", lambda self: self.run())
    job = service.create("123", "run", JobOptions())
    assert store.get(job["id"])["status"] == "blocked"
    assert called == []


def test_queued_job_blocks_when_settings_revision_changed(tmp_path, monkeypatch):
    store = JobStore(tmp_path)
    service = JobService(
        store, runner=lambda *_a, **_k: pytest.fail("runner must not be called"),
        settings_guard=lambda _course, _cw, revision: revision == "current",
    )
    monkeypatch.setattr("grader.jobs.threading.Thread.start", lambda self: self.run())
    job = service.create("456", "report", JobOptions(), course_id="123",
                         settings_revision="old")
    assert store.get(job["id"])["status"] == "blocked"


def test_store_interrupts_queued_job_on_restart(tmp_path):
    store = JobStore(tmp_path)
    store.save({"id": "queued", "status": "queued", "created_at": "1"})
    assert JobStore(tmp_path).get("queued")["status"] == "interrupted"


def test_web_job_keeps_anonymous_token_ref_and_passes_safe_overrides(tmp_path, monkeypatch):
    store = JobStore(tmp_path / "jobs")
    commands = []
    token_root = tmp_path / "tokens"
    token_root.mkdir()
    service = JobService(
        store,
        runner=lambda command, **_kwargs: (
            commands.append(command) or subprocess.CompletedProcess(command, 0, "ok", "")
        ),
        token_resolver=lambda ref: token_root / f"{ref}.json",
    )
    monkeypatch.setattr("grader.jobs.threading.Thread.start", lambda self: self.run())
    ref = "a" * 64
    job = service.create("456", "run", JobOptions(), course_id="123", token_ref=ref)
    saved = store.get(job["id"])
    assert saved["course_id"] == "123" and saved["token_ref"] == ref
    assert commands[0][-4:] == ["--course-id", "123", "--token-file",
                                str(token_root / f"{ref}.json")]


def test_full_is_single_stage_qwen25_without_refine(tmp_path, monkeypatch):
    """標準のfullジョブはQwen2.5単独の1段階採点。Qwen3審判(refine)を含めない。"""
    store = JobStore(tmp_path / "jobs")
    commands, switches = [], []
    service = JobService(
        store,
        runner=lambda command, **_kwargs: (
            commands.append(command) or subprocess.CompletedProcess(command, 0, "ok", "")
        ),
        model_switcher=lambda phase: (switches.append(phase) or True, ""),
        model_guard=lambda _phase: (True, ""),
    )
    monkeypatch.setattr("grader.jobs.threading.Thread.start", lambda self: self.run())
    job = service.create("456", "full", JobOptions())
    saved = store.get(job["id"])
    assert saved["status"] == "succeeded"
    assert saved["completed_steps"] == ["run", "report"]
    # モデル切替は一次採点(Qwen2.5)の1回だけ。Qwen3へは切り替えない。
    assert switches == ["run"]
    assert [command[3] for command in commands] == ["run", "report"]


def test_confirmed_full_uses_prepare_and_hybrid_with_owner(tmp_path, monkeypatch):
    store = JobStore(tmp_path / "jobs")
    commands, switches, guards = [], [], []
    token_root = tmp_path / "tokens"
    token_root.mkdir()
    service = JobService(
        store,
        runner=lambda command, **_kwargs: (
            commands.append(command) or subprocess.CompletedProcess(command, 0, "ok", "")
        ),
        token_resolver=lambda ref: token_root / f"{ref}.json",
        model_switcher=lambda phase: (switches.append(phase) or True, ""),
        model_guard=lambda phase: (guards.append(phase) or True, ""),
    )
    monkeypatch.setattr("grader.jobs.threading.Thread.start", lambda self: self.run())
    ref = "b" * 64
    job = service.create(
        "456", "full", JobOptions(hybrid=True),
        course_id="123", token_ref=ref,
    )
    saved = store.get(job["id"])
    assert saved["status"] == "succeeded"
    assert saved["completed_steps"] == ["prepare", "hybrid"]
    assert switches == guards == ["run"]
    assert [command[3] for command in commands] == ["fetch", "hybrid"]
    assert commands[1][-2:] == ["--owner-ref", ref]


def test_individual_phase_keeps_manual_model_compatibility(tmp_path, monkeypatch):
    store = JobStore(tmp_path / "jobs")
    switches = []
    service = JobService(
        store,
        runner=lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "ok", ""),
        model_switcher=lambda phase: (switches.append(phase) or True, ""),
        model_guard=lambda _phase: (True, ""),
    )
    monkeypatch.setattr("grader.jobs.threading.Thread.start", lambda self: self.run())
    job = service.create("456", "run", JobOptions())
    assert store.get(job["id"])["status"] == "succeeded"
    assert switches == []


def test_full_stops_before_command_when_model_switch_fails(tmp_path, monkeypatch):
    store = JobStore(tmp_path / "jobs")
    commands = []
    service = JobService(
        store,
        runner=lambda command, **_kwargs: (
            commands.append(command) or subprocess.CompletedProcess(command, 0, "ok", "")
        ),
        model_switcher=lambda _phase: (False, "モデル準備がタイムアウトしました"),
        model_guard=lambda _phase: (True, ""),
    )
    monkeypatch.setattr("grader.jobs.threading.Thread.start", lambda self: self.run())
    job = service.create("456", "full", JobOptions())
    saved = store.get(job["id"])
    assert saved["status"] == "blocked"
    assert saved["current_step"] == "model:run"
    assert "タイムアウト" in saved["error"]
    assert commands == []


def test_command_timeout_is_reported_and_stops_full_pipeline(tmp_path, monkeypatch):
    store = JobStore(tmp_path / "jobs")
    commands = []

    def timeout(command, **_kwargs):
        commands.append(command)
        raise subprocess.TimeoutExpired(command, 7200)

    service = JobService(
        store, runner=timeout,
        model_switcher=lambda _phase: (True, ""),
        model_guard=lambda _phase: (True, ""),
    )
    monkeypatch.setattr("grader.jobs.threading.Thread.start", lambda self: self.run())
    job = service.create("456", "full", JobOptions())
    saved = store.get(job["id"])
    assert saved["status"] == "failed"
    assert saved["current_step"] == "run"
    assert "制限時間" in saved["error"]
    assert len(commands) == 1


def test_hybrid_grading_timestamps_are_persisted_on_the_job(tmp_path, monkeypatch):
    """採点段階の時刻計測がジョブへ永続化され、進捗APIから参照できる形で残る。"""
    import json as _json

    store = JobStore(tmp_path / "jobs")
    token_root = tmp_path / "tokens"
    token_root.mkdir()
    stamps = {
        "first_primary_saved_at": "2026-07-25T10:00:00+0900",
        "primary_completed_at": "2026-07-25T10:01:00+0900",
        "review_started_at": "2026-07-25T10:01:01+0900",
        "first_ready_for_human_review_at": "2026-07-25T10:00:30+0900",
        "review_completed_at": "2026-07-25T10:02:00+0900",
    }

    def runner(command, **_kwargs):
        stdout = "log line\n" + _json.dumps({"status": "succeeded", "timestamps": stamps})
        return subprocess.CompletedProcess(command, 0, stdout, "")

    service = JobService(
        store, runner=runner,
        token_resolver=lambda ref: token_root / f"{ref}.json",
        model_switcher=lambda phase: (True, ""),
        model_guard=lambda phase: (True, ""),
    )
    monkeypatch.setattr("grader.jobs.threading.Thread.start", lambda self: self.run())
    job = service.create(
        "456", "full", JobOptions(hybrid=True), course_id="123", token_ref="b" * 64)
    saved = store.get(job["id"])
    assert saved["status"] == "succeeded"
    assert saved["grading_timestamps"] == stamps


def test_unparsable_hybrid_output_does_not_fail_the_job(tmp_path, monkeypatch):
    store = JobStore(tmp_path / "jobs")
    token_root = tmp_path / "tokens"
    token_root.mkdir()
    service = JobService(
        store,
        runner=lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 0, "no json here", ""),
        token_resolver=lambda ref: token_root / f"{ref}.json",
        model_switcher=lambda phase: (True, ""),
        model_guard=lambda phase: (True, ""),
    )
    monkeypatch.setattr("grader.jobs.threading.Thread.start", lambda self: self.run())
    job = service.create(
        "456", "full", JobOptions(hybrid=True), course_id="123", token_ref="b" * 64)
    saved = store.get(job["id"])
    assert saved["status"] == "succeeded"
    assert "grading_timestamps" not in saved


def test_refine_remains_available_as_a_standalone_diagnostic_phase(tmp_path, monkeypatch):
    """refineはfullから外したが、診断・比較用に単独フェーズとして実行できる。"""
    store = JobStore(tmp_path / "jobs")
    commands, switches = [], []
    service = JobService(
        store,
        runner=lambda command, **_kwargs: (
            commands.append(command) or subprocess.CompletedProcess(command, 0, "ok", "")
        ),
        model_switcher=lambda phase: (switches.append(phase) or True, ""),
        model_guard=lambda _phase: (True, ""),
    )
    monkeypatch.setattr("grader.jobs.threading.Thread.start", lambda self: self.run())
    job = service.create("456", "refine", JobOptions())
    saved = store.get(job["id"])
    assert saved["status"] == "succeeded"
    assert saved["completed_steps"] == ["refine"]
    # 単独実行では自動切替せず、管理者が用意したモデルをguardで確認する運用のまま
    assert switches == []
    assert [command[3] for command in commands] == ["refine"]
