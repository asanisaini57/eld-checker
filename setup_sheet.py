"""Apply the sheet's presentation layer: colour rules, dropdowns, date format.

Safe to re-run at any time — it replaces formatting rules and never touches cell
values. Run it after changing the header row, adding columns, or if the colours
or dropdowns ever go missing.

    python setup_sheet.py
"""

import sys

import app


def main():
    cfg = app.load_config()
    if not cfg["sheet_id"]:
        print("No sheet configured. Set sheet_id in config.json first.")
        return 1

    ws = app._get_sheet(cfg)
    header_row = ws.row_values(1)
    if not any(h.strip() for h in header_row):
        print("The sheet has no header row — nothing to set up.")
        return 1

    mapping, unmapped = app.map_columns(header_row)
    print(f"Sheet : {ws.spreadsheet.title} / {ws.title}")
    print(f"Header: {len(header_row)} columns, {len(mapping)} recognised")
    if unmapped:
        print(f"        not recognised (never written): {', '.join(unmapped)}")

    rules = app.apply_colour_rules(ws, mapping)
    print(f"Colour rules installed : {rules}")

    drops = app.apply_dropdowns(ws, header_row)
    print(f"Dropdown columns set   : {drops}")

    if 'date' in mapping:
        cell = app._a1(mapping['date'], 2)
        end = app._a1(mapping['date'], ws.row_count)
        ws.format(f"{cell}:{end}",
                  {'numberFormat': {'type': 'DATE', 'pattern': 'd mmm yyyy'}})
        print(f"Date column format     : {cell}:{end} -> d mmm yyyy")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
