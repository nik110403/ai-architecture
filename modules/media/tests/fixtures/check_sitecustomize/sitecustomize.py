"""Test-only: any connection or name lookup that is not loopback raises and leaves a sentinel file.
Loaded through PYTHONPATH by the suite when it runs the CLI; never imported by the module itself."""
import os
import socket

_SENTINEL = os.environ.get("AIARCH_TEST_SENTINEL")
_LOCAL = ("127.0.0.1", "::1", "localhost", "")
_real_connect, _real_create, _real_lookup = socket.socket.connect, socket.create_connection, socket.getaddrinfo


def _hit(what):
    if _SENTINEL:
        with open(_SENTINEL, "a") as handle:
            handle.write(what + "\n")
    raise OSError("test: off-host network refused")


def _local(address):
    return isinstance(address, tuple) and bool(address) and address[0] in _LOCAL


def _connect(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6) and not _local(address):
        _hit("connect")
    return _real_connect(self, address)


def _create(address, *args, **kwargs):
    if not _local(address):
        _hit("create_connection")
    return _real_create(address, *args, **kwargs)


def _lookup(host, *args, **kwargs):
    if host not in _LOCAL:
        _hit("getaddrinfo")
    return _real_lookup(host, *args, **kwargs)


socket.socket.connect, socket.create_connection, socket.getaddrinfo = _connect, _create, _lookup
