"""risk.py — does a permission request deserve a different warning?

`rm -rf` and `ls` used to be announced identically: the permission phrase
was built from the tool name alone. permission_risk() looks at the command.

Three tiers, cheapest first:

  1. Local rules (DESTRUCTIVE) — obviously destructive shapes: any rm, a
     force-push, reset --hard, DROP TABLE through a database client, and so
     on. Instant, no network, and final: the remote model cannot talk a
     local verdict down.
  2. Local read-only check — a command whose every segment is a known
     read-only program is harmless and never leaves the machine.
  3. Everything else goes to the decider (backend "jev" only), REDACTED:
     heredoc bodies and long quoted text dropped, and URLs, hosts, IPs,
     emails, secrets, long tokens and every path but its last component
     replaced. What remains is program names, flags and file names.

Rules match the command with quoted text and heredoc bodies blanked out, so
`pgrep -fl 'curl.*|bash'` or an `echo` of documentation does not trip them —
except code handed to an interpreter with -c / -e, which is exactly where a
`bash -c "rm -rf …"` hides. Measured on 10,760 real Bash commands before
the rules shipped; see the notes on each for what they got wrong first.
"""

import re

# (reason, pattern). The reason completes "Careful — this one …".
_DESTRUCTIVE = [
    # Any rm. It is irreversible whatever its flags; -rf is just the loud one.
    ("deletes files", r"(?:^|[;&|(\s'\"])(?:sudo\s+)?rm\s"),
    ("deletes files", r"\bfind\b[^|;&]*\s-delete\b|\bfind\b[^|;&]*-exec\s+rm\b|\bxargs\s+(?:-\S+\s+)*rm\b"
                      r"|\brsync\b[^|;&]*\s--delete\b"
                      r"|\bshutil\.rmtree\(|\bos\.(?:remove|unlink|rmdir|removedirs)\(|\.unlink\("),
    ("force-pushes", r"\bgit\s+push\b[^|;&]*(?:\s--force(?:-with-lease)?\b|\s-f\b|\s\+\S)"),
    # Not `git restore --staged .`: that only unstages. It was all 108 of this
    # rule's hits in the first draft.
    ("throws away uncommitted work",
     r"\bgit\s+(?:reset\s+--hard|clean\s+-[A-Za-z]*f|checkout\s+--\s+\.|stash\s+(?:drop|clear)"
     r"|restore\s+(?!(?:-\S+\s+)*--staged\s)(?:-\S+\s+)*\.(?:\s|$))"),
    ("deletes a branch", r"\bgit\s+(?:branch\s+(?:-\S+\s+)*-D\b|push\s+[^|;&]*--delete\b|push\s+\S+\s+:\S"
                         r"|tag\s+-d\b)"),
    ("rewrites history", r"\bgit\s+(?:filter-branch|filter-repo|update-ref\s+-d)"),
    ("overwrites a disk", r"\bdd\b[^|;&]*\bof=|\bmkfs\b|\bdiskutil\s+(?:erase|zero|secureErase)"),
    ("changes permissions on everything", r"\bch(?:mod|own)\s+-R\b"),
    ("deletes cloud resources",
     r"\bkubectl\s+delete\b|\bterraform\s+destroy\b|\baws\s+s3\s+(?:rm\b[^|;&]*--recursive|rb\b)"
     r"|\bgcloud\b[^|;&]*\bdelete\b|\bgh\s+repo\s+delete\b|\bheroku\s+(?:apps:destroy|pg:reset)"),
    ("prunes Docker", r"\bdocker\s+(?:system|volume|image)\s+prune\b|\bdocker\s+rm\s+-f\b"),
    ("runs a script from the internet", r"\b(?:curl|wget)\b[^|;&]*\|\s*(?:sudo\s+)?(?:ba|z)?sh\b"),
    ("runs as root", r"(?:^|[;&|(\s'\"])sudo\s"),
    ("kills processes", r"\bkillall\b|\bpkill\s+-9\b"),
    ("clears the app's data", r"\badb\b[^|;&]*\b(?:uninstall|pm\s+clear)\b"),
]
_DESTRUCTIVE = [(reason, re.compile(p)) for reason, p in _DESTRUCTIVE]

