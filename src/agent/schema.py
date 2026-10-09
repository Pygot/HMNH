# src/agent/schema.py

MIGRATIONS = (
    """
CREATE TABLE IF NOT EXISTS threads (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT '{}',
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}',
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_by_thread ON messages(thread_id, id);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    thread_id TEXT REFERENCES threads(id) ON DELETE SET NULL,
    owner TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    state TEXT NOT NULL,
    stage TEXT NOT NULL,
    rating REAL,
    request TEXT,
    report TEXT,
    events TEXT NOT NULL DEFAULT '[]',
    clarification TEXT,
    error TEXT,
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_by_owner ON jobs(owner, created);
CREATE TABLE IF NOT EXISTS memory (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated REAL NOT NULL
);
""",
    """
CREATE TABLE IF NOT EXISTS roles (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT NOT NULL DEFAULT '',
    permissions TEXT NOT NULL DEFAULT '[]',
    builtin INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT NOT NULL DEFAULT '',
    email TEXT,
    role_id TEXT NOT NULL REFERENCES roles(id) ON DELETE RESTRICT,
    password_hash TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    totp_secret TEXT,
    totp_enabled INTEGER NOT NULL DEFAULT 0,
    totp_counter INTEGER,
    recovery_codes TEXT NOT NULL DEFAULT '[]',
    email_code_login INTEGER NOT NULL DEFAULT 0,
    email_verified INTEGER NOT NULL DEFAULT 0,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    token_version INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    password_changed REAL,
    last_login REAL
);
CREATE INDEX IF NOT EXISTS users_by_role ON users(role_id);
CREATE TABLE IF NOT EXISTS links (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    purpose TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created REAL NOT NULL,
    expires REAL NOT NULL,
    used REAL,
    created_by TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS links_by_user ON links(user_id, purpose);
CREATE TABLE IF NOT EXISTS email_codes (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    purpose TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    expires REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, purpose)
);
CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    prefix TEXT NOT NULL UNIQUE,
    secret_hash TEXT NOT NULL,
    permissions TEXT,
    created REAL NOT NULL,
    expires REAL,
    last_used REAL,
    revoked REAL
);
CREATE INDEX IF NOT EXISTS api_keys_by_user ON api_keys(user_id);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    actor_id TEXT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT 'ok',
    ip TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS audit_by_time ON audit(ts);
CREATE INDEX IF NOT EXISTS audit_by_action ON audit(action, id);
""",
    """
CREATE TABLE IF NOT EXISTS mail_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    actor_id TEXT,
    actor TEXT NOT NULL DEFAULT '',
    to_address TEXT NOT NULL,
    subject TEXT NOT NULL,
    kind TEXT NOT NULL,
    purpose TEXT NOT NULL,
    template TEXT NOT NULL DEFAULT '',
    job_id TEXT NOT NULL DEFAULT '',
    candidate_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    message_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS mail_log_by_time ON mail_log(ts);
CREATE INDEX IF NOT EXISTS mail_log_by_address ON mail_log(to_address);
CREATE TABLE IF NOT EXISTS suppressions (
    address TEXT PRIMARY KEY COLLATE NOCASE,
    reason TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL,
    added_by TEXT NOT NULL DEFAULT ''
);
""",
    """
CREATE TABLE IF NOT EXISTS candidates (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL,
    headline TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    employer TEXT NOT NULL DEFAULT '',
    skills TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'sourced',
    rating REAL,
    researched INTEGER NOT NULL DEFAULT 0,
    tags TEXT NOT NULL DEFAULT '[]',
    notes TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL,
    updated REAL NOT NULL,
    created_by TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS candidates_by_name ON candidates(name_key);
CREATE INDEX IF NOT EXISTS candidates_by_status ON candidates(status);
CREATE TABLE IF NOT EXISTS candidate_links (
    candidate_id TEXT NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    url_key TEXT NOT NULL,
    url TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (candidate_id, url_key)
);
CREATE INDEX IF NOT EXISTS candidate_links_by_key ON candidate_links(url_key);
CREATE TABLE IF NOT EXISTS contacts (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'manual',
    created REAL NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    UNIQUE (candidate_id, kind, value)
);
CREATE TABLE IF NOT EXISTS hires (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    role_title TEXT NOT NULL,
    department TEXT NOT NULL DEFAULT '',
    started TEXT NOT NULL DEFAULT '',
    ended TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT 'active',
    rating INTEGER,
    review TEXT NOT NULL DEFAULT '',
    job_id TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL,
    updated REAL NOT NULL,
    created_by TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS hires_by_candidate ON hires(candidate_id);
CREATE TABLE IF NOT EXISTS candidate_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id TEXT NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    job_id TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS candidate_events_by_candidate ON candidate_events(candidate_id, id);
""",
    """
CREATE TABLE IF NOT EXISTS routines (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    skill TEXT NOT NULL,
    location TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 5,
    goal TEXT NOT NULL DEFAULT 'hiring',
    requirements TEXT NOT NULL DEFAULT '[]',
    options TEXT NOT NULL DEFAULT '{}',
    min_rating REAL NOT NULL,
    need_musts INTEGER NOT NULL DEFAULT 1,
    need_count INTEGER NOT NULL DEFAULT 1,
    interval_hours INTEGER NOT NULL,
    max_runs INTEGER NOT NULL,
    runs INTEGER NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    next_run REAL,
    last_run REAL,
    last_job TEXT NOT NULL DEFAULT '',
    last_result TEXT NOT NULL DEFAULT '',
    best_rating REAL,
    notify_emails TEXT NOT NULL DEFAULT '[]',
    purpose_confirmed INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    created_by TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS routines_by_due ON routines(status, next_run);
CREATE TABLE IF NOT EXISTS routine_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    routine_id TEXT NOT NULL REFERENCES routines(id) ON DELETE CASCADE,
    ts REAL NOT NULL,
    finished REAL,
    job_id TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'running',
    best_rating REAL,
    matches INTEGER NOT NULL DEFAULT 0,
    summary TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS routine_runs_by_routine ON routine_runs(routine_id, id);
CREATE INDEX IF NOT EXISTS routine_runs_by_job ON routine_runs(job_id);
""",
    """
CREATE TABLE IF NOT EXISTS finance_categories (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE,
    kind TEXT NOT NULL,
    created REAL NOT NULL,
    UNIQUE (name, kind)
);
CREATE TABLE IF NOT EXISTS finance_entries (
    id TEXT PRIMARY KEY,
    day TEXT NOT NULL,
    month TEXT NOT NULL,
    kind TEXT NOT NULL,
    amount INTEGER NOT NULL,
    category_id TEXT REFERENCES finance_categories(id) ON DELETE SET NULL,
    note TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'manual',
    created REAL NOT NULL,
    created_by TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS finance_by_day ON finance_entries(day);
CREATE INDEX IF NOT EXISTS finance_by_month ON finance_entries(month, kind);
CREATE INDEX IF NOT EXISTS finance_by_category ON finance_entries(category_id);
""",
    """
CREATE TABLE IF NOT EXISTS message_keys (
    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    public_key TEXT NOT NULL,
    wrapped_private TEXT NOT NULL,
    salt TEXT NOT NULL,
    iterations INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    role_id TEXT REFERENCES roles(id) ON DELETE CASCADE,
    encrypted INTEGER NOT NULL DEFAULT 0,
    key_version INTEGER NOT NULL DEFAULT 1,
    pair TEXT UNIQUE,
    created REAL NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS conversations_by_role ON conversations(role_id);
CREATE TABLE IF NOT EXISTS members (
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    owner INTEGER NOT NULL DEFAULT 0,
    joined REAL NOT NULL,
    last_read INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (conversation_id, user_id)
);
CREATE INDEX IF NOT EXISTS members_by_user ON members(user_id);
CREATE TABLE IF NOT EXISTS conversation_keys (
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    wrapped TEXT NOT NULL,
    sender_key TEXT NOT NULL,
    PRIMARY KEY (conversation_id, user_id, version)
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    sender_id TEXT REFERENCES users(id) ON DELETE SET NULL,
    sender_name TEXT NOT NULL,
    body TEXT NOT NULL,
    encrypted INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1,
    ts REAL NOT NULL,
    removed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS chat_messages_by_conversation ON chat_messages(conversation_id, id);
CREATE TABLE IF NOT EXISTS announcements (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    audience TEXT NOT NULL,
    audience_id TEXT NOT NULL DEFAULT '',
    author_id TEXT REFERENCES users(id) ON DELETE SET NULL,
    author_name TEXT NOT NULL,
    created REAL NOT NULL,
    expires REAL,
    pinned INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS announcements_by_time ON announcements(created);
CREATE TABLE IF NOT EXISTS announcement_reads (
    announcement_id TEXT NOT NULL REFERENCES announcements(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ts REAL NOT NULL,
    PRIMARY KEY (announcement_id, user_id)
);
""",
)
