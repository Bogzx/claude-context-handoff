#!/usr/bin/env python3
"""One-time cloud setup for orch's pushes and relay (orch-app docs/PUSH.md). Idempotent: every
step looks before it changes anything, so running it again only reports "ok".

    python3 cloud/firebase_setup.py [--project ID] [--dry-run] [--copy-to HOST ...]

The project is --project, else project_info.project_id of the app's google-services.json in
~/.config/orch/. Run it where the Firebase CLI (`npx -y firebase-tools`) is logged in as the
project's owner. It uses that login's own access token (refreshed by a harmless CLI call, read
from ~/.config/configstore/firebase-tools.json, never printed); this file holds no secret and
names no project.

 1. enables the APIs: FCM, Firestore, IAM, Identity Toolkit, Secure Token, Firebase Rules;
 2. creates the (default) Firestore database, Native mode, in --location (default eur3);
 3. enables Anonymous sign-in, through `firebase deploy --only auth` (the CLI's own
    provisioning; the REST call that would do it, initializeAuth, needs billing);
 4. a custom role orchPush with only what a machine needs (send FCM messages; get, list and
    delete Firestore documents), held by the service account orch-push;
 5. one JSON key for it at ~/.config/orch/firebase-sa.json (mode 600), copied the same way to
    every --copy-to host (ssh), never printed;
 6. releases cloud/firestore.rules as the Firestore rules.
"""
import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

GOOGLE_SERVICES = Path.home() / ".config" / "orch" / "google-services.json"
APIS = ["fcm.googleapis.com", "firestore.googleapis.com", "iam.googleapis.com",
        "identitytoolkit.googleapis.com", "securetoken.googleapis.com",
        "firebaserules.googleapis.com"]
ROLE = "orchPush"
PERMISSIONS = sorted(["cloudmessaging.messages.create", "datastore.databases.getMetadata",
                      "datastore.entities.get", "datastore.entities.list",
                      "datastore.entities.delete"])
ACCOUNT = "orch-push"
KEY_FILE = Path.home() / ".config" / "orch" / "firebase-sa.json"
RULES = Path(__file__).resolve().with_name("firestore.rules")
CLI_CONFIG = Path.home() / ".config" / "configstore" / "firebase-tools.json"
FIREBASE = ["npx", "-y", "firebase-tools"]


class Failed(Exception):
    pass


def firebase(*args, cwd=None):
    r = subprocess.run([*FIREBASE, *args], cwd=cwd, capture_output=True, text=True, timeout=300,
                       check=False)
    if r.returncode != 0:
        raise Failed(f"firebase {' '.join(args)} failed: {(r.stderr or r.stdout).strip()[-500:]}")
    return r.stdout


def cli_token():
    """The Firebase CLI's access token, fresh (the CLI refreshes it when it runs)."""
    firebase("projects:list", "--json")
    try:
        return json.loads(CLI_CONFIG.read_text())["tokens"]["access_token"]
    except (OSError, ValueError, KeyError) as e:
        raise Failed(f"no Firebase CLI login in {CLI_CONFIG} ({type(e).__name__}): run "
                     "`npx -y firebase-tools login`") from None


class Api:
    def __init__(self, token, project, patient=True):
        self.token, self.project, self.patient = token, project, patient

    def call(self, method, url, body=None):
        """(HTTP status, JSON). A just-enabled API answers SERVICE_DISABLED for a few minutes:
        a patient Api waits up to 3 minutes for it."""
        for _ in range(18):
            status, out = self.once(method, url, body)
            if not (self.patient and status == 403 and "SERVICE_DISABLED" in json.dumps(out)):
                break
            time.sleep(10)
        return status, out

    def once(self, method, url, body=None):
        req = urllib.request.Request(url, method=method, headers={
            "Authorization": f"Bearer {self.token}", "x-goog-user-project": self.project,
            "Content-Type": "application/json"},
            data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw or b"{}")
            except ValueError:
                return e.code, {"error": {"message": raw[:300].decode(errors="replace")}}

    def need(self, method, url, body=None, ok=(200,)):
        status, out = self.call(method, url, body)
        if status not in ok:
            raise Failed(f"{method} {url.split('?')[0]}: HTTP {status} "
                         f"{(out.get('error') or {}).get('message', out)}")
        return out

    def wait(self, op, base):
        """Poll a long-running operation until it is done."""
        for _ in range(60):
            if op.get("done"):
                if op.get("error"):
                    raise Failed(f"operation {op.get('name')}: {op['error']}")
                return op
            time.sleep(5)
            op = self.need("GET", f"{base}/{op['name']}")
        raise Failed(f"operation {op.get('name')} still running after 5 minutes")


