"""Offline tests for hooks/orch_push.py: the sealed envelope, the service account's OAuth, the FCM
send and its errors. The network is a fake. Needs the `cryptography` package (Ubuntu 20.04 has 2.8:
`uv run --no-project --python 3.8 --with pytest --with cryptography==2.8 pytest`); without it the
crypto tests skip and only the missing-package behaviour is checked."""
import base64
import json
import random
import sys
import zlib
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(HOOKS))
import orch_push

KEY = bytes(range(32))
# docs/PUSH.md "Test vectors": key AAECAwQF...Hh8 (bytes 0..31), nonce bytes 100..111, machine box1.
VECTOR_PUSH = {"v": "1", "machine": "box1", "nonce": "ZGVmZ2hpamtsbW5v",
               "ct": "MMH7rDLj1q4yJ45VZhR6AgC0FWHLPakeRZEIM6zT0Yd013D9jQHmifOd3o4Q-LTT8TG4C3sWgXCvQ2P8"
                     "4cNv611_95Dkc2XCyvc-MZKVqW3PBTUwruTQFzKOfL16vK5zJXGk7rQLA3LP4lLMC0js5c8"}
VECTOR_RELAY = {"v": "1", "machine": "box1", "nonce": "ZGVmZ2hpamtsbW5v",
                "ct": "MMGb6iTnFq4u5vDKIcnYABMgFuTbwFdjsn2MqhV5grtk1DxZ1RVnBPArKcl3ksNDCn05qeB1U1qazr_i"
                      "XTPv4WvH9z5Rgrzxa62TkQex2qdfgzpwEI8q_AJqF4mS8eaLC8IJZrVCnjyTeHLNN7z2FlAELi7-Ys7n"
                      "GXg"}


@pytest.fixture
def crypto():
    return pytest.importorskip("cryptography")


def test_an_envelope_opens_only_with_its_key_direction_and_machine(crypto):
    msg = {"seq": 3, "kind": "question", "body": {"text": "Deploy? " * 200}}
    env = orch_push.seal(KEY, "push", "box1", msg)
    assert set(env) == {"v", "machine", "nonce", "ct"} and env["v"] == "1"
    assert len(env["nonce"]) == 16 and "=" not in env["ct"] + env["nonce"]
    assert len(orch_push.b64d(env["ct"])) < 400  # compressed before sealing
    assert orch_push.unseal(KEY, "push", "box1", env) == msg
    assert orch_push.sealed_size("box1", msg) == sum(len(k) + len(v) for k, v in env.items())
    assert orch_push.seal(KEY, "push", "box1", msg)["nonce"] != env["nonce"]
    for key, direction, machine in ((bytes(32), "push", "box1"), (KEY, "relay", "box1"),
                                    (KEY, "push", "box2")):
        with pytest.raises(Exception):  # noqa: B017 cryptography's InvalidTag
            orch_push.unseal(key, direction, machine, env)
    with pytest.raises(ValueError):
        orch_push.unseal(KEY, "push", "box1", dict(env, v="2"))


def test_the_published_test_vectors_open(crypto):
    assert orch_push.b64e(KEY) == "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
    assert orch_push.unseal(KEY, "push", "box1", VECTOR_PUSH) == {
        "seq": 7, "sent": 1762334650.5, "kind": "heartbeat", "orchestrator": None,
        "body": {"states": {}, "accounts": None}}
    assert orch_push.unseal(KEY, "relay", "box1", VECTOR_RELAY) == {
        "id": "6f1c2e0a-0000-4000-8000-000000000001", "time": 1762334650.0, "kind": "answer",
        "question": "q1a2b3c4d", "text": "Not yet"}


def test_a_decompression_bomb_is_refused(crypto):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    plain = zlib.compress(b"[" + b"0," * (1 << 20) + b"0]", 9)
    nonce = bytes(12)
    env = {"v": "1", "machine": "box1", "nonce": orch_push.b64e(nonce),
           "ct": orch_push.b64e(AESGCM(KEY).encrypt(nonce, plain, orch_push.aad("relay", "box1")))}
    with pytest.raises(ValueError, match="1 MB"):
        orch_push.unseal(KEY, "relay", "box1", env)


def test_without_the_cryptography_package_it_says_so_once(monkeypatch):
    for name in [m for m in sys.modules if m.split(".")[0] == "cryptography"] + [
            "cryptography", "cryptography.hazmat.backends",
            "cryptography.hazmat.primitives.ciphers.aead"]:
        monkeypatch.setitem(sys.modules, name, None)
    with pytest.raises(orch_push.Unavailable, match="python3-cryptography"):
        orch_push.seal(KEY, "push", "box1", {})


# --- Google ---------------------------------------------------------------------------------------

class Net:
    """A fake for orch_push.http: answers by URL, records every call."""

    def __init__(self):
        self.calls = []
        self.fcm = [(200, {"name": "projects/p/messages/1"})]

    def __call__(self, method, url, body=None, headers=None, form=False, timeout=20):
        self.calls.append({"method": method, "url": url, "body": body, "headers": headers or {},
                           "form": form})
        if url == orch_push.TOKEN_URI:
            return 200, {"access_token": f"at{len(self.calls)}", "expires_in": 3599}
        if "fcm.googleapis.com" in url:
            return self.fcm.pop(0) if len(self.fcm) > 1 else self.fcm[0]
        return 404, {}


@pytest.fixture
def sa(crypto, tmp_path):
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(65537, 2048, default_backend())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    path = tmp_path / "firebase-sa.json"
    path.write_text(json.dumps({
        "type": "service_account", "project_id": "orch-test", "private_key_id": "kid1",
        "private_key": pem, "client_email": "orch-push@orch-test.iam.gserviceaccount.com",
        "token_uri": orch_push.TOKEN_URI}))
    return path, key.public_key()


