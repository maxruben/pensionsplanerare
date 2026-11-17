"""Testscenarier för pension_planner.

Bygger ParsedPlan-objekt direkt och kör calculate_plan utan att gå via
text-parsning eller GUI. Verifierar att uträkningar fungerar för varierade indata.

Kör med:
    python test_scenarios.py

För att generera en JSON-fil som kan öppnas via Arkiv → Öppna i programmet:
    python generate_test_plans.py
"""

from parsing import Breakpoint, Insurance, ParsedPlan
from pension_planner import calculate_plan, fmt_kr


DEFAULT_BRACKETS = [(63, 24000), (70, 28000), (75, 26000), (80, 23000), (85, 21500), (100, 21500)]
NUM_COST_SETS_DEFAULT = 5


# Alla scenarier som en datastruktur — delas av test-kör och JSON-generering.
SCENARIOS = [
    {
        'title': "Minimal — bara statlig pension från 67 år, livsvarigt",
        'parsed': ParsedPlan(
            plan_name='Minimal',
            insurances=[
                Insurance('Inkomstpension', (2030, 6), None),
                Insurance('Premiepension', (2030, 6), None),
            ],
            breakpoints=[Breakpoint((2030, 6), 18000, 13000)],
            birth_year=1963, birth_month=6, earliest_start=(2030, 6),
        ),
        'sparande': 500_000,
        'brackets': DEFAULT_BRACKETS,
    },
    {
        'title': "Tidig pension 60 år, hög månadskostnad",
        'parsed': ParsedPlan(
            plan_name='Tidig pension',
            insurances=[
                Insurance('Privat pensionsförsäkring', (2023, 1), (10, 0)),
                Insurance('Inkomstpension', (2030, 1), None),
            ],
            breakpoints=[
                Breakpoint((2023, 1), 35000, 25000),
                Breakpoint((2030, 1), 50000, 35000),
            ],
            birth_year=1963, birth_month=1, earliest_start=(2023, 1),
        ),
        'sparande': 800_000,
        'brackets': [(70, 35000), (80, 30000), (100, 25000)],
    },
    {
        'title': "Många försäkringar med olika startår och durationer",
        'parsed': ParsedPlan(
            plan_name='Komplex plan',
            insurances=[
                Insurance('Avanza Pension', (2026, 6), (5, 0)),
                Insurance('Alecta ITP2', (2026, 6), None),
                Insurance('AMF ITPK', (2026, 6), (10, 0)),
                Insurance('AMF ITPK 2', (2026, 6), (10, 0)),
                Insurance('Handelsbanken', (2027, 1), (12, 0)),
                Insurance('Skandia', (2028, 6), None),
                Insurance('SPP', (2026, 6), (2, 0)),
                Insurance('Inkomstpension', (2027, 6), None),
                Insurance('Premiepension', (2027, 6), None),
            ],
            breakpoints=[
                Breakpoint((2026, 6), 55000, 39000),
                Breakpoint((2028, 6), 60000, 42000),
                Breakpoint((2031, 6), 45000, 32000),
                Breakpoint((2038, 6), 38000, 27000),
            ],
            birth_year=1963, birth_month=6, earliest_start=(2026, 6),
        ),
        'sparande': 600_000,
        'brackets': DEFAULT_BRACKETS,
    },
    {
        'title': "Sen pension 70 år, högt sparande",
        'parsed': ParsedPlan(
            plan_name='Sen pension',
            insurances=[
                Insurance('Privat', (2033, 6), (15, 0)),
                Insurance('Inkomstpension', (2033, 6), None),
            ],
            breakpoints=[
                Breakpoint((2033, 6), 45000, 32000),
                Breakpoint((2048, 6), 25000, 18000),
            ],
            birth_year=1963, birth_month=6, earliest_start=(2033, 6),
        ),
        'sparande': 1_500_000,
        'brackets': [(70, 30000), (80, 25000), (100, 20000)],
    },
    {
        'title': "Permanent underskott — utgifter > inkomst hela tiden",
        'parsed': ParsedPlan(
            plan_name='Underskott',
            insurances=[
                Insurance('Allmän pension', (2030, 6), None),
            ],
            breakpoints=[Breakpoint((2030, 6), 20000, 14000)],
            birth_year=1963, birth_month=6, earliest_start=(2030, 6),
        ),
        'sparande': 300_000,
        'brackets': [(70, 25000), (80, 22000), (100, 20000)],
    },
    {
        'title': "December-född — åldersövergångar sent på året",
        'parsed': ParsedPlan(
            plan_name='Dec-född',
            insurances=[
                Insurance('Pensionsförsäkring', (2026, 12), (10, 0)),
                Insurance('Inkomstpension', (2030, 12), None),
            ],
            breakpoints=[
                Breakpoint((2026, 12), 28000, 20000),
                Breakpoint((2030, 12), 45000, 32000),
            ],
            birth_year=1963, birth_month=12, earliest_start=(2026, 12),
        ),
        'sparande': 500_000,
        'brackets': DEFAULT_BRACKETS,
    },
    {
        'title': "Statisk — en utbetalning, samma belopp hela tiden",
        'parsed': ParsedPlan(
            plan_name='Statisk',
            insurances=[
                Insurance('Livränta', (2025, 1), None),
            ],
            breakpoints=[Breakpoint((2025, 1), 30000, 22000)],
            birth_year=1960, birth_month=1, earliest_start=(2025, 1),
        ),
        'sparande': 400_000,
        'brackets': DEFAULT_BRACKETS,
    },
    {
        'title': "Variabel — många breakpoints med olika belopp",
        'parsed': ParsedPlan(
            plan_name='Variabel',
            insurances=[
                Insurance('Tjänstepension', (2026, 1), (12, 0)),
                Insurance('Inkomstpension', (2027, 1), None),
            ],
            breakpoints=[
                Breakpoint((2026, 1), 30000, 22000),
                Breakpoint((2027, 1), 50000, 36000),
                Breakpoint((2030, 1), 48000, 34000),
                Breakpoint((2035, 1), 42000, 30000),
                Breakpoint((2038, 1), 25000, 18000),
            ],
            birth_year=1963, birth_month=6, earliest_start=(2026, 1),
        ),
        'sparande': 600_000,
        'brackets': DEFAULT_BRACKETS,
    },
    {
        'title': "Kortfristig — bara en försäkring i 5 år, sen inget",
        'parsed': ParsedPlan(
            plan_name='Kortfristig',
            insurances=[
                Insurance('Pensionsförsäkring 5 år', (2026, 1), (5, 0)),
            ],
            breakpoints=[Breakpoint((2026, 1), 25000, 18000)],
            birth_year=1963, birth_month=1, earliest_start=(2026, 1),
        ),
        'sparande': 700_000,
        'brackets': DEFAULT_BRACKETS,
    },
    {
        'title': "Disparit — överskott först, underskott när allt löpt ut",
        'parsed': ParsedPlan(
            plan_name='Disparit',
            insurances=[
                Insurance('Stor tjänstepension', (2026, 1), (10, 0)),
                Insurance('Inkomstpension', (2026, 1), None),
            ],
            breakpoints=[
                Breakpoint((2026, 1), 60000, 42000),
                Breakpoint((2036, 1), 15000, 11000),
            ],
            birth_year=1963, birth_month=1, earliest_start=(2026, 1),
        ),
        'sparande': 300_000,
        'brackets': DEFAULT_BRACKETS,
    },

    # ── Jämförelse-set: samma person/kapital, olika uttagstider ────────────────
    # Demonstrerar att kortare privat utbetalning kan ge högre slutkvar genom
    # att fler år hamnar i basavdrag-zonen för enbart statlig pension.
    {
        'title': "Jämförelse — kort uttag (10 år)",
        'parsed': ParsedPlan(
            plan_name='2026-11 10 år',
            insurances=[
                Insurance('Privat pensionsförsäkring', (2026, 11), (10, 0)),
                Insurance('Inkomstpension', (2027, 5), None),
                Insurance('Premiepension', (2027, 5), None),
            ],
            breakpoints=[
                Breakpoint((2026, 11), 25000, 18500),  # privat alone
                Breakpoint((2027, 5), 53000, 38000),   # +statlig pension
                Breakpoint((2036, 11), 28000, 22500),  # privat slutar, bara statlig
            ],
            birth_year=1963, birth_month=11, earliest_start=(2026, 11),
        ),
        'sparande': 400_000,
        'brackets': DEFAULT_BRACKETS,
    },
    {
        'title': "Jämförelse — medellång uttag (12 år)",
        'parsed': ParsedPlan(
            plan_name='2026-11 12 år',
            insurances=[
                Insurance('Privat pensionsförsäkring', (2026, 11), (12, 0)),
                Insurance('Inkomstpension', (2027, 5), None),
                Insurance('Premiepension', (2027, 5), None),
            ],
            breakpoints=[
                Breakpoint((2026, 11), 21000, 15500),
                Breakpoint((2027, 5), 49000, 35000),
                Breakpoint((2038, 11), 28000, 22500),
            ],
            birth_year=1963, birth_month=11, earliest_start=(2026, 11),
        ),
        'sparande': 400_000,
        'brackets': DEFAULT_BRACKETS,
    },
    {
        'title': "Jämförelse — långt uttag (15 år)",
        'parsed': ParsedPlan(
            plan_name='2026-11 15 år',
            insurances=[
                Insurance('Privat pensionsförsäkring', (2026, 11), (15, 0)),
                Insurance('Inkomstpension', (2027, 5), None),
                Insurance('Premiepension', (2027, 5), None),
            ],
            breakpoints=[
                Breakpoint((2026, 11), 17000, 12500),
                Breakpoint((2027, 5), 45000, 32000),
                Breakpoint((2041, 11), 28000, 22500),
            ],
            birth_year=1963, birth_month=11, earliest_start=(2026, 11),
        ),
        'sparande': 400_000,
        'brackets': DEFAULT_BRACKETS,
    },

    # ── Stora portföljer — testar färg/layout med många försäkringar ──────────
    {
        'title': "Stor portfölj — 14 försäkringar",
        'parsed': ParsedPlan(
            plan_name='Bred portfölj 14',
            insurances=[
                Insurance('Avanza Pension', (2026, 6), (5, 0)),
                Insurance('Alecta ITP2', (2026, 6), None),
                Insurance('AMF ITPK', (2026, 6), (10, 0)),
                Insurance('AMF ITPK 2', (2026, 6), (10, 0)),
                Insurance('Handelsbanken', (2027, 1), (12, 0)),
                Insurance('Skandia', (2028, 6), None),
                Insurance('SPP', (2026, 6), (2, 0)),
                Insurance('Nordea', (2027, 6), (8, 0)),
                Insurance('SEB Trygg Liv', (2026, 6), (15, 0)),
                Insurance('Swedbank Robur', (2028, 1), (10, 0)),
                Insurance('Folksam', (2026, 6), (7, 0)),
                Insurance('KPA Pension', (2030, 6), None),
                Insurance('Inkomstpension', (2030, 6), None),
                Insurance('Premiepension', (2030, 6), None),
            ],
            breakpoints=[
                Breakpoint((2026, 6), 48000, 34000),
                Breakpoint((2030, 6), 75000, 53000),
                Breakpoint((2036, 6), 52000, 37000),
                Breakpoint((2041, 6), 32000, 25000),
            ],
            birth_year=1963, birth_month=6, earliest_start=(2026, 6),
        ),
        'sparande': 800_000,
        'brackets': DEFAULT_BRACKETS,
    },
    {
        'title': "Mycket stor portfölj — 18 försäkringar",
        'parsed': ParsedPlan(
            plan_name='Maximal portfölj 18',
            insurances=[
                Insurance('Avanza Pension', (2026, 6), (5, 0)),
                Insurance('Alecta ITP2', (2026, 6), None),
                Insurance('AMF ITPK', (2026, 6), (10, 0)),
                Insurance('AMF ITPK 2', (2026, 6), (10, 0)),
                Insurance('Handelsbanken Liv', (2027, 1), (12, 0)),
                Insurance('Skandia Liv', (2028, 6), None),
                Insurance('SPP Pension', (2026, 6), (2, 0)),
                Insurance('Nordea Liv', (2027, 6), (8, 0)),
                Insurance('SEB Trygg Liv', (2026, 6), (15, 0)),
                Insurance('Swedbank Robur', (2028, 1), (10, 0)),
                Insurance('Folksam LO Pension', (2026, 6), (7, 0)),
                Insurance('KPA Pension', (2030, 6), None),
                Insurance('Länsförsäkringar', (2027, 6), (12, 0)),
                Insurance('Movestic', (2029, 1), (5, 0)),
                Insurance('Söderberg Partners', (2026, 6), (4, 0)),
                Insurance('Danica Pension', (2028, 6), (10, 0)),
                Insurance('Inkomstpension', (2030, 6), None),
                Insurance('Premiepension', (2030, 6), None),
            ],
            breakpoints=[
                Breakpoint((2026, 6), 55000, 39000),
                Breakpoint((2030, 6), 92000, 65000),
                Breakpoint((2036, 6), 62000, 44000),
                Breakpoint((2041, 6), 30000, 24000),
            ],
            birth_year=1963, birth_month=6, earliest_start=(2026, 6),
        ),
        'sparande': 1_200_000,
        'brackets': DEFAULT_BRACKETS,
    },
]


