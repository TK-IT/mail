"""Lightweight email forwarding framework.

The emailtunnel module uses the standard library module 'email'
to parse and represent email messages, and the aiosmtpd module
to accept messages via SMTP, and the standard library smtplib module
to forward messages via SMTP.

The emailtunnel module exports the classes:
SMTPReceiver -- an SMTP server that handles errors in processing
RelayMixin -- mixin providing a `deliver` method to send email
SMTPForwarder -- implementation of SMTPReceiver that forwards emails
LoggingReceiver -- simple implementation of SMTPReceiver that logs via print()
Message -- encapsulation of email.message.Message
Envelope -- connecting a Message to its sender and recipients

The emailtunnel module exports the exception:
InvalidRecipient -- raised to return SMTP 550 to remote peers

When the module is run from the command-line, SMTPForwarder is instantiated to
run an open SMTP relay.

See also the submodule:
emailtunnel.send -- simple construction and sending of email
"""

from io import BytesIO
import os
import re
import copy
import time
import logging
import datetime

import email
import email.utils
import email.mime.multipart
from email.generator import BytesGenerator
from email.header import Header
from email.charset import QP
import email.message

import smtplib

import asyncio
import aiosmtpd.controller


logger = logging.getLogger('emailtunnel')


def _fix_eols(data):
    if isinstance(data, str):
        return re.sub(r'(?:\r\n|\n|\r)', "\r\n", data)
    elif isinstance(data, bytes):
        return re.sub(br'(?:\r\n|\n|\r)', b"\r\n", data)
    else:
        raise TypeError('data must be str or bytes, not %s'
                        % type(data).__name__)


def make_message_id(domain):
    now_str = datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S.%f')
    return '%s@%s' % (now_str, domain)


def now_string():
    """Return the current date and time as a string."""
    return datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S.%f")


def decode_any_header(value):
    '''Wrapper around email.header.decode_header to absorb all errors.'''
    try:
        chunks = email.header.decode_header(value)
    except email.errors.HeaderParseError:
        chunks = [(value, None)]

    header = email.header.Header()
    for string, charset in chunks:
        if charset is not None:
            if not isinstance(charset, email.header.Charset):
                charset = email.header.Charset(charset)
        try:
            try:
                header.append(string, charset, errors='strict')
            except UnicodeDecodeError:
                header.append(string, 'latin1', errors='strict')
        except:
            header.append(string, charset, errors='replace')
    return header


class InvalidRecipient(Exception):
    pass


class Message(object):
    def __init__(self, message=None):
        if isinstance(message, email.message.Message):
            self.message = message
        elif message:
            assert isinstance(message, bytes)

            self.message = email.message_from_bytes(message)

            if not self._sanity_check(message):
                self._sanity_log_invalid(message)

        else:
            self.message = email.mime.multipart.MIMEMultipart()

    def _sanity_check(self, message):
        a = message.rstrip(b'\n')
        b = self.as_bytes().rstrip(b'\n')
        return a == b or self._sanity_strip(a) == self._sanity_strip(b)

    def _sanity_strip(self, data):
        data = re.sub(b': *', b': ', data)
        lines = re.split(br'[\r\n]+', data.rstrip())
        return tuple(line.rstrip() for line in lines)

    def _sanity_log_invalid(self, message):
        try:
            dirname = 'insane'
            basename = os.path.join(dirname, now_string())
            try:
                os.mkdir(dirname)
            except FileExistsError:
                pass
            with open(basename + '.in', 'ab') as fp:
                fp.write(message)
            with open(basename + '.out', 'ab') as fp:
                fp.write(self.as_bytes())
            logger.debug(
                'Data is not sane; logging to %s' % (basename,))
        except:
            logger.exception(
                'Data is not sane and could not log to %s; continuing anyway'
                % (basename,))

    def __str__(self):
        return str(self.message)

    def as_bytes(self):
        """Return the entire formatted message as a bytes object."""
        # Instead of using self.message.as_bytes() directly,
        # we copy and edit the implementation of email.Message.as_bytes
        # since it does not accept maxheaderlen, which we wish to set to 0
        # for transparency.

        policy = self.message.policy
        fp = BytesIO()
        g = BytesGenerator(fp,
                           mangle_from_=False,
                           maxheaderlen=0,
                           policy=policy)
        g.flatten(self.message, unixfrom=None)
        return fp.getvalue()

    def add_received_line(self, value):
        # This is a hack! email.message.Message does not support
        # adding headers in front of everything else.
        # We have to access the internal _headers API to do this.

        headers = list(self.message._headers)
        self.message._headers = []
        self.message['Received'] = value
        self.message._headers += headers

    def add_header(self, key, value):
        self.message[key] = value

    def get_header(self, key, default=None):
        return self.message.get(key, default)

    def header_items(self):
        '''Iterate the message's header fields and values as Header objects.'''
        for field, value in self.message.items():
            yield (field, decode_any_header(value))

    def get_all_headers(self, key):
        """Return a list of headers with the given key.

        If no headers are found, the empty list is returned.
        """

        return self.message.get_all(key, [])

    def get_unique_header(self, key):
        values = self.get_all_headers(key)
        if len(values) == 0:
            raise KeyError('header %r' % (key,))
        else:
            return values[0]

    def set_unique_header(self, key, value):
        try:
            self.message.replace_header(key, value)
        except KeyError:
            self.message[key] = value

    def set_body_text(self, body, encoding):
        body_part = email.message.MIMEPart()
        if encoding:
            encoded = body.encode(encoding)
            body_part.set_payload(encoded)
            body_part['Content-Type'] = 'text/plain'
            email.charset.add_charset(encoding, QP, QP)
            body_part.set_charset(encoding)
        else:
            body_part.set_payload(body)
            body_part['Content-Type'] = 'text/plain'
        self.message.set_payload(body_part)

    @property
    def subject(self):
        try:
            subject = self.get_unique_header('Subject')
        except KeyError:
            subject = ''

        return decode_any_header(subject)

    @subject.setter
    def subject(self, s):
        self.set_unique_header('Subject', s)

    @classmethod
    def compose(cls, from_, to, subject, body, message_id=None):
        message = cls()
        message.add_header('From', from_)
        message.add_header('To', to)
        message.subject = subject
        message.add_header(
            'Date',
            datetime.datetime.utcnow().strftime("%a, %d %b %Y %T +0000"))
        if message_id is None:
            domain = from_.split('@')[1]
            message_id = make_message_id(domain)
        message.add_header('Message-ID', message_id)
        message.set_body_text(body, 'utf-8')
        return message