def test_the_oauth_assertion_is_an_rs256_jwt_of_the_service_account(sa, monkeypatch):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    path, public = sa
    google = orch_push.Google.load(path)
    jwt = google.assertion(1762334650.7)
    head, claims, sig = jwt.split(".")
    assert json.loads(orch_push.b64d(head)) == {"alg": "RS256", "typ": "JWT", "kid": "kid1"}
    assert json.loads(orch_push.b64d(claims)) == {
        "iss": "orch-push@orch-test.iam.gserviceaccount.com", "scope": orch_push.SCOPES,
        "aud": "https://oauth2.googleapis.com/token", "iat": 1762334650, "exp": 1762338250}
    public.verify(orch_push.b64d(sig), f"{head}.{claims}".encode(), padding.PKCS1v15(),
                  hashes.SHA256())  # raises when wrong


def test_a_send_uses_one_cached_token_and_the_android_options(sa, monkeypatch):
    net = Net()
    monkeypatch.setattr(orch_push, "http", net)
    google = orch_push.Google.load(sa[0])
    assert google.send("tok", {"v": "1"}, "question", None)[0] == 200
    assert google.send("tok", {"v": "1"}, "state", "box1:state:w1:p0")[0] == 200
    token_calls = [c for c in net.calls if c["url"] == orch_push.TOKEN_URI]
    assert len(token_calls) == 1 and token_calls[0]["form"]
    assert token_calls[0]["body"]["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
    first, second = [c for c in net.calls if "fcm" in c["url"]]
    assert first["url"] == "https://fcm.googleapis.com/v1/projects/orch-test/messages:send"
    assert first["headers"] == {"Authorization": "Bearer at1"}
    assert first["body"] == {"message": {"token": "tok", "data": {"v": "1"}, "android": {
        "priority": "HIGH", "ttl": "86400s"}}}
    assert second["body"]["message"]["android"] == {
        "priority": "NORMAL", "ttl": "86400s", "collapse_key": "box1:state:w1:p0"}
    net.fcm = [(401, {"error": {"status": "UNAUTHENTICATED"}})]
    google.send("tok", {}, "state", None)
    google.send("tok", {}, "state", None)
    assert len([c for c in net.calls if c["url"] == orch_push.TOKEN_URI]) == 2  # 401: new token


def test_fcm_errors_say_whether_to_retry_drop_the_token_or_the_message():
    def err(status, code, message="", fcm=None):
        details = [{"@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError",
                    "errorCode": fcm}] if fcm else []
        return status, {"error": {"code": status, "status": code, "message": message,
                                  "details": details}}
    assert orch_push.outcome(*err(404, "NOT_FOUND", fcm="UNREGISTERED")) == "token"
    assert orch_push.outcome(*err(403, "PERMISSION_DENIED", fcm="SENDER_ID_MISMATCH")) == "token"
    assert orch_push.outcome(*err(400, "INVALID_ARGUMENT", "The registration token is not a "
                                  "valid FCM registration token", "INVALID_ARGUMENT")) == "token"
    assert orch_push.outcome(*err(400, "INVALID_ARGUMENT", "data too big",
                                  "INVALID_ARGUMENT")) == "drop"
    assert orch_push.outcome(*err(403, "PERMISSION_DENIED", "no permission")) == "drop"
    for status, code in ((429, "RESOURCE_EXHAUSTED"), (500, "INTERNAL"), (503, "UNAVAILABLE"),
                         (401, "UNAUTHENTICATED"), (0, "NETWORK")):
        assert orch_push.outcome(*err(status, code)) == "retry"
    assert orch_push.error_code(*err(404, "NOT_FOUND", "gone", "UNREGISTERED")) == (
        "UNREGISTERED", "gone")


def test_a_missing_or_partial_service_account_is_unavailable(tmp_path):
    with pytest.raises(orch_push.Unavailable, match="no service account key"):
        orch_push.Google.load(tmp_path / "none.json")
    (tmp_path / "sa.json").write_text(json.dumps({"project_id": "p"}))
    with pytest.raises(orch_push.Unavailable, match="client_email"):
        orch_push.Google.load(tmp_path / "sa.json")


def test_fit_trims_in_order_and_says_what_it_cut(crypto):
    big = "x" * 3000
    rnd = random.Random(1)

    def noise(n):  # text that doesn't compress
        return base64.b64encode(bytes(rnd.getrandbits(8) for _ in range(n))).decode()[:n]
    agent = {"pane": "w1:p0", "last": big, "report": {"summary": noise(300)}}
    workers = [{"pane": f"w1:p{i}", "last": noise(700), "report": {"summary": noise(500),
                                                                    "tests": [noise(100)]}}
               for i in range(1, 4)]
    body = {"pane": "w1:p0", "agent": agent, "workers": workers, "report": agent["report"],
            "dialogs": [], "questions": []}
    msg = orch_push.fit("box1", {"seq": 1, "kind": "state", "orchestrator": "w1:p0",
                                 "body": body})
    assert msg["trimmed"][:2] == ["agent_report", "last"]
    assert orch_push.sealed_size("box1", msg) <= orch_push.DATA_LIMIT
    assert body["agent"]["report"]  # the caller's copy is untouched
    small = orch_push.fit("box1", {"seq": 1, "kind": "state", "orchestrator": "w1:p0",
                                   "body": {"pane": "w1:p0"}})
    assert "trimmed" not in small
    huge = orch_push.fit("box1", {"seq": 2, "kind": "system", "orchestrator": None,
                                  "body": {"body": noise(5000)}})
    assert huge["body"] is None and huge["trimmed"] == ["all"]
