"""Parser för text kopierad från minpension.se."""

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Insurance:
    name: str
    start: Optional[tuple]  # (year, month)
    duration: Optional[tuple]  # (years, months); None == Livsvarigt
    # Per-brytpunkts kr/mån-belopp före skatt, parallell till ParsedPlan.breakpoints.
    # Tom lista = ingen per-försäkring-data tillgänglig (befintliga paste-flöden).
    amounts_before_tax: list = field(default_factory=list)


@dataclass
class Breakpoint:
    date: tuple  # (year, month)
    before_tax: Optional[int]
    after_tax: Optional[int]


@dataclass
class ParsedPlan:
    plan_name: str = ''
    insurances: list = field(default_factory=list)
    breakpoints: list = field(default_factory=list)
    birth_year: Optional[int] = None
    birth_month: Optional[int] = None
    earliest_start: Optional[tuple] = None


_MONTHS = {
    'januari': 1, 'februari': 2, 'mars': 3, 'april': 4,
    'maj': 5, 'juni': 6, 'juli': 7, 'augusti': 8,
    'september': 9, 'oktober': 10, 'november': 11, 'december': 12,
}


def parse_amount(s):
    """Parse amount string like '56 900' to integer, returns None for '-' or empty."""
    s = s.strip()
    if not s or s == '-':
        return None
    return int(s.replace(' ', '').replace('\xa0', ''))


def parse_date(date_str):
    """Parse '2026, mars' to (2026, 3)."""
    match = re.match(r'(\d{4}),\s*(\w+)', date_str.strip())
    if match:
        year = int(match.group(1))
        month = _MONTHS.get(match.group(2).lower())
        return (year, month) if month else None
    return None


def parse_duration(dur_str):
    """Parse '5 år' or '12 år' to (years, months). Returns None for 'Livet ut'."""
    dur_str = dur_str.strip()
    if 'livet ut' in dur_str.lower():
        return None
    years = 0
    months = 0
    y_match = re.search(r'(\d+)\s*år', dur_str)
    m_match = re.search(r'(\d+)\s*mån', dur_str)
    if y_match:
        years = int(y_match.group(1))
    if m_match:
        months = int(m_match.group(1))
    return (years, months)


def extract_age_info(age_str, date_str):
    """Extract birth year and month from age string and date."""
    age_match = re.match(r'(\d+)\s*år(?:,?\s*(\d+)\s*mån)?', age_str.strip())
    date = parse_date(date_str)
    if age_match and date:
        years = int(age_match.group(1))
        months = int(age_match.group(2)) if age_match.group(2) else 0
        total_months = date[0] * 12 + date[1] - (years * 12 + months)
        birth_year = total_months // 12
        birth_month = total_months % 12
        if birth_month == 0:
            birth_month = 12
            birth_year -= 1
        return birth_year, birth_month
    return None, None