def project_id(given):
    if given:
        return given
    try:
        return json.loads(GOOGLE_SERVICES.read_text())["project_info"]["project_id"]
    except (OSError, ValueError, KeyError, TypeError):
        raise Failed(f"no --project and no project_info.project_id in {GOOGLE_SERVICES}") from None


def absent(status, out, dry):
    """404, or (in a dry run) an API this run would enable first."""
    return status == 404 or (dry and status == 403 and "SERVICE_DISABLED" in json.dumps(out))


def report(state, text):
    print(f"{state:13} {text}", flush=True)


def enable_apis(api, dry):
    out = api.need("GET", f"https://serviceusage.googleapis.com/v1/projects/{api.project}"
                          "/services?filter=state:ENABLED&pageSize=200")
    on = {s["config"]["name"] for s in out.get("services") or []}
    missing = [a for a in APIS if a not in on]
    if not missing:
        return report("ok", "APIs enabled")
    if dry:
        return report("would enable", ", ".join(missing))
    op = api.need("POST", f"https://serviceusage.googleapis.com/v1/projects/{api.project}"
                          "/services:batchEnable", {"serviceIds": missing})
    api.wait(op, "https://serviceusage.googleapis.com/v1")
    report("enabled", ", ".join(missing))


def database(api, dry, location):
    url = f"https://firestore.googleapis.com/v1/projects/{api.project}/databases"
    status, out = api.call("GET", f"{url}/(default)")
    if status == 200:
        if out.get("locationId") != location or out.get("type") != "FIRESTORE_NATIVE":
            raise Failed(f"Firestore (default) exists as {out.get('type')} in "
                         f"{out.get('locationId')}, not FIRESTORE_NATIVE in {location}")
        return report("ok", f"Firestore (default) in {location}")
    if not absent(status, out, dry):
        raise Failed(f"GET Firestore (default): HTTP {status} {out}")
    if dry:
        return report("would create", f"Firestore (default), Native, {location}")
    op = api.need("POST", f"{url}?databaseId=(default)",
                  {"type": "FIRESTORE_NATIVE", "locationId": location})
    api.wait(op, "https://firestore.googleapis.com/v1")
    report("created", f"Firestore (default), Native, {location}")


def anonymous_enabled(api):
    status, out = api.call("GET", "https://identitytoolkit.googleapis.com/admin/v2/projects/"
                                  f"{api.project}/config")
    return status == 200 and ((out.get("signIn") or {}).get("anonymous") or {}).get("enabled")


def anonymous_auth(api, dry):
    if anonymous_enabled(api):
        return report("ok", "Anonymous sign-in enabled")
    if dry:
        return report("would enable", "Anonymous sign-in (firebase deploy --only auth)")
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "firebase.json").write_text(json.dumps({"auth": {"providers": {
            "anonymous": True}}}))
        firebase("deploy", "--only", "auth", "--project", api.project, "--non-interactive",
                 cwd=tmp)
    if not anonymous_enabled(api):
        raise Failed("firebase deploy --only auth ran, but Anonymous sign-in is still off")
    report("enabled", "Anonymous sign-in")


