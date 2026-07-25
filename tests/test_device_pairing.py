import json
import os

from grader.device_pairing import DevicePairingStore


def test_device_code_is_one_time_hash_only_owner_scoped_and_revocable(tmp_path):
    now = [1_700_000_000]
    store = DevicePairingStore(tmp_path, clock=lambda: now[0])
    made = store.create_code("a" * 64, label="TA laptop", expires_days=90)
    claimed = store.claim(made["pairing_code"])
    assert claimed["device_token"].startswith("cgd_")
    assert store.claim(made["pairing_code"]) is None
    path = tmp_path / ("a" * 64 + ".json")
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert claimed["device_token"] not in path.read_text(encoding="utf-8")
    assert len(saved[0]["token_sha256"]) == 64
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert store.verify(claimed["device_token"])["owner_ref"] == "a" * 64
    assert store.list_owner("b" * 64) == []
    assert not store.revoke("b" * 64, claimed["device_id"])
    assert store.revoke("a" * 64, claimed["device_id"])
    assert store.verify(claimed["device_token"]) is None


def test_device_expiry_and_limits(tmp_path):
    now = [100]
    store = DevicePairingStore(tmp_path, clock=lambda: now[0], code_ttl_seconds=60)
    made = store.create_code("a" * 64, label="x", expires_days=1)
    now[0] = 161
    assert store.claim(made["pairing_code"]) is None
