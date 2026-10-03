"""Installed-app OAuth with PKCE and OS credential-store persistence."""

import json

import keyring
from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from .config import Settings
from .errors import ConfigurationError, PermissionDenied, WorkflowError

SCOPES = (
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.student-submissions.me.readonly",
    "https://www.googleapis.com/auth/classroom.courseworkmaterials.readonly",
    "https://www.googleapis.com/auth/classroom.announcements.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
)
SERVICE = "classroomautowork.google-oauth"

# Google returns canonical scope names; these documented aliases have identical read authority.
SCOPE_ALIASES = {
    "email": "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/classroom.coursework.me.readonly": "https://www.googleapis.com/auth/classroom.student-submissions.me.readonly",
}


def normalized_scopes(scopes) -> set[str]:
    return {SCOPE_ALIASES.get(scope, scope) for scope in scopes}


def normalize_scope_response(response):
    """Normalize only known aliases before oauthlib's strict scope comparison.

    Unknown/additional permissions remain present so oauthlib rejects them.
    No token, auth code, credential or token response is logged.
    """
    payload = response.json()
    if isinstance(payload.get("scope"), str):
        payload["scope"] = " ".join(sorted(normalized_scopes(payload["scope"].split())))
        response._content = json.dumps(payload).encode("utf-8")
    return response


def secure_keyring() -> None:
    backend = type(keyring.get_keyring())
    if backend.__module__ not in {
        "keyring.backends.Windows",
        "keyring.backends.macOS",
        "keyring.backends.SecretService",
        "keyring.backends.kwallet",
    }:
        raise ConfigurationError(
            "An OS credential-store backend is required; plaintext/fallback keyrings are rejected."
        )


def verify_identity(credentials: Credentials, expected_email: str) -> dict:
    with AuthorizedSession(credentials) as session:
        response = session.get("https://openidconnect.googleapis.com/v1/userinfo", timeout=30)
        if response.status_code != 200:
            raise PermissionDenied("OAuth identity lookup failed. Reauthorize the school account.")
        identity = response.json()
    if (
        not identity.get("email_verified")
        or identity.get("email", "").lower() != expected_email.lower()
    ):
        raise PermissionDenied("The authorized account does not match the configured school email.")
    return {
        "email": identity["email"],
        "subject": identity["sub"],
        "hosted_domain": identity.get("hd"),
    }


def credentials_for(settings: Settings, *, authorize: bool = False) -> tuple[Credentials, dict]:
    client_path = settings.client_json
    if not client_path.is_file():
        raise ConfigurationError(
            "OAuth client JSON does not exist. Create a Desktop app client in Google Cloud."
        )
    try:
        client = json.loads(client_path.read_text(encoding="utf-8-sig")).get("installed", {})
    except ValueError as exc:
        raise ConfigurationError("OAuth client JSON is invalid.") from exc
    if (
        not client.get("client_id")
        or not client.get("client_secret")
        or client.get("auth_uri")
        not in {
            "https://accounts.google.com/o/oauth2/auth",
            "https://accounts.google.com/o/oauth2/v2/auth",
        }
        or client.get("token_uri") != "https://oauth2.googleapis.com/token"
    ):
        raise ConfigurationError(
            "Use a genuine Google Desktop app client JSON with official OAuth endpoints."
        )
    secure_keyring()
    account_key = settings.school_email + ":" + client["client_id"]
    stored = None if authorize else keyring.get_password(SERVICE, account_key)
    credentials = None
    if stored:
        try:
            credentials = Credentials.from_authorized_user_info(json.loads(stored))
        except ValueError as exc:
            raise ConfigurationError(
                "OS credential-store entry is invalid. Run auth again."
            ) from exc
        if not set(SCOPES).issubset(normalized_scopes(credentials.scopes or ())):
            raise PermissionDenied(
                "Stored authorization lacks required read-only scopes. Run auth again."
            )
        if normalized_scopes(credentials.scopes or ()) - set(SCOPES):
            raise PermissionDenied(
                "Stored authorization includes unexpected scopes. Run auth again."
            )
        if credentials.expired and credentials.refresh_token:
            try:
                credentials.refresh(Request())
            except Exception as exc:
                raise PermissionDenied(
                    "OAuth refresh failed; run auth again. No token has been logged."
                ) from exc
    if not credentials or not credentials.valid:
        if not authorize:
            raise PermissionDenied("No valid OAuth authorization. Run classroomaw auth first.")
        # Use the already validated installed config, not a second file read.
        flow = InstalledAppFlow.from_client_config(
            {"installed": client}, scopes=SCOPES, autogenerate_code_verifier=True
        )
        flow.oauth2session.register_compliance_hook(
            "access_token_response", normalize_scope_response
        )
        try:
            credentials = flow.run_local_server(
                host="127.0.0.1",
                port=0,
                open_browser=True,
                timeout_seconds=300,
                prompt="consent select_account",
                login_hint=settings.school_email,
                include_granted_scopes="false",
                authorization_prompt_message="请在浏览器中使用学校账户授权：{url}",
                success_message="OAuth callback received. You can close this tab; account and API validation follows.",
            )
        except Exception as exc:
            raise WorkflowError(
                "OAuth did not complete ("
                + type(exc).__name__
                + "). Check browser consent, test-user setup and school admin policy."
            ) from exc
    identity = verify_identity(credentials, settings.school_email)
    granted = normalized_scopes(credentials.granted_scopes or credentials.scopes or ())
    if granted != set(SCOPES):
        raise PermissionDenied(
            "Granted scopes differ from the fixed read-only scope set. Reauthorize."
        )
    keyring.set_password(SERVICE, account_key, credentials.to_json())
    return credentials, identity
