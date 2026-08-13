"""
SSH Reverse Tunnel — LM Studio to VPS.

Forwards VPS port 22236 to LM Studio on 127.0.0.1:1234. On the VPS a socat unit
(llm-relay) bridges 0.0.0.0:11234 -> 127.0.0.1:22236, which is what the curator
containers reach at http://172.17.0.1:11234/v1.

Supervises the tunnel rather than just starting it, because two failure modes
kept taking it down silently:

  1. After sleep/suspend the TCP link dies without either end noticing. The ssh
     client keeps running, the remote port stays bound, and every request into
     it hangs until timeout. Keepalives (ServerAlive*) now tear the client down
     within ~45s so the supervisor can reconnect.
  2. The sshd session of a dead tunnel keeps port 22236 bound. A new tunnel then
     fails to bind but — without ExitOnForwardFailure — stays alive doing
     nothing, which looks identical to a working tunnel from the outside. Now it
     exits, and the supervisor kills the stale session before retrying.

Usage:
    uv run python scripts/start_tunnel.py            # supervise (blocks)
    uv run python scripts/start_tunnel.py --check    # one-shot end-to-end probe
    uv run python scripts/start_tunnel.py --once     # start once, no supervision
"""

import argparse
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

SSH_HOST = "kurt@91.98.29.231"
REMOTE_PORT = 22236
LOCAL_HOST = "127.0.0.1"
LOCAL_PORT = 1234

RETRY_DELAY = 5             # seconds before the first reconnect attempt
MAX_RETRY_DELAY = 60        # ceiling for the backoff
STABLE_AFTER = 120          # a tunnel alive this long resets the backoff
HEALTH_INTERVAL = 300       # seconds between end-to-end probes
FIRST_HEALTH_DELAY = 20     # first probe shortly after connecting

# Only the Windows OpenSSH binary talks to the Windows ssh-agent service that
# holds the passphrase-protected key. Git-Bash and WSL ssh do not see it and
# fail with "Permission denied (publickey)".
_WIN_SSH = r"C:\Windows\System32\OpenSSH\ssh.exe"

logger = logging.getLogger("tunnel")


def _default_log_file() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "newsletter-curator"
    else:
        base = Path(os.environ.get("DATA_DIR", "."))
    return base / "tunnel.log"


def _setup_logging(log_file: Path):
    log_file.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    logger.addHandler(stream)


def ssh_binary() -> str:
    """Path to the ssh that can see the agent holding our key."""
    if os.name == "nt" and Path(_WIN_SSH).exists():
        return _WIN_SSH
    return shutil.which("ssh") or "ssh"


def _ssh_opts() -> list[str]:
    return [
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        # Bind failure must kill the client instead of leaving a silent no-op.
        "-o", "ExitOnForwardFailure=yes",
        # Detect a link that died during sleep within ~45 seconds.
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=3",
        "-o", "TCPKeepAlive=yes",
    ]


def build_ssh_command() -> list[str]:
    return [
        ssh_binary(),
        "-R", f"{LOCAL_HOST}:{REMOTE_PORT}:{LOCAL_HOST}:{LOCAL_PORT}",
        "-N",  # no remote command
        *_ssh_opts(),
        SSH_HOST,
    ]


def is_local_port_open() -> bool:
    """Check if LM Studio is listening on the local port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(2)
        return s.connect_ex((LOCAL_HOST, LOCAL_PORT)) == 0


def _run_remote(script: str, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a shell snippet on the VPS over its own short-lived ssh connection."""
    return subprocess.run(
        [ssh_binary(), *_ssh_opts(), SSH_HOST, script],
        capture_output=True, text=True, timeout=timeout,
    )


def health_check() -> bool:
    """
    End-to-end probe: ask the VPS to fetch the model list through the tunnel.

    This is the only check that proves the whole chain works. A listening port
    proves nothing — a dead tunnel accepts connections and never answers.
    """
    script = (
        f"curl -s -o /dev/null -w '%{{http_code}}' -m 5 "
        f"http://{LOCAL_HOST}:{REMOTE_PORT}/v1/models"
    )
    try:
        result = _run_remote(script, timeout=25)
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Health check could not run: %s", exc)
        return False
    return result.stdout.strip() == "200"


