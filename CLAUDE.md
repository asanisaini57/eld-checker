# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A Flask web app that reads ELD (Electronic Logging Device) logbook PDFs exported from
TruckX, checks each day for odometer tampering and missing paperwork, and upserts one
row per driver per day into a Google Sheet used for daily compliance review.

The daily routine it supports: download each driver's logbook PDF from TruckX, drop them
all on the upload zone, review the findings and the row preview, then push to the sheet.

## Running

```bash
python app.py            # server at http://127.0.0.1:5000
python launcher.py       # small Tkinter start/stop window (writes server.log)
```

Dependencies: `pip install -r requirements.txt` (or run `setup.bat`, which also creates a
desktop shortcut).

## Configuration

`config.json` — not secret, safe to edit:

| Key | Meaning |
|---|---|
| `sheet_id` | Target spreadsheet ID. **Empty by default** — `/export` refuses to run until set. |
| `worksheet` | Tab name. Empty = first tab. |
| `jump_threshold_miles` | Stationary odometer movement above this is a Jump (default 10). |
| `speed_limit_mph` | Implied-speed alert threshold (default 75). |
| `apply_colors` | Reserved. Leave `false` — the sheet does its own conditional formatting. |

`ELD_SHEET_ID`, `ELD_WORKSHEET` and `ELD_CREDS_PATH` override these via environment.

`service_account.json` holds the Google private key. It is gitignored and must never be
committed. Only the `spreadsheets` scope is requested.

## Architecture

`app.py` is the whole application. Flow:

1. **`parse_pdf`** — opens the PDF **once** and groups pages by `Record Date`. A page
   without a Record Date belongs to the day above it (TruckX continues tables across
   pages). Returns `{'driver_name', 'days': [{date, meta, events, skipped_rows}]}`,
   oldest day first.
2. **`_day_metadata`** — header-block fields from each day's combined text: truck unit,
   trailer, `Shipping Docs` (alphanumeric, e.g. `T263861`), `Unidentified Driver
   Records`, start location, destination, `Miles Today`, DVIR block, signature block.
3. **`analyze_day`** — findings for one day. **Events are only ever compared with the
   next event on the same day.** Comparing across a day boundary used to invent
   violations whenever a day was missing from the PDF.
4. **`build_day_row`** — one sheet row per day, in the sheet's own vocabulary
   (`Yes` / `No` / `Missing` / `Miss/Corr` / `N/A`, `Okay` / `Pending` / `Resolved`).
   A day with no driving and no miles becomes `Working = No` with `N/A` across the checks.
5. **`push_rows`** — upsert keyed on (date, driver), dates compared as parsed dates so
   format differences don't create duplicates.

### Table column order

`row[0]` status, `[1]` start time, `[2]` duration, `[3]` location, `[4]` engine hours,
`[5]` odometer, `[6]` notes. A duty row whose odometer can't be read is **counted in
`skipped_rows` and reported**, never silently dropped — dropping it closes the gap
between its neighbours and forges a violation.

### Sheet writing rules

- Columns are resolved from the sheet's **own header row** via `HEADER_ALIASES`.
  Nothing is hardcoded to a column letter, and the header row is **never** rewritten.
- Only mapped columns are written, cell by cell. `HOS Violations`, `Action Taken`,
  `Remark`, `Trailer hard Copy`, and any unrecognised column are left untouched.
- Date output format comes from `config.json`'s `date_format` (a strptime pattern), or is
  detected from what's already in the sheet when that's blank. Because writes use
  `USER_ENTERED`, Sheets stores a real date and **displays it using the column's number
  format** — so the Date column also needs a matching number format
  (`d mmm yyyy` on the staging sheet), or it renders in whatever pattern was there before.
- **Miss/Corr**: an item the sheet records as `Missing` that the PDF now shows present is
  written as `Miss/Corr` and the day's Status becomes `Resolved`. An existing `Miss/Corr`
  is never downgraded. A day with any still-open item stays `Pending`.
- **Signature timing** (`signature_due`): drivers certify at end of day, so an unsigned log
  for **today** is `N/A`, not `Missing`, and raises no finding. The full lifecycle of one
  date is `N/A` (that day) → `Missing` (once past and still unsigned) → `Miss/Corr` (signed
  later), with Status following `Okay` → `Pending` → `Resolved`.
- Values must match the sheet's dropdown options exactly, or data validation rejects them.

## Staging-sheet workflow

The configured sheet ("auto eld log sheet") is a **staging** sheet, not the master. Rows
are filled here, then copied into the master by hand. Its 18 columns run
`Date, D/N, U, Working, HOS Violations, Unassigned, DVIR, Shipping ID, PC, Sign, Jump,
Start Location, Destination, Trailer ID, Tamper With time, Action Taken, Status, Remark`.

