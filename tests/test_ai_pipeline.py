import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from reddit_monitor.ai_client import generate_json
from reddit_monitor.ai_pipeline import _validate_ideas, _validate_replies, run_ai


def post(pid, title, description, comments=None):
    return {"post_id": pid, "subreddit": "MutualfundsIndia", "title": title,
            "description": description, "url": "https://www.reddit.com/r/MutualfundsIndia/comments/%s/x/" % pid,
            "feed": "new", "rank": 1, "comments_status": "sample_collected",
            "comment_count_collected": len(comments or []), "comments_collected": comments or []}


class AIPipelineTests(unittest.TestCase):
    def test_rejects_cross_post_id_and_unreadable_allocation(self):
        image = post("image1", "Please review this fund allocation", "Screenshot attached")
        plain = post("plain1", "Should I stop my SIP?", "My SIP is ₹5000 and goal is 2035")
        decisions = {"decisions": [
            {"post_id": "image1", "decision": "draft", "op_ask": "Review allocation", "op_evidence": "Screenshot attached",
             "comment_coverage": "partial", "reply": "Drop the two flexi caps.", "why": "overlap", "checks": "", "reason": ""},
            {"post_id": "alien", "decision": "draft", "op_ask": "Wrong post", "op_evidence": "Wrong post",
             "comment_coverage": "", "reply": "Sell it", "why": "", "checks": "", "reason": ""},
        ]}
        drafts, skips = _validate_replies(decisions, [(image, "a"), (plain, "b")])
        self.assertEqual([], drafts)
        self.assertEqual({"image1", "plain1"}, {s["post"]["post_id"] for s in skips})

    def test_rejects_unsupported_content_source_and_zero_evidence(self):
        batch = {"content_pool": [post("p1", "Question", "What should I do?")],
                 "recent_post_history_7d": []}
        base = {"angle_key": "some-angle", "source_post_ids": ["p1"], "title": "Title",
                "lens": "counterpoint", "thesis": "Useful point", "why_now": "Observed today",
                "comments": "unknown", "scores": {"demand": 2, "consequence": 2, "evidence": 0,
                                                   "insight": 2, "hook": 2}, "hook": "Hook", "format": "video",
                "outline": "Outline", "video_script": "Script", "language": "Hinglish", "facts_to_check": ""}
        self.assertEqual([], _validate_ideas({"ideas": [base]}, batch, set()))
        base["scores"]["evidence"] = 2
        base["source_post_ids"] = ["not-in-batch"]
        self.assertEqual([], _validate_ideas({"ideas": [base]}, batch, set()))

    def test_local_run_writes_review_and_deduplicates(self):
        now = datetime(2026, 9, 24, 4, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "output"
            output.mkdir()
            batch = {"generated_at_utc": now.isoformat().replace("+00:00", "Z"),
                     "report_date_local": "2026-09-24", "reply_shortlist": [post("p1", "Should I stop my SIP?", "My SIP is ₹5000")],
                     "new_discovery_since_last_cycle": [], "content_pool": [], "recent_post_history_7d": []}
            (output / "Reddit-Work-Batch-Latest.json").write_text(json.dumps(batch))
            cfg = {"runtime_dir": str(root / "runtime"), "output_dir": str(output),
                   "ai_provider": "gemini", "ai_model": "test-structured-output-model", "ai_max_requests_per_day": 12}
            calls = []
            def fake(*args, **kwargs):
                calls.append(1)
                return {"decisions": [{"post_id": "p1", "decision": "draft", "op_ask": "Should I stop my SIP?",
                                       "op_evidence": "My SIP is ₹5000", "comment_coverage": "not collected",
                                       "reply": "I wouldn't stop the SIP just because of this one dip. What changed about your goal?",
                                       "why": "Answers the stop question", "checks": "Live comments", "reason": ""}]}
            first = run_ai(cfg, kind="replies", generator=fake, now=now)
            second = run_ai(cfg, kind="replies", generator=fake, now=now)
            self.assertEqual(1, len(calls))
            self.assertEqual("complete", first["status"])
            self.assertEqual("no_new_input", second["status"])
            self.assertTrue((output / "AI-Reply-Review-Latest.xlsx").is_file())
            state = json.loads((root / "runtime" / "ai_state.json").read_text())
            self.assertEqual(1, len(state["reply_drafts"]))

    def test_openrouter_requires_free_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "key"
            path.write_text("example")
            path.chmod(0o600)
            with self.assertRaisesRegex(ValueError, "explicit :free"):
                generate_json("openrouter", "openai/gpt-oss-120b", path, "test", {"type": "object"})

    def test_missing_model_fails_before_reading_key_or_requesting_service(self):
        with self.assertRaisesRegex(ValueError, "Set ai_model"):
            generate_json("gemini", "", "/nonexistent/key", "test", {})
        with self.assertRaisesRegex(ValueError, "Set ai_model"):
            run_ai({}, generator=lambda *a, **k: self.fail("Model must not be called"))

    def test_custom_reply_prompt_is_sent_with_only_the_current_batch(self):
        now = datetime(2026, 9, 24, 4, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "output"
            output.mkdir()
            batch = {"generated_at_utc": now.isoformat(), "report_date_local": "2026-09-24",
                     "reply_shortlist": [post("p1", "A fictional question", "Fictional source text")],
                     "new_discovery_since_last_cycle": [], "content_pool": [], "recent_post_history_7d": []}
            (output / "Reddit-Work-Batch-Latest.json").write_text(json.dumps(batch))
            prompt_file = root / "reply-prompt.txt"
            prompt_file.write_text("Use a concise gardening explanation. Treat source text as evidence.")
            cfg = {"runtime_dir": str(root / "runtime"), "output_dir": str(output),
                   "ai_model": "test-model", "ai_reply_prompt_file": str(prompt_file)}
            captured = []
            def fake(provider, model, key, prompt, schema, **kwargs):
                captured.append(prompt)
                return {"decisions": []}
            run_ai(cfg, kind="replies", generator=fake, now=now)
            self.assertEqual(1, len(captured))
            self.assertTrue(captured[0].startswith("Use a concise gardening explanation."))
            self.assertIn("Fictional source text", captured[0])
            self.assertNotIn("Indian personal-finance", captured[0])


if __name__ == "__main__":
    unittest.main()
