"""Health probe for the browser connection ("websocket") of the Reflex backend.

Does what the browser does when the page loads: opens the websocket on
/_event, waits for the server's greeting, joins the app's event channel and
waits for the confirmation. Exits 0 when all of that answers within the
timeout, 1 otherwise.

/ping alone is not enough: on 2026-10-21 /ping answered 200 while the page
showed "Cannot connect to server: websocket error" and the container sat at
100% CPU. Standard library only, so it runs with the plain system python.

Usage: python3 ws_probe.py [port] [timeout_seconds]
"""

import base64
import os
import socket
import sys

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
TIMEOUT = float(sys.argv[2]) if len(sys.argv) > 2 else 10
NAMESPACE = "/_event"


def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed")
        buf += chunk
    return buf


def recv_frame(sock):
    b1, b2 = recv_exact(sock, 2)
    length = b2 & 0x7F
    if length == 126:
        length = int.from_bytes(recv_exact(sock, 2), "big")
    elif length == 127:
        length = int.from_bytes(recv_exact(sock, 8), "big")
    if b2 & 0x80:
        recv_exact(sock, 4)  # servers do not mask, but be safe
    return b1 & 0x0F, recv_exact(sock, length)


def send_text(sock, text):
    payload = text.encode()
    mask = os.urandom(4)
    header = bytes([0x81, 0x80 | len(payload)])  # payloads here are < 126 bytes
    sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))


def wait_for(sock, prefix):
    """Read text frames until one starts with prefix (skips engine.io pings)."""
    while True:
        opcode, data = recv_frame(sock)
        if opcode == 0x8:
            raise ConnectionError("server closed the websocket")
        text = data.decode(errors="replace")
        if text.startswith(prefix):
            return text
        if text == "2":  # engine.io ping -> pong
            send_text(sock, "3")


def main():
    sock = socket.create_connection(("127.0.0.1", PORT), timeout=TIMEOUT)
    sock.settimeout(TIMEOUT)
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall(
        (
            "GET /_event/?EIO=4&transport=websocket HTTP/1.1\r\n"
            f"Host: localhost:{PORT}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode()
    )
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = sock.recv(1)
        if not chunk:
            raise ConnectionError("no answer to websocket upgrade")
        head += chunk
    if b" 101 " not in head.split(b"\r\n", 1)[0]:
        raise ConnectionError("upgrade refused: " + head.split(b"\r\n", 1)[0].decode())
    wait_for(sock, "0")  # engine.io open
    send_text(sock, f"40{NAMESPACE},")  # join the event channel
    reply = wait_for(sock, f"4")
    if not reply.startswith(f"40{NAMESPACE}"):
        raise ConnectionError("channel join refused: " + reply[:80])
    sock.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # any failure = unhealthy
        print(f"websocket probe failed: {e}", file=sys.stderr)
        sys.exit(1)