def free_remote_port() -> str:
    """
    Kill whatever holds REMOTE_PORT on the VPS (a stale sshd session).

    Returns one of: NO_LISTENER, FREED, STILL_BOUND, ERROR.
    """
    port = REMOTE_PORT
    script = f"""
    pids=$(ss -tlnp "sport = :{port}" 2>/dev/null | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u)
    if [ -z "$pids" ]; then
        ss -tln "sport = :{port}" 2>/dev/null | grep -q ":{port}" && echo FOREIGN_LISTENER || echo NO_LISTENER
        exit 0
    fi
    kill $pids 2>/dev/null
    sleep 2
    if ss -tln "sport = :{port}" 2>/dev/null | grep -q ":{port}"; then
        kill -9 $pids 2>/dev/null
        sleep 1
    fi
    ss -tln "sport = :{port}" 2>/dev/null | grep -q ":{port}" && echo STILL_BOUND || echo FREED
    """
    try:
        result = _run_remote(script, timeout=40)
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Could not clear remote port: %s", exc)
        return "ERROR"
    status = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "ERROR"
    logger.info("Remote port %d cleanup: %s", port, status)
    return status


def kill_stale_local_tunnels():
    """Kill any earlier tunnel client for this forward, so they cannot stack."""
    spec = f"{REMOTE_PORT}:{LOCAL_HOST}:{LOCAL_PORT}"
    try:
        if os.name == "nt":
            ps = (
                "Get-CimInstance Win32_Process -Filter \"Name='ssh.exe'\" | "
                f"Where-Object {{ $_.CommandLine -like '*{spec}*' }} | "
                "ForEach-Object { $_.ProcessId }"
            )
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, text=True, timeout=30,
            ).stdout
            pids = [int(p) for p in out.split() if p.strip().isdigit()]
            for pid in pids:
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, timeout=15)
                logger.info("Killed stale local tunnel process %d", pid)
        else:
            subprocess.run(["pkill", "-f", f"ssh.*{spec}"], capture_output=True, timeout=15)
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Could not check for stale local tunnels: %s", exc)


def _start_tunnel_process() -> subprocess.Popen:
    cmd = build_ssh_command()
    logger.info("Starting tunnel: VPS:%d -> %s:%d", REMOTE_PORT, LOCAL_HOST, LOCAL_PORT)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    return subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, creationflags=creationflags,
    )


def supervise():
    """Keep the tunnel up until interrupted."""
    kill_stale_local_tunnels()
    delay = RETRY_DELAY

    while True:
        if not is_local_port_open():
            logger.warning(
                "LM Studio is not listening on %s:%d — tunnel will connect but stay useless",
                LOCAL_HOST, LOCAL_PORT,
            )

        proc = _start_tunnel_process()
        started = time.monotonic()
        next_health = started + FIRST_HEALTH_DELAY

        try:
            while proc.poll() is None:
                time.sleep(2)
                now = time.monotonic()
                if now < next_health:
                    continue
                if health_check():
                    logger.info("Health check OK (uptime %.0fs)", now - started)
                    next_health = now + HEALTH_INTERVAL
                else:
                    logger.warning("Health check FAILED — restarting tunnel")
                    proc.terminate()
                    break
        except KeyboardInterrupt:
            proc.terminate()
            proc.wait(timeout=10)
            logger.info("Tunnel stopped by user.")
            return

        try:
            output, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            output = ""

        alive_for = time.monotonic() - started
        output = (output or "").strip()
        logger.warning(
            "Tunnel exited after %.0fs (code %s)%s",
            alive_for, proc.returncode, f": {output}" if output else "",
        )

        # The classic stale-session case: the port is still bound by a dead
        # sshd, so every retry would fail the same way until it is cleared.
        if "remote port forwarding failed" in output.lower():
            free_remote_port()

        if alive_for >= STABLE_AFTER:
            delay = RETRY_DELAY  # was healthy for a while; treat as a fresh start

        try:
            logger.info("Reconnecting in %ds...", delay)
            time.sleep(delay)
        except KeyboardInterrupt:
            logger.info("Tunnel stopped by user.")
            return
        delay = min(delay * 2, MAX_RETRY_DELAY)


def main():
    parser = argparse.ArgumentParser(description="SSH reverse tunnel to VPS")
    parser.add_argument("--check", action="store_true",
                        help="Run one end-to-end probe and exit")
    parser.add_argument("--once", action="store_true",
                        help="Start the tunnel once in the foreground, no supervision")
    parser.add_argument("--free-port", action="store_true",
                        help="Kill whatever holds the remote port, then exit")
    parser.add_argument("--log-file", type=Path, default=_default_log_file())
    args = parser.parse_args()

    _setup_logging(args.log_file)

    if args.check:
        ok = health_check()
        print(f"Tunnel is {'UP' if ok else 'DOWN'} (VPS:{REMOTE_PORT} -> local:{LOCAL_PORT})")
        sys.exit(0 if ok else 1)

    if args.free_port:
        status = free_remote_port()
        sys.exit(0 if status in ("FREED", "NO_LISTENER") else 1)

    if args.once:
        kill_stale_local_tunnels()
        proc = _start_tunnel_process()
        try:
            proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
        sys.exit(proc.returncode or 0)

    supervise()


if __name__ == "__main__":
    main()
