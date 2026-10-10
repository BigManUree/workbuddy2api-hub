#!/usr/bin/env python
"""Run a command on the WB2api ECS host over SSH (password auth).

Credentials come from environment variables (or a .env file next to the
failover proxy) — nothing is hardcoded:

    WB2API_HOST           e.g. 203.0.113.10
    WB2API_SSH_USER       default root
    WB2API_SSH_PASSWORD   your root password

Usage:
    python wb2api-ssh.py "remote command"
    echo 'script' | python wb2api-ssh.py -        # run stdin via bash -s

Requires: pip install paramiko
"""
import os
import sys
import time

import paramiko

def _load_dotenv():
    """Read ../failover/.env if present; real environment variables win."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "failover", ".env")
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


_load_dotenv()

HOST = os.environ.get("WB2API_HOST", "")
USER = os.environ.get("WB2API_SSH_USER", "root")
PASSWORD = os.environ.get("WB2API_SSH_PASSWORD", "")


def main():
    if not HOST or not PASSWORD:
        sys.exit("set WB2API_HOST and WB2API_SSH_PASSWORD first")
    if len(sys.argv) < 2:
        sys.exit("missing command")
    if sys.argv[1] == "-":
        cmd = "bash -s"
        stdin_data = sys.stdin.read()
    else:
        cmd = sys.argv[1]
        stdin_data = None

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    last_err = None
    for attempt in range(6):
        try:
            client.connect(HOST, username=USER, password=PASSWORD, timeout=20,
                           look_for_keys=False, allow_agent=False,
                           banner_timeout=30, auth_timeout=30)
            break
        except Exception as exc:  # transient SYN drops to port 22
            last_err = exc
            time.sleep(5)
    else:
        raise last_err
    client.get_transport().set_keepalive(15)
    stdin, stdout, stderr = client.exec_command(
        cmd, get_pty=False, timeout=600,
    )
    if stdin_data is not None:
        stdin.write(stdin_data)
        stdin.flush()
        stdin.channel.shutdown_write()
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    rc = stdout.channel.recv_exit_status()
    if out:
        sys.stdout.write(out)
    if err:
        sys.stderr.write(err)
    client.close()
    sys.exit(rc)


if __name__ == "__main__":
    main()
