"""Per-user credential resolution (multi-user Phase 2).

API clients accept an optional per-user credentials dict. When a value is set
for the user it wins. Only the primary admin may fall back to the global
os.environ value (the shared/admin config, which is that account mirrored
outward); every other user — including a second admin — must provide their own
account-level values so blank fields do not accidentally sync someone else's
library. See global_fallback_allowed().

In the shared-NAS model only auth/account values differ per user — server URLs,
library IDs and engine settings stay global, so they simply fall through to
os.environ when absent from the user's dict.
"""

import logging
import os

logger = logging.getLogger(__name__)

_ALLOW_GLOBAL_FALLBACK_KEY = "__allow_global_fallback__"

# Keys stored per-user (credentials/accounts + per-service enable toggles).
# Server URLs, library IDs, and engine/catalog settings stay global.
PER_USER_CREDENTIAL_KEYS = frozenset({
    # Audiobookshelf (server URL stays global; API token + library + collection are per-user)
    "ABS_KEY", "ABS_LIBRARY_ID", "ABS_COLLECTION_NAME",
    # KOReader / KoSync (server URL global; account is per-user)
    "KOSYNC_USER", "KOSYNC_KEY", "KOSYNC_ENABLED", "KOSYNC_AUTH_METHOD",
    "DEVICE_SYNC_COLLECTION_SOURCE", "DEVICE_SYNC_COLLECTIONS",
    "DEVICE_SYNC_EXCLUDED_SHELVES", "DEVICE_SYNC_HARDCOVER_LISTS",
    "DEVICE_SYNC_HARDCOVER_LIST_NAMES",
    # Storyteller
    "STORYTELLER_USER", "STORYTELLER_PASSWORD", "STORYTELLER_ENABLED",
    # Calibre-Web (Automated)
    "CWA_USERNAME", "CWA_PASSWORD", "CWA_ENABLED",
    "CWA_SYNC_TOKEN", "CWA_SYNC_ENABLED",
    # BookOrbit (account + the user's own destination collection)
    "BOOKORBIT_USER", "BOOKORBIT_PASSWORD", "BOOKORBIT_ENABLED",
    "BOOKORBIT_SHELF_NAME",
    # BookOrbit KOReader-sync account (annotation hub spoke; kosync-style creds)
    "BOOKORBIT_KOSYNC_USER", "BOOKORBIT_KOSYNC_KEY", "BOOKORBIT_KOSYNC_OWNER",
    # Kavita (auth key identifies the Kavita user; collection/library are user choices)
    "KAVITA_ENABLED", "KAVITA_API_KEY", "KAVITA_LIBRARY_ID", "KAVITA_COLLECTION_NAME",
    # Grimmory / BookLore (account + the user's own shelf/library)
    "BOOKLORE_USER", "BOOKLORE_PASSWORD", "BOOKLORE_ENABLED",
    "BOOKLORE_SHELF_NAME", "BOOKLORE_LIBRARY_ID", "BOOKLORE_ANNOTATION_SYNC",
    # Readest (Supabase cloud sync; the account is per-user and the rotating
    # access/refresh tokens are cached per-user — the user never pastes a JWT)
    "READEST_ANNOTATION_SYNC", "READEST_EMAIL", "READEST_PASSWORD",
    "READEST_ACCESS_TOKEN", "READEST_REFRESH_TOKEN", "READEST_TOKEN_EXPIRES_AT",
    "READEST_UPLOAD_ON_MATCH", "READEST_GROUP_NAME",
    "READEST_UPLOAD_READING",
    # BookFusion
    "BOOKFUSION_ENABLED", "BOOKFUSION_ACCESS_TOKEN", "BOOKFUSION_API_KEY",
    "BOOKFUSION_ANNOTATION_SYNC",
    # Trackers (write targets are per-user accounts)
    "HARDCOVER_TOKEN", "HARDCOVER_ENABLED",
    "HARDCOVER_GRIMMORY_LIST_SYNC", "HARDCOVER_GRIMMORY_LIST_PREFIX",
    "HARDCOVER_GRIMMORY_LIST_EXCLUDED_SHELVES",
    "STORYGRAPH_SESSION_COOKIE", "STORYGRAPH_REMEMBER_USER_TOKEN", "STORYGRAPH_ENABLED",
})


