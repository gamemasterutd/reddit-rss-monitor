"""Upload the one Work handoff file with a narrow Google Drive OAuth grant.

Uses Python's standard library. Tokens and the desktop OAuth client stay in the
monitor's private runtime directory; no Drive desktop application is needed.
"""

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


SCOPE = "https://www.googleapis.com/auth/drive.file"
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
DRIVE_URL = "https://www.googleapis.com/drive/v3/files"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
FOLDER_NAME = "Reddit RSS Monitor"
FILE_NAME = "Reddit-Work-Batch-Latest.txt"


class DriveError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def _secure_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with os.fdopen(os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as out:
        json.dump(value, out, indent=2)
        out.write("\n")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _load_client(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    client = data.get("installed") or {}
    if not client.get("client_id") or not client.get("client_secret"):
        raise DriveError("Use a Google OAuth Desktop application credentials JSON file")
    return {"client_id": client["client_id"], "client_secret": client["client_secret"]}


def _json_request(url, method="GET", token=None, payload=None, content_type="application/json"):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if payload is not None:
        headers["Content-Type"] = content_type
        if isinstance(payload, dict):
            payload = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=payload, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            raw = response.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as exc:
        # Never echo OAuth tokens or full API error bodies into monitor logs.
        raise DriveError("Google request failed (HTTP %s)" % exc.code, status=exc.code) from exc
    except urllib.error.URLError as exc:
        raise DriveError("Google request could not connect: %s" % exc.reason) from exc


def _form_request(url, values):
    payload = urllib.parse.urlencode(values).encode("utf-8")
    return _json_request(url, "POST", payload=payload,
                         content_type="application/x-www-form-urlencoded")


def _authorize_in_browser(client):
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    received = {}

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self):
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if self.path.startswith("/?") and params.get("state", [None])[0] == state:
                received["code"] = params.get("code", [None])[0]
                received["error"] = params.get("error", [None])[0]
                message = b"Authorization received. You can return to the Reddit monitor."
            else:
                message = b"Google authorization callback was not recognized."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(message)

        def log_message(self, format, *args):
            pass

    with HTTPServer(("127.0.0.1", 0), Callback) as server:
        server.timeout = 5
        redirect = "http://127.0.0.1:%d/" % server.server_port
        params = {
            "client_id": client["client_id"], "redirect_uri": redirect,
            "response_type": "code", "scope": SCOPE, "access_type": "offline",
            "prompt": "consent", "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256",
        }
        url = AUTH_URL + "?" + urllib.parse.urlencode(params)
        print("Open this Google sign-in link in your work-account browser:\n%s" % url, flush=True)
        webbrowser.open(url)
        print("Waiting up to three minutes for the one-time Google authorization...", flush=True)
        deadline = time.monotonic() + 180
        while "code" not in received and "error" not in received and time.monotonic() < deadline:
            server.handle_request()
    if received.get("error"):
        raise DriveError("Google authorization was not granted: %s" % received["error"])
    if not received.get("code"):
        raise DriveError("Google authorization timed out; run setup again")
    token = _form_request(TOKEN_URL, {
        "code": received["code"], "client_id": client["client_id"],
        "client_secret": client["client_secret"], "code_verifier": verifier,
        "redirect_uri": redirect, "grant_type": "authorization_code",
    })
    if not token.get("refresh_token"):
        raise DriveError("Google did not issue an offline refresh token; check the OAuth client setup")
    token["expires_at"] = time.time() + int(token.get("expires_in", 3600)) - 60
    return token


def connect_drive(client_json, runtime_dir):
    runtime = Path(runtime_dir)
    client = _load_client(client_json)
    saved_client = runtime / "google-drive-client.json"
    saved_state = runtime / "google-drive-state.json"
    if saved_client.is_file() and saved_state.is_file():
        try:
            prior = _load_client(saved_client)
            state = json.loads(saved_state.read_text(encoding="utf-8"))
            if prior["client_id"] == client["client_id"] and state.get("token", {}).get("refresh_token"):
                return DriveUploader(runtime)
        except (OSError, ValueError, DriveError):
            pass
    token = _authorize_in_browser(client)
    _secure_json(saved_client, {"installed": client})
    _secure_json(saved_state, {"token": token})
    return DriveUploader(runtime)


def _multipart(metadata, contents):
    boundary = "redditrss_" + secrets.token_hex(12)
    body = ("--%s\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n%s\r\n"
            "--%s\r\nContent-Type: text/plain; charset=UTF-8\r\n\r\n" %
            (boundary, json.dumps(metadata), boundary)).encode("utf-8")
    return "multipart/related; boundary=%s" % boundary, body + contents + ("\r\n--%s--" % boundary).encode("ascii")


class DriveUploader:
    def __init__(self, runtime_dir):
        self.runtime = Path(runtime_dir)
        self.client = _load_client(self.runtime / "google-drive-client.json")
        self.state_path = self.runtime / "google-drive-state.json"
        self.state = json.loads(self.state_path.read_text(encoding="utf-8"))

    def _save(self):
        _secure_json(self.state_path, self.state)

    def _token(self):
        token = self.state["token"]
        if token.get("access_token") and time.time() < token.get("expires_at", 0):
            return token["access_token"]
        refreshed = _form_request(TOKEN_URL, {
            "client_id": self.client["client_id"], "client_secret": self.client["client_secret"],
            "refresh_token": token["refresh_token"], "grant_type": "refresh_token",
        })
        token["access_token"] = refreshed["access_token"]
        token["expires_at"] = time.time() + int(refreshed.get("expires_in", 3600)) - 60
        self._save()
        return token["access_token"]

    def _api(self, url, method="GET", payload=None, content_type="application/json"):
        return _json_request(url, method, token=self._token(), payload=payload, content_type=content_type)

    def upload(self, path):
        source = Path(path)
        if not source.is_file() or source.name != FILE_NAME:
            raise DriveError("Expected the local %s file" % FILE_NAME)
        contents = source.read_bytes()
        digest = hashlib.sha256(contents).hexdigest()
        if self.state.get("file_id") and digest == self.state.get("sha256"):
            return {"file_id": self.state["file_id"], "unchanged": True,
                    "url": "https://drive.google.com/file/d/%s/view" % self.state["file_id"]}

        folder_id = self.state.get("folder_id")
        if not folder_id:
            created = self._api(DRIVE_URL + "?fields=id", "POST", {
                "name": FOLDER_NAME, "mimeType": "application/vnd.google-apps.folder",
            })
            folder_id = created["id"]
            self.state["folder_id"] = folder_id
            self._save()

        file_id = self.state.get("file_id")
        if file_id:
            result = self._api(UPLOAD_URL + "/" + urllib.parse.quote(file_id) + "?uploadType=media&fields=id",
                               "PATCH", contents, "text/plain; charset=UTF-8")
        else:
            content_type, body = _multipart({"name": FILE_NAME, "mimeType": "text/plain", "parents": [folder_id]},
                                            contents)
            result = self._api(UPLOAD_URL + "?uploadType=multipart&fields=id", "POST", body, content_type)
        self.state["file_id"] = result["id"]
        self.state["sha256"] = digest
        self._save()
        return {"file_id": result["id"], "unchanged": False,
                "url": "https://drive.google.com/file/d/%s/view" % result["id"]}
