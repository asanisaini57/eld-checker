"""Diagnose why the ELD Checker won't start on this PC.

    python check_setup.py       (or:  py -3 check_setup.py)

Prints the interpreter in use, whether each dependency is importable by *that*
interpreter, whether the settings files are present, whether port 5000 is free,
and the tail of server.log. Send the whole output when asking for help.
"""

import importlib
import importlib.metadata
import os
import socket
import sys
import urllib.request

DIST_NAMES = {"flask": "flask", "pdfplumber": "pdfplumber", "gspread": "gspread",
              "google.oauth2.service_account": "google-auth"}

BASE = os.path.dirname(os.path.abspath(__file__))
DEPS = ["flask", "pdfplumber", "gspread", "google.oauth2.service_account"]
FILES = ["app.py", "launcher.py", "config.json", "roster.json",
         "service_account.json", "templates/index.html", "templates/login.html"]


def line(title):
    print("\n" + title)
    print("-" * len(title))


def main():
    problems = []

    line("INTERPRETER")
    print("  executable :", sys.executable)
    print("  version    :", sys.version.split()[0])
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    print("  pythonw.exe:", "found" if os.path.exists(pythonw) else "NOT FOUND (launcher falls back)")
    if sys.version_info < (3, 9):
        problems.append(f"Python {sys.version.split()[0]} is too old — install 3.12 or newer.")

    line("DEPENDENCIES (as seen by the interpreter above)")
    for name in DEPS:
        try:
            importlib.import_module(name)
            try:
                version = importlib.metadata.version(DIST_NAMES.get(name, name))
            except importlib.metadata.PackageNotFoundError:
                version = ""
            print(f"  OK      {name} {version}")
        except Exception as e:
            print(f"  FAILED  {name}  ->  {type(e).__name__}: {e}")
            problems.append(
                f"{name} is not installed for this interpreter. Run:\n"
                f'      "{sys.executable}" -m pip install -r requirements.txt'
            )

    line("FILES")
    for rel in FILES:
        path = os.path.join(BASE, rel)
        if os.path.exists(path):
            print(f"  OK      {rel}")
        else:
            print(f"  MISSING {rel}")
            problems.append(f"{rel} is missing from {BASE}")

    line("PORT 5000")
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 5000))
        print("  free — the server can start")
    except OSError:
        # Occupied. If it answers, the app is already up and this is not a fault.
        try:
            urllib.request.urlopen("http://127.0.0.1:5000/", timeout=3)
            print("  in use by the ELD Checker itself — already running, open "
                  "http://127.0.0.1:5000")
        except Exception:
            print("  IN USE by something that isn't the app")
            problems.append("Port 5000 is taken by another program. Close it (look for "
                            "python.exe in Task Manager) and try again.")
    finally:
        sock.close()

    line("CAN THE APP BE IMPORTED?")
    sys.path.insert(0, BASE)
    try:
        import app  # noqa: F401
        print("  OK — app.py imports cleanly")
        try:
            print("  roster :", len(app.load_roster()), "drivers")
            print("  sheet  :", "configured" if app.load_config()["sheet_id"] else "NOT SET")
        except Exception as e:
            print("  settings could not be read:", e)
    except Exception as e:
        print(f"  FAILED  ->  {type(e).__name__}: {e}")
        problems.append(f"app.py itself fails to load: {type(e).__name__}: {e}")

    log = os.path.join(BASE, "server.log")
    line("SERVER.LOG (last 20 lines)")
    if os.path.exists(log):
        with open(log, encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-20:]
        print("".join("  " + l for l in tail) if tail else "  (empty)")
    else:
        print("  (no server.log yet — press Start in the launcher first)")

    line("VERDICT")
    if problems:
        print(f"  {len(problems)} problem(s) found:\n")
        for i, p in enumerate(problems, 1):
            print(f"  {i}. {p}")
    else:
        print("  No problems found. The app should start.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
