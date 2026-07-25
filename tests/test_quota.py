"""experiment_quota(演習系3点候補の分布較正)のテスト。"""
from __future__ import annotations

import pathlib

from grader.report import apply_experiment_quota


class FakeCfg:
    def __init__(self, quota, data_dir="/nonexistent"):
        self._quota = quota
        self.data_dir = pathlib.Path(data_dir)

    def get(self, *keys, default=None):
        if keys and keys[0] == "experiment_quota":
            return self._quota
        return default


def _rows(n_candidates, n_others=0):
    rows = []
    for i in range(n_candidates):
        rows.append({
            "student_id": f"c{i}", "category": "candidate_3",
            "content_score": 3, "score_after_late": 3.0,
            "tier": "strong" if i % 2 == 0 else "borderline",
            "late": False, "flags": "",
        })
    for i in range(n_others):
        rows.append({
            "student_id": f"o{i}", "category": "auto_2",
            "content_score": 2, "score_after_late": 2.0,
            "tier": "", "late": False, "flags": "",
        })
    return rows


def test_quota_disabled_leaves_rows_untouched():
    rows = _rows(10)
    cfg = FakeCfg({"enabled": False})
    apply_experiment_quota(rows, cfg, "cw", "EXPERIMENT", {}, penalty=1)
    assert all(r["category"] == "candidate_3" for r in rows)


def test_quota_only_applies_to_experiment():
    rows = _rows(10)
    cfg = FakeCfg({"enabled": True, "max_rate": 0.3, "band": 2})
    apply_experiment_quota(rows, cfg, "cw", "KANSOU", {}, penalty=1)
    assert all(r["category"] == "candidate_3" for r in rows)


def test_quota_partitions_candidates():
    # 提出30人(候補20+その他10)、max_rate=0.3 → cap=9、band=3 → auto_3=6
    rows = _rows(20, n_others=10)
    cfg = FakeCfg({"enabled": True, "max_rate": 0.3, "band": 3})
    crit = {f"c{i}": 3.0 - 0.1 * i for i in range(20)}  # c0が最上位
    apply_experiment_quota(rows, cfg, "cw", "EXPERIMENT", crit, penalty=1)
    cands = [r for r in rows if r["student_id"].startswith("c")]
    auto3 = [r for r in cands if r["category"] == "auto_3"]
    band = [r for r in cands if r["category"] == "candidate_3"]
    demoted = [r for r in cands if r["category"] == "auto_2"]
    assert len(auto3) == 6
    assert len(band) == 3
    assert len(demoted) == 11
    # 上位が確定・下位が降格(critの高い順)
    assert {r["student_id"] for r in auto3} == {f"c{i}" for i in range(6)}
    assert all("quota_confirmed" in r["flags"] for r in auto3)
    # 降格側はスコア2+返却保留フラグ
    for r in demoted:
        assert r["content_score"] == 2
        assert r["score_after_late"] == 2
        assert "quota_demoted" in r["flags"]
        assert "demoted_from_3" in r["flags"]
    # 3点総数はcap(9)を超えない(境界帯を全承認しても9)
    assert len(auto3) + len(band) == 9


def test_quota_demotion_applies_late_penalty():
    rows = _rows(3)
    rows[2]["late"] = True
    cfg = FakeCfg({"enabled": True, "max_rate": 0.34, "band": 1})  # cap=1, auto=0
    crit = {"c0": 3.0, "c1": 2.5, "c2": 2.5}
    apply_experiment_quota(rows, cfg, "cw", "EXPERIMENT", crit, penalty=1)
    late_row = next(r for r in rows if r["student_id"] == "c2")
    assert late_row["category"] == "auto_2"
    assert late_row["score_after_late"] == 1  # 2点から遅延減点1


def test_quota_fewer_candidates_than_cap():
    # 候補がcapより少なければ、band分を残して全て自動確定
    rows = _rows(4, n_others=26)
    cfg = FakeCfg({"enabled": True, "max_rate": 0.3, "band": 2})  # cap=9, auto=7
    crit = {f"c{i}": 3.0 for i in range(4)}
    apply_experiment_quota(rows, cfg, "cw", "EXPERIMENT", crit, penalty=1)
    cands = [r for r in rows if r["student_id"].startswith("c")]
    assert sum(1 for r in cands if r["category"] == "auto_3") == 4
    assert sum(1 for r in cands if r["category"] == "auto_2") == 0
