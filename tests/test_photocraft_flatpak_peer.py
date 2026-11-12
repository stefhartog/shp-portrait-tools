"""Synthetic listener tests for the staging-only Flatpak peer check."""

import os
import socket
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from shp_photocraft_tools.shared.photocraft import flatpak_peer


SERVER = r"""
import socket, sys
listener = socket.socket()
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind(("127.0.0.1", int(sys.argv[1])))
listener.listen()
print(listener.getsockname()[1], flush=True)
connection, _ = listener.accept()
if sys.argv[2] == "close-listener":
    listener.close()
print("accepted", flush=True)
data = connection.recv(100)
print(len(data), flush=True)
connection.close()
listener.close()
"""

IMPOSTOR = r"""
import socket, sys
listener = socket.socket()
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind(("127.0.0.1", int(sys.argv[1])))
listener.listen()
print("ready", flush=True)
sys.stdin.read()
listener.close()
"""


class FlatpakPeerTests(unittest.TestCase):
    def setUp(self):
        self.children = []
        self.sockets = []
        app_id = patch.object(flatpak_peer, "_flatpak_id", return_value=flatpak_peer.APP_ID)
        app_id.start()
        self.addCleanup(app_id.stop)
        self.addCleanup(self.clean_up)

    def clean_up(self):
        for sock in self.sockets:
            sock.close()
        for child in self.children:
            if child.stdin:
                child.stdin.close()
            child.wait(timeout=5)
            if child.stdout:
                child.stdout.close()
            if child.stderr:
                child.stderr.close()

    def listener(self, mode="normal"):
        process = subprocess.Popen(
            [sys.executable, "-I", "-B", "-c", SERVER, "0", mode],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.children.append(process)
        port = int(process.stdout.readline())
        sock = socket.create_connection(("127.0.0.1", port), timeout=2)
        self.sockets.append(sock)
        self.assertEqual(process.stdout.readline().strip(), "accepted")
        return process, sock, port

    def verify(self, process, sock):
        flatpak_peer.verify_flatpak_peer(sock, process.pid, Path(sys.executable).resolve(), timeout=0.1)

    def test_exact_accepted_socket_and_listener_owned_by_expected_process(self):
        process, sock, _ = self.listener()
        self.verify(process, sock)
        sock.sendall(b"synthetic only")
        self.assertEqual(process.stdout.readline().strip(), "14")

    def test_wrong_process_and_wrong_flatpak_id_fail_closed(self):
        process, sock, _ = self.listener()
        with self.assertRaises(flatpak_peer.PeerIdentityError):
            flatpak_peer.verify_flatpak_peer(sock, os.getpid(), Path(sys.executable).resolve(), timeout=0.1)
        self.assertEqual(sock.fileno(), -1)
        process, sock, _ = self.listener()
        with patch.object(flatpak_peer, "_flatpak_id", return_value="not.photocraft"):
            with self.assertRaises(flatpak_peer.PeerIdentityError):
                self.verify(process, sock)
        self.assertEqual(sock.fileno(), -1)
        self.assertEqual(process.stdout.readline().strip(), "0")

    def test_authentication_is_checked_before_each_exchange(self):
        process, sock, _ = self.listener()

        class FakeClient:
            _sock = sock

            def __init__(self):
                self.sent = 0

            def _exchange(self, message):
                self.sent += 1
                return {"ok": True}

        client = flatpak_peer.guard_client_authentication(
            FakeClient(), process.pid, Path(sys.executable).resolve())
        with patch.object(flatpak_peer, "_flatpak_id", return_value="not.photocraft"):
            with self.assertRaises(flatpak_peer.PeerIdentityError):
                client._exchange({"method": "auth", "params": {"token": "synthetic"}})
        self.assertEqual(client.sent, 0)
        self.assertEqual(process.stdout.readline().strip(), "0")

        process, sock, _ = self.listener()
        client = FakeClient()
        client._sock = sock
        flatpak_peer.guard_client_authentication(client, process.pid, Path(sys.executable).resolve())
        self.assertEqual(client._exchange({"method": "auth", "params": {"token": "synthetic"}}), {"ok": True})
        with patch.object(flatpak_peer, "_flatpak_id", return_value="not.photocraft"):
            with self.assertRaises(flatpak_peer.PeerIdentityError):
                client._exchange({"method": "auth", "params": {"token": "synthetic"}})
        self.assertEqual(client.sent, 1)

    def test_executable_mismatch_and_unreadable_metadata_fail_closed(self):
        for mismatch in ("executable", "metadata"):
            with self.subTest(mismatch=mismatch):
                process, sock, _ = self.listener()
                expected = "/usr/bin/true" if mismatch == "executable" else Path(sys.executable).resolve()
                with patch.object(flatpak_peer, "_flatpak_id",
                                  side_effect=PermissionError() if mismatch == "metadata" else None,
                                  return_value=flatpak_peer.APP_ID):
                    with self.assertRaises(flatpak_peer.PeerIdentityError):
                        flatpak_peer.verify_flatpak_peer(sock, process.pid, expected, timeout=0.1)
                self.assertEqual(sock.fileno(), -1)

    def test_reused_port_does_not_match_existing_connection(self):
        process, sock, port = self.listener("close-listener")
        impostor = subprocess.Popen(
            [sys.executable, "-I", "-B", "-c", IMPOSTOR, str(port)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.children.append(impostor)
        self.assertEqual(impostor.stdout.readline().strip(), "ready")
        with self.assertRaises(flatpak_peer.PeerIdentityError):
            self.verify(process, sock)
        self.assertEqual(sock.fileno(), -1)

    def test_changed_tcp_table_and_inaccessible_fd_fail_closed(self):
        process, sock, _ = self.listener()
        original = flatpak_peer._socket_inodes
        calls = []

        def changed(*args):
            calls.append(None)
            value = original(*args)
            return value if len(calls) == 1 else (set(), value[1])

        with patch.object(flatpak_peer, "_socket_inodes", side_effect=changed):
            with self.assertRaises(flatpak_peer.PeerIdentityError):
                self.verify(process, sock)
        self.assertEqual(sock.fileno(), -1)
        process, sock, _ = self.listener()
        with patch.object(flatpak_peer, "_owned_inodes", side_effect=PermissionError()):
            with self.assertRaises(flatpak_peer.PeerIdentityError):
                self.verify(process, sock)
        self.assertEqual(sock.fileno(), -1)

    def test_exited_process_and_changed_identity_fail_closed(self):
        process, sock, _ = self.listener()
        sock.sendall(b"synthetic only")
        process.wait(timeout=5)
        with self.assertRaises(flatpak_peer.PeerIdentityError):
            self.verify(process, sock)
        self.assertEqual(sock.fileno(), -1)
        process, sock, _ = self.listener()
        with patch.object(flatpak_peer, "_flatpak_id",
                          side_effect=[flatpak_peer.APP_ID, "not.photocraft"]):
            with self.assertRaises(flatpak_peer.PeerIdentityError):
                self.verify(process, sock)
        self.assertEqual(sock.fileno(), -1)


if __name__ == "__main__":
    unittest.main()
