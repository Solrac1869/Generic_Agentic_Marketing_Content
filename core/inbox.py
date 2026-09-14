#!/usr/bin/env python3
"""inbox.py, reading replies the operator sends to the Telegram bot.

The audit product's webhook owns the Telegram connection, so this system
cannot call getUpdates: it answers 409 while a webhook is registered. That was
true for months while publish.py believed it was polling, and stopped_ids()
swallowed the error and reported that nothing had been vetoed.

So the webhook appends one line per authorised message to a file, and this
reads it over an SSH key restricted by a forced command to that file. Nothing
here can write to the revenue host.

Line format is "<epoch> <text>", so a reply of any length survives as one line.
"""

import os
import subprocess

KEY = "/root/.ssh/id_status_reader"
HOST = os.environ.get("INBOX_HOST", "")  # optional second host


def read_lines(key=KEY, host=HOST, timeout=60):
    """Every recorded reply. Returns (rows, error) where rows is [(ts, text)].

    A failure returns an empty list and the reason, never a partial read that
    could look like an empty inbox.
    """
    try:
        out = subprocess.run(
            ["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
             "-i", key, host],
            capture_output=True, text=True, timeout=timeout)
        if out.returncode != 0:
            return [], f"ssh failed: {out.stderr.strip()[:90]}"
        rows = []
        for line in out.stdout.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and parts[0].isdigit():
                rows.append((int(parts[0]), parts[1]))
        return rows, None
    except Exception as e:
        return [], f"{type(e).__name__}: {str(e)[:90]}"
