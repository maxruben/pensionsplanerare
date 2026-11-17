"""Generera test_plans.json med scenarier som kan öppnas via Arkiv → Öppna.

Använder scenarierna från test_scenarios.py och konverterar varje ParsedPlan
till minpension.se-formaterad text som programmets parser kan läsa.

Kör med:
    python generate_test_plans.py
"""

import json
import os

from parsing import ParsedPlan, parse_minpension_text
from test_scenarios import DEFAULT_BRACKETS, NUM_COST_SETS_DEFAULT, SCENARIOS


SWEDISH_MONTHS = [
    'januari', 'februari', 'mars', 'april', 'maj', 'juni',
    'juli', 'augusti', 'september', 'oktober', 'november', 'december',
]


def _date_str(date):
    return f"{date[0]}, {SWEDISH_MONTHS[date[1] - 1]}"


def _duration_str(duration):
    if duration is None:
        return 'Livet ut'
    y, m = duration
    if m == 0:
        return f"{y} år"
    return f"{y} år, {m} mån"


def _age_str(birth_year, birth_month, date):
    yr, mo = date
    months = yr * 12 + mo - (birth_year * 12 + birth_month)
    years = months // 12
    rem = months % 12
    if rem == 0:
        return f"{years} år"
    return f"{years} år, {rem} mån"


def generate_paste_text(parsed: ParsedPlan) -> str:
    """Generera minpension.se-stilad text som parse_minpension_text kan läsa."""
    state_pension_names = {'inkomstpension', 'premiepension'}
    lines = []

    # Header + plan-namn + 'Skapad'-rad (parsern går bakåt från 'Skapad' och
    # behöver några rader att leta i för att hitta plan-namnet)
    lines.append('Pensionsplan')
    lines.append('')
    lines.append(parsed.plan_name)
    lines.append('Skapad 2026-05-01')
    lines.append('')

    # Pensionsförsäkring-tabell
    lines.append('Pensionsförsäkring\tInformation\tUttagsstart\tUttagslängd')
    for ins in parsed.insurances:
        if ins.name.lower() in state_pension_names:
            continue
        start = _date_str(ins.start) if ins.start else ''
        dur = _duration_str(ins.duration)
        lines.append(f"{ins.name}\t\t{start}\t{dur}")
    lines.append('')

    # Allmän pension (Inkomstpension / Premiepension) i egen sektion
    has_state = any(
        ins.name.lower() in state_pension_names for ins in parsed.insurances
    )
    if has_state:
        lines.append('Allmän pension')
        for ins in parsed.insurances:
            if ins.name.lower() not in state_pension_names:
                continue
            start = _date_str(ins.start) if ins.start else ''
            dur = _duration_str(ins.duration)
            lines.append(f"{ins.name}\t\t{start}\t{dur}")
        lines.append('')

    # Beloppssektion
    lines.append('Alla belopp visas som kr/mån')

    # Åldersrad innan första datumet → parsern kan härleda birth_year/month
    if parsed.breakpoints and parsed.birth_year:
        first_bp = parsed.breakpoints[0]
        lines.append(_age_str(parsed.birth_year, parsed.birth_month, first_bp.date))

    # Datumrader
    for bp in parsed.breakpoints:
        lines.append(_date_str(bp.date))

    # Summa före skatt med en rad per datumcolumn
    lines.append('Summa före skatt')
    for bp in parsed.breakpoints:
        v = bp.before_tax
        lines.append(str(v) if v is not None else '-')

    # Summa efter skatt
    lines.append('Summa efter skatt')
    for bp in parsed.breakpoints:
        v = bp.after_tax
        lines.append(str(v) if v is not None else '-')

    # Tom rad signalerar parser-loopen att vi är klara
    lines.append('')
    return '\n'.join(lines)


def verify(parsed_original, paste_text):
    """Verifiera att genererad text parsas tillbaka till samma struktur."""
    re_parsed = parse_minpension_text(paste_text)
    issues = []
    if re_parsed.plan_name != parsed_original.plan_name:
        issues.append(f"plan_name: {re_parsed.plan_name!r} != {parsed_original.plan_name!r}")
    if re_parsed.birth_year != parsed_original.birth_year:
        issues.append(f"birth_year: {re_parsed.birth_year} != {parsed_original.birth_year}")
    if re_parsed.birth_month != parsed_original.birth_month:
        issues.append(f"birth_month: {re_parsed.birth_month} != {parsed_original.birth_month}")
    if re_parsed.earliest_start != parsed_original.earliest_start:
        issues.append(f"earliest_start: {re_parsed.earliest_start} != {parsed_original.earliest_start}")
    if len(re_parsed.breakpoints) != len(parsed_original.breakpoints):
        issues.append(
            f"breakpoints count: {len(re_parsed.breakpoints)} != "
            f"{len(parsed_original.breakpoints)}"
        )
    else:
        for i, (a, b) in enumerate(zip(re_parsed.breakpoints, parsed_original.breakpoints)):
            if a.date != b.date or a.before_tax != b.before_tax or a.after_tax != b.after_tax:
                issues.append(f"breakpoint[{i}]: {a} != {b}")
    return issues


def main():
    plans = []
    print("Genererar paste_text för scenarier...")
    all_ok = True
    for scenario in SCENARIOS:
        parsed = scenario['parsed']
        text = generate_paste_text(parsed)
        issues = verify(parsed, text)
        status = "OK" if not issues else "FAIL"
        print(f"  [{status}] {scenario['title']}")
        if issues:
            for issue in issues:
                print(f"      - {issue}")
            all_ok = False
        plans.append({'paste_text': text})

    if not all_ok:
        print("\nVarning: vissa scenarier parsades inte tillbaka identiskt.")

    # Bygg JSON i samma format som programmet sparar i (version 2)
    data = {
        'version': 2,
        'settings': {
            'sparande': '500000',
            'ages': [str(age) for age, _ in DEFAULT_BRACKETS],
            'cost_sets': [
                [str(c) for _, c in DEFAULT_BRACKETS]
                for _ in range(NUM_COST_SETS_DEFAULT)
            ],
            'active_set': 0,
            # Frontar jämförelseplanen med 10 år — användaren kan navigera till
            # 12 år / 15 år för att se trade-offen.
            'active_plan': '2026-11 10 år',
        },
        'plans': plans,
    }

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'test_plans.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"\nSkapade {out_path}")
    print(f"Öppna via Arkiv → Öppna... i pension_planner för att se {len(plans)} planer.")


if __name__ == '__main__':
    main()
