"""grade_submission の冪等性・再提出検知・エラー処理(VLM呼び出しはモック)。"""
import asyncio
import json

import fitz
import pytest

from grader.config import Config
from grader.grade import Grader


def _cfg():
    return Config(raw={"vllm": {"model": "dummy"}, "grading": {"runs": 2, "concurrency": 2}})


def _run_json(scores=(1, 1, 0)):
    names = ["quantitative", "method", "discussion"]
    return {
        "gate": {"pass": True, "reason": "ok"},
        "criteria": [
            {"name": n, "score": s, "evidence": "e", "comment": "c"}
            for n, s in zip(names, scores)
        ],
        "total": sum(scores),
        "flags": [],
        "notable": None,
    }


@pytest.fixture
def pdf(tmp_path):
    p = tmp_path / "s.pdf"
    doc = fitz.open()
    doc.new_page()
    doc.save(p)
    doc.close()
    return p


def test_grade_submission_and_skip(tmp_path, pdf, monkeypatch):
    g = Grader(_cfg())
    calls = {"n": 0}

    async def fake_once(pages):
        calls["n"] += 1
        return _run_json()

    monkeypatch.setattr(g, "grade_once", fake_once)
    rp = tmp_path / "r.json"
    res = asyncio.run(g.grade_submission("s1", [], rp, source_pdf=pdf))
    assert res["final_score"] == 2 and res["status"] == "ok" and calls["n"] == 2

    # 2回目はスキップ(冪等)
    asyncio.run(g.grade_submission("s1", [], rp, source_pdf=pdf))
    assert calls["n"] == 2

    # 再提出(内容変更)を検知して再採点
    doc = fitz.open(pdf)
    doc.new_page()
    doc.saveIncr()
    doc.close()
    asyncio.run(g.grade_submission("s1", [], rp, source_pdf=pdf))
    assert calls["n"] == 4

    # --force でも再採点
    asyncio.run(g.grade_submission("s1", [], rp, source_pdf=pdf, force=True))
    assert calls["n"] == 6


def test_grade_submission_error_recorded(tmp_path, pdf, monkeypatch):
    g = Grader(_cfg())

    async def boom(pages):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(g, "grade_once", boom)
    res = asyncio.run(g.grade_submission("s2", [], tmp_path / "r2.json", source_pdf=pdf))
    assert res["status"] == "ERROR" and "error" in res["flags"]
    saved = json.loads((tmp_path / "r2.json").read_text())
    assert saved["status"] == "ERROR"