def parse_minpension_text(text: str) -> ParsedPlan:
    """Parse text copied from minpension.se into a ParsedPlan."""
    result = ParsedPlan()
    if not text:
        return result

    lines = [l.replace('\r', '').strip() for l in text.strip().split('\n')]

    # Plan name
    for i, line in enumerate(lines):
        if line.startswith('Skapad '):
            for j in range(i - 1, max(i - 10, 0), -1):
                if lines[j] and not lines[j].isdigit():
                    result.plan_name = lines[j]
                    break
            break

    # Insurance table
    for i, line in enumerate(lines):
        if line.startswith('Pensionsf') and '\t' in line:
            j = i + 1
            while j < len(lines):
                l = lines[j]
                if '\t' in l:
                    cells = l.split('\t')
                    name = cells[0].strip()
                    start_str = cells[2].strip() if len(cells) > 2 else ''
                    dur_str = cells[3].strip() if len(cells) > 3 else ''
                    if name:
                        start = parse_date(start_str)
                        dur = parse_duration(dur_str) if dur_str else None
                        result.insurances.append(Insurance(name=name, start=start, duration=dur))
                elif l in ('Ändra', '\xc4ndra', ''):
                    j += 1
                    continue
                elif 'allm' in l.lower() and '\t' in l:
                    j += 1
                    continue
                elif j + 1 < len(lines) and '\t' in lines[j + 1]:
                    j += 1
                    continue
                else:
                    break
                j += 1
            break

    # Allmän pension (Inkomstpension, Premiepension) — kan ligga utanför huvudtabellen
    existing_names = {ins.name.lower() for ins in result.insurances}
    state_pension_names = ('inkomstpension', 'premiepension')
    for line in lines:
        cells = line.split('\t')
        if not cells:
            continue
        name = cells[0].strip()
        if name.lower() not in state_pension_names:
            continue
        if name.lower() in existing_names:
            continue
        start = None
        duration = None
        for cell in cells[1:]:
            cell_s = cell.strip()
            if not cell_s:
                continue
            if start is None:
                d = parse_date(cell_s)
                if d:
                    start = d
                    continue
            if duration is None:
                duration = parse_duration(cell_s)
        result.insurances.append(Insurance(name=name, start=start, duration=duration))
        existing_names.add(name.lower())

    # Amounts (breakpoints with dates + före/efter skatt)
    amounts_start = None
    for i, line in enumerate(lines):
        if 'alla belopp visas som' in line.lower():
            amounts_start = i
            break

    dates = []
    before_tax = []
    after_tax = []
    if amounts_start is not None:
        i = amounts_start + 1
        dates_age_line = None
        while i < len(lines):
            line = lines[i]
            if 'summa' in line.lower():
                break
            if parse_date(line):
                if dates_age_line:
                    birth_yr, birth_mo = extract_age_info(dates_age_line, line)
                    if birth_yr and result.birth_year is None:
                        result.birth_year, result.birth_month = birth_yr, birth_mo
                dates.append(parse_date(line))
                dates_age_line = None
            elif re.search(r'\d+\s*[åa]r', line.lower()) or 'år' in line:
                dates_age_line = line
            i += 1

        num_columns = len(dates)
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
            elif line == '' and after_tax:
                break
            else:
                i += 1

    # Build breakpoints
    for idx, date in enumerate(dates):
        b = before_tax[idx] if idx < len(before_tax) else None
        a = after_tax[idx] if idx < len(after_tax) else None
        result.breakpoints.append(Breakpoint(date=date, before_tax=b, after_tax=a))

    # Earliest withdrawal start
    for ins in result.insurances:
        if ins.start:
            if result.earliest_start is None or ins.start < result.earliest_start:
                result.earliest_start = ins.start

    # Per-försäkring-belopp (om data är hämtad via minpension-scraper-wizarden)
    _parse_per_insurance_amounts(lines, len(result.breakpoints), result.insurances)

    # Disambiguera försäkringar med samma namn: lägg till "#N"-suffix när
    # samma namn förekommer flera gånger (t.ex. 4 st "AMF - ITPK" → #1..#4).
    # Görs efter parsningen så matchning i _parse_per_insurance_amounts fortsatt
    # fungerar på rå-namn.
    _name_counts = {}
    for ins in result.insurances:
        _name_counts[ins.name] = _name_counts.get(ins.name, 0) + 1
    _seen = {}
    for ins in result.insurances:
        if _name_counts.get(ins.name, 0) > 1:
            n = _seen.get(ins.name, 0) + 1
            _seen[ins.name] = n
            ins.name = f'{ins.name} #{n}'

    return result


def _is_value_line(line: str) -> bool:
    """Returnerar True om raden är tom, '-' eller bara numerisk (med mellanslag)."""
    s = line.strip()
    if not s or s == '-':
        return True
    cleaned = s.replace(' ', '').replace('\xa0', '').lstrip('-')
    return cleaned.isdigit()


def _normalize_name(name: str) -> str:
    """Normalisera försäkringsnamn för matchning: gemener, kollapsade mellanslag,
    tar bort mellanslag runt bindestreck (så 'AMF - ITPK' == 'AMF -ITPK')."""
    s = ' '.join(name.split()).lower()
    return re.sub(r'\s*-\s*', '-', s)