def role(api, dry):
    base = f"https://iam.googleapis.com/v1/projects/{api.project}/roles"
    status, out = api.call("GET", f"{base}/{ROLE}")
    want = {"title": "orch push and relay", "stage": "GA", "includedPermissions": PERMISSIONS,
            "description": "Send FCM messages; get, list and delete Firestore documents "
                           "(orch relay)."}
    if absent(status, out, dry):
        if dry:
            return report("would create", f"role {ROLE}: {', '.join(PERMISSIONS)}")
        api.need("POST", base, {"roleId": ROLE, "role": want})
        return report("created", f"role {ROLE}: {', '.join(PERMISSIONS)}")
    if status != 200:
        raise Failed(f"GET role {ROLE}: HTTP {status} {out}")
    if out.get("deleted"):
        if dry:
            return report("would undelete", f"role {ROLE}")
        out = api.need("POST", f"{base}/{ROLE}:undelete", {"etag": out.get("etag")})
    if sorted(out.get("includedPermissions") or []) != PERMISSIONS:
        if dry:
            return report("would update", f"role {ROLE} permissions to {', '.join(PERMISSIONS)}")
        api.need("PATCH", f"{base}/{ROLE}?updateMask=includedPermissions",
                 {"includedPermissions": PERMISSIONS, "etag": out.get("etag")})
        return report("updated", f"role {ROLE}: {', '.join(PERMISSIONS)}")
    report("ok", f"role {ROLE}")


def email(project):
    return f"{ACCOUNT}@{project}.iam.gserviceaccount.com"


def service_account(api, dry):
    base = f"https://iam.googleapis.com/v1/projects/{api.project}/serviceAccounts"
    status, out = api.call("GET", f"{base}/{email(api.project)}")
    if status == 200:
        return report("ok", f"service account {email(api.project)}")
    if not absent(status, out, dry):
        raise Failed(f"GET service account: HTTP {status} {out}")
    if dry:
        return report("would create", f"service account {email(api.project)}")
    api.need("POST", base, {"accountId": ACCOUNT, "serviceAccount": {
        "displayName": "orch push and relay",
        "description": "orch serve: FCM pushes and the relay mailbox"}})
    report("created", f"service account {email(api.project)}")


def binding(api, dry):
    url = f"https://cloudresourcemanager.googleapis.com/v1/projects/{api.project}"
    member, name = f"serviceAccount:{email(api.project)}", f"projects/{api.project}/roles/{ROLE}"
    for attempt in range(6):  # a new service account takes a moment to be visible to IAM
        policy = api.need("POST", f"{url}:getIamPolicy",
                          {"options": {"requestedPolicyVersion": 3}})
        found = next((b for b in policy.get("bindings") or []
                      if b.get("role") == name and not b.get("condition")), None)
        if found and member in found.get("members", []):
            return report("ok", f"{ACCOUNT} holds {ROLE}")
        if dry:
            return report("would grant", f"{ROLE} to {ACCOUNT}")
        if found:
            found["members"].append(member)
        else:
            policy.setdefault("bindings", []).append({"role": name, "members": [member]})
        policy["version"] = 3
        status, out = api.call("POST", f"{url}:setIamPolicy", {"policy": policy})
        if status == 200:
            return report("granted", f"{ROLE} to {ACCOUNT}")
        if attempt == 5 or status not in (400, 409):
            raise Failed(f"setIamPolicy: HTTP {status} {(out.get('error') or {}).get('message')}")
        time.sleep(10)