# SQL only through a database client: the first draft flagged a Python
# script doing string replacement on the text "DELETE FROM".
_DB_CLIENT = re.compile(r"\b(?:psql|mysql|mariadb|sqlite3|clickhouse(?:-client)?|bq|snowsql|cqlsh|mongosh?)\b")
_SQL_DESTRUCTIVE = re.compile(
    r"(?i)\b(?:drop\s+(?:table|database|schema|collection)|truncate\s+(?:table\s+)?\w+"
    r"|delete\s+from\s+\w+\s*(?:;|$|['\"]))|\.drop\(\)")

_HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n\s*\2\s*(?:\n|$)", re.S)
# A heredoc an interpreter reads is code; one `cat` writes to a file is data.
_INTERPRETER_BEFORE = re.compile(r"(?:\b(?:ba|z)?sh|\bpython3?|\bnode|\bperl|\bruby|\bpsql|\bsqlite3"
                                 r"|\bmysql)\b[^\n|;&]*$")
_QUOTED = re.compile(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"")
_INTERPRETER_ARG = re.compile(r"(?:\b(?:ba|z)?sh|\bpsql|\bsqlite3|\bmysql|\bpython3?|\bnode|\bperl|\bruby)"
                              r"\s+(?:\S+\s+)*?-[ce]\s*$")


def _code_only(command):
    """The command with heredoc bodies and quoted text blanked, except a
    quoted argument handed to an interpreter via -c / -e, which is code."""
    def heredoc(m):
        line_start = command.rfind("\n", 0, m.start()) + 1
        return m.group(0) if _INTERPRETER_BEFORE.search(command[line_start:m.start()]) else "<<heredoc\n"
    text = _HEREDOC.sub(heredoc, command)
    out, last = [], 0
    for m in _QUOTED.finditer(text):
        out.append(text[last:m.start()])
        before = text[max(0, m.start() - 80):m.start()]
        out.append(m.group(0) if _INTERPRETER_ARG.search(before) else "''")
        last = m.end()
    out.append(text[last:])
    return "".join(out)


def local_risk(command):
    """Why this command is destructive, as a clause, or None."""
    if not command:
        return None
    code = _code_only(command)
    for reason, rx in _DESTRUCTIVE:
        if rx.search(code):
            return reason
    if _DB_CLIENT.search(code) and _SQL_DESTRUCTIVE.search(command):
        return "deletes data"
    return None


_READ_ONLY = {
    "cd", "ls", "cat", "head", "tail", "wc", "grep", "rg", "egrep", "echo", "printf", "pwd",
    "which", "type", "file", "stat", "du", "df", "sort", "uniq", "cut", "tr", "jq", "tree",
    "date", "ps", "sleep", "true", "diff", "basename", "dirname", "realpath", "column", "nl",
    "comm", "cmp", "shasum", "md5", "test", "[", "lsof", "uname", "whoami", "id",
}
_GIT_READ = {
    "status", "log", "diff", "show", "rev-parse", "ls-files", "blame", "branch", "remote",
    "describe", "shortlog", "reflog", "rev-list", "cat-file", "grep",
}
_SEGMENT = re.compile(r"\s*(?:&&|\|\||;|\||\n)\s*")
_WRITES = re.compile(r"(?<![0-9&])>{1,2}\s*(?!/dev/null|&)\S|\btee\b|\bsed\s+-i|\bfind\b.*-(?:delete|exec)")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def is_read_only(command):
    """True only if every segment runs a known read-only program and nothing
    writes. Strict on purpose: a destructive command wrongly let through here
    loses its warning, while a harmless one wrongly kept out costs one
    decider call."""
    code = _code_only(command or "")
    if not code.strip() or _WRITES.search(code) or "$(" in code or "`" in code or "<<" in code:
        return False
    for segment in _SEGMENT.split(code):
        words = [w for w in segment.split() if not _ASSIGNMENT.match(w)]
        if not words:
            continue
        program = words[0].rsplit("/", 1)[-1]
        if program == "git":
            sub = next((w for w in words[1:] if not w.startswith("-")), "")
            if sub not in _GIT_READ:
                return False
            if sub == "branch" and any(w in ("-D", "-d", "-m", "-M", "--delete") for w in words):
                return False
        elif program not in _READ_ONLY:
            return False
    return True


# ── Redaction: what may leave the machine ──────────────────────────────────

