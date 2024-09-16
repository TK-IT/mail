import os
import re
import sys
import json
import datetime
import textwrap
import itertools
import traceback
from collections import namedtuple, OrderedDict

import email.header
import email.utils

from emailtunnel import (
    SMTPForwarder, Message, InvalidRecipient, Envelope, logger,
)

import tkmail.address

from tkmail.address import (
    GroupAlias,
    # PeriodAlias, DirectAlias,
)
from tkmail.dmarc import has_strict_dmarc_policy
from tkmail.delivery_reports import parse_delivery_report
import tkmail.headers
from emailtunnel.mailhole import MailholeRelayMixin


RecipientGroup = namedtuple(
    'RecipientGroup', 'origin recipients'.split())


def now_string():
    """Return the current date and time as a string."""
    return datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S.%f")


class TKForwarder(SMTPForwarder, MailholeRelayMixin):
    REWRITE_FROM = False
    STRIP_HTML = False

    # MAIL_FROM = 'admin@TAAGEKAMMERET.dk'
    MAIL_FROM = None

    ERROR_TEMPLATE = """
    This is the mail system of TAAGEKAMMERET.

    The following exception was raised when processing the message below:

    {traceback}

    This exception will not be reported again before the mail server has
    been restarted.

    Envelope sender: {mailfrom}
    Envelope recipients: {rcpttos}
    Envelope message hidden.
    """

    ERROR_TEMPLATE_CONSTRUCTION = """
    This is the mail system of TAAGEKAMMERET.

    The following exception was raised when CONSTRUCTING AN ENVELOPE:

    {traceback}

    This exception will not be reported again before the mail server has
    been restarted.

    Raw data:

    {data}
    """

    def __init__(self, *args, **kwargs):
        self.year = tkmail.address.get_current_period()
        self.exceptions = set()
        self.delivered = 0
        self.deliver_recipients = {}
        super(TKForwarder, self).__init__(*args, **kwargs)

    def should_mailhole(self, message, recipient, sender):
        # Send everything to mailhole
        return True

    def startup_log(self):
        logger.info(
            'TKForwarder listening on %s:%s, relaying to %s:%s, GF year %s',
            self.host, self.port, self.relay_host, self.relay_port, self.year)

    def log_receipt(self, peer, envelope):
        mailfrom = envelope.mailfrom
        message = envelope.message

        if type(mailfrom) == str:
            sender = '<%s>' % mailfrom
        else:
            sender = repr(mailfrom)

        try:
            recipients_header = OrderedDict()
            for address, formatted, header in envelope.recipients():
                if address is not None:
                    address = re.sub(r'@(T)AAGE(K)AMMERET\.dk$', r'@@\1\2',
                                     address, 0, re.I)
                    recipients_header.setdefault(header, []).append(address)
            recipients = ' '.join(
                '%s: <%s>' % (header, '>, <'.join(group))
                for header, group in recipients_header.items())
        except Exception as exn:
            logger.exception('Envelope.recipients() processing failed')
            rcpttos = envelope.rcpttos
            if type(rcpttos) == list and all(type(x) == str for x in rcpttos):
                rcpttos = [re.sub(r'@(T)AAGE(K)AMMERET\.dk$', r'@@\1\2',
                                  address, 0, re.I)
                           for address in rcpttos]
                if len(rcpttos) == 1:
                    recipients = '<%s>' % rcpttos[0]
                else:
                    recipients = ', '.join('<%s>' % x for x in rcpttos)
            else:
                recipients = repr(rcpttos)
            recipients = 'To: ' + recipients

        logger.info("%s", recipients)

    def log_delivery(self, message, recipients, sender):
        if all('@' in rcpt for rcpt in recipients):
            parts = [rcpt.split('@', 1) for rcpt in recipients]
            parts.sort(key=lambda x: (x[1].lower(), x[0].lower()))
            by_domain = [
                (domain, [a[0] for a in aa])
                for domain, aa in itertools.groupby(
                    parts, key=lambda x: x[1])
            ]
            recipients_string = ', '.join(
                '<%s@%s>' % (','.join(aa), domain)
                for domain, aa in by_domain)
        else:
            recipients_string = ', '.join('<%s>' % x for x in recipients)

        if len(recipients_string) > 200:
            age = self.deliver_recipients.get(recipients_string)
            if age is None or self.delivered - age > 40:
                self.deliver_recipients[recipients_string] = self.delivered
                recipients_string += ' [%d]' % self.delivered
            else:
                recipients_string = '%s... [%d]' % (
                    recipients_string[:197], age)

        self.delivered += 1

        logger.info('To: %s', recipients_string)

    def handle_delivery_report(self, envelope):
        if envelope.mailfrom != '<>':
            return
        rcpttos = tuple(r.lower() for r in envelope.rcpttos)
        if rcpttos != ('admin@taagekammeret.dk',):
            return
        report = parse_delivery_report(envelope.message.message)
        if not report:
            return
        original_mailfrom = report.message.get('Return-Path') or '(unknown)'
        inner_envelope = Envelope(Message(report.message), original_mailfrom,
                                  report.recipients)
        description = summary = report.notification
        self.store_failed_envelope(envelope, description, summary,
                                   inner_envelope)
        logger.info('%s', summary)
        return True

    def reject(self, envelope):
        # Reject delivery status notifications not sent to admin@
        try:
            content_type = envelope.message.get_unique_header('Content-Type')
        except KeyError:
            content_type = ''
        ctype_report = content_type.startswith('multipart/report')
        ctype_delivery = 'report-type=delivery-status' in content_type
        if ctype_report and ctype_delivery:
            return 'Content-Type looks like a DSN'

        rcpttos = tuple(r.lower() for r in envelope.rcpttos)
        to_admin = rcpttos == ('admin@taagekammeret.dk',)
        subject = envelope.message.subject
        subject_str = str(subject)
        delivery_status_subject = (
            'Delayed Mail' in subject_str or
            'Undelivered Mail Returned to Sender' in subject_str)
        if to_admin and delivery_status_subject:
            return 'Subject looks like a DSN'

        # Reject if a header is not encoded properly
        header_items = envelope.message.header_items()
        headers = [header for field, header in header_items]
        chunks = sum((header._chunks for header in headers), [])
        any_unknown = any(charset == email.charset.UNKNOWN8BIT
                          for string, charset in chunks)
        if any_unknown:
            return 'invalid header encoding'

        if envelope.mailfrom == '<>':
            # RFC 5321, 4.5.5. Messages with a Null Reverse-Path:
            # "[Automated email processors] SHOULD NOT reply to messages
            # with a null reverse-path, and they SHOULD NOT add a non-null
            # reverse-path, or change a null reverse-path to a non-null one,
            # to such messages when forwarding."
            # Since we would forward this message with a non-null reverse-path,
            # we should reject it instead.
            return 'null reverse-path'

        n_from = len(envelope.message.get_all_headers('From'))
        if n_from != 1:
            return 'wrong number of From-headers (%s)' % n_from
        if not envelope.from_domain:
            return 'invalid From-header'
        if not self.REWRITE_FROM:
            dkim_sigs = envelope.message.get_all_headers('DKIM-Signature')
            if envelope.strict_dmarc_policy and not dkim_sigs:
                return ('%s has strict DMARC policy, ' % envelope.from_domain +
                        'but message has no DKIM-Signature header')

    def handle_envelope(self, envelope, peer):
        # Call get_current_period only once per envelope
        self.year = tkmail.address.get_current_period()
        if self.handle_delivery_report(envelope):
            return
        envelope.from_domain = self.get_from_domain(envelope)
        envelope.strict_dmarc_policy = self.strict_dmarc_policy(envelope)
        reject_reason = self.reject(envelope)
        if reject_reason:
            summary = 'Rejected by TKForwarder.reject (%s)' % reject_reason
            logger.info('%s', summary)
            self.store_failed_envelope(envelope, summary, summary)
            return
        if not self.REWRITE_FROM and not self.STRIP_HTML:
            self.fix_headers(envelope.message)
        return super(TKForwarder, self).handle_envelope(envelope, peer)

    def fix_headers(self, message):
        # Fix References: header that has been broken by Postfix.
        # If an email has a DKIM-Signature on the RFC5322.References header,
        # and Postfix breaks RFC5322.References into multiple lines because
        # some lines were longer than 990 bytes, then the DKIM-Signature
        # becomes invalid. Since no sane email provider would apply a
        # DKIM-Signature to a RFC5322.References header with erroneous
        # whitespace, we remove any erroneous whitespace, that is,
        # whitespace between two angle brackets <...>, from the header.
        references_header = message.get_all_headers('References')
        fixed_header = []
        for v in references_header:
            # Move space/newline in front of the '<'.
            v2 = re.sub(r'(<[^<> \n\r\t]*)([ \n\r\t]+)', r'\2\1', v)
            if v != v2:
                # We had to move some whitespace to fix the header,
                # so we may inadvertently have created new too-long lines.
                # Replace all whitespace with CR LF SP to avoid long lines.
                v2 = '\r\n '.join(v2.split())
            fixed_header.append(v2)
        if references_header == fixed_header:
            # No change required
            return
        # We had to fix RFC5322.References. In order to use set_unique_header
        # (to retain the position of the header in the message) there must be
        # only one RFC5322.References in the message.
        try:
            v2, = fixed_header
        except ValueError:
            # Write fixed_header to log.
            logger.exception('Multiple References-headers: %r', fixed_header)
            # I think multiple RFC5322.References headers is invalid,
            # but the RFC does not make it entirely clear.
            # On the other hand, who would have both multiple
            # RFC5322.References *and* header lines longer than 990 bytes?
            raise NotImplementedError(
                'Multiple References:-headers and some/all are invalid')
        message.set_unique_header('References', v2)

    def get_from_domain(self, envelope):
        from_domain_mo = re.search(r'@([^ \t\n>]+)',
                                   envelope.message.get_header('From', ''))
        if from_domain_mo:
            return from_domain_mo.group(1)

    def strict_dmarc_policy(self, envelope):
        if envelope.from_domain:
            return has_strict_dmarc_policy(envelope.from_domain)

    def translate_recipient(self, rcptto):
        name, domain = rcptto.split('@')
        recipients = tkmail.address.translate_recipient(self.year, name)
        if not recipients:
            logger.info("%s resolved to the empty list", name)
            raise InvalidRecipient(rcptto)
        recipients.sort(key=lambda r: r[1])
        group_iter = itertools.groupby(recipients, key=lambda r: r[1])
        groups = []
        for origin, group in group_iter:
            email_addresses = [email_address for email_address, origin in group]
            groups.append(
                RecipientGroup(origin=origin, recipients=frozenset(email_addresses))
            )
        return groups

    def get_group_recipients(self, group):
        return group.recipients

    def get_envelope_mailfrom(self, envelope, recipients=None):
        if self.MAIL_FROM is not None:
            return self.MAIL_FROM
        return envelope.mailfrom

    def get_extra_headers(self, envelope, group):
        sender = self.get_envelope_mailfrom(envelope)
        list_name = str(group.origin).lower()
        is_group = isinstance(group.origin, GroupAlias)
        headers = tkmail.headers.get_extra_headers(sender, list_name, is_group)
        if self.REWRITE_FROM:
            orig_from = envelope.message.get_header("From")
            headers.append(("From", self.get_from_header(envelope, group)))
            headers.append(("Reply-To", orig_from))
        return headers

    def get_from_header(self, envelope, group):
        orig_from = envelope.message.get_header("From")
        orig_to = group.origin.name.lower()
        parsed = email.utils.getaddresses([orig_from])
        name = parsed[0][0]
        addr = "%s@TAAGEKAMMERET.dk" % orig_to.upper()
        return email.utils.formataddr(("%s via %s" % (name, orig_to), addr))

    def forward(self, original_envelope, message, recipients, sender):
        if self.REWRITE_FROM or self.STRIP_HTML:
            del message.message["DKIM-Signature"]
        if self.STRIP_HTML:
            from emailtunnel.extract_text import get_body_text

            t = get_body_text(message.message)
            message.set_unique_header("Content-Type", "text/plain")
            del message.message["Content-Transfer-Encoding"]
            charset = email.charset.Charset("utf-8")
            charset.header_encoding = charset.body_encoding = email.charset.QP
            message.message.set_payload(t, charset=charset)
        super().forward(original_envelope, message, recipients, sender)

    def log_invalid_recipient(self, envelope, exn):
        # Use logging.info instead of the default logging.error
        logger.info('Invalid recipient: %r', exn.args)

    def handle_invalid_recipient(self, envelope, exn):
        self.store_failed_envelope(
            envelope, str(exn), 'Invalid recipient: %s' % exn)

    def handle_error(self, envelope, str_data):
        exc_value = sys.exc_info()[1]
        exc_typename = type(exc_value).__name__
        filename, line, function, text = traceback.extract_tb(
            sys.exc_info()[2])[0]

        tb = ''.join(traceback.format_exc())
        if envelope:
            try:
                self.store_failed_envelope(
                    envelope, str(tb),
                    '%s: %s' % (exc_typename, exc_value))
            except:
                logger.exception("Could not store_failed_envelope")

        exc_key = (filename, line, exc_typename)

        if exc_key not in self.exceptions:
            self.exceptions.add(exc_key)
            self.forward_to_admin(envelope, str_data, tb)

    def forward_to_admin(self, envelope, str_data, tb):
        # admin_emails = tkmail.address.get_admin_emails()
        admin_emails = ['mathiasrav@gmail.com']

        sender = recipient = 'admin@TAAGEKAMMERET.dk'

        if envelope:
            subject = '[TK-mail] Unhandled exception in processing'
            body = textwrap.dedent(self.ERROR_TEMPLATE).format(
                traceback=tb, mailfrom=envelope.mailfrom,
                rcpttos=envelope.rcpttos)

        else:
            subject = '[TK-mail] Could not construct envelope'
            body = textwrap.dedent(self.ERROR_TEMPLATE_CONSTRUCTION).format(
                traceback=tb, data=str_data)

        admin_message = Message.compose(
            sender, recipient, subject, body)
        admin_message.add_header('Auto-Submitted', 'auto-replied')

        try:
            headers = tkmail.headers.get_extra_headers(sender, 'tkmailerror',
                                                       is_group=True)
            for k, v in headers:
                admin_message.add_header(k, v)
        except Exception:
            logger.exception("Could not add extra headers in forward_to_admin")

        self.deliver(admin_message, admin_emails, sender)

    def store_failed_envelope(self, envelope, description, summary,
                              inner_envelope=None):
        now = now_string()

        try:
            os.mkdir('error')
        except FileExistsError:
            pass

        if inner_envelope is None:
            inner_envelope = envelope
        with open('error/%s.json' % now, 'w') as fp:
            metadata = {
                'mailfrom': inner_envelope.mailfrom,
                'rcpttos': inner_envelope.rcpttos,
                'subject': str(inner_envelope.message.subject),
                'date': inner_envelope.message.get_header('Date'),
                'summary': summary,
            }
            json.dump(metadata, fp)

        with open('error/%s.txt' % now, 'w') as fp:
            fp.write('From: %s\n' % inner_envelope.mailfrom)
            fp.write('To: %s\n' % inner_envelope.rcpttos)
            try:
                fp.write('Subject: %s\n' % inner_envelope.message.subject)
            except Exception:
                fp.write('Unknown subject\n')
            try:
                fp.write('Date: %s\n' % inner_envelope.message.get_header('Date'))
            except Exception:
                fp.write('Unknown date\n')
            fp.write('Summary: %s\n' % summary)
            fp.write('\n%s\n' % description)
