"""
Run this once to connect a Google account for Calendar + Gmail:

    python google_auth_setup.py

Needs GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET in .env first — see the
comment block above them in .env.example for how to get those from
Google Cloud Console. Opens your browser for Google's consent screen,
then stores the token encrypted (see actions/google_auth.py's module
docstring for exactly how). Re-run any time to re-auth or after adding
a new scope.

After saving the token, this also makes one real, read-only call to
Calendar and Gmail each — not just "a token was saved", but "the token
actually works" — same principle as verifier/core.py: never trust a
success report you haven't independently checked.
"""
import sys

print("\n=== FRIDAY — Google Account Setup (Calendar + Gmail) ===\n")

from config import config

if not (config.integrations.google_client_id and config.integrations.google_client_secret):
    print("  GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not set in .env.")
    print("  See the comment block above them in .env.example for how to get those")
    print("  from Google Cloud Console (takes about 2 minutes), then re-run this.\n")
    sys.exit(1)

print("  Opening your browser for Google's consent screen...")
print("  (grant access to Calendar and Gmail when prompted)\n")

from actions import google_auth

try:
    status = google_auth.run_interactive_setup()
except Exception as e:
    print(f"  Setup failed: {e}\n")
    sys.exit(1)

if not status.get("connected"):
    print(f"  Consent finished but something's off: {status.get('reason')}\n")
    sys.exit(1)

print("  Token saved and encrypted.\n")
print("=== Verifying the token actually works ===\n")

creds = google_auth.get_credentials()

try:
    from googleapiclient.discovery import build

    cal = build("calendar", "v3", credentials=creds)
    cal_list = cal.calendarList().list(maxResults=1).execute()
    n_calendars = len(cal_list.get("items", []))
    print(f"  Calendar API: OK — can see your calendar list ({n_calendars} shown, may be capped at 1).")
except Exception as e:
    print(f"  Calendar API check FAILED: {e}")
    print("  (Is the Google Calendar API enabled for your project in Cloud Console?)")

try:
    from googleapiclient.discovery import build

    gmail = build("gmail", "v1", credentials=creds)
    profile = gmail.users().getProfile(userId="me").execute()
    print(f"  Gmail API: OK — connected as {profile.get('emailAddress', '?')}.")
except Exception as e:
    print(f"  Gmail API check FAILED: {e}")
    print("  (Is the Gmail API enabled for your project in Cloud Console?)")

print("\nGranted scopes:")
for s in status.get("scopes", []):
    print(f"  - {s}")

print("\nDone. Ask FRIDAY \"are you connected to Google?\" any time to check this")
print("from inside the app instead of re-running this script.\n")
