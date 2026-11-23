"""Small authentication adapter for Streamlit.

Supabase is used only when the public URL and anon key are configured.  Local
development otherwise uses an intentionally limited demo account and never
pretends to provide production authentication.
"""
from dataclasses import dataclass, field
import hashlib
import secrets
from typing import Any

import requests

from . import config

DEMO_EMAIL = "demo@example.com"
DEMO_PASSWORD = "demo-password"


class AuthError(RuntimeError):
    """Raised when authentication cannot be completed."""


def validate_credentials(email: str, password: str) -> None:
    email = email.strip()
    if not email or "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        raise AuthError("Enter a valid email address.")
    if len(password) < 8:
        raise AuthError("Password must contain at least 8 characters.")
    if password.isnumeric():
        raise AuthError("Password must contain letters or symbols.")


@dataclass(frozen=True)
class AuthUser:
    id: str
    email: str
    access_token: str | None = None
    refresh_token: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def _user_from_response(data: dict[str, Any]) -> AuthUser:
    user = data.get("user") or {}
    if not user.get("id") or not user.get("email"):
        raise AuthError("Supabase returned an incomplete user.")
    return AuthUser(
        id=user["id"],
        email=user["email"],
        access_token=data.get("access_token"),
        refresh_token=data.get("refresh_token"),
        metadata=user.get("user_metadata") or {},
    )


class SupabaseAuth:
    """Email/password authentication using Supabase's public auth API."""

    def __init__(self, url: str, anon_key: str, timeout: int = 20):
        self.base = url.rstrip("/") + "/auth/v1"
        self.headers = {"apikey": anon_key, "Content-Type": "application/json"}
        self.timeout = timeout

    def _request(self, method: str, path: str, **kwargs: Any) -> AuthUser:
        try:
            response = requests.request(
                method, self.base + path, headers=self.headers, timeout=self.timeout, **kwargs
            )
        except requests.RequestException as exc:
            raise AuthError("Could not reach Supabase authentication.") from exc
        if not response.ok:
            try:
                body = response.json()
                message = (
                    body.get("msg")
                    or body.get("message")
                    or body.get("error_description")
                    or body.get("error")
                )
            except ValueError:
                message = None
            raise AuthError(message or f"Supabase authentication failed ({response.status_code}).")
        return _user_from_response(response.json())

    def sign_in(self, email: str, password: str) -> AuthUser:
        validate_credentials(email, password)
        return self._request("POST", "/token?grant_type=password",
                             json={"email": email.strip(), "password": password})

    def sign_up(self, email: str, password: str) -> AuthUser:
        validate_credentials(email, password)
        return self._request("POST", "/signup",
                             json={"email": email.strip(), "password": password})

    def refresh(self, refresh_token: str) -> AuthUser:
        if not refresh_token:
            raise AuthError("This session needs to sign in again.")
        return self._request("POST", "/token?grant_type=refresh_token",
                             json={"refresh_token": refresh_token})

    def sign_out(self, access_token: str) -> None:
        if not access_token:
            return
        try:
            requests.post(
                self.base + "/logout",
                headers={**self.headers, "Authorization": f"Bearer {access_token}"},
                timeout=self.timeout,
            )
        except requests.RequestException:
            pass


class LocalDemoAuth:
    """A deliberately bounded fallback for trying the app without Supabase."""

    def sign_in(self, email: str, password: str) -> AuthUser:
        if email.strip().lower() != DEMO_EMAIL or not secrets.compare_digest(password, DEMO_PASSWORD):
            raise AuthError(f"Local demo login only supports {DEMO_EMAIL}.")
        user_id = hashlib.sha256(DEMO_EMAIL.encode()).hexdigest()
        return AuthUser(id=user_id, email=DEMO_EMAIL, access_token=None)

    def sign_up(self, email: str, password: str) -> AuthUser:
        raise AuthError("Sign-up is unavailable in local demo mode; configure Supabase to enable it.")


