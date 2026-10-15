"""Shared parsing helpers: a YAML loader that keeps dates as strings, handle and status cleanup."""
import re

import yaml


class _StrDateLoader(yaml.SafeLoader):
    """SafeLoader that leaves timestamps as strings (two kep.yaml files have invalid dates)."""


_StrDateLoader.yaml_implicit_resolvers = {
    ch: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for ch, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}

try:  # the C loader is ~10x faster; used for the large sigs.yaml history
    class _CStrDateLoader(yaml.CSafeLoader):
        pass
    _CStrDateLoader.yaml_implicit_resolvers = _StrDateLoader.yaml_implicit_resolvers
except AttributeError:  # pragma: no cover
    _CStrDateLoader = _StrDateLoader


def load_yaml(text: str):
    return yaml.load(text, Loader=_CStrDateLoader)


BOTS = {"k8s-ci-robot", "k8s-triage-robot", "fejta-bot", "k8s-merge-robot", "dependabot",
        "github-actions", "codecov", "linux-foundation-easycla", "k8s-release-robot"}
_HANDLE = re.compile(r"@?([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))")


def clean_handle(value) -> str | None:
    """'@SergeyKanzhelev' / '"@foo"  # role' / 'Name (@foo)' -> 'sergeykanzhelev'. None if no handle."""
    if value is None:
        return None
    s = str(value).split("#")[0].strip().strip('"').strip("'")
    m = re.search(r"@([A-Za-z0-9-]+)", s) or _HANDLE.fullmatch(s.strip())
    if not m:
        return None
    login = m.group(1).lower().strip("-")
    if not login or is_bot(login):
        return None
    return login


def is_bot(login: str) -> bool:
    login = login.lower()
    return login in BOTS or login.endswith("[bot]") or login.endswith("-bot") or login.endswith("-robot")


def handles(values) -> list[str]:
    out = []
    for v in values or []:
        h = clean_handle(v)
        if h and h not in out:
            out.append(h)
    return out


# KEP status values seen in the wild, including typos and inline comments
_STATUS_MAP = {
    "provisional": "provisional", "implementable": "implementable", "implementeable": "implementable",
    "implemented": "implemented", "imlpemented": "implemented", "deferred": "deferred",
    "rejected": "rejected", "withdrawn": "withdrawn", "replaced": "replaced",
    "superseded": "replaced", "removed": "withdrawn",
}
STATUSES = sorted(set(_STATUS_MAP.values()))


def clean_status(value) -> str | None:
    if not value:
        return None
    s = str(value).split("#")[0].strip().strip('"').lower()
    s = s.split("(")[0].strip()
    if "|" in s:  # template placeholder left in place
        return None
    return _STATUS_MAP.get(s)


_KEP_PATH = re.compile(r"keps/(?:[\w-]+/)?(\d+)-")


def kep_id(number) -> str:
    return f"KEP-{int(number)}"


def kep_id_from_ref(ref) -> str | None:
    """'/keps/sig-node/4438-sidecar-restart-termination' or 'KEP-4438' or 4438 -> 'KEP-4438'."""
    if ref is None:
        return None
    s = str(ref)
    m = _KEP_PATH.search(s) or re.search(r"(?:KEP-?|#)(\d+)", s, re.I) or re.fullmatch(r"\s*(\d+)\s*", s)
    return kep_id(m.group(1)) if m else None
