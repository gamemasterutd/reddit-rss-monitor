import json
import re
import unicodedata
from pathlib import Path

from .models import FilterResult

class FilterProfileError(ValueError):
    pass

def _norm(text):
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = text.replace("’", "'")
    return re.sub(r"\s+", " ", text).strip()

def _phrase_pattern(phrase):
    phrase = _norm(phrase)
    escaped = re.escape(phrase).replace(r"\ ", r"\s+")
    left = r"(?<![a-z0-9_])" if phrase and phrase[0].isalnum() else ""
    right = r"(?![a-z0-9_])" if phrase and phrase[-1].isalnum() else ""
    return re.compile(left + escaped + right, re.IGNORECASE)

def phrase_matches(text, phrase):
    return bool(_phrase_pattern(phrase).search(_norm(text)))

class RelevanceEngine:
    def __init__(self, profile):
        self.profile = self._validate(profile)
        self.weights = self.profile["weights"]
        self.thresholds = self.profile["thresholds"]
        self._compiled = {}

    @classmethod
    def from_file(cls, path):
        try:
            profile = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FilterProfileError("Cannot load filter profile: %s" % exc)
        return cls(profile)

    def _validate(self, p):
        if not isinstance(p, dict):
            raise FilterProfileError("Profile must be a JSON object")
        for key in ("weights", "thresholds", "categories", "decision_intent_phrases", "personal_context_phrases", "noise_phrases", "hard_exclusion_phrases", "review_triggers"):
            if key not in p:
                raise FilterProfileError("Profile missing key: %s" % key)
        if not isinstance(p["categories"], list) or not p["categories"]:
            raise FilterProfileError("categories must be a non-empty list")
        names = set()
        for cat in p["categories"]:
            if not isinstance(cat, dict) or not cat.get("name"):
                raise FilterProfileError("Each category requires a name")
            if cat["name"] in names:
                raise FilterProfileError("Duplicate category: %s" % cat["name"])
            names.add(cat["name"])
            cat.setdefault("strong_phrases", [])
            cat.setdefault("supporting_phrases", [])
        return p

    def _matches(self, text, phrases):
        found = []
        for phrase in phrases:
            key = phrase
            pat = self._compiled.get(key)
            if pat is None:
                pat = _phrase_pattern(phrase)
                self._compiled[key] = pat
            if pat.search(text):
                found.append(phrase)
        return found

    def classify(self, title, description):
        text = _norm("%s\n%s" % (title or "", description or ""))
        hard = self._matches(text, self.profile["hard_exclusion_phrases"])
        if hard:
            return FilterResult("excluded", "", -999.0, hard, "Hard exclusion: %s" % ", ".join(hard))

        decision = self._matches(text, self.profile["decision_intent_phrases"])
        personal = self._matches(text, self.profile["personal_context_phrases"])
        noise = self._matches(text, self.profile["noise_phrases"])
        results = []
        for cat in self.profile["categories"]:
            strong = self._matches(text, cat["strong_phrases"])
            supporting = self._matches(text, cat["supporting_phrases"])
            if not strong and not supporting:
                continue
            score = len(strong) * float(self.weights["strong_phrase"])
            score += len(supporting) * float(self.weights["supporting_phrase"])
            score += len(decision) * float(self.weights["decision_intent"])
            score += len(personal) * float(self.weights["personal_context"])
            score -= len(noise) * float(self.weights["noise_penalty"])
            matched = strong + supporting + decision + personal + noise
            results.append((score, cat["name"], matched, bool(strong)))

        if not results:
            return FilterResult("excluded", "", 0.0, noise, "No configured topic matched")
        results.sort(key=lambda x: (x[0], x[3], x[1]), reverse=True)
        score, topic, matched, has_strong = results[0]
        review_reasons = []
        for trigger in self.profile["review_triggers"]:
            phrases = trigger.get("phrases", [])
            trigger_matches = self._matches(text, phrases)
            if not trigger_matches:
                continue
            if trigger.get("requires_decision_intent") and not decision:
                continue
            review_reasons.append(trigger.get("reason", trigger.get("name", "Manual review")))
            matched += trigger_matches

        relevant_threshold = float(self.thresholds["relevant"])
        review_threshold = float(self.thresholds["needs_review"])
        unique_matched = []
        seen = set()
        for term in matched:
            k = term.lower()
            if k not in seen:
                seen.add(k)
                unique_matched.append(term)

        if review_reasons and score >= review_threshold:
            return FilterResult("review", topic, score, unique_matched, "; ".join(dict.fromkeys(review_reasons)))
        if score >= relevant_threshold:
            return FilterResult("relevant", topic, score, unique_matched, "Relevant threshold met")
        if has_strong and score >= review_threshold:
            return FilterResult("relevant", topic, score, unique_matched, "Strong topic phrase matched")
        if score >= review_threshold:
            return FilterResult("review", topic, score, unique_matched, "Borderline relevance")
        return FilterResult("excluded", topic, score, unique_matched, "Below relevance threshold")