_TLDS = r"(?:com|net|org|io|dev|ai|co|app|cloud|internal|local|lan|corp|xyz|us|uk|de|eu)"
_REDACTIONS = [
    (re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://\S+"), "<url>"),
    (re.compile(r"(?i)\bbearer\s+[^\s'\"]+"), "Bearer <secret>"),
    (re.compile(r"(?i)\b(authorization|proxy-authorization|x-api-key|api-key|cookie|x-auth-token)\s*:\s*[^'\"\n]+"),
     r"\1: <secret>"),
    (re.compile(r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|PASS|PWD|API_?KEY|ACCESS_?KEY"
                r"|PRIVATE_?KEY|AUTH|CREDENTIALS?|SESSION|COOKIE)[A-Z0-9_]*)=(\"[^\"]*\"|'[^']*'|\S+)"),
     r"\1=<secret>"),
    (re.compile(r"(?i)(--?(?:token|password|passwd|pass|secret|api-?key|key|auth|access-key"
                r"|private-key|client-secret)(?:=|\s+))(\"[^\"]*\"|'[^']*'|\S+)"), r"\1<secret>"),
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}|\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_\w{20,}"
                r"|\bxox[abprs]-[\w-]{10,}|\bAKIA[0-9A-Z]{16}\b|\bAIza[\w-]{30,}|\beyJ[\w-]+\.[\w-]+\.[\w-]+"),
     "<secret>"),
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "<email>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"(?i)\b(?:[a-z0-9-]+\.)+" + _TLDS + r"\b(?::\d+)?"), "<host>"),
    (re.compile(r"\b[0-9a-f]{24,}\b|\b[A-Za-z0-9+/_-]{32,}={0,2}"), "<token>"),
    # Any path keeps only its last component, which says what is touched;
    # the directories above it say whose machine and which project.
    (re.compile(r"(?:~|\$\{?\w+\}?)?(?:\.{0,2}/)?(?:[\w.@%+-]+/)+([\w.@%+*-]+)"), r"<path>/\1"),
]
_LONG_QUOTED = re.compile(r"'[^']{60,}'|\"(?:[^\"\\]|\\.){60,}\"")


def redact(command, limit=600):
    """The command as it may be shown to a third party. 600 characters
    covers 99% of real commands once redacted; truncating at 300 first hid
    the tail, which is where a destructive step in a compound command sits.
    Heredoc bodies are always dropped here, even ones an interpreter reads:
    a script can contain anything, so it never leaves the machine."""
    text = _HEREDOC.sub("<<heredoc\n", command or "")
    text = _LONG_QUOTED.sub("<text>", text)
    for rx, replacement in _REDACTIONS:
        text = rx.sub(replacement, text)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit] + " …"


# ── The decider, for what the local rules cannot place ────────────────────

_CRITERIA = {
    "reads": "only reads, inspects, searches or prints; changes nothing",
    "reversible": "changes files, builds, installs, commits or restarts things in routine ways "
                  "that are easy to undo",
    "destructive": "deletes, overwrites, force-pushes, discards work or data, or changes shared "
                   "systems in a way that is hard or impossible to undo",
}


def remote_destructive_probability(command, config):
    """P(destructive) for a redacted command, per the decider, or None for
    no opinion (backend local, any failure, or no probability returned)."""
    try:
        import decider
    except ImportError:
        return None
    state = ("A coding agent is asking permission to run this shell command. Paths, hosts, "
             "URLs and secrets are redacted.\n\n" + redact(command))
    result = decider.choice(
        state,
        "Decide what running this command would do to the user's files, repositories, "
        "machine or services.",
        _CRITERIA,
        config=config,
    )
    if result is None:
        return None
    try:
        return float(result.probs["destructive"])
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


# Measured on the same corpus: the 40 real commands that would go to the
# decider peaked at 0.12, read-only ones at 0.09, while ten destructive
# commands the local rules miss (prisma migrate reset, redis-cli FLUSHALL,
# helm uninstall, supabase db reset, ...) scored 0.83 to 1.00.
PERMISSION_RISK_MIN = 0.5


def permission_risk(tool_name, tool_input, config=None):
    """Why this permission request is destructive, as a clause completing
    "Careful — this one ...", or None for an ordinary one. Bash only."""
    if tool_name != "Bash" or not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    reason = local_risk(command)
    if reason:
        return reason
    if is_read_only(command):
        return None
    if config is None:
        from home import load_config
        config = load_config()
    p = remote_destructive_probability(command, config)
    minimum = (config.get("decider") or {}).get("permission_risk_min", PERMISSION_RISK_MIN)
    if p is not None and p >= minimum:
        return "looks hard to undo"
    return None
