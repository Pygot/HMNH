# src/agent/permissions.py
from enum import StrEnum

ALL = "*"
GROUPS = {
    "Chat and research": (
        ("chat.use", "Use the chat"),
        ("research.run", "Start searches"),
        ("reports.view", "Read reports"),
        ("reports.export", "Download reports"),
        ("reports.delete", "Delete reports"),
    ),
    "Candidates": (
        ("candidates.view", "See candidates and their history"),
        ("candidates.edit", "Edit candidates and notes"),
        ("contacts.view", "See contact details"),
        ("contacts.edit", "Add and change contact details"),
        ("hires.manage", "Record hires and outcomes"),
    ),
    "Emails": (
        ("emails.view", "Read email templates and drafts"),
        ("emails.edit", "Edit email templates"),
        ("emails.send", "Send emails"),
        ("emails.log", "Read the sent mail log"),
    ),
    "Automation": (
        ("routines.view", "See routines"),
        ("routines.manage", "Create and change routines"),
    ),
    "Messages": (
        ("messages.use", "Send and read messages"),
        ("messages.groups", "Create and manage groups"),
        ("messages.announce", "Post announcements"),
        ("messages.moderate", "Remove messages and announcements, manage any group"),
    ),
    "Insights": (
        ("stats.view", "See the dashboard and statistics"),
        ("finance.view", "See revenue and finance data"),
        ("finance.edit", "Add and change finance data"),
    ),
    "Data": (
        ("data.export", "Export data"),
        ("data.import", "Import data"),
        ("data.delete", "Delete stored data"),
    ),
    "Assistant": (
        ("tie.use", "Talk to Tie"),
        ("voice.use", "Use voice"),
    ),
    "Workspace": (
        ("context.edit", "Change the hiring context"),
        ("config.manage", "Manage connections, modules and the security policy"),
    ),
    "People and access": (
        ("users.view", "See accounts and their sessions"),
        ("users.manage", "Create, change and remove accounts"),
        ("roles.manage", "Create and change roles"),
        ("audit.view", "Read the audit log"),
    ),
    "API": (
        ("api.use", "Use the API"),
        ("apikeys.own", "Create own API keys"),
        ("apikeys.all", "Manage every API key"),
    ),
}
LABELS = {value: label for entries in GROUPS.values() for value, label in entries}
KNOWN = frozenset(LABELS)


class Permission(StrEnum):
    """A named permission that a role or an API key can grant.

    The values match the permission names listed in GROUPS and are plain
    strings, so members compare equal to their text.
    """

    CHAT_USE = "chat.use"
    RESEARCH_RUN = "research.run"
    REPORTS_VIEW = "reports.view"
    REPORTS_EXPORT = "reports.export"
    REPORTS_DELETE = "reports.delete"
    CANDIDATES_VIEW = "candidates.view"
    CANDIDATES_EDIT = "candidates.edit"
    CONTACTS_VIEW = "contacts.view"
    CONTACTS_EDIT = "contacts.edit"
    HIRES_MANAGE = "hires.manage"
    EMAILS_VIEW = "emails.view"
    EMAILS_EDIT = "emails.edit"
    EMAILS_SEND = "emails.send"
    EMAILS_LOG = "emails.log"
    ROUTINES_VIEW = "routines.view"
    ROUTINES_MANAGE = "routines.manage"
    MESSAGES_USE = "messages.use"
    MESSAGES_GROUPS = "messages.groups"
    MESSAGES_ANNOUNCE = "messages.announce"
    MESSAGES_MODERATE = "messages.moderate"
    STATS_VIEW = "stats.view"
    FINANCE_VIEW = "finance.view"
    FINANCE_EDIT = "finance.edit"
    DATA_EXPORT = "data.export"
    DATA_IMPORT = "data.import"
    DATA_DELETE = "data.delete"
    TIE_USE = "tie.use"
    VOICE_USE = "voice.use"
    CONTEXT_EDIT = "context.edit"
    CONFIG_MANAGE = "config.manage"
    USERS_VIEW = "users.view"
    USERS_MANAGE = "users.manage"
    ROLES_MANAGE = "roles.manage"
    AUDIT_VIEW = "audit.view"
    API_USE = "api.use"
    APIKEYS_OWN = "apikeys.own"
    APIKEYS_ALL = "apikeys.all"


def expand(granted: object) -> frozenset[str]:
    """Turn a stored permission grant into the set of effective permissions.

    Args:
        granted: Stored grant, normally a list of permission names. The star
            entry stands for every known permission.

    Returns:
        The known permission names that are granted. Empty when the grant is not
        a list, tuple or set.
    """
    names = (
        {str(item) for item in granted}
        # Any other type, including a bare string, grants nothing instead of being iterated
        # character by character.
        if isinstance(granted, (list, tuple, set, frozenset))
        else set()
    )
    if ALL in names:
        return KNOWN
    # Unknown names are dropped, so a stale or forged entry can never grant anything.
    return frozenset(names & KNOWN)


def is_known(name: str) -> bool:
    """Check whether a name is a known permission.

    Args:
        name: Permission name to check.

    Returns:
        True when the name is listed in the permission groups.
    """
    return name in KNOWN