class Envelope(object):
    def __init__(self, message, mailfrom, rcpttos):
        """See also SMTPReceiver.process_message.

        mailfrom is a string; rcpttos is a list of recipients.
        """

        assert isinstance(message, Message)
        self.message = message
        self.mailfrom = mailfrom
        self.rcpttos = rcpttos

    def recipients(self):
        '''
        Returns a list of (address, name_address, header)-triples
        where address is either an envelope recipient or None,
        name_address is an RFC822 address specification, and header is the
        header in which the address were found, or "Bcc" if not present.
        If a recipient is present in both To and Cc, To takes precedence.
        The list is sorted by header such that To comes before Cc.
        '''

        headers = 'To Resent-To Cc Resent-Cc Bcc Resent-Bcc'.split()
        visible_recipients = {}
        for k in headers:
            raw_values = self.message.get_all_headers(k)
            if not raw_values:
                continue
            # Don't apply decode_any_header to raw_values
            # before passing it to email.utils.getaddresses,
            # since the recipient =?utf8?q?foo=2Cbar?= <foo@bar>
            # would be turned into foo,bar <foo@bar>,
            # which would be interpreted as <foo>, bar <foo@bar>.
            for realname, address in email.utils.getaddresses(raw_values):
                # Actually there's no need to apply decode_any_header to
                # realname here since we don't need to interpret it.
                # realname = str(decode_any_header(realname))
                address = str(decode_any_header(address))
                # Use setdefault so that To takes precedence over Cc
                visible_recipients.setdefault(address, (realname, k))

        result = []
        for address in self.rcpttos:
            realname, header = visible_recipients.pop(address, ('', 'Bcc'))
            formatted = email.utils.formataddr((realname, address))
            result.append((address, formatted, header))
        for address, (realname, header) in visible_recipients.items():
            formatted = email.utils.formataddr((realname, address))
            result.append((None, formatted, header))
        # Sort in order of "headers".
        result.sort(key=lambda t: headers.index(t[2]))
        return result


class Handler:
    def __init__(self, smtp_receiver: 'SMTPReceiver'):
        self.smtp_receiver = smtp_receiver

    @asyncio.coroutine
    def handle_DATA(self, server, session, envelope):
        if isinstance(envelope.content, str):
            data = envelope.original_content
        else:
            data = envelope.content
        status = self.smtp_receiver.process_message(
            session.peer[:2],
            envelope.mail_from, envelope.rcpt_tos, data)
        return '250 OK' if status is None else status


