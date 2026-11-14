"""Tests for the network guard every test runs under (tests/network_guard.py, DESIGN C58).

Refused destinations are reserved, so a guard that failed to refuse would still reach nothing real:
192.0.2.1 is TEST-NET-1 (RFC 5737), 2001:db8::1 the IPv6 documentation prefix (RFC 3849) and
``.invalid`` a reserved name (RFC 2606).
"""

import http.client
import socket
from http.server import BaseHTTPRequestHandler

import network_guard as testee
import pytest

ADDRESS = ('192.0.2.1', 9)
# Bounds an attempt a broken guard let through, which would otherwise wait out TCP's retries.
TIMEOUT_SECONDS = 1


class OkHandler(BaseHTTPRequestHandler):
    """Answers every request with an empty 200."""

    def do_GET(self):
        """Send an empty 200."""
        self.send_response(200)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def log_message(self, format, *args):
        """Keep request logs out of the test's streams."""


@pytest.mark.parametrize('host', [
    None, 'localhost', 'LOCALHOST.', 'api.localhost', b'localhost', '127.0.0.1', '127.8.9.10', b'127.0.0.1', '::1',
])
def test_is_loopback_accepts_loopback(host):
    """Loopback names and addresses are allowed."""
    assert testee.is_loopback(host)


@pytest.mark.parametrize('host', [
    '192.0.2.1', '2001:db8::1', 'example.invalid', '', '0.0.0.0', '::', 'localhost.example.invalid',
    'notlocalhost', 42,
])
def test_is_loopback_rejects_everything_else(host):
    """Everything else, including the unspecified addresses and look-alike names, is refused."""
    assert not testee.is_loopback(host)


def connect():
    """Connect a TCP socket to the reserved address."""
    with socket.socket() as sock:
        sock.settimeout(TIMEOUT_SECONDS)
        sock.connect(ADDRESS)


def connect_ex():
    """Connect a TCP socket to the reserved address, reporting errors by number."""
    with socket.socket() as sock:
        sock.settimeout(TIMEOUT_SECONDS)
        sock.connect_ex(ADDRESS)


def connect_ipv6():
    """Connect an IPv6 TCP socket to the reserved documentation address."""
    with socket.socket(socket.AF_INET6) as sock:
        sock.settimeout(TIMEOUT_SECONDS)
        sock.connect(('2001:db8::1', 9, 0, 0))


def sendto():
    """Send a UDP datagram to the reserved address."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(b'x', ADDRESS)


def sendto_with_flags():
    """Send a UDP datagram to the reserved address, with flags."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(b'x', 0, ADDRESS)


def sendmsg():
    """Send a UDP message to the reserved address."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendmsg([b'x'], [], 0, ADDRESS)


@pytest.mark.parametrize(('attempt', 'destination'), [
    (connect, "connect to ('192.0.2.1', 9)"),
    (connect_ex, "connect_ex to ('192.0.2.1', 9)"),
    (connect_ipv6, "connect to ('2001:db8::1', 9, 0, 0)"),
    (sendto, "sendto to ('192.0.2.1', 9)"),
    (sendto_with_flags, "sendto to ('192.0.2.1', 9)"),
    (sendmsg, "sendmsg to ('192.0.2.1', 9)"),
    (lambda: socket.create_connection(ADDRESS, timeout=TIMEOUT_SECONDS), "getaddrinfo of '192.0.2.1'"),
    (lambda: socket.getaddrinfo('example.invalid', 80), "getaddrinfo of 'example.invalid'"),
    (lambda: socket.gethostbyname('example.invalid'), "gethostbyname of 'example.invalid'"),
    (lambda: socket.gethostbyname_ex('example.invalid'), "gethostbyname_ex of 'example.invalid'"),
    (lambda: socket.gethostbyaddr('192.0.2.1'), "gethostbyaddr of '192.0.2.1'"),
    (lambda: socket.getnameinfo(ADDRESS, 0), "getnameinfo of '192.0.2.1'"),
], ids=['connect', 'connect_ex', 'connect_ipv6', 'sendto', 'sendto_flags', 'sendmsg', 'create_connection',
        'getaddrinfo', 'gethostbyname', 'gethostbyname_ex', 'gethostbyaddr', 'getnameinfo'])
def test_every_test_is_guarded_against_external_destinations(network_refusals, attempt, destination):
    """Under the automatic guard, each entry point refuses an external destination, names it and records it."""
    try:
        with pytest.raises(testee.ExternalNetworkError, match=f'{testee.MARKER}: ') as refused:
            attempt()
        assert destination in str(refused.value)
    finally:  # taken even after a failure, so that the failure stays the only report
        taken = network_refusals.take()
    assert taken == [str(refused.value)]


def test_a_refusal_is_an_os_error():
    """Code under test sees a refusal as it would a refused connection, so it fails rather than hangs."""
    assert issubclass(testee.ExternalNetworkError, OSError)


@pytest.mark.parametrize('host', ['127.0.0.1', 'localhost'])
def test_loopback_connections_work(loopback_server, host):
    """Requests to the suite's own loopback servers, by address or by name, pass the guard."""
    port = int(loopback_server(OkHandler).rsplit(':', 1)[1])
    connection = http.client.HTTPConnection(host, port, timeout=10)
    try:
        connection.request('GET', '/')
        assert connection.getresponse().status == 200
    finally:
        connection.close()


def test_unix_sockets_work(tmp_path):
    """Unix sockets are local, so the guard lets them connect."""
    path = str(tmp_path / 'sock')
    with socket.socket(socket.AF_UNIX) as server, socket.socket(socket.AF_UNIX) as client:
        server.bind(path)
        server.listen()
        client.connect(path)
        server.accept()[0].close()


def test_connected_datagrams_to_loopback_work():
    """A loopback datagram passes, whether addressed by sendto or sendmsg."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver, \
            socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
        receiver.bind(('127.0.0.1', 0))
        receiver.settimeout(TIMEOUT_SECONDS)
        sender.sendto(b'a', receiver.getsockname())
        sender.sendmsg([b'b'], [], 0, receiver.getsockname())
        sender.connect(receiver.getsockname())
        sender.sendmsg([b'c'])
        assert [receiver.recv(1) for _ in range(3)] == [b'a', b'b', b'c']


def test_install_and_restore_replace_every_entry_point():
    """Installing replaces each entry point; leaving the patch context restores exactly what was there."""
    targets = [(socket.socket, name) for name in ('connect', 'connect_ex', 'sendto', 'sendmsg')]
    targets += [(socket, name) for name in (
        'getaddrinfo', 'gethostbyname', 'gethostbyname_ex', 'gethostbyaddr', 'getnameinfo',
    )]
    before = [getattr(target, name) for target, name in targets]
    reports = []
    with pytest.MonkeyPatch.context() as patch:
        testee.NetworkGuard(reports.append).install(patch.setattr)
        assert all(getattr(target, name) is not old for (target, name), old in zip(targets, before, strict=True))
    assert [getattr(target, name) for target, name in targets] == before
    assert reports == []
