"""Allowlisted read-only commands for automatic private pages.

A command is one argv list checked here before it runs; nothing reaches a shell
unchecked. Outputs that look like secrets are withheld whole, never redacted.
Forms follow the tools' own docs (ssh(1), dpkg-query(1), systemctl(1),
nvidia-smi(1), git-status(1), docs.docker.com format references).
"""

from __future__ import annotations

import math
import re
import shlex
import socket
import subprocess
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from doclayer.settings import settings


def configured_read_roots() -> tuple[str, ...]:
    """Roots under which `head` may read (settings.private_read_roots, `/`-terminated)."""
    return tuple(settings.private_read_roots)


def read_roots_text() -> str:
    """The read roots for messages and prompts, without trailing slashes."""
    roots = [root.rstrip("/") for root in configured_read_roots()]
    return ", ".join(roots) if roots else "(no read roots configured)"


# Readable files: notes plus project build manifests. *.properties stays out: gradle.properties
# and local.properties commonly hold signing passwords and keys.
READ_SUFFIXES = (".md", ".txt", ".gradle", ".gradle.kts", ".toml")
READ_NAMES = ("package.json", "go.mod")
_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_META = re.compile(r"[|;&<>`\n\r]|\$\(|\$\{(?!(?:Package|Version|Status|binary:Package)\})")
_SECRET_WORDS = r"\.ssh|\.gnupg|credential|secret|token|(?:^|/)\.env|id_rsa|id_ed25519"
_SECRET_PATH = re.compile(_SECRET_WORDS, re.I)
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:-]*$")
_PATTERN = re.compile(r"^[A-Za-z0-9*][A-Za-z0-9._+:*-]*$")
_UNIT = re.compile(r"^[A-Za-z0-9@._*:-]+$")
_PROPS = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:,[A-Za-z][A-Za-z0-9]*)*$")
_PS_FIELDS = {"ID", "Image", "Names", "Status", "Ports", "CreatedAt", "RunningFor", "State", "HealthStatus"}
_INSPECT_FIELDS = {
    "Name",
    "Image",
    "Created",
    "RestartCount",
    "State.Status",
    "State.StartedAt",
    "Config.Image",
    "HostConfig.RestartPolicy.Name",
}
_TEMPLATE = re.compile(r"\{\{\s*\.([A-Za-z.]+)\s*\}\}")


def _path(value: str) -> str | None:
    if not value.startswith("/"):
        return "paths must be absolute"
    if ".." in value.split("/"):
        return "path traversal"
    return None


def _template(value: str, fields: set[str]) -> str | None:
    body = value.removeprefix("table ")
    if "{{" in _TEMPLATE.sub("", body) or not _TEMPLATE.search(body):
        return "docker format must use plain {{.Field}} references"
    bad = [f for f in _TEMPLATE.findall(body) if f not in fields]
    return f"docker field not allowed: {bad[0]}" if bad else None


def _names(args, pattern=_NAME):
    return None if args and all(pattern.match(a) for a in args) else "expected names"


def _dpkg(args):
    if not args or args[0] != "-W":
        return "dpkg-query requires -W"
    rest = args[1:]
    if rest and (rest[0].startswith("-f=") or rest[0].startswith("--showformat=")):
        rest = rest[1:]
    return _names(rest, _PATTERN)


def _systemctl(args):
    if args[:1] == ["--user"]:
        args = args[1:]
    if args[:1] in (["is-active"], ["is-enabled"]):
        return _names(args[1:], _UNIT)
    if args[:2] == ["list-units", "--no-pager"]:
        rest = [a for a in args[2:] if a != "--all"]
        return None if not rest else (_names(rest, _UNIT) if len(rest) == 1 else "one pattern")
    # systemctl(1): `cat` prints unit files and drop-ins; `show -p` prints the named
    # properties. Both only read. Unit file text still passes the secret scan.
    if args[:1] == ["cat"]:
        return _names(args[1:], _UNIT) if len(args) == 2 else "systemctl cat takes one unit"
    if args[:1] == ["show"] and len(args) == 4 and args[2] == "-p" and _PROPS.match(args[3]):
        return _names(args[1:2], _UNIT)
    return (
        "systemctl allows is-active, is-enabled, list-units --no-pager [--all] [PATTERN], "
        "cat UNIT or show UNIT -p PROP[,PROP]"
    )


