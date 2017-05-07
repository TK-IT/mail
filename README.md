## A lightweight email forwarding framework

### Introduction

`emailtunnel` is a small Python 3 framework that uses the `smtplib` and `email`
standard library modules along with `aiosmtpd` to implement simple mailing
list forwarding.

The user must supply a function that maps symbolic recipient addresses on their
own domain to user email addresses.

A simple example, operating on the domain `maillist.local` with two users
and three lists:

```
USERS = {
    'admin@maillist.local': ['c2h5oh@example.com'],
    'luser@maillist.local': ['noreply@yahoo.com'],
    'all@maillist.local': ['c2h5oh@example.com', 'noreply@yahoo.com'],
}

class SimpleForwarder(emailtunnel.SMTPForwarder):
    def translate_recipient(self, rcptto):
        try:
            return USERS[rcptto]
        except KeyError:
            raise emailtunnel.InvalidRecipient(rcptto)

    def translate_subject(self, envelope):
        return '[Simple-List] %s' % envelope.message.subject
```

The `translate_recipient` method either returns a list of external recipients
to relay the envelope to, or the empty list to silently drop the email,
or it may raise `InvalidRecipient` to respond with SMTP error 550.
If another exception is raised while processing the message,
emailtunnel responds to the SMTP peer with SMTP error 451,
indicating that the error is temporary, and the peer should try again later.
In this case, the application should override `handle_error` to inform the
local admin of the failure.


### Repository overview

The framework is implemented in `emailtunnel/__init__.py`,
implementing the following classes:

* InvalidRecipient (exception)
* Message (encapsulating an instance of `email.message.Message`)
* Envelope (encapsulating a Message, a recipient and a sender)
* SMTPReceiver (abstract base class utilizing `aiosmtpd`)
* LoggingReceiver (simple implementation of SMTPReceiver)
* RelayMixin (mixin providing envelope delivery to a relay)
* SMTPForwarder (subclass of SMTPReceiver)

The framework may be tested by running `python -m emailtunnel --help`,
which runs the code in `emailtunnel/__main__.py`
that allows simple logging and relaying of emails.

The `emailtunnel.send` module may be run from the command line to send simple
emails specified via command line parameters and standard input.


### Application example

The `tkmail.server` module implements `TKForwarder`, an application of
`emailtunnel.SMTPForwarder`.

It supports logging of exceptions and misdeliveries to a list of admins,
and it uses the `tkmail.address` module to perform delicate parsing of
recipient addresses.

The `TKForwarder` is started by running the `tkmail` module from the command
line by calling `python -m tkmail --help`.

The `tkmail.monitor` module is designed to be run daily from a cron job,
and it checks the error directory and sends a report to admins.

The `tkmail.test` module starts an instance of `TKForwarder`,
feeds it test messages and checks the relayed messages for correctness.


### SMTPForwarder logic

When an SMTP client connects and sends an SMTP envelope,
the method `SMTPReceiver.process_message` is invoked by `emailtunnel`.
First, the message data is stored in an instance of `Message`,
which performs a sanity roundtrip parsing check to make sure that
`data == str(Message(data))` modulo trailing whitespace.

Then, the envelope is passed to `handle_envelope`,
which is implemented in a subclass (such as SMTPForwarder).
If `handle_envelope` returns None, `emailtunnel` assumes that the envelope was
successfully delivered.
Otherwise, it must return a string, which is returned to the SMTP remote peer.
If an exception occurs in `handle_envelope`, SMTP error 451 is returned to the
peer ("Requested action aborted: error in processing").
The subclass may implement `handle_error` to do further logging.

The `SMTPForwarder` class implements `handle_envelope`
by transforming the Subject via `translate_subject` in the subclass
and by transforming the list of recipients via `get_envelope_recipients`.
The default implementation of `get_envelope_recipients` transforms each
recipient using `translate_recipient`, which is the identity map by default.
The forwarded envelope has the sender provided in `get_envelope_mailfrom`.
The default implementation of `get_envelope_mailfrom` returns the sender
of the incoming envelope as the outgoing sender.

The envelope is passed on with only the subject changed
using `RelayMixin.deliver`, which requires the attributes
`relay_host` and `relay_port` to be set.

If `InvalidRecipient` is raised during `get_envelope_recipients`, SMTP error
550 is returned to the SMTP peer (mailbox unavailable) and no email is relayed.
