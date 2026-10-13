"""Positive question/source scope matching; lexical relevance alone is insufficient."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

_SCOPE_KEYS = ("provider", "platform", "service", "offering", "edition", "version", "region")
_FILLER = {
    "on",
    "in",
    "via",
    "through",
    "for",
    "with",
    "what",
    "which",
    "how",
    "the",
    "a",
    "an",
    "my",
    "our",
    "current",
    "latest",
    "is",
    "are",
    "do",
    "does",
    "i",
    "should",
    "can",
    "configure",
    "use",
    "api",
    "sdk",
    "request",
    "parameter",
    "parameters",
}
_NAME = r"(?:[A-Z][A-Za-z0-9_.-]*|[a-z][a-z0-9]*(?:[-_.][a-z0-9]+)+)(?: [A-Z][A-Za-z0-9_.-]*){0,2}"
_REQUEST_UNIT = re.compile(
    r"^(?:(?:one|single|each|every|a|an|per|\d+)\s+)(?:reply|response|request|call|turn|message|batch|session)s?(?:\s|$)",
    re.I,
)
_MEASUREMENT = re.compile(
    r"^(?:\d+(?:\.\d+)?\s+)?(?:[kmgt]i?b|bytes?|tokens?|seconds?|milliseconds?|minutes?|hours?|ms|[kmgt]bps|rpm|tpm|rps)(?:\s|$)",
    re.I,
)


def words(text):
    return set(re.findall(r"[a-z0-9]+", str(text).lower()))


QUERY_SCOPE_POLICY = 2


@dataclass(frozen=True)
class Identities:
    """Recognized scope identities: declared hosts and catalog subjects."""

    hosts: frozenset = frozenset()
    subjects: frozenset = frozenset()
    #: Identity-key sets that name one machine (aliases sharing an ssh HostName,
    #: a host's IP from ssh config or infra/fleet-hosts.md, catalog aliases).
    groups: frozenset = frozenset()
    #: What else says "this host": its own pages and the services recorded on it.
    profiles: tuple = ()
    #: Identity keys that are not a declared host name (ssh-only names such as `fw`,
    #: catalog-free aliases). Such a word may be ordinary prose or a product name, so
    #: it names its host only in host position (`on fw`, `fw runs ...`).
    aliases: frozenset = frozenset()


@dataclass(frozen=True)
class HostProfile:
    """One host's identity keys, canonical page references and recorded service keys."""

    keys: frozenset = frozenset()
    pages: frozenset = frozenset()
    services: frozenset = frozenset()
    #: Refs of the catalog pages of those services (they say what runs on the host).
    service_pages: frozenset = frozenset()
    #: (service key, page ref) pairs: which service each of those pages is about.
    service_page_keys: frozenset = frozenset()


_IDENTITY_TTL_S = 30.0
_identity_cache: dict = {}


_IPV4 = re.compile(r"(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?![\w-]|\.\d|/\d)")


