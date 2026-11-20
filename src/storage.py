"""Two interchangeable backends:
- JsonStore: data/opportunities.json, zero setup, for local use.
- SupabaseStore: free Postgres, needed when the dashboard is hosted, because
  both GitHub Actions and Streamlit Cloud must see the same data."""
import json
from datetime import datetime, timedelta, timezone

import requests

from . import config

TRACKING_FIELDS = ("status", "notes")


def get_store():
    url, key = config.get("SUPABASE_URL"), config.get("SUPABASE_KEY")
    return SupabaseStore(url, key) if url and key else JsonStore()


def _cutoff() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=config.KEEP_DAYS)).isoformat()


class JsonStore:
    name = "local JSON"

    def __init__(self, path=config.ROOT / "data" / "opportunities.json"):
        self.path = path

    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        with open(self.path, encoding="utf-8") as f:
            return {r["id"]: r for r in json.load(f)}

    def _write(self, rows: dict):
        self.path.parent.mkdir(exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(list(rows.values()), f, ensure_ascii=False, indent=1)
        tmp.replace(self.path)

    def all(self) -> list[dict]:
        return list(self._read().values())

    def known_ids(self) -> set[str]:
        return set(self._read())

    def upsert(self, records: list[dict]):
        rows = self._read()
        for r in records:
            existing = rows.get(r["id"], {})
            rows[r["id"]] = {**r, **{k: existing[k] for k in TRACKING_FIELDS if k in existing}}
        self._write(rows)

    def update(self, opp_id: str, **fields):
        rows = self._read()
        if opp_id in rows:
            rows[opp_id].update(fields)
            self._write(rows)

    def log_run(self, run: dict):
        path = self.path.parent / "runs.json"
        runs = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        path.write_text(json.dumps((runs + [run])[-30:], ensure_ascii=False, indent=1), encoding="utf-8")

    def runs(self) -> list[dict]:
        path = self.path.parent / "runs.json"
        return json.loads(path.read_text(encoding="utf-8"))[::-1] if path.exists() else []

    def prune(self) -> int:
        rows, cutoff = self._read(), _cutoff()
        keep = {k: v for k, v in rows.items()
                if v.get("status") not in ("new", "skipped") or v.get("found_at", "") >= cutoff}
        self._write(keep)
        return len(rows) - len(keep)
def check_supabase_key(key: str):
    """The public key can't write to the table (row-level security), which only shows up
    later as a confusing 401. Catch it immediately with a clear message."""
    import base64
    wrong = "SUPABASE_KEY is the public key. Use the SECRET key: Supabase > Project Settings > API Keys."
    if key.startswith("sb_publishable_"):
        raise RuntimeError(wrong)
    if key.startswith("eyJ"):
        try:
            payload = key.split(".")[1]
            role = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))).get("role")
        except Exception:
            return
        if role != "service_role":
            raise RuntimeError(wrong + f" (this key's role is '{role}', it must be 'service_role')")

class SupabaseStore:
    name = "Supabase"

    def __init__(self, url: str, key: str):
        check_supabase_key(key)
        self.base = url.rstrip("/") + "/rest/v1/opportunities"
        self.h = {"apikey": key, "Content-Type": "application/json"}
        if key.startswith("eyJ"):  # legacy JWT keys also go in Authorization; new sb_secret_ keys must not
            self.h["Authorization"] = f"Bearer {key}"

    def _get(self, params: dict) -> list[dict]:
        rows, start = [], 0
        while True:
            h = {**self.h, "Range": f"{start}-{start + 999}"}
            r = requests.get(self.base, headers=h, params=params, timeout=30)
            r.raise_for_status()
            chunk = r.json()
            rows += chunk
            if len(chunk) < 1000:
                return rows
            start += 1000

    def all(self) -> list[dict]:
        return self._get({"select": "*"})

    def known_ids(self) -> set[str]:
        return {r["id"] for r in self._get({"select": "id"})}

    def upsert(self, records: list[dict]):
        # Insert-only: rows that already exist are left untouched, so your status and
        # notes are never overwritten, and new rows keep the status the pipeline set.
        for i in range(0, len(records), 200):
            r = requests.post(self.base, headers={**self.h, "Prefer": "resolution=ignore-duplicates"},
                              json=records[i:i + 200], timeout=30)
            if not r.ok:
                raise RuntimeError(f"Supabase insert failed: {r.status_code} {r.text[:300]}")

    def log_run(self, run: dict):
        url = self.base.rsplit("/", 1)[0] + "/runs"
        r = requests.post(url, headers=self.h, json={"ran_at": run["ran_at"], "stats": run}, timeout=30)
        if not r.ok:
            print(f"  [supabase] couldn't log run: {r.status_code} (did you create the runs table?)")

    def runs(self) -> list[dict]:
        url = self.base.rsplit("/", 1)[0] + "/runs"
        r = requests.get(url, headers=self.h, params={"select": "stats", "order": "ran_at.desc", "limit": 30}, timeout=30)
        return [row["stats"] for row in r.json()] if r.ok else []

    def update(self, opp_id: str, **fields):
        r = requests.patch(self.base, headers=self.h, params={"id": f"eq.{opp_id}"}, json=fields, timeout=30)
        r.raise_for_status()

    def prune(self) -> int:
        r = requests.delete(self.base, headers={**self.h, "Prefer": "return=representation"},
                            params={"status": "in.(new,skipped)", "found_at": f"lt.{_cutoff()}"}, timeout=30)
        r.raise_for_status()
        return len(r.json())


