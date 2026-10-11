"""Local, review-only editorial pass over the saved Reddit batch."""

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .ai_client import AIUnavailable, generate_json
from .ai_prompts import CONTENT_PROMPT, REPLY_PROMPT
from .xlsx_writer import write_xlsx


IST = ZoneInfo("Asia/Kolkata")
PACIFIC = ZoneInfo("America/Los_Angeles")
REPLY_HEADERS = ["Reviewed IST", "Post ID", "Subreddit", "Post title", "Post link", "Feed placement",
                 "Saved comments", "Comment coverage", "OP asks", "Reply draft", "Why it adds value",
                 "Facts to check", "Team status", "Source generated UTC"]
SKIP_HEADERS = ["Reviewed IST", "Post ID", "Post title", "Post link", "Reason", "Source generated UTC"]
IDEA_HEADERS = ["Date IST", "Angle key", "Title or hook", "Source post IDs", "Source links",
                "Editorial lens", "Distinct thesis", "Why now", "Comment comparison / limits",
                "Five gates (0–2)", "Format", "Outline", "Video script", "Language", "Facts to check",
                "Team status", "Source generated UTC"]
AUDIT_HEADERS = ["Run IST", "Run type", "Source generated UTC", "Candidates examined", "Outcome / limits"]


def _obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _str():
    return {"type": "string"}


def _arr(item):
    return {"type": "array", "items": item}


REPLY_SCHEMA = _obj({"decisions": _arr(_obj({
    "post_id": _str(), "decision": {"type": "string", "enum": ["draft", "skip"]},
    "op_ask": _str(), "op_evidence": _str(), "comment_coverage": _str(),
    "reply": _str(), "why": _str(), "checks": _str(), "reason": _str(),
}))})
SCORES_SCHEMA = _obj({k: {"type": "integer", "minimum": 0, "maximum": 2}
                      for k in ("demand", "consequence", "evidence", "insight", "hook")})
CONTENT_SCHEMA = _obj({"ideas": _arr(_obj({
    "angle_key": _str(), "source_post_ids": _arr(_str()), "title": _str(),
    "lens": _str(), "thesis": _str(), "why_now": _str(), "comments": _str(),
    "scores": SCORES_SCHEMA, "hook": _str(), "format": _str(), "outline": _str(),
    "video_script": _str(), "language": _str(), "facts_to_check": _str(),
})), "rejected": _arr(_str())})


def _atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temp.replace(path)


def _load_state(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"requests": {}, "reply_fingerprints": {}, "reply_drafts": {}, "reply_skips": {},
                "content_dates": [], "ideas": {}, "audit": []}


def _clip(value, limit):
    return (value or "")[:limit]


def _post_excerpt(row, with_comments=True):
    result = {k: row.get(k) for k in ("post_id", "subreddit", "title", "url", "published_utc",
                                      "feed", "rank", "change", "classification", "topic",
                                      "engagement", "comments_status", "comment_count_collected")}
    result["description"] = _clip(row.get("description"), 2500)
    result["comments"] = [{"id": c.get("id"), "body": _clip(c.get("body"), 650), "url": c.get("url")}
                          for c in (row.get("comments_collected") or [])[:10]] if with_comments else []
    return result