# Library-lookup credentials the primary admin's account also lends to the
# engine's global singletons (shelf-watch, scans, suggestions, ABS socket,
# manifest). web_server mirrors these to the global settings when the primary
# admin saves; user_bootstrap warns at boot when the two stores diverge.
ENGINE_MIRROR_KEYS = (
    "ABS_KEY", "ABS_LIBRARY_ID",
    "BOOKLORE_USER", "BOOKLORE_PASSWORD", "BOOKLORE_SHELF_NAME", "BOOKLORE_LIBRARY_ID",
    "BOOKORBIT_USER", "BOOKORBIT_PASSWORD", "BOOKORBIT_SHELF_NAME",
    "KAVITA_API_KEY", "KAVITA_LIBRARY_ID", "KAVITA_COLLECTION_NAME",
    "CWA_USERNAME", "CWA_PASSWORD", "CWA_SYNC_TOKEN",
)


# UI grouping for the per-user credentials page. (group_label, [(key, label, type)])
# type: 'text' (blank clears), 'secret' (blank keeps existing), 'bool' (checkbox).
PER_USER_FIELD_GROUPS = [
    ("Audiobookshelf", [
        ("ABS_KEY", "API token", "secret"),
        ("ABS_LIBRARY_ID", "Library ID (optional, for a separate library)", "text"),
        ("ABS_COLLECTION_NAME", "Collection name (synced books moved here)", "text"),
    ]),
    ("KOReader / KoSync", [
        ("KOSYNC_ENABLED", "Enabled", "bool"),
        (
            "KOSYNC_AUTH_METHOD",
            "Authentication",
            "select:kosync=KoSync headers (default)|basic=HTTP Basic (Calibre-Web Automated)",
        ),
        ("KOSYNC_USER", "Sync username", "text"),
        ("KOSYNC_KEY", "Sync password", "secret"),
    ]),
    ("KOReader Collections", [
        (
            "DEVICE_SYNC_COLLECTION_SOURCE",
            "Collection source",
            "select:off=Off / Disabled|grimmory=Grimmory Shelves|hardcover=Hardcover Lists",
        ),
        (
            "DEVICE_SYNC_COLLECTIONS",
            "Grimmory shelf mode",
            "select:off=Off / Disabled|all=All Shelves|magic=Magic Shelves Only|shelf=Regular Shelves Only",
        ),
        ("DEVICE_SYNC_EXCLUDED_SHELVES", "Grimmory shelves to exclude", "text"),
        (
            "DEVICE_SYNC_HARDCOVER_LISTS",
            "Hardcover list mode",
            "select:all=All Lists|selected=Selected Lists Only",
        ),
        ("DEVICE_SYNC_HARDCOVER_LIST_NAMES", "Hardcover list names", "text"),
    ]),
    ("Storyteller", [
        ("STORYTELLER_ENABLED", "Enabled", "bool"),
        ("STORYTELLER_USER", "Username", "text"),
        ("STORYTELLER_PASSWORD", "Password", "secret"),
    ]),
    ("Grimmory", [
        ("BOOKLORE_ENABLED", "Enabled", "bool"),
        ("BOOKLORE_USER", "Username", "text"),
        ("BOOKLORE_PASSWORD", "Password", "secret"),
        ("BOOKLORE_SHELF_NAME", "Shelf name (synced books moved here)", "text"),
        ("BOOKLORE_LIBRARY_ID", "Library ID (optional; blank uses all libraries)", "text"),
        ("BOOKLORE_ANNOTATION_SYNC", "Highlight sync", "bool"),
    ]),
    ("BookFusion", [
        ("BOOKFUSION_ENABLED", "Enabled", "bool"),
        ("BOOKFUSION_ACCESS_TOKEN", "Access token", "secret"),
        ("BOOKFUSION_API_KEY", "Calibre API key (for uploads)", "secret"),
        ("BOOKFUSION_ANNOTATION_SYNC", "Highlight sync", "bool"),
    ]),
    ("BookOrbit", [
        ("BOOKORBIT_ENABLED", "Enabled", "bool"),
        ("BOOKORBIT_USER", "Username", "text"),
        ("BOOKORBIT_PASSWORD", "Password", "secret"),
        ("BOOKORBIT_SHELF_NAME", "Collection name (synced books moved here)", "text"),
        ("BOOKORBIT_KOSYNC_USER", "KOReader sync username (highlight sync)", "text"),
        ("BOOKORBIT_KOSYNC_KEY", "KOReader sync password (highlight sync)", "secret"),
        ("BOOKORBIT_KOSYNC_OWNER", "KOReader sync owner (must match BookOrbit username)", "text"),
    ]),
    ("Kavita", [
        ("KAVITA_ENABLED", "Enabled", "bool"),
        ("KAVITA_API_KEY", "Authentication key", "secret"),
        ("KAVITA_LIBRARY_ID", "Library ID (optional; blank uses all libraries)", "text"),
        ("KAVITA_COLLECTION_NAME", "Collection name (synced books moved here)", "text"),
    ]),
    ("Readest", [
        ("READEST_ANNOTATION_SYNC", "Highlight sync", "bool"),
        ("READEST_EMAIL", "Account email", "text"),
        ("READEST_PASSWORD", "Account password", "secret"),
        ("READEST_UPLOAD_ON_MATCH", "Upload matched books to Readest", "bool"),
        ("READEST_UPLOAD_READING", "Upload books you are currently reading", "bool"),
        ("READEST_GROUP_NAME", "Group name for uploaded books", "text"),
    ]),
    ("Calibre-Web Automated", [
        ("CWA_ENABLED", "Enabled", "bool"),
        ("CWA_USERNAME", "Username", "text"),
        ("CWA_PASSWORD", "Password", "secret"),
        ("CWA_SYNC_ENABLED", "Kobo sync enabled", "bool"),
        ("CWA_SYNC_TOKEN", "Kobo sync token", "secret"),
    ]),
    ("Hardcover", [
        ("HARDCOVER_ENABLED", "Enabled", "bool"),
        ("HARDCOVER_TOKEN", "API token", "secret"),
        (
            "HARDCOVER_GRIMMORY_LIST_SYNC",
            "Grimmory shelves to Hardcover lists",
            "select:off=Off / Disabled|all=All Shelves|magic=Magic Shelves Only|shelf=Regular Shelves Only",
        ),
        ("HARDCOVER_GRIMMORY_LIST_PREFIX", "Hardcover list name prefix", "text"),
        ("HARDCOVER_GRIMMORY_LIST_EXCLUDED_SHELVES", "Grimmory shelves to exclude", "text"),
    ]),
    ("StoryGraph", [
        ("STORYGRAPH_ENABLED", "Enabled", "bool"),
        ("STORYGRAPH_SESSION_COOKIE", "Session cookie", "secret"),
        ("STORYGRAPH_REMEMBER_USER_TOKEN", "Remember-user token", "secret"),
    ]),
]