class SMTPReceiver:
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.thread = None
        self.ready_timeout = 1.0
        self.thread_exception = None
        self.controller = aiosmtpd.controller.Controller(
            Handler(self), hostname=host, port=port)

    def startup_log(self):
        logger.debug('Initialize SMTPReceiver on %s:%s'
                      % (self.host, self.port))

    def run(self):
        self.start()
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        self.stop()

    def start(self):
        self.controller.start()

    def stop(self):
        self.controller.stop()

    def log_receipt(self, peer, envelope):
        ipaddr, port = peer
        mailfrom = envelope.mailfrom
        rcpttos = envelope.rcpttos
        message = envelope.message

        if type(mailfrom) == str:
            sender = '<%s>' % mailfrom
        else:
            sender = repr(mailfrom)

        if type(rcpttos) == list and all(type(x) == str for x in rcpttos):
            if len(rcpttos) == 1:
                recipients = 'To: <%s>' % rcpttos[0]
            else:
                recipients = 'To: %s' % ', '.join('<%s>' % x for x in rcpttos)
        else:
            recipients = 'To: %s' % (repr(rcpttos),)

        if ipaddr == '127.0.0.1':
            source = ''
        else:
            source = ' Peer: %s:%s' % (ipaddr, port)

        logger.info("Subject: %r From: %s %s%s" %
                    (str(message.subject), sender, recipients, source))

    def process_message(self, peer, mailfrom, rcpttos, data):
        """
        peer is a tuple of (ipaddr, port).
        mailfrom is the raw sender address.
        rcpttos is a list of raw recipient addresses.
        data is the full text of the message.
        """

        try:
            ipaddr, port = peer
            message = Message(data)
            envelope = Envelope(message, mailfrom, rcpttos)
        except:
            logger.exception("Could not construct envelope!")
            try:
                self.handle_error(None, data.decode('latin1'))
            except:
                logger.exception("handle_error(None) threw exception")
            return '451 Requested action aborted: error in processing'

        try:
            self.log_receipt(peer, envelope)
            return self.handle_envelope(envelope, peer)
        except:
            logger.exception("Could not handle envelope!")
            try:
                self.handle_error(envelope, data.decode('latin1'))
            except:
                logger.exception("handle_error threw exception")

            # Instruct the sender to retry sending the message later.
            return '451 Requested action aborted: error in processing'

    def handle_envelope(self, envelope, peer):
        raise NotImplementedError()

    def handle_error(self, envelope, str_data):
        pass


class LoggingReceiver(SMTPReceiver):
    """Message handler for --log mode"""

    def handle_envelope(self, envelope, peer):
        now = datetime.datetime.now().strftime(' %Y-%m-%d %H:%M:%S ')
        print(now.center(79, '='))
        print(str(envelope.message))


class RelayMixin(object):
    def configure_relay(self):
        # relay_host = smtplib.SMTP_SSL(hostname, self.port)
        relay_host = smtplib.SMTP(self.relay_host, self.relay_port)
        relay_host.set_debuglevel(0)
        # relay_host.starttls()
        # relay_host.login(self.username, self.password)
        return relay_host

    def log_delivery(self, message, recipients, sender):
        logger.info('To: %r From: %r Subject: %r' %
                    (recipients, sender, str(message.subject)))

    def deliver(self, message, recipients, sender):
        relay_host = self.configure_relay()
        self.log_delivery(message, recipients, sender)
        try:
            data = _fix_eols(message.as_bytes())
            relay_host.sendmail(sender, recipients, data)
        finally:
            try:
                relay_host.quit()
            except smtplib.SMTPServerDisconnected:
                pass


