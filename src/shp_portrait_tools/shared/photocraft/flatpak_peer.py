"""Non-cryptographic Linux check of an established Flatpak peer."""

import configparser
import os
from pathlib import Path
import select
import socket
import time


APP_ID = "ai.storyteller.photocraft"


class PeerIdentityError(RuntimeError):
    """The socket's peer could not be checked safely."""


def _process_identity(pid):
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return int(fields[19])


def _flatpak_id(pid):
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    with Path(f"/proc/{pid}/root/.flatpak-info").open() as stream:
        parser.read_file(stream)
    return parser["Application"]["name"]


def _tcp_address(address):
    host, port = address
    if host != "127.0.0.1" or not 0 < port <= 65535:
        raise PeerIdentityError("Expected an IPv4 loopback endpoint")
    return f"0100007F:{port:04X}"


def _socket_inodes(local, remote):
    listener = set()
    peer = set()
    for line in Path("/proc/net/tcp").read_text().splitlines()[1:]:
        fields = line.split()
        if len(fields) < 10:
            raise PeerIdentityError("Invalid kernel TCP table")
        if fields[1] == remote and fields[3] == "0A":
            listener.add(fields[9])
        elif fields[1] == remote and fields[2] == local and fields[3] == "01" and fields[9] != "0":
            peer.add(fields[9])
    return listener, peer


def _owned_inodes(pid):
    result = set()
    for entry in Path(f"/proc/{pid}/fd").iterdir():
        try:
            target = os.readlink(entry)
        except FileNotFoundError:
            continue
        if target.startswith("socket:[") and target.endswith("]"):
            result.add(target[8:-1])
    return result


def _verify(sock, pid, expected_executable, identity_check, timeout):
    if sock.family != socket.AF_INET or sock.type & socket.SOCK_STREAM != socket.SOCK_STREAM:
        raise PeerIdentityError("Expected an established IPv4 TCP socket")
    local = _tcp_address(sock.getsockname())
    remote = _tcp_address(sock.getpeername())
    if type(pid) is not int or pid <= 0 or not 0 < timeout <= 3:
        raise PeerIdentityError("Invalid process identity or timeout")
    expected = Path(expected_executable).stat()
    start = _process_identity(pid)
    pidfd = os.pidfd_open(pid)
    try:
        def verify_process():
            if select.select([pidfd], [], [], 0)[0] or _process_identity(pid) != start:
                raise PeerIdentityError("Editor process exited or changed")
            proc = Path(f"/proc/{pid}")
            executable = (proc / "exe").stat()
            if proc.stat().st_uid != os.getuid() or (
                executable.st_dev, executable.st_ino
            ) != (expected.st_dev, expected.st_ino):
                raise PeerIdentityError("Editor process owner or executable does not match")
            if not identity_check(pid):
                raise PeerIdentityError("Editor sandbox identity does not match")

        deadline = time.monotonic() + timeout
        while True:
            verify_process()
            listeners, peers = _socket_inodes(local, remote)
            owned = _owned_inodes(pid)
            if len(listeners) == 1 and len(peers) == 1 and listeners <= owned and peers <= owned:
                break
            if time.monotonic() >= deadline:
                raise PeerIdentityError("Listener and accepted connection ownership could not be verified")
            time.sleep(0.01)
        verify_process()
        if _socket_inodes(local, remote) != (listeners, peers) or not (listeners | peers) <= _owned_inodes(pid):
            raise PeerIdentityError("Socket ownership changed during verification")
    finally:
        os.close(pidfd)


def verify_flatpak_peer(sock, pid, expected_executable, *, timeout=1.0):
    """Check one already-connected socket; caller must use that exact socket.

    This is not server authentication and does not defend against a malicious
    process belonging to the same user. The caller must close the socket on
    failure and must repeat the check on every reconnect before sending a token.
    """
    try:
        _verify(sock, pid, expected_executable, lambda value: _flatpak_id(value) == APP_ID, timeout)
    except PeerIdentityError:
        sock.close()
        raise
    except (OSError, ValueError, KeyError, IndexError, AttributeError, configparser.Error) as error:
        sock.close()
        raise PeerIdentityError("Could not verify PhotoCraft process or socket") from error


def guard_client_authentication(client, pid, expected_executable):
    """Verify each new connection before the client sends its auth request.

    The caller must independently obtain the expected PID and executable from a
    verified PhotoCraft deployment. This guard is not wired into the installer.
    """
    exchange = client._exchange

    def checked_exchange(message):
        if message.get("method") == "auth":
            verify_flatpak_peer(client._sock, pid, expected_executable)
        return exchange(message)

    client._exchange = checked_exchange
    return client


def guard_discovered_client(client, resolve_peer):
    """Re-resolve the pinned process and inspect its socket on every auth."""
    exchange = client._exchange

    def checked_exchange(message):
        if message.get("method") == "auth":
            checked = False
            try:
                pid, executable = resolve_peer()
                verify_flatpak_peer(client._sock, pid, executable)
                checked = True
            finally:
                if not checked and client._sock is not None:
                    client._sock.close()
        return exchange(message)

    client._exchange = checked_exchange
    return client