def write_private(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def key(api, dry):
    base = (f"https://iam.googleapis.com/v1/projects/{api.project}/serviceAccounts/"
            f"{email(api.project)}/keys")
    status, out = api.call("GET", f"{base}?keyTypes=USER_MANAGED")
    keys = {k["name"].rsplit("/", 1)[-1] for k in out.get("keys") or []} if status == 200 else set()
    try:
        mine = json.loads(KEY_FILE.read_text()).get("private_key_id")
    except (OSError, ValueError):
        mine = None
    others = len(keys - {mine})
    note = f" ({others} other user-managed key{'s' if others != 1 else ''})" if others else ""
    if mine and mine in keys:
        return report("ok", f"key {mine[:8]}… at {KEY_FILE}{note}")
    if dry:
        return report("would create", f"a key at {KEY_FILE} (mode 600){note}")
    out = api.need("POST", base, {"privateKeyType": "TYPE_GOOGLE_CREDENTIALS_FILE",
                                  "keyAlgorithm": "KEY_ALG_RSA_2048"})
    write_private(KEY_FILE, base64.b64decode(out["privateKeyData"]))
    report("created", f"key {out['name'].rsplit('/', 1)[-1][:8]}… at {KEY_FILE}{note}")


def rules(api, dry):
    base = f"https://firebaserules.googleapis.com/v1/projects/{api.project}"
    release = f"projects/{api.project}/releases/cloud.firestore"
    source = RULES.read_text()
    status, out = api.call("GET", f"https://firebaserules.googleapis.com/v1/{release}")
    if status == 200:
        current = api.need("GET", f"https://firebaserules.googleapis.com/v1/{out['rulesetName']}")
        files = (current.get("source") or {}).get("files") or []
        if [f.get("content") for f in files] == [source]:
            return report("ok", f"Firestore rules = {RULES.name}")
    elif status != 404:
        raise Failed(f"GET release cloud.firestore: HTTP {status} {out}")
    if dry:
        return report("would release", f"{RULES.name} as the Firestore rules")
    ruleset = api.need("POST", f"{base}/rulesets", {"source": {"files": [
        {"name": "firestore.rules", "content": source}]}})["name"]
    if status == 200:
        api.need("PATCH", f"https://firebaserules.googleapis.com/v1/{release}",
                 {"release": {"name": release, "rulesetName": ruleset}})
    else:
        api.need("POST", f"{base}/releases", {"name": release, "rulesetName": ruleset})
    report("released", f"{RULES.name} ({ruleset.rsplit('/', 1)[-1]})")


def copy_key(hosts, dry):
    data = KEY_FILE.read_bytes() if KEY_FILE.exists() else None
    for host in hosts:
        if data is None and dry:
            report("would copy", f"the new key to {host}:~/.config/orch/firebase-sa.json (600)")
            continue
        if data is None:
            raise Failed(f"no key at {KEY_FILE} to copy")
        remote = subprocess.run(["ssh", host, "sha256sum .config/orch/firebase-sa.json"],
                                capture_output=True, text=True, timeout=60, check=False)
        if remote.stdout.split()[:1] == [hashlib.sha256(data).hexdigest()]:
            report("ok", f"{host}: the same key")
            continue
        if dry:
            report("would copy", f"the key to {host}:~/.config/orch/firebase-sa.json (600)")
            continue
        install = ("umask 077 && mkdir -p .config/orch && "
                   "cat > .config/orch/.firebase-sa.json.tmp && "
                   "mv .config/orch/.firebase-sa.json.tmp .config/orch/firebase-sa.json")
        r = subprocess.run(["ssh", host, install], input=data, capture_output=True, timeout=60,
                           check=False)
        if r.returncode != 0:
            raise Failed(f"copy to {host}: {r.stderr.decode(errors='replace').strip()[-300:]}")
        report("copied", f"the key to {host}:~/.config/orch/firebase-sa.json (600)")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--project", help=f"Firebase project id (default: from {GOOGLE_SERVICES})")
    p.add_argument("--location", default="eur3", help="Firestore location (default eur3)")
    p.add_argument("--dry-run", action="store_true", help="look only; print what would change")
    p.add_argument("--copy-to", nargs="*", default=[], metavar="HOST",
                   help="ssh hosts that get the same key, e.g. host2 user@host3")
    args = p.parse_args(argv)
    try:
        api = Api(cli_token(), project_id(args.project), patient=not args.dry_run)
        enable_apis(api, args.dry_run)
        database(api, args.dry_run, args.location)
        for step in (anonymous_auth, role, service_account, binding, key, rules):
            step(api, args.dry_run)
        copy_key(args.copy_to, args.dry_run)
    except Failed as e:
        print(f"firebase_setup: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
