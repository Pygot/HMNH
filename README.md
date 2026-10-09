<!-- README.md -->
# Hiring workspace with a person research agent

A self hosted workspace for hiring teams. At its core is an agent that researches a person for a stated goal (hiring, sales or due diligence) and returns a short report in which every claim has a source URL. It works from public, professional information only, asks you which profile is the right one when it is unsure, and treats profiles that look too good to be true with extra scepticism. Around it sit the tools a team needs: accounts with roles and permissions, a candidate database with hiring history, optional email sending, routines that keep searching until someone fits, optional finance data, a dashboard and a documented API, import and export of everything, and secure team messaging with optional end to end encryption.

Apify does the heavy lifting: Apify Actors read LinkedIn, Facebook and Instagram profiles and can also run the Google search that discovers them. The agent never requests those sites directly.

Everything beyond the research agent is optional and can be switched off by an administrator.

## Quick start

```bash
uv sync
uv run agent serve
```

That is all. You do not create a `.env` file by hand:

1. The first start prints a setup link in the terminal. Open it and create the first account. It becomes the administrator.
2. The next page asks for the connections you want: a language model, an Apify token, a search provider, voice, email. Each one is optional, each is checked when you save it, and the values are written to a private settings file for you.
3. Prefer the terminal? Run `uv run agent init` and answer the questions. Secrets are typed without echo.

Without any connection the workspace still starts. You can manage people, roles and settings, and the pages tell you what is missing before searches can run.

Command line research works too:

```bash
uv run agent status
uv run agent research --goal hiring --name "Jan Novak" --city Brno --employer Acme --confirm-lawful-purpose
uv run agent skill-search --goal hiring --skill Rust --location Brno --confirm-lawful-purpose
```

## Connections

Open Administration, Connections (or use `agent init`). Nothing here is mandatory.

- Language model: Anthropic, OpenAI or an OpenAI compatible address, a local Ollama, your Claude login, or only your Apify token. With `LLM_PROVIDER=claude` no key is needed: the agent runs the `claude` command of Claude Code on the same computer, which must be installed and signed in (`claude login`). Each call is a non-interactive `claude -p` with all tools turned off, so nothing can run on your machine, and `LLM_MODEL` is optional. With only `APIFY_TOKEN` the agent uses the AI models of your Apify account through `https://openrouter.apify.actor/api/v1`. Apify documents that gateway for use from inside the Apify platform, so it may refuse calls from your own server. The check on the Connections page tells you plainly, and a key from another provider always works.
- Apify token: profiles of LinkedIn, Facebook and Instagram, Google search, and the optional AI gateway.
- Search: Brave Search or Google through Apify.
- Voice: an ElevenLabs key.
- Email: an SMTP server for sign in codes, invitations and the messages you approve.

How values are stored and applied:

- They go to the private settings file (`.env`, or the file named by `AGENT_ENV_FILE`). It is written atomically, with owner only permissions, never through a link, and only for a fixed list of keys. Line breaks and `${` in values are refused.
- Secret values are never shown again. A saved secret is marked as saved, and an empty field keeps it.
- A change is validated before anything is written. The services restart in place once running searches finish. A value that is also set in the server environment wins, and the page says so.
- The file is plain text on your disk, like any `.env`. On Windows file permissions cannot hide it from other administrators of the machine.
- You can still use environment variables or a hand written `.env` as before. Real environment variables win over the file.

| Variable | Purpose |
| --- | --- |
| LLM_API_KEY, LLM_PROVIDER, LLM_MODEL, LLM_BASE_URL | Language model |
| LLM_OLLAMA_SCHEME, LLM_OLLAMA_HOST, LLM_OLLAMA_PORT | Local Ollama server |
| SEARCH_PROVIDER, BRAVE_API_KEY | Search provider |
| APIFY_TOKEN | Apify Actors, search, and optionally the AI gateway |
| ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID | Voice |
| SMTP_HOST, SMTP_PORT, SMTP_SECURITY, SMTP_USERNAME, SMTP_PASSWORD, SMTP_FROM, SMTP_FROM_NAME | Email |
| AGENT_CONTACT, OUTPUT_DIR | User agent contact and export folder |
| WEB_ACCESS_TOKEN, WEB_API_TOKEN | Optional shared sign in token and shared API token |
| WEB_COOKIE_SECURE, WEB_PUBLIC_ORIGIN, WEB_ALLOWED_HOSTS | Web deployment |
| STORE__PATH, STORE__RETENTION_DAYS | Database file and how long chats and reports are kept |
| AUTH__KEY_FILE | Where the server key for encrypted values is kept |