# find(1) tests that only select and print paths. Actions (-exec, -execdir, -ok,
# -okdir, -delete), file writers (-fprint*, -fls) and formatters are never allowed.
_FIND_VALUE = re.compile(r"^[A-Za-z0-9._+:*?/\[\]@,-]+$")
_FIND_TYPES = {"f", "d", "l"}


def _find(args):
    paths = []
    while args and not args[0].startswith("-"):
        paths.append(args[0])
        args = args[1:]
    if not paths:
        return "find requires at least one absolute PATH before its tests"
    for path in paths:
        if _path(path):
            return _path(path)
    while args:
        option, value, args = args[0], args[1] if len(args) > 1 else None, args[2:]
        if value is None:
            return f"find {option} requires a value"
        if option == "-maxdepth":
            if not value.isdigit():
                return "find -maxdepth requires a number"
        elif option in ("-name", "-iname", "-path"):
            if value.startswith("-") or not _FIND_VALUE.match(value):
                return f"find {option} requires a plain glob"
        elif option == "-type":
            if value not in _FIND_TYPES:
                return "find -type allows f, d or l"
        elif option == "-mtime":
            if not re.fullmatch(r"[+-]?\d+", value):
                return "find -mtime requires [+|-]N"
        else:
            return f"find option not allowed: {option}"
    return None


def _ps(args):
    return (
        None
        if args in (["-eo", "pid,comm"], ["-eo", "pid,ppid,comm"])
        else "ps allows -eo pid,comm or -eo pid,ppid,comm"
    )


def _docker(args):
    if args[:1] == ["ps"]:
        rest = [a for a in args[1:] if a != "-a"]
        if len(rest) == 2 and rest[0] == "--format":
            return _template(rest[1], _PS_FIELDS)
        return "docker ps requires --format"
    rest = args[1:]
    if args[:1] == ["inspect"]:
        if rest[:2] == ["--type", "container"]:
            rest = rest[2:]
        if len(rest) == 3 and rest[0] == "-f":
            return _template(rest[1], _INSPECT_FIELDS) or _names(rest[2:])
    return "docker allows ps --format or inspect [--type container] -f"


def _git(args):
    if args[:1] == ["--no-optional-locks"]:
        locks, args = True, args[1:]
    else:
        locks = False
    if len(args) < 3 or args[0] != "-C":
        return "git requires -C DIR"
    if _path(args[1]):
        return _path(args[1])
    sub = args[2:]
    if (
        sub[0] == "rev-parse"
        and len(sub) >= 2
        and all(a in {"HEAD", "--short", "--abbrev-ref", "--show-toplevel"} for a in sub[1:])
        and ("HEAD" in sub or sub[1:] == ["--show-toplevel"])
    ):
        return None
    if sub[:3] == ["log", "--oneline", "-n"] and len(sub) == 4 and sub[3].isdigit() and int(sub[3]) <= 50:
        return None
    if sub == ["status", "--short"]:
        return None if locks else "git status requires --no-optional-locks (git-status(1) BACKGROUND REFRESH)"
    if sub == ["branch", "--show-current"]:
        return None
    return "git allows rev-parse HEAD, log --oneline -n N, status --short, branch --show-current"


def _head(args):
    if len(args) != 3 or args[0] != "-n" or not args[1].isdigit() or not 0 < int(args[1]) <= 200:
        return "head requires -n N (N <= 200) PATH"
    path = args[2]
    if _path(path):
        return _path(path)
    roots = configured_read_roots()
    if not roots or not path.startswith(roots):
        return "file reads only under " + read_roots_text()
    if not path.endswith(READ_SUFFIXES) and path.rsplit("/", 1)[-1] not in READ_NAMES:
        return "file reads only for .md, .txt, .gradle(.kts), .toml, package.json or go.mod"
    return None


def _nvidia(args):
    query = [a for a in args if a.startswith("--query-gpu=")]
    form = [a for a in args if a in ("--format=csv", "--format=csv,noheader")]
    return (
        None
        if len(args) == 2 and len(query) == 1 and len(form) == 1
        else "nvidia-smi allows --query-gpu=... --format=csv[,noheader]"
    )


def _findmnt(args):
    if args == []:
        return None
    if len(args) == 1:
        return _path(args[0])
    if len(args) == 2 and args[0] == "--target":
        return _path(args[1])
    return "findmnt allows [PATH] or --target PATH"