def _read_value_lines(lines: list, start_idx: int, max_count: int):
    """Läs upp till max_count värde-rader från start_idx. Stoppar vid första
    icke-värde, icke-tomma rad (signal att vi nått slutet av värdesektionen).
    Returnerar (amounts, next_idx). amounts paddas med None till max_count."""
    amounts = []
    j = start_idx
    while j < len(lines) and len(amounts) < max_count:
        line = lines[j].strip()
        if not line:
            # Tom rad — om vi redan har data, sluta. Annars hoppa.
            if amounts:
                break
            j += 1
            continue
        if _is_value_line(line):
            amounts.append(parse_amount(line))
            j += 1
        else:
            # Icke-värde, icke-tom — slut på värdesektion
            break
    while len(amounts) < max_count:
        amounts.append(None)
    return amounts, j


def _parse_per_insurance_amounts(lines: list, num_dates: int, insurances: list) -> None:
    """Parsa per-försäkring kr/mån-belopp efter Summa-raderna.

    Sätter `insurance.amounts_before_tax` på matchande insurances. Muterar in-place.

    Robust mot:
    - Försäkringsnamn med olika whitespace runt bindestreck ('-' vs ' - ')
    - Inbäddade detaljsektioner (om användaren råkat expandera en pension manuellt
      i Chrome — då finns extra rader som 'Försäkringsnummer:', 'Uttagsstart' osv
      mitt i värdesektionen). Vi hoppar över obekanta rader istället för att
      konsumera värden från dem.
    """
    if num_dates <= 0 or not insurances:
        return

    # Hitta start av per-kategori-sektionen
    start_idx = None
    for i, line in enumerate(lines):
        if 'läs om underlaget' in line.lower():
            start_idx = i + 1
            break
    if start_idx is None:
        # Fallback: efter raden "Summa efter skatt" + num_dates värden
        for i, line in enumerate(lines):
            lower = line.lower()
            if 'summa' in lower and 'efter' in lower and 'skatt' in lower:
                start_idx = i + num_dates + 1
                break
    if start_idx is None:
        return

    category_names_lower = {'lön', 'privat pension', 'tjänstepension', 'allmän pension'}
    # End-markörer som matchas exakt (inte substring) — "tips till dig" duger
    # inte eftersom det förekommer även inne i expanderade detalj-block
    # ('1 tips till dig'-undersektion). "Observera" / "Så har vi räknat" är
    # specifika för sidans slutsektion.
    end_markers_exact = {'observera', 'så har vi räknat', 'att tänka på'}

    available = list(insurances)  # FIFO matchning för insurances med samma namn
    # För-normaliserade namn-cache för snabb matchning
    norm_names = [_normalize_name(ins.name) for ins in insurances]

    i = start_idx
    while i < len(lines):
        # Slut om alla försäkringar matchats — inga fler värden av intresse
        if not available:
            break
        line = lines[i].strip()
        line_lower = line.lower()

        if line_lower in end_markers_exact:
            break
        if not line:
            i += 1
            continue

        # Kategori-rubrik? (med ev. "(N)" + värden för kategori-total)
        if line_lower in category_names_lower:
            i += 1
            if i < len(lines) and re.match(r'^\(\d+\)$', lines[i].strip()):
                i += 1
            # Konsumera kategori-totalvärden — använd robusta läsaren så vi
            # stoppar vid första icke-värde-rad
            _, i = _read_value_lines(lines, i, num_dates)
            continue

        # Värde-rad utan kontext — okänd, hoppa
        if _is_value_line(line):
            i += 1
            continue

        # Hitta matchande försäkring via normaliserat namn
        target = _normalize_name(line)
        matched_avail_idx = None
        for j, ins in enumerate(available):
            # Återanvänd cachen — hitta originalens index för att kunna matcha
            orig_idx = insurances.index(ins)
            if norm_names[orig_idx] == target:
                matched_avail_idx = j
                break

        if matched_avail_idx is not None:
            amounts, i = _read_value_lines(lines, i + 1, num_dates)
            available.pop(matched_avail_idx).amounts_before_tax = amounts
        else:
            # Okänd icke-värde-rad — sannolikt expanderad detalj. Hoppa BARA
            # denna rad (ej num_dates värden) så vi inte kommer ur synk.
            i += 1