def _is_address(key):
    return bool(re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", str(key)))


def _group_keys(names):
    """Identity keys of one machine's names. An IPv4 address is kept twice: as its
    punctuation-free key (statement-side mention checks) and as written, the only
    form a question's address is matched against."""
    keys = {_identity_key(name) for name in names if name}
    return keys | {str(name) for name in names if _is_address(name)}


def _plain_merge(groups):
    merged = []
    for group in groups:
        group = set(group)
        for other in [m for m in merged if m & group]:
            group |= other
            merged.remove(other)
        merged.append(group)
    return merged


def _merge_groups(groups, declared=()):
    """Union groups of identity keys that share a key.

    `declared` holds one key set per distinct declared host. A key that sits beside
    two or more of them (a stale ssh entry reusing another host's address) is
    ambiguous: it names neither host and merges nothing.
    """
    groups = [set(group) for group in groups]
    owners = [frozenset(keys) for keys in _plain_merge(k for k in declared if k)]
    if owners:
        named = {key for keys in owners for key in keys}
        near = {}
        for group in groups:
            touching = {index for index, keys in enumerate(owners) if keys & group}
            for key in group:
                near.setdefault(key, set()).update(touching)
        shared = {key for key, touching in near.items() if len(touching) > 1 and key not in named}
        groups = [group - shared for group in groups]
    merged = []
    for group in _plain_merge(groups):
        touching = [keys for keys in owners if keys & group]
        if len(touching) > 1:
            # Still joined through ambiguous names: keep each host's own names only.
            merged.extend(keys & group for keys in touching)
        else:
            merged.append(group)
    return frozenset(frozenset(m) for m in merged)


def _declared_hosts(root, catalog_groups, host_keys):
    """One key set per distinct declared host: each catalog host entry, and each host
    infra/fleet-hosts.md records under its own bullet or heading."""
    from doclayer import coverage

    declared = [set(keys) for keys in catalog_groups]
    try:
        if root:
            text = (Path(root) / "infra/fleet-hosts.md").read_text(encoding="utf-8")
            for name, _ in coverage.fleet_host_blocks(text):
                key = _identity_key(name)
                if len(key) >= 2 and key in host_keys and not any(key in keys for keys in declared):
                    declared.append({key})
    except Exception:  # noqa: BLE001
        pass  # noqa: S110
    return declared


def _host_groups(root, catalog_groups=(), declared=()):
    """Equivalent host identities: ssh aliases sharing a HostName, the IPs ssh config
    and infra/fleet-hosts.md give each host, and each host catalog entry's own names.
    An address or alias shared by distinct declared hosts joins nothing."""
    from doclayer import coverage

    groups = list(catalog_groups)
    try:
        groups += [_group_keys(g) for g in coverage.ssh_host_groups(Path("~/.ssh/config").expanduser())]
    except Exception:  # noqa: BLE001 - recognition degrades to spelling only
        pass  # noqa: S110
    try:
        if root:
            text = (Path(root) / "infra/fleet-hosts.md").read_text(encoding="utf-8")
            groups += [_group_keys((n, ip)) for n, ip in coverage.fleet_host_addresses(text)]
    except Exception:  # noqa: BLE001
        pass  # noqa: S110
    return frozenset(g for g in _merge_groups((g for g in groups if len(g) > 1), declared) if len(g) > 1)


def _page_ref(path):
    return re.sub(r"\.md$", "", str(path).strip().lower())


def _host_profiles(root, catalog, groups=()):
    """Per host: its own canonical pages and the services the fleet page records on it.

    Own pages: a catalog host entry's pages, and any infra/ page whose stem, title,
    aliases, product or tags name the host. Services: the catalog's non-host
    subjects (slug, title, aliases) named inside the host's bullet or section of
    infra/fleet-hosts.md. Nothing is hard-coded; a missing source contributes nothing.
    """
    from doclayer import coverage, frontmatter

    if not root:
        return ()
    root = Path(root)
    try:
        blocks = coverage.fleet_host_blocks((root / "infra/fleet-hosts.md").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        blocks = []
    services = {}
    service_pages = {}
    host_keys = set()
    for entry in catalog.subjects.values() if catalog is not None else ():
        if entry.kind == "host":
            host_keys |= {_identity_key(n) for n in (entry.slug, entry.title, *entry.aliases) if n}
            continue
        if entry.kind == "host-inventory":
            continue
        keys = {_identity_key(n) for n in (entry.slug, entry.title, *entry.aliases) if n}
        services[entry.slug] = {k for k in keys if len(k) >= 4}
        service_pages[entry.slug] = {_page_ref(page.path) for page in entry.pages}
    profiles = {}
    for name, text in blocks:
        key = _identity_key(name)
        if len(key) < 2 or not (key in host_keys or any(key in g for g in groups)):
            continue  # only a known host's bullet or section records what runs on it
        keys = next((set(g) for g in groups if key in g), {key})
        spans = {k for _, k, _, _ in _identity_spans(text)}
        named = [slug for slug, names in services.items() if names & spans and not names <= keys]
        found = {k for slug in named for k in services[slug]} - keys
        if found:
            profile = profiles.setdefault(frozenset(keys), [set(), set(), set(), set()])
            profile[1].update(found)
            profile[2].update(ref for slug in named for ref in service_pages[slug])
            profile[3].update(
                (key, ref) for slug in named for key in services[slug] - keys for ref in service_pages[slug]
            )
    pages = {}
    for entry in catalog.subjects.values() if catalog is not None else ():
        if entry.kind == "host":
            for page in entry.pages:
                pages.setdefault(
                    frozenset({_identity_key(entry.slug), *(_identity_key(a) for a in entry.aliases)}), set()
                ).add(_page_ref(page.path))
    for path in sorted((root / "infra").glob("*.md")):
        try:
            meta, _ = frontmatter.parse(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        tags = meta.get("tags") or []
        aliases = meta.get("aliases") or []
        names = [
            path.stem,
            meta.get("title"),
            meta.get("product"),
            *(tags if isinstance(tags, list) else [tags]),
            *(aliases if isinstance(aliases, list) else [aliases]),
        ]
        page_keys = {_identity_key(n) for n in names if n}
        for keys in {k for k in groups} | {frozenset({k}) for k in host_keys}:
            if keys & page_keys:
                pages.setdefault(keys, set()).add(_page_ref(f"infra/{path.name}"))
    for keys, refs in pages.items():
        profiles.setdefault(keys, [set(), set(), set(), set()])[0].update(refs)
    return tuple(
        HostProfile(frozenset(k), frozenset(v[0]), frozenset(v[1]), frozenset(v[2]), frozenset(v[3]))
        for k, v in profiles.items()
    )


def _load_identities():
    from doclayer import coverage, routing
    from doclayer.settings import settings

    hosts = {settings.ssh_host}
    subjects = set()
    root_hint = None
    catalog = None
    catalog_groups = []
    try:
        hosts.update(coverage.ssh_host_aliases(Path("~/.ssh/config").expanduser(), settings.catalog_cache_dir))
    except Exception:  # noqa: BLE001 - recognition degrades to declared hosts
        pass  # noqa: S110
    try:
        root, _ = settings.brain_repo_selection()
        root_hint = root
        catalog = routing.load_catalog(root, settings.catalog_cache_dir, settings.routing_policy_path)
        for entry in catalog.subjects.values():
            names = {entry.slug, entry.title, *entry.aliases}
            (hosts if entry.kind == "host" else subjects).update(names)
            if entry.kind == "host":
                catalog_groups.append({_identity_key(n) for n in names if n})
    except Exception:  # noqa: BLE001 - an unavailable catalog recognizes nothing extra
        pass  # noqa: S110
    host_keys = _identity_keys(h for h in hosts if h)
    declared = _declared_hosts(root_hint, [*catalog_groups, {_identity_key(settings.ssh_host)}], host_keys)
    groups = _host_groups(root_hint, catalog_groups, declared)
    names = {key for keys in declared for key in keys}
    aliases = {
        key
        for key in host_keys | {k for g in groups for k in g}
        if key not in names and not key.isdigit() and not _is_address(key)
    }
    return Identities(
        frozenset(h for h in hosts if h),
        frozenset(s for s in subjects if s),
        groups,
        _host_profiles(root_hint, catalog, groups),
        frozenset(aliases),
    )


def _brain_pointer():
    return Path.home() / ".config/doclayer/brain-repo"


def _pointer_state():
    """What the brain-repo pointer currently selects, so a repo switch is seen at once."""
    pointer = _brain_pointer()
    try:
        info = pointer.lstat()
        target = os.readlink(pointer) if pointer.is_symlink() else pointer.read_text(encoding="utf-8").strip()
        return (info.st_mtime_ns, info.st_ino, target)
    except OSError:
        return None


def _cached_identities():
    from doclayer.settings import settings

    key = (str(settings._brain_repo_override), os.environ.get("DOC_LAYER_BRAIN_REPO"), _pointer_state())  # noqa: SLF001
    cached = _identity_cache.get(key)
    if cached and time.monotonic() - cached[0] < _IDENTITY_TTL_S:
        return cached[1]
    identities = _load_identities()
    _identity_cache[key] = (time.monotonic(), identities)
    return identities


def known_identities():
    """Hosts and catalog subjects a scope may legitimately name; never raises."""
    return _cached_identities()


def _query_platform_corrections(question):
    """Only identified parser artifacts present literally in this question."""
    corrections = {}
    for match in re.finditer(r"\b(?:on|in|via|through)\s+([\w.-]+(?: [\w.-]+){0,2})", question, re.I):
        raw = match[1]
        temporal = re.search(
            r"\s+(?:as\s+of|after(?=\s+(?:deployed|deployment|release|version))|before(?=\s+(?:deployed|deployment|release|version)))\b",
            question[match.start(1) :],
            re.I,
        )
        if temporal and temporal.start() < len(raw):
            host = raw[: temporal.start()].strip()
            if host:
                corrections[raw] = host
                corrections[host + " " + raw[temporal.start() :].split()[0]] = host
    for match in re.finditer(
        r"\b(?:on|in|via|through)\s+([\w.-]+)['’]s\s+(?:(?:current|local|personal|own|desktop)\s+)*(?:desktop\s+)?(?:workstation|computer|desktop)\b",
        question,
        re.I,
    ):
        if re.fullmatch(r"[A-Z][a-z]+", match[1]):
            corrections[match[1]] = None
    match = re.match(
        r"^(?:what|which)\s+(?:is|are)\s+(?:the\s+)?((?:canonical|current|actual|observed|declared)\s+[\w.-]+(?:\s+(?:production|local|runtime|private))?)\s+deployment\s+(?:topology|configuration|layout)\b",
        question,
        re.I,
    )
    if match:
        corrections[match[1]] = None
    return corrections


#: Words that describe the kind of lookup, never name a platform.
_GENERIC = {
    "existing",
    "canonical",
    "production",
    "product",
    "installed",
    "actual",
    "observed",
    "declared",
    "documented",
    "authorized",
    "stored",
    "now",
}


def _identity_key(text):
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def _identity_spans(text, longest=4):
    """Contiguous whitespace-joined token runs of `text`, longest first per start."""
    tokens = [(m.start(), m.end(), m[0].strip(".-")) for m in re.finditer(r"[A-Za-z0-9_.-]+", text)]
    for start in range(len(tokens)):
        for end in range(min(len(tokens), start + longest), start, -1):
            run = tokens[start:end]
            if any(not text[run[i][1] : run[i + 1][0]].isspace() for i in range(len(run) - 1)):
                continue
            yield (
                " ".join(token[2] for token in run),
                _identity_key("".join(token[2] for token in run)),
                run[0][0],
                run[-1][0] + len(run[-1][2]),
            )


def _first_identity(text, keys):
    """The written spelling of the first recognized identity inside `text`."""
    for span, key, _, _ in _identity_spans(text):
        if len(key) >= 2 and key in keys:
            return span
    return None


_QUALIFIED = re.compile(r"(?<![\w.-])([A-Za-z0-9][\w-]*)(?:\.[A-Za-z][\w-]*)+")
_HOST_BEFORE = re.compile(r"\b(?:on|to|from|at|in)\s+(?P<the>the\s+)?(?:[\w.-]+@)?$", re.I)
_HOST_AFTER = re.compile(r"\s+(?:is|are|was|runs?|running|serves?|serving|has|have|hosts?)\b", re.I)
#: What may follow "on the <alias>" / "in the <alias>" when the alias ends the noun
#: phrase: punctuation (? . , ; : ! ( ) | ] em dash, a spaced hyphen, closing quotes),
#: the end, or a verb or preposition.
#: "in the edge vault" and the compound "on the edge/proxy pair" do not.
_PHRASE_END = re.compile(
    r'\s*(?:$|[?.,;:!()|\]\u2014"\'\u201d\u2019]|(?<=\s)-(?=\s)|(?<=\s)(?:is|are|was|were|runs?|running|serves?|serving|has|have|had|hosts?|does|did|do|can|will|uses?|'
    r"on|in|at|to|from|for|with|by|of|via|through|over|under|after|before|since|until)\b)",
    re.I,
)


def _in_host_position(text, begin, end):
    """Whether text[begin:end] is used as a host: after on/to/from/at/in (after
    "on the"/"in the" only when it ends the noun phrase), as the subject before
    is/runs/serves/has, or as the whole subject of the question."""
    before, after = text[:begin], text[end:]
    host_before = _HOST_BEFORE.search(before)
    if host_before and (not host_before.group("the") or _PHRASE_END.match(after)):
        return True
    if _HOST_AFTER.match(after):
        return True
    return bool(
        re.fullmatch(r"\W*(?:(?:what|who|where)\s+(?:is|are)\s+)?(?:the\s+)?", before, re.I)
        and re.fullmatch(r"\W*", after)
    )


def _positional(identities, key):
    """An alias that is an ordinary word (alphabetic, four letters or more: edge,
    jammy, macbook, proxmox) names its host only in host position. A short alias or
    one with digits (gpu1, nas1, vps3, app-2, r640) is a host token anywhere."""
    return key in identities.aliases and key.isalpha() and len(key) >= 4


def _group_of(identities, key):
    return next((group for group in identities.groups if key in group), None)


def host_spans(text):
    """(begin, end, keys) for each host the text names: a declared name anywhere, an
    alias only in host position, the first label of a DNS-qualified name, and an
    IPv4 address only as an exact address token. `keys` is the host's equivalence
    group (its names and addresses), or its one key when it has no group."""
    identities = known_identities()
    host_keys = _identity_keys(identities.hosts)
    found = []

    def add(begin, end, key):
        group = _group_of(identities, key)
        if group is None and key not in host_keys:
            return
        if _positional(identities, key) and not _in_host_position(text, begin, end):
            return
        found.append((begin, end, frozenset(group or {key})))

    for _, key, begin, end in _identity_spans(text):
        if len(key) >= 2 and not key.isdigit():
            add(begin, end, key)
    for match in _QUALIFIED.finditer(text):
        whole, label = _identity_key(match[0]), _identity_key(match[1])
        if whole not in host_keys and _group_of(identities, whole) is None and len(label) >= 2:
            add(match.start(), match.end(), label)
    for match in _IPV4.finditer(text):
        group = _group_of(identities, match[0])
        if group is not None:
            found.append((match.start(), match.end(), group))
    return found


def question_hosts(question):
    """Identity keys of the known hosts the question names: each named host's whole
    equivalence group, so its name, aliases and addresses all count."""
    named = set()
    for _, _, keys in host_spans(question):
        named |= keys
    return named


def host_group(value):
    """The equivalence group (two or more keys) of the one host a phrase names, or None."""
    groups = {keys for _, _, keys in host_spans(value) if len(keys) > 1}
    return next(iter(groups)) if len(groups) == 1 else None


#: Words that make a neighbouring host name part of a hardware model phrase.
_VENDOR_WORDS = {
    "hp",
    "dell",
    "lenovo",
    "apple",
    "intel",
    "amd",
    "mac",
    "elite",
    "prodesk",
    "precision",
    "poweredge",
    "thinkcentre",
    "nuc",
}
_RUN_JOINERS = (" ", "-", "_", ".")


def _in_model_phrase(text, tokens, start, end, group, identities):
    """Whether the word run tokens[start..end] sits inside a longer hardware model
    phrase ("Lenovo ThinkCentre M90q", "Dell PowerEdge R640").

    On each side, the joined neighbouring words are read outward: a vendor word, or
    an alphanumeric token that is no host name (a model number), makes it a model
    phrase. A neighbour that is itself a host name or alias is part of a host list
    ("for H in svc1 app1"), so reading continues past it to the next word;
    anything else ends that side.
    """
    hosts = _identity_keys(identities.hosts) | {k for g in identities.groups for k in g}

    def joined(left, right):
        return text[tokens[left].end() : tokens[right].start()] in _RUN_JOINERS

    def host_run(index, step):
        """The far end of a host name (one to four joined words) read from `index`
        in direction `step`, or None."""
        for length in range(4, 0, -1):
            far = index + step * (length - 1)
            if not 0 <= far < len(tokens):
                continue
            low, high = min(index, far), max(index, far)
            if (
                all(joined(i, i + 1) for i in range(low, high))
                and "".join(tokens[i][0].lower() for i in range(low, high + 1)) in hosts
            ):
                return far
        return None

    for step, index in ((-1, start - 1), (1, end + 1)):
        previous = start if step < 0 else end
        while 0 <= index < len(tokens) and (joined(index, previous) if step < 0 else joined(previous, index)):
            word = tokens[index][0].lower()
            if word in _VENDOR_WORDS:
                return True
            far = host_run(index, step)
            if far is not None:
                previous, index = far, far + step
                continue
            if re.search(r"[a-z]", word) and re.search(r"\d", word):
                return True
            break
    return False


def _links_to_own_page(text, group, identities):
    """Whether text wikilinks a page that exactly one host owns, and that host is
    `group`'s. Profiles whose keys overlap are one host (a host's single-key profile
    and its whole equivalence group), so they count as one owner."""
    for target in re.findall(r"\[\[([^\]|#]+)", text):
        link = f"[[{target}]]"
        owners = _plain_merge(
            profile.keys for profile in identities.profiles if profile.pages and links_to_page(link, profile.pages)
        )
        if len(owners) == 1 and owners[0] & group:
            return True
    return False


def mentions_group(text, group):
    """Whether text names the host of `group`: one of its names as a run of words
    (`worker1`, `svc1-services`; an ordinary-word alias only in host position; never
    inside a hardware model phrase such as "Dell PowerEdge R640"), one of its addresses as an
    exact address token, or a wikilink to a page only that host owns."""
    if any(match[0] in group for match in _IPV4.finditer(text)):
        return True
    identities = known_identities()
    if _links_to_own_page(text, group, identities):
        return True
    names = {key for key in group if not key.isdigit() and not _is_address(key)}
    tokens = list(re.finditer(r"[A-Za-z0-9]+", text))
    for start in range(len(tokens)):
        joined = ""
        for end in range(start, min(start + 4, len(tokens))):
            if end > start and text[tokens[end - 1].end() : tokens[end].start()] not in _RUN_JOINERS:
                break
            joined += tokens[end][0].lower()
            if (
                joined in names
                and (
                    not _positional(identities, joined)
                    or _in_host_position(text, tokens[start].start(), tokens[end].end())
                )
                and not _in_model_phrase(text, tokens, start, end, group, identities)
            ):
                return True
    return False


def question_host_relations(question):
    """(own page refs, recorded service keys) of the hosts the question names."""
    named = question_hosts(question)
    pages, services = set(), set()
    for profile in known_identities().profiles:
        if profile.keys & named:
            pages |= profile.pages
            services |= profile.services
    return pages, services


def question_without_hosts(question):
    """The question with every written spelling of a known host cut out: each host it
    names (name, alias, qualified name, exact address) and any other host name."""
    keys = _identity_keys(known_identities().hosts)
    cuts = [(begin, end) for begin, end, _ in host_spans(question)]
    for _, key, begin, end in _identity_spans(question):
        if len(key) >= 2 and key in keys:
            cuts.append((begin, end))
    out, position = [], 0
    for begin, end in sorted(cuts):
        if begin >= position:
            out.append(question[position:begin])
        position = max(position, end)
    out.append(question[position:])
    return " ".join(out)


def question_host_pages(question):
    """Refs of the pages that belong to a host the question names: its own pages and
    the pages of the services recorded on it. Empty when the question names no host."""
    named = question_hosts(question)
    refs = set()
    if named:
        for profile in known_identities().profiles:
            if profile.keys & named:
                refs |= profile.pages | profile.service_pages
    return refs


def question_service_pages(question):
    """Refs of the pages of services the question names that are recorded on a host
    it names ("Is Forgejo installed on svc1?" -> svc1's Forgejo page)."""
    named = question_hosts(question)
    if not named:
        return set()
    spans = {key for _, key, _, _ in _identity_spans(question) if len(key) >= 2}
    return {
        ref
        for profile in known_identities().profiles
        if profile.keys & named
        for key, ref in profile.service_page_keys
        if key in spans
    }


def links_to_page(text, refs):
    """Whether text wikilinks to one of the page refs (path or bare stem, no `.md`)."""
    stems = {ref.rsplit("/", 1)[-1] for ref in refs}
    for target in re.findall(r"\[\[([^\]|#]+)", text):
        ref = _page_ref(target)
        if ref in refs or ("/" not in ref and ref in stems):
            return True
    return False


def mentions_host(text, hosts):
    """Whether text names one of `hosts` (identity keys), as a whole identifier or word run."""
    return any(key in hosts for _, key, _, _ in _identity_spans(text) if len(key) >= 2)


def _splits_subject_name(question, value, subject_keys):
    """True when `value` is only part of a longer catalog subject name, such as
    'GPT-6 Responses' inside 'GPT-6 Responses API': a product name is the subject
    of the question, not a platform it runs on."""
    begin = question.find(value)
    if begin < 0:
        return False
    end = begin + len(value)
    for _, key, start, stop in _identity_spans(question):
        if len(key) >= 2 and key in subject_keys and start < end and stop > begin and (start < begin or stop > end):
            return True
    return False


def _identity_keys(values):
    return {_identity_key(value) for value in values}


def _clean_platform_value(value):
    """Cut a captured phrase down to its name: stop at sentence punctuation,
    lookup-kind words and ordinary lowercase words."""
    head = re.split(r"[.;:!?,](?=\s|$)", value)[0]
    tokens = []
    for token in head.split():
        low = token.lower()
        if not tokens:
            if low in _FILLER or low in _GENERIC:
                continue
        elif (
            low in _GENERIC
            or low in ("as", "of")
            or (re.fullmatch(r"[a-z]+", token) and not re.fullmatch(r"[a-z]+", tokens[0]))
        ):
            break
        tokens.append(token)
    return " ".join(tokens)


def _without_filler(value):
    return " ".join(token for token in value.split() if token.lower() not in _FILLER)


_QUOTE = "[`'\"\u2018\u2019\u201c\u201d]?"


def _platform_phrase(question, value):
    """'strong' when the question says where something runs ("on X", "via X",
    "provider X"); 'weak' for grammar that only implies a platform ("for X",
    "with X", "X request/API/SDK/deployment/service"); else None."""
    name = r"\s+".join(re.escape(token) for token in value.split())
    q = _QUOTE  # the name may be wrapped in a quote, backtick or curly quote: on `OneCloud`
    if any(
        re.search(pattern, question, re.I)
        for pattern in (
            rf"\b(?:on|in|via|through|using)\s+(?:(?:the|our|my|a|an)\s+)?{q}{name}{q}(?![\w-])",
            rf"\b(?:provider|platform|service)\s+{q}{name}{q}(?![\w-])",
        )
    ):
        return "strong"
    if any(
        re.search(pattern, question, re.I)
        for pattern in (
            rf"\b(?:for|with)\s+(?:(?:the|our|my|a|an)\s+)?{q}{name}{q}(?![\w-])",
            rf"(?<![\w-]){q}{name}{q}\s+(?:request|API|SDK|deployment|service)\b",
        )
    ):
        return "weak"
    return None


_MACHINE_NOUN = re.compile(r"\s+(?:workstation|desktop|laptop|computer|machine|pc|box)\b", re.I)


def _describes_machine(flat_question, value):
    """True when every occurrence of `value` modifies a generic machine noun ("the Linux
    workstation"): the phrase describes a machine by kind, it names no hosting platform."""
    ends = [m.end() for m in re.finditer(r"(?<![\w.-])" + re.escape(value) + r"(?![\w-])", flat_question, re.I)]
    return bool(ends) and all(_MACHINE_NOUN.match(flat_question, end) for end in ends)


def _normalize_platform_scopes(question, scopes):
    """One rule for question-derived platform scopes (values literal in this question).

    a. a known host or alias is kept, as that identity;
    b. a catalog subject name, or a token of one, is the question's subject, never a filter;
    c. any other fragment is kept when the question states it as where something
       runs ("on X", "via X", "hosted on X", "running on X", "provider X"); grammar that
       only implies a platform ("for X", "X API") counts only when nothing stronger is stated;
    d. everything else drops out.
    Independently supplied stored constraints and typed scopes pass through.
    """
    flat_question = " ".join(question.split())
    identities = known_identities()
    host_keys = _identity_keys(identities.hosts)
    subject_keys = _identity_keys(identities.subjects) - host_keys
    subject_words = {
        w
        for name in identities.subjects
        if _identity_key(name) not in host_keys
        for w in words(name)
        if len(w) > 2 and w not in _FILLER and w not in _GENERIC
    }
    kept = []
    for item in scopes:
        if item.get("dimension") != "platform" or " ".join(str(item.get("value", "")).split()) not in flat_question:
            kept.append((item, None))
            continue
        value = _clean_platform_value(item["value"])
        host = _first_identity(value, host_keys) if value else None
        if host:
            kept.append(({**item, "value": host}, "host"))
        elif not value or words(value) <= _FILLER or _error_message_object(flat_question, value):
            continue
        elif _describes_machine(flat_question, value) and question_hosts(flat_question):
            continue  # "the Linux workstation" describes the asker's machine; the named host is the scope
        elif (
            words(value) <= subject_words
            or _first_identity(value, subject_keys)
            or _splits_subject_name(flat_question, value, subject_keys)
        ):
            continue
        else:
            phrase = _platform_phrase(flat_question, value)
            if phrase:
                kept.append(({**item, "value": value}, phrase))
    stated = any(kind in ("host", "strong") for _, kind in kept) or _first_identity(question, host_keys) is not None
    result = []
    for item, kind in kept:
        if kind == "weak" and stated:
            continue
        if kind and any(
            other["dimension"] == "platform" and words(item["value"]) <= words(other["value"]) for other in result
        ):
            continue
        result.append(item)
    return result


_ERROR_PHRASES = (
    r"no\s+space\s+left\s+on",
    r"connection\s+refused\s+on",
    r"address\s+already\s+in\s+use\s+on",
    r"read-only\s+file\s+system(?:\s+on)?",
    r"permission\s+denied\s+on",
    r"disk\s+quota\s+exceeded\s+on",
    r"too\s+many\s+open\s+files\s+on",
)
_GENERIC_STORAGE_OBJECTS = frozenset(
    {
        "device",
        "disk",
        "drive",
        "volume",
        "filesystem",
        "file system",
        "partition",
        "mount",
    }
)
_QUOTED_SPAN = re.compile(
    r"`([^`]+)`|\"([^\"]+)\"|\u201c([^\u201d]+)\u201d|\u2018([^\u2019]+)\u2019|(?<!\w)'([^']+)'(?!\w)"
)


def _error_message_object(flat_question, value):
    """True when `value` (a non-host platform candidate) names no hosting platform: it is
    `port <n>`, or every occurrence of it sits right after a known error phrase ("No space
    left on device", "Connection refused on ...", "Permission denied on ...") or inside a
    quoted or backticked span that holds more than the value itself. A bare backticked
    name (`OneCloud`) and a plain "space left on OneCloud" stay scopes."""
    if re.fullmatch(r"port\s+\d+", value.strip(), re.I):
        return True
    occurrences = [m.span() for m in re.finditer(r"(?<![\w.-])" + re.escape(value) + r"(?![\w-])", flat_question, re.I)]
    if not occurrences:
        return False
    spans = [(m.start(), m.end(), next(g for g in m.groups() if g)) for m in _QUOTED_SPAN.finditer(flat_question)]
    # An article may precede only a generic storage object: "No space left on the device", not "the OneCloud instance".
    article = r"(?:the\s+)?" if value.strip().lower() in _GENERIC_STORAGE_OBJECTS else ""
    phrase = re.compile(
        r"\b(?:" + "|".join(_ERROR_PHRASES) + r")[`'\"\u2019\u201d]?\s+(?:on\s+)?" + article + r"[`'\"\u2018\u201c]?$",
        re.I,
    )

    def explained(start, end):
        if phrase.search(flat_question[:start]):
            return True
        return any(a <= start and end <= b and inner.strip().lower() != value.strip().lower() for a, b, inner in spans)

    return all(explained(start, end) for start, end in occurrences)


def effective_question_scopes(question, scopes):
    """Retain every independent constraint; repair exact known query artifacts."""
    corrections = _query_platform_corrections(question)
    result = []
    for item in scopes:
        replacement = corrections.get(item.get("value")) if item.get("dimension") == "platform" else None
        if item.get("dimension") != "platform" or item.get("value") not in corrections:
            result.append(dict(item))
        elif replacement is not None:
            result.append({**item, "value": replacement})
    return _normalize_platform_scopes(question, result)


def requested_scopes(question, subject=None):
    """Identify explicit named usage constraints, not arbitrary question keywords."""
    found = []
    patterns = [
        ("platform", r"^(?:([\w.-]+(?: [\w.-]+){0,6}?))\s+(?:request|API|SDK|deployment|service)\b"),
        ("platform", rf"\b(?:on|in|via|through|using)\s+{_QUOTE}({_NAME})"),
        ("edition", r"\b([\w.-]+)\s+edition\b"),
        ("version", r"\b(?:version|release)\s+(v?\d[\w.-]*)"),
        ("version", r"\b(v\d+(?:\.\d+)*(?:-[\w]+)?)\b"),
        ("region", r"\bregion\s+([\w.-]+)"),
        ("region", r"\b([\w.-]+)\s+region\b"),
    ]
    if re.search(r"\b(?:invoke|invocation|hosted|hosting|deploy|deployment)\b", question, re.I):
        patterns.append(("platform", rf"\b(?:for|with)\s+({_NAME})"))
    # Explicit provider/platform/service labels disambiguate lowercased names
    # from incidental purpose clauses such as 'for reliability'.
    for match in re.finditer(
        r"\b(?:for|with)\s+(?:the\s+)?(?:provider|platform|service)\s+([\w.-]+(?: [\w.-]+){0,2})", question, re.I
    ):
        value = re.split(
            r"\s+(?:for|with|to|in|and|or|version|edition|region|request|parameters|api|sdk)\b",
            match[1],
            maxsplit=1,
            flags=re.I,
        )[0]
        found.append({"dimension": "platform", "value": value})
    # Lowercase user input is still an explicit hosting constraint. Bound the
    # name at ordinary clause/facet separators rather than capitalization.
    separators = {
        "on",
        "via",
        "through",
        "for",
        "with",
        "to",
        "in",
        "and",
        "or",
        "version",
        "release",
        "edition",
        "region",
        "request",
        "parameters",
        "api",
        "sdk",
        "environment",
        "environments",
        "deployment",
        "deployments",
        "host",
        "hosts",
        "machine",
        "machines",
    }
    for match in re.finditer(rf"(?=\b(?:on|in|via|through)\s+{_QUOTE}([\w.-]+(?: [\w.-]+){{0,2}}))", question, re.I):
        if _REQUEST_UNIT.match(match[1]):
            continue
        tokens = []
        for token in match[1].split():
            if token.lower() in separators:
                break
            tokens.append(token)
        if any(token.lower() not in _FILLER for token in tokens):
            # The span as written; the single scope rule cleans it.
            found.append({"dimension": "platform", "value": " ".join(tokens)})
    subject_words = words(subject or "")
    for dimension, pattern in patterns:
        for match in re.finditer(pattern, question, re.I if dimension != "platform" or pattern.startswith("^") else 0):
            if (
                pattern.startswith("^")
                and re.search(r"\bdeployment$", match[0], re.I)
                and re.match(r"\s+and\s+" + _NAME, question[match.end() :])
            ):
                # Coordinated component deployments are the subjects being
                # inspected; retain a named deployment host in other forms.
                continue
            if (
                dimension == "platform"
                and re.match(r"for\s", match[0], re.I)
                and re.match(
                    r"\s+(?:corrective|correction|replay|audit|testing|test|validation|review|recovery|migration|acceptance)\b",
                    question[match.end() :],
                    re.I,
                )
            ):
                # A named work phase is the purpose of the lookup, not its host.
                continue
            value = " ".join(token for token in match[1].split() if token.lower() not in _FILLER).strip()
            if not value or words(value) <= _FILLER:
                continue
            # A named product alone is relevance, while editions/versions and
            # explicit usage platforms are additional applicability constraints.
            if dimension == "platform" and subject_words and words(value) == subject_words:
                continue
            if dimension == "platform":
                # Keep the span as the question wrote it: the single scope rule
                # cleans it, and a value the question never says is never a filter.
                value = " ".join(match[1].split())
            item = {"dimension": dimension, "value": value}
            if not any(
                item["dimension"] == existing["dimension"] and words(item["value"]) <= words(existing["value"])
                for existing in found
            ):
                found.append(item)
    typed_values = [words(item["value"]) for item in found if item["dimension"] != "platform"]
    return effective_question_scopes(
        question,
        [
            item
            for item in found
            if not (
                item["dimension"] == "platform"
                and (
                    subject_words
                    and words(item["value"]) == subject_words
                    or words(item["value"]) in typed_values
                    or _MEASUREMENT.match(_without_filler(item["value"]))
                    or _REQUEST_UNIT.match(_without_filler(item["value"]))
                )
            )
        ],
    )


def source_scope(meta, *, title="", heading="", path="", urls=(), statement=""):
    explicit = {}
    applicability = meta.get("applicability", {})
    if not isinstance(applicability, dict):
        applicability = {}
    for key in _SCOPE_KEYS:
        value = applicability.get(key, meta.get(key))
        if value:
            explicit[key] = value
    # A scoped subsection is more precise than a multi-version/edition page.
    # A breadcrumb child overrides its ancestors for each dimension. Multiple
    # constraints inside that one heading remain a joint scope, never merged
    # with a sibling or a broader parent heading.
    for segment in heading.split(" / "):
        heading_scopes = {}
        for constraint in requested_scopes(segment):
            values = heading_scopes.setdefault(constraint["dimension"], [])
            if not any(words(constraint["value"]) == words(value) for value in values):
                values.append(constraint["value"])
        for dimension, values in heading_scopes.items():
            explicit[dimension] = values[0] if len(values) == 1 else values
    contexts = [title, heading, str(meta.get("product", ""))]
    # Frontmatter tags and aliases name the hosts/products a page is about
    # (for example the tag 'ubuntu-server'); they are context, never explicit scope.
    if path:
        contexts.append(re.sub(r"[-_]+", " ", Path(str(path)).stem))
    for key in ("tags", "aliases"):
        values = meta.get(key) or []
        contexts.extend(str(value) for value in ([values] if isinstance(values, str) else values) if value)
    if statement:
        contexts.append(statement)  # Exact selected fact, never page or sibling body.
    provenance = []
    for url in urls:
        try:
            parsed = urlsplit(str(url).strip("<>"))
        except ValueError:
            continue
        if parsed.scheme in ("http", "https"):
            provenance.append(parsed.hostname or "")
            provenance.append(parsed.path)
    return json.dumps({"explicit": explicit, "context": contexts, "provenance": provenance}, ensure_ascii=False)


def matches_identity(value, candidates):
    """Equivalent contiguous identifier spelling, never arbitrary word joins."""
    if isinstance(candidates, (list, tuple)):
        return any(matches_identity(value, candidate) for candidate in candidates)
    wanted = re.sub(r"[^a-z0-9]", "", str(value).lower())
    if not wanted:
        return False
    text = str(candidates)
    tokens = list(re.finditer(r"[A-Za-z0-9_.-]+", text))
    for start, token in enumerate(tokens):
        joined = ""
        for end in range(start, len(tokens)):
            if end > start and not text[tokens[end - 1].end() : tokens[end].start()].isspace():
                break
            joined += re.sub(r"[^a-z0-9]", "", tokens[end][0].lower())
            if joined == wanted:
                return True
            if len(joined) >= len(wanted):
                break
    return False


def _scope_contains(value, actual, dimension, group=None):
    if words(value) <= words(actual):
        return True
    # A host is named by any of its names or addresses.
    if group and mentions_group(" ".join(map(str, actual)) if isinstance(actual, list) else str(actual), group):
        return True
    # Host/application/provider identifiers commonly differ only by rendering
    # as OneCloud versus One Cloud. Versions/editions/regions never use this.
    return dimension in ("platform", "provider", "service", "offering") and matches_identity(value, actual)


def matches_scope(scope, constraints):
    if not constraints:
        return True
    try:
        data = json.loads(scope or "{}")
    except (ValueError, TypeError):
        return False
    explicit = data.get("explicit", {})
    context = words(" ".join(str(value) for value in data.get("context", [])))
    provenance = words(" ".join(str(value) for value in data.get("provenance", [])))
    for constraint in constraints:
        key, wanted = constraint["dimension"], words(constraint["value"])
        # A host constraint is the host's equivalence group, however the question spelled it.
        group = host_group(constraint["value"]) if key == "platform" else None
        actual = explicit.get(key)
        if actual is not None:
            if not _scope_contains(constraint["value"], actual, key, group):
                return False
            continue
        # Service/offering can declare the hosting boundary without a platform
        # field. A model provider must never override that more specific scope.
        if key == "platform":
            hosting = explicit.get("service", explicit.get("offering"))
            if hosting is not None:
                if not _scope_contains(constraint["value"], hosting, key, group):
                    return False
                continue
        # Provider is a useful fallback only if no explicit hosting scope exists.
        if key == "platform" and "provider" in explicit:
            if _scope_contains(constraint["value"], explicit["provider"], key, group):
                continue
        if not wanted <= context | provenance:
            candidates = data.get("context", []) + data.get("provenance", [])
            if not any(_scope_contains(constraint["value"], candidate, key, group) for candidate in candidates):
                return False
    return True


def source_urls(text):
    """Per-statement citations, never a mixed page's global source lineage."""
    return re.findall(r"https?://[^\s\]>)]+", text)