def get_auth() -> SupabaseAuth | LocalDemoAuth:
    """Return the configured backend without ever using the service-role key."""
    url, anon_key = config.get("SUPABASE_URL"), config.get("SUPABASE_ANON_KEY")
    if url and anon_key:
        return SupabaseAuth(url, anon_key)
    return LocalDemoAuth()


def render_auth(st_module=None) -> AuthUser | None:
    """Render a compact Streamlit login form and keep the user in session state.

    This function is optional app glue: importing this module does not require
    Streamlit, and callers can continue using their existing dashboard layout.
    """
    if st_module is None:
        import streamlit as st_module
    key = "auth_user"
    if st_module.session_state.get(key):
        return st_module.session_state[key]

    backend = get_auth()
    left, right = st_module.columns([1.15, 0.85], gap="large")
    with left:
        st_module.markdown(
        "<div class='auth-page'>"
        "<div class='auth-story'><div class='auth-mark'>OR</div>"
        "<div class='auth-kicker'>PERSONAL OPPORTUNITY INTELLIGENCE</div>"
        "<h1>Make every opportunity feel personal.</h1>"
        "<p>Import your profile, find relevant roles, and track applications without losing your privacy.</p>"
        "<div class='auth-feature'><b>01</b><span>Personalized fit</span></div>"
        "<div class='auth-feature'><b>02</b><span>Private application tracker</span></div>"
        "<div class='auth-feature'><b>03</b><span>Evidence-backed decisions</span></div>"
        "</div></div>",
        unsafe_allow_html=True,
        )

    with right:
        st_module.markdown("<div class='auth-card'><div class='auth-card-label'>YOUR WORKSPACE</div>", unsafe_allow_html=True)
        sign_in_tab, sign_up_tab = st_module.tabs(["Sign in", "Create account"])
        submitted = False
        sign_up = False
        email = ""
        password = ""

        with sign_in_tab:
            with st_module.form("sign_in"):
                email = st_module.text_input("Email", value=DEMO_EMAIL if isinstance(backend, LocalDemoAuth) else "", key="login_email")
                password = st_module.text_input(
                    "Password",
                    type="password",
                    key="login_password",
                    help=f"Local demo password: {DEMO_PASSWORD}" if isinstance(backend, LocalDemoAuth) else None,
                )
                submitted = st_module.form_submit_button("Sign in", type="primary", width="stretch")
        with sign_up_tab:
            with st_module.form("sign_up"):
                if isinstance(backend, LocalDemoAuth):
                    st_module.caption("Create-account mode requires Supabase configuration.")
                    st_module.form_submit_button("Create account", disabled=True)
                else:
                    signup_email = st_module.text_input("Email", key="signup_email")
                    signup_password = st_module.text_input("Password", type="password", key="signup_password")
                    signup_confirm = st_module.text_input("Confirm password", type="password", key="signup_confirm")
                    sign_up_submitted = st_module.form_submit_button("Create account", type="primary", width="stretch")
                    if sign_up_submitted:
                        if signup_password != signup_confirm:
                            st_module.error("Passwords do not match.")
                        else:
                            email, password, sign_up, submitted = signup_email, signup_password, True, True
        st_module.markdown("</div>", unsafe_allow_html=True)
    if submitted:
        try:
            user = backend.sign_up(email, password) if sign_up else backend.sign_in(email, password)
            if sign_up and isinstance(backend, SupabaseAuth) and not user.access_token:
                st_module.info("Check your email to confirm the account, then sign in.")
                return None
            st_module.session_state[key] = user
            st_module.rerun()
        except AuthError as exc:
            st_module.error(str(exc))
    return None


def sign_out(st_module=None) -> None:
    """Clear the local Streamlit session; Supabase access tokens are short-lived."""
    if st_module is None:
        import streamlit as st_module
    user = st_module.session_state.pop("auth_user", None)
    if user and isinstance(get_auth(), SupabaseAuth):
        get_auth().sign_out(user.access_token or "")
    for key in ("user_profile", "resume_review", "scan_result", "triage_pos"):
        st_module.session_state.pop(key, None)
    st_module.rerun()