def _reply_candidates(batch, state):
    rows = []
    seen = set()
    for row in batch.get("reply_shortlist", []) + batch.get("new_discovery_since_last_cycle", [])[:25]:
        pid = row.get("post_id")
        if not pid or pid in seen:
            continue
        seen.add(pid)
        excerpt = _post_excerpt(row)
        digest = hashlib.sha256(json.dumps(excerpt, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if state["reply_fingerprints"].get(pid) != digest and pid not in state["reply_drafts"]:
            rows.append((excerpt, digest))
    return rows[:25]


def _incomplete_image_allocation(post):
    title = (post.get("title") or "").lower()
    if not re.search(r"(?:fund|portfolio|sip).*(?:review|allocation)|(?:review|allocation).*(?:fund|portfolio|sip)", title):
        return False
    text = post.get("description") or ""
    numeric = re.findall(r"(?:₹|rs\.?\s*)\s*\d|\d+(?:\.\d+)?\s*%", text, flags=re.I)
    return len(numeric) < 2


def _validate_replies(result, candidates):
    known = {row["post_id"]: row for row, _ in candidates}
    drafts, skips = [], []
    used = set()
    for item in result.get("decisions", []):
        if not isinstance(item, dict):
            continue
        pid = item.get("post_id")
        if pid not in known or pid in used:
            continue
        used.add(pid)
        post = known[pid]
        reply = (item.get("reply") or "").strip()
        reason = (item.get("reason") or "").strip()
        if _incomplete_image_allocation(post):
            reason = "Key allocation details absent from captured OP text/image unreadable"
            reply = ""
        if item.get("decision") != "draft" or not reply or len(reply.split()) > 140 or not item.get("op_ask") or not item.get("op_evidence"):
            skips.append({"post": post, "reason": reason or "No grounded, direct reply"})
            continue
        if re.search(r"(?:disclosure:|one-page version|shared risk boundary)", reply, re.I):
            skips.append({"post": post, "reason": "Template or irrelevant reply failed local quality gate"})
            continue
        drafts.append({"post": post, "item": item})
    for post, _ in candidates:
        if post["post_id"] not in used:
            skips.append({"post": post, "reason": "Model did not decide on this post"})
    return drafts[:5], skips


def _validate_ideas(result, batch, existing_keys):
    known = {r.get("post_id"): r for r in batch.get("content_pool", [])
             + batch.get("recent_post_history_7d", []) if r.get("post_id")}
    out = []
    for item in result.get("ideas", [])[:4]:
        if not isinstance(item, dict):
            continue
        key = (item.get("angle_key") or "").strip().lower()
        ids = item.get("source_post_ids") or []
        scores = item.get("scores") or {}
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{3,79}", key) or key in existing_keys:
            continue
        if not ids or any(pid not in known for pid in ids):
            continue
        if not all(isinstance(scores.get(k), int) and 0 <= scores[k] <= 2 for k in
                   ("demand", "consequence", "evidence", "insight", "hook")):
            continue
        if scores["evidence"] == 0 or scores["insight"] == 0 or not item.get("thesis") or not item.get("hook"):
            continue
        if not out and not (item.get("video_script") or "").strip():
            continue
        out.append({"item": item, "key": key, "links": [known[pid]["url"] for pid in ids]})
        existing_keys.add(key)
    return out


def _workbooks(output_dir, state):
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    drafts = []
    for d in state["reply_drafts"].values():
        p, i = d["post"], d["item"]
        drafts.append([d["time_ist"], p["post_id"], p["subreddit"], p["title"], p["url"],
                       "%s / rank %s" % (p.get("feed"), p.get("rank")), p.get("comment_count_collected"),
                       i.get("comment_coverage"), i.get("op_ask"), i.get("reply"), i.get("why"),
                       i.get("checks"), "Needs review", d["source"]])
    skips = [[s["time_ist"], s["post"]["post_id"], s["post"]["title"], s["post"]["url"],
              s["reason"], s["source"]] for s in state["reply_skips"].values()]
    reply_path = write_xlsx(output / "AI-Reply-Review-Latest.xlsx", [
        {"name": "Drafts", "headers": REPLY_HEADERS, "rows": drafts, "hyperlink_col": 5},
        {"name": "Skipped", "headers": SKIP_HEADERS, "rows": skips, "hyperlink_col": 4},
        {"name": "Run Log", "headers": AUDIT_HEADERS,
         "rows": [[a.get(k, "") for k in ("time_ist", "kind", "source", "examined", "outcome")]
                  for a in state["audit"] if a["kind"] == "replies"]},
    ])
    ideas = []
    for entry in state["ideas"].values():
        i = entry["item"]
        scores = ", ".join("%s=%s" % (k, v) for k, v in i["scores"].items())
        ideas.append([entry["time_ist"], entry["key"], i.get("title"), ", ".join(i["source_post_ids"]),
                      "\n".join(entry["links"]), i.get("lens"), i.get("thesis"), i.get("why_now"),
                      i.get("comments"), scores, i.get("format"), i.get("outline"), i.get("video_script"),
                      i.get("language"), i.get("facts_to_check"), "Needs review", entry["source"]])
    content_path = write_xlsx(output / "AI-Content-Ideas-Latest.xlsx", [
        {"name": "Ideas", "headers": IDEA_HEADERS, "rows": ideas},
        {"name": "Audit", "headers": AUDIT_HEADERS,
         "rows": [[a.get(k, "") for k in ("time_ist", "kind", "source", "examined", "outcome")]
                  for a in state["audit"] if a["kind"] == "content"]},
    ])
    return {"replies": reply_path, "content": content_path}


def run_ai(cfg, kind="both", generator=generate_json, now=None):
    """One call per eligible mode; write local review files only after validation."""
    model = (cfg.get("ai_model") or "").strip()
    if not model:
        raise ValueError("Set ai_model to a model ID supported by your provider")
    now = now or datetime.now(timezone.utc)
    runtime = Path(cfg["runtime_dir"])
    state_path = runtime / "ai_state.json"
    state = _load_state(state_path)
    output_base = Path(cfg["output_dir"])
    batch_path = output_base / ".data" / "Reddit-Work-Batch-Latest.json"
    if not batch_path.is_file():
        batch_path = output_base / "Reddit-Work-Batch-Latest.json"
    batch = json.loads(batch_path.read_text(encoding="utf-8"))
    source = batch["generated_at_utc"]
    source_time = datetime.fromisoformat(source.replace("Z", "+00:00"))
    if now - source_time > __import__("datetime").timedelta(hours=5):
        return {"status": "stale_input", "source": source}
    key_file = cfg.get("ai_key_file") or runtime / "ai_api_key"
    provider = cfg.get("ai_provider", "gemini")
    pacific_day = now.astimezone(PACIFIC).date().isoformat()
    max_calls = int(cfg.get("ai_max_requests_per_day", 12))
    results = []
    for mode in (["replies", "content"] if kind == "both" else [kind]):
        if mode == "content" and now.astimezone(IST).hour < int(cfg.get("ai_content_hour_ist", 21)):
            continue
        if mode == "content" and batch["report_date_local"] in state["content_dates"]:
            continue
        candidates = _reply_candidates(batch, state) if mode == "replies" else None
        if mode == "replies" and not candidates:
            continue
        if state["requests"].get(pacific_day, 0) >= max_calls:
            results.append("%s: local call cap reached" % mode)
            continue
        if mode == "replies":
            payload = {"source_utc": source, "posts": [r for r, _ in candidates]}
            prompt_file = cfg.get("ai_reply_prompt_file")
            instructions = Path(prompt_file).expanduser().read_text(encoding="utf-8") if prompt_file else REPLY_PROMPT
            prompt = instructions + "\n\nReturn JSON for every supplied post. INPUT DATA:\n" + json.dumps(payload, ensure_ascii=False)
            schema = REPLY_SCHEMA
        else:
            pool = [_post_excerpt(r, with_comments=True) for r in batch.get("content_pool", [])[:25]]
            payload = {"source_utc": source, "posts": pool,
                       "selected_history_7d": batch.get("recent_post_history_7d", [])[:60],
                       "existing_angle_keys": list(state["ideas"])[:100]}
            prompt_file = cfg.get("ai_content_prompt_file")
            instructions = Path(prompt_file).expanduser().read_text(encoding="utf-8") if prompt_file else CONTENT_PROMPT
            prompt = instructions + "\n\nINPUT DATA:\n" + json.dumps(payload, ensure_ascii=False)
            schema = CONTENT_SCHEMA
        state["requests"][pacific_day] = state["requests"].get(pacific_day, 0) + 1
        _atomic_json(state_path, state)
        try:
            response = generator(provider, model, key_file, prompt, schema,
                                 thinking="medium" if mode == "replies" else "high")
        except AIUnavailable as exc:
            results.append("%s: %s" % (mode, exc))
            continue
        timestamp = now.astimezone(IST).strftime("%d-%m-%y %H:%M IST")
        if mode == "replies":
            drafts, skips = _validate_replies(response, candidates)
            for post, digest in candidates:
                state["reply_fingerprints"][post["post_id"]] = digest
            for d in drafts:
                state["reply_drafts"][d["post"]["post_id"]] = dict(d, time_ist=timestamp, source=source)
            for s in skips:
                state["reply_skips"][s["post"]["post_id"]] = dict(s, time_ist=timestamp, source=source)
            outcome = "%d drafts, %d skips; saved comment samples may be incomplete" % (len(drafts), len(skips))
            examined = len(candidates)
        else:
            ideas = _validate_ideas(response, batch, set(state["ideas"]))
            for idea in ideas:
                state["ideas"][idea["key"]] = dict(idea, time_ist=timestamp, source=source)
            state["content_dates"].append(batch["report_date_local"])
            outcome = "%d ideas; market trends not independently verified by local AI run" % len(ideas)
            examined = len(payload["posts"])
        state["audit"].append({"time_ist": timestamp, "kind": mode, "source": source,
                               "examined": examined, "outcome": outcome})
        _atomic_json(state_path, state)
        results.append("%s: %s" % (mode, outcome))
    paths = _workbooks(cfg["output_dir"], state)
    return {"status": "complete" if results else "no_new_input", "results": results,
            "files": {k: str(v) for k, v in paths.items()}, "source": source}
