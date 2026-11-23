"""Profile persistence and resume text extraction."""
import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

import requests

from . import config
from .auth import AuthUser


MAX_RESUME_BYTES = 5 * 1024 * 1024
MAX_RESUME_CHARS = 20000


def extract_resume_text(source: str | Path | bytes | BinaryIO) -> str:
    """Extract UTF-8 text or PDF text, importing pypdf only for PDF input."""
    if isinstance(source, (str, Path)):
        path = Path(source)
        data = path.read_bytes()
        name = path.name.lower()
    elif isinstance(source, bytes):
        data, name = source, ""
    else:
        data = source.read()
        name = str(getattr(source, "name", "")).lower()
    if len(data) > MAX_RESUME_BYTES:
        raise ValueError("Resume is too large. Upload a PDF or text file smaller than 5 MB.")
    if not data:
        raise ValueError("The uploaded resume is empty.")

    if name.endswith(".pdf") or data[:4] == b"%PDF":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise RuntimeError("PDF resume extraction requires optional dependency: pip install pypdf") from exc
        try:
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                raise ValueError("The PDF is password-protected. Upload an unlocked copy.")
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("This PDF could not be read. Upload a text-based PDF or paste a .txt resume.") from exc
    else:
        text = data.decode("utf-8", errors="replace")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[^\S\n]+", " ", text).strip()
    if len(text) < 30:
        raise ValueError("No usable text was found. The resume may be scanned or image-only.")
    return text[:MAX_RESUME_CHARS]


def extract_profile_from_resume(resume_text: str) -> dict[str, Any]:
    """Use the configured model to propose a structured profile for user review.

    This is extraction only: the user remains responsible for reviewing and
    correcting every field before it is saved.
    """
    key = config.get("GEMINI_API_KEY")
    if not key:
        return {}
    from .classifier import call_gemini

    prompt = f"""Read this resume and return ONLY a JSON object for a user-reviewed profile.
Use these fields:
name, headline, nationality, residence, availability, degree, languages,
target_roles (array), skills (array), experience (array), summary.
Do not infer sensitive or uncertain facts. Keep all values concise. Use "" or []
when the resume does not establish a value.

RESUME:
{resume_text[:MAX_RESUME_CHARS]}"""
    try:
        raw = call_gemini(prompt, key)
        match = re.search(r"\{.*\}", raw, re.S)
        parsed = json.loads(match.group(0) if match else raw)
        if not isinstance(parsed, dict):
            return {}
        allowed = {
            "name", "headline", "nationality", "residence", "availability",
            "degree", "languages", "target_roles", "skills", "experience", "summary",
        }
        clean = {key: parsed[key] for key in allowed if key in parsed}
        for field in ("target_roles", "skills", "languages", "experience"):
            if field in clean and not isinstance(clean[field], list):
                clean[field] = [str(clean[field])] if clean[field] else []
        for field in ("name", "headline", "nationality", "residence", "availability",
                      "degree", "summary"):
            if field in clean:
                clean[field] = str(clean[field])[:1000]
        return clean
    except Exception:
        return {}


class LocalProfileStore:
    """JSON profile store for local/demo mode."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or config.ROOT / "data" / "profiles.json")

    def _read(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def load(self, user_id: str) -> dict[str, Any] | None:
        return self._read().get(user_id)

    def save(self, user_id: str, profile: dict[str, Any]) -> dict[str, Any]:
        rows = self._read()
        rows[user_id] = dict(profile)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        return rows[user_id]


class SupabaseProfileStore:
    """Profile REST adapter; RLS scopes every request to the authenticated user."""

    def __init__(self, url: str, user: AuthUser, timeout: int = 20):
        if not user.access_token:
            raise ValueError("A Supabase access token is required to persist a profile.")
        self.endpoint = url.rstrip("/") + "/rest/v1/profiles"
        self.headers = {
            "apikey": config.get("SUPABASE_ANON_KEY"),
            "Authorization": f"Bearer {user.access_token}",
            "Content-Type": "application/json",
        }
        self.user = user
        self.timeout = timeout

    def load(self, user_id: str | None = None) -> dict[str, Any] | None:
        user_id = user_id or self.user.id
        response = requests.get(
            self.endpoint, headers=self.headers,
            params={"select": "*", "user_id": f"eq.{user_id}"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        rows = response.json()
        if not rows:
            return None
        row = rows[0]
        profile = dict(row.get("profile") or {})
        if row.get("resume_text"):
            profile["resume_text"] = row["resume_text"]
        return profile

    def save(self, user_id: str, profile: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "user_id": user_id,
            "full_name": profile.get("name", ""),
            "resume_text": profile.get("resume_text", ""),
            "profile": profile,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        response = requests.post(
            self.endpoint,
            headers={**self.headers, "Prefer": "resolution=merge-duplicates,return=representation"},
            params={"on_conflict": "user_id"},
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return profile


def get_profile_store(user: AuthUser | None = None):
    """Select Supabase for an authenticated user, otherwise safe local storage."""
    if user and user.access_token and config.get("SUPABASE_URL") and config.get("SUPABASE_ANON_KEY"):
        return SupabaseProfileStore(config.get("SUPABASE_URL"), user)
    return LocalProfileStore()


def load_profile(user: AuthUser | None = None, store=None) -> dict[str, Any] | None:
    store = store or get_profile_store(user)
    if user:
        return store.load(user.id)
    return None


def save_profile(profile: dict[str, Any], user: AuthUser | None = None, store=None) -> dict[str, Any]:
    store = store or get_profile_store(user)
    if user:
        return store.save(user.id, profile)
    raise ValueError("A user is required to save a profile.")