class SMTPForwarder(SMTPReceiver, RelayMixin):
    def __init__(self, receiver_host, receiver_port, relay_host, relay_port):
        self.relay_host = relay_host
        self.relay_port = relay_port
        super(SMTPForwarder, self).__init__(receiver_host, receiver_port)

    def get_envelope_recipients(self, envelope):
        """May be overridden in subclasses.

        Given an envelope, return a list of target recipients.
        By default, processes each recipient using translate_recipient,
        sorts the result, filters out empty addresses and duplicates.
        """

        # envelope.recipients() returns To: before Cc:.
        # The order matters if translate_recipient returns a group object
        # rather than a simple list of email addresses since handle_envelope
        # doesn't forward a message multiple times to the same recipient.

        invalid = []
        recipients = []
        for rcptto, formatted, header in envelope.recipients():
            if rcptto is None:
                continue
            try:
                translated = self.translate_recipient(rcptto)
            except InvalidRecipient as e:
                if len(e.args) == 1:
                    invalid.append(e.args[0])
                else:
                    invalid.append(e)
            else:
                if isinstance(translated, str):
                    raise ValueError(
                        'translate_recipient must return a list, not a string')
                recipients += list(translated)

        if invalid:
            raise InvalidRecipient(invalid)

        return recipients

    def translate_recipient(self, rcptto):
        """Should be overridden in subclasses.

        Given a single recipient, return a list of target recipients.
        By default, returns the input recipient.
        """

        return [rcptto]

    def translate_subject(self, envelope):
        """Implement to translate the subject to something else.

        If None is returned, the subject is not changed.
        Otherwise, the subject of the message in the given envelope
        is changed to the returned value before being forwarded.
        """
        return None

    def get_envelope_mailfrom(self, envelope):
        """Compute the address to use as MAIL FROM.

        This is the Return-Path to which returned emails are sent.
        By default, returns the same MAIL FROM as the received envelope,
        but should be changed.
        """
        return envelope.mailfrom

    def get_envelope_received(self, envelope, peer, recipients=None):
        """Compute the value of the Received:-header to add.

        By default, we add a header with From, By, For and date information
        according to RFC 2821.

        The implementation may return None to disable the addition
        of the Received:-header.
        """

        ipaddr, port = peer
        return 'from %s\nby %s\nfor %s;\n%s' % (
            ipaddr, 'emailtunnel.local',
            ', '.join('<%s>' % rcpt for rcpt in envelope.rcpttos),
            datetime.datetime.utcnow().strftime(
                '%a, %e %b %Y %T +0000 (UTC)'),
        )

    def _get_envelope_received_header(self, envelope, peer, **kwargs):
        s = self.get_envelope_received(envelope, peer, **kwargs)
        if s is None:
            return None
        lines = [line.strip() for line in s.splitlines()]
        continuation_ws = '\t'
        linesep = '\n' + continuation_ws
        h = Header(linesep.join(lines),
                   header_name='Received',
                   continuation_ws=continuation_ws)
        return h

    def log_invalid_recipient(self, envelope, exn):
        logger.error(repr(exn))

    def handle_invalid_recipient(self, envelope, exn):
        pass

    def get_extra_headers(self, envelope, group):
        headers = []
        fields = 'Sender List-Id List-Unsubscribe List-Help List-Subscribe'
        # Call get_sender_header, get_list_id_header,
        # get_list_unsubscribe_header, get_list_help_header,
        # get_list_subscribe_header
        for h in fields.split():
            method_name = 'get_%s_header' % h.lower().replace('-', '_')
            try:
                f = getattr(self, method_name)
            except AttributeError:
                continue
            value = f(envelope, group)
            if value is not None:
                headers.append((h, value))
        return headers

    def _add_extra_headers(self, envelope, group):
        for field, value in self.get_extra_headers(envelope, group):
            envelope.message.set_unique_header(field, value)

    def handle_envelope(self, envelope, peer):
        try:
            recipients = self.get_envelope_recipients(envelope)
        except InvalidRecipient as exn:
            self.log_invalid_recipient(envelope, exn)

            self.handle_invalid_recipient(envelope, exn)

            return '550 Requested action not taken: mailbox unavailable'

        new_subject = self.translate_subject(envelope)
        if new_subject is not None:
            envelope.message.subject = new_subject

        # Remove falsy recipients (empty string or None)
        recipients = [r for r in recipients if r]

        if all(isinstance(r, str) for r in recipients):
            # No extra data; forward the envelope in one delivery

            # Remove duplicates
            recipients = sorted(set(recipients))

            mailfrom = self.get_envelope_mailfrom(envelope)

            received = self._get_envelope_received_header(envelope, peer)

            if received is not None:
                envelope.message.add_received_line(received)

            self.deliver(envelope.message, recipients, mailfrom)

        else:
            original_envelope = envelope
            already_sent = set()
            for group in recipients:
                envelope = copy.deepcopy(original_envelope)
                mailfrom = self.get_envelope_mailfrom(
                    envelope, recipients=group)
                received = self._get_envelope_received_header(
                    envelope, peer, recipients=group)
                if received is not None:
                    envelope.message.add_received_line(received)
                group_recipients = self.get_group_recipients(group)

                # Remove duplicates
                group_recipients = set(group_recipients)
                # Remove recipients already sent to
                group_recipients -= already_sent
                if not group_recipients:
                    continue
                # Store new recipients
                already_sent |= group_recipients
                # Sort recipients for deterministic handling
                group_recipients = sorted(group_recipients)

                self._add_extra_headers(envelope, group)
                self.deliver(envelope.message, group_recipients, mailfrom)
