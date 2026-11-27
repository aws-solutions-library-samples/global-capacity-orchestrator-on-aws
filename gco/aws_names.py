"""The AWS Region name rule the CLI and the services share.

The CLI checks a Region before it puts it into a kubectl, SSM or AWS CLI
argv, and the services check one before they use it to pick a queue or a
client. Each used to carry its own copy of the pattern. Keeping the one copy
here means a change to the rule reaches every caller.

Check a whole value with :func:`is_aws_region_name` (or
``AWS_REGION_NAME_RE.fullmatch``), not ``re.match`` with a ``$`` anchor: in
Python ``$`` also matches just before a trailing newline, so ``match`` would
accept ``"us-east-1\\n"``.

This module imports nothing but the standard library: the service images
ship ``gco/`` (without ``gco/stacks/``), and the CLI imports it for every
Region check, so it has to stay cheap to import.
"""

from __future__ import annotations

import re

#: The shape of an AWS Region name in any partition: ``us-east-1``,
#: ``ap-southeast-5``, ``us-gov-west-1``, ``cn-north-1``, ``us-isob-east-1``.
#: It carries no anchors, so a larger pattern (an ECR registry host, say) can
#: embed it.
AWS_REGION_NAME_PATTERN = r"[a-z]{2,4}(?:-[a-z0-9]+)+-[0-9]+"

#: :data:`AWS_REGION_NAME_PATTERN`, compiled. Call ``fullmatch`` on it.
AWS_REGION_NAME_RE = re.compile(AWS_REGION_NAME_PATTERN)


def is_aws_region_name(value: str) -> bool:
    """Return whether the whole of ``value`` has the shape of an AWS Region name.

    Only the shape is checked. A name that passes may still be a Region that
    does not exist or that the account has not enabled.
    """
    return AWS_REGION_NAME_RE.fullmatch(value) is not None
