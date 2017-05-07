import logging
import argparse

from emailtunnel import LoggingReceiver, SMTPForwarder


def validate_address(v):
    host, port = v.split(':')
    port = int(port)
    return host, port


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--listen',
        type=validate_address,
        default=('0.0.0.0', 9000),
        help='hostname and port to listen on (default 0.0.0.0:9000)',
    )
    parser.add_argument(
        '--relay',
        type=validate_address,
        default=('127.0.0.1', 25),
        help='hostname and port to relay to (default 127.0.0.1:25)',
    )
    parser.add_argument(
        '--log',
        action='store_true',
        help='instead of relaying email, log all received',
    )
    args = parser.parse_args()
    if not args.log and not args.relay:
        parser.error("Must specify either --relay or --log")

    receiver_host, receiver_port = args.listen

    if args.log:
        logging.debug("Start LoggingReceiver")
        server = LoggingReceiver(receiver_host, receiver_port)

    else:
        logging.debug("Start SMTPForwarder")
        relay_host, relay_port = args.relay
        server = SMTPForwarder(
            receiver_host, receiver_port,
            relay_host, relay_port)

    server.run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)

    main()