Column positions are never hardcoded — `map_columns` resolves them from the sheet's own
header row, so the layout can be rearranged in Sheets without touching the code.

**Do not clear the staging sheet between runs.** Miss/Corr is detected by comparing a
freshly parsed day against what the sheet already records for that (date, driver). If the
rows are wiped, every day looks new and no correction is ever found.

### Roster and day blocks

`roster.json` lists the drivers in the order their rows appear under each date, with each
driver's unit and any alternate spellings the PDF might use (`aliases`). A PDF's driver
name is resolved through `match_roster_driver` before the upsert, so an alias updates the
driver's existing row instead of appending a near-duplicate. A name matching nothing is
still written, but reported as `unknown_drivers` so the spelling can be fixed.

`ensure_day_blocks` appends a Date/driver/unit row for every roster driver missing from a
given date, leaving the check columns blank. It runs on `/export` for the dates being
written plus today and tomorrow, and on `POST /prepare` ("Prepare next day" in the UI) for
just today and tomorrow. It is **purely additive** — it never reads or modifies a filled
row, so uploading one driver's PDF cannot disturb any other driver or any manual column.

A day with no logbook page is written as `Working = No` with `N/A` across the checks —
`fill_missing_days` synthesises those rows for every date from the earliest log up to
today, because a driver who never logged into the app produces no page for that date.

### Colours

`apply_colour_rules` installs Google Sheets **conditional formatting**, not fixed cell
colours — so rows added later colour themselves with no extra step. It is re-runnable: it
deletes the tab's existing rules first. Nineteen rules:

- `Date`/`D/N`/`U` are banded per date by `=MOD($A2, 4) = n`, cycling `BAND_COLOURS`
  against the date's serial number. Consecutive dates therefore always differ and a shade
  only returns after four days — no per-date setup, and it can never drift out of step.
- Presence columns (DVIR, Shipping ID, Sign, Start Location, Destination, Trailer ID):
  `Yes` green, `Missing` solid red, `Miss/Corr` brown, `N/A` grey.
- Flag columns (HOS Violations, Unassigned, Jump, Tamper) plus `PC` invert it — `No` green,
  `Yes` red — because Yes is the bad answer there. Note `FLAG_COLOURED` is deliberately
  wider than `FLAG_FIELDS`: personal conveyance colours red for review but does **not** on
  its own push the day's Status to Pending.
- `Working`: Yes green, No amber.
- `Status`: Okay green, Resolved brown, Pending red.

### Dropdowns

`apply_dropdowns` puts a data-validation list on all 14 check columns, keyed on **header
text** rather than the write-mapping so manual columns (Action Taken) get one too.

The presence lists carry no `No` — an item is present, `Missing`, `Miss/Corr` or `N/A`, so
`No` was only a worse way of saying `Missing`. `trailer_id` was the one field writing `No`
for an absent value and now writes `MISSING` like its neighbours. `No` remains in the
yes/no lists, where it is the good answer and what the tool writes.
`strict` is deliberately `False`: an off-list value is flagged with a warning marker rather
than refused, so a write can never be blocked by an options list that has fallen behind the
code. Date, D/N and U stay free text.

### Re-running the presentation layer

`python setup_sheet.py` reapplies colour rules, dropdowns and the date number format. It
only touches formatting, never cell values, and is safe to run any time — after editing the
header row, adding a column, or if formatting goes missing.

## Gotchas

- `app.run(debug=False)` is deliberate. With `debug=True` the reloader runs the server in
  a child process that `launcher.py`'s Stop button cannot kill, leaving a zombie holding
  port 5000 — and the debugger is remote code execution for anything reaching the port.
- Driver name arrives in two layouts: `Driver Name: X` or split as `Driver X Name: Y`.
  `_clean_name` bounds the capture; an unbounded `.+?` swallowed the rest of the page and
  corrupted the upsert key.
- PDF content (driver names, locations, filenames) is rendered through `esc()` in
  `templates/index.html`. Keep it that way — those strings come from outside.
- `eld_check.py` is the original standalone prototype, superseded by `app.py`. Not used.

## What isn't automated

`Action Taken`, `Remark` and `Trailer hard Copy` are never written.

`HOS Violations` is a **default, not a check** — the logbook PDF carries no hours-of-service
data at all (only the ruleset name, "USA 70 hours / 8 days"). It is written as `No` on a
working day and `N/A` otherwise. Anything already in that cell other than `No`/`N/A` is
left as-is, via `DEFAULTED_FIELDS` in `reconcile_row`, so a violation somebody recorded by
hand can never be reset to `No` by a later run. If real HOS checking is ever wanted, it
needs a source other than this PDF.
