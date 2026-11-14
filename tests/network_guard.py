"""Refuse connections and name lookups beyond loopback, so no test can reach an external service (DESIGN C58).

The guard replaces the Python ``socket`` layer's entry points: ``socket.socket.connect``, ``connect_ex``,
``sendto`` and ``sendmsg``, and the module functions ``getaddrinfo``, ``gethostbyname``, ``gethostbyname_ex``,
``gethostbyaddr`` and ``getnameinfo``. Everything
built on them is covered, including ``http.client``, ``urllib``, ``requests``, asyncio, uvicorn and yt-dlp.
Native code that opens its own sockets, and child processes other than those that install the guard
themselves, are not covered.

A refused attempt raises ``ExternalNetworkError``, an ``OSError``, so the code under test fails as on a
refused connection instead of hanging. Code under test may swallow that error, so every attempt is also
reported, and the owner fails the test from the report: ``tests/conftest.py`` per test, and the lifecycle
service through its output.
"""

import ipaddress
import socket
import sys
from collections.abc import Callable

MARKER = 'NETWORK GUARD REFUSED'
LOOPBACK_NAMES = frozenset({'localhost', 'localhost.'})

type Report = Callable[[str], None]
type Patch = Callable[[object, str, object], None]


class ExternalNetworkError(OSError):
    """A connection or lookup the guard refused, because its destination is not loopback."""


def is_loopback(host: object) -> bool:
    """Return whether a host names loopback: ``None``, ``localhost`` or a name under it, or a loopback address."""
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode('ascii', 'replace')
    if not isinstance(host, str):
        return False
    name = host.lower()
    if name in LOOPBACK_NAMES or name.rstrip('.').endswith('.localhost'):
        return True
    try:
        return ipaddress.ip_address(name.split('%', 1)[0]).is_loopback
    except ValueError:
        return False


class NetworkGuard:
    """Replacements for the socket entry points, which refuse and report every destination beyond loopback."""

    def __init__(self, report: Report) -> None:
        """Remember where refusals are reported and capture the originals the replacements call."""
        self._report = report
        self._connect = socket.socket.connect
        self._connect_ex = socket.socket.connect_ex
        self._sendto = socket.socket.sendto
        self._sendmsg = socket.socket.sendmsg
        self._getaddrinfo = socket.getaddrinfo
        self._gethostbyname = socket.gethostbyname
        self._gethostbyname_ex = socket.gethostbyname_ex
        self._gethostbyaddr = socket.gethostbyaddr
        self._getnameinfo = socket.getnameinfo

    def install(self, patch: Patch) -> None:
        """Install every replacement with ``patch(target, name, value)``, such as ``monkeypatch.setattr``."""
        guard = self

        def connect(sock: socket.socket, address: object) -> None:
            """Connect unless the destination is beyond loopback."""
            guard._check_address(sock, address, 'connect')
            guard._connect(sock, address)

        def connect_ex(sock: socket.socket, address: object) -> int:
            """Connect, reporting errors by number, unless the destination is beyond loopback."""
            guard._check_address(sock, address, 'connect_ex')
            return guard._connect_ex(sock, address)

        def sendto(sock: socket.socket, data: bytes, *args: object) -> int:
            """Send a datagram unless its destination, the last argument, is beyond loopback."""
            guard._check_address(sock, args[-1] if args else None, 'sendto')
            return guard._sendto(sock, data, *args)

        def sendmsg(sock: socket.socket, buffers: object, *args: object) -> int:
            """Send a message unless its destination, the fourth argument when given, is beyond loopback."""
            if len(args) > 2 and args[2] is not None:
                guard._check_address(sock, args[2], 'sendmsg')
            return guard._sendmsg(sock, buffers, *args)

        def getaddrinfo(host: object, *args: object, **kwargs: object) -> object:
            """Resolve a loopback host; refuse any other."""
            guard._check_host(host, 'getaddrinfo')
            return guard._getaddrinfo(host, *args, **kwargs)

        def gethostbyname(host: str) -> str:
            """Resolve a loopback host; refuse any other."""
            guard._check_host(host, 'gethostbyname')
            return guard._gethostbyname(host)

        def gethostbyname_ex(host: str) -> tuple[str, list[str], list[str]]:
            """Resolve a loopback host; refuse any other."""
            guard._check_host(host, 'gethostbyname_ex')
            return guard._gethostbyname_ex(host)

        def gethostbyaddr(host: str) -> tuple[str, list[str], list[str]]:
            """Reverse-resolve a loopback address; refuse any other."""
            guard._check_host(host, 'gethostbyaddr')
            return guard._gethostbyaddr(host)

        def getnameinfo(address: tuple[str, int], flags: int) -> tuple[str, str]:
            """Reverse-resolve a loopback socket address; refuse any other."""
            guard._check_host(address[0] if isinstance(address, tuple) and address else address, 'getnameinfo')
            return guard._getnameinfo(address, flags)

        for name, replacement in (
            ('connect', connect), ('connect_ex', connect_ex), ('sendto', sendto), ('sendmsg', sendmsg),
        ):
            patch(socket.socket, name, replacement)
        for name, replacement in (
            ('getaddrinfo', getaddrinfo), ('gethostbyname', gethostbyname), ('gethostbyname_ex', gethostbyname_ex),
            ('gethostbyaddr', gethostbyaddr), ('getnameinfo', getnameinfo),
        ):
            patch(socket, name, replacement)

    def _check_address(self, sock: socket.socket, address: object, operation: str) -> None:
        """Refuse an IP socket's destination beyond loopback; other families, such as Unix sockets, are local."""
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            host = address[0] if isinstance(address, tuple) and address else address
            if not is_loopback(host):
                self._refuse(f'{operation} to {address!r}')

    def _check_host(self, host: object, operation: str) -> None:
        """Refuse a lookup of anything but loopback, IP literals included."""
        if not is_loopback(host):
            self._refuse(f'{operation} of {host!r}')

    def _refuse(self, attempt: str) -> None:
        """Report the attempt, then raise so that it reaches nothing."""
        message = f'{MARKER}: {attempt}; tests must not reach beyond loopback (AGENTS.md, Tests)'
        self._report(message)
        raise ExternalNetworkError(message)


def guard_process() -> None:
    """Guard the whole current process for good, reporting each refusal on standard error.

    For a child process the suite starts, whose owner reads its output for ``MARKER``.
    """

    def report(message: str) -> None:
        """Write the refusal to standard error at once, so it is in the output however the process ends."""
        print(message, file=sys.stderr, flush=True)

    NetworkGuard(report).install(setattr)
