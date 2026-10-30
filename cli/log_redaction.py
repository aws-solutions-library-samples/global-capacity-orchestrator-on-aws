"""Keep credentials out of the CLI's log output.

``gco -v`` used to raise the AWS SDK loggers to DEBUG, and botocore's debug
output carries credential material: the SigV4 ``CanonicalRequest`` (with the
``x-amz-security-token`` header), the request's ``Authorization`` header and
signature, and, from ``botocore.parsers``, every response body, which for
``sts:AssumeRole`` is a complete set of temporary credentials. Anyone who
pasted ``-v`` output into a bug report, a CI log or an agent transcript
shipped their session token with it (reported privately against v8.15.2).

Two defences, both wired in :func:`cli.main._configure_cli_logging`:

* ``-v`` no longer reaches SDK debug logging: the SDK loggers go to INFO
  (credential-provider discovery, nothing signed). SDK debug is the explicit,
  flag-only ``--debug-aws``, which keeps ``botocore.parsers`` at INFO so no
  response body is ever logged.
* :class:`CredentialRedactionFilter` sits on the root handler in every mode
  and rewrites the credential-shaped fields :func:`redact` knows about, so
  the signing details ``--debug-aws`` does print are safe to share, and a
  stray token in any other log line is scrubbed too.

The patterns are a denylist, so they are deliberately broad on the request
side (where the fields are a fixed, known set) and the response side is
handled by never logging it rather than by pattern.
"""

from __future__ import annotations

import logging
import re

REDACTED = "[REDACTED]"

#: Mapping keys whose values are credentials wherever they appear: botocore's
#: parameter dumps (Python ``repr``), JSON documents, and the ``key=value``
#: form of query strings and form bodies.
_SECRET_KEYS = (
    "SecretAccessKey",
    "SessionToken",
    "SecretString",
    "SecretBinary",
    "WebIdentityToken",
    "SAMLAssertion",
    "Password",
    "authorizationToken",
    "accessToken",
    "refreshToken",
    "idToken",
    "clientSecret",
)
_SECRET_KEY_ALTERNATION = "|".join(_SECRET_KEYS)

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # SigV4: the signature itself, in the Authorization header
    # (``Signature=<hex>``) and in botocore.auth's ``Signature:\n<hex>`` line.
    (re.compile(r"(Signature[=:]\s*)[0-9a-fA-F]{64}"), rf"\1{REDACTED}"),
    # The session token as a canonical header (``x-amz-security-token:<token>``
    # inside the CanonicalRequest), as a header in a request repr
    # (``'X-Amz-Security-Token': b'<token>'``) and as a query parameter.
    (re.compile(r"(x-amz-security-token:)\s*\S+", re.IGNORECASE), rf"\1{REDACTED}"),
    (
        re.compile(r"""(['"]x-amz-security-token['"]\s*:\s*b?['"])[^'"]*(['"])""", re.IGNORECASE),
        rf"\1{REDACTED}\2",
    ),
    (re.compile(r"""(x-amz-security-token=)[^&\s'"]+""", re.IGNORECASE), rf"\1{REDACTED}"),
    # Credential-shaped keys in repr/JSON documents: ``"SessionToken": "..."``,
    # ``'SecretString': '...'`` (escaped quotes inside the value included).
    (
        re.compile(
            rf"""(['"](?:{_SECRET_KEY_ALTERNATION})['"]\s*:\s*b?['"])(?:[^'"\\]|\\.)*(['"])""",
            re.IGNORECASE,
        ),
        rf"\1{REDACTED}\2",
    ),
    # The same keys as XML elements (STS and the other query-protocol services).
    (
        re.compile(
            rf"(<(?:{_SECRET_KEY_ALTERNATION})>)[^<]*(</(?:{_SECRET_KEY_ALTERNATION})>)",
            re.IGNORECASE,
        ),
        rf"\1{REDACTED}\2",
    ),
    # The same keys in query strings and form bodies.
    (
        re.compile(rf"""((?:{_SECRET_KEY_ALTERNATION})=)[^&\s'"]+""", re.IGNORECASE),
        rf"\1{REDACTED}",
    ),
    # Bearer tokens and the Slurm REST API's user token header.
    (
        re.compile(
            r"""(authorization['"]?\s*[:=]\s*b?['"]?\s*bearer\s+)[^'"\s,}]+""", re.IGNORECASE
        ),
        rf"\1{REDACTED}",
    ),
    (
        re.compile(r"""(x-slurm-user-token['"]?\s*[:=]\s*b?['"]?)[^'"\s,}]+""", re.IGNORECASE),
        rf"\1{REDACTED}",
    ),
    # Credentials spelled the way the AWS config file and environment spell them.
    (re.compile(r"(aws_secret_access_key\s*[=:]\s*)\S+", re.IGNORECASE), rf"\1{REDACTED}"),
    (re.compile(r"(aws_session_token\s*[=:]\s*)\S+", re.IGNORECASE), rf"\1{REDACTED}"),
)


def redact(text: str) -> str:
    """Return ``text`` with every credential-shaped value replaced by ``[REDACTED]``."""
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class CredentialRedactionFilter(logging.Filter):
    """Rewrite each record's message through :func:`redact` before a handler emits it.

    Installed on the root handler, so it sees every record from every logger,
    the SDK's included. A record whose message does not change is left alone
    (its lazy ``%`` formatting intact); one that does is rewritten to the
    redacted, already-formatted string. A record whose message cannot be
    formatted at all is passed through for the formatter to report.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        redacted = redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = None
        return True
