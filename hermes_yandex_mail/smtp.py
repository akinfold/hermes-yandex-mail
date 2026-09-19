"""Hand-driven SMTP submission to Yandex, over verified TLS.

Deliberately does not call :meth:`smtplib.SMTP.send_message` or
:meth:`~smtplib.SMTP.data`. Both bundle "write the payload" and "read the
reply" into one call, and :meth:`smtplib.SMTP.send` turns any ``OSError`` into
``SMTPServerDisconnected`` whether nothing or everything reached the socket. A
caller cannot then tell *nothing was sent* from *it may have been delivered* —
and those two demand opposite behaviour from an agent: the first is safe to
retry, the second must never be retried, because a duplicate message to a real
person is the worst outcome this module can produce.

So the transaction is driven step by step, and one flag records where it got
to: whether the server has answered ``354`` and accepted responsibility for
what follows. Everything raised before that is "nothing was sent"; everything
after it is "assume it was delivered, and say that the confirmation is
missing". The flag is set just *before* the payload is written rather than
after, deliberately: a write that fails on its first byte is then reported as
possibly-delivered, which is the harmless direction to be wrong in. Reporting
a delivered message as unsent is what produces a duplicate.

TLS is :func:`ssl.create_default_context` with no way to weaken it, for the
reason 0.2.1 records: an unauthenticated connection carries the app password.
"""

from __future__ import annotations

import contextlib
import re
import smtplib
import ssl
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

__all__ = [
    "DEFAULT_SMTP_HOST",
    "DEFAULT_SMTP_PORT",
    "DeliveryResult",
    "SendError",
    "YandexSMTPClient",
]

DEFAULT_SMTP_HOST = "smtp.yandex.ru"
DEFAULT_SMTP_PORT = 465
DEFAULT_TIMEOUT = 30

#: Remote text is quoted back for diagnosis, never woven into a sentence and
#: never unbounded: it is data from a machine we do not control.
MAX_SERVER_TEXT = 200

_ENHANCED_RE = re.compile(r"^([245]\.\d{1,3}\.\d{1,3})\b")
_LEADING_DOT_RE = re.compile(rb"(?m)^\.")


class SendError(RuntimeError):
    """Delivery did not happen. Raised only when nothing was transmitted."""


@dataclass(frozen=True)
class DeliveryResult:
    """What the server did with one submission.

    ``confirmed`` is False once the server has answered ``354`` and the closing
    ``250`` has not come back. That is not a failure: the message may well have
    been delivered, and the caller must report it as sent-but-unconfirmed
    rather than invite a retry.
    """

    accepted: tuple[str, ...]
    refused: dict[str, dict[str, object]] = field(default_factory=dict)
    confirmed: bool = True


def _clean(raw: object) -> str:
    """One bounded, single-line string out of whatever the server said."""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    text = " ".join(text.split())
    return text[:MAX_SERVER_TEXT]


def _refusal(code: int, raw: object) -> dict[str, object]:
    text = _clean(raw)
    match = _ENHANCED_RE.match(text)
    return {"code": code, "status": match.group(1) if match else "", "server_text": text}


def _dot_stuffed(payload: bytes) -> bytes:
    """Escape leading dots and close the DATA block, as :meth:`smtplib.SMTP.data` would.

    Copied rather than imported: ``smtplib._quote_periods`` is private, and a
    body line consisting of a single ``.`` would otherwise end the message
    early — truncating what the recipient receives without any error anywhere.
    """
    quoted = _LEADING_DOT_RE.sub(b"..", payload)
    if not quoted.endswith(b"\r\n"):
        quoted += b"\r\n"
    return quoted + b".\r\n"


def _abandon(conn: smtplib.SMTP | None) -> None:
    """Drop a connection that never became usable, without a second failure."""
    if conn is not None:
        with contextlib.suppress(OSError, smtplib.SMTPException):
            conn.close()


def _default_connection(host: str, port: int) -> smtplib.SMTP:
    """Implicit TLS with certificate and hostname verification, and a timeout."""
    return smtplib.SMTP_SSL(
        host, port, context=ssl.create_default_context(), timeout=DEFAULT_TIMEOUT
    )