class UserScopedStore:
    """Overlay private workflow state on the shared opportunity catalog."""

    name = "Supabase + private profile"

    def __init__(self, catalog, user_id: str, access_token: str | None = None):
        self.catalog = catalog
        self.user_id = user_id
        self.access_token = access_token
        self.local_path = config.ROOT / "data" / "user_state.json"

    def _local_states(self) -> dict:
        if not self.local_path.exists():
            return {}
        with open(self.local_path, encoding="utf-8") as f:
            return json.load(f)

    def _remote_states(self) -> dict[str, dict]:
        if not self.access_token or not config.get("SUPABASE_ANON_KEY"):
            return {}
        endpoint = config.get("SUPABASE_URL").rstrip("/") + "/rest/v1/user_opportunity_state"
        r = requests.get(
            endpoint,
            headers={"apikey": config.get("SUPABASE_ANON_KEY"), "Authorization": f"Bearer {self.access_token}"},
            params={"select": "opportunity_id,status,notes", "user_id": f"eq.{self.user_id}"},
            timeout=30,
        )
        r.raise_for_status()
        return {row["opportunity_id"]: row for row in r.json()}

    def _states(self) -> dict:
        if self.access_token:
            return self._remote_states()
        return self._local_states().get(self.user_id, {})

    def all(self) -> list[dict]:
        states = self._states()
        rows = []
        for row in self.catalog.all():
            state = states.get(row["id"], {})
            rows.append({**row, "status": state.get("status", "new"), "notes": state.get("notes", "")})
        return rows

    def update(self, opp_id: str, **fields):
        current = self._states().get(opp_id, {})
        payload = {
            "user_id": self.user_id,
            "opportunity_id": opp_id,
            "status": fields.get("status", current.get("status", "new")),
            "notes": fields.get("notes", current.get("notes", "")),
        }
        if self.access_token:
            endpoint = config.get("SUPABASE_URL").rstrip("/") + "/rest/v1/user_opportunity_state"
            r = requests.post(
                endpoint,
                headers={
                    "apikey": config.get("SUPABASE_ANON_KEY"),
                    "Authorization": f"Bearer {self.access_token}",
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates,return=minimal",
                },
                params={"on_conflict": "user_id,opportunity_id"},
                json=payload,
                timeout=30,
            )
            r.raise_for_status()
            return
        states = self._local_states()
        user = states.setdefault(self.user_id, {})
        current = user.setdefault(opp_id, {"status": "new", "notes": ""})
        current.update({k: v for k, v in fields.items() if k in ("status", "notes")})
        self.local_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.local_path, "w", encoding="utf-8") as f:
            json.dump(states, f, ensure_ascii=False, indent=2)

    def runs(self):
        return self.catalog.runs()


def get_user_store(user):
    """Return a private state overlay for an authenticated Streamlit user."""
    token = getattr(user, "access_token", None)
    if token and config.get("SUPABASE_URL") and config.get("SUPABASE_ANON_KEY"):
        catalog = AuthenticatedCatalogStore(config.get("SUPABASE_URL"), token)
    else:
        catalog = get_store()
    return UserScopedStore(catalog, user.id, token)


class AuthenticatedCatalogStore:
    """Read-only catalog access using the signed-in user's token."""

    name = "Supabase catalog + private profile"

    def __init__(self, url: str, access_token: str, timeout: int = 30):
        base = url.rstrip("/") + "/rest/v1"
        self.opportunities = base + "/opportunities"
        self.runs_endpoint = base + "/runs"
        self.headers = {
            "apikey": config.get("SUPABASE_ANON_KEY"),
            "Authorization": f"Bearer {access_token}",
        }
        self.timeout = timeout

    def all(self) -> list[dict]:
        r = requests.get(self.opportunities, headers=self.headers,
                         params={"select": "*"}, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def runs(self) -> list[dict]:
        r = requests.get(self.runs_endpoint, headers=self.headers,
                         params={"select": "stats", "order": "ran_at.desc", "limit": 30},
                         timeout=self.timeout)
        r.raise_for_status()
        return [row["stats"] for row in r.json()]