## Accounts, roles and permissions

- Sign in with a username and password. Passwords are hashed with scrypt, checked against a weak password list, and verified in constant time. Unknown users cost the same time as known ones.
- Optional for every account: a one time sign in link (temp link), an emailed code, and an authenticator app (TOTP) with recovery codes. An administrator can require an authenticator for everyone.
- People are invited with a link that lets them choose their own password. Administrators can also create a password reset link or a one time sign in link, shown once.
- Your account page shows where you are signed in and lets you sign out elsewhere, change your password, confirm your email and make API keys.
- Roles are named sets of permissions. Built in roles: admin, manager, recruiter, viewer, finance, service. Administrators edit them or make new ones, in a matrix grouped by area.
- Every page, button and API call checks the permission, and anything a role may not use is hidden. A missing permission looks like a missing page. Nobody can grant a permission they do not hold, edit a role or an account above their own, or change their own role. The last administrator cannot be removed.
- Changing a role, status or password ends the old sessions at once, so a removed permission does not linger.
- Every sign in, denial, change and export is written to an audit log with address and outcome, never with secrets. It can be filtered and downloaded as CSV.
- Administration, Security switches modules on and off for everyone: password sign in, sign in links, required authenticator, email codes, API keys, candidates, researched people memory, routines, finance, sending email, messages, private messages.

Permission groups: chat and research, candidates, emails, automation, messages, insights, data, assistant, workspace, people and access, API.

## Candidates and hiring history

- Every finished search adds or updates a candidate with the rating, the profile links and a timeline, so you see when you already know someone. A report says "Seen before" with the earlier ratings and any hires.
- Add people by hand, edit them, tag them, keep notes and move them through statuses. Candidates who were never hired are removed after a year by default (`CANDIDATES__RETENTION_DAYS`).
- Recording a hire keeps the role, department, start date and the email and phone the person gave you, saved as contact details. Later you record how it went: outcome, dates, a one to five rating and a review. Past hires show on every future report of the same person.
- Contact details come only from what you or the candidate provided. The agent never scrapes private contact details. Contacts need their own permission.
- A candidate can be exported or erased (with all contacts, hires and history) from their page.
- Keep records to what matters for hiring. Do not store health, beliefs, family or other sensitive details.

## Sending email

Drafts stay text by default. Sending is optional: connect SMTP, then an administrator switches on Sending email under Security.

- Each message is composed on its own page with the draft filled in, and nothing leaves until you tick that you reviewed it. There is no bulk send. Team messages from routines go only to the addresses you listed.
- Outreach messages end with an opt out line. Addresses on the do not contact list (case and plus tags are ignored) are never written to. Add anyone who asks to stop.
- Limits: a few messages per person per hour and a global hourly cap. Header injection is impossible because subjects are single line and addresses are validated.
- Every message is recorded in the sent mail log with who sent it and the result. The sender sees only the draft they are allowed to see.

## Routines

When a skill search finds nobody perfect, choose Keep looking on the report. A routine repeats the search on a schedule until a candidate meets your rule (a minimum rating, how many people, optionally every must have requirement), then it stops and tells you.

- Choose the interval, the maximum number of runs, and an optional list of people to email when it is satisfied, used up or failing.
- Cost and safety limits: a daily cap on runs, one run at a time by default, a limit on active routines, the same compliance checks as the chat on every run, and a stop after repeated failures. Interrupted runs are retried after a restart.
- Routines are created by confirming a lawful purpose. Imported routines arrive paused.

## Finance (optional)

An administrator can switch on revenue and finance data. Add revenue and expense entries with an optional category of your own, see monthly charts, breakdowns by category and the full history with filters, change the currency label, and export it all. Amounts are stored as exact integers. Charts are drawn on the server as plain SVG in black, white and grey, with shapes and labels instead of colours.

## Dashboard and API

The dashboard shows your own numbers in one place: searches, candidates, hires, routines, email, team and finance. Each part appears only if your role may read it. Pick a period, and save any chart as an SVG file.

