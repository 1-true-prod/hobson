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
  3. Everything else goes to the decider (backend "jev" only) as a
     decider.Command, which the decider redacts before it leaves: heredoc
     bodies and long quoted text dropped, and URLs, hosts, IPs, emails,
     secrets, long tokens and every path but its last component replaced.
     What remains is program names, flags and file names.

Rules match the command with quoted text and heredoc bodies blanked out, so
`pgrep -fl 'curl.*|bash'` or an `echo` of documentation does not trip them —
except code handed to an interpreter with -c / -e, which is exactly where a
`bash -c "rm -rf …"` hides, and command substitutions, which run wherever
they are written: `echo "$(rm -rf ~)"` is an rm. Measured on 10,760 real
Bash commands before the rules shipped; see the notes on each for what they
got wrong first. The substitution reading was replayed on 10,992: it flags
3 more (all Python heredocs ending in a `python -c` holding test strings)
and clears 91 fewer as read-only (a `$(git …)` in quotes), which costs a
decider call each, not a warning.
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

# Group 3 is the rest of the opening line, which is still code; group 4 the body.
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1([^\n]*)\n(.*?)\n\s*\2\s*(?:\n|$)", re.S)
# A heredoc an interpreter reads is code; one `cat` writes to a file is data.
_INTERPRETER_BEFORE = re.compile(r"(?:\b(?:ba|z)?sh|\bpython3?|\bnode|\bperl|\bruby|\bpsql|\bsqlite3"
                                 r"|\bmysql)\b[^\n|;&]*$")
_SHELL_BEFORE = re.compile(r"\b(?:ba|z)?sh\b[^\n|;&]*$")
_INTERPRETER_ARG = re.compile(r"(?:\b(?:ba|z)?sh|\bpsql|\bsqlite3|\bmysql|\bpython3?|\bnode|\bperl|\bruby)"
                              r"\s+(?:\S+\s+)*?-[ce]\s*$")
# Substitutions nested deeper than this are read as raw text: every level
# rescans what follows it, and 25,000 unclosed `$(` took 8 seconds.
_MAX_DEPTH = 20
# String literals in another language's source, and the stray quotes left over.
_QUOTED = re.compile(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"")
_STRAY_QUOTE = re.compile(r"['\"`]")


def _code_only(command):
    """What the shell would run, as one text every rule reads: quoted text
    and heredoc bodies blanked, except code handed to an interpreter (-c /
    -e, or a heredoc it reads), which is kept minus its string literals.

    Every command substitution is kept, rewritten as $(…), because it runs
    wherever it is written: inside double quotes and unquoted heredocs too.
    Blanking those whole is how `echo "$(rm -rf ~)"` passed as read-only.
    The rest of a heredoc's opening line is kept too: `cat <<EOF && rm -rf
    ~` runs the rm."""
    def heredoc(m):
        before = command[command.rfind("\n", 0, m.start()) + 1:m.start()]
        body = m.group(4)
        if _SHELL_BEFORE.search(before):
            kept = _blank_literals(body)
        elif _INTERPRETER_BEFORE.search(before):
            # Another language: its quotes are not the shell's, so none may
            # survive to pair with a quote outside the heredoc.
            kept = _STRAY_QUOTE.sub(" ", _QUOTED.sub(" _ ", body))
        else:
            kept = ""
        # With an unquoted delimiter the shell runs these before anything reads the body.
        runs = [] if m.group(1) else _expansions(body, 0, None)[0]
        return ("<<heredoc" + m.group(3) + "\n" + kept + "\n"
                + "".join("$(" + s + ")\n" for s in runs))
    try:
        return _blank_literals(_HEREDOC.sub(heredoc, command))
    except RecursionError:  # absurd nesting: let every rule see all of it
        return command


def _blank_literals(text, depth=0):
    """`text` with each quoted string blanked to '', followed by any
    substitution inside it, and each substitution, quoted or not, as $(…)
    with its own quoting read afresh."""
    if depth > _MAX_DEPTH:
        return text  # too deep to be real: let every rule see all of it
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            out.append(text[i:i + 2])
            i += 2
        elif c == "'" or text.startswith("$'", i):
            start = i
            i = (_close(text, i + 2, "'") if c == "$" else _close_single(text, i + 1)) + 1
            out.append(_quoted(text, start, i, [], depth))
        elif c == '"':
            start = i
            runs, close = _expansions(text, i + 1, '"')
            i = close + 1
            out.append(_quoted(text, start, i, runs, depth))
        elif text.startswith("$(", i):
            close = _close_paren(text, i + 2)
            out.append("$(" + _blank_literals(text[i + 2:close], depth + 1) + ")")
            i = close + 1
        elif c == "`":
            close = _close(text, i + 1, "`")
            out.append("$(" + _blank_literals(text[i + 1:close], depth + 1) + ")")
            i = close + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _quoted(text, start, end, runs, depth):
    """A quoted string as code: whole when handed to an interpreter, else
    blank, with the substitutions inside it still run."""
    if _INTERPRETER_ARG.search(text[max(0, start - 80):start]):
        return text[start:end]
    return "''" + "".join(" $(" + _blank_literals(s, depth + 1) + ")" for s in runs)


def _expansions(text, i, stop):
    """The substitutions in `text` from i where quotes are plain characters:
    inside double quotes (stop='"') or an unquoted heredoc body (stop=None).
    Returns their bodies and the index of `stop`, or len(text)."""
    runs, n = [], len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
        elif c == stop:
            return runs, i
        elif text.startswith("$(", i):
            close = _close_paren(text, i + 2)
            runs.append(text[i + 2:close])
            i = close + 1
        elif c == "`":
            close = _close(text, i + 1, "`")
            runs.append(text[i + 1:close])
            i = close + 1
        else:
            i += 1
    return runs, n


def _close_paren(text, i):
    """The index of the `)` closing a `$(` that ends at i, or len(text)."""
    depth, n = 1, len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "'":
            i = _close_single(text, i + 1)
        elif c == '"':
            i = _expansions(text, i + 1, '"')[1]
        elif c == "`":
            i = _close(text, i + 1, "`")
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if not depth:
                return i
        i += 1
    return n


def _close_single(text, i):
    """The closing ' of a single-quoted string (no escapes), or len(text)."""
    end = text.find("'", i)
    return len(text) if end < 0 else end


def _close(text, i, quote):
    """The unescaped `quote` closing a string that opened before i, or len(text)."""
    n = len(text)
    while i < n and text[i] != quote:
        i += 2 if text[i] == "\\" else 1
    return min(i, n)


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
    if (not code.strip() or _WRITES.search(code)
            or any(s in code for s in ("$(", "`", "<(", "<<"))):
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


# ── The decider, for what the local rules cannot place ────────────────────

_CRITERIA = {
    "reads": "only reads, inspects, searches or prints; changes nothing",
    "reversible": "changes files, builds, installs, commits or restarts things in routine ways "
                  "that are easy to undo",
    "destructive": "deletes, overwrites, force-pushes, discards work or data, or changes shared "
                   "systems in a way that is hard or impossible to undo",
}


def remote_destructive_probability(command, config):
    """P(destructive) for the command, per the decider (which redacts it),
    or None for no opinion (backend local, any failure, or no probability)."""
    try:
        import decider
    except ImportError:
        return None
    return decider.probability(
        "destructive",
        ["A coding agent is asking permission to run this shell command. Paths, hosts, "
         "URLs and secrets are redacted.\n\n", decider.Command(command)],
        "Decide what running this command would do to the user's files, repositories, "
        "machine or services.",
        _CRITERIA,
        config=config,
    )


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
