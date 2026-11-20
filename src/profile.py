"""Profile persistence and resume text extraction."""
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

import requests

from . import config
from .auth import AuthUser


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

    if name.endswith(".pdf") or data[:4] == b"%PDF":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise RuntimeError("PDF resume extraction requires optional dependency: pip install pypdf") from exc
        return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages).strip()
    return data.decode("utf-8", errors="replace").strip()


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
