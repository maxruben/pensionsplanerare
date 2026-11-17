import pyperclip
import re

def extract_year(date_str):
    """Extract year from date string like '2026, januari'"""
    match = re.match(r'(\d{4})', date_str)
    return int(match.group(1)) if match else None

def extract_age(age_str):
    """Extract age from string like '63 år, 8 mån'"""
    match = re.match(r'(\d+)\s*år', age_str)
    return int(match.group(1)) if match else None

def extract_months(age_str):
    """Extract months from string like '63 år, 8 mån'"""
    match = re.search(r'(\d+)\s*mån', age_str)
    return int(match.group(1)) if match else 0

def extract_month(date_str):
    """Extract month number from date string like '2026, januari'"""
    months = {
        'januari': 1, 'februari': 2, 'mars': 3, 'april': 4,
        'maj': 5, 'juni': 6, 'juli': 7, 'augusti': 8,
        'september': 9, 'oktober': 10, 'november': 11, 'december': 12
    }
    for name, num in months.items():
        if name in date_str.lower():
            return num
    return 1

def fill_missing_years(headers, data_rows):
    """Fill in missing years with columns using previous column's values"""
    if not headers:
        return headers, data_rows

    filled_headers = []
    filled_data = [[] for _ in data_rows]

    for i, (age, date) in enumerate(headers):
        # Add this column
        filled_headers.append((age, date))
        for j, row in enumerate(data_rows):
            filled_data[j].append(row[i + 1] if i + 1 < len(row) else '')

        # Check if there's a gap to the next column
        if i + 1 < len(headers):
            current_year = extract_year(date)
            next_year = extract_year(headers[i + 1][1])
            current_age = extract_age(age)

            if current_year and next_year and current_age:
                # Fill in missing years
                for missing_year in range(current_year + 1, next_year):
                    missing_age = current_age + (missing_year - current_year)
                    new_age = f"{missing_age} år, 8 mån"
                    new_date = f"{missing_year}, januari"
                    filled_headers.append((new_age, new_date))

                    # Copy values from previous column
                    for j in range(len(data_rows)):
                        filled_data[j].append(filled_data[j][-1])

    # Rebuild data rows with labels
    new_data_rows = []
    for j, row in enumerate(data_rows):
        new_data_rows.append([row[0]] + filled_data[j])

    return filled_headers, new_data_rows

def parse_table(text):
    lines = text.strip().split('\n')

    # Skip the first line (header note like "Alla belopp visas som kr/mån")
    lines = lines[1:]

    # Parse column headers (age + date pairs)
    headers = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()

        # Empty line or data row starts
        if not line or line.startswith('Summa'):
            break

        # Check if this is an age line (contains "år")
        if 'år' in line:
            age = line
            date = lines[i + 1].strip() if i + 1 < len(lines) else ""
            headers.append((age, date))
            i += 2
        else:
            i += 1

    num_columns = len(headers)

    # Find data rows - label followed by values on separate lines
    data_rows = []
    j = i
    while j < len(lines):
        line = lines[j].strip()

        if line.startswith('Summa'):
            label = line
            values = []
            # Collect the next num_columns lines as values
            for k in range(1, num_columns + 1):
                if j + k < len(lines):
                    values.append(lines[j + k].strip())
            data_rows.append([label] + values)
            j += num_columns + 1
        else:
            j += 1

    return headers, data_rows

def to_tsv(headers, data_rows):
    result_lines = []

    # Header row 1: ages (with empty first cell for row labels)
    result_lines.append('\t' + '\t'.join(h[0] for h in headers))

    # Header row 2: dates
    result_lines.append('\t' + '\t'.join(h[1] for h in headers))

    # Header row 3: age they will become that year
    numerical_ages = []
    for h in headers:
        age = extract_age(h[0])
        if age:
            months_since_birthday = extract_months(h[0])
            current_month = extract_month(h[1])
            # If birthday already happened this year, they turn current age
            # If birthday is later this year, they turn current age + 1
            if current_month - months_since_birthday >= 1:
                # Birthday was already this calendar year
                numerical_ages.append(str(age))
            else:
                # Birthday is later this calendar year
                numerical_ages.append(str(age + 1))
        else:
            numerical_ages.append('')
    result_lines.append('\t' + '\t'.join(numerical_ages))

    # Data rows
    for row in data_rows:
        result_lines.append('\t'.join(row))

    return '\n'.join(result_lines)

def main():
    # Read from clipboard
    text = pyperclip.paste()

    if not text:
        print("Clipboard is empty!")
        return

    # Debug: show first 10 lines of clipboard content
    print("--- Clipboard content (first 10 lines) ---")
    for i, line in enumerate(text.strip().split('\n')[:10]):
        print(f"  [{i}] {repr(line)}")
    print("---")

    # Parse and convert
    headers, data_rows = parse_table(text)

    if not headers:
        print("Could not parse headers from clipboard text")
        return

    # Fill in missing years
    headers, data_rows = fill_missing_years(headers, data_rows)

    tsv_output = to_tsv(headers, data_rows)

    # Write back to clipboard
    pyperclip.copy(tsv_output)

    print(f"Converted {len(headers)} columns and {len(data_rows)} rows")
    print("Result copied to clipboard - ready to paste into Excel!")

if __name__ == "__main__":
    main()
