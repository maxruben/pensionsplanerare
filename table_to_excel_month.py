import pyperclip
import re


def parse_amount(s):
    """Parse amount string like '56 900' to integer, returns None for '-' or empty."""
    s = s.strip()
    if not s or s == '-':
        return None
    return int(s.replace(' ', '').replace('\xa0', ''))


def parse_date(date_str):
    """Parse '2026, mars' to (2026, 3)."""
    months = {
        'januari': 1, 'februari': 2, 'mars': 3, 'april': 4,
        'maj': 5, 'juni': 6, 'juli': 7, 'augusti': 8,
        'september': 9, 'oktober': 10, 'november': 11, 'december': 12
    }
    match = re.match(r'(\d{4}),\s*(\w+)', date_str.strip())
    if match:
        year = int(match.group(1))
        month = months.get(match.group(2).lower())
        return (year, month) if month else None
    return None


def main():
    text = pyperclip.paste()

    if not text:
        print("Clipboard is empty!")
        return

    lines = [l.replace('\r', '').strip() for l in text.strip().split('\n')]

    # Skip header note line ("Alla belopp visas som kr/mån")
    lines = lines[1:]

    # Parse age/date pairs until we hit a "Summa" line
    dates = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.lower().startswith('summa'):
            break
        # Check if this is an age line (contains "år")
        if 'år' in line.lower():
            # Next line should be a date
            if i + 1 < len(lines):
                date = parse_date(lines[i + 1])
                if date:
                    dates.append(date)
            i += 2
        else:
            i += 1

    num_columns = len(dates)

    if num_columns == 0:
        print("No date columns found")
        return

    # Parse data rows: label followed by num_columns values
    before_tax = []
    after_tax = []

    while i < len(lines):
        line = lines[i]
        lower = line.lower()
        if 'summa' in lower and 'skatt' in lower:
            values = []
            for k in range(1, num_columns + 1):
                if i + k < len(lines):
                    values.append(parse_amount(lines[i + k]))
                else:
                    values.append(None)

            if 'efter' in lower:
                after_tax = values
            else:
                before_tax = values

            i += num_columns + 1
        else:
            i += 1

    if not before_tax and not after_tax:
        print("Could not find tax data rows")
        return

    # Build breakpoints: (year, month) -> (before, after)
    breakpoints = []
    for idx, date in enumerate(dates):
        b = before_tax[idx] if idx < len(before_tax) else None
        a = after_tax[idx] if idx < len(after_tax) else None
        breakpoints.append((date, b, a))

    print(f"Found {len(breakpoints)} breakpoints:")
    for (y, m), b, a in breakpoints:
        print(f"  {y}-{m:02d}: före={b}, efter={a}")

    # Build monthly data from Jan 2027 to Dec 2053
    result_lines = ["Före skatt\tEfter skatt"]

    for year in range(2027, 2054):
        for month in range(1, 13):
            before_val = None
            after_val = None
            for (by, bm), b, a in breakpoints:
                if (by, bm) <= (year, month):
                    if b is not None:
                        before_val = b
                    if a is not None:
                        after_val = a
                else:
                    break

            before_str = str(before_val) if before_val is not None else ''
            after_str = str(after_val) if after_val is not None else ''
            result_lines.append(f"{before_str}\t{after_str}")

    tsv_output = '\n'.join(result_lines)
    pyperclip.copy(tsv_output)

    print(f"\nGenerated {12 * 27} monthly rows (2027-01 to 2053-12)")
    print("Result copied to clipboard - ready to paste into Excel!")


if __name__ == "__main__":
    main()
