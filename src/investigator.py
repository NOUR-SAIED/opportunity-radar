"""User-triggered, evidence-backed due diligence for one opportunity."""
import json
import re
from html.parser import HTMLParser

import requests

from . import config
from .classifier import call_gemini


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


def fetch_posting(url: str) -> str:
    response = requests.get(url, headers=config.HTTP_HEADERS, timeout=30)
    response.raise_for_status()
    parser = _TextParser()
    parser.feed(response.text)
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()[:14000]


def _json_object(text: str) -> dict:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise
        value = json.loads(match.group(0))
    return value if isinstance(value, dict) else {}


def investigate(opportunity: dict, profile: dict) -> dict:
    """Fetch one canonical posting and return a concise structured report."""
    url = opportunity.get("url", "")
    try:
        posting = fetch_posting(url) if url else ""
    except requests.RequestException as exc:
        posting = ""
        fetch_error = str(exc)
    else:
        fetch_error = ""

    if not posting:
        return {
            "verdict": "Needs manual review",
            "confidence": "low",
            "evidence": [],
            "unknowns": [fetch_error or "The canonical posting could not be fetched."],
            "next_steps": ["Open the source link and verify work authorization, location, and deadline."],
            "provider": "retrieval-only",
        }

    key = config.get("GEMINI_API_KEY")
    if not key:
        return {
            "verdict": "Needs manual review",
            "confidence": "low",
            "evidence": [posting[:800]],
            "unknowns": ["Add GEMINI_API_KEY for evidence-based AI analysis."],
            "next_steps": ["Check the posting for nationality, work authorization, and application deadline."],
            "provider": "retrieval-only",
        }

    prompt = f"""Investigate this opportunity for the candidate. Use only the posting text as evidence.
Return ONLY one JSON object with:
verdict (one of "Strong match", "Possible match", "Needs manual review", "Likely blocked"),
confidence ("high", "medium", or "low"),
evidence (up to 4 short exact quotes from the posting),
unknowns (up to 4 things the posting does not establish),
next_steps (up to 4 concrete actions for the candidate),
fit_summary (one concise sentence).
Do not invent facts and do not claim a legal conclusion.

CANDIDATE:
{json.dumps(profile, ensure_ascii=False)}

OPPORTUNITY:
{json.dumps({k: opportunity.get(k, "") for k in ("title", "organization", "location", "description", "url")}, ensure_ascii=False)}

CANONICAL POSTING:
{posting}"""
    try:
        report = _json_object(call_gemini(prompt, key))
        report["provider"] = "gemini"
        report["evidence"] = [str(x)[:400] for x in report.get("evidence", [])[:4]]
        report["unknowns"] = [str(x)[:300] for x in report.get("unknowns", [])[:4]]
        report["next_steps"] = [str(x)[:300] for x in report.get("next_steps", [])[:4]]
        return report
    except Exception as exc:
        return {
            "verdict": "Needs manual review",
            "confidence": "low",
            "evidence": [posting[:800]],
            "unknowns": [f"AI investigation failed: {exc}"],
            "next_steps": ["Review the canonical posting manually."],
            "provider": "retrieval-only",
        }
