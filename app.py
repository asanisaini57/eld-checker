import pdfplumber
import re
import json
import hmac
import secrets
import tempfile
import os
from datetime import datetime, date, timedelta
from flask import (Flask, request, jsonify, render_template, session,
                   redirect, url_for)

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
CREDS_PATH  = os.environ.get("ELD_CREDS_PATH", os.path.join(BASE_DIR, "service_account.json"))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
ROSTER_PATH = os.path.join(BASE_DIR, "roster.json")

app = Flask(__name__)

# 25 MB is generous for a logbook PDF and stops an upload from exhausting a free
# host's memory.
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024
# A generated key logs everyone out on restart, which is safe but annoying — set
# ELD_SECRET_KEY in the host's environment to keep sessions across deploys.
app.secret_key = os.environ.get("ELD_SECRET_KEY") or secrets.token_hex(32)

LOCAL_ADDRESSES = {"127.0.0.1", "::1", "localhost"}
OPEN_ENDPOINTS  = {"login", "static"}


def _load_users():
    """ELD_USERS="name:password,name2:password2" — one entry per person."""
    users = {}
    for part in os.environ.get("ELD_USERS", "").split(","):
        name, _, password = part.partition(":")
        if name.strip() and password.strip():
            users[name.strip()] = password.strip()
    return users


USERS = _load_users()


