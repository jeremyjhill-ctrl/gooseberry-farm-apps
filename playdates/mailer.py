"""Sending mail, and being honest when we cannot.

(Copied from Pasture Keeper's mailer.py so both apps send mail the same, proven way.
Farm-apps changes: each storefront names its own From setting, and a "memory"
provider lets the tests check what would have been sent.)

The app needs to send very little: a password reset, an invitation, a receipt.
What it needs badly is for those few to arrive, and for the ones that do not
to be visible rather than silent.

Two implementations, chosen by environment, the same shape as storage.py:

    MAIL_PROVIDER=console   (default; prints to the log, sends nothing)
    MAIL_PROVIDER=resend    (a host)
      RESEND_API_KEY, plus each storefront's From setting
      (BAKERY_MAIL_FROM, PLAYDATE_MAIL_FROM)
    MAIL_PROVIDER=memory    (tests only; keeps messages in a list)

The console one is the default on purpose. A farm running this on a laptop,
and every test, must not need an account anywhere - and a missing setting
must never quietly turn into "mail was sent" when nothing was.

Why not just smtplib: a transactional provider handles the part that is
actually hard, which is not the sending. It is being trusted by the mailbox
on the other end. That rests on DKIM signatures, a warmed sending domain and
somebody watching bounce rates, none of which is a thing to build.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request

# What Resend will accept as a sender: either a bare address, or a display
# name with the address in angle brackets.
_SENDER = re.compile(r'^(?:[^<>@\s]+@[^<>@\s]+\.[^<>@\s]+'
                     r'|[^<>]+<[^<>@\s]+@[^<>@\s]+\.[^<>@\s]+>)$')


def _log(message):
    """Say something in a way that actually reaches a hosted log.

    print() alone does not. Under gunicorn the app's stdout is a pipe rather
    than a terminal, so Python block-buffers it: a line can sit unflushed for
    a long time, or until the worker restarts. On a laptop everything appears
    instantly and the problem is invisible; on a host, the one diagnostic
    that matters - did the mail go, and if not why - silently does not arrive.

    stderr, and flushed, so it is out of the process before the next line
    runs. This was found the hard way, debugging an email that had almost
    certainly already failed and said so into a buffer nobody could read.
    """
    print(message, file=sys.stderr, flush=True)


class ConsoleMail:
    """Prints the message instead of sending it.

    For a laptop and for tests. Deliberately loud and deliberately honest -
    it says NOT SENT, because the failure this guards against is somebody
    concluding mail works because nothing complained.
    """

    kind = 'console'
    configured = False

    def send(self, to, subject, text, html=None):
        lines = ['--- EMAIL (NOT SENT - no mail provider configured) ---',
                 f'  To      : {to}',
                 f'  Subject : {subject}']
        lines += [f'  | {line}' for line in text.strip().splitlines()]
        lines.append('--- end ---')
        _log('\n'.join(lines))
        return True

    def send_result(self, to, subject, text, html=None):
        self.send(to, subject, text, html)
        return 'console', 'No email service is connected here, so nothing was sent.'


class ResendMail:
    """Resend's HTTP API.

    Deliberately urllib rather than their SDK: one POST of JSON, and a
    dependency that has to be kept current in return for saving six lines is
    a bad trade in a codebase this size.
    """

    kind = 'resend'
    configured = True
    ENDPOINT = 'https://api.resend.com/emails'

    # Who we say we are. Not politeness - necessity.
    #
    # urllib identifies itself as "Python-urllib/3.11" unless told otherwise,
    # and Resend's API sits behind Cloudflare, whose Browser Integrity Check
    # rejects that signature outright. The request never reaches Resend: it
    # comes back 403 with "error code: 1010", which is a Cloudflare page and
    # not a Resend error at all, so nothing appears in their dashboard and
    # there is nothing to find at their end. It cost an evening.
    #
    # This is precisely the kind of thing an official SDK does for you, and
    # the reason it is not here is a trade I made deliberately and got wrong
    # on one detail. One header is still cheaper than the dependency.
    USER_AGENT = 'GooseberryFarmApps/1.0 (+https://gooseberrybridge.com)'

    def __init__(self, api_key, sender):
        self.api_key = api_key
        self.sender = sender

    def send(self, to, subject, text, html=None):
        return self.send_result(to, subject, text, html)[0] == 'sent'

    def send_result(self, to, subject, text, html=None):
        """('sent', id) if it was accepted. Otherwise, with a reason in the log:
          ('limit', why)  Resend's daily or monthly quota is used up: try again after it resets
          ('retry', why)  a passing problem (Resend busy or down, the connection dropped): try again soon
          ('failed', why) something trying again won't fix (a bad address, a revoked key)


        Never raises. A password reset that cannot be sent should show the
        person a page saying so - not a 500 that tells them nothing and tells
        us nothing either.
        """
        payload = {
            'from': self.sender,
            'to': [to] if isinstance(to, str) else list(to),
            'subject': subject,
            'text': text,
        }
        if html:
            payload['html'] = html

        request = urllib.request.Request(
            self.ENDPOINT,
            data=json.dumps(payload).encode('utf-8'),
            headers={
                'Authorization': f'Bearer {self.api_key}',
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'User-Agent': self.USER_AGENT,
            },
            method='POST',
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                body = json.loads(response.read().decode('utf-8') or '{}')
            _log(f'MAIL SENT to {to}: {body.get("id", "(no id)")}')
            return 'sent', body.get('id', '')
        except urllib.error.HTTPError as exc:
            # The body carries the actual reason - an unverified domain, a
            # revoked key - and it is worth having in the log rather than a
            # bare status code. The key itself is never in here.
            detail = exc.read().decode('utf-8', 'replace')[:300]
            _log(f'MAIL REFUSED for {to} ({exc.code}): {detail}')
            return classify_refusal(exc.code, detail)
        except Exception as exc:                        # noqa: BLE001
            _log(f'MAIL FAILED for {to}: {type(exc).__name__}: {exc}')
            return 'retry', f'Could not reach the email service ({type(exc).__name__}).'


class MemoryMail:
    """For tests: remembers what would have been sent."""

    kind = 'memory'
    configured = True

    def __init__(self):
        self.sent = []

    refuse_with = None  # tests: set to ('limit', why) etc. to act like a refusal

    def send(self, to, subject, text, html=None):
        return self.send_result(to, subject, text, html)[0] == 'sent'

    def send_result(self, to, subject, text, html=None):
        if self.refuse_with:
            return self.refuse_with
        self.sent.append({'to': to, 'subject': subject, 'text': text})
        return 'sent', 'memory'


def classify_refusal(status, detail):
    """What a refusal from Resend means for trying again (see send_result)."""
    text = (detail or '').lower()
    if status == 429 and ('daily_quota' in text or 'monthly_quota' in text):
        return 'limit', "Resend's daily email limit was reached. It goes out after the limit resets (6 pm Central in winter, 7 pm in summer)."
    if status == 429 or status >= 500:
        return 'retry', 'The email service was busy or down ({}).'.format(status)
    return 'failed', 'The email service refused it ({}): {}'.format(status, (detail or '')[:200])


def build_mailer(from_setting='MAIL_FROM'):
    """Pick a mailer from the environment.

    Refuses to start misconfigured rather than falling back to console:
    quietly printing password resets into a log file on a host, while the
    app tells people to check their inbox, is the exact failure this whole
    module exists to prevent.
    """
    provider = os.environ.get('MAIL_PROVIDER', 'console').lower()
    if provider == 'console':
        return ConsoleMail()
    if provider == 'memory':
        return MemoryMail()

    if provider == 'resend':
        api_key = os.environ.get('RESEND_API_KEY')
        sender = os.environ.get(from_setting)
        missing = [name for name, value in
                   (('RESEND_API_KEY', api_key), (from_setting, sender))
                   if not value]
        if missing:
            raise RuntimeError(
                f'MAIL_PROVIDER=resend but {" and ".join(missing)} '
                f'{"is" if len(missing) == 1 else "are"} not set. Refusing to '
                f'start: falling back to printing mail on a console here would '
                f'mean password resets going into a log while people are told '
                f'to check their inbox.'
            )
        # Checked here rather than discovered at the provider. A MAIL_FROM
        # that lost its closing bracket on the way into a settings box is
        # accepted by everything until the first real send, which then fails
        # with a 422 nobody sees - and the only clue is in the provider's own
        # request log, which is the last place anyone looks. It cost an
        # evening. Two lines and a startup failure is a better trade.
        if not _SENDER.match(sender.strip()):
            raise RuntimeError(
                f'{from_setting} is {sender!r}, which is not an address this app '
                f'can send from. It must be either\n'
                f'    hello@gooseberrybridge.com\n'
                f'or\n'
                f'    Gooseberry Bridge Farm <hello@gooseberrybridge.com>\n'
                f'Note the closing ">" - losing it is the usual mistake, and '
                f'the provider rejects every send with a 422 that never '
                f'reaches anybody.'
            )
        return ResendMail(api_key, sender.strip())

    raise RuntimeError(
        f'MAIL_PROVIDER={provider!r} is not a provider this app knows. '
        f'Use "console" or "resend".'
    )
