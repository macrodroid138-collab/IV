"""Every 5 minutes: same thing as the MacroDroid HTTP request (Run workflow)."""
import ssl
import time
import urllib.request

# Put your fine-grained GitHub token in token.txt (one line).
TOKEN = open("token.txt", encoding="utf-8").read().strip()

URL = "https://api.github.com/repos/macrodroid138-collab/IV/actions/workflows/update-bars.yml/dispatches"
BODY = b'{"ref":"main","inputs":{"loops":"1"}}'

# Avoid Windows "CERTIFICATE_VERIFY_FAILED" with some Python installs / AV intercept.
CTX = ssl._create_unverified_context()

while True:
    req = urllib.request.Request(
        URL,
        data=BODY,
        method="POST",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "IV-ping",
        },
    )
    try:
        with urllib.request.urlopen(req, context=CTX) as r:
            print(time.strftime("%H:%M:%S"), "ok", r.status)
    except Exception as e:
        print(time.strftime("%H:%M:%S"), "error", e)
    time.sleep(300)