_FORMS = {
    "dpkg-query": _dpkg,
    "apt-cache": lambda a: _names(a[1:]) if a[:1] == ["policy"] else "apt-cache allows policy",
    "snap": lambda a: (
        None if a == ["list"] or (a[:1] == ["list"] and len(a) == 2 and _NAME.match(a[1])) else "snap allows list"
    ),
    "flatpak": lambda a: None if a == ["list"] else "flatpak allows list",
    "command": lambda a: None if len(a) == 2 and a[0] == "-v" and _NAME.match(a[1]) else "command allows -v NAME",
    "uname": lambda a: None if a == ["-a"] else "uname allows -a",
    "lsb_release": lambda a: None if a == ["-a"] else "lsb_release allows -a",
    "hostname": lambda a: None if a == [] else "hostname takes no arguments",
    "lsblk": lambda a: None if a == [] else "lsblk takes no arguments",
    "df": lambda a: (
        None if a == ["-h"] else (_path(a[1]) if a[:1] == ["-h"] and len(a) == 2 else "df allows -h [PATH]")
    ),
    "findmnt": _findmnt,
    "nvidia-smi": _nvidia,
    "systemctl": _systemctl,
    "docker": _docker,
    "git": _git,
    "ls": lambda a: _path(a[1]) if len(a) == 2 and a[0] == "-la" else "ls allows -la PATH",
    "stat": lambda a: _path(a[0]) if len(a) == 1 else "stat allows one PATH",
    "head": _head,
    "find": _find,
    "ps": _ps,
    "free": lambda a: None if a in (["-h"], ["-m"]) else "free allows -h or -m",
    "ss": lambda a: None if a in (["-ltn"], ["-lun"], ["-ltnu"]) else "ss allows -ltn, -lun or -ltnu",
    "id": lambda a: None if a == [] or (len(a) == 1 and _USER.match(a[0])) else "id allows [USER]",
    "groups": lambda a: None if a == [] or (len(a) == 1 and _USER.match(a[0])) else "groups allows [USER]",
}


def normalize(argv: list) -> list:
    """Planner habits made safe before the allowlist runs: a dpkg-query format with a
    real newline or tab becomes the backslash escape dpkg-query itself expands."""
    if not argv or argv[0] != "dpkg-query" or not all(isinstance(a, str) for a in argv):
        return argv
    return [a.replace("\n", "\\n").replace("\t", "\\t") if a.startswith(("-f=", "--showformat=")) else a for a in argv]


_BIN_DIRS = ("/usr/bin/", "/bin/", "/usr/sbin/", "/sbin/", "/usr/local/bin/", "/snap/bin/")


def resolved_binary(name: str, stdout: str) -> str | None:
    """The executable path `command -v NAME` printed, when it is a file in a system
    bin directory. Aliases, functions and builtins print no such path, so they never
    qualify for a version check, and the check runs the path, not the name."""
    lines = stdout.strip().splitlines()
    path = lines[0] if lines else ""
    if (
        path.startswith(_BIN_DIRS)
        and path.rsplit("/", 1)[-1] == name
        and not _META.search(path)
        and not any(c.isspace() for c in path)
    ):
        return path
    return None


def refusal(host: str, argv: list[str], *, hosts: list[str], resolved: set[str]) -> str | None:
    """None when (host, argv) is allowed; otherwise why it is refused."""
    if host not in hosts:
        return f"host {host!r} is not an allowed ssh alias"
    if not argv or not all(isinstance(a, str) and a for a in argv):
        return "argv must be non-empty strings"
    if any(_META.search(a) for a in argv):
        return "shell metacharacter"
    if any(_SECRET_PATH.search(a) for a in argv[1:]):
        return "secret-bearing path"
    command, args = argv[0], argv[1:]
    check = _FORMS.get(command)
    if check is None:
        if command in resolved and (args == ["--version"] or (command == "java" and args == ["-version"])):
            return None
        return f"{command} is not an allowed command"
    return check(args)


_PEM = re.compile(r"-----BEGIN [A-Z ]*KEY-----")
_ASSIGN = re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key)\w*[\"']?\s*[=:]\s*\S+")
_URL_CREDENTIALS = re.compile(r"[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@", re.I)
_RUN = re.compile(r"[A-Za-z0-9+/=_-]{32,}")


def _entropy(value: str) -> float:
    counts = Counter(value)
    return -sum(n / len(value) * math.log2(n / len(value)) for n in counts.values())


