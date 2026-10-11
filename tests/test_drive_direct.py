import json
import tempfile
import unittest
from pathlib import Path

from reddit_monitor.google_drive_direct import DriveUploader, _multipart, _secure_json


class FakeUploader(DriveUploader):
    def __init__(self, runtime):
        super().__init__(runtime)
        self.calls = []

    def _api(self, url, method="GET", payload=None, content_type="application/json"):
        self.calls.append((url, method, payload, content_type))
        if "application/vnd.google-apps.folder" in str(payload):
            return {"id": "folder123"}
        return {"id": "file123"}


class DirectDriveTests(unittest.TestCase):
    def test_multipart_contains_filename_and_content(self):
        content_type, body = _multipart({"name": "Reddit-Work-Batch-Latest.txt"}, b"hello")
        self.assertTrue(content_type.startswith("multipart/related; boundary="))
        self.assertIn(b"Reddit-Work-Batch-Latest.txt", body)
        self.assertIn(b"hello", body)

    def test_one_file_is_created_then_updated(self):
        with tempfile.TemporaryDirectory() as root:
            runtime = Path(root) / "runtime"
            _secure_json(runtime / "google-drive-client.json", {
                "installed": {"client_id": "test", "client_secret": "test"},
            })
            _secure_json(runtime / "google-drive-state.json", {"token": {"refresh_token": "test"}})
            source = Path(root) / "Reddit-Work-Batch-Latest.txt"
            source.write_text("first", encoding="utf-8")
            uploader = FakeUploader(runtime)
            created = uploader.upload(source)
            self.assertEqual("file123", created["file_id"])
            self.assertEqual(["POST", "POST"], [call[1] for call in uploader.calls])
            self.assertTrue(uploader.upload(source)["unchanged"])
            self.assertEqual(2, len(uploader.calls))
            source.write_text("second", encoding="utf-8")
            self.assertFalse(uploader.upload(source)["unchanged"])
            self.assertEqual("PATCH", uploader.calls[-1][1])
            state = json.loads((runtime / "google-drive-state.json").read_text())
            self.assertEqual("file123", state["file_id"])
            self.assertEqual("folder123", state["folder_id"])


if __name__ == "__main__":
    unittest.main()