def resolve_setting(credentials, key, default=None):
    """Resolve a config value for a (possibly per-user) client.

    Returns the user's value when present and non-empty. For recognized
    per-user account keys, regular user bundles do not fall back to the global
    admin environment unless their registry explicitly allows it.
    """
    if credentials:
        val = credentials.get(key)
        if val not in (None, ""):
            return val
        if key in PER_USER_CREDENTIAL_KEYS and credentials.get(_ALLOW_GLOBAL_FALLBACK_KEY) is False:
            return default
    return os.environ.get(key, default)


def global_fallback_allowed(database_service, user) -> bool:
    """Whether this user's BLANK per-user credentials may fall back to the global config.

    The single source of truth for the ``_ALLOW_GLOBAL_FALLBACK_KEY`` flag that
    callers put in the credentials dict ``resolve_setting`` consumes. Only the
    PRIMARY admin may inherit: the global settings are that admin's own account
    mirrored outward (see ENGINE_MIRROR_KEYS), so letting a second admin inherit
    them would silently sync their books against the primary admin's
    Audiobookshelf/Grimmory/BookOrbit/CWA accounts.

    Fails closed — a database hiccup must never widen credential access.
    """
    if user is None or not getattr(user, "is_admin", False) or database_service is None:
        return False
    try:
        return bool(database_service.is_primary_admin(getattr(user, "id", None)))
    except Exception as e:
        logger.warning(
            "Primary-admin lookup failed for user %s; denying global credential fallback: %s",
            getattr(user, "id", "?"), e, exc_info=True,
        )
        return False


def user_setting(key, default=None):
    """Resolve a setting for the current request/cycle's user (ambient context),
    falling back to the global os.environ value. Use this for direct (non-client,
    non-cached) settings reads — library id, enable flags, search scope — that
    must honor the logged-in user instead of the global/admin config."""
    from src.utils.user_context import get_current_user_credentials
    return resolve_setting(get_current_user_credentials(), key, default)
