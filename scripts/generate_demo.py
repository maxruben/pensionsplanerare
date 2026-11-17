"""Generera Saved/demo_planer.json från en fabricerad demo.

Körs av build.py före distribution. Kan också köras manuellt:

    python scripts/generate_demo.py

Bygger två planer (2027-01 10 år och 15 år) med fabricerade siffror
och kombinerar dem med inställningarna i templates/demo_settings.json
(sparande, tillväxt, utgifter, utgiftskategorier). Utdatafilen är
ignored av .gitignore — sköts helt av scriptet.
"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path

# Se till att projektroten är i sys.path så parsing/pension_planner kan importeras
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from parsing import Breakpoint, Insurance, ParsedPlan
from pension_planner import _parsed_plan_to_dict, _active_at

TEMPLATE_PATH = ROOT / 'templates' / 'demo_settings.json'
OUTPUT_PATH = ROOT / 'Saved' / 'demo_planer.json'

BIRTH_YEAR = 1962
BIRTH_MONTH = 5

# Datum-lista över breakpoint-datum för alla planer (samma i båda).
# Alla försäkringars start- och slutdatum måste finnas i listan för att
# _synth_optimized_plan ska kunna slå upp per-datum-belopp korrekt.
BP_DATES = sorted(set([
    (2027, 1),   # pension börjar (tjänstepensioner + Avanza start)
    (2027, 5),   # allmän pension tillkommer
    (2028, 1), (2029, 1),
    (2030, 1),   # skattesänkning från 68
    (2031, 1), (2032, 1),
    (2033, 1),   # SPP startar, Avanza slutar (5 år från 2027-01)
    (2035, 1),
    (2036, 1),   # SPP slutar (3 år)
    (2037, 1),   # tjänstepensioner 10-år slutar
    (2039, 1),
    (2042, 1),   # tjänstepensioner 15-år slutar (plan 2)
    (2045, 1), (2050, 1),
]))


def _tax_rate_at(d):
    """Skattesatser per datum (förhöjt grundavdrag från 68 år = 2030 för 1962)."""
    if d < (2030, 1):
        return 0.31
    if d < (2033, 1):
        return 0.21
    return 0.19


def _ins(name, start, dur, amount_per_bp):
    """Skapa Insurance med amounts = amount_per_bp för alla bp där
    försäkringen är aktiv, 0 annars."""
    amounts = [
        amount_per_bp if _active_at(d, start, dur) else 0 for d in BP_DATES
    ]
    return Insurance(
        name=name, start=start, duration=dur, amounts_before_tax=amounts,
    )


def _build_plan(name, insurances):
    """Bygg ParsedPlan där bp-totaler beräknas från per-insurance amounts."""
    bps = []
    for d in BP_DATES:
        total_before = 0
        for ins in insurances:
            if _active_at(d, ins.start, ins.duration):
                bp_idx = BP_DATES.index(d)
                total_before += ins.amounts_before_tax[bp_idx] or 0
        rate = _tax_rate_at(d)
        total_after = int(total_before * (1 - rate)) if total_before > 0 else None
        bps.append(Breakpoint(
            date=d, before_tax=int(total_before), after_tax=total_after,
        ))
    return ParsedPlan(
        plan_name=name, insurances=insurances, breakpoints=bps,
        birth_year=BIRTH_YEAR, birth_month=BIRTH_MONTH,
        earliest_start=(2027, 1),
    )


def build_plans():
    """Returnera de två demo-planerna."""
    p1 = _build_plan('Demo 2027-01 10 år', [
        _ins('Avanza - Pensionförsäkring',       (2027, 1),  (5, 0),   3200),
        _ins('Alecta - ITP2 Ålderspension',        (2027, 1),  None,     5400),
        _ins('AMF - ITPK #1',                       (2027, 1),  (10, 0),  3200),
        _ins('AMF - ITPK #2',                       (2027, 1),  (10, 0),  1400),
        _ins('Handelsbanken Liv - Avtalspension ITPK 2018',
                                                    (2027, 1),  (10, 0),  2400),
        _ins('Skandia - Pensionsförsäkring',       (2027, 5),  None,     1100),
        _ins('SPP - ITPK Egenpension',              (2033, 1),  (3, 0),   1200),
        _ins('Inkomstpension',                      (2027, 5),  None,    15600),
        _ins('Premiepension',                       (2027, 5),  None,     3400),
    ])
    p2 = _build_plan('Demo 2027-01 15 år', [
        _ins('Avanza - Pensionförsäkring',       (2027, 1),  (5, 0),   3200),
        _ins('Alecta - ITP2 Ålderspension',        (2027, 1),  None,     5400),
        _ins('AMF - ITPK #1',                       (2027, 1),  (15, 0),  2200),
        _ins('AMF - ITPK #2',                       (2027, 1),  (15, 0),   950),
        _ins('Handelsbanken Liv - Avtalspension ITPK 2018',
                                                    (2027, 1),  (15, 0),  1650),
        _ins('Skandia - Pensionsförsäkring',       (2027, 5),  None,     1100),
        _ins('SPP - ITPK Egenpension',              (2033, 1),  (3, 0),   1200),
        _ins('Inkomstpension',                      (2027, 5),  None,    15600),
        _ins('Premiepension',                       (2027, 5),  None,     3400),
    ])
    return p1, p2


def main():
    if not TEMPLATE_PATH.exists():
        sys.exit(f'Saknar template: {TEMPLATE_PATH}')
    with open(TEMPLATE_PATH, encoding='utf-8') as f:
        settings = json.load(f)

    p1, p2 = build_plans()

    data = {
        'version': 2,
        'settings': settings,
        'plans': [
            {
                'paste_text': '# Demo-plan — fabricerade siffror för screenshots.\n',
                'sim_parsed': _parsed_plan_to_dict(p1),
            },
            {
                'paste_text': '# Demo-plan — fabricerade siffror för screenshots.\n',
                'sim_parsed': _parsed_plan_to_dict(p2),
            },
        ],
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f'Skapade {OUTPUT_PATH.relative_to(ROOT)}: 2 planer, birth {BIRTH_YEAR}-{BIRTH_MONTH:02d}')


if __name__ == '__main__':
    main()