@app.before_request
def require_login():
    """Gate everything behind a sign-in unless the request is local.

    A deployment that forgets ELD_USERS refuses remote requests outright rather
    than serving an open door onto the company sheet.
    """
    if request.endpoint in OPEN_ENDPOINTS:
        return None

    if not USERS:
        if request.remote_addr in LOCAL_ADDRESSES:
            return None
        return ("This deployment has no ELD_USERS set, so it will not serve remote "
                "requests. Set ELD_USERS in the host's environment.", 403)

    if session.get("user"):
        return None

    if request.path.startswith(("/analyze", "/export", "/prepare")):
        return jsonify({"error": "Session expired — reload the page and sign in."}), 401
    return redirect(url_for("login", next=request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    if not USERS:
        return redirect(url_for("index"))

    error = None
    if request.method == "POST":
        name = request.form.get("username", "").strip()
        expected = USERS.get(name, "")
        # Constant-time compare so responses can't be timed to guess the value
        if expected and hmac.compare_digest(request.form.get("password", ""), expected):
            session["user"] = name
            session.permanent = True
            return redirect(request.args.get("next") or url_for("index"))
        error = "Incorrect username or password."

    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def load_config():
    """Sheet target and thresholds. config.json, overridable by environment."""
    cfg = {
        "sheet_id": "",
        "worksheet": "",
        "apply_colors": False,
        "jump_threshold_miles": 10,
        "speed_limit_mph": 75,
        # strptime pattern for how dates should be written. Empty = copy whatever
        # format the sheet already uses.
        "date_format": "",
    }
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except (OSError, ValueError):
            pass
    cfg["sheet_id"]  = os.environ.get("ELD_SHEET_ID",  cfg["sheet_id"])
    cfg["worksheet"] = os.environ.get("ELD_WORKSHEET", cfg["worksheet"])
    return cfg


def load_roster():
    """Drivers in the order their rows should appear under each date."""
    if not os.path.exists(ROSTER_PATH):
        return []
    try:
        with open(ROSTER_PATH, encoding="utf-8") as f:
            return json.load(f).get("drivers", [])
    except (OSError, ValueError):
        return []


def match_roster_driver(name, roster):
    """Resolve a name from a PDF to its roster entry, or None."""
    target = _norm_driver(name)
    for entry in roster:
        if target == _norm_driver(entry.get("name")):
            return entry
        if any(target == _norm_driver(a) for a in entry.get("aliases", [])):
            return entry
    return None


# ── Duty statuses ────────────────────────────────────────────────────
# Truck is moving — odometer MUST increase
ACTIVE_STATUSES = {"DRIVING", "PERSONAL", "YARD", "YARD MOVES"}
# Truck is stopped — odometer must NOT change
STATIONARY_STATUSES = {"OFF DUTY", "ON DUTY", "SLEEPER BERTH"}
ALL_STATUSES = ACTIVE_STATUSES | STATIONARY_STATUSES

LOW_DIFF_MILES      = 3    # diff <= this → low priority finding
SHORT_DRIVE_MIN     = 5    # driving <= this (mins) with low/no change → low priority
SHORT_DRIVE_LOC_MIN = 15   # driving <= this (mins) → skip location-change check

# ── Sheet vocabulary — must match the dropdown options in the sheet ──
YES        = "Yes"
NO         = "No"
MISSING    = "Missing"
CORRECTED  = "Miss/Corr"
NA         = "N/A"
ST_OKAY     = "Okay"
ST_PENDING  = "Pending"
ST_RESOLVED = "Resolved"

DATE_RE = r"Record Date:\s*(\d{2}-\d{2}-\d{4})"
PDF_DATE_FMT = "%m-%d-%Y"

# Columns where MISSING is the bad value and a later fix means Miss/Corr
PRESENCE_FIELDS = ["dvir", "shipping_id", "sign", "start_location", "destination"]
# Columns where YES is the bad value (no corrected state)
FLAG_FIELDS = ["unassigned", "jump", "tamper", "hos_violations"]

# Fields the tool fills with a default it cannot actually verify. A "Yes" already
# in the sheet was put there by a person who checked, and must never be
# overwritten with the default.
DEFAULTED_FIELDS = ["hos_violations"]


# ══════════════════════════════════════════════════════════════════════
#  PDF parsing
# ══════════════════════════════════════════════════════════════════════

def normalize_status(raw):
    if not raw:
        return None
    s = raw.strip().replace('\n', ' ').upper()
    if 'SLEEPER' in s:
        return 'SLEEPER BERTH'
    if s.startswith('YARD'):
        return 'YARD'
    return s


def parse_odo(val):
    if val is None:
        return None
    cleaned = re.sub(r'[^\d]', '', str(val))
    return int(cleaned) if cleaned else None


def parse_duration_minutes(duration_str):
    """'02hrs 35mins' / '27mins' / '04hrs 01min' → total minutes (int or None)."""
    if not duration_str:
        return None
    s = duration_str.strip().replace('\n', ' ')
    hours = re.search(r'(\d+)\s*hr', s)
    mins  = re.search(r'(\d+)\s*min', s)
    total = 0
    if hours:
        total += int(hours.group(1)) * 60
    if mins:
        total += int(mins.group(1))
    return total if total > 0 else None


def extract_zip(location_str):
    """Extract a 5-digit ZIP, but only where it looks like a trailing ZIP."""
    if not location_str:
        return None
    match = re.search(r'\b(\d{5})\b\s*$', location_str.strip())
    if match:
        return match.group(1)
    # ZIP following a two-letter state ("Indio, CA 92201, USA")
    match = re.search(r'\b[A-Z]{2}[,\s]+(\d{5})\b', location_str)
    return match.group(1) if match else None


def locations_differ(loc_a, loc_b):
    """True if two location strings represent different places."""
    if not loc_a or not loc_b:
        return False  # can't compare — don't flag
    zip_a, zip_b = extract_zip(loc_a), extract_zip(loc_b)
    if zip_a and zip_b:
        return zip_a != zip_b
    norm = lambda s: re.sub(r'[\s;,]+', ' ', s).strip().upper()
    return norm(loc_a) != norm(loc_b)


def _clean_name(raw):
    """Trim a captured name at the next 'Label:' and keep only name-like tokens."""
    if not raw:
        return None
    raw = re.split(r"\s+[A-Za-z][A-Za-z /'-]*:", raw)[0]
    toks = []
    for tok in raw.split():
        if not re.fullmatch(r"[A-Za-z][A-Za-z'.\-]*", tok):
            break
        toks.append(tok)
        if len(toks) == 4:
            break
    name = " ".join(toks).strip(" .-'")
    return name or None


def _extract_driver_name(flat):
    """TruckX renders this either as 'Driver Name: X' or split as 'Driver X Name: Y'."""
    m = re.search(r"Driver Name:\s*(.{0,80})", flat)
    if m:
        name = _clean_name(m.group(1))
        if name:
            return name
    m = re.search(r"Driver\s+([A-Za-z][A-Za-z'\-]{1,30})\s+Name:\s*(.{0,60})", flat)
    if m:
        first = _clean_name(m.group(1))
        last  = _clean_name(m.group(2))
        if first and last:
            return f"{first} {last}"
        return first or last
    return None


def _present(val):
    """A field counts as present unless it's blank or a TruckX placeholder."""
    return bool(val) and str(val).strip() not in ("", "--", "-", "N/A", "None")


def _day_metadata(text):
    """Header-block fields for one day, from that day's combined page text."""
    flat = re.sub(r"\s+", " ", text)

    def grab(pattern, default=""):
        m = re.search(pattern, flat)
        return m.group(1).strip() if m else default

    start_loc = ""
    start_m = re.search(r"Start\s+([A-Za-z][A-Za-z .\-]+,\s*[A-Z]{2})[^L]*?Location:\s*(\d{5})?", flat)
    if start_m:
        start_loc = start_m.group(1).strip()
        if start_m.group(2):
            start_loc += " " + start_m.group(2)

    miles = grab(r"Miles Today:\s*([\d.]+)")
    try:
        miles_today = float(miles) if miles else None
    except ValueError:
        miles_today = None

    return {
        "truck_unit":     grab(r"Truck Tractor ID:\s*(\S+)"),
        "trailer_id":     grab(r"Trailer ID:\s*(\S+)"),
        # Shipping doc numbers are alphanumeric ("T263861"), not digits only
        "shipping_docs":  grab(r"Shipping Docs:\s*(\S+)"),
        "unidentified":   grab(r"Unidentified Driver\s+(No|Yes)\b"),
        "eld_malfunction": grab(r"ELD Malfunction\s+(No|Yes)\b"),
        "carrier":        grab(r"Carrier:\s*(.{0,40}?)(?=\s+Start\s*/|\s+Miles|$)"),
        "start_location": start_loc,
        "destination":    grab(r"Destination:\s*(.{0,60}?)(?=\s+Engine|\s+Status|\s+Driver|$)"),
        "miles_today":    miles_today,
        "dvir_done":      bool(re.search(r"Pre\s*-?\s*Trip Inspection\s*\{", text, re.IGNORECASE)),
        "signed":         bool(re.search(r"I certified that", text, re.IGNORECASE))
                          and bool(re.search(r"are true and", text, re.IGNORECASE)),
    }


def _events_from_table(table, page_date):
    """Duty-status rows for one page. Returns (events, skipped_row_count)."""
    events, skipped = [], 0
    if not table:
        return events, skipped

    for row in table:
        if not row or not row[0]:
            continue
        status = normalize_status(row[0])
        if not status or status not in ALL_STATUSES:
            continue  # header rows, DVIR blocks, etc.

        odo = parse_odo(row[5]) if len(row) > 5 else None
        if odo is None:
            # A duty row we recognised but couldn't read the odometer from.
            # Counted, not silently dropped — it breaks the comparison chain.
            skipped += 1
            continue

        duration_str = row[2].strip().replace('\n', ' ') if len(row) > 2 and row[2] else ""
        events.append({
            'date':     page_date,
            'status':   status,
            'time':     (row[1] or "").strip(),
            'odo':      odo,
            'duration': duration_str,
            'duration_minutes': parse_duration_minutes(duration_str),
            'location': (row[3] or "").strip().replace('\n', ', ') if len(row) > 3 else "",
            'notes':    (row[6] or "").strip().replace('\n', ' ') if len(row) > 6 else "",
        })

    return events, skipped


def parse_pdf(pdf_path):
    """Open the PDF once and group everything by Record Date.

    Returns {'driver_name': str, 'days': [ {date, meta, events, skipped_rows}, ... ]}
    sorted oldest first. Pages without a Record Date belong to the day above them.
    """
    days = []          # ordered list of day dicts
    by_date = {}       # date string -> day dict
    current = None
    driver_name = None

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            date_match = re.search(DATE_RE, text)

            if date_match:
                date_str = date_match.group(1)
                # A day already seen (continuation after another day) keeps one entry
                current = by_date.get(date_str)
                if current is None:
                    current = {'date': date_str, 'text': "", 'events': [], 'skipped_rows': 0}
                    by_date[date_str] = current
                    days.append(current)

            if current is None:
                continue  # leading page with no Record Date — nothing to attach it to

            current['text'] += "\n" + text

            if driver_name is None:
                driver_name = _extract_driver_name(re.sub(r"\s+", " ", text))

            events, skipped = _events_from_table(page.extract_table(), current['date'])
            current['events'].extend(events)
            current['skipped_rows'] += skipped

    for day in days:
        day['meta'] = _day_metadata(day['text'])
        day['events'].sort(key=lambda e: _event_sort_key(e))
        del day['text']

    days.sort(key=lambda d: datetime.strptime(d['date'], PDF_DATE_FMT))

    return {'driver_name': driver_name or 'Unknown', 'days': days}


def _event_sort_key(event):
    """Sort within a day by start time. Unparseable times sort last, not first."""
    try:
        return (0, datetime.strptime(event['time'], "%H:%M:%S").time())
    except (ValueError, TypeError):
        try:
            return (0, datetime.strptime(event['time'], "%H:%M").time())
        except (ValueError, TypeError):
            return (1, datetime.min.time())


# ══════════════════════════════════════════════════════════════════════
#  Findings — compared only within a single day
# ══════════════════════════════════════════════════════════════════════

def signature_due(day_date_str, today):
    """Whether the driver's certification can be expected for this date yet.

    Drivers certify at the end of the day, so an unsigned log for today isn't a
    finding — it only becomes Missing once the day is behind us.
    """
    try:
        day_date = datetime.strptime(day_date_str, PDF_DATE_FMT).date()
    except (ValueError, TypeError):
        return True
    return day_date < today


def analyze_day(day, cfg, today):
    """Odometer / speed / location findings for one day's events.

    Events are only ever compared to the next event *on the same day*, so a
    missing day in the logbook can no longer manufacture a violation.
    """
    findings = []
    events = day['events']
    speed_limit = cfg["speed_limit_mph"]

    for i in range(len(events) - 1):
        curr, next_ev = events[i], events[i + 1]
        status   = curr['status']
        curr_odo, next_odo = curr['odo'], next_ev['odo']
        diff     = next_odo - curr_odo

        base = {
            'date':        curr['date'],
            'time':        curr['time'],
            'status':      status,
            'next_status': next_ev['status'],
            'next_time':   next_ev['time'],
            'from_odo':    curr_odo,
            'to_odo':      next_odo,
            'diff':        diff,
            'duration':    curr['duration'],
        }

        # ── Odometer ──────────────────────────────────────────────────
        if status in ACTIVE_STATUSES and diff <= 0:
            findings.append({**base,
                'type': _active_priority(curr_odo, next_odo, diff, curr),
                'rule': 'active_no_increase',
                'message': f"Odometer did not increase after {status} (changed by {diff:+,})"})

        elif status in STATIONARY_STATUSES and diff != 0:
            findings.append({**base,
                'type': _stationary_priority(curr_odo, next_odo, diff, cfg["jump_threshold_miles"]),
                'rule': 'stationary_changed',
                'message': f"Odometer changed after {status} (changed by {diff:+,})"})

        # ── Speed ─────────────────────────────────────────────────────
        if status == "DRIVING" and diff > 0:
            dur_min = curr['duration_minutes']
            if dur_min:
                implied_mph = (diff / dur_min) * 60
                if implied_mph > speed_limit:
                    findings.append({**base,
                        'type': 'SPEED', 'rule': 'speed_alert',
                        'implied_mph': round(implied_mph, 1),
                        'message': f"Implied speed {round(implied_mph, 1)} mph over "
                                   f"{curr['duration']} ({diff:,} miles)"})

        # ── Location ──────────────────────────────────────────────────
        curr_loc, next_loc = curr['location'], next_ev['location']

        if status == "DRIVING":
            dur_min = curr['duration_minutes']
            too_short = dur_min is not None and dur_min <= SHORT_DRIVE_LOC_MIN
            if not too_short and curr_loc and next_loc and not locations_differ(curr_loc, next_loc):
                findings.append({**base,
                    'type': 'LOCATION', 'rule': 'driving_no_location_change',
                    'from_loc': curr_loc, 'to_loc': next_loc,
                    'message': f"No location change after DRIVING for {curr['duration']}"})

        elif status in STATIONARY_STATUSES and locations_differ(curr_loc, next_loc):
            findings.append({**base,
                'type': 'LOCATION', 'rule': 'stationary_location_changed',
                'from_loc': curr_loc, 'to_loc': next_loc,
                'message': f"Location changed during {status}: {curr_loc} → {next_loc}"})

    # ── Per-day paperwork ─────────────────────────────────────────────
    blank = {'time': '—', 'status': '—', 'next_status': '—', 'next_time': '—',
             'from_odo': 0, 'to_odo': 0, 'diff': 0, 'duration': '—'}

    if day.get('no_log'):
        # No page for this date at all — the driver never logged in, so none of
        # the paperwork checks apply.
        findings.append({**blank, 'type': 'LOW', 'rule': 'no_log_for_date',
                         'date': day['date'],
                         'message': f"No logbook page for {day['date']} — recorded as not working"})
        return findings

    if not day['meta']['signed'] and signature_due(day['date'], today):
        findings.append({**blank, 'type': 'SIGNATURE', 'rule': 'signature_missing',
                         'date': day['date'],
                         'message': f"Driver signature/certification missing for {day['date']}"})

    if day['skipped_rows']:
        findings.append({**blank, 'type': 'LOW', 'rule': 'unreadable_rows',
                         'date': day['date'],
                         'message': f"{day['skipped_rows']} duty row(s) had no readable odometer "
                                    f"and were left out of the comparison"})

    return findings


def _active_priority(curr_odo, next_odo, diff, curr):
    """Downgrade to LOW if odometer is 0, diff is tiny, or drive was very short."""
    if curr_odo == 0 or next_odo == 0:
        return 'LOW'
    if abs(diff) <= LOW_DIFF_MILES:
        return 'LOW'
    dur = curr.get('duration_minutes')
    if dur is not None and dur <= SHORT_DRIVE_MIN:
        return 'LOW'
    return 'ERROR'


def _stationary_priority(curr_odo, next_odo, diff, jump_limit):
    """LOW for yard/GPS drift, ERROR once movement passes the jump threshold.

    Uses the same limit as the sheet's Jump column so the findings list and the
    sheet can never disagree about whether a day had a jump.
    """
    if curr_odo == 0 or next_odo == 0:
        return 'LOW'
    if abs(diff) <= jump_limit:
        return 'LOW'
    return 'ERROR'


# ══════════════════════════════════════════════════════════════════════
#  Sheet row per day
# ══════════════════════════════════════════════════════════════════════

def build_day_row(driver_name, day, findings, cfg, today):
    """One sheet row's worth of values for a single day, in sheet vocabulary."""
    meta   = day['meta']
    events = day['events']

    worked = any(e['status'] == 'DRIVING' for e in events) or bool(meta['miles_today'])

    # A "jump" is stationary odometer movement past the noise limit, or an active
    # event whose odometer failed to advance. Both surface as ERROR findings.
    jump = any(
        f['type'] == 'ERROR' and f['rule'] in ('stationary_changed', 'active_no_increase')
        for f in findings
    )

    tamper = any(
        re.search(r"-\s*\d+\s*(sec|min|hr)", e.get('duration', ''), re.IGNORECASE)
        for e in events
    )

    row = {
        'date':           day['date'],
        'driver_name':    driver_name,
        'unit':           meta['truck_unit'],
        'working':        YES if worked else NO,
        # The logbook PDF carries no HOS data at all, so this is a default that a
        # person can override in the sheet — never a checked result.
        'hos_violations': NO,
        'unassigned':     YES if meta['unidentified'] == 'Yes' else NO,
        'dvir':           YES if meta['dvir_done'] else MISSING,
        'shipping_id':    YES if _present(meta['shipping_docs']) else MISSING,
        'pc':             YES if any(e['status'] == 'PERSONAL' for e in events) else NO,
        # Certification is done at end of day — not yet due for today, so an
        # unsigned log for today is N/A rather than Missing.
        'sign':           YES if meta['signed']
                          else (MISSING if signature_due(day['date'], today) else NA),
        'jump':           YES if jump else NO,
        'start_location': YES if _present(meta['start_location']) else MISSING,
        'destination':    YES if _present(meta['destination']) else MISSING,
        # Missing, not No — matches the other presence columns so an absent
        # trailer shows red like any other missing item.
        'trailer_id':     YES if _present(meta['trailer_id']) else MISSING,
        'tamper':         YES if tamper else NO,
    }

    if not worked:
        # Driver didn't work — the checks don't apply, matching how the sheet
        # is filled by hand.
        for key in PRESENCE_FIELDS + FLAG_FIELDS + ['pc', 'trailer_id']:
            row[key] = NA

    row['status'] = ST_OKAY if _row_is_clean(row) else ST_PENDING
    row['_miles_today'] = meta['miles_today']
    row['_no_log'] = bool(day.get('no_log'))
    return row


def _row_is_clean(row):
    """No open issue in any automated column."""
    if any(row.get(f) == MISSING for f in PRESENCE_FIELDS):
        return False
    if any(row.get(f) == YES for f in FLAG_FIELDS):
        return False
    return True


def _blank_day(date_str, truck_unit):
    """A day the logbook has no page for — the driver never logged in.

    The unit is carried over from a day that does have a log so the row still
    identifies the truck, matching how these rows are filled by hand.
    """
    return {
        'date': date_str,
        'events': [],
        'skipped_rows': 0,
        'no_log': True,
        'meta': {
            'truck_unit': truck_unit, 'trailer_id': '', 'shipping_docs': '',
            'unidentified': 'No', 'eld_malfunction': 'No', 'carrier': '',
            'start_location': '', 'destination': '', 'miles_today': None,
            'dvir_done': False, 'signed': False,
        },
    }


def fill_missing_days(days, today):
    """Add a not-working row for every date between the first log and today.

    A driver who never logged into the app produces a logbook with no page for
    that date, which is how we know they weren't working.
    """
    if not days:
        return days

    known = {d['date'] for d in days}
    first = datetime.strptime(days[0]['date'], PDF_DATE_FMT).date()
    if today < first:
        return days

    # Most recent unit seen, used to label the blank days
    unit = next((d['meta']['truck_unit'] for d in reversed(days)
                 if d['meta'].get('truck_unit')), '')

    filled = list(days)
    cursor = first
    while cursor <= today:
        stamp = cursor.strftime(PDF_DATE_FMT)
        if stamp not in known:
            filled.append(_blank_day(stamp, unit))
        cursor += timedelta(days=1)

    filled.sort(key=lambda d: datetime.strptime(d['date'], PDF_DATE_FMT))
    return filled


def analyze_pdf(pdf_path, filename, cfg, today=None):
    """Full result for one PDF: findings plus one prepared sheet row per day."""
    today  = today or date.today()
    parsed = parse_pdf(pdf_path)
    driver = parsed['driver_name']
    parsed['days'] = fill_missing_days(parsed['days'], today)

    all_findings, rows = [], []
    for day in parsed['days']:
        day_findings = analyze_day(day, cfg, today)
        all_findings.extend(day_findings)
        rows.append(build_day_row(driver, day, day_findings, cfg, today))

    all_findings.sort(key=lambda f: (f.get('date') or '', f.get('time') or ''))

    return {
        "filename":       filename,
        "driver_name":    driver,
        "date_from":      parsed['days'][0]['date']  if parsed['days'] else None,
        "date_to":        parsed['days'][-1]['date'] if parsed['days'] else None,
        "total_events":   sum(len(d['events']) for d in parsed['days']),
        "total_findings": len(all_findings),
        "findings":       all_findings,
        "sheet_rows":     rows,
    }


# ══════════════════════════════════════════════════════════════════════
#  Google Sheet — column mapping driven by the sheet's own header row
# ══════════════════════════════════════════════════════════════════════

# Header text (normalised) → field key. Exact matches only; anything the sheet
# has that isn't listed here is left completely untouched.
HEADER_ALIASES = {
    'date':                          'date',
    'driver name': 'driver_name', 'd/n': 'driver_name', 'driver': 'driver_name',
    'unit': 'unit', 'u': 'unit', 'truck': 'unit',
    'working':                       'working',
    'hos violations': 'hos_violations', 'hos': 'hos_violations',
    'unassigned':                    'unassigned',
    'dvir':                          'dvir',
    'shipping id': 'shipping_id', 'shipping': 'shipping_id',
    'personal conveync': 'pc', 'personal conveyance': 'pc', 'pc': 'pc',
    'sign': 'sign', 'signature': 'sign',
    'jump':                          'jump',
    'start location': 'start_location',
    'destination':    'destination',
    'trailer id':     'trailer_id',
    'tamper with time': 'tamper', 'tamper': 'tamper',
    'status':                        'status',
}

# Never written — yours to fill in
MANUAL_HEADERS = {'action taken', 'remark', 'trailer hard copy'}


def _norm_header(text):
    return re.sub(r'\s+', ' ', (text or '')).strip().lower()


def map_columns(header_row):
    """{field_key: 0-based column index} plus the headers we chose not to touch."""
    mapping, skipped = {}, []
    for idx, raw in enumerate(header_row):
        norm = _norm_header(raw)
        if not norm:
            continue
        field = HEADER_ALIASES.get(norm)
        if field and field not in mapping:
            mapping[field] = idx
        elif norm not in MANUAL_HEADERS:
            skipped.append(raw)
    return mapping, skipped


# ── Date handling ────────────────────────────────────────────────────
# (name, strptime format, formatter) — order matters, first match wins
SHEET_DATE_FORMATS = [
    ("%m-%d-%Y", lambda d: d.strftime("%m-%d-%Y")),
    ("%d %b %Y", lambda d: f"{d.day} {d.strftime('%b')} {d.year}"),
    ("%d %B %Y", lambda d: f"{d.day} {d.strftime('%B')} {d.year}"),
    ("%b %d, %Y", lambda d: f"{d.strftime('%b')} {d.day}, {d.year}"),
    ("%m/%d/%Y", lambda d: d.strftime("%m/%d/%Y")),
    ("%Y-%m-%d", lambda d: d.strftime("%Y-%m-%d")),
]


def parse_any_date(value):
    if not value or not str(value).strip():
        return None
    text = re.sub(r'\s+', ' ', str(value)).strip()
    for fmt, _ in SHEET_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def detect_date_format(existing_dates, preferred=""):
    """How to write dates: the configured format, else whatever the sheet uses.

    A configured format matters when the target sheet is a staging sheet whose
    rows get pasted into another sheet — the dates have to match the destination,
    not the empty staging sheet.
    """
    if preferred:
        for fmt, formatter in SHEET_DATE_FORMATS:
            if fmt == preferred:
                return formatter

    samples = [d for d in existing_dates if d and str(d).strip()][:25]
    if not samples:
        return SHEET_DATE_FORMATS[0][1]
    for fmt, formatter in SHEET_DATE_FORMATS:
        if all(_parses_as(s, fmt) for s in samples):
            return formatter
    return SHEET_DATE_FORMATS[0][1]


def _parses_as(value, fmt):
    try:
        datetime.strptime(re.sub(r'\s+', ' ', str(value)).strip(), fmt)
        return True
    except ValueError:
        return False


def _norm_driver(name):
    return re.sub(r'\s+', ' ', (name or '')).strip().lower()


# ── Miss/Corr reconciliation ─────────────────────────────────────────

def reconcile_row(new_row, existing_cells, mapping):
    """Merge a computed row with what the sheet already says for that day.

    An item that the sheet recorded as Missing and the PDF now shows as present
    becomes Miss/Corr, and the day's Status becomes Resolved.
    """
    merged = dict(new_row)
    corrections = []

    def existing(field):
        idx = mapping.get(field)
        if idx is None or idx >= len(existing_cells):
            return ""
        return _norm_header(existing_cells[idx])

    for field in PRESENCE_FIELDS:
        if field not in mapping:
            continue
        was, now = existing(field), merged.get(field)
        if was == _norm_header(CORRECTED):
            merged[field] = CORRECTED          # already corrected — keep it
        elif was == _norm_header(MISSING) and now == YES:
            merged[field] = CORRECTED
            corrections.append(field)

    # A default the tool can't verify must not overwrite a person's finding.
    for field in DEFAULTED_FIELDS:
        if field not in mapping:
            continue
        was = existing(field)
        if was and was not in (_norm_header(NO), _norm_header(NA)):
            merged[field] = existing_cells[mapping[field]].strip()

    if 'status' in mapping:
        was_status = existing('status')
        open_issues = not _row_is_clean(merged)
        if open_issues:
            merged['status'] = ST_PENDING
        elif corrections or was_status == _norm_header(ST_RESOLVED):
            merged['status'] = ST_RESOLVED
        else:
            merged['status'] = ST_OKAY

    merged['_corrections'] = corrections
    return merged


def _google_credentials():
    """Service-account credentials from the environment, else the local file.

    Hosted deployments must use GOOGLE_CREDENTIALS_JSON — the key file is
    gitignored and must never be committed or bundled into a deploy.
    """
    from google.oauth2.service_account import Credentials

    # Only the Sheets scope is needed — the sheet is opened by key, not searched.
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]

    raw = os.environ.get("GOOGLE_CREDENTIALS_JSON", "").strip()
    if raw:
        try:
            return Credentials.from_service_account_info(json.loads(raw), scopes=scopes)
        except (ValueError, KeyError) as e:
            raise RuntimeError(f"GOOGLE_CREDENTIALS_JSON is not valid service-account JSON: {e}")

    if os.path.exists(CREDS_PATH):
        return Credentials.from_service_account_file(CREDS_PATH, scopes=scopes)

    raise RuntimeError(
        "No Google credentials. Set GOOGLE_CREDENTIALS_JSON to the contents of the "
        f"service-account JSON, or place the file at {CREDS_PATH}."
    )


def _get_sheet(cfg):
    import gspread

    if not cfg["sheet_id"]:
        raise RuntimeError(
            "No spreadsheet configured. Put your sheet ID in config.json "
            '("sheet_id": "...") or set the ELD_SHEET_ID environment variable.'
        )

    sh = gspread.authorize(_google_credentials()).open_by_key(cfg["sheet_id"])
    return sh.worksheet(cfg["worksheet"]) if cfg["worksheet"] else sh.sheet1


def _col_letters(col_idx):
    """0-based column index → column letters, correct past Z (25 → Z, 26 → AA)."""
    letters, n = "", col_idx + 1
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(ord('A') + rem) + letters
    return letters


def _a1(col_idx, row_num):
    """0-based column index → A1 reference, correct past column Z."""
    return f"{_col_letters(col_idx)}{row_num}"


def _index_rows(all_values, date_col, driver_col):
    """{(date, normalised driver): (row_number, cells)} for every filled row."""
    index = {}
    for row_num, row in enumerate(all_values[1:], start=2):
        d = parse_any_date(row[date_col]) if len(row) > date_col else None
        who = _norm_driver(row[driver_col]) if len(row) > driver_col else ""
        if d and who:
            index[(d, who)] = (row_num, row)
    return index


def ensure_day_blocks(ws, mapping, dates, fmt_date, roster):
    """Make sure every date has a row per roster driver, in roster order.

    Only ever appends rows that don't exist. Existing rows are never read for
    this purpose and never modified, so preparing tomorrow's block cannot
    disturb anything already filled in.
    """
    if not roster or not dates:
        return []

    all_values = ws.get_all_values()
    date_col, driver_col = mapping['date'], mapping['driver_name']
    present = set(_index_rows(all_values, date_col, driver_col).keys())

    next_row = len(all_values) + 1
    updates, created = [], []

    for day in sorted(dates):
        for entry in roster:
            key = (day, _norm_driver(entry.get("name")))
            if key in present:
                continue
            present.add(key)
            skeleton = {
                'date':        fmt_date(day),
                'driver_name': entry.get("name", ""),
                'unit':        entry.get("unit", ""),
            }
            for field, value in skeleton.items():
                if field in mapping:
                    updates.append({'range': _a1(mapping[field], next_row),
                                    'values': [[value]]})
            created.append(f"{fmt_date(day)} — {entry.get('name', '')}")
            next_row += 1

    if updates:
        ws.batch_update(updates, value_input_option="USER_ENTERED")
    return created


def push_rows(ws, prepared, cfg, today=None):
    """Upsert one row per (date, driver). Only mapped columns are written."""
    today = today or date.today()
    roster = load_roster()

    all_values  = ws.get_all_values()
    if not all_values:
        raise RuntimeError("The sheet is empty — it needs a header row first.")

    header_row  = all_values[0]
    mapping, unmapped = map_columns(header_row)

    for required in ('date', 'driver_name'):
        if required not in mapping:
            raise RuntimeError(
                f"Could not find a '{required}' column in the sheet header row. "
                f"Headers seen: {', '.join(h for h in header_row if h.strip())}"
            )

    date_col, driver_col = mapping['date'], mapping['driver_name']
    fmt_date = detect_date_format(
        [r[date_col] if len(r) > date_col else "" for r in all_values[1:]],
        cfg.get("date_format", ""),
    )

    # Every date this upload touches, plus today and tomorrow so the next day's
    # list is always standing ready.
    wanted_dates = {parse_any_date(r['date']) for r in prepared}
    wanted_dates.discard(None)
    wanted_dates |= {today, today + timedelta(days=1)}
    blocks_created = ensure_day_blocks(ws, mapping, wanted_dates, fmt_date, roster)

    # Re-read so the rows just created are found and filled in place
    all_values = ws.get_all_values()
    index = _index_rows(all_values, date_col, driver_col)

    next_row = len(all_values) + 1
    updates, added, updated, corrections, unknown = [], 0, 0, [], []

    for row in prepared:
        day_date = parse_any_date(row['date'])

        # Resolve the PDF's spelling to the roster entry so an alias lands on the
        # driver's existing row instead of appending a near-duplicate.
        entry = match_roster_driver(row['driver_name'], roster)
        if entry:
            row['driver_name'] = entry['name']
        elif roster:
            unknown.append(row['driver_name'])

        key = (day_date, _norm_driver(row['driver_name']))

        if key in index:
            row_num, existing_cells = index[key]
            merged = reconcile_row(row, existing_cells, mapping)
            updated += 1
        else:
            row_num, existing_cells = next_row, []
            next_row += 1
            merged = reconcile_row(row, existing_cells, mapping)
            added += 1

        for field, col_idx in mapping.items():
            value = merged.get(field)
            if value is None:
                continue
            if field == 'date':
                value = fmt_date(day_date) if day_date else row['date']
            updates.append({'range': _a1(col_idx, row_num), 'values': [[value]]})

        for field in merged.get('_corrections', []):
            corrections.append(f"{row['driver_name']} {row['date']}: {field} → {CORRECTED}")

    if updates:
        ws.batch_update(updates, value_input_option="USER_ENTERED")

    return {
        "rows_added":     added,
        "rows_updated":   updated,
        "corrections":    corrections,
        "blocks_created": blocks_created,
        "unknown_drivers": sorted(set(unknown)),
        "columns_filled": sorted(mapping.keys()),
        "columns_left_alone": unmapped,
    }


# ══════════════════════════════════════════════════════════════════════
#  Routes
# ══════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════
#  Colour rules
# ══════════════════════════════════════════════════════════════════════
# Applied as Google Sheets *conditional formatting*, not as fixed cell colours,
# so every row added later colours itself with no extra step.

def _rgb(hex_code):
    h = hex_code.lstrip('#')
    return {'red': int(h[0:2], 16) / 255,
            'green': int(h[2:4], 16) / 255,
            'blue': int(h[4:6], 16) / 255}


GOOD    = (_rgb('#d9ead3'), _rgb('#274e13'), False)  # light green, dark green text
BAD     = (_rgb('#cc0000'), _rgb('#ffffff'), True)   # solid red, white bold
CORRECT = (_rgb('#b45f06'), _rgb('#ffffff'), True)   # brown, white bold — Miss/Corr
WATCH   = (_rgb('#fce5cd'), _rgb('#7f6000'), False)  # amber
NEUTRAL = (_rgb('#efefef'), _rgb('#888888'), False)  # grey — N/A

# Date / driver / unit bands. Cycled by the date's serial number so consecutive
# dates never share a colour and a shade only returns after four days. Kept to
# structural tints that can't be mistaken for a good/bad status colour.
BAND_COLOURS = [
    (_rgb('#e6e0f8'), _rgb('#20124d'), False),  # lavender
    (_rgb('#d0e2f3'), _rgb('#0b3c5d'), False),  # light blue
    (_rgb('#d5e8e4'), _rgb('#0c443c'), False),  # light teal
    (_rgb('#f6d9e0'), _rgb('#741b47'), False),  # light rose
]

# Value → style, per kind of column
PRESENCE_STYLES = [(YES, GOOD), (MISSING, BAD), (CORRECTED, CORRECT), (NA, NEUTRAL)]
FLAG_STYLES     = [(NO, GOOD), (YES, BAD), (NA, NEUTRAL)]
WORKING_STYLES  = [(YES, GOOD), (NO, WATCH), (NA, NEUTRAL)]
STATUS_STYLES   = [(ST_OKAY, GOOD), (ST_RESOLVED, CORRECT), (ST_PENDING, BAD), (NA, NEUTRAL)]

# Columns coloured green-when-Yes
PRESENCE_COLOURED = PRESENCE_FIELDS + ['trailer_id']
# Columns coloured red-when-Yes. Wider than FLAG_FIELDS: personal conveyance is
# flagged red for review but doesn't on its own make the day Pending.
FLAG_COLOURED = FLAG_FIELDS + ['pc']


def _fmt(style):
    bg, fg, bold = style
    return {'backgroundColor': bg,
            'textFormat': {'foregroundColor': fg, 'bold': bold}}


def _ranges(sheet_id, fields, mapping, last_row):
    out = []
    for field in fields:
        if field in mapping:
            out.append({'sheetId': sheet_id, 'startRowIndex': 1, 'endRowIndex': last_row,
                        'startColumnIndex': mapping[field], 'endColumnIndex': mapping[field] + 1})
    return out


def apply_colour_rules(ws, mapping):
    """Replace this tab's conditional formatting with the ELD colour scheme."""
    sheet_id = ws.id
    last_row = ws.row_count

    # Clear whatever rules are already on the tab so this is re-runnable
    meta = ws.spreadsheet.fetch_sheet_metadata()
    current = next((s for s in meta.get('sheets', [])
                    if s['properties']['sheetId'] == sheet_id), {})
    requests = [{'deleteConditionalFormatRule': {'sheetId': sheet_id, 'index': 0}}
                for _ in range(len(current.get('conditionalFormats', [])))]

    def add(ranges, condition, style):
        if not ranges:
            return
        requests.append({'addConditionalFormatRule': {
            'index': len(requests),
            'rule': {'ranges': ranges,
                     'booleanRule': {'condition': condition, 'format': _fmt(style)}},
        }})

    def text_is(value):
        return {'type': 'TEXT_EQ', 'values': [{'userEnteredValue': value}]}

    def formula(expr):
        return {'type': 'CUSTOM_FORMULA', 'values': [{'userEnteredValue': expr}]}

    # Date / driver / unit banded per date, so one date block reads as one group.
    # Consecutive dates alternate because their serial numbers alternate parity.
    id_cols = _ranges(sheet_id, ['date', 'driver_name', 'unit'], mapping, last_row)
    date_ref = f"${_col_letters(mapping['date'])}2"
    for slot, style in enumerate(BAND_COLOURS):
        add(id_cols,
            formula(f'=AND({date_ref}<>"",MOD({date_ref},{len(BAND_COLOURS)})={slot})'),
            style)

    for value, style in PRESENCE_STYLES:
        add(_ranges(sheet_id, PRESENCE_COLOURED, mapping, last_row), text_is(value), style)
    for value, style in FLAG_STYLES:
        add(_ranges(sheet_id, FLAG_COLOURED, mapping, last_row), text_is(value), style)
    for value, style in WORKING_STYLES:
        add(_ranges(sheet_id, ['working'], mapping, last_row), text_is(value), style)
    for value, style in STATUS_STYLES:
        add(_ranges(sheet_id, ['status'], mapping, last_row), text_is(value), style)

    ws.spreadsheet.batch_update({'requests': requests})
    return sum(1 for r in requests if 'addConditionalFormatRule' in r)


# ══════════════════════════════════════════════════════════════════════
#  Dropdowns
# ══════════════════════════════════════════════════════════════════════
# Keyed on header text rather than the write-mapping, so manual columns like
# Action Taken get a dropdown even though the tool never writes to them.

# No "No" in the presence lists — an item is either there, missing, corrected, or
# not applicable, so "No" was only ever a way to say Missing badly. "No" stays in
# the yes/no lists, where it is the good answer and what the tool writes.
PRESENCE_OPTIONS = [YES, MISSING, CORRECTED, NA]
YES_NO_OPTIONS   = [YES, NO, NA]
ACTION_OPTIONS   = ["No Need", YES, NA]
STATUS_OPTIONS   = [ST_OKAY, ST_PENDING, ST_RESOLVED, NA]

DROPDOWN_GROUPS = [
    (['dvir', 'shipping id', 'shipping', 'sign', 'signature',
      'start location', 'destination', 'trailer id'],            PRESENCE_OPTIONS),
    (['working', 'pc', 'personal conveync', 'personal conveyance',
      'hos violations', 'hos', 'unassigned', 'jump',
      'tamper with time', 'tamper'],                             YES_NO_OPTIONS),
    (['action taken'],                                           ACTION_OPTIONS),
    (['status'],                                                 STATUS_OPTIONS),
]


def apply_dropdowns(ws, header_row):
    """Put a value dropdown on every check column, rows 2 to the sheet's end.

    Rejection is deliberately *not* strict: an out-of-list value gets flagged
    with a warning marker rather than refused, so a write can never be blocked
    by a list that has fallen behind the code.
    """
    sheet_id, last_row = ws.id, ws.row_count
    by_header = {}
    for idx, raw in enumerate(header_row):
        norm = _norm_header(raw)
        if norm and norm not in by_header:
            by_header[norm] = idx

    requests = []
    for headers, options in DROPDOWN_GROUPS:
        for name in headers:
            idx = by_header.get(name)
            if idx is None:
                continue
            requests.append({'setDataValidation': {
                'range': {'sheetId': sheet_id, 'startRowIndex': 1, 'endRowIndex': last_row,
                          'startColumnIndex': idx, 'endColumnIndex': idx + 1},
                'rule': {
                    'condition': {'type': 'ONE_OF_LIST',
                                  'values': [{'userEnteredValue': o} for o in options]},
                    'showCustomUi': True,
                    'strict': False,
                },
            }})

    if requests:
        ws.spreadsheet.batch_update({'requests': requests})
    return len(requests)


@app.route("/")
def index():
    return render_template("index.html")


def _each_uploaded_pdf(files):
    """Yield (filename, temp_path) for each uploaded PDF, cleaning up after."""
    for file in files:
        if not file.filename or not file.filename.lower().endswith(".pdf"):
            yield file.filename, None
            continue
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                tmp_path = tmp.name
                file.save(tmp)
            yield file.filename, tmp_path
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass


@app.route("/analyze", methods=["POST"])
def analyze():
    files = request.files.getlist("pdf")
    if not files or all(not f.filename for f in files):
        return jsonify({"error": "No file uploaded"}), 400

    cfg = load_config()
    reports = []
    for filename, path in _each_uploaded_pdf(files):
        if path is None:
            reports.append({"filename": filename, "error": "File must be a PDF"})
            continue
        try:
            reports.append(analyze_pdf(path, filename, cfg))
        except Exception as e:
            reports.append({"filename": filename, "error": str(e)})

    return jsonify({"reports": reports, "sheet_configured": bool(cfg["sheet_id"])})


@app.route("/prepare", methods=["POST"])
def prepare():
    """Create the roster block for today and tomorrow without any upload.

    Purely additive — appends the rows that don't exist yet and touches nothing
    already on the sheet.
    """
    cfg = load_config()
    roster = load_roster()
    if not roster:
        return jsonify({"error": "roster.json has no drivers listed"}), 400

    try:
        ws = _get_sheet(cfg)
        all_values = ws.get_all_values()
        if not all_values:
            return jsonify({"error": "The sheet needs a header row first"}), 400

        mapping, _ = map_columns(all_values[0])
        if 'date' not in mapping or 'driver_name' not in mapping:
            return jsonify({"error": "Sheet needs Date and driver-name columns"}), 400

        date_col = mapping['date']
        fmt_date = detect_date_format(
            [r[date_col] if len(r) > date_col else "" for r in all_values[1:]],
            cfg.get("date_format", ""),
        )

        today = date.today()
        created = ensure_day_blocks(
            ws, mapping, {today, today + timedelta(days=1)}, fmt_date, roster
        )
    except Exception as e:
        return jsonify({"error": f"Google Sheets error: {e}"}), 500

    return jsonify({
        "ok": True,
        "rows_created": len(created),
        "blocks_created": created,
        "sheet_url": f"https://docs.google.com/spreadsheets/d/{cfg['sheet_id']}/edit",
    })


@app.route("/export", methods=["POST"])
def export():
    files = request.files.getlist("pdf")
    if not files or all(not f.filename for f in files):
        return jsonify({"error": "No file uploaded"}), 400

    cfg = load_config()
    prepared, skipped = [], []
    for filename, path in _each_uploaded_pdf(files):
        if path is None:
            skipped.append(filename)
            continue
        try:
            result = analyze_pdf(path, filename, cfg)
            if result['driver_name'] == 'Unknown':
                skipped.append(f"{filename} (no driver name found)")
                continue
            prepared.extend(result['sheet_rows'])
        except Exception as e:
            skipped.append(f"{filename} ({e})")

    if not prepared:
        return jsonify({"error": "Could not extract any row data. " + "; ".join(skipped)}), 400

    try:
        ws = _get_sheet(cfg)
        result = push_rows(ws, prepared, cfg)
    except Exception as e:
        return jsonify({"error": f"Google Sheets error: {e}"}), 500

    result["skipped"]   = skipped
    result["ok"]        = True
    result["sheet_url"] = f"https://docs.google.com/spreadsheets/d/{cfg['sheet_id']}/edit"
    return jsonify(result)


if __name__ == "__main__":
    print("ELD Checker running at http://127.0.0.1:5000")
    # debug=False: the reloader runs the server in a child process, which the
    # launcher's Stop button cannot terminate, and the debugger is remote-code
    # execution for anything that can reach the port.
    app.run(debug=False)