def print_scenario(scenario):
    """Skriv ut nyckeltal för ett scenario — första 3 år, sista 3 år."""
    title = scenario['title']
    parsed = scenario['parsed']
    sparande = scenario['sparande']
    brackets = scenario['brackets']

    print()
    print('=' * 78)
    print(f"SCENARIO: {title}")
    print('=' * 78)
    print(f"  Födelse:        {parsed.birth_year}-{parsed.birth_month:02d}")
    print(f"  Tidigaste uttag: {parsed.earliest_start}")
    print(f"  Försäkringar:   {len(parsed.insurances)}")
    print(f"  Sparande:       {fmt_kr(sparande)}")
    print(f"  Brytpunkter:    {brackets}")
    print()

    calc = calculate_plan(parsed, sparande=sparande, cost_brackets=brackets)
    if not calc.yearly:
        print("  (Inga år beräknade)")
        return

    hdr = f"  {'År':<6}{'Ålder':<7}{'Diff/år':>11}{'Utg/mån':>10}"
    hdr += f"{'Kvar #1':>13}{'Kvar #2':>13}{'Kvar #3':>13}"
    print(hdr)
    print('  ' + '-' * 73)

    def row(y):
        return (
            f"  {y.year:<6}{y.age:<7}"
            f"{fmt_kr(y.overskott_ar):>11}{fmt_kr(y.utgifter_man):>10}"
            f"{fmt_kr(y.kvar_1):>13}{fmt_kr(y.kvar_2):>13}{fmt_kr(y.kvar_3):>13}"
        )

    yrs = calc.yearly
    for y in yrs[:3]:
        print(row(y))
    if len(yrs) > 6:
        print('  ...')
    for y in yrs[-3:]:
        print(row(y))

    last = yrs[-1]
    print()
    print(f"  Slutsaldo år {last.year}:  "
          f"Kvar#1={fmt_kr(last.kvar_1)}  "
          f"Kvar#2={fmt_kr(last.kvar_2)}  "
          f"Kvar#3={fmt_kr(last.kvar_3)}")


def main():
    for s in SCENARIOS:
        print_scenario(s)
    print()
    print('=' * 78)
    print("Alla scenarier körda.")
    print('=' * 78)


if __name__ == '__main__':
    main()