def possible_secret(text: str) -> bool:
    if _PEM.search(text) or _ASSIGN.search(text) or _URL_CREDENTIALS.search(text):
        return True
    for run in _RUN.findall(text):
        for part in run.split("/"):
            if (
                len(part) >= 32
                and re.search(r"[a-z]", part)
                and re.search(r"[A-Z]", part)
                and re.search(r"\d", part)
                and _entropy(part) >= 4.0
            ):
                return True
    return False


def evidence_label(host: str, argv: list[str], index: int, at: datetime) -> str:
    name = re.sub(r"[^A-Za-z0-9_-]+", "", argv[0]) or "command"
    return f"live check {host} {name} #{index} @{at.strftime('%Y-%m-%dT%H:%MZ')}"


def evidence_text(host: str, argv: list[str], exit_code: int | None, stdout: str, stderr: str, note: str = "") -> str:
    lines = [f"host: {host}", f"command: {shlex.join(argv)}", f"exit: {exit_code}"]
    if note:
        lines.append(f"note: {note}")
    return "\n".join(lines) + f"\nstdout:\n{stdout or '(no output)'}\nstderr:\n{stderr or '(no output)'}\n"


@dataclass
class CommandResult:
    host: str
    argv: list[str]
    exit_code: int | None
    stdout: str
    stderr: str
    status: str  # ran | unreachable | withheld
    note: str = ""


def remote_string(argv: list[str], *, read_roots: tuple[str, ...] | None = None) -> str:
    """The exact string the shell runs. `head` resolves symlinks first and re-checks
    the read roots, extension and secret-path words on the resolved path."""
    if argv[0] != "head":
        return shlex.join(argv)
    if read_roots is None:
        read_roots = configured_read_roots()
    roots = "|".join(shlex.quote(root) + "*" for root in read_roots) or "''"
    return (
        f"p=$(readlink -f -- {shlex.quote(argv[3])}) || exit 3; "
        f'case "$p" in {roots}) ;; *) echo "refused: resolves outside the read roots" >&2; exit 3;; esac; '
        'case "$p" in *.md|*.txt|*.gradle|*.gradle.kts|*.toml|*/package.json|*/go.mod) ;; '
        '*) echo "refused: resolved file is not a readable kind" >&2; exit 3;; esac; '
        'case "$p" in *.ssh*|*.gnupg*|*credential*|*secret*|*token*|*/.env*|*id_rsa*|*id_ed25519*) '
        'echo "refused: secret-bearing path" >&2; exit 3;; esac; '
        f'exec head -n {int(argv[2])} -- "$p"'
    )


def _cut(text: str, cap_bytes: int) -> tuple[str, bool]:
    raw = (text or "").encode()
    return (raw[:cap_bytes].decode(errors="ignore"), True) if len(raw) > cap_bytes else (text or "", False)


def run_command(
    host: str, argv: list[str], *, timeout_s: int, cap_bytes: int, runner=None, local_host: str | None = None
) -> CommandResult:
    """Run one already-allowed argv: `sh -c` on the worker host, BatchMode ssh elsewhere
    (ssh(1): the command string is run by the remote login shell; exit 255 = ssh error)."""
    runner = runner or subprocess.run
    local_host = local_host or settings.gap_owner_host or socket.gethostname()
    command = remote_string(argv)
    full = (
        ["sh", "-c", command]
        if host == local_host
        else ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "--", host, command]
    )
    try:
        done = runner(
            full,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return CommandResult(host, argv, None, "", "", "ran", f"timed out after {timeout_s} s")
    except (OSError, subprocess.SubprocessError) as exc:
        return CommandResult(host, argv, None, "", "", "unreachable", f"unavailable: {type(exc).__name__}")
    if host != local_host and done.returncode == 255:
        return CommandResult(host, argv, 255, "", "", "unreachable", "ssh connection failed (exit 255)")
    stdout, cut_out = _cut(done.stdout, cap_bytes)
    stderr, cut_err = _cut(done.stderr, cap_bytes)
    note = f"output truncated at {cap_bytes} bytes" if cut_out or cut_err else ""
    if possible_secret(stdout) or possible_secret(stderr):
        return CommandResult(host, argv, done.returncode, "", "", "withheld", "withheld: possible secret")
    return CommandResult(host, argv, done.returncode, stdout, stderr, "ran", note)
