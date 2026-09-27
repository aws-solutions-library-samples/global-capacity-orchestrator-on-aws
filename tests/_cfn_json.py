"""Decode a synthesized ``Stack.to_json_string`` property back into data.

CDK renders a JSON string that embeds tokens as an ``Fn::Join`` of literal
JSON fragments and intrinsics (a string with no tokens stays a plain string).
:func:`decode_cfn_json` reverses that for template assertions: each intrinsic
is swapped for a marker, the text is parsed, and every marker is put back as
the intrinsic itself (a string that was only a token) or as an ``Fn::Join``
of the string's literal and intrinsic parts, which is how CDK renders that
string on its own. Numbers and booleans come back with their JSON types,
which is the point: CloudFormation stringifies them in an object property.
"""

from __future__ import annotations

import json
import re
from typing import Any

_MARKER = re.compile(r"@@CFN([0-9]+)@@")


def decode_cfn_json(value: Any) -> Any:
    """Return the data a ``Stack.to_json_string`` template value encodes."""
    if isinstance(value, str):
        return json.loads(value)
    if not isinstance(value, dict) or set(value) != {"Fn::Join"}:
        raise AssertionError(f"not a synthesized JSON string: {value!r}")
    separator, parts = value["Fn::Join"]
    if separator != "":
        raise AssertionError(f"unexpected Fn::Join separator {separator!r}")
    intrinsics: list[Any] = []
    text: list[str] = []
    for part in parts:
        if isinstance(part, str):
            if _MARKER.search(part):
                raise AssertionError("literal fragment already contains a marker")
            text.append(part)
        else:
            text.append(f"@@CFN{len(intrinsics)}@@")
            intrinsics.append(part)
    return _restore(json.loads("".join(text)), intrinsics)


def _restore(node: Any, intrinsics: list[Any]) -> Any:
    if isinstance(node, dict):
        if any(_MARKER.search(key) for key in node):
            raise AssertionError("a token inside an object key is not supported")
        return {key: _restore(item, intrinsics) for key, item in node.items()}
    if isinstance(node, list):
        return [_restore(item, intrinsics) for item in node]
    if not isinstance(node, str):
        return node
    pieces = _MARKER.split(node)
    if len(pieces) == 1:
        return node
    parts: list[Any] = []
    for index, piece in enumerate(pieces):
        if index % 2:
            parts.append(intrinsics[int(piece)])
        elif piece:
            parts.append(piece)
    if len(parts) == 1:
        return parts[0]
    return {"Fn::Join": ["", parts]}
