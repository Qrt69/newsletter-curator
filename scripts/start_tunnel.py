"""
SSH Reverse Tunnel — LM Studio to VPS.

Forwards VPS port 22236 to local LM Studio on port 1234.
Keeps the tunnel alive with automatic reconnection on failure.

Usage:
    uv run python scripts/start_tunnel.py
    uv run python scripts/start_tunnel.py --check   # test if tunnel is already up
"""

import argparse
import subprocess
import socket
import sys
import time

SSH_HOST = "kurt@91.98.29.231"
REMOTE_PORT = 22236
LOCAL_HOST = "127.0.0.1"
LOCAL_PORT = 1234
RETRY_DELAY = 5  # seconds between reconnection attempts


def is_local_port_open() -> bool:
    """Check if LM Studio is listening on the local port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(2)
        return s.connect_ex((LOCAL_HOST, LOCAL_PORT)) == 0


def build_ssh_command() -> list[str]:
    return [
        "ssh",
        "-R", f"{LOCAL_HOST}:{REMOTE_PORT}:{LOCAL_HOST}:{LOCAL_PORT}",
        "-N",                    # no remote command
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        SSH_HOST,
    ]


def check_tunnel() -> bool:
    """Quick check: try to connect to the remote forwarded port via SSH."""
    result = subprocess.run(
        ["ssh", SSH_HOST, f"nc -z {LOCAL_HOST} {REMOTE_PORT}"],
        capture_output=True,
        timeout=10,
    )
    return result.returncode == 0


def run_tunnel():
    if not is_local_port_open():
        print(f"WARNING: LM Studio not detected on {LOCAL_HOST}:{LOCAL_PORT}")
        print("The tunnel will start but won't be useful until LM Studio is running.")

    cmd = build_ssh_command()
    print(f"Starting SSH tunnel: VPS:{REMOTE_PORT} -> local:{LOCAL_PORT}")
    print(f"Command: {' '.join(cmd)}")
    print("Press Ctrl+C to stop.\n")

    while True:
        proc = subprocess.Popen(cmd)
        try:
            proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
            proc.wait()
            print("\nTunnel stopped.")
            return

        print(f"Tunnel exited (code {proc.returncode}). Reconnecting in {RETRY_DELAY}s...")
        try:
            time.sleep(RETRY_DELAY)
        except KeyboardInterrupt:
            print("\nTunnel stopped.")
            return


def main():
    parser = argparse.ArgumentParser(description="SSH reverse tunnel to VPS")
    parser.add_argument("--check", action="store_true", help="Check if tunnel is active")
    args = parser.parse_args()

    if args.check:
        try:
            if check_tunnel():
                print(f"Tunnel is UP (VPS:{REMOTE_PORT} -> local:{LOCAL_PORT})")
                sys.exit(0)
            else:
                print(f"Tunnel is DOWN")
                sys.exit(1)
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            print(f"Check failed: {exc}")
            sys.exit(1)
    else:
        run_tunnel()


if __name__ == "__main__":
    main()
