"""Linear-time validation helpers for immutable container image references."""

from __future__ import annotations

_LOWER_HEX = frozenset("0123456789abcdef")
_LOWER_ALNUM = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")
_HOST_ALNUM = _LOWER_ALNUM | frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
_TAG_FIRST_CHARACTERS = _HOST_ALNUM | frozenset("_")
_TAG_CHARACTERS = _TAG_FIRST_CHARACTERS | frozenset(".-")
_DIGEST_SEPARATOR = "@sha256:"


def _valid_host_label(label: str) -> bool:
    return (
        bool(label)
        and label[0] in _HOST_ALNUM
        and label[-1] in _HOST_ALNUM
        and all(character in _HOST_ALNUM or character == "-" for character in label)
    )


def _valid_registry(segment: str) -> bool:
    """Validate a DNS-style registry host with an optional numeric TCP port."""
    if segment.count(":") > 1:
        return False
    host = segment
    if ":" in segment:
        host, port_text = segment.rsplit(":", 1)
        if not port_text.isascii() or not port_text.isdigit() or len(port_text) > 5:
            return False
        port = int(port_text)
        if not 1 <= port <= 65535:
            return False
    return bool(host) and all(_valid_host_label(label) for label in host.split("."))


def _valid_repository_component(component: str) -> bool:
    """Validate one lowercase distribution repository-name component."""
    if not component or component[0] not in _LOWER_ALNUM:
        return False
    index = 0
    length = len(component)
    while True:
        while index < length and component[index] in _LOWER_ALNUM:
            index += 1
        if index == length:
            return True
        if component[index] == ".":
            index += 1
        elif component[index] == "_":
            index += 1
            if index < length and component[index] == "_":
                index += 1
        elif component[index] == "-":
            while index < length and component[index] == "-":
                index += 1
        else:
            return False
        if index == length or component[index] not in _LOWER_ALNUM:
            return False


def immutable_sha256_digest(value: object) -> str | None:
    """Return a lowercase SHA-256 digest for one strict image reference.

    Parsing is explicit and linear-time: registry, lowercase repository path,
    optional Docker tag, and digest are bounded independently. Uppercase tag
    characters remain valid, while malformed hosts, ports, and separators are
    rejected before a deployment can reach the image-pull phase.
    """
    if not isinstance(value, str) or value.count(_DIGEST_SEPARATOR) != 1:
        return None
    name, digest = value.split(_DIGEST_SEPARATOR, 1)
    if (
        not name
        or "@" in name
        or len(digest) != 64
        or any(character not in _LOWER_HEX for character in digest)
    ):
        return None

    last_slash = name.rfind("/")
    last_colon = name.rfind(":")
    tag: str | None = None
    repository = name
    if last_colon > last_slash:
        repository = name[:last_colon]
        tag = name[last_colon + 1 :]
    if tag is not None and (
        not tag
        or len(tag) > 128
        or tag[0] not in _TAG_FIRST_CHARACTERS
        or any(character not in _TAG_CHARACTERS for character in tag[1:])
    ):
        return None

    segments = repository.split("/")
    if any(not segment for segment in segments):
        return None
    first = segments[0]
    has_registry = len(segments) > 1 and (
        "." in first or ":" in first or first.casefold() == "localhost"
    )
    repository_segments = segments
    if has_registry:
        if not _valid_registry(first):
            return None
        repository_segments = segments[1:]
    if not all(_valid_repository_component(component) for component in repository_segments):
        return None
    return digest
