"""Local-need classification.

Decides whether a knowledge gap needs local evidence: operator profiles,
private-question scope and the profile file's `local_need_terms`.
"""

from __future__ import annotations

import re

from doclayer.local_profiles import load_profiles
from doclayer.question_scope import private_question
from doclayer.settings import settings


def _profiles():
    """The operator's profiles; a missing file leaves profile routing dormant."""
    return load_profiles(settings.local_profiles_path)


def local_profile(record) -> str | None:
    """Name of the first matching (non-stop) profile, or None."""
    text = f"{record.canonical_subject or record.identity} {record.representative_question}".lower()
    profile = _profiles().select(text)
    return profile.name if profile else None


#: Why a private need waits for an agent: the curator receives private material
#: only as a gap-worker private job, and this reason records the direct-authoring route.
DIRECT_AUTHORING_REASON = (
    "private need: author the canonical page directly from read-only local "
    "evidence, then run doclayer-gaps verify --existing"
)


def is_local_need(record, catalog=None) -> bool:
    if record.need_class == "needs_local_evidence":
        return True
    text = record.representative_question.lower()
    subject = record.canonical_subject or record.identity.removeprefix("unknown:")
    entry = catalog.subjects.get(subject) if catalog else None
    if private_question(record.representative_question, subject=subject, entry=entry):
        return True
    profiles = _profiles()
    name = local_profile(record)
    if name and any(p.name == name and p.always_local for p in profiles.profiles):
        return True
    # A local host name alone does not turn public model/library guidance into
    # local evidence. Installation/access/configuration is the distinguishing need.
    return bool(name or (profiles.local_terms and profiles.local_terms.search(text))) and bool(
        re.search(
            r"\b(installed|deployment|deployed|hosted|access routes|configuration|config|storage|worker|curator|curation|imports|propagation|filesystem|accessibility|keyboard|desktop|workstation|test route|verification routes|runtime|ssh|corpus audit)\b",
            text,
        )
    )
