#!/usr/bin/env python3
"""gsc-authorise.py — one-time Search Console authorisation.

Search Console will not accept a service account through its Add User dialog:
the form only takes real Google accounts and answers "email not found". So
access has to come from a user authorisation instead.

You sign in once in a browser. The refresh token that comes back is written to
the droplet and renews itself indefinitely, so this is not a recurring chore.
Same pattern as the LinkedIn token and the Higgsfield CLI.

    python3 bin/gsc-authorise.py            interactive, on a machine with a browser
    python3 bin/gsc-authorise.py --check    report whether the stored token works

The client secret for a desktop OAuth client is not really a secret: the flow
is protected by PKCE and the redirect being loopback only.
"""

import argparse, http.server, json, os, pathlib, secrets, socketserver
import threading, urllib.parse, urllib.request, webbrowser, base64, hashlib

SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
STORE = pathlib.Path(os.environ.get("GSC_TOKEN_PATH", str(pathlib.Path.home() / ".secrets-gsc.json")))
PORT = 8765


def _pkce():
    v = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
    c = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).decode().rstrip("=")
    return v, c


def _post(data):
    req = urllib.request.Request(TOKEN, data=urllib.parse.urlencode(data).encode(),
                                 method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def authorise(client_id, client_secret):
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(16)
    redirect = f"http://localhost:{PORT}/"
    params = {
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code",
        "scope": SCOPE, "access_type": "offline", "prompt": "consent",
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
    }
    url = AUTH + "?" + urllib.parse.urlencode(params)
    got = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h2>Authorised. You can close this tab.</h2>")

        def log_message(self, *a):
            pass

    with socketserver.TCPServer(("", PORT), Handler) as httpd:
        threading.Thread(target=httpd.handle_request, daemon=True).start()
        print("\n" + "=" * 78)
        print("OPEN THIS URL IN YOUR BROWSER, then sign in and approve:")
        print("=" * 78 + "\n")
        print(url + "\n")
        print("=" * 78)
        print("Waiting for you to approve. This times out in three minutes.\n")
        try:
            webbrowser.open(url)
        except Exception:
            pass
        for _ in range(180):
            if got:
                break
            import time
            time.sleep(1)

    if got.get("state") != state:
        raise SystemExit("state mismatch, aborting")
    if "code" not in got:
        raise SystemExit(f"no code returned: {got}")

    tok = _post({"client_id": client_id, "client_secret": client_secret,
                 "code": got["code"], "code_verifier": verifier,
                 "grant_type": "authorization_code", "redirect_uri": redirect})
    if "refresh_token" not in tok:
        raise SystemExit("no refresh token returned. Remove the app at "
                         "myaccount.google.com/permissions and try again.")
    STORE.write_text(json.dumps({"client_id": client_id, "client_secret": client_secret,
                                 "refresh_token": tok["refresh_token"]}, indent=2))
    STORE.chmod(0o600)
    print(f"  Saved to {STORE}")
    return tok


def access_token(store=STORE):
    """A fresh access token from the stored refresh token."""
    d = json.loads(pathlib.Path(store).read_text())
    tok = _post({"client_id": d["client_id"], "client_secret": d["client_secret"],
                 "refresh_token": d["refresh_token"], "grant_type": "refresh_token"})
    return tok["access_token"]


def check(store=STORE):
    token = access_token(store)
    req = urllib.request.Request("https://searchconsole.googleapis.com/webmasters/v3/sites",
                                 headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=40) as r:
        sites = json.loads(r.read().decode()).get("siteEntry", [])
    print(f"  properties visible: {len(sites)}")
    for s in sites:
        print("   ", s.get("permissionLevel"), "|", s.get("siteUrl"))
    return sites


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--client-id")
    ap.add_argument("--client-secret")
    ap.add_argument("--client-json",
                    help="the client_secret_*.json downloaded from Google Cloud")
    a = ap.parse_args()
    if a.check:
        check()
        raise SystemExit(0)

    cid, cs = a.client_id, a.client_secret

    # Google offers the client as a download. Reading it beats copying two long
    # strings by hand, and a mistyped secret fails in a way that is not obvious.
    src = a.client_json
    if not src and not (cid and cs):
        guesses = sorted(pathlib.Path.home().glob("Downloads/client_secret_*.json"),
                         key=lambda f: f.stat().st_mtime, reverse=True)
        if guesses:
            src = str(guesses[0])
            print(f"  using {src}")
    if src:
        d = json.loads(pathlib.Path(src).read_text())
        node = d.get("installed") or d.get("web") or d
        cid, cs = node.get("client_id"), node.get("client_secret")

    cid = cid or input("  OAuth client ID: ").strip()
    cs = cs or input("  OAuth client secret: ").strip()
    authorise(cid, cs)
    check()