class YandexSMTPClient:
    """One submission per connection; the shape mirrors ``YandexIMAPClient``."""

    def __init__(
        self,
        login: str,
        password: str,
        host: str = DEFAULT_SMTP_HOST,
        port: int = DEFAULT_SMTP_PORT,
        connection_factory: Callable[[str, int], smtplib.SMTP] | None = None,
    ) -> None:
        self._login = login
        self._password = password
        self._host = host
        self._port = port
        self._factory = connection_factory or _default_connection
        self._conn: smtplib.SMTP | None = None

    def __enter__(self) -> YandexSMTPClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def connect(self) -> smtplib.SMTP:
        if self._conn is not None:
            return self._conn
        conn = None
        try:
            conn = self._factory(self._host, self._port)
            conn.ehlo_or_helo_if_needed()
            conn.login(self._login, self._password)
        except smtplib.SMTPAuthenticationError as exc:
            _abandon(conn)
            raise SendError(
                "Yandex refused the SMTP login. The app password needs the Mail scope, and "
                f"the login must be the full address. Nothing was sent. ({_clean(exc.smtp_error)})"
            ) from exc
        except (OSError, smtplib.SMTPException) as exc:
            # The socket is open by now whenever the factory itself succeeded —
            # a wrong app password is the likeliest first-run failure, and it
            # must not leave a TLS connection dangling.
            _abandon(conn)
            raise SendError(
                f"Cannot reach {self._host}:{self._port}. Nothing was sent. ({_clean(exc)})"
            ) from exc
        self._conn = conn
        return conn

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        # The transaction is already decided; a failure to say goodbye must
        # never turn a delivered message into an error.
        with contextlib.suppress(OSError, smtplib.SMTPException):
            conn.quit()

    def send(self, sender: str, recipients: Sequence[str], payload: bytes) -> DeliveryResult:
        """Submit one message, reporting exactly which recipients were accepted."""
        conn = self.connect()
        refused = self._envelope(conn, sender, recipients)
        accepted = tuple(r for r in recipients if r not in refused)
        if not accepted:
            self._reset(conn)
            raise SendError(
                "The server refused every recipient, so nothing was sent: "
                + "; ".join(f"{a} ({d['code']})" for a, d in refused.items())
            )
        return self._transmit(conn, payload, accepted, refused)

    def _envelope(
        self, conn: smtplib.SMTP, sender: str, recipients: Sequence[str]
    ) -> dict[str, dict[str, object]]:
        """MAIL FROM and one RCPT TO per recipient. Nothing is written yet."""
        try:
            code, resp = conn.mail(sender)
            if code != 250:
                self._reset(conn)
                raise SendError(
                    f"The server rejected the sender {sender}: {code} {_clean(resp)}. "
                    "Nothing was sent."
                )
            refused: dict[str, dict[str, object]] = {}
            for address in recipients:
                code, resp = conn.rcpt(address)
                if code not in (250, 251):
                    refused[address] = _refusal(code, resp)
            return refused
        except smtplib.SMTPException as exc:
            raise SendError(
                f"The server ended the conversation. Nothing was sent. ({exc})"
            ) from exc
        except OSError as exc:
            raise SendError("The connection dropped before the message was sent.") from exc

    @staticmethod
    def _reset(conn: smtplib.SMTP) -> None:
        with contextlib.suppress(OSError, smtplib.SMTPException):
            conn.rset()

    @staticmethod
    def _transmit(
        conn: smtplib.SMTP,
        payload: bytes,
        accepted: tuple[str, ...],
        refused: dict[str, dict[str, object]],
    ) -> DeliveryResult:
        """DATA, then the payload, then the reply — with the one flag that matters."""
        written = False
        try:
            conn.putcmd("data")
            code, resp = conn.getreply()
            if code != 354:
                raise SendError(
                    f"The server refused to accept the message body: {code} {_clean(resp)}. "
                    "Nothing was sent."
                )
            written = True
            conn.send(_dot_stuffed(payload))
            code, resp = conn.getreply()
        except SendError:
            raise
        except (OSError, smtplib.SMTPException) as exc:
            if not written:
                raise SendError(
                    f"The connection dropped before the message was sent. ({exc})"
                ) from exc
            return DeliveryResult(accepted=accepted, refused=refused, confirmed=False)
        if code != 250:
            # The payload is already on the wire, so this is not "nothing was
            # sent" however the server answers: report it as unconfirmed.
            return DeliveryResult(accepted=accepted, refused=refused, confirmed=False)
        return DeliveryResult(accepted=accepted, refused=refused, confirmed=True)