The API mirrors this. Create a personal API key under Account (it is shown once and can never do more than you can, or less if you narrow it), then:

```bash
curl http://localhost:8000/api/v1/me -H "Authorization: Bearer $API_KEY"
curl http://localhost:8000/api/v1/stats/candidates -H "Authorization: Bearer $API_KEY"
curl "http://localhost:8000/api/v1/candidates?q=rust&size=10" -H "Authorization: Bearer $API_KEY"
```

- Resources: me, stats (by category, with SVG charts), candidates with hires and contacts, routines, finance, announcements, audit, users, roles, export and import, plus everything the first version offered (research, skill search, jobs, answers, reports, events, email templates and drafts, voice).
- Permission aware: the OpenAPI document at `/api/v1/openapi.json` and the API page list only what the caller may use. A call without permission gets a clear 403 and is audited.
- Lists are paged (`page`, `size`), errors have one shape, requests are rate limited per credential and per address, and failed attempts lock an address out for a while (a correct key is never turned away because of someone else's failures). Every successful write is recorded in the audit log with the key that made it.
- Keys stop working when the password changes or is reset, when two step sign in is removed, and when someone signs out everywhere. Creating a key asks for your password, and administrators with the permission can see and revoke other people's keys on the account page of that person.
- `WEB_API_TOKEN` still works as a shared credential with the permissions of the service role. Searches started with a key appear in the shared workspace, searches started with the shared token stay private to it.

## Import and export

Administration, Export and Import, or the API. Everything can be backed up or moved to another server.

- Export a JSON bundle of the datasets your role may read (candidates with links and history, contacts, hires, finance, routines, templates, do not contact list, hiring context, saved reports, accounts without passwords, roles, audit log, mail log) or one table as CSV. CSV cells that look like formulas are quoted.
- Import the same JSON, or CSV for candidates, contacts, hires, finance and the do not contact list. You see exactly what would be added or updated first, and nothing is saved unless every row passes. Importing is all or nothing, repeatable without duplicates, and each dataset needs its own edit permission.
- Accounts, roles, the audit log, mail log and saved reports are export only. Messages are never exported.

## Messages and announcements

Direct messages, a channel for each role, groups with owners, and announcements to everyone, a role or a group.

- Messages are encrypted on the server before they are stored. That protects a copied database file, but whoever controls the running server, including its administrators, can in principle read them.
- Private conversations (direct or group) are end to end encrypted in the browser. You choose a messaging passphrase that never leaves your browser. Your private key is wrapped with it (PBKDF2 with at least 600000 rounds, enforced by the browser) and only the wrapped copy is stored. Each conversation has a random key that is wrapped separately for every member with a shared secret derived from their keys, and bound to the conversation. The server stores only scrambled text and cannot read it.
- Changing who is in a private group always makes a new key. People added later cannot read what was said before. Someone who is removed, or who leaves, keeps what they already read but gets nothing new once a remaining member uses Change the key (it is offered in every private conversation, and after someone can no longer sign in). Resetting your keys needs your account password.
- The browser checks what it can without trusting the server: your own public key must match your private key, other people's keys are pinned on first sight and a change is shown with the new fingerprint and needs your explicit confirmation, a conversation that was private is never silently downgraded to plain text, plain or repeated messages inside a private conversation are flagged and ignored, and the key version can only go forward. Each conversation lists everyone's fingerprint so you can compare them in person or by phone.
- What this protects: a database leak, a stolen backup, a curious administrator, and a server that stores or logs messages. What it does not: a server that is fully taken over, because it also delivers the code your browser runs and could swap keys the first time two people talk (comparing fingerprints out of band is the defence), a compromised or shared browser while your key is unlocked, members who copy messages, a member who colludes with a hostile server to forge a colleague inside a group (there are no per message signatures), and the fact that the server knows who talks to whom, when, how much, and the names of groups. The server could also withhold messages.
- Role channels and announcements are not end to end encrypted, because their audience changes with roles.
- Your unlocked key stays in the browser tab only (session storage), is bound to the signed in account, is dropped when you sign out from any page and locks again after 30 idle minutes (`MESSAGES__UNLOCK_MINUTES`). If you forget the passphrase nobody can recover your private conversations. Resetting your keys makes old private conversations unreadable for you until someone changes the key.
- Messages are limited in size and rate, removed messages are wiped, retention can be set (`MESSAGES__RETENTION_DAYS`), and people without the messaging permission see nothing of it.

## Look and feel

Monochrome gray, black and white with soft corners, no gradients and no neon. A slow, quiet animated background (a few drifting patches of light with a touch of black) is built from compositor friendly transforms only, pauses when the tab is hidden and stays still if your device asks for less motion. Turn it off under Settings, Appearance. Everything fits one screen on normal displays and works from phones to wide screens.

## What the research agent does

1. Parses the input into an identity: a name or nickname with optional city, employers, roles and skills, or a PDF or DOCX CV.
2. Optionally reads your company's public pages to learn its category and products, so candidates are rated in that context.
3. Finds candidate profiles with a search API (Brave Search API or the Apify Google Search Actor).
4. Reads LinkedIn, Facebook and Instagram profiles only through Apify Actors, and the open web (portfolio, GitHub, publications, talks) through robots.txt respecting fetches.
5. Scores every candidate against the identity. If the top two are close, or the best is below the threshold, it stops and asks you.
6. Summarises each source, merges the claims and tags every finding as supported (two or more independent sources), single-source or uncertain.
7. Checks your candidate requirements against the findings: each one is met, partial, unmet or unknown, with the evidence it relies on.
8. Rates the person from 0 to 5 on skill fit, evidence of work, recency and consistency, each with a one line reason.
9. Applies scepticism: warning signs such as a very high rating with little corroboration, overlapping roles, promotional wording or long award lists lower the rating and are explained.
10. Exports Markdown and JSON with the company profile, the requirements, a sources list and a limitations section.

A requirement is written as `kind:label[:level[:years[:priority]]]`. Kinds are language, skill, experience, education, certification, location, industry and custom. Priority is must (the default) or nice. Requirements about protected or sensitive attributes are refused.

## Using Apify

The agent runs these Actors synchronously with your token and caps the cost of every run:

| Purpose | Default Actor | Setting |
| --- | --- | --- |
| Google search for profiles and pages | apify/google-search-scraper | APIFY_GOOGLE_ACTOR |
| LinkedIn profiles, no cookies | harvestapi/linkedin-profile-scraper | APIFY_LINKEDIN_ACTOR |
| Facebook pages | apify/facebook-pages-scraper | APIFY_FACEBOOK_ACTOR |
| Instagram profiles | apify/instagram-profile-scraper | APIFY_INSTAGRAM_ACTOR |

- Each run is limited by `maxTotalChargeUsd` (setting APIFY_MAX_CHARGE_USD, default 0.5, the minimum Apify accepts).
- Email search is not requested from the LinkedIn Actor, and contact details are discarded from every Actor output.
- Private Instagram profiles are skipped, not bypassed.
- `agent status`, the Connections page and Settings show how much of your monthly Apify usage is left.

## Apify Actor

`.actor/` makes this repository a public Apify Actor (`hmnh-person-research`): the input form, output view, store text and a Dockerfile are there, and the entry point is `python -m agent.actor`. On the platform `APIFY_TOKEN` is set, so the Actor uses the AI models and the scrapers of the account that runs it, or a key given in the input. To publish it, install the Apify CLI, run `apify login`, then `apify push` in this folder. Tests do not need the Apify SDK.

## Voice

Set an ElevenLabs key to switch voice on. Without a key nothing voice related is shown or reachable. Speech to text records up to 30 seconds in the chat and Tie's box. Text to speech reads messages and a short report summary, built on the server from stored text, never taken from the browser. Recordings and spoken text are sent to ElevenLabs and not stored. Voice requests are rate limited and the key never reaches the browser.

## Tie, the focus buddy

Tie is a small animated necktie at the bottom of the left rail. Talk to him to brainstorm, plan a search or weigh a candidate. He nudges you only when something needs you, keeps an optional focus timer, and respects the reduced motion preference. Nudges can be off, text or voice, and every line and timing is configuration under `BUDDY__`.

## Live view

Everything the agent finds streams into the chat as it happens, as a log or as a growing graph of sources, findings and requirements. A finished report keeps an Activity tab that replays it. The feed is a standard server sent events stream that resumes after a dropped connection.

## Security

- Strict content security policy with a nonce per response, CSRF tokens on every form and a header token on every JSON call, same origin checks, hardened cookies, host allow list, request size limits and rate limits. The server refuses to listen beyond loopback with insecure cookies.
- Sessions are rotated at sign in, bound to the account version, and checked against the database on every request, so a role change or a disabled account takes effect immediately. Staged sessions keep half signed in people out of everything.
- Single use links, codes and recovery codes are redeemed atomically. Authenticator codes cannot be replayed. Secrets are compared in constant time and stored only as hashes, or encrypted with the server key.
- Authorization is checked where the work is done, inside the same database transaction, so a membership or role change cannot slip between the check and the action.
- Secrets never go to logs, the audit log, exports or error messages. Link tokens are masked in server logs.
- Outbound page fetches refuse private addresses, unusual ports and redirects into them, check the address that was actually connected, ignore proxy environment variables, only decode a short list of text encodings, and stop after a total time.
- Sign in attempts are limited by address and by account, and only failures count, so one person cannot lock another out and a correct password is never refused because of someone else. Behind a reverse proxy set `WEB_TRUSTED_PROXIES` (comma separated addresses of the proxies); without it every visitor looks like the proxy. IPv6 clients are grouped by network.
- Changing what the workspace connects to needs your password again. A saved secret is cleared when its service address changes, the email server and the sign in rules can only be changed by an administrator who can manage everything, and a settings change that fails to start is rolled back.
- Mail is rate limited per recipient, honours the do not contact list even with plus tags or comments in addresses, and the people who sent a request are re-checked after their form arrives, so access removed in the meantime takes effect.
- Known limits: with required two step sign in, whoever knows the password first can enrol the authenticator, so send invitations and password resets only to the right person; the server key sits next to the database, so keep backups of the two apart; the setup code is printed once in the console at first start.
- Search engines are told to stay away from everything: every response, including the sign in page, the API and static files, carries `X-Robots-Tag: noindex, nofollow, noarchive`, every page has a robots meta tag, and `/robots.txt` disallows all. This does not replace a firewall or TLS if you expose the port to the internet.

## Stored data

Chats, reports, candidates, finance data, messages and settings live in an SQLite database on your machine (`data/agent.db`, `STORE__PATH` changes it, the folder is ignored by git). The server key for encrypted values is in `data/secret.key` (`AUTH__KEY_FILE`). Back both up and keep the key apart from copies of the database. Nothing is uploaded except what the connections you chose need.

- Display choices (theme, density, background, live view, Tie mode, research defaults) stay in a cookie in your browser.
- Chats and reports older than 90 days are removed at start up (`STORE__RETENTION_DAYS`). Reports describe real people, so keep this short.
- Settings, Your data deletes chats and reports, your remembered context, or everything. Single chats, reports and candidates can be deleted too.

## Configuration

Every tunable value lives in `src/agent/config.py` and can be overridden with an environment variable or a line in `.env`. Nested values use a double underscore:

```bash
SCORING__THRESHOLD=0.8
ROUTINES__MAX_RUNS_PER_DAY=40
AUTH__POLICY__REQUIRE_TOTP=true
MAIL__SEND_PER_HOUR=30
MESSAGES__KDF_ITERATIONS=800000
FINANCE__CURRENCY=EUR
```

Web server values use the WEB_ prefix, for example WEB_PORT, WEB_ALLOWED_HOSTS and WEB_COOKIE_SECURE. Roles can be defined from the start with `AUTH__ROLES`, and the security switches have defaults under `AUTH__POLICY__`.

## Compliance

- Only public, professional information relevant to the stated goal is collected.
- Sensitive attributes, private contact details and addresses are never reported or scraped.
- You confirm a lawful purpose before every run by saying yes to the plan the agent proposes, and a notice reminds you that the person may need to be informed, for example under GDPR. Routines carry the same confirmation.
- Requests that look like stalking, harassment or locating a private individual are refused, and so are requirements that rely on protected or sensitive attributes.
- Candidate records can be exported and erased on request, and unhired candidates expire. Outreach emails carry an opt out line and honour a do not contact list.
- robots.txt is respected and the tool identifies itself honestly in its user agent.
- Output is decision support. A human must review it before acting.

## Development

```bash
uv run pytest
uv format
uv run ruff check .
```

Tests use mocked search, LLM, Apify, voice and mail responses and never touch the network. A test checks that every file follows the code rules: path comment on the first line, no other comments, import order and globals on top. Another checks that every route declares who may use it.
