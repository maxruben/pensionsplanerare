"""Pension Planner — fristående GUI-app för pensionsplanering.

Klistra in text från minpension.se, justera variabler live, exportera PDF.
"""

import argparse
import io
import json
import os
import re
import sys
import tkinter as tk
from dataclasses import dataclass, field
from datetime import date
from tkinter import filedialog, messagebox, ttk
from typing import Optional

def _app_dir() -> str:
    """Returnerar katalogen där .py- eller .exe-filen ligger."""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _rename_plan_in_text(text: str, old_name: str, new_name: str) -> str:
    """Byt plannamnet i klistrad minpension-text. Namnet är första icke-tomma,
    icke-numeriska raden ovanför 'Skapad ...' — samma regel som parsern."""
    lines = text.split('\n')
    stripped = [l.replace('\r', '').strip() for l in lines]
    for i, line in enumerate(stripped):
        if line.startswith('Skapad '):
            for j in range(i - 1, max(i - 10, 0), -1):
                if stripped[j] and not stripped[j].isdigit():
                    if stripped[j] == old_name:
                        lines[j] = lines[j].replace(old_name, new_name, 1)
                    return '\n'.join(lines)
            break
    return '\n'.join(lines)


STATE_FILE = os.path.join(_app_dir(), '.pension_planner_state.json')

# Födelseår som används för åldersberäkning när planen saknar födelsedatum
# (godtyckligt valt; riktiga planer från minpension.se har alltid ett eget).
FALLBACK_BIRTH_YEAR = 1960

# Simuleringen räknar till och med året personen fyller END_AGE. Värdet styrs
# av fältet "Räkna till ålder" i huvudfönstret (sparas i planfilen) och hålls
# i synk med fältet av PensionPlannerApp._sync_end_age.
DEFAULT_END_AGE = 90
MIN_END_AGE = 70
MAX_END_AGE = 110
END_AGE = DEFAULT_END_AGE

# Versionen är v{VERSION_MAJOR}.<n>.0, där n = antal commits i repot plus
# VERSION_COUNT_OFFSET. Förskjutningen gör att numreringen fortsätter efter
# v1.2.0, den sista versionen före det här repot — första committen här är
# v1.3.0. bump_version.py läser samma två konstanter.
VERSION_MAJOR = 1
VERSION_COUNT_OFFSET = 2
# Fallback som används när git inte är tillgängligt (typ vid .exe-körning).
# Uppdatera innan en .exe-build via "python bump_version.py".
FALLBACK_VERSION = 'v1.0.0'
FALLBACK_SHA = '0000000'
FALLBACK_DATE = '2026-11-17'


def get_app_version():
    """Returnerar (version_str, sha, commit_date, runtime_kind).

    Hämtar från git om möjligt (för .py-körning). Annars fallback (för .exe).
    Versionen är v<VERSION_MAJOR>.<antal commits + VERSION_COUNT_OFFSET>.0.
    runtime_kind är 'exe' om appen körs som PyInstaller-bundle, annars 'py'.
    commit_date är ISO-datum (YYYY-MM-DD) för HEAD-committen.
    """
    runtime_kind = 'exe' if getattr(sys, 'frozen', False) else 'py'
    if runtime_kind == 'py':
        try:
            import subprocess
            # Under pythonw.exe är parent-processens stdin/stdout/stderr None.
            # Om subprocess ärver dem kan git hänga för alltid på Windows. Sätt
            # ALLA explicit + CREATE_NO_WINDOW så inget konsol-fönster blinkar.
            creationflags = 0
            if sys.platform == 'win32':
                creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            kw = {
                'cwd': _app_dir(),
                'stdin': subprocess.DEVNULL,
                'stderr': subprocess.DEVNULL,
                'text': True,
                'creationflags': creationflags,
                'timeout': 3,
            }
            count = int(subprocess.check_output(
                ['git', 'rev-list', '--count', 'HEAD'], **kw,
            ).strip()) + VERSION_COUNT_OFFSET
            sha = subprocess.check_output(
                ['git', 'rev-parse', '--short', 'HEAD'], **kw,
            ).strip()
            commit_date = subprocess.check_output(
                ['git', 'show', '-s', '--format=%cs', 'HEAD'], **kw,
            ).strip()
            return (f'v{VERSION_MAJOR}.{count}.0', sha, commit_date,
                    runtime_kind)
        except Exception:
            pass
    return FALLBACK_VERSION, FALLBACK_SHA, FALLBACK_DATE, runtime_kind

import matplotlib
matplotlib.use('TkAgg')
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.ticker import FixedLocator, FuncFormatter, MultipleLocator
from matplotlib.offsetbox import (
    AnnotationBbox, DrawingArea, HPacker, TextArea, VPacker,
)
from matplotlib.patches import Circle
from matplotlib.lines import Line2D

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

from parsing import Breakpoint, Insurance, ParsedPlan, parse_minpension_text


# ---------------------------------------------------------------------------
# Beräkningskärna
# ---------------------------------------------------------------------------

@dataclass
class MonthlyRow:
    year: int
    month: int
    age: int
    before_tax: Optional[int]
    after_tax: Optional[int]
    utgifter: int
    overskott_man: int
    overskott_ar: Optional[int] = None


@dataclass
class YearlyRow:
    year: int
    age: int
    overskott_ar: int
    utgifter_man: int  # månadskostnad enligt brytpunkter för åldern detta år
    acc_1: float
    kvar_1: float
    acc_2: float
    kvar_2: float
    acc_3: float
    kvar_3: float


@dataclass
class CalculatedPlan:
    monthly: list = field(default_factory=list)
    yearly: list = field(default_factory=list)


def cost(age: int, brackets: list) -> int:
    """Returnerar månadskostnad för given ålder utifrån brytpunkter.

    brackets = [(63, 24000), (70, 28000), ...] sorterat stigande på ålder.
    Hittar minsta brytpunkt där age <= brytpunkt-åldern.
    """
    for limit_age, cost_value in brackets:
        if age <= limit_age:
            return cost_value
    return brackets[-1][1] if brackets else 0


def calculate_plan(
    parsed: ParsedPlan,
    sparande: float,
    cost_brackets: list,
    end_year: int = None,
    growth_pct: float = 0.0,
    growth_mode: str = 'monthly',
) -> CalculatedPlan:
    """Beräkna månadsrader och årssummering.

    end_year: sista året i prognosen. Default = året då personen fyller
    END_AGE ("Räkna till ålder", normalt 90), annars 2053 som fallback.
    growth_pct: årlig procentuell tillväxt på kvarvarande sparande (kvar #1-3).
    Tillämpas månadsvis (1/12 per månad). 0.0 = ingen tillväxt.
    growth_mode: 'monthly' (diff + ränta varje månad, regler per månad) eller
    'yearly' (ränta månadsvis, diff vid årets slut, regler per år).
    """
    start_year = parsed.earliest_start[0] if parsed.earliest_start else 2027
    earliest_start = parsed.earliest_start
    birth_year = parsed.birth_year
    birth_month = parsed.birth_month
    breakpoints = parsed.breakpoints

    if end_year is None:
        end_year = (birth_year + END_AGE) if birth_year else 2053

    result = CalculatedPlan()

    # Förbered breakpoints sorterade på datum (de bör redan vara det)
    sorted_bp = sorted(breakpoints, key=lambda b: b.date)

    # Hitta första breakpoint med värde (för "första-uttaget"-fyllning)
    first_before = next((bp.before_tax for bp in sorted_bp if bp.before_tax is not None), None)
    first_after = next((bp.after_tax for bp in sorted_bp if bp.after_tax is not None), None)

    # Pass 1: månadsrader (hoppa över månader före tidigaste utbetalning)
    for year in range(start_year, end_year + 1):
        for month in range(1, 13):
            if earliest_start and (year, month) < earliest_start:
                continue
            if birth_year and birth_month:
                if month >= birth_month:
                    age = year - birth_year
                else:
                    age = year - birth_year - 1
            else:
                age = year - FALLBACK_BIRTH_YEAR

            before_val = None
            after_val = None
            for bp in sorted_bp:
                if bp.date <= (year, month):
                    if bp.before_tax is not None:
                        before_val = bp.before_tax
                    if bp.after_tax is not None:
                        after_val = bp.after_tax
                else:
                    break

            if earliest_start and (year, month) >= earliest_start:
                if before_val is None:
                    before_val = first_before
                if after_val is None:
                    after_val = first_after

            utgifter = cost(age, cost_brackets)

            if earliest_start and (year, month) < earliest_start:
                overskott_man = 0
            else:
                eff_after = after_val if after_val is not None else 0
                overskott_man = eff_after - utgifter

            result.monthly.append(MonthlyRow(
                year=year, month=month, age=age,
                before_tax=before_val, after_tax=after_val,
                utgifter=utgifter, overskott_man=overskott_man,
            ))

    # Pass 2: årssummering + sätt overskott_ar på decemberraderna.
    monthly_rate = (growth_pct / 100.0) / 12.0 if growth_pct > 0 else 0.0
    use_monthly = growth_mode == 'monthly'
    acc1 = 0.0
    acc2 = 0.0
    acc3 = 0.0
    kvar1 = float(sparande)
    kvar2 = float(sparande)
    kvar3 = float(sparande)
    for year in range(start_year, end_year + 1):
        year_rows = [m for m in result.monthly if m.year == year]
        year_sum = sum(m.overskott_man for m in year_rows)
        for m in year_rows:
            if m.month == 12:
                m.overskott_ar = year_sum

        age_for_year = (year - birth_year) if birth_year else (year - FALLBACK_BIRTH_YEAR)
        utgifter_man = cost(age_for_year, cost_brackets)

        if use_monthly:
            # Månadsvis: ränta + diff per månad, regler per månad
            for m in year_rows:
                if monthly_rate > 0:
                    kvar1 *= (1.0 + monthly_rate)
                    kvar2 *= (1.0 + monthly_rate)
                    kvar3 *= (1.0 + monthly_rate)
                diff = m.overskott_man
                acc1 += diff
                kvar1 += diff
                if diff < 0:
                    acc2 += diff
                    acc3 += diff
                    kvar2 += diff
                    kvar3 += diff
                else:
                    acc3 += diff / 2
                    kvar3 += diff / 2
        else:
            # Årsvis: ränta månadsvis, diff vid årets slut, regler per år
            if monthly_rate > 0 and year_rows:
                factor = (1.0 + monthly_rate) ** len(year_rows)
                kvar1 *= factor
                kvar2 *= factor
                kvar3 *= factor
            acc1 += year_sum
            kvar1 += year_sum
            if year_sum < 0:
                acc2 += year_sum
                acc3 += year_sum
                kvar2 += year_sum
                kvar3 += year_sum
            else:
                acc3 += year_sum / 2
                kvar3 += year_sum / 2

        result.yearly.append(YearlyRow(
            year=year, age=age_for_year, overskott_ar=year_sum,
            utgifter_man=utgifter_man,
            acc_1=acc1, kvar_1=kvar1,
            acc_2=acc2, kvar_2=kvar2,
            acc_3=acc3, kvar_3=kvar3,
        ))

    return result


# ---------------------------------------------------------------------------
# Greedy-optimering av (start, längd) per försäkring
# ---------------------------------------------------------------------------

# Namn-fragment som indikerar "allmän pension" (Inkomst- + Premiepension).
# Dessa har en tidigast-tillåten startpunkt (3 år före riktålder).
_ALLMAN_PENSION_HINTS = ('inkomstpension', 'premiepension', 'tilläggspension')


def _is_allman_pension(name: str) -> bool:
    n = (name or '').lower()
    return any(h in n for h in _ALLMAN_PENSION_HINTS)


def _riktalder_for_birth(birth_year):
    """Riktålder för svenska pensionssystemet baserat på födelseår.
    Baserat på 2026-reformen; framtida värden är prognoser."""
    if birth_year is None:
        return None
    if birth_year <= 1958:
        return 65
    if birth_year <= 1960:
        return 66
    if birth_year <= 1966:
        return 67
    if birth_year <= 1980:
        return 68
    return 69


def _earliest_allman_from_birth(birth_year, birth_month):
    """Tidigast tillåtet uttag av Inkomst-/Premiepension = riktålder - 3 år.
    Räknat från personens födelsemånad."""
    r = _riktalder_for_birth(birth_year)
    if r is None:
        return None
    tidigast_age = r - 3
    yr = birth_year + tidigast_age
    return (yr, birth_month or 1)


def _dur_to_months(dur):
    """(år, mån) -> antal månader. None => None (Livet ut)."""
    if dur is None:
        return None
    y, m = dur
    return int(y) * 12 + int(m)


def _months_to_dur(m):
    if m is None:
        return None
    return (m // 12, m % 12)


def _add_months(date, months):
    y, mo = date
    total = (y * 12 + (mo - 1)) + months
    return (total // 12, total % 12 + 1)


def _date_le(a, b):
    return a <= b


def _active_at(date, start, dur):
    """Är försäkringen aktiv vid `date` givet start + dur (None = livet ut)?"""
    if start is None:
        return False
    if date < start:
        return False
    if dur is None:
        return True
    end = _add_months(start, _dur_to_months(dur))
    return date < end


def _approx_capital_kr_months(ins):
    """Approximera försäkringens 'kapital' som kr·månader baserat på
    original amounts_before_tax. Används för att skala om beloppet vid ny dur.

    Returnerar (kapital_kr_manader, snitt_manadsbelopp).
    """
    if not ins.amounts_before_tax:
        return 0, 0
    positive = [a for a in ins.amounts_before_tax if a and a > 0]
    if not positive:
        return 0, 0
    avg_monthly = sum(positive) / len(positive)
    dur_m = _dur_to_months(ins.duration)
    if dur_m and dur_m > 0:
        return avg_monthly * dur_m, avg_monthly
    # Livet ut: uppskatta baserat på ~25 år (89 - 64 typiskt)
    return avg_monthly * 25 * 12, avg_monthly


def _monthly_from_new_config(orig_ins, new_start, new_dur, growth_pct=0.0):
    """Beräkna nytt konstant månadsbelopp för orig_ins vid ny (start, dur).

    Kapital-bevarande + tillväxts-medveten: om vi skjuter starten framåt
    ligger kapitalet kvar och växer med growth_pct % per år. Nytt månadsbelopp
    = kapital × (1+g)^delay_år / nya_månader.

    Livet ut: skala snitt-månadsbelopp med (1+g)^delay_år (utan dur-effekt).
    """
    kapital, avg = _approx_capital_kr_months(orig_ins)
    # Tillväxts-faktor mellan original-start och ny start.
    g = growth_pct / 100.0 if growth_pct else 0.0
    delay_months = 0
    if orig_ins.start is not None and new_start is not None:
        delay_months = ((new_start[0] - orig_ins.start[0]) * 12
                        + (new_start[1] - orig_ins.start[1]))
    growth_factor = (1.0 + g) ** (delay_months / 12.0) if g else 1.0

    if new_dur is None:
        # Livet ut: skala snitt-beloppet med tillväxten
        return avg * growth_factor
    new_m = _dur_to_months(new_dur)
    if not new_m or new_m <= 0:
        return 0
    if orig_ins.duration is None:
        # Original var Livet ut, förkortas till fast dur → behåll snitt-nivå
        # och skala med tillväxten (approximerar större avsluts-boost)
        return avg * growth_factor
    return kapital * growth_factor / new_m


# Bakåtkompatibel wrapper — används inte längre men behållen ifall något
# externt anropar den.
def _monthly_from_new_dur(orig_ins, new_dur):
    return _monthly_from_new_config(orig_ins, orig_ins.start, new_dur, 0.0)


def _parsed_plan_to_dict(p):
    """Serialisera en ParsedPlan (för sparande av SIM-planer i state)."""
    return {
        'plan_name': p.plan_name,
        'birth_year': p.birth_year,
        'birth_month': p.birth_month,
        'earliest_start': list(p.earliest_start) if p.earliest_start else None,
        'insurances': [
            {
                'name': ins.name,
                'start': list(ins.start) if ins.start else None,
                'duration': list(ins.duration) if ins.duration else None,
                'amounts_before_tax': list(ins.amounts_before_tax),
            }
            for ins in p.insurances
        ],
        'breakpoints': [
            {
                'date': list(bp.date),
                'before_tax': bp.before_tax,
                'after_tax': bp.after_tax,
            }
            for bp in p.breakpoints
        ],
    }


def _dict_to_parsed_plan(d):
    """Deserialisera en ParsedPlan från dict (state-load för SIM-planer)."""
    return ParsedPlan(
        plan_name=d.get('plan_name', ''),
        birth_year=d.get('birth_year'),
        birth_month=d.get('birth_month'),
        earliest_start=tuple(d['earliest_start']) if d.get('earliest_start') else None,
        insurances=[
            Insurance(
                name=i.get('name', ''),
                start=tuple(i['start']) if i.get('start') else None,
                duration=tuple(i['duration']) if i.get('duration') else None,
                amounts_before_tax=list(i.get('amounts_before_tax') or []),
            )
            for i in d.get('insurances', [])
        ],
        breakpoints=[
            Breakpoint(
                date=tuple(b['date']),
                before_tax=b.get('before_tax'),
                after_tax=b.get('after_tax'),
            )
            for b in d.get('breakpoints', [])
        ],
    )


def _tax_rate_for_age(age, age_tax_brackets):
    """Returnerar skattesats (0-1) för given ålder från åldersbrackets.
    Brackets format: [(upp_till_ålder, pct), …], sorterade stigande.
    Sista bracketen fångar alla högre åldrar."""
    if not age_tax_brackets:
        return None
    for upper, pct in age_tax_brackets:
        if age <= upper:
            return pct / 100.0
    return age_tax_brackets[-1][1] / 100.0


# Visas i huvudfönstret i stället för plan-flikarna när ingen plan är inläst
NO_PLAN_TEXT = (
    'Ingen plan är inläst.\n\n'
    'Öppna en grupp redan sparade planer med Arkiv → Öppna...\n\n'
    'Alternativt hämtar du planer från minpension.se med\n'
    'Verktyg → Hämta från minpension.se... eller '
    'knappen "Hämta plan" uppe till höger.\n'
    'Du kan också klistra in en kopierad plan med\n'
    'Verktyg → Klistra in kopierad text från minpension.se...'
)


# Default åldersbaserade skattesatser för pension (grov approximation)
# Före riktålder + 1: normal pensions-skatt.
# Från år man fyller 66 (grundavdrag börjar höjas).
# Från 1 jan året efter man fyller 67 (dvs 68 hela året): förhöjt grundavdrag → mycket lägre skatt.
DEFAULT_AGE_TAX_BRACKETS = [(65, 30), (67, 20), (99, 10)]


def _synth_optimized_plan(configs, template_parsed,
                          age_tax_brackets=None):
    """Bygg ParsedPlan från configs = list av dict{name, orig_ins, start, dur, monthly}.

    Skattefaktor tas från åldersbaserade brackets om age_tax_brackets ges,
    annars från template_parsen (fallback för bakåtkompat).
    """
    # Samla alla bp-datum: alla starts + alla ends + templates bp-datum
    all_dates = set()
    for c in configs:
        if c['start'] is None:
            continue
        all_dates.add(c['start'])
        if c['dur'] is not None:
            end = _add_months(c['start'], _dur_to_months(c['dur']))
            all_dates.add(end)
    for bp in template_parsed.breakpoints:
        all_dates.add(bp.date)
    all_dates = sorted(all_dates)

    birth_y = template_parsed.birth_year
    birth_m = template_parsed.birth_month or 1

    def _age_at(date):
        y, m = date
        if not birth_y:
            return None
        age = y - birth_y
        if m < birth_m:
            age -= 1
        return age

    # Backward-fill skattesats från template (fallback när ingen ålder-modell)
    tmpl_bps = template_parsed.breakpoints
    tmpl_rates = [None] * len(tmpl_bps)
    for i, bp in enumerate(tmpl_bps):
        if bp.before_tax and bp.after_tax is not None and bp.before_tax > 0:
            tmpl_rates[i] = 1.0 - (bp.after_tax / bp.before_tax)
    last_known = None
    for i in reversed(range(len(tmpl_bps))):
        if tmpl_rates[i] is not None:
            last_known = tmpl_rates[i]
        elif last_known is not None:
            tmpl_rates[i] = last_known

    def _tmpl_tax_rate_at(date):
        idx = None
        for i, bp in enumerate(tmpl_bps):
            if bp.date <= date:
                idx = i
            else:
                break
        if idx is None:
            return None
        return tmpl_rates[idx]

    def _tax_rate_at(date):
        if age_tax_brackets is not None:
            age = _age_at(date)
            if age is not None:
                return _tax_rate_for_age(age, age_tax_brackets)
        return _tmpl_tax_rate_at(date)

    def _orig_amount_at(ins, date):
        """Hämta ins.amounts_before_tax vid närmaste tidigare bp i template.
        Returnerar 0 om ins inte har amounts eller om vi är före första bp."""
        if not ins.amounts_before_tax:
            return 0
        idx = None
        for i, bp in enumerate(tmpl_bps):
            if bp.date <= date:
                idx = i
            else:
                break
        if idx is None or idx >= len(ins.amounts_before_tax):
            return 0
        return ins.amounts_before_tax[idx] or 0

    new_bps = []
    for d in all_dates:
        total = 0.0
        for c in configs:
            if not _active_at(d, c['start'], c['dur']):
                continue
            # Oförändrade försäkringar: använd originalets faktiska belopp
            # vid detta datum (som varierar per bp), inte konstant snitt
            if c.get('use_original_amounts'):
                total += _orig_amount_at(c['orig_ins'], d)
            else:
                total += c['monthly']
        total_int = int(round(total))
        rate = _tax_rate_at(d)
        if rate is not None and total_int > 0:
            after = int(round(total_int * (1 - rate)))
        else:
            after = None
        new_bps.append(Breakpoint(date=d, before_tax=total_int, after_tax=after))

    # Insurances i syntetisk plan: kopior med nya start/dur och amounts per bp
    new_ins = []
    for c in configs:
        arr = []
        for d in all_dates:
            if not _active_at(d, c['start'], c['dur']):
                arr.append(0)
                continue
            if c.get('use_original_amounts'):
                arr.append(int(_orig_amount_at(c['orig_ins'], d)))
            else:
                arr.append(int(round(c['monthly'])))
        new_ins.append(Insurance(
            name=c['name'], start=c['start'], duration=c['dur'],
            amounts_before_tax=arr,
        ))

    earliest = min((c['start'] for c in configs if c['start']), default=None)
    return ParsedPlan(
        plan_name=f'FÖRSLAG ({template_parsed.plan_name})',
        insurances=new_ins,
        breakpoints=new_bps,
        birth_year=template_parsed.birth_year,
        birth_month=template_parsed.birth_month,
        earliest_start=earliest or template_parsed.earliest_start,
    )


def _score_plan(synth, sparande, brackets, end_year, growth_pct, growth_mode,
                kvar_idx=1, metric_type='end', min_kvar_floor=None,
                balance=0.5):
    """Räkna slutvärde/min/total för synth-planens Kvar #(kvar_idx+1).
    Högre = bättre.

    metric_type:
      'end'      → slutvärde
      'min'      → lägsta värde över perioden
      'total'    → summa alla år
      'match'    → -Σ(underskott/1000)² + liten vikt på slutvärde
      'balanced' → (1-b) × match-komponent + b × slutvärde/100
                   där b = balance (0.0-1.0). 0.0 = pure match,
                   1.0 = pure slutvärde.

    Om min_kvar_floor är satt och något år går under det → -inf."""
    calc = calculate_plan(
        synth, sparande, brackets, end_year, growth_pct, growth_mode,
    )
    if not calc.yearly:
        return float('-inf')
    attr = f'kvar_{kvar_idx + 1}'
    values = [getattr(y, attr) for y in calc.yearly]
    if min_kvar_floor is not None and min(values) < min_kvar_floor:
        return float('-inf')
    if metric_type == 'end':
        return values[-1]
    if metric_type == 'min':
        return min(values)
    if metric_type == 'total':
        return sum(values)
    if metric_type in ('match', 'balanced'):
        deficit_sum_sq = 0.0
        for m in calc.monthly:
            if m.overskott_man is not None and m.overskott_man < 0:
                d = -m.overskott_man / 1000.0
                deficit_sum_sq += d * d
        match_comp = -deficit_sum_sq
        end_val = values[-1] if values else 0
        end_comp = end_val / 100.0
        if metric_type == 'match':
            # Pure match med minimal tie-breaker på slutvärde
            return match_comp + end_val / 1e6
        b = max(0.0, min(1.0, balance))
        return (1.0 - b) * match_comp + b * end_comp
    return values[-1]


def _generate_options(entry, allman_min_start, start_options,
                      dur_options_fixed, end_year=None):
    """Ge lista av (start, dur)-alternativ för en försäkring. Om end_year
    ges filtreras durations där utbetalningen skulle sluta EFTER end_year
    (icke-livsvariga försäkringar) — pengar som betalas ut efter end_year
    räknas ändå inte i optimeringens score."""
    is_allman = _is_allman_pension(entry['name'])
    is_livet_ut = entry['orig_ins'].duration is None
    # Bygg lista av tillåtna start-datum; för allmän pension, lägg alltid
    # till gränsen själv så vi kan starta exakt på den. Inkludera också
    # försäkringens originalstart så den ALLTID är valbar (även om den
    # ligger utanför standard-rutnätet, t.ex. 2026-12).
    starts = []
    for s in start_options:
        if is_allman and allman_min_start and s < allman_min_start:
            continue
        starts.append(s)
    if is_allman and allman_min_start and allman_min_start not in starts:
        starts.append(allman_min_start)
    orig_start = entry['orig_ins'].start
    if orig_start is not None:
        if not (is_allman and allman_min_start and orig_start < allman_min_start):
            if orig_start not in starts:
                starts.append(orig_start)
    starts = sorted(set(starts))
    opts = []
    for s in starts:
        if is_livet_ut:
            opts.append((s, None))
        else:
            for d in dur_options_fixed:
                # Filtrera durations som skulle sträcka utbetalning efter end_year
                if end_year is not None:
                    end_date = _add_months(s, _dur_to_months(d))
                    # end_date är första månad UTAN utbetalning; senaste
                    # utbetalning är månaden före. Vi vill att senaste
                    # utbetalning ligger senast dec end_year.
                    last_pay = _add_months(end_date, -1)
                    if last_pay[0] > end_year:
                        continue
                opts.append((s, d))
    # Om alla durations filtrerades bort (start är för sent för att rymma
    # kortaste dur), returnera åtminstone kortaste dur för att inte tappa
    # försäkringen helt.
    if not opts and not is_livet_ut and starts and dur_options_fixed:
        opts.append((starts[0], dur_options_fixed[0]))
    return opts


def optimize_plan_greedy(
    base_parsed,
    sparande,
    cost_brackets,
    end_year=None,
    growth_pct=0.0,
    growth_mode='monthly',
    allman_min_start=None,
    start_options=None,
    dur_options_fixed=None,
    kvar_idx=1,
    metric_type='end',
    min_kvar_floor=None,
    age_tax_brackets=None,
    locked_names=None,
    balance=0.5,
    max_passes=8,
    progress_cb=None,
):
    """Föreslå (start, längd) per försäkring greedy för att maximera metric.

    Returnerar (synth_parsed, final_configs, best_score, history).
    """
    if start_options is None:
        # Kvartalsvis 2027-01 till 2032-10 = 24 alternativ
        start_options = []
        for y in range(2027, 2033):
            for m in (1, 4, 7, 10):
                start_options.append((y, m))
    if dur_options_fixed is None:
        dur_options_fixed = [(5, 0), (8, 0), (10, 0), (12, 0), (15, 0), (20, 0)]

    # Bygg initial config från base_parsed
    _locked_init = set(locked_names or ())
    configs = []
    for ins in base_parsed.insurances:
        kapital, avg = _approx_capital_kr_months(ins)
        start = ins.start
        # Tvinga allmän pension att respektera constraint direkt från början —
        # ett minpension-flöde kan innehålla en tidigare start än vad som
        # egentligen är tillåtet. Låsta försäkringar behålls som originalet
        # (om användaren låst en olaglig start är det deras beslut).
        if (allman_min_start is not None and _is_allman_pension(ins.name)
                and start is not None and start < allman_min_start
                and ins.name not in _locked_init):
            start = allman_min_start
        configs.append({
            'name': ins.name,
            'orig_ins': ins,
            'start': start,
            'dur': ins.duration,
            'monthly': avg,
        })

    def make_score(kvar, mtype, floor):
        def _s(cfgs):
            synth = _synth_optimized_plan(cfgs, base_parsed, age_tax_brackets)
            return _score_plan(
                synth, sparande, cost_brackets, end_year,
                growth_pct, growth_mode, kvar, mtype, floor, balance,
            )
        return _s

    total_evals = [0]
    cancelled = [False]

    locked = set(locked_names or ())

    def greedy_loop(configs, score_fn, phase_label=''):
        best_score = score_fn(configs)
        history = [(0, None, None, None, best_score)]
        for pass_idx in range(max_passes):
            changed = False
            for ci, entry in enumerate(configs):
                if entry['name'] in locked:
                    continue
                opts = _generate_options(
                    entry, allman_min_start, start_options,
                    dur_options_fixed, end_year,
                )
                best_local = best_score
                best_choice = None
                for (new_start, new_dur) in opts:
                    if (new_start, new_dur) == (entry['start'], entry['dur']):
                        continue
                    trial_entry = dict(entry)
                    trial_entry['start'] = new_start
                    trial_entry['dur'] = new_dur
                    trial_entry['monthly'] = _monthly_from_new_config(
                        entry['orig_ins'], new_start, new_dur, growth_pct,
                    )
                    trial = list(configs)
                    trial[ci] = trial_entry
                    s = score_fn(trial)
                    total_evals[0] += 1
                    if progress_cb is not None and total_evals[0] % 50 == 0:
                        label = f'{phase_label}{entry["name"]}'
                        cont = progress_cb(
                            pass_idx + 1, ci, label,
                            total_evals[0], best_score,
                        )
                        if cont is False:
                            cancelled[0] = True
                            return configs, best_score, history
                    if s > best_local:
                        best_local = s
                        best_choice = (new_start, new_dur, trial_entry['monthly'])
                if best_choice is not None:
                    new_start, new_dur, new_monthly = best_choice
                    configs[ci] = dict(entry)
                    configs[ci]['start'] = new_start
                    configs[ci]['dur'] = new_dur
                    configs[ci]['monthly'] = new_monthly
                    best_score = best_local
                    history.append((pass_idx + 1, entry['name'],
                                    new_start, new_dur, best_score))
                    changed = True
            if not changed:
                break
        return configs, best_score, history

    # Pilot-pass: om vi har ett golv, kör först "maximera min utan golv" så
    # att vi hamnar i en bra startposition innan vi applicerar golv+metric.
    if min_kvar_floor is not None:
        pilot_score = make_score(kvar_idx, 'min', None)
        configs, pilot_best, _ = greedy_loop(
            configs, pilot_score, phase_label='[pilot] ',
        )
        if cancelled[0]:
            synth = _synth_optimized_plan(configs, base_parsed, age_tax_brackets)
            return synth, configs, pilot_best, []

    # Slutlig pass med användarens val + eventuellt golv
    final_score = make_score(kvar_idx, metric_type, min_kvar_floor)
    configs, best_score, history = greedy_loop(
        configs, final_score, phase_label='',
    )

    # Om golvet fortfarande inte kan uppfyllas, rapportera pilot-bäst istället
    # så användaren får se hur nära vi kommer.
    if best_score == float('-inf') and min_kvar_floor is not None:
        raw_score = make_score(kvar_idx, metric_type, None)(configs)
        best_score = raw_score

    synth = _synth_optimized_plan(configs, base_parsed, age_tax_brackets)
    return synth, configs, best_score, history


# ---------------------------------------------------------------------------
# Formatering
# ---------------------------------------------------------------------------

def fmt_kr(x) -> str:
    """Formatera tal med mellanslag som tusenavdelare, inga decimaler."""
    if x is None or x == '':
        return ''
    try:
        return f"{round(float(x)):,}".replace(',', '\xa0')
    except (ValueError, TypeError):
        return str(x)


# ---------------------------------------------------------------------------
# PDF-export
# ---------------------------------------------------------------------------

def export_to_pdf(
    filename: str,
    plan_name: str,
    sparande: float,
    cost_brackets: list,
    parsed: ParsedPlan,
    calc: CalculatedPlan,
    fig: Figure,
):
    pdf_title = f"Plan {plan_name}" if plan_name else "Pensionsplan"
    doc = SimpleDocTemplate(
        filename,
        pagesize=landscape(A4),
        leftMargin=1.5*cm, rightMargin=1.5*cm,
        topMargin=1.5*cm, bottomMargin=1.5*cm,
        title=pdf_title,
    )
    styles = getSampleStyleSheet()
    story = []

    story.append(Paragraph(f"<b>{pdf_title}</b>", styles['Title']))
    story.append(Paragraph(f"Skapad: {date.today().isoformat()}", styles['Normal']))
    story.append(Paragraph(f"Sparande: {fmt_kr(sparande)} kr", styles['Normal']))
    story.append(Spacer(1, 0.4*cm))

    # Kostnadsbrytpunkter och försäkringar sida vid sida
    cost_data = [['Upp till ålder', 'Utgift/mån']]
    for age, c in cost_brackets:
        cost_data.append([str(age), fmt_kr(c)])
    cost_table = Table(cost_data, colWidths=[3*cm, 3*cm])
    cost_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('ALIGN', (1, 1), (-1, -1), 'RIGHT'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
    ]))

    ins_data = [['Försäkring', 'Start', 'Längd']]
    for ins in parsed.insurances:
        start = f"{ins.start[0]}-{ins.start[1]:02d}" if ins.start else ''
        dur = f"{ins.duration[0]} år, {ins.duration[1]} mån" if ins.duration else 'Livsvarigt'
        ins_data.append([ins.name, start, dur])
    ins_table = Table(ins_data, colWidths=[9*cm, 3*cm, 4*cm])
    ins_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
    ]))

    layout = Table([[cost_table, ins_table]], colWidths=[7*cm, 17*cm])
    layout.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP')]))
    story.append(layout)
    story.append(PageBreak())

    # Diagram på egen sida — fyller höjden
    # Rita tillfälligt om figuren med högre aspect-ratio för PDF (utan att påverka GUI)
    buf = io.BytesIO()
    orig_size = fig.get_size_inches()
    try:
        fig.set_size_inches(10, 6.5)
        fig.savefig(buf, format='png', dpi=150, bbox_inches='tight')
    finally:
        fig.set_size_inches(orig_size)
    buf.seek(0)
    story.append(Image(buf, width=26*cm, height=17*cm))
    story.append(PageBreak())

    # Årlig sammanfattning
    story.append(Paragraph("<b>Årlig sammanfattning</b>", styles['Heading2']))
    story.append(Spacer(1, 0.2*cm))
    # Kolumnbeskrivning som hjälp att tolka tabellen
    story.append(Paragraph("<b>Kolumnbeskrivning</b>", styles['Heading4']))
    desc_data = [[term, desc] for term, desc in COLUMN_DESCRIPTIONS]
    desc_table = Table(desc_data, colWidths=[3*cm, 12*cm])
    desc_table.setStyle(TableStyle([
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 1),
    ]))
    story.append(desc_table)
    story.append(Spacer(1, 0.3*cm))
    yr_data = [['År', 'Ålder', 'Diff/år',
                'Ack diff #1', KVAR_LABELS[0],
                'Ack diff #2', KVAR_LABELS[1],
                'Ack diff #3', KVAR_LABELS[2]]]
    for y in calc.yearly:
        yr_data.append([
            str(y.year), str(y.age),
            fmt_kr(y.overskott_ar),
            fmt_kr(y.acc_1), fmt_kr(y.kvar_1),
            fmt_kr(y.acc_2), fmt_kr(y.kvar_2),
            fmt_kr(y.acc_3), fmt_kr(y.kvar_3),
        ])
    yr_table = Table(yr_data, repeatRows=1, colWidths=[1.5*cm, 1.5*cm] + [2.5*cm]*7)
    yr_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
        ('GRID', (0, 0), (-1, -1), 0.3, colors.grey),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('ALIGN', (2, 1), (-1, -1), 'RIGHT'),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
    ]))
    story.append(yr_table)

    doc.build(story)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

DEFAULT_BRACKETS = [(63, 24000), (70, 28000), (75, 26000), (80, 23000), (85, 21500), (100, 21500)]
NUM_COST_SETS = 5

# Färger för Kvar-graferna i diagram + tooltip (matplotlib tab10 defaults)
KVAR_COLORS = ['#1f77b4', '#ff7f0e', '#2ca02c']

# Etiketter för de tre Kvar-graferna. Index 0=#1, 1=#2, 2=#3. Speglar
# antagandet om hur mycket av årets överskott som sparas (100% / 0% / 50%).
KVAR_LABELS = ('Spara allt överskott', 'Spara inget', 'Spara hälften av överskottet')

# Hjälp-text som visas i paste-rutan när den är tom
PASTE_PLACEHOLDER = (
    "1. Logga in på minpension.se\n"
    "2. Öppna en uttagsplan\n"
    "3. Markera allt (Ctrl+A) och kopiera (Ctrl+C)\n"
    "4. Klistra in här (Ctrl+V eller höger klick + Klistra in)"
)

# Kolumnbeskrivning — används både i GUI:t och i PDF-exporten
COLUMN_DESCRIPTIONS = [
    ("Diff/mån:", "Efter skatt minus Utgifter (per månad)"),
    ("Diff/år:", "Summa av månadens Diff för året"),
    ("Ack diff #1:", "Bästa fallet: 100% av överskottet sparas"),
    ("Ack diff #2:", "Sämsta fallet: 0% av överskottet sparas"),
    ("Ack diff #3:", "Realistiskt: 50% av överskottet sparas"),
    (f"{KVAR_LABELS[0]}:", "Sparande + Ack diff #1 (bästa fall)"),
    (f"{KVAR_LABELS[1]}:", "Sparande + Ack diff #2 (sämsta fall)"),
    (f"{KVAR_LABELS[2]}:", "Sparande + Ack diff #3 (mellan)"),
]

# Färgpalett för försäkringar (matplotlib tab20 — 20 distinkta färger,
# räcker till de flesta planer utan att färger upprepas)
INSURANCE_COLORS = [
    '#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd',
    '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf',
    '#aec7e8', '#ffbb78', '#98df8a', '#ff9896', '#c5b0d5',
    '#c49c94', '#f7b6d2', '#c7c7c7', '#dbdb8d', '#9edae5',
]


def _make_dot_image(color, size=14):
    """Skapa en liten färgad cirkel som tkinter-bild."""
    from PIL import Image, ImageDraw, ImageTk
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((1, 1, size - 2, size - 2), fill=color)
    return ImageTk.PhotoImage(img)


def _monitor_bounds_at(px: int, py: int):
    """Returnerar (left, top, right, bottom) för monitorn som innehåller (px, py).

    Faller tillbaka till primära skärmen om Windows-API:et inte är tillgängligt
    eller om punkten inte matchar någon monitor.
    """
    if sys.platform == 'win32':
        for rect in _get_windows_monitors().values():
            l, t, r, b = rect
            if l <= px < r and t <= py < b:
                return rect
    return None


def center_dialog_on_parent(dialog, parent):
    """Centrera en modal dialog över parent-fönstret och kläm mot rätt monitor.

    Robust mot multi-monitor: hittar monitorn som parent-fönstret ligger på
    och klämmer dialogen mot den, så dialogen aldrig hamnar off-screen och
    dyker alltid upp på samma skärm som huvudfönstret.
    """
    parent.update_idletasks()
    dialog.update_idletasks()
    dw = max(dialog.winfo_reqwidth(), dialog.winfo_width())
    dh = max(dialog.winfo_reqheight(), dialog.winfo_height())
    px = parent.winfo_rootx()
    py = parent.winfo_rooty()
    pw = parent.winfo_width()
    ph = parent.winfo_height()

    # Rå centrering på parent
    x = px + (pw - dw) // 2
    y = py + (ph - dh) // 2

    # Hitta monitorn parent ligger på (mitten av parent för att undvika kanter)
    center_x = px + pw // 2
    center_y = py + ph // 2
    bounds = _monitor_bounds_at(center_x, center_y)
    if bounds is None:
        # Fallback: primära skärmen
        left, top = 0, 0
        right = dialog.winfo_screenwidth()
        bottom = dialog.winfo_screenheight()
    else:
        left, top, right, bottom = bounds

    x = max(left, min(x, right - dw))
    y = max(top, min(y, bottom - dh))
    dialog.geometry(f'+{x}+{y}')


class _Tooltip:
    """Enkel hover-tooltip för en tk-widget. text_func() anropas vid hover."""

    def __init__(self, widget, text_func):
        self.widget = widget
        self.text_func = text_func
        self.tipwindow = None
        widget.bind('<Enter>', self._show, add='+')
        widget.bind('<Leave>', self._hide, add='+')
        widget.bind('<FocusOut>', self._hide, add='+')

    def _show(self, event=None):
        text = self.text_func()
        if not text or self.tipwindow:
            return
        x = self.widget.winfo_rootx() + self.widget.winfo_width() + 6
        y = self.widget.winfo_rooty()
        tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tk.Label(
            tw, text=text,
            background='#ffffe0', relief='solid', borderwidth=1,
            font=('TkDefaultFont', 9), padx=6, pady=3,
        ).pack()
        self.tipwindow = tw

    def _hide(self, event=None):
        if self.tipwindow:
            self.tipwindow.destroy()
            self.tipwindow = None


def _insurance_active_in_year(ins, year: int) -> bool:
    """Returnerar True om försäkringen betalar ut någon gång under det givna året."""
    if not ins.start:
        return False
    sy = ins.start[0]
    if year < sy:
        return False
    if ins.duration is None:
        return True  # Livsvarigt
    sm = ins.start[1]
    dy, dm = ins.duration
    last_idx = sy * 12 + sm + dy * 12 + dm - 1
    last_year = (last_idx - 1) // 12
    return year <= last_year


def _insurance_active_in_month(ins, year: int, month: int) -> bool:
    """Är försäkringen aktiv under given (år, månad)?"""
    if not ins.start:
        return False
    sy, sm = ins.start
    first_idx = sy * 12 + sm
    if ins.duration is None:
        last_idx = 10**9
    else:
        dy, dm = ins.duration
        last_idx = first_idx + dy * 12 + dm - 1
    m_idx = year * 12 + month
    return first_idx <= m_idx <= last_idx


def _insurance_months_in_year(ins, year: int) -> int:
    """Antal månader (0-12) som försäkringen är aktiv under det givna året."""
    if not ins.start:
        return 0
    sy, sm = ins.start
    first_idx = sy * 12 + sm
    if ins.duration is None:
        last_idx = (year + 1) * 12  # +1 month past year-end → garanterar 12 om aktiv
    else:
        dy, dm = ins.duration
        last_idx = first_idx + dy * 12 + dm - 1
    year_first = year * 12 + 1
    year_last = year * 12 + 12
    active_first = max(first_idx, year_first)
    active_last = min(last_idx, year_last)
    return max(0, active_last - active_first + 1)


def _yearly_per_insurance_amounts(parsed, year: int):
    """Beräkna månadsbelopp (före och efter skatt) per försäkring för ett år.

    Returnerar lista av dictar:
      {'idx', 'name', 'months', 'before_mo', 'after_mo',
       'before_mo_max', 'after_mo_max', 'varies'}
    för försäkringar med per-brytpunkt-data (`amounts_before_tax`) som är
    aktiva minst en månad under året. Tomt om planen saknar data.

    before_mo/after_mo är det lägsta månadsbeloppet under året; *_mo_max är
    det högsta. 'varies' är True om beloppet ändras under året (då skiljer
    min och max). Skattesats backward-fylls från nästa kända brytpunkt
    (samma logik som i Inkomst per försäkring-vyn).
    """
    if not parsed or not parsed.insurances or not parsed.breakpoints:
        return []
    bps = parsed.breakpoints
    n = len(bps)

    tax_rates = [None] * n
    for i, bp in enumerate(bps):
        if bp.before_tax and bp.after_tax is not None and bp.before_tax > 0:
            tax_rates[i] = 1.0 - (bp.after_tax / bp.before_tax)
    last_known = None
    for i in reversed(range(n)):
        if tax_rates[i] is not None:
            last_known = tax_rates[i]
        elif last_known is not None:
            tax_rates[i] = last_known
    net_mult = [
        (1.0 - r) if r is not None else None for r in tax_rates
    ]

    bp_month_idx = [
        (bp.date[0] * 12 + bp.date[1]) if bp.date else None for bp in bps
    ]

    results = []
    for idx, ins in enumerate(parsed.insurances):
        if not ins.amounts_before_tax or not ins.start:
            continue
        sy, sm = ins.start
        first_idx = sy * 12 + sm
        if ins.duration is None:
            last_idx = 10**9
        else:
            dy, dm = ins.duration
            last_idx = first_idx + dy * 12 + dm - 1

        monthly_before = []  # list of (before_amt, after_amt) per active month
        monthly_after = []
        amts = ins.amounts_before_tax
        for m in range(1, 13):
            month_idx = year * 12 + m
            if month_idx < first_idx or month_idx > last_idx:
                continue
            bp_idx = None
            for i, bmi in enumerate(bp_month_idx):
                if bmi is not None and bmi <= month_idx:
                    bp_idx = i
                else:
                    break
            if bp_idx is None or bp_idx >= len(amts):
                continue
            amt = amts[bp_idx]
            if amt is None:
                continue
            monthly_before.append(amt)
            mult = net_mult[bp_idx]
            monthly_after.append(round(amt * mult) if mult is not None else amt)
        if not monthly_before:
            continue
        b_min, b_max = min(monthly_before), max(monthly_before)
        a_min, a_max = min(monthly_after), max(monthly_after)
        results.append({
            'idx': idx, 'name': ins.name, 'months': len(monthly_before),
            'before_mo': b_min, 'after_mo': a_min,
            'before_mo_max': b_max, 'after_mo_max': a_max,
            'varies': (b_min != b_max),
        })
    return results


def _monthly_per_insurance_amounts(parsed, year: int, month: int):
    """Som _yearly_per_insurance_amounts men för en enda månad.

    Returnerar samma struktur — `months` är alltid 1, min == max för
    månadsbeloppen (ingen variation inom en månad).
    """
    if not parsed or not parsed.insurances or not parsed.breakpoints:
        return []
    bps = parsed.breakpoints
    n = len(bps)

    tax_rates = [None] * n
    for i, bp in enumerate(bps):
        if bp.before_tax and bp.after_tax is not None and bp.before_tax > 0:
            tax_rates[i] = 1.0 - (bp.after_tax / bp.before_tax)
    last_known = None
    for i in reversed(range(n)):
        if tax_rates[i] is not None:
            last_known = tax_rates[i]
        elif last_known is not None:
            tax_rates[i] = last_known
    net_mult = [
        (1.0 - r) if r is not None else None for r in tax_rates
    ]

    bp_month_idx = [
        (bp.date[0] * 12 + bp.date[1]) if bp.date else None for bp in bps
    ]
    month_idx = year * 12 + month

    # Hitta brytpunkten som gäller för denna månad
    bp_idx = None
    for i, bmi in enumerate(bp_month_idx):
        if bmi is not None and bmi <= month_idx:
            bp_idx = i
        else:
            break
    if bp_idx is None:
        return []

    results = []
    for idx, ins in enumerate(parsed.insurances):
        if not ins.amounts_before_tax or not ins.start:
            continue
        if not _insurance_active_in_month(ins, year, month):
            continue
        amts = ins.amounts_before_tax
        if bp_idx >= len(amts):
            continue
        amt = amts[bp_idx]
        if amt is None:
            continue
        mult = net_mult[bp_idx]
        after_amt = round(amt * mult) if mult is not None else amt
        results.append({
            'idx': idx, 'name': ins.name, 'months': 1,
            'before_mo': amt, 'after_mo': after_amt,
            'before_mo_max': amt, 'after_mo_max': after_amt,
            'varies': False,
        })
    return results


def _monthly_insurance_changes(parsed, year: int, month: int):
    """Returnera lista av (namn, delta_kr) för försäkringar som ändras
    från föregående månad. Positiv delta = börjar/ökar, negativ = slutar/minskar.

    Skattereglering ingår inte — bara ändringar i before_tax-beloppet per
    försäkring (dvs verkliga försäkrings-händelser).
    """
    cur = _monthly_per_insurance_amounts(parsed, year, month)
    prev_year = year if month > 1 else year - 1
    prev_month = month - 1 if month > 1 else 12
    prev = _monthly_per_insurance_amounts(parsed, prev_year, prev_month)
    cur_by_idx = {r['idx']: r['before_mo'] for r in cur}
    prev_by_idx = {r['idx']: r['before_mo'] for r in prev}
    # Namn per idx (från cur eller prev — samma insurances-lista)
    name_by_idx = {r['idx']: r['name'] for r in cur}
    for r in prev:
        name_by_idx.setdefault(r['idx'], r['name'])
    all_idx = set(cur_by_idx) | set(prev_by_idx)
    changes = []
    for idx in all_idx:
        c = cur_by_idx.get(idx, 0)
        p = prev_by_idx.get(idx, 0)
        if c != p:
            changes.append((name_by_idx[idx], c - p))
    # Sortera på störst absolutbelopp först
    changes.sort(key=lambda x: -abs(x[1]))
    return changes


def _make_insurance_strip_image(color_states, dot_size=10, gap=2):
    """Skapa en horisontell PhotoImage med en cirkel/halvcirkel per försäkring.

    color_states: lista av (color, full) där full=True ger hel cirkel, False halv.
    Returnerar None om listan är tom.
    """
    if not color_states:
        return None
    from PIL import Image, ImageDraw, ImageTk
    n = len(color_states)
    width = n * dot_size + (n - 1) * gap
    height = dot_size
    img = Image.new('RGBA', (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    for i, (color, full) in enumerate(color_states):
        x = i * (dot_size + gap)
        bbox = (x + 1, 1, x + dot_size - 1, dot_size - 1)
        if full:
            draw.ellipse(bbox, fill=color)
        else:
            # Vänster halvcirkel (90°-270° i PIL:s koordinatsystem)
            draw.pieslice(bbox, start=90, end=270, fill=color)
    return ImageTk.PhotoImage(img)


def _auto_hide_scrollbar(scrollbar):
    """Returnera en xscrollcommand-callback som gömmer scrollbaren när allt får plats."""
    def cb(first, last):
        if float(first) <= 0.0 and float(last) >= 1.0:
            scrollbar.grid_remove()
        else:
            scrollbar.grid()
        scrollbar.set(first, last)
    return cb


def _open_with_default_app(path: str):
    """Öppna en fil med systemets standardprogram. Misslyckas tyst om det inte går."""
    try:
        if sys.platform == 'win32':
            os.startfile(path)  # noqa: PLW1514 (Windows-only)
        elif sys.platform == 'darwin':
            import subprocess
            subprocess.run(['open', path], check=False)
        else:
            import subprocess
            subprocess.run(['xdg-open', path], check=False)
    except Exception as e:
        messagebox.showwarning(
            "Kunde inte öppna fil",
            f"Filen sparades men kunde inte öppnas:\n{path}\n\n{e}",
        )


class PlanTab:
    """En plan-flik med UI och state (paste, försäkringar, tabeller, diagram, PDF)."""

    def __init__(self, app, notebook: ttk.Notebook):
        self.app = app
        self.notebook = notebook
        self.frame = ttk.Frame(notebook)
        notebook.add(self.frame, text='Ny plan')

        self.parsed: Optional[ParsedPlan] = None
        self.calc: Optional[CalculatedPlan] = None
        self.fetched_at: Optional[str] = None  # ISO datum-tid när planen hämtades via wizarden
        self._dot_images = []  # behåll referenser till PhotoImage så de inte tas bort
        self.hover_line = None  # vertikal hover-linje i diagrammet
        self.hover_text = None  # flytande tooltip
        self._parse_after_id = None  # id för debouncad on_parse
        self._chart_dirty = True   # diagrammet behöver ritas vid nästa render
        self._kvar_lines = []      # set av Line2D efter chart render
        self._placeholder_visible = False  # placeholder-text i paste-rutan
        self._table_tooltip = None       # Toplevel som visar försäkringar för hovered rad
        self._table_tooltip_row = None
        self._tooltip_dots = {}          # cache av PhotoImage per färg
        self._build_ui()
        self._show_paste_placeholder()

    def _update_tab_text(self, name: str):
        try:
            self.notebook.tab(self.frame, text=name or 'Ny plan')
        except tk.TclError:
            pass
        # Uppdatera även "Aktiv plan"-rutan om denna flik är aktiv
        self.app._update_active_plan_label()
        # Visa/dölj "ingen plan inläst"-texten i huvudfönstret
        self.app._update_no_plan_view()

    def _build_ui(self):
        # Kol 0 (vänster): försäkringslistan, fast bredd
        # Kol 1 (höger): årstabell + diagram, får all extra bredd.
        # (Månadsvis detalj ligger i ett eget fönster, se monthly_detail_window.)
        self.frame.columnconfigure(0, weight=0, minsize=380)
        self.frame.columnconfigure(1, weight=1, minsize=500)
        self.frame.rowconfigure(0, weight=1)

        # ---- Vänster panel: paste + försäkringar ----
        left = ttk.Frame(self.frame, padding=8)
        left.grid(row=0, column=0, sticky='nsew')
        left.columnconfigure(0, weight=1)

        # paste_text-widgeten behålls (används av bef. kod via self.paste_text)
        # men grid:as INTE i huvudfönstret. Visas i popup-dialog via
        # _open_paste_dialog som når via Verktyg → "Klistra in kopierad
        # text från minpension.se...". width=20 behålls för kompatibilitet.
        self.paste_text = tk.Text(left, height=8, width=20, wrap='word')
        # <<Modified>> fyrar efter alla ändringar (paste, typning, programmatisk insert).
        # Vi debouncar on_parse 150 ms efter senaste ändring.
        self.paste_text.bind('<<Modified>>', self._on_text_modified)
        # Rensa text-arean innan ny paste så ny inklistring inte adderas till gammal
        self.paste_text.bind('<<Paste>>', self._on_text_paste, add='+')
        # Ctrl+A markerar all text (Tk Text defaultar inte till select-all)
        self.paste_text.bind('<Control-a>', self._select_all_text)
        self.paste_text.bind('<Control-A>', self._select_all_text)
        # Placeholder-text när rutan är tom
        self.paste_text.tag_configure('placeholder', foreground='#888')
        self.paste_text.bind('<FocusIn>', self._on_paste_focus_in, add='+')
        self.paste_text.bind('<FocusOut>', self._on_paste_focus_out, add='+')
        self.paste_menu = tk.Menu(self.frame, tearoff=False)
        self.paste_menu.add_command(
            label="Klistra in",
            command=lambda: self.paste_text.event_generate('<<Paste>>'),
        )
        self.paste_text.bind('<Button-3>', self._show_paste_menu)

        ins_frame = ttk.LabelFrame(left, text="Försäkringar (auto-fylld)", padding=6)
        ins_frame.grid(row=0, column=0, sticky='nsew', pady=(0, 4))
        ins_frame.columnconfigure(0, weight=1)
        ins_frame.rowconfigure(0, weight=1)
        self.ins_tree = ttk.Treeview(ins_frame, show='tree', height=20)
        self.ins_tree.column('#0', width=320, stretch=True)
        self.ins_tree.tag_configure(
            'details', foreground='#666666',
            font=('TkDefaultFont', 9),
        )
        # Varannan försäkring får ljusgrå bakgrund för visuell separation
        self.ins_tree.tag_configure('pair_a', background='#ffffff')
        self.ins_tree.tag_configure('pair_b', background='#f0f0f0')
        ins_ysb = ttk.Scrollbar(ins_frame, orient='vertical', command=self.ins_tree.yview)
        self.ins_tree.configure(yscrollcommand=ins_ysb.set)
        self.ins_tree.grid(row=0, column=0, sticky='nsew')
        ins_ysb.grid(row=0, column=1, sticky='ns')
        left.rowconfigure(0, weight=1)

        # ---- Höger panel: resultat + diagram + export ----
        right = ttk.Frame(self.frame, padding=8)
        right.grid(row=0, column=1, sticky='nsew')
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=2)
        right.rowconfigure(1, weight=3)

        result_frame = ttk.LabelFrame(right, text="Årlig sammanfattning", padding=6)
        result_frame.grid(row=0, column=0, sticky='nsew')
        result_frame.columnconfigure(0, weight=1)
        result_frame.rowconfigure(0, weight=1)
        cols = ('year', 'age', 'osk',
                'acc1', 'kvar1', 'acc2', 'kvar2', 'acc3', 'kvar3')
        # Korta envradiga rubriker för kvar-kolumnerna; fulla namnen används
        # i diagrammets legend, tooltipen och checkboxarna under "Visa i diagram".
        labels = ('År', 'Ålder', 'Diff/år',
                  'Ack diff #1', 'Spara allt',
                  'Ack diff #2', 'Spara inget',
                  'Ack diff #3', 'Spara hälften')
        # Egen Treeview-stil utan disclosure-triangel-utrymme i tree-kolumnen
        # (vi har inga child-rader → den reserverade indikator-platsen blev
        # bara "luft" på vänstra sidan av Försäkr.-kolumnen)
        _compact_style = ttk.Style()
        if _compact_style.layout('Compact.Treeview.Item'):
            pass  # redan registrerad
        try:
            _compact_style.layout('Compact.Treeview.Item', [
                ('Treeitem.padding', {'sticky': 'nswe', 'children': [
                    ('Treeitem.image', {'side': 'left', 'sticky': ''}),
                    ('Treeitem.text', {'sticky': 'nswe'}),
                ]}),
            ])
        except tk.TclError:
            pass
        self.result_tree = ttk.Treeview(
            result_frame, columns=cols, show='tree headings', height=12,
            style='Compact.Treeview',
        )
        # Tree-kolumnen (#0) bär en horisontell strip av försäkrings-cirklar.
        # Bredden anpassas dynamiskt i recalculate baserat på antal försäkringar.
        self.result_tree.heading('#0', text='Försäkr.')
        self.result_tree.column('#0', width=60, minwidth=30, anchor='w', stretch=False)
        yearly_widths = {
            'year': 55, 'age': 45, 'osk': 75,
            'acc1': 90, 'kvar1': 75,
            'acc2': 90, 'kvar2': 75,
            'acc3': 90, 'kvar3': 90,
        }
        for c, label in zip(cols, labels):
            self.result_tree.heading(c, text=label)
            anchor = 'center' if c in ('year', 'age') else 'e'
            w = yearly_widths.get(c, 75)
            self.result_tree.column(c, width=w, minwidth=w, anchor=anchor, stretch=False)
        ysb = ttk.Scrollbar(result_frame, orient='vertical', command=self.result_tree.yview)
        xsb = ttk.Scrollbar(result_frame, orient='horizontal', command=self.result_tree.xview)
        self.result_tree.configure(
            yscrollcommand=ysb.set,
            xscrollcommand=_auto_hide_scrollbar(xsb),
        )
        # Zebra-rand per rad (varje år)
        self.result_tree.tag_configure('row_a', background='#ffffff')
        self.result_tree.tag_configure('row_b', background='#f0f0f0')
        self.result_tree.grid(row=0, column=0, sticky='nsew')
        # Tooltip som visar aktiva försäkringar för hovered rad
        self.result_tree.bind('<Motion>', self._on_result_row_motion)
        self.result_tree.bind('<Leave>', self._hide_table_tooltip)
        ysb.grid(row=0, column=1, sticky='ns')
        xsb.grid(row=1, column=0, sticky='ew')

        chart_frame = ttk.LabelFrame(right, text="Kvar över tid", padding=6)
        chart_frame.grid(row=1, column=0, sticky='nsew', pady=(6, 0))
        chart_frame.columnconfigure(0, weight=1)
        chart_frame.rowconfigure(0, weight=1)
        self.fig = Figure(figsize=(8, 4), dpi=80)
        self.ax = self.fig.add_subplot(111)
        self.canvas = FigureCanvasTkAgg(self.fig, master=chart_frame)
        self.canvas.get_tk_widget().grid(row=0, column=0, sticky='nsew')
        self.canvas.mpl_connect('motion_notify_event', self._on_chart_hover)
        self.canvas.mpl_connect('axes_leave_event', self._on_chart_leave)
        self.canvas.mpl_connect('figure_leave_event', self._on_chart_leave)
        # Position-läsare nere till vänster (uppdateras varje muspekarrörelse,
        # till skillnad från popup som hoppar mellan hela år)
        self.chart_pos_label = ttk.Label(
            chart_frame, text='', anchor='w',
            font=('TkDefaultFont', 9), foreground='#555',
        )
        self.chart_pos_label.grid(row=1, column=0, sticky='ew', pady=(2, 0))
        # Lägsta-värde per synlig Kvar-kurva — fast text, uppdateras bara
        # vid chart-rendering / visibility-change
        self.chart_min_frame = ttk.Frame(chart_frame)
        self.chart_min_frame.grid(row=2, column=0, sticky='ew')

        bottom = ttk.Frame(right)
        bottom.grid(row=2, column=0, sticky='ew', pady=(6, 0))
        ttk.Button(bottom, text="Exportera PDF", command=self.on_export_pdf).pack(side='right')

    # ---- Callbacks ----

    def _on_text_paste(self, event):
        """Rensa text-arean innan default-paste så ny text ersätter den gamla."""
        # Om placeholder är synlig — ta bort den först (annars tas tagg och text bort tillsammans)
        self._clear_paste_placeholder()
        self.paste_text.delete('1.0', 'end')

    def _select_all_text(self, event):
        self.paste_text.tag_add('sel', '1.0', 'end-1c')
        self.paste_text.mark_set('insert', '1.0')
        return 'break'

    # ---- Placeholder ----

    def _show_paste_placeholder(self):
        """Visa hjälp-texten om rutan är tom och inte fokuserad."""
        if self._placeholder_visible:
            return
        if self.paste_text.get('1.0', 'end-1c').strip():
            return
        self.paste_text.insert('1.0', PASTE_PLACEHOLDER, ('placeholder',))
        self._placeholder_visible = True
        self.paste_text.edit_modified(False)

    def _clear_paste_placeholder(self):
        """Ta bort placeholder-texten (kallas vid focus eller paste)."""
        if not self._placeholder_visible:
            return
        self.paste_text.delete('1.0', 'end')
        self._placeholder_visible = False
        self.paste_text.edit_modified(False)

    def _on_paste_focus_in(self, event):
        self._clear_paste_placeholder()

    def _on_paste_focus_out(self, event):
        self._show_paste_placeholder()

    def _on_text_modified(self, event):
        # <<Modified>> behöver återställas för att fyra igen vid nästa ändring
        if not self.paste_text.edit_modified():
            return
        self.paste_text.edit_modified(False)
        # Hoppa över parse om bara placeholder syns
        if self._placeholder_visible:
            return
        # Vid programmatisk inläsning av planer undertrycks både dirty och parse
        if self.app._suppress_dirty:
            return
        self.app._mark_plans_dirty()
        # Debouncad parse — schemalägg, avbryt eventuell tidigare schemaläggning
        if self._parse_after_id is not None:
            try:
                self.frame.after_cancel(self._parse_after_id)
            except tk.TclError:
                pass
        self._parse_after_id = self.frame.after(150, self._do_parse)

    def _do_parse(self):
        self._parse_after_id = None
        self.on_parse()

    def _on_chart_hover(self, event):
        if not self.calc or not self.calc.yearly or event.inaxes != self.ax:
            return
        if event.xdata is None:
            return

        # Position-läsaren under grafen: visar exakt y-värde vid muspekaren,
        # avrundat till närmaste 10 000 kr. Uppdateras pixel-för-pixel (i
        # motsats till popup som hoppar mellan hela år). Format: YYYY.M där
        # M är månadsnumret 1-12 (Jan-Dec) härlett från fraktionen av året.
        if event.ydata is not None:
            year_int = int(event.xdata)
            frac = event.xdata - year_int
            month = min(12, max(1, int(frac * 12) + 1))
            y_rounded = round(event.ydata / 10_000) * 10_000
            self.chart_pos_label.config(
                text=f'År {year_int}.{month}:  {fmt_kr(y_rounded)} kr',
            )

        years = [y.year for y in self.calc.yearly]
        pre_year = years[0] - 1 if years else None
        candidates = ([pre_year] if pre_year is not None else []) + years
        c_idx = min(range(len(candidates)), key=lambda i: abs(candidates[i] - event.xdata))
        year = candidates[c_idx]
        is_start = pre_year is not None and year == pre_year

        if self.hover_line is None:
            self.hover_line = self.ax.axvline(
                x=year, color='#888888', linestyle='--', alpha=0.6, linewidth=1,
            )
        else:
            self.hover_line.set_xdata([year, year])
            self.hover_line.set_visible(True)

        # Specialfall: startpunkten före planen — visa ursprungssparandet
        if is_start:
            sparande = self.app.get_sparande() or 0.0
            children = [
                TextArea(f"År {year} (start)", textprops=dict(fontsize=9, weight='bold')),
                TextArea(f"Ursprungssparande: {fmt_kr(sparande)}",
                         textprops=dict(fontsize=9)),
            ]
            content = VPacker(children=children, align='left', pad=2, sep=3)
            xmin, xmax = self.ax.get_xlim()
            if year < (xmin + xmax) / 2:
                xybox, box_alignment = (10, -10), (0, 1)
            else:
                xybox, box_alignment = (-10, -10), (1, 1)
            if self.hover_text is not None:
                try:
                    self.hover_text.remove()
                except (AttributeError, ValueError):
                    pass
            self.hover_text = AnnotationBbox(
                content, xy=(year, 0.98),
                xycoords=('data', 'axes fraction'),
                xybox=xybox, boxcoords='offset points',
                box_alignment=box_alignment,
                bboxprops=dict(boxstyle='round,pad=0.4',
                               fc='#ffffe0', ec='#888888', alpha=0.95),
                zorder=10,
            )
            self.ax.add_artist(self.hover_text)
            self.canvas.draw_idle()
            return

        y = self.calc.yearly[c_idx - (1 if pre_year is not None else 0)]

        # Bygg tooltip-innehåll
        children = [
            TextArea(f"År {year}, {y.age} år", textprops=dict(fontsize=9)),
            TextArea(f"Utgifter: {fmt_kr(y.utgifter_man)}/mån", textprops=dict(fontsize=9)),
        ]

        # Kvar #1/#2/#3 med färgade streck framför (hoppa över dolda)
        kvar_values = (y.kvar_1, y.kvar_2, y.kvar_3)
        states = [v.get() for v in self.app.show_kvar_vars]
        for k_idx, kvar in enumerate(kvar_values):
            if not states[k_idx]:
                continue
            da = DrawingArea(20, 6, 0, 0)
            da.add_artist(Line2D(
                [0, 20], [3, 3], color=KVAR_COLORS[k_idx], linewidth=2,
            ))
            row = HPacker(
                children=[da, TextArea(f"{KVAR_LABELS[k_idx]}: {fmt_kr(kvar)}",
                                       textprops=dict(fontsize=9))],
                align='center', pad=0, sep=4,
            )
            children.append(row)

        # Aktiva försäkringar med färgade cirklar
        active = []
        if self.parsed and self.parsed.insurances:
            for i, ins in enumerate(self.parsed.insurances):
                if _insurance_active_in_year(ins, year):
                    color = INSURANCE_COLORS[i % len(INSURANCE_COLORS)]
                    active.append((color, ins.name))

        if active:
            children.append(TextArea(
                "Aktiva försäkringar:",
                textprops=dict(fontsize=8, weight='bold'),
            ))
            for color, name in active:
                da = DrawingArea(10, 10, 0, 0)
                da.add_artist(Circle((5, 5), 4, color=color))
                ta = TextArea(name, textprops=dict(fontsize=8))
                row = HPacker(children=[da, ta], align='center', pad=0, sep=4)
                children.append(row)

        content = VPacker(children=children, align='left', pad=2, sep=3)

        # Position: x följer året (nära linjen), y fast högt upp i grafen
        # oberoende av muspekarens y-position. Växla sida så den inte går utanför.
        xmin, xmax = self.ax.get_xlim()
        if year < (xmin + xmax) / 2:
            xybox = (10, -10)
            box_alignment = (0, 1)  # top-left vid xy + offset
        else:
            xybox = (-10, -10)
            box_alignment = (1, 1)  # top-right vid xy + offset

        # AnnotationBbox stödjer inte enkel uppdatering — ta bort och skapa nytt
        if self.hover_text is not None:
            try:
                self.hover_text.remove()
            except Exception:
                pass
        self.hover_text = AnnotationBbox(
            content, xy=(year, 0.98),
            xycoords=('data', 'axes fraction'),  # x i data, y i axel-fraktion
            xybox=xybox, boxcoords='offset points',
            box_alignment=box_alignment,
            bboxprops=dict(boxstyle='round,pad=0.4', fc='#ffffe0', ec='#888888', alpha=0.95),
            zorder=10,
        )
        self.ax.add_artist(self.hover_text)

        self.canvas.draw_idle()

    # ---- Tooltip för Årlig sammanfattning ----

    def _on_result_row_motion(self, event):
        """Visa tooltip med aktiva försäkringar för raden under muspekaren."""
        if not self.parsed or not self.parsed.insurances:
            self._hide_table_tooltip()
            return
        # Hoppa över heading/separator-områden — identify_row ger annars
        # närmaste rad istället för '' när cursorn är ovanför första raden
        region = self.result_tree.identify_region(event.x, event.y)
        if region not in ('cell', 'tree'):
            self._hide_table_tooltip()
            return
        row_id = self.result_tree.identify_row(event.y)
        if not row_id:
            self._hide_table_tooltip()
            return
        if row_id == self._table_tooltip_row:
            return  # samma rad → behåll tooltipen
        values = self.result_tree.item(row_id, 'values')
        if not values:
            return
        try:
            year = int(values[0])
        except (ValueError, TypeError):
            return
        self._show_table_tooltip(year, event, row_id=row_id, tree=self.result_tree)
        self._table_tooltip_row = row_id

    def _on_monthly_row_motion(self, event, tree):
        """Tooltip för Månadsvis detalj-tabellen — visar år+månad. `tree` är
        tabellen i Månadsvis detalj-fönstret (den ligger inte i fliken)."""
        if not self.parsed or not self.parsed.insurances:
            self._hide_table_tooltip()
            return
        region = tree.identify_region(event.x, event.y)
        if region not in ('cell', 'tree'):
            self._hide_table_tooltip()
            return
        row_id = tree.identify_row(event.y)
        if not row_id:
            self._hide_table_tooltip()
            return
        if row_id == self._table_tooltip_row:
            return
        values = tree.item(row_id, 'values')
        if not values or len(values) < 2:
            return
        try:
            year = int(values[0])
            month = int(values[1])
        except (ValueError, TypeError):
            return
        self._show_table_tooltip(
            year, event, row_id=row_id, tree=tree, month=month,
        )
        self._table_tooltip_row = row_id

    def _show_table_tooltip(self, year, event, row_id=None, tree=None, month=None):
        self._destroy_table_tooltip()
        if tree is None:
            tree = self.result_tree
        items = []
        if month is not None:
            for i, ins in enumerate(self.parsed.insurances):
                if _insurance_active_in_month(ins, year, month):
                    color = INSURANCE_COLORS[i % len(INSURANCE_COLORS)]
                    items.append((i, color, ins.name, 1))
        else:
            for i, ins in enumerate(self.parsed.insurances):
                months = _insurance_months_in_year(ins, year)
                if months > 0:
                    color = INSURANCE_COLORS[i % len(INSURANCE_COLORS)]
                    items.append((i, color, ins.name, months))
        if not items:
            return
        # Per-försäkring månadsbelopp (om data finns)
        amounts_by_idx = {}
        if month is not None:
            data = _monthly_per_insurance_amounts(self.parsed, year, month)
            title = f'Aktiva försäkringar år {year} månad {month:02d}:'
        else:
            data = _yearly_per_insurance_amounts(self.parsed, year)
            title = f'Aktiva försäkringar år {year}:'
        for entry in data:
            amounts_by_idx[entry['idx']] = entry
        has_amounts = bool(amounts_by_idx)

        BG = '#ffffe0'
        tw = tk.Toplevel(self.frame)
        tw.wm_overrideredirect(True)
        tw.withdraw()  # placera först, visa sen
        tw.configure(bg=BG, highlightbackground='#888', highlightthickness=1)
        tk.Label(
            tw, text=title,
            font=('TkDefaultFont', 9, 'bold'),
            bg=BG, padx=6, pady=2, anchor='w',
        ).pack(anchor='w', fill='x', pady=(2, 0))

        if has_amounts:
            # Tre-kolumns grid: [dot+namn] [före skatt kr/mån] [efter skatt kr/mån]
            grid = tk.Frame(tw, bg=BG)
            grid.pack(anchor='w', fill='x', padx=6, pady=(0, 2))
            tk.Label(
                grid, text='', bg=BG, font=('TkDefaultFont', 9, 'bold'),
            ).grid(row=0, column=0, sticky='w')
            tk.Label(
                grid, text='Före skatt', bg=BG,
                font=('TkDefaultFont', 9, 'bold'),
            ).grid(row=0, column=1, sticky='e', padx=(12, 0))
            tk.Label(
                grid, text='Efter skatt', bg=BG,
                font=('TkDefaultFont', 9, 'bold'),
            ).grid(row=0, column=2, sticky='e', padx=(12, 0))

            def _fmt_range(lo, hi):
                if lo == hi:
                    return f'{fmt_kr(lo)} kr/mån'
                return f'{fmt_kr(lo)}–{fmt_kr(hi)} kr/mån'

            sum_b_mo = 0  # summa av månadsbelopp (varje försäkrings min-belopp)
            sum_a_mo = 0
            for r, (i, color, name, months) in enumerate(items, start=1):
                if color not in self._tooltip_dots:
                    self._tooltip_dots[color] = _make_dot_image(color, size=12)
                name_frame = tk.Frame(grid, bg=BG)
                name_frame.grid(row=r, column=0, sticky='w')
                tk.Label(
                    name_frame, image=self._tooltip_dots[color], bg=BG,
                ).pack(side='left')
                # Månads-suffix gäller bara årsvyn — i månadsvyn är 'months'
                # alltid 1 och meningslöst att visa
                if month is None and months < 12:
                    suffix = f' ({months} mån)'
                else:
                    suffix = ''
                tk.Label(
                    name_frame, text=name + suffix, bg=BG,
                    font=('TkDefaultFont', 9), padx=4, anchor='w',
                ).pack(side='left', anchor='nw')
                entry = amounts_by_idx.get(i)
                if entry:
                    before_s = _fmt_range(entry['before_mo'], entry['before_mo_max'])
                    after_s = _fmt_range(entry['after_mo'], entry['after_mo_max'])
                    sum_b_mo += entry['before_mo']
                    sum_a_mo += entry['after_mo']
                else:
                    before_s = '—'
                    after_s = '—'
                tk.Label(
                    grid, text=before_s, bg=BG,
                    font=('TkDefaultFont', 9), anchor='e',
                ).grid(row=r, column=1, sticky='e', padx=(12, 0))
                tk.Label(
                    grid, text=after_s, bg=BG,
                    font=('TkDefaultFont', 9), anchor='e',
                ).grid(row=r, column=2, sticky='e', padx=(12, 0))
            # Total-rad — summan av kr/mån när alla räknade försäkringar är aktiva
            sep = tk.Frame(grid, bg='#888', height=1)
            sep.grid(row=len(items) + 1, column=0, columnspan=3, sticky='ew', pady=(2, 2))
            tk.Label(
                grid, text='Summa per månad', bg=BG,
                font=('TkDefaultFont', 9, 'bold'),
            ).grid(row=len(items) + 2, column=0, sticky='w')
            tk.Label(
                grid, text=f'{fmt_kr(sum_b_mo)} kr/mån', bg=BG,
                font=('TkDefaultFont', 9, 'bold'), anchor='e',
            ).grid(row=len(items) + 2, column=1, sticky='e', padx=(12, 0))
            tk.Label(
                grid, text=f'{fmt_kr(sum_a_mo)} kr/mån', bg=BG,
                font=('TkDefaultFont', 9, 'bold'), anchor='e',
            ).grid(row=len(items) + 2, column=2, sticky='e', padx=(12, 0))
        else:
            # Manuell paste utan per-försäkring-belopp — visa bara aktiva
            # försäkringar (inga belopp). I månadsvyn är varje rad per def
            # aktiv hela månaden, så inget månads-suffix.
            for i, color, name, months in items:
                row = tk.Frame(tw, bg=BG)
                row.pack(anchor='w', fill='x', padx=4)
                if color not in self._tooltip_dots:
                    self._tooltip_dots[color] = _make_dot_image(color, size=12)
                tk.Label(row, image=self._tooltip_dots[color], bg=BG).pack(side='left')
                if month is not None:
                    suffix = ''
                elif months >= 12:
                    suffix = ' (hela året)'
                else:
                    suffix = f' ({months} mån)'
                tk.Label(
                    row, text=name + suffix, bg=BG,
                    font=('TkDefaultFont', 9), padx=4,
                    wraplength=280, justify='left',
                ).pack(side='left', anchor='nw')
        tk.Frame(tw, bg=BG, height=4).pack()

        # Beräkna position så tooltipen INTE överlappar raden under muspekaren.
        # Default: under raden. Om det inte får plats nedåt: ovanför raden.
        tw.update_idletasks()
        h = tw.winfo_reqheight()
        x = event.x_root + 8
        bbox = tree.bbox(row_id) if row_id else None
        if bbox:
            rx, ry, rw, rh = bbox
            tree_y = tree.winfo_rooty()
            row_top = tree_y + ry
            row_bottom = tree_y + ry + rh
            screen_h = self.frame.winfo_screenheight()
            y_below = row_bottom + 4
            if y_below + h <= screen_h:
                y = y_below
            else:
                y = max(0, row_top - h - 4)
        else:
            y = event.y_root + 20  # fallback
        tw.wm_geometry(f'+{x}+{y}')
        tw.deiconify()
        self._table_tooltip = tw

    def _hide_table_tooltip(self, event=None):
        self._destroy_table_tooltip()
        self._table_tooltip_row = None

    def _destroy_table_tooltip(self):
        if self._table_tooltip is not None:
            try:
                self._table_tooltip.destroy()
            except tk.TclError:
                pass
            self._table_tooltip = None

    def _on_chart_leave(self, event):
        if self.hover_line is not None:
            self.hover_line.set_visible(False)
        if self.hover_text is not None:
            self.hover_text.set_visible(False)
        self.chart_pos_label.config(text='')
        self.canvas.draw_idle()

    def _show_paste_menu(self, event):
        self.paste_text.focus_set()
        try:
            self.paste_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.paste_menu.grab_release()

    def _clear_outputs(self):
        """Återställ alla resultat-vyer i denna plan till tomt läge."""
        self.parsed = None
        self.calc = None
        self.ins_tree.delete(*self.ins_tree.get_children())
        self.result_tree.delete(*self.result_tree.get_children())
        self.app._refresh_monthly_window(self)
        self.ax.clear()
        self.canvas.draw()
        self._update_chart_min_label()
        self._update_tab_text('Ny plan')

    def on_parse(self):
        text = self.paste_text.get('1.0', 'end')
        try:
            parsed = parse_minpension_text(text)
        except Exception:
            self._clear_outputs()
            return

        if not parsed.insurances and not parsed.breakpoints:
            self._clear_outputs()
            return

        self.parsed = parsed

        # Lägg till allmän pension (inkomst- och premiepension) om de inte redan finns
        first_after_date = next(
            (bp.date for bp in self.parsed.breakpoints if bp.after_tax is not None),
            None,
        )
        default_start = first_after_date or self.parsed.earliest_start or (2027, 1)
        existing = {ins.name.lower() for ins in self.parsed.insurances}
        for name in ('Inkomstpension', 'Premiepension'):
            if name.lower() not in existing:
                self.parsed.insurances.append(Insurance(
                    name=name, start=default_start, duration=None,
                ))

        self._update_tab_text(self.parsed.plan_name)

        self.ins_tree.delete(*self.ins_tree.get_children())
        self._dot_images = []
        for i, ins in enumerate(self.parsed.insurances):
            bg_tag = 'pair_a' if i % 2 == 0 else 'pair_b'
            color = INSURANCE_COLORS[i % len(INSURANCE_COLORS)]
            dot = _make_dot_image(color)
            self._dot_images.append(dot)
            start = f"{ins.start[0]}-{ins.start[1]:02d}" if ins.start else '?'
            dur = (
                f"{ins.duration[0]} år, {ins.duration[1]} mån"
                if ins.duration else 'Livsvarigt'
            )
            self.ins_tree.insert(
                '', 'end', text=' ' + ins.name, image=dot,
                tags=(bg_tag,),
            )
            self.ins_tree.insert(
                '', 'end',
                text=f'        {start}, {dur}',
                tags=(bg_tag, 'details'),
            )

        self.recalculate()

        # Uppdatera Inkomst per försäkring-vyn om den visar denna flik
        if (self.app._insurance_income_window is not None
                and self.app._insurance_income_window.tab is self):
            try:
                self.app._insurance_income_window.refresh_for_tab(self)
            except tk.TclError:
                self.app._insurance_income_window = None
        # Uppdatera Jämför planer-fönstret om det är öppet (graf + matris)
        self.app._refresh_compare_window()

    def apply_parsed(self, parsed):
        """Sätt en färdig ParsedPlan direkt (t.ex. från greedy-optimering) utan
        att gå via paste-text-flödet. Uppdaterar UI och räknar om."""
        self.parsed = parsed
        # Ersätt paste_text med en beskrivande stub så placeholder inte visas
        prev = self.app._suppress_dirty
        self.app._suppress_dirty = True
        try:
            self._placeholder_visible = False
            self.paste_text.delete('1.0', 'end')
            self.paste_text.insert(
                '1.0',
                f'# {parsed.plan_name}\n'
                f'# Simulerad plan (greedy-optimering) — approximerade månadsbelopp.\n'
                f'# Ändra inte i denna ruta; den är inte parse:bar.\n',
            )
            self.paste_text.edit_modified(False)
        finally:
            self.app._suppress_dirty = prev

        self._update_tab_text(parsed.plan_name)
        self.ins_tree.delete(*self.ins_tree.get_children())
        self._dot_images = []
        for i, ins in enumerate(parsed.insurances):
            bg_tag = 'pair_a' if i % 2 == 0 else 'pair_b'
            color = INSURANCE_COLORS[i % len(INSURANCE_COLORS)]
            dot = _make_dot_image(color)
            self._dot_images.append(dot)
            start = f"{ins.start[0]}-{ins.start[1]:02d}" if ins.start else '?'
            dur = (
                f"{ins.duration[0]} år, {ins.duration[1]} mån"
                if ins.duration else 'Livsvarigt'
            )
            self.ins_tree.insert(
                '', 'end', text=' ' + ins.name, image=dot, tags=(bg_tag,),
            )
            self.ins_tree.insert(
                '', 'end', text=f'        {start}, {dur}',
                tags=(bg_tag, 'details'),
            )
        self.recalculate()
        if (self.app._insurance_income_window is not None
                and self.app._insurance_income_window.tab is self):
            try:
                self.app._insurance_income_window.refresh_for_tab(self)
            except tk.TclError:
                self.app._insurance_income_window = None
        self.app._refresh_compare_window()

    def _best_legend_loc(self):
        """Välj det hörn (övre/nedre × vänster/höger) som har mest luft mellan
        synliga Kvar-linjer och kanten — så legenden inte täcker grafen."""
        if not self.calc or not self.calc.yearly:
            return 'upper right'
        states = [v.get() for v in self.app.show_kvar_vars]
        attrs = [a for s, a in zip(states, ('kvar_1', 'kvar_2', 'kvar_3')) if s]
        if not attrs:
            return 'upper right'
        yearly = self.calc.yearly
        n_sample = max(1, len(yearly) // 5)  # första/sista 20%

        left_vals = [getattr(y, a) for y in yearly[:n_sample] for a in attrs]
        right_vals = [getattr(y, a) for y in yearly[-n_sample:] for a in attrs]
        all_vals = [getattr(y, a) for y in yearly for a in attrs]
        g_min, g_max = min(all_vals), max(all_vals)

        # Hur mycket luft (i kr) finns mellan linjer och varje hörn?
        spaces = {
            'upper left':  g_max - max(left_vals),
            'upper right': g_max - max(right_vals),
            'lower left':  min(left_vals) - g_min,
            'lower right': min(right_vals) - g_min,
        }
        return max(spaces, key=spaces.get)

    def recalculate(self):
        if not self.parsed:
            return
        sparande = self.app.get_sparande()
        brackets = self.app.get_brackets()
        if sparande is None or not brackets:
            return

        self.calc = calculate_plan(
            self.parsed, sparande, brackets,
            growth_pct=self.app.get_growth_pct(),
            growth_mode=self.app.get_growth_mode(),
        )

        # Uppdatera årlig tabell — färg baseras på årtal (samma år = samma färg över flikar)
        self.result_tree.delete(*self.result_tree.get_children())
        # Bygg försäkrings-indikator: en cirkel per aktiv försäkring (hel = aktiv
        # hela året, halv = del av året). Cachar PhotoImage per unik kombination.
        insurances = self.parsed.insurances if self.parsed else []
        self._year_strip_images = {}  # cache: tuple-key → PhotoImage
        # Bredda #0-kolumnen så strip:en får plats (10 px/cirkel + 2 px gap + 10 padding)
        n_ins = len(insurances)
        # 8 px padding (2 vänster, 6 höger) — indikator-utrymmet borttaget
        # via Compact.Treeview-stilen så cirklarna kan börja nära kolumnkanten
        ins_col_width = max(40, n_ins * 10 + max(0, n_ins - 1) * 2 + 8)
        self.result_tree.column('#0', width=ins_col_width)
        for y in self.calc.yearly:
            tag = 'row_a' if y.year % 2 == 0 else 'row_b'
            states = []
            for i, ins in enumerate(insurances):
                months = _insurance_months_in_year(ins, y.year)
                if months == 0:
                    continue
                color = INSURANCE_COLORS[i % len(INSURANCE_COLORS)]
                states.append((color, months >= 12))
            key = tuple(states)
            img = self._year_strip_images.get(key)
            if img is None and states:
                img = _make_insurance_strip_image(list(states))
                self._year_strip_images[key] = img
            kwargs = {'values': (
                y.year,
                y.age,
                fmt_kr(y.overskott_ar),
                fmt_kr(y.acc_1), fmt_kr(y.kvar_1),
                fmt_kr(y.acc_2), fmt_kr(y.kvar_2),
                fmt_kr(y.acc_3), fmt_kr(y.kvar_3),
            ), 'tags': (tag,)}
            if img is not None:
                kwargs['image'] = img
            self.result_tree.insert('', 'end', **kwargs)

        # Månadsvis detalj-fönstret (om öppet och denna flik är aktiv)
        self.app._refresh_monthly_window(self)

        # Lazy chart-rendering: rita bara om denna flik är synlig just nu.
        # Annars markeras diagrammet som "dirty" och ritas vid första flikbyte hit.
        self._chart_dirty = True
        if self._is_active_tab():
            self._render_chart()

    def _is_active_tab(self) -> bool:
        # Under file-load skjuts ALL chart-rendering upp till slutet
        if getattr(self.app, '_initial_load_in_progress', False):
            return False
        try:
            return str(self.frame) == self.notebook.select()
        except (tk.TclError, AttributeError):
            return False

    def ensure_chart_rendered(self):
        """Rendera diagrammet om det är 'dirty' (kallas vid flikbyte)."""
        if self._chart_dirty and self.calc is not None:
            self._render_chart()

    def _render_chart(self):
        """Rita Kvar-diagrammet utifrån self.calc. Tung matplotlib-operation —
        anropas bara när fliken är synlig."""
        if self.calc is None:
            return
        sparande = self.app.get_sparande() or 0.0
        self.ax.clear()
        # Hover-artefakter förstördes av clear() — återställ refs så de lazy-skapas igen
        self.hover_line = None
        self.hover_text = None
        years = [y.year for y in self.calc.yearly]
        kvar_attrs = ('kvar_1', 'kvar_2', 'kvar_3')
        states = [v.get() for v in self.app.show_kvar_vars]
        self._kvar_lines = []
        # Startpunkt: året innan första plan-året, värde = ursprungssparandet.
        # Gör att grafen alltid visar utgångsnivån och att planer med olika
        # startår är visuellt jämförbara.
        pre_year = years[0] - 1 if years else None
        for k_idx, attr in enumerate(kvar_attrs):
            y_vals = [getattr(y, attr) for y in self.calc.yearly]
            if pre_year is not None:
                x_plot = [pre_year] + years
                y_plot = [sparande] + y_vals
            else:
                x_plot = years
                y_plot = y_vals
            line, = self.ax.plot(
                x_plot, y_plot,
                label=KVAR_LABELS[k_idx],
                color=KVAR_COLORS[k_idx], linewidth=2,
            )
            line.set_visible(states[k_idx])
            self._kvar_lines.append(line)

        growth_pct = self.app.get_growth_pct()
        title = f'Kvar över tid med {fmt_kr(sparande)} kr ursprungligt sparande'
        if growth_pct > 0:
            title += f' och {growth_pct:g}%'.replace('.', ',') + ' tillväxt'
        self.ax.set_title(title)
        self.ax.set_xlabel('År')
        self.ax.set_ylabel('Kvar av sparande (kr)')
        # Legenden visar bara synliga linjer i bästa övre hörnet
        visible = [ln for ln in self._kvar_lines if ln.get_visible()]
        if visible:
            self.ax.legend(
                visible, [ln.get_label() for ln in visible],
                loc=self._best_legend_loc(),
            )
        # Y-axel: tusenavskiljare (mellanslag, svensk stil) på tick-etiketter
        self.ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: fmt_kr(v)))

        self.ax.xaxis.set_major_locator(MultipleLocator(5))
        self.ax.xaxis.set_minor_locator(MultipleLocator(1))

        # Y minor: 50 000 kr default (extra synlig referensnivå);
        # höj steget om data-range är extremt så vi inte får 1000+ ticks
        ymin, ymax = self.ax.get_ylim()
        range_y = max(1.0, ymax - ymin)
        y_step = 10_000_000
        for candidate in (50_000, 100_000, 250_000, 500_000,
                          1_000_000, 2_000_000, 5_000_000):
            if range_y / candidate <= 80:
                y_step = candidate
                break
        self.ax.yaxis.set_minor_locator(MultipleLocator(y_step))

        # Lite längre + tjockare streck på y-axelns 50 000-nivå
        self.ax.tick_params(axis='y', which='major', length=8, width=1.2)
        self.ax.tick_params(axis='y', which='minor', length=5, width=0.8)

        # Grid: separata stilar per axel + nivå så y-minor (50 000) syns tydligt
        self.ax.grid(which='major', axis='x', alpha=0.4, linewidth=0.8)
        self.ax.grid(which='minor', axis='x', alpha=0.15, linewidth=0.5)
        self.ax.grid(which='major', axis='y', alpha=0.45, linewidth=1.2)
        self.ax.grid(which='minor', axis='y', alpha=0.30, linewidth=0.8)

        self.fig.tight_layout()
        self.canvas.draw()
        self._chart_dirty = False
        self._update_chart_min_label()

    def _update_chart_min_label(self):
        """Visa lägsta värdet för varje synlig Kvar-kurva under grafen.
        Anropas bara vid render eller visibility-change, inte vid mus-rörelse."""
        for w in self.chart_min_frame.winfo_children():
            w.destroy()
        if not self.calc or not self.calc.yearly:
            return
        states = [v.get() for v in self.app.show_kvar_vars]
        kvar_attrs = ('kvar_1', 'kvar_2', 'kvar_3')
        shown_any = False
        for k_idx, attr in enumerate(kvar_attrs):
            if not states[k_idx]:
                continue
            vals = [getattr(y, attr) for y in self.calc.yearly]
            if not vals:
                continue
            min_v = min(vals)
            if not shown_any:
                tk.Label(
                    self.chart_min_frame, text='Lägsta värde: ',
                    font=('TkDefaultFont', 9), fg='#555',
                ).pack(side='left')
            else:
                tk.Label(
                    self.chart_min_frame, text='  |  ',
                    font=('TkDefaultFont', 9), fg='#555',
                ).pack(side='left')
            tk.Label(
                self.chart_min_frame, text='■',
                font=('TkDefaultFont', 10), fg=KVAR_COLORS[k_idx],
            ).pack(side='left')
            tk.Label(
                self.chart_min_frame,
                text=f' {KVAR_LABELS[k_idx]}: {fmt_kr(min_v)} kr',
                font=('TkDefaultFont', 9), fg='#555',
            ).pack(side='left')
            shown_any = True

    def on_export_pdf(self):
        if not self.parsed or not self.calc:
            messagebox.showerror("Fel", "Tolka data först.")
            return
        # Säkerställ att diagrammet är uppdaterat innan det embed:as i PDF:en
        self.ensure_chart_rendered()
        plan_name = self.parsed.plan_name or 'pensionsplan'
        default_filename = ''.join(
            c if c.isalnum() or c in ' _-' else '_' for c in plan_name
        ) + '.pdf'
        last_dir = self.app._last_pdf_dir
        filename = filedialog.asksaveasfilename(
            defaultextension='.pdf',
            filetypes=[('PDF', '*.pdf')],
            initialfile=default_filename,
            initialdir=last_dir if last_dir and os.path.isdir(last_dir) else None,
        )
        if not filename:
            return
        try:
            sparande = self.app.get_sparande() or 0.0
            brackets = self.app.get_brackets() or []
            export_to_pdf(
                filename, plan_name, sparande, brackets,
                self.parsed, self.calc, self.fig,
            )
            self.app._last_pdf_dir = os.path.dirname(filename)
            self.app._remember_state()
            if messagebox.askyesno(
                "Klar",
                f"PDF sparad:\n{filename}\n\nÖppna med standardprogram?",
            ):
                _open_with_default_app(filename)
        except Exception as e:
            messagebox.showerror("Fel vid PDF-export", str(e))


# Färgpalett för olika planer i jämför-fönstret (matplotlib's tableau-färger)
COMPARE_COLORS = [
    '#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd',
    '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf',
]


class CompareWindow:
    """Toplevel-fönster som överlagrar Kvar-linjer från flera planer för jämförelse."""

    LINESTYLES = [':', '-', '--']  # Kvar #1=prickad, #2=heldragen, #3=streckad

    def __init__(self, app):
        self.app = app
        self.top = tk.Toplevel(app.root)
        self.top.title("Jämför planer")
        self._apply_initial_geometry()
        self.top.protocol('WM_DELETE_WINDOW', self._on_close)

        # State som speglas i .pension_planner_state.json
        cs = app._compare_state or {}
        saved_plans = set(cs.get('plans') or [])
        show_kvar_saved = cs.get('show_kvar') or [True, False, False]

        self.plan_vars = {}  # plan_name → BooleanVar
        for tab in app.tabs:
            name = tab.parsed.plan_name if (tab.parsed and tab.parsed.plan_name) else ''
            if not name:
                continue
            # Defaulta till markerad om inget tidigare sparat, annars sparat värde
            default = (name in saved_plans) if saved_plans else True
            self.plan_vars[name] = tk.BooleanVar(value=default)

        self.kvar_vars = [
            tk.BooleanVar(value=bool(show_kvar_saved[i]) if i < len(show_kvar_saved) else False)
            for i in range(3)
        ]

        # Gemensam slutårs-variabel för Rankning + Mix, sparad i state-filen
        # (0 = automatisk = sista året i valda planer)
        saved_end_year = 0
        try:
            saved_end_year = int(app._read_state().get('compare_end_year', 0) or 0)
        except (ValueError, AttributeError, tk.TclError):
            saved_end_year = 0
        self.compare_end_year_var = tk.IntVar(value=saved_end_year)
        self.compare_end_year_var.trace_add(
            'write', lambda *_: self._on_compare_end_year_change(),
        )

        # Gemensam kvar-strategi för Rankning + Mix, sparad i state-filen
        # (0=Spara allt, 1=Spara inget, 2=Spara hälften)
        saved_kvar = 1
        try:
            saved_kvar = int(app._read_state().get('compare_kvar_strategy', 1))
            if saved_kvar not in (0, 1, 2):
                saved_kvar = 1
        except (ValueError, AttributeError, tk.TclError):
            saved_kvar = 1
        self.compare_kvar_var = tk.IntVar(value=saved_kvar)
        self.compare_kvar_var.trace_add(
            'write', lambda *_: self._on_compare_kvar_change(),
        )

        # Mix-tab state — nollställs vid varje familj-byte i _rebuild_mix_family_info
        # _mix_source_vars: (name, occ) → StringVar med 'Mixa' eller display-namnet
        # för en av family-planerna (används för att låsa försäkringen till just
        # den planens värde).
        self._mix_source_vars = {}
        self._mix_current_varying = []
        self._mix_current_n_source = 0
        self._mix_current_family_tabs = []
        self._last_mix_results = None
        self._last_mix_family_tabs = None
        self._last_mix_varying_keys = None
        self._mix_min_year = 2020
        self._mix_max_year = 2100

        self._build_layout()
        self._lines = []  # alla ritade Line2D
        self._line_meta = []  # parallell lista med dicts {plan_name, k_idx, calc}
        self.hover_line = None
        self.hover_text = None
        self._sync_preset_combo_to_selection()
        self._redraw()
        self._render_monthly_income()
        self._rebuild_insurance_matrix()
        self._rebuild_ranking()
        self._rebuild_mix_families()

    def _build_layout(self):
        # Vänster panel: checkboxar för planer + Kvar #
        left = ttk.Frame(self.top, padding=8)
        left.pack(side='left', fill='y')

        # Presets: namngivna grupper av planer + Kvar #
        ttk.Label(
            left, text="Preset:", font=('TkDefaultFont', 9, 'bold'),
        ).pack(anchor='w')
        preset_row = ttk.Frame(left)
        preset_row.pack(anchor='w', pady=(2, 4), fill='x')
        self.preset_var = tk.StringVar(value='')
        self.preset_combo = ttk.Combobox(
            preset_row, textvariable=self.preset_var, state='readonly', width=22,
        )
        self.preset_combo.pack(side='left')
        self.preset_combo.bind('<<ComboboxSelected>>', self._on_preset_selected)
        preset_btns = ttk.Frame(left)
        preset_btns.pack(anchor='w', pady=(0, 8), fill='x')
        ttk.Button(
            preset_btns, text='Spara som...', command=self._save_preset, width=12,
        ).pack(side='left', padx=(0, 4))
        ttk.Button(
            preset_btns, text='Ta bort', command=self._delete_preset, width=10,
        ).pack(side='left')
        self._refresh_preset_combo()

        ttk.Separator(left, orient='horizontal').pack(fill='x', pady=4)

        ttk.Label(left, text="Välj planer:", font=('TkDefaultFont', 9, 'bold')).pack(anchor='w')
        self._plans_frame = ttk.Frame(left)
        self._plans_frame.pack(anchor='w', pady=(2, 8), fill='x')
        plans_frame = self._plans_frame
        # Spara checkbox-ordningen så range-operationer kan slå upp index → namn
        self._plan_names_ordered = list(self.plan_vars.keys())
        self._anchor_idx = None  # senaste klickade plan (för shift+klick-range)
        # Bakgrundsfärg för left-panel (används för "släckt" färg-svab)
        try:
            neutral_bg = plans_frame.cget('background') or '#f0f0f0'
        except tk.TclError:
            neutral_bg = '#f0f0f0'
        self._plans_neutral_bg = neutral_bg
        self._plan_color_swatches = {}  # plan_name → tk.Label

        for i, name in enumerate(self._plan_names_ordered):
            self._add_plan_row(name, i)

        ttk.Label(
            left,
            text="Tips: Shift+vänsterklick = markera intervall, "
                 "Shift+högerklick = avmarkera intervall",
            font=('TkDefaultFont', 8), foreground='#666',
            wraplength=200, justify='left',
        ).pack(anchor='w', pady=(0, 4))

        ttk.Separator(left, orient='horizontal').pack(fill='x', pady=4)

        ttk.Label(left, text="Visa graf:", font=('TkDefaultFont', 9, 'bold')).pack(anchor='w')
        kvar_frame = ttk.Frame(left)
        kvar_frame.pack(anchor='w', pady=(2, 8), fill='x')
        for i, var in enumerate(self.kvar_vars):
            cb = ttk.Checkbutton(
                kvar_frame, text=KVAR_LABELS[i], variable=var,
                command=self._on_selection_change,
            )
            cb.pack(anchor='w')

        ttk.Label(
            left,
            text=f"(Prickad = {KVAR_LABELS[0]}, "
                 f"heldragen = {KVAR_LABELS[1]}, "
                 f"streckad = {KVAR_LABELS[2]})",
            font=('TkDefaultFont', 8), foreground='#666',
            wraplength=180, justify='left',
        ).pack(anchor='w')

        # Höger panel: notebook med Graf + Försäkringar-jämförelse
        right = ttk.Frame(self.top)
        right.pack(side='left', fill='both', expand=True)

        # Accent-stil för Räkna mix-knappen när mix-resultatet är föråldrat
        try:
            _style = ttk.Style()
            _style.configure(
                'Accent.TButton', background='#f39c12', foreground='#000',
            )
            _style.map(
                'Accent.TButton',
                background=[('active', '#e67e22'), ('!disabled', '#f39c12')],
            )
        except tk.TclError:
            pass
        self._mix_stale = False

        self.right_notebook = ttk.Notebook(right)
        self.right_notebook.pack(fill='both', expand=True)

        # Flik 1: Graf
        graph_tab = ttk.Frame(self.right_notebook)
        self.right_notebook.add(graph_tab, text='Graf')
        self.fig = Figure(figsize=(9, 6), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.canvas = FigureCanvasTkAgg(self.fig, master=graph_tab)
        self.canvas.get_tk_widget().pack(fill='both', expand=True)
        self.canvas.mpl_connect('motion_notify_event', self._on_hover)
        self.canvas.mpl_connect('axes_leave_event', self._on_leave)
        self.canvas.mpl_connect('figure_leave_event', self._on_leave)
        # Position-läsare under grafen (samma stil som i huvudfönstret)
        self.pos_label = ttk.Label(
            graph_tab, text='', anchor='w',
            font=('TkDefaultFont', 9), foreground='#555',
        )
        self.pos_label.pack(fill='x', pady=(2, 0))

        # Flik 2: Månadsinkomst (månad-för-månad pensionsutbetalning per plan)
        monthly_tab = ttk.Frame(self.right_notebook)
        self.right_notebook.add(monthly_tab, text='Månadsinkomst')
        m_top = ttk.Frame(monthly_tab, padding=(6, 4, 6, 2))
        m_top.pack(fill='x')
        ttk.Label(
            m_top, text='Visa:',
            font=('TkDefaultFont', 9, 'bold'),
        ).pack(side='left', padx=(0, 6))
        self.monthly_tax_var = tk.StringVar(value='after')
        ttk.Radiobutton(
            m_top, text='Efter skatt', variable=self.monthly_tax_var,
            value='after', command=self._render_monthly_income,
        ).pack(side='left')
        ttk.Radiobutton(
            m_top, text='Före skatt', variable=self.monthly_tax_var,
            value='before', command=self._render_monthly_income,
        ).pack(side='left', padx=(6, 0))
        self.monthly_fig = Figure(figsize=(9, 5), dpi=100)
        self.monthly_ax = self.monthly_fig.add_subplot(111)
        self.monthly_canvas = FigureCanvasTkAgg(self.monthly_fig, master=monthly_tab)
        self.monthly_canvas.get_tk_widget().pack(fill='both', expand=True)
        self.monthly_canvas.mpl_connect(
            'motion_notify_event', self._on_monthly_hover,
        )
        self.monthly_canvas.mpl_connect(
            'axes_leave_event', self._on_monthly_leave,
        )
        self.monthly_canvas.mpl_connect(
            'figure_leave_event', self._on_monthly_leave,
        )
        self.monthly_pos_label = ttk.Label(
            monthly_tab, text='', anchor='w',
            font=('TkDefaultFont', 9), foreground='#555',
        )
        self.monthly_pos_label.pack(fill='x', pady=(2, 0))
        # State för monthly-grafen (fylls i _render_monthly_income)
        self._monthly_lines = []
        self._monthly_line_meta = []
        self._monthly_hover_line = None
        self._monthly_hover_text = None

        # Flik 3: Försäkringar (matris: rader = försäkring, kolumner = plan)
        self.ins_tab = ttk.Frame(self.right_notebook)
        self.right_notebook.add(self.ins_tab, text='Försäkringar')

        # Flik 3: Rankning — sortera valda planer efter kvar-mått
        self.rank_tab = ttk.Frame(self.right_notebook)
        self.right_notebook.add(self.rank_tab, text='Rankning')

        # Flik 4: Mix (EXPERIMENTELL) — mixa försäkrings-uttag från valda planer
        self.mix_tab = ttk.Frame(self.right_notebook)
        self.right_notebook.add(self.mix_tab, text='⚗ Mix (experimentell)')
        # Varningsbanner överst
        warn = tk.Frame(self.mix_tab, bg='#fff3cd')
        warn.pack(fill='x')
        tk.Label(
            warn,
            text='⚠  EXPERIMENTELL FUNKTION  —  '
                 'Resultatet är en beräkningsmodell baserad på riktiga månadsbelopp '
                 'från valda planer. Skatten approximeras från en av planerna '
                 '(korrekt inom samma start-familj men verifiera stora skillnader mot minpension.se).',
            bg='#fff3cd', fg='#7a5900',
            font=('TkDefaultFont', 9, 'bold'),
            wraplength=900, justify='left', padx=8, pady=6,
        ).pack(fill='x')
        # Kontroller
        mctrl = ttk.Frame(self.mix_tab, padding=(8, 8, 8, 4))
        mctrl.pack(fill='x')
        ttk.Label(
            mctrl, text='Bara planer med samma start kan mixas.',
            font=('TkDefaultFont', 9), foreground='#666',
        ).grid(row=0, column=0, columnspan=6, sticky='w', pady=(0, 4))
        ttk.Label(
            mctrl, text='Start-familj:', font=('TkDefaultFont', 9, 'bold'),
        ).grid(row=1, column=0, sticky='w', padx=(0, 6))
        self.mix_family_var = tk.StringVar(value='')
        self.mix_family_combo = ttk.Combobox(
            mctrl, textvariable=self.mix_family_var, state='readonly', width=20,
        )
        self.mix_family_combo.grid(row=1, column=1, sticky='w')
        self.mix_family_combo.bind(
            '<<ComboboxSelected>>', lambda e: self._rebuild_mix_family_info(),
        )
        ttk.Label(
            mctrl, text='Kvar-strategi:',
            font=('TkDefaultFont', 9, 'bold'),
        ).grid(row=1, column=2, sticky='w', padx=(16, 6))
        # Gemensam med Rankning-fliken (via self.compare_kvar_var) — sparas
        # i state-filen så inställningen kommer ihåg mellan sessioner
        self.mix_kvar_var = self.compare_kvar_var
        self.mix_kvar_var.trace_add(
            'write', lambda *_: self._mix_mark_stale(),
        )
        for i, lbl in enumerate(KVAR_LABELS):
            ttk.Radiobutton(
                mctrl, text=lbl, variable=self.mix_kvar_var, value=i,
            ).grid(row=1, column=3 + i, sticky='w', padx=4)
        # Slutår-kontroll: begränsa metrikerna till året <= detta.
        # Fylls med giltigt intervall när en familj väljs.
        ttk.Label(
            mctrl, text='Slutår för jämförelse:',
            font=('TkDefaultFont', 9, 'bold'),
        ).grid(row=2, column=0, sticky='w', pady=(6, 0))
        # Gemensam med Rankning-fliken (via self.compare_end_year_var)
        self.mix_end_year_var = self.compare_end_year_var
        self.mix_end_year_spin = ttk.Spinbox(
            mctrl, textvariable=self.mix_end_year_var,
            from_=2020, to=2100, width=8,
            command=self._on_mix_end_year_change,
        )
        self.mix_end_year_spin.grid(
            row=2, column=1, sticky='w', pady=(6, 0), padx=(6, 0),
        )
        self.mix_end_year_spin.bind(
            '<Return>', lambda e: self._on_mix_end_year_change(),
        )
        self.mix_end_year_spin.bind(
            '<FocusOut>', lambda e: self._on_mix_end_year_change(),
        )
        self.mix_end_year_hint = ttk.Label(
            mctrl, text='', font=('TkDefaultFont', 9), foreground='#666',
        )
        self.mix_end_year_hint.grid(
            row=2, column=2, columnspan=4, sticky='w', padx=(8, 0), pady=(6, 0),
        )

        self.mix_run_btn = ttk.Button(
            mctrl, text='Räkna mix', command=self._run_mix,
        )
        self.mix_run_btn.grid(row=3, column=0, sticky='w', pady=(6, 0))
        self.mix_status = ttk.Label(
            mctrl, text='', font=('TkDefaultFont', 9), foreground='#555',
        )
        self.mix_status.grid(
            row=3, column=1, columnspan=5, sticky='w', padx=(8, 0), pady=(6, 0),
        )
        # Body för info + resultat
        self.mix_body = ttk.Frame(self.mix_tab)
        self.mix_body.pack(fill='both', expand=True, padx=8, pady=4)

        # Föreslå plan har flyttats till eget fönster (Verktyg → Föreslå plan)

        # Kontroller överst
        # Gemensam med Mix-fliken (via self.compare_kvar_var)
        self.rank_kvar_var = self.compare_kvar_var
        self.rank_metric_var = tk.StringVar(value='slutvarde')
        ctrl = ttk.Frame(self.rank_tab, padding=(8, 8, 8, 4))
        ctrl.pack(fill='x')
        ttk.Label(
            ctrl, text='Kvar-strategi:',
            font=('TkDefaultFont', 9, 'bold'),
        ).grid(row=0, column=0, sticky='w', padx=(0, 6))
        for i, lbl in enumerate(KVAR_LABELS):
            ttk.Radiobutton(
                ctrl, text=lbl, variable=self.rank_kvar_var, value=i,
                command=self._rebuild_ranking,
            ).grid(row=0, column=1 + i, sticky='w', padx=4)
        ttk.Label(
            ctrl, text='Sortera på:', font=('TkDefaultFont', 9, 'bold'),
        ).grid(row=1, column=0, sticky='w', padx=(0, 6), pady=(6, 0))
        metrics = [
            ('slutvarde', 'Slutvärde (sista året)'),
            ('lagsta', 'Lägsta värde (över hela perioden)'),
            ('total', 'Total (summan över alla år)'),
        ]
        for i, (val, lbl) in enumerate(metrics):
            ttk.Radiobutton(
                ctrl, text=lbl, variable=self.rank_metric_var, value=val,
                command=self._rebuild_ranking,
            ).grid(row=1, column=1 + i, sticky='w', padx=4, pady=(6, 0))
        # Slutår-kontroll — begränsar metrikerna till året <= detta
        ttk.Label(
            ctrl, text='Slutår för jämförelse:',
            font=('TkDefaultFont', 9, 'bold'),
        ).grid(row=2, column=0, sticky='w', pady=(6, 0))
        # Gemensam med Mix-fliken (via self.compare_end_year_var) — så
        # slutåret synkas mellan Rankning och Mix och sparas i state
        self.rank_end_year_var = self.compare_end_year_var
        self.rank_end_year_spin = ttk.Spinbox(
            ctrl, textvariable=self.rank_end_year_var,
            from_=2020, to=2100, width=8,
            command=self._rebuild_ranking,
        )
        self.rank_end_year_spin.grid(
            row=2, column=1, sticky='w', pady=(6, 0), padx=(6, 0),
        )
        self.rank_end_year_spin.bind(
            '<Return>', lambda e: self._rebuild_ranking(),
        )
        self.rank_end_year_spin.bind(
            '<FocusOut>', lambda e: self._rebuild_ranking(),
        )
        self.rank_end_year_hint = ttk.Label(
            ctrl, text='', font=('TkDefaultFont', 9), foreground='#666',
        )
        self.rank_end_year_hint.grid(
            row=2, column=2, columnspan=4, sticky='w', padx=(8, 0), pady=(6, 0),
        )
        self._rank_min_year = 2020
        self._rank_max_year = 2100
        # Container för själva tabellen (byggs om i _rebuild_ranking)
        self.rank_body = ttk.Frame(self.rank_tab)
        self.rank_body.pack(fill='both', expand=True, padx=8, pady=4)

    def _on_selection_change(self):
        self._save_state()
        self._sync_preset_combo_to_selection()
        self._redraw()
        self._render_monthly_income()
        self._rebuild_insurance_matrix()
        self._rebuild_ranking()
        self._rebuild_mix_families()

    def _rebuild_mix_families(self):
        """Fyll dropdown med start-familjer bland valda planer.
        En 'familj' = alla planer med samma earliest_start."""
        if not hasattr(self, 'mix_family_combo'):
            return
        families = {}  # start-str → antal planer
        for name, var in self.plan_vars.items():
            if not var.get():
                continue
            tab = next(
                (t for t in self.app.tabs
                 if t.parsed and t.parsed.plan_name == name),
                None,
            )
            if tab is None or not tab.parsed.earliest_start:
                continue
            y, m = tab.parsed.earliest_start
            key = f'{y}-{m:02d}'
            families[key] = families.get(key, 0) + 1
        # Bara familjer med >= 2 planer — inget att mixa annars
        available = [
            f'{k}  ({n} planer)' for k, n in sorted(families.items()) if n >= 2
        ]
        self.mix_family_combo['values'] = available
        current = self.mix_family_var.get().split('  ')[0]
        if current and any(v.startswith(current + '  ') for v in available):
            for v in available:
                if v.startswith(current + '  '):
                    self.mix_family_var.set(v)
                    break
        elif available:
            self.mix_family_var.set(available[0])
        else:
            self.mix_family_var.set('')
        self._rebuild_mix_family_info()

    def _rebuild_mix_family_info(self):
        for w in self.mix_body.winfo_children():
            w.destroy()
        fam = self.mix_family_var.get()
        if not fam:
            ttk.Label(
                self.mix_body,
                text=(
                    'Ingen start-familj tillgänglig. Välj minst 2 planer med '
                    'samma uttagsstart i vänstra panelen.'
                ),
                foreground='#666',
            ).pack(pady=20)
            return
        family_key = fam.split('  ')[0]
        # Hitta planer i familjen
        family_tabs = self._family_tabs(family_key)
        if len(family_tabs) < 2:
            ttk.Label(
                self.mix_body, text='Behöver minst 2 planer.', foreground='#666',
            ).pack(pady=20)
            return

        # Räkna hur många försäkringar som varierar → antalet kombinationer
        varying, identical = self._classify_family_insurances(family_tabs)
        n_source = len(family_tabs)
        self._mix_current_varying = varying
        self._mix_current_n_source = n_source
        self._mix_current_family_tabs = family_tabs

        # Uppdatera Slutår-Spinboxens intervall utifrån familjens planer.
        # Default = sista året (samma som förut).
        min_year = None
        max_year = None
        for _, tab in family_tabs:
            if tab.calc and tab.calc.yearly:
                fy = tab.calc.yearly[0].year
                ly = tab.calc.yearly[-1].year
                if min_year is None or fy < min_year:
                    min_year = fy
                if max_year is None or ly > max_year:
                    max_year = ly
        if min_year is not None and max_year is not None:
            self._mix_min_year = min_year
            self._mix_max_year = max_year
            try:
                self.mix_end_year_spin.config(from_=min_year, to=max_year)
                # Sätt default (max_year) endast om ingen tidigare värde finns
                # (0) eller om värdet är utanför nya intervallet
                cur = self.mix_end_year_var.get()
                if cur == 0 or cur < min_year or cur > max_year:
                    self.mix_end_year_var.set(max_year)
                end_year_for_hint = self.mix_end_year_var.get()
                age_at_end = None
                for _, tab in family_tabs:
                    if not tab.calc or not tab.calc.yearly:
                        continue
                    for yy in tab.calc.yearly:
                        if yy.year == end_year_for_hint:
                            age_at_end = yy.age
                            break
                    if age_at_end is not None:
                        break
                if age_at_end is not None:
                    hint = (f'Välj slutår mellan {min_year} och {max_year}; '
                            f'ålder {age_at_end} år vid slutår '
                            f'{end_year_for_hint}')
                else:
                    hint = (f'Välj slutår mellan {min_year} och '
                            f'{max_year}')
                self.mix_end_year_hint.config(text=hint)
            except tk.TclError:
                pass

        # Nollställ source-vars per familj-byte
        self._mix_source_vars = {}
        for key in varying:
            self._mix_source_vars[key] = tk.StringVar(value='Mixa')

        info_lines = [
            f'Familj {family_key}: {n_source} planer.',
            f'Identiska försäkringar: {len(identical)}. '
            f'Varierande försäkringar: {len(varying)}.',
        ]
        ttk.Label(
            self.mix_body, text='\n'.join(info_lines),
            foreground='#333', justify='left',
        ).pack(anchor='w', pady=(4, 4))
        self._mix_combos_label = ttk.Label(
            self.mix_body, text='', foreground='#333',
        )
        self._mix_combos_label.pack(anchor='w', pady=(0, 8))
        if varying:
            ttk.Label(
                self.mix_body,
                text='Varierande försäkringar (välj "Mixa" eller lås till '
                     'en specifik plan — försäkringen ingår alltid i summan):',
                font=('TkDefaultFont', 9, 'bold'),
                wraplength=800, justify='left',
            ).pack(anchor='w')

            # Bygg dropdown-värden: 'Mixa' + "Lås till <plan-namn>" per plan
            dropdown_values = ['Mixa'] + [
                f'Lås till {pname}' for pname, _ in family_tabs
            ]
            combo_width = max(len(v) for v in dropdown_values) + 2

            grid_frame = ttk.Frame(self.mix_body)
            grid_frame.pack(anchor='w', fill='x', pady=(2, 0))
            for i, key in enumerate(varying):
                name, occ = key
                disp = name if occ == 0 else f'{name} #{occ + 1}'
                var = self._mix_source_vars[key]
                var.trace_add('write', lambda *_: self._mix_recount_combos())
                var.trace_add('write', lambda *_: self._mix_mark_stale())
                ttk.Label(
                    grid_frame, text=disp, foreground='#333',
                ).grid(row=i, column=0, sticky='w', padx=(4, 12))
                ttk.Combobox(
                    grid_frame, textvariable=var, values=dropdown_values,
                    state='readonly', width=combo_width,
                ).grid(row=i, column=1, sticky='w')
        self._mix_recount_combos()

    def _mix_mark_stale(self):
        """Markera att mix-resultatet är föråldrat — kräver ny körning.
        Kallas när sökrymden eller kvar-strategi ändras."""
        # Bara relevant om ett resultat visas — annars behöver vi inte varna
        if getattr(self, '_last_mix_results', None) is None:
            return
        self._mix_stale = True
        try:
            self.mix_status.config(
                text='⚠ Ändringar sedan senaste körning — klicka "Räkna mix" för att uppdatera',
                foreground='#c0392b',
            )
        except (AttributeError, tk.TclError):
            pass
        try:
            # Använd en accent-stil på knappen för att dra ögat
            self.mix_run_btn.config(style='Accent.TButton')
        except (AttributeError, tk.TclError):
            pass

    def _mix_clear_stale(self):
        """Ta bort stale-markeringen (efter lyckad körning)."""
        self._mix_stale = False
        try:
            self.mix_status.config(foreground='#555')
        except (AttributeError, tk.TclError):
            pass
        try:
            self.mix_run_btn.config(style='TButton')
        except (AttributeError, tk.TclError):
            pass

    def _mix_recount_combos(self):
        """Uppdatera 'Kombinationer att pröva'-labeln utifrån dropdown-val
        per varierande försäkring."""
        if not hasattr(self, '_mix_combos_label'):
            return
        try:
            self._mix_combos_label.winfo_exists()
        except tk.TclError:
            return
        n_source = getattr(self, '_mix_current_n_source', 1)
        varying = getattr(self, '_mix_current_varying', [])
        active = self._active_varying_keys(varying)
        n_active = len(active)
        n_combos = n_source ** n_active if n_active else 1
        text = (
            f'Kombinationer att pröva: {n_combos} '
            f'(mixar {n_active} av {len(varying)} varierande försäkringar).'
        )
        color = '#333'
        if n_combos > 10000:
            text += '  ⚠ många kombinationer'
            color = '#a06000'
        try:
            self._mix_combos_label.config(text=text, foreground=color)
        except tk.TclError:
            pass

    def _active_varying_keys(self, varying):
        """Returnera de varierande försäkringar som är i 'Mixa'-läge."""
        out = []
        for k in varying:
            var = self._mix_source_vars.get(k)
            if var is None or var.get() == 'Mixa':
                out.append(k)
        return out

    def _locked_varying_sources(self, varying):
        """Returnera dict {key: plan_idx} för varierande som är låsta till
        en specifik plan."""
        family_tabs = getattr(self, '_mix_current_family_tabs', [])
        name_to_idx = {pname: idx for idx, (pname, _) in enumerate(family_tabs)}
        out = {}
        for k in varying:
            var = self._mix_source_vars.get(k)
            if var is None:
                continue
            val = var.get()
            if val == 'Mixa':
                continue
            # Format: "Lås till <plan-namn>"
            if val.startswith('Lås till '):
                pname = val[len('Lås till '):]
                if pname in name_to_idx:
                    out[k] = name_to_idx[pname]
        return out

    def _family_tabs(self, family_key: str):
        """Returnera lista av (name, tab) för planer i vald familj, sorterade
        på plan-namn — så template-planen (index 0) alltid är samma oavsett
        i vilken ordning användaren markerar planerna. Utan detta kunde
        mix-resultaten skifta bara pga plan-ordning."""
        y_s, m_s = family_key.split('-')
        target = (int(y_s), int(m_s))
        out = []
        for name, var in self.plan_vars.items():
            if not var.get():
                continue
            tab = next(
                (t for t in self.app.tabs
                 if t.parsed and t.parsed.plan_name == name),
                None,
            )
            if tab is None or tab.parsed.earliest_start != target:
                continue
            out.append((name, tab))
        out.sort(key=lambda item: item[0])
        return out

    def _classify_family_insurances(self, family_tabs):
        """Dela upp försäkringar i identiska (samma i alla planer) vs varierande.
        Nyckel: (namn, förekomst-index inom planen).
        Returnerar (varying_keys, identical_keys)."""
        per_plan_map = {}  # plan_idx → {key: ins}
        for pi, (_, tab) in enumerate(family_tabs):
            seen = {}
            m = {}
            for ins in tab.parsed.insurances:
                k = seen.get(ins.name, 0)
                seen[ins.name] = k + 1
                m[(ins.name, k)] = ins
            per_plan_map[pi] = m

        all_keys = set()
        for m in per_plan_map.values():
            all_keys.update(m.keys())

        def _sig(ins):
            # Bara start + duration räknas för att avgöra om en försäkring
            # varierar mellan planer. Belopp-skillnader mellan planer med
            # samma uttags-konfiguration är oftast bara minpension.se's
            # avrundningar och skapar en meningslös sökrymds-explosion.
            if ins is None:
                return None
            return (ins.start, ins.duration)

        varying, identical = [], []
        for key in sorted(all_keys):
            sigs = {_sig(per_plan_map[pi].get(key)) for pi in per_plan_map}
            if len(sigs) == 1:
                identical.append(key)
            else:
                varying.append(key)
        return varying, identical

    def _run_mix(self):
        """Kör mix-optimering på vald start-familj — chunked med progress."""
        fam = self.mix_family_var.get()
        if not fam:
            return
        family_key = fam.split('  ')[0]
        family_tabs = self._family_tabs(family_key)
        if len(family_tabs) < 2:
            return

        varying_all, identical = self._classify_family_insurances(family_tabs)
        # Filtrera till bara de varierande försäkringar användaren markerat
        # för mix. Låsta varierande ingår ändå i summeringen via base med
        # den valda plan-källan (default plan 0).
        varying = self._active_varying_keys(varying_all)
        locked_source = self._locked_varying_sources(varying_all)
        n_source = len(family_tabs)
        n_combos = n_source ** len(varying) if varying else 1
        if n_combos == 0:
            return
        if n_combos > 50000:
            if not messagebox.askyesno(
                'Många kombinationer',
                f'{n_combos} kombinationer kan ta lång tid. Fortsätta?',
                parent=self.top,
            ):
                return

        # Setup: progressbar + avbryt-knapp ersätter Räkna mix-knappen under körning
        self._mix_gen = getattr(self, '_mix_gen', 0) + 1
        my_gen = self._mix_gen
        self.mix_run_btn.config(state='disabled')
        self._mix_clear_stale()  # rensa varning — vi räknar nu
        # Stäng ev. öppen fördelnings-popup — nya körningen ger nya iid:n så
        # popupen skulle bli föräldralös (kan inte destroyas via hover-flödet)
        self._destroy_mix_hover_tip()
        self._mix_hover_row = None
        self.mix_status.config(text=f'Räknar 0 av {n_combos} kombinationer…')

        # Skapa progress + avbryt-widgets (rensa gamla först)
        if hasattr(self, '_mix_progress'):
            try:
                self._mix_progress.destroy()
            except tk.TclError:
                pass
        if hasattr(self, '_mix_cancel_btn'):
            try:
                self._mix_cancel_btn.destroy()
            except tk.TclError:
                pass
        self._mix_progress = ttk.Progressbar(
            self.mix_status.master, mode='determinate',
            maximum=n_combos, value=0, length=200,
        )
        self._mix_progress.grid(row=4, column=0, sticky='w', pady=(4, 0))
        self._mix_cancel_btn = ttk.Button(
            self.mix_status.master, text='Avbryt',
            command=self._cancel_mix,
        )
        self._mix_cancel_btn.grid(
            row=4, column=1, sticky='w', pady=(4, 0), padx=(8, 0),
        )
        self.top.update_idletasks()

        # Förbered gemensam beräknings-state
        state = self._prepare_mix_state(family_tabs, varying, locked_source)
        state['gen'] = my_gen
        state['family_tabs'] = family_tabs
        state['varying'] = varying
        state['n_combos'] = n_combos
        state['results'] = []
        state['idx'] = 0

        # Kör chunkat via after()
        self.top.after(0, lambda: self._mix_step(state))

    def _on_compare_kvar_change(self):
        """Trace-callback för gemensam kvar-strategi: spara i state + re-render."""
        try:
            state = self.app._read_state()
            state['compare_kvar_strategy'] = int(self.compare_kvar_var.get())
            import json as _json
            with open(STATE_FILE, 'w', encoding='utf-8') as f:
                _json.dump(state, f, indent=2, ensure_ascii=False)
        except (ValueError, tk.TclError, OSError):
            pass
        try:
            self._rebuild_ranking()
        except (AttributeError, tk.TclError):
            pass

    def _on_compare_end_year_change(self):
        """Trace-callback för gemensam slutårs-variabel: uppdatera båda
        flikarna och spara i state-filen."""
        # Uppdatera state-filen (global inställning)
        try:
            state = self.app._read_state()
            state['compare_end_year'] = int(self.compare_end_year_var.get())
            import json as _json
            with open(STATE_FILE, 'w', encoding='utf-8') as f:
                _json.dump(state, f, indent=2, ensure_ascii=False)
        except (ValueError, tk.TclError, OSError):
            pass
        # Re-render båda flikarna (spinboxen som ändrats har egen
        # command-callback som täcker en av dem, men trace körs efter
        # spinbox-command så vi säkerställer båda uppdateras)
        try:
            self._rebuild_ranking()
        except (AttributeError, tk.TclError):
            pass
        try:
            self._on_mix_end_year_change()
        except (AttributeError, tk.TclError):
            pass

    def _on_mix_end_year_change(self):
        """Slutår ändrades — rendera om resultaten (utan att räkna om mixen)
        och uppdatera hint-texten med ålder vid slutåret."""
        try:
            self._mix_end_year_var_clamp()
        except tk.TclError:
            return
        # Uppdatera hint även utan resultat (t.ex. innan mixen körts)
        try:
            end_year = int(self.mix_end_year_var.get())
            min_y = getattr(self, '_mix_min_year', end_year)
            max_y = getattr(self, '_mix_max_year', end_year)
            age_at_end = None
            fam_tabs = getattr(self, '_mix_current_family_tabs', []) or []
            for _, tab in fam_tabs:
                if not tab.calc or not tab.calc.yearly:
                    continue
                for yy in tab.calc.yearly:
                    if yy.year == end_year:
                        age_at_end = yy.age
                        break
                if age_at_end is not None:
                    break
            if age_at_end is not None:
                hint = (f'Välj slutår mellan {min_y} och {max_y}; '
                        f'ålder {age_at_end} år vid slutår {end_year}')
            else:
                hint = f'Välj slutår mellan {min_y} och {max_y}'
            self.mix_end_year_hint.config(text=hint)
        except (ValueError, tk.TclError, AttributeError):
            pass
        # Re-rendera resultat-tabellen om vi har resultat att visa
        if getattr(self, '_last_mix_results', None) is None:
            return
        self._show_mix_results(
            self._last_mix_results,
            self._last_mix_family_tabs,
            self._last_mix_varying_keys,
        )

    def _mix_end_year_var_clamp(self):
        """Kläm slutår-värdet inom giltigt intervall."""
        min_y = getattr(self, '_mix_min_year', 2020)
        max_y = getattr(self, '_mix_max_year', 2100)
        try:
            y = int(self.mix_end_year_var.get())
        except (ValueError, tk.TclError):
            self.mix_end_year_var.set(max_y)
            return
        clamped = max(min_y, min(max_y, y))
        if clamped != y:
            self.mix_end_year_var.set(clamped)

    def _mix_metrics_at(self, vals, years, end_year):
        """Räkna slutvärde/lägsta/summa fram t.o.m. end_year.
        Returnerar dict eller None om ingen data <= end_year."""
        idx_end = None
        for i, y in enumerate(years):
            if y <= end_year:
                idx_end = i
            else:
                break
        if idx_end is None:
            return None
        v = vals[:idx_end + 1]
        y = years[:idx_end + 1]
        i_min = min(range(len(v)), key=lambda k: v[k])
        return {
            'slutvarde': v[-1],
            'slut_year': y[-1],
            'lagsta': v[i_min],
            'lagsta_ar': y[i_min],
            'total': sum(v),
        }

    def _render_mix_self_test(self, family_tabs, end_year):
        """Kör mix-approximationen mot varje riktig plan i familjen och
        jämför slutvärden. Visar diff i mix_body så användaren ser
        approximationens felmarginal."""
        if not family_tabs:
            return
        brackets = self.app.get_brackets() or []
        sparande = self.app.get_sparande() or 0.0
        growth_pct = self.app.get_growth_pct() or 0.0
        growth_mode = self.app.get_growth_mode()
        kvar_idx = self.mix_kvar_var.get()
        attr = ('kvar_1', 'kvar_2', 'kvar_3')[kvar_idx]

        # Template = första planen (samma val som mix-motorn använder)
        template_parsed = family_tabs[0][1].parsed

        # Bygg per-plan-map på samma sätt som _run_mix
        per_plan_map = []
        for _, tab in family_tabs:
            seen = {}
            m = {}
            for ins in tab.parsed.insurances:
                k = seen.get(ins.name, 0)
                seen[ins.name] = k + 1
                m[(ins.name, k)] = ins
            per_plan_map.append(m)

        rows = []
        diffs_pct = []
        for pi, (pname, tab) in enumerate(family_tabs):
            if not tab.calc or not tab.calc.yearly:
                continue
            # Verkligt slutvärde för denna plan vid end_year
            real_vals = [getattr(y, attr) for y in tab.calc.yearly]
            real_years = [y.year for y in tab.calc.yearly]
            real_m = self._mix_metrics_at(real_vals, real_years, end_year)
            if real_m is None:
                continue
            real_end = real_m['slutvarde']

            # Bygg mix-config där varje försäkring tas från denna plan
            chosen = {}
            for key, ins in per_plan_map[pi].items():
                chosen[key] = (pi, ins)
            try:
                synth = self._synthesize_mix_plan(
                    chosen, family_tabs, template_parsed,
                )
                calc = calculate_plan(
                    synth, sparande, brackets, None,
                    growth_pct, growth_mode,
                )
            except Exception:
                continue
            if not calc.yearly:
                continue
            mix_vals = [getattr(y, attr) for y in calc.yearly]
            mix_years = [y.year for y in calc.yearly]
            mix_m = self._mix_metrics_at(mix_vals, mix_years, end_year)
            if mix_m is None:
                continue
            mix_end = mix_m['slutvarde']
            diff = mix_end - real_end
            base = abs(real_end) if abs(real_end) > 1e-6 else 1.0
            diff_pct = 100.0 * diff / base
            rows.append((pname, real_end, mix_end, diff, diff_pct))
            diffs_pct.append(diff_pct)

        if not rows:
            return

        # Bygg text-blurb med monospace-font
        max_name = min(35, max(len(r[0]) for r in rows))
        lines = []
        lines.append(
            'Approximationens kvalitet (self-test) — varje plan mixad med '
            'sina EGNA configs bör ge samma slutvärde som originalet:'
        )
        header = (
            f'  {"Plan":<{max_name}} '
            f'{"Verkligt":>13} '
            f'{"Mix-approx":>13} '
            f'{"Diff":>13} '
            f'{"%":>7}'
        )
        lines.append(header)
        lines.append('  ' + '-' * (max_name + 3 + 13 + 1 + 13 + 1 + 13 + 1 + 7))
        for pname, real_end, mix_end, diff, diff_pct in rows:
            name_show = pname if len(pname) <= max_name else pname[:max_name - 1] + '…'
            diff_str = f'{diff:+,.0f}'.replace(',', '\xa0')
            lines.append(
                f'  {name_show:<{max_name}} '
                f'{fmt_kr(real_end):>13} '
                f'{fmt_kr(mix_end):>13} '
                f'{diff_str:>13} '
                f'{diff_pct:>+6.1f}%'
            )
        # Sammanfattning
        abs_diffs = [abs(d) for d in diffs_pct]
        mean_abs = sum(abs_diffs) / len(abs_diffs)
        max_abs = max(abs_diffs)
        lines.append('')
        lines.append(
            f'  Medelfel: ±{mean_abs:.1f}%   '
            f'Max fel: ±{max_abs:.1f}%   '
            f'(tolka mix-slutvärden med denna felmarginal)'
        )

        # Färg baserat på max-fel: <2% grön, <5% gul, annars röd
        if max_abs < 2:
            fg = '#0a6b0a'
        elif max_abs < 5:
            fg = '#8a6a00'
        else:
            fg = '#a00'

        lbl = tk.Label(
            self.mix_body,
            text='\n'.join(lines),
            font=('Consolas', 9),
            justify='left',
            anchor='w',
            fg=fg,
            bg='#f8f8f8',
            relief='solid',
            borderwidth=1,
            padx=6,
            pady=4,
        )
        lbl._mix_result = True
        lbl.pack(anchor='w', fill='x', pady=(0, 6))

    def _cancel_mix(self):
        """Bryt pågående mix-körning genom att invalidera gen-räknaren."""
        self._mix_gen = getattr(self, '_mix_gen', 0) + 1
        self.mix_status.config(text='Avbruten.')
        self._finish_mix_ui()

    def _finish_mix_ui(self):
        """Städa upp progressbar/avbryt-widgets när körning slutförs eller avbryts."""
        self.mix_run_btn.config(state='normal')
        if hasattr(self, '_mix_progress'):
            try:
                self._mix_progress.destroy()
            except tk.TclError:
                pass
        if hasattr(self, '_mix_cancel_btn'):
            try:
                self._mix_cancel_btn.destroy()
            except tk.TclError:
                pass

    def _prepare_mix_state(self, family_tabs, varying_keys, locked_source=None):
        """Bygg per-plan-mappar och choice_space en gång så step-funktionen kan
        återanvända dem utan att göra om jobbet varje varv. locked_source =
        dict {key: plan_idx} för varierande försäkringar som ska låsas till
        en specifik plan istället för att mixas."""
        import itertools
        locked_source = locked_source or {}
        template_name, template_tab = family_tabs[0]
        template_parsed = template_tab.parsed
        per_plan_map = []
        for _, tab in family_tabs:
            seen = {}
            m = {}
            for ins in tab.parsed.insurances:
                k = seen.get(ins.name, 0)
                seen[ins.name] = k + 1
                m[(ins.name, k)] = ins
            per_plan_map.append(m)
        base_ins_by_key = {}
        for k in per_plan_map[0].keys():
            if k in varying_keys:
                continue
            # Låst varierande: hämta från specificerad plan (fallback plan 0)
            src_idx = locked_source.get(k, 0)
            if k in per_plan_map[src_idx]:
                base_ins_by_key[k] = (src_idx, per_plan_map[src_idx][k])
            else:
                base_ins_by_key[k] = (0, per_plan_map[0][k])
        brackets = self.app.get_brackets() or []
        sparande = self.app.get_sparande() or 0.0
        growth_pct = self.app.get_growth_pct() or 0.0
        growth_mode = self.app.get_growth_mode()
        choice_space = list(itertools.product(
            range(len(family_tabs)), repeat=len(varying_keys),
        ))
        return {
            'template_parsed': template_parsed,
            'per_plan_map': per_plan_map,
            'base_ins_by_key': base_ins_by_key,
            'brackets': brackets,
            'sparande': sparande,
            'growth_pct': growth_pct,
            'growth_mode': growth_mode,
            'choice_space': choice_space,
        }

    def _mix_step(self, state):
        """Kör en batch av kombinationer, uppdatera progress, schemalägg nästa."""
        # Avbruten? (någon inkrementerade _mix_gen efter denna körning startades)
        if state['gen'] != self._mix_gen:
            return
        CHUNK = 100
        varying = state['varying']
        per_plan_map = state['per_plan_map']
        base = state['base_ins_by_key']
        family_tabs = state['family_tabs']
        template_parsed = state['template_parsed']
        choice_space = state['choice_space']
        results = state['results']
        n_combos = state['n_combos']
        kvar_idx = self.mix_kvar_var.get()
        attr = ('kvar_1', 'kvar_2', 'kvar_3')[kvar_idx]

        end = min(state['idx'] + CHUNK, len(choice_space))
        try:
            for i in range(state['idx'], end):
                choices = choice_space[i]
                chosen = dict(base)
                skip = False
                for var_i, plan_idx in enumerate(choices):
                    key = varying[var_i]
                    if key not in per_plan_map[plan_idx]:
                        skip = True
                        break
                    chosen[key] = (plan_idx, per_plan_map[plan_idx][key])
                if skip:
                    continue
                synth = self._synthesize_mix_plan(
                    chosen, family_tabs, template_parsed,
                )
                calc = calculate_plan(
                    synth, state['sparande'], state['brackets'], None,
                    state['growth_pct'], state['growth_mode'],
                )
                if not calc.yearly:
                    continue
                vals = [getattr(y, attr) for y in calc.yearly]
                years = [y.year for y in calc.yearly]
                results.append({
                    'choices': choices,
                    'vals': vals,
                    'years': years,
                })
        except Exception as e:
            _show_error_copyable(
                self.top, 'Mix-fel', f'Kunde inte räkna: {e}',
            )
            self._mix_gen += 1  # invalidera
            self.mix_status.config(text='Fel — se dialog.')
            self._finish_mix_ui()
            return

        state['idx'] = end
        try:
            self._mix_progress.config(value=end)
        except (tk.TclError, AttributeError):
            pass
        pct = int(100 * end / n_combos) if n_combos else 100
        self.mix_status.config(
            text=f'Räknar {end} av {n_combos} kombinationer ({pct} %)…',
        )

        if end < len(choice_space):
            # Yield till event-loopen så avbryt-klick kan behandlas
            self.top.after(1, lambda: self._mix_step(state))
        else:
            # Klar
            self.mix_status.config(
                text=f'Klar — {len(results)} giltiga kombinationer.',
            )
            self._finish_mix_ui()
            self._show_mix_results(
                results, state['family_tabs'], state['varying'],
            )

    def _compute_all_mixes(self, family_tabs, varying_keys):
        """Enumeraera alla kombinationer och räkna kvar-metriker för varje.
        Använder första planen som template för skatte-satser och födelsedata."""
        import itertools

        template_name, template_tab = family_tabs[0]
        template_parsed = template_tab.parsed
        # Indexerade ins-listor per plan
        per_plan_map = []
        for _, tab in family_tabs:
            seen = {}
            m = {}
            for ins in tab.parsed.insurances:
                k = seen.get(ins.name, 0)
                seen[ins.name] = k + 1
                m[(ins.name, k)] = ins
            per_plan_map.append(m)
        # För icke-varierande försäkringar: använd template-planens (första)
        base_ins_by_key = {}
        for k in per_plan_map[0].keys():
            if k not in varying_keys:
                base_ins_by_key[k] = (0, per_plan_map[0][k])

        # Global brackets och sparande
        brackets = self.app.get_brackets() or []
        sparande = self.app.get_sparande() or 0.0
        growth_pct = self.app.get_growth_pct() or 0.0
        growth_mode = self.app.get_growth_mode()

        results = []
        # Enumeraera alla val: varje varierande försäkring kan komma från vilken som helst av N planer
        choice_space = list(itertools.product(
            range(len(family_tabs)), repeat=len(varying_keys),
        ))

        for choices in choice_space:
            # Bygg chosen_ins_by_key
            chosen = dict(base_ins_by_key)
            for var_i, plan_idx in enumerate(choices):
                key = varying_keys[var_i]
                if key not in per_plan_map[plan_idx]:
                    # Nyckeln saknas i den valda planen — hoppa denna kombination
                    chosen = None
                    break
                chosen[key] = (plan_idx, per_plan_map[plan_idx][key])
            if chosen is None:
                continue

            # Syntetisera ParsedPlan
            synth = self._synthesize_mix_plan(
                chosen, family_tabs, template_parsed,
            )
            calc = calculate_plan(
                synth, sparande, brackets, None, growth_pct, growth_mode,
            )
            if not calc.yearly:
                continue
            kvar_idx = self.mix_kvar_var.get()
            attr = ('kvar_1', 'kvar_2', 'kvar_3')[kvar_idx]
            vals = [getattr(y, attr) for y in calc.yearly]
            years = [y.year for y in calc.yearly]
            i_min = min(range(len(vals)), key=lambda i: vals[i])
            results.append({
                'choices': choices,
                'slutvarde': vals[-1],
                'lagsta': vals[i_min],
                'lagsta_ar': years[i_min],
                'total': sum(vals),
            })
        return results

    def _synthesize_mix_plan(self, chosen_ins_by_key, family_tabs, template_parsed):
        """Bygg en syntetisk ParsedPlan från valda försäkringar.
        Breakpoints = union av datum från alla involverade källplaner.
        Före-skatt = sum av amounts vid varje datum. Efter-skatt = template-planens
        skattefaktor tillämpad på totalen."""
        # Samla alla brytpunkts-datum
        all_dates = set()
        for _, tab in family_tabs:
            for bp in tab.parsed.breakpoints:
                all_dates.add(bp.date)
        all_dates = sorted(all_dates)

        # Backward-fill skattesats från template
        tmpl_bps = template_parsed.breakpoints
        tmpl_rates = [None] * len(tmpl_bps)
        for i, bp in enumerate(tmpl_bps):
            if bp.before_tax and bp.after_tax is not None and bp.before_tax > 0:
                tmpl_rates[i] = 1.0 - (bp.after_tax / bp.before_tax)
        last_known = None
        for i in reversed(range(len(tmpl_bps))):
            if tmpl_rates[i] is not None:
                last_known = tmpl_rates[i]
            elif last_known is not None:
                tmpl_rates[i] = last_known

        def _amount_from_source(ins, source_tab, date):
            """Amount from `ins` at `date` given its source plan's breakpoints."""
            if not ins.amounts_before_tax:
                return 0
            bps = source_tab.parsed.breakpoints
            idx = None
            for i, bp in enumerate(bps):
                if bp.date <= date:
                    idx = i
                else:
                    break
            if idx is None or idx >= len(ins.amounts_before_tax):
                return 0
            return ins.amounts_before_tax[idx] or 0

        def _tax_rate_at(date):
            idx = None
            for i, bp in enumerate(tmpl_bps):
                if bp.date <= date:
                    idx = i
                else:
                    break
            if idx is None:
                return None
            return tmpl_rates[idx]

        # Bygg breakpoints
        new_bps = []
        for d in all_dates:
            total_before = 0
            for key, (plan_idx, ins) in chosen_ins_by_key.items():
                _, source_tab = family_tabs[plan_idx]
                total_before += _amount_from_source(ins, source_tab, d)
            rate = _tax_rate_at(d)
            if rate is not None and total_before > 0:
                total_after = round(total_before * (1 - rate))
            else:
                total_after = None
            new_bps.append(Breakpoint(
                date=d, before_tax=int(total_before), after_tax=total_after,
            ))

        # Insurances: kopior med korrekta amounts per nya bp-datum
        new_ins = []
        for key, (plan_idx, ins) in chosen_ins_by_key.items():
            _, source_tab = family_tabs[plan_idx]
            new_amounts = [
                _amount_from_source(ins, source_tab, d) or 0
                for d in all_dates
            ]
            new_ins.append(Insurance(
                name=ins.name, start=ins.start, duration=ins.duration,
                amounts_before_tax=new_amounts,
            ))

        return ParsedPlan(
            plan_name=f'MIX ({template_parsed.plan_name})',
            insurances=new_ins,
            breakpoints=new_bps,
            birth_year=template_parsed.birth_year,
            birth_month=template_parsed.birth_month,
            earliest_start=template_parsed.earliest_start,
        )

    def _on_mix_row_hover(self, event):
        """Visa popup-fönster med hela fördelningen för raden."""
        tree = getattr(self, '_mix_tree', None)
        if tree is None:
            return
        row_id = tree.identify_row(event.y)
        if not row_id:
            self._on_mix_row_leave(event)
            return
        if row_id == getattr(self, '_mix_hover_row', None):
            return
        info = self._mix_row_info.get(row_id)
        if not info:
            return
        self._mix_hover_row = row_id
        try:
            self._render_mix_detail(info, event)
        except Exception:
            pass

    def _build_mix_detail_text(self, info):
        """Bygg fördelnings-text för given rad-info. Returnerar None vid fel."""
        family_tabs = self._mix_family_tabs_for_hover
        per_plan_map = self._mix_per_plan_map_disp
        varying_keys = self._mix_varying_keys_for_hover

        ins_with_source = []
        if info['kind'] == 'ref':
            pi = info['plan_idx']
            for ins in family_tabs[pi][1].parsed.insurances:
                ins_with_source.append((ins, family_tabs[pi][0]))
        else:
            choices = info['choices']
            var_source = {
                varying_keys[i]: pi for i, pi in enumerate(choices)
            }
            for key, ins in per_plan_map[0].items():
                pi = var_source.get(key, 0)
                if key in per_plan_map[pi]:
                    ins_with_source.append(
                        (per_plan_map[pi][key], family_tabs[pi][0]),
                    )
                else:
                    ins_with_source.append((ins, family_tabs[0][0]))

        def _fmt_ym(ym):
            return f'{ym[0]}-{ym[1]:02d}' if ym else '?'

        def _fmt_dur(dur):
            if dur is None:
                return 'Livet ut'
            y, m = dur
            if y and m:
                return f'{y} år {m} mån'
            if y:
                return f'{y} år'
            if m:
                return f'{m} mån'
            return '—'

        varying_names = {k[0] for k in varying_keys}
        # Inställnings-rad överst med kontext för beräkningen
        try:
            kvar_label = KVAR_LABELS[self.compare_kvar_var.get()]
        except (IndexError, tk.TclError, AttributeError):
            kvar_label = '?'
        try:
            set_num = self.app.active_set_var.get() + 1
        except (tk.TclError, AttributeError):
            set_num = '?'
        try:
            sparande = self.app.get_sparande() or 0
            sparande_s = f'{fmt_kr(sparande)} kr'
        except (AttributeError, TypeError):
            sparande_s = '?'
        try:
            growth = self.app.get_growth_pct() or 0
            if growth > 0:
                growth_s = f'{growth:g} %'.replace('.', ',')
            else:
                growth_s = 'ingen'
        except (AttributeError, TypeError):
            growth_s = '?'
        try:
            end_year = int(self.compare_end_year_var.get())
            # Hitta åldern vid slutåret (från första valda plans calc)
            age_at_end = None
            for _, tab in (family_tabs or []):
                if not tab.calc or not tab.calc.yearly:
                    continue
                for yy in tab.calc.yearly:
                    if yy.year == end_year:
                        age_at_end = yy.age
                        break
                if age_at_end is not None:
                    break
            if age_at_end is not None:
                end_year_s = f'{end_year} (ålder {age_at_end} år)'
            else:
                end_year_s = str(end_year)
        except (ValueError, tk.TclError, AttributeError):
            end_year_s = '?'

        # Räkna ut närmast originalplan tidigt så vi kan markera avvikare i
        # fördelnings-listan nedan
        closest_names = []
        closest_max_c = 0
        closest_total = 0
        closest_source_names = set()  # plan-namn som räknas som "närmast"
        if info.get('kind') == 'mix' and family_tabs:
            choices = info.get('choices') or ()
            counts = {}
            for pi in choices:
                counts[pi] = counts.get(pi, 0) + 1
            if counts:
                closest_max_c = max(counts.values())
                closest_idxs = [
                    pi for pi, c in counts.items() if c == closest_max_c
                ]
                closest_names = [family_tabs[pi][0] for pi in closest_idxs]
                closest_source_names = set(closest_names)
                closest_total = len(choices)

        lines = [
            'Inställningar:',
            f'  Kvar-strategi: {kvar_label}',
            f'  Utgiftsset: Set {set_num}',
            f'  Ursprungssparande: {sparande_s}',
            f'  Tillväxt: {growth_s}',
            f'  Slutår för jämförelse: {end_year_s}',
            '',
            f'Fördelning för {info["label"]}:',
            '',
        ]
        name_w = max((len(ins.name) for ins, _ in ins_with_source), default=20)
        for ins, src in ins_with_source:
            is_var = ins.name in varying_names
            # För mix-rader: markera varying-försäkringar där källan AVVIKER
            # från närmast originalplan med ⚠ istället för ●
            if is_var and closest_source_names and src not in closest_source_names:
                marker = '* '
            elif is_var:
                marker = '● '
            else:
                marker = '  '
            name_col = f'{ins.name:<{name_w}}'
            sd = f'{_fmt_ym(ins.start)} · {_fmt_dur(ins.duration)}'
            lines.append(f'{marker}{name_col}   {sd:<22}   {src}')
        # Metric-rad efter fördelningen
        m = info.get('metrics')
        if m:
            lines.append('')
            lines.append(
                f'Slutvärde: {fmt_kr(m["slutvarde"])} kr   '
                f'Lägsta: {fmt_kr(m["lagsta"])} kr   '
                f'År: {m["lagsta_ar"]}   '
                f'Summa: {fmt_kr(m["total"])} kr'
            )
        # Sammanfattning av närmast originalplan (efter markeringarna ovan)
        if closest_names:
            if len(closest_names) == 1:
                lines.append(
                    f'Närmast originalplan: {closest_names[0]} '
                    f'(matchar {closest_max_c} av {closest_total} mixade '
                    f'försäkringar; * = avviker)'
                )
            else:
                lines.append(
                    f'Närmast originalplaner (matchar {closest_max_c} av '
                    f'{closest_total}; * = avviker): '
                    + ', '.join(closest_names)
                )
        return '\n'.join(lines)

    def _render_mix_detail(self, info, event=None):
        """Visa fördelnings-popup för given rad-info."""
        text = self._build_mix_detail_text(info)
        if text is None:
            return
        # Destroya ev. tidigare popup och skapa ny nära muspekaren
        self._destroy_mix_hover_tip()
        tw = tk.Toplevel(self.top)
        tw.wm_overrideredirect(True)
        tk.Label(
            tw, text=text, justify='left', anchor='w',
            font=('Consolas', 9),
            background='#ffffe0', relief='solid', borderwidth=1,
            padx=8, pady=6,
        ).pack()
        if event is not None:
            x = event.x_root + 14
            y = event.y_root + 10
        else:
            x, y = 100, 100
        tw.wm_geometry(f'+{x}+{y}')
        try:
            tw.attributes('-topmost', True)
        except tk.TclError:
            pass
        tw.lift()
        self._mix_hover_tip = tw

    def _on_mix_row_leave(self, event=None):
        self._mix_hover_row = None
        self._destroy_mix_hover_tip()

    def _on_mix_row_right_click(self, event):
        """Högerklick på rad: kopiera fördelnings-texten till urklipp."""
        tree = getattr(self, '_mix_tree', None)
        if tree is None:
            return
        row_id = tree.identify_row(event.y)
        if not row_id:
            return
        info = self._mix_row_info.get(row_id)
        if not info:
            return
        text = self._build_mix_detail_text(info)
        if text is None:
            return
        try:
            self.top.clipboard_clear()
            self.top.clipboard_append(text)
            self.top.update()  # tvinga fram uppdatering till OS-clipboard
        except tk.TclError:
            return
        # Kort feedback: ändra popup-etiketten tillfälligt
        try:
            tw = self._mix_hover_tip
            if tw is not None:
                for child in tw.winfo_children():
                    if isinstance(child, tk.Label):
                        cur = child.cget('text')
                        child.config(
                            text='✓ Kopierat till urklipp!\n\n' + cur,
                        )
                        break
        except tk.TclError:
            pass


    def _destroy_mix_hover_tip(self):
        tw = getattr(self, '_mix_hover_tip', None)
        if tw is not None:
            try:
                tw.destroy()
            except tk.TclError:
                pass
            self._mix_hover_tip = None

    def _show_mix_results(self, results, family_tabs, varying_keys):
        """Rendera top-20 kombinationer sorterade på slutvärde vid valt slutår.
        Sparar också results/family_tabs/varying_keys så att slutår-ändring kan
        re-rendera utan ny kalkyl."""
        # Stäng ev. öppen fördelnings-popup — gammalt tree destrueras nedan
        # och popup:en skulle bli hängande utan tillhörande iid
        self._destroy_mix_hover_tip()
        self._mix_hover_row = None
        # Rensa gamla resultat under family-info
        for w in list(self.mix_body.winfo_children()):
            if getattr(w, '_mix_result', False):
                w.destroy()

        # Behåll för framtida slutår-re-render
        self._last_mix_results = results
        self._last_mix_family_tabs = family_tabs
        self._last_mix_varying_keys = varying_keys

        if not results:
            lbl = ttk.Label(
                self.mix_body, text='Inga giltiga kombinationer.',
                foreground='#a00',
            )
            lbl._mix_result = True
            lbl.pack(anchor='w', pady=(8, 0))
            return

        # Räkna metriker vid valt slutår
        try:
            end_year = int(self.mix_end_year_var.get())
        except (ValueError, tk.TclError):
            end_year = getattr(self, '_mix_max_year', 9999)

        # Uppdatera hint med ålder vid slutåret
        age_at_end = None
        for _, tab in family_tabs:
            if not tab.calc or not tab.calc.yearly:
                continue
            for yy in tab.calc.yearly:
                if yy.year == end_year:
                    age_at_end = yy.age
                    break
            if age_at_end is not None:
                break
        try:
            min_y = getattr(self, '_mix_min_year', end_year)
            max_y = getattr(self, '_mix_max_year', end_year)
            if age_at_end is not None:
                self.mix_end_year_hint.config(
                    text=f'Välj slutår mellan {min_y} och {max_y}; '
                         f'ålder {age_at_end} år vid slutår {end_year}',
                )
            else:
                self.mix_end_year_hint.config(
                    text=f'Välj slutår mellan {min_y} och {max_y}',
                )
        except (AttributeError, tk.TclError):
            pass

        computed = []
        for r in results:
            m = self._mix_metrics_at(r['vals'], r['years'], end_year)
            if m is None:
                continue
            computed.append({**r, **m})
        if not computed:
            lbl = ttk.Label(
                self.mix_body,
                text=f'Inga resultat med data t.o.m. år {end_year}.',
                foreground='#a00',
            )
            lbl._mix_result = True
            lbl.pack(anchor='w', pady=(8, 0))
            return

        computed.sort(key=lambda r: r['slutvarde'], reverse=True)
        # Dedup:a på slutvärde (avrundat till närmaste 100 kr för att fånga
        # avrundningsvariation). För varje unikt slutvärde behåll första
        # kombinationen som representant + räknare för hur många kombinationer
        # som gav samma nivå.
        grouped = []
        seen_key = None
        for r in computed:
            key = round(r['slutvarde'] / 100) * 100
            if grouped and grouped[-1]['_key'] == key:
                grouped[-1]['_count'] += 1
            else:
                grouped.append({**r, '_key': key, '_count': 1})
        top = grouped[:20]
        best = top[0]['slutvarde']

        header = ttk.Label(
            self.mix_body,
            text=(
                f'Slutår för jämförelse: {end_year}. '
                f'Först originalplanerna som referens, sen top {len(top)} '
                f'mix-kombinationer sorterade på kvar-värdet vid slutåret '
                f'(#1 = bäst).'
            ),
            font=('TkDefaultFont', 9, 'bold'),
        )
        header._mix_result = True
        header.pack(anchor='w', pady=(12, 4))

        # Self-test: hur bra approximeras hela planer av mix-modellen?
        # För varje riktig plan: bygg mix med den planens egna configs och
        # jämför mot planens verkliga slutvärde. Visar approximationens
        # felmarginal så användaren vet vad de kan lita på.
        try:
            self._render_mix_self_test(family_tabs, end_year)
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            try:
                with open('selftest_debug.log', 'a', encoding='utf-8') as f:
                    f.write(f'--- self-test call failed ---\n{tb}\n')
            except OSError:
                pass
            # Visa åtminstone en fel-etikett så användaren ser att något gick fel
            err_lbl = ttk.Label(
                self.mix_body,
                text=f'(Self-test misslyckades: {e})',
                foreground='#a00',
                font=('TkDefaultFont', 9, 'italic'),
            )
            err_lbl._mix_result = True
            err_lbl.pack(anchor='w', pady=(4, 0))

        # Tabell: # | Slut | Lägsta | År | Summa | Δ | Val per varierande försäkring
        cols = ['pos', 'slutvarde', 'count', 'lagsta', 'lagsta_ar', 'total', 'diff']
        for i, key in enumerate(varying_keys):
            cols.append(f'v{i}')
        headers = {
            'pos': '#', 'slutvarde': 'Slutvärde',
            'count': 'Antal',
            'lagsta': 'Lägsta', 'lagsta_ar': 'År',
            'total': 'Summa', 'diff': 'Δ #1',
        }
        for i, key in enumerate(varying_keys):
            name, occ = key
            disp = name if occ == 0 else f'{name} #{occ + 1}'
            # Trunkera för header
            if len(disp) > 22:
                disp = disp[:20] + '…'
            headers[f'v{i}'] = disp
        widths = {
            'pos': 140, 'slutvarde': 110, 'count': 55,
            'lagsta': 110, 'lagsta_ar': 55, 'total': 130, 'diff': 100,
        }
        for i in range(len(varying_keys)):
            widths[f'v{i}'] = 200

        container = ttk.Frame(self.mix_body)
        container._mix_result = True
        container.pack(fill='both', expand=True, pady=(0, 4))
        tree = ttk.Treeview(
            container, columns=cols, show='headings',
            height=min(len(top) + len(family_tabs) + 2, 18),
        )
        for c in cols:
            tree.heading(c, text=headers[c])
            anchor = 'center' if c == 'lagsta_ar' else (
                'e' if c in ('slutvarde', 'lagsta', 'total', 'diff') else 'w'
            )
            tree.column(c, width=widths[c], minwidth=widths[c], anchor=anchor, stretch=False)
        tree.tag_configure('best', background='#e0f0d0', font=('TkDefaultFont', 9, 'bold'))
        tree.tag_configure('row_a', background='#ffffff')
        tree.tag_configure('row_b', background='#f5f5f5')
        tree.tag_configure(
            'ref', background='#e6f0ff', font=('TkDefaultFont', 9, 'italic'),
        )

        vsb = ttk.Scrollbar(container, orient='vertical', command=tree.yview)
        hsb = ttk.Scrollbar(container, orient='horizontal', command=tree.xview)
        tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        tree.grid(row=0, column=0, sticky='nsew')
        vsb.grid(row=0, column=1, sticky='ns')
        hsb.grid(row=1, column=0, sticky='ew')
        container.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)

        # Fullständigt plan-namn för val-cellerna — inte bara "8 år"-suffixet.
        # Visa försäkringens faktiska uttagsstart och längd i val-cellerna,
        # inte källplanens namn — det är det som är intressant när man ska
        # skapa en ny plan på minpension.se.
        plan_labels = [pname for pname, _ in family_tabs]

        def _fmt_start_dur(ins):
            if ins is None:
                return '—'
            start = (f'{ins.start[0]}-{ins.start[1]:02d}'
                     if ins.start else '?')
            if ins.duration is None:
                dur = 'Livet ut'
            else:
                y, m = ins.duration
                if y and m:
                    dur = f'{y} år {m} mån'
                elif y:
                    dur = f'{y} år'
                elif m:
                    dur = f'{m} mån'
                else:
                    dur = '—'
            return f'{start} · {dur}'

        # Per-plan-map för att kunna slå upp ins → start/duration i val-celler
        per_plan_map_disp = []
        for _, tab in family_tabs:
            m = {(ins.name, 0): ins for ins in tab.parsed.insurances}
            per_plan_map_disp.append(m)

        # Hover-tooltip state: iid → {kind, choices/plan_idx, label}
        row_info = {}
        self._mix_row_info = row_info
        self._mix_per_plan_map_disp = per_plan_map_disp
        self._mix_varying_keys_for_hover = varying_keys
        self._mix_family_tabs_for_hover = family_tabs
        self._mix_hover_tip = None
        self._mix_hover_row = None

        # Referens-rader: originalplanerna utan mix, så användaren kan jämföra
        # mixens topp-rader mot dem direkt. Metrikerna klämmas mot samma slutår.
        kvar_idx = self.mix_kvar_var.get()
        kvar_attr = ('kvar_1', 'kvar_2', 'kvar_3')[kvar_idx]
        for pi, (pname, tab) in enumerate(family_tabs):
            if not tab.calc or not tab.calc.yearly:
                continue
            vals = [getattr(y, kvar_attr) for y in tab.calc.yearly]
            years = [y.year for y in tab.calc.yearly]
            m = self._mix_metrics_at(vals, years, end_year)
            if m is None:
                continue
            diff = m['slutvarde'] - best
            diff_s = f'{diff:+,.0f}'.replace(',', ' ') + ' kr'
            values = [
                pname,
                f'{fmt_kr(m["slutvarde"])} kr',
                '',  # Antal-kolumnen tom för referens-rader
                f'{fmt_kr(m["lagsta"])} kr',
                str(m['lagsta_ar']),
                f'{fmt_kr(m["total"])} kr',
                diff_s,
            ]
            # Val-cellerna: originalplanens start · längd för respektive försäkring
            for key in varying_keys:
                ins = per_plan_map_disp[pi].get(key)
                values.append(_fmt_start_dur(ins))
            iid = tree.insert('', 'end', values=values, tags=('ref',))
            row_info[iid] = {
                'kind': 'ref', 'plan_idx': pi, 'label': pname,
                'metrics': {
                    'slutvarde': m['slutvarde'], 'lagsta': m['lagsta'],
                    'lagsta_ar': m['lagsta_ar'], 'total': m['total'],
                },
            }
        # Skiljelinje-rad (tom) om vi lagt in ref-rader
        if family_tabs:
            tree.insert(
                '', 'end',
                values=[''] * len(cols),
                tags=('row_a',),
            )

        for i, r in enumerate(top):
            diff = r['slutvarde'] - best
            diff_s = '—' if i == 0 else f'{diff:+,.0f}'.replace(',', ' ') + ' kr'
            tag = 'best' if i == 0 else ('row_a' if i % 2 == 1 else 'row_b')
            count = r.get('_count', 1)
            count_s = str(count) if count > 1 else ''
            values = [
                f'#{i + 1}',
                f'{fmt_kr(r["slutvarde"])} kr',
                count_s,
                f'{fmt_kr(r["lagsta"])} kr',
                str(r['lagsta_ar']),
                f'{fmt_kr(r["total"])} kr',
                diff_s,
            ]
            for var_i, plan_idx in enumerate(r['choices']):
                key = varying_keys[var_i]
                ins = per_plan_map_disp[plan_idx].get(key)
                values.append(_fmt_start_dur(ins))
            iid = tree.insert('', 'end', values=values, tags=(tag,))
            row_info[iid] = {
                'kind': 'mix', 'choices': r['choices'], 'label': f'#{i + 1}',
                'metrics': {
                    'slutvarde': r['slutvarde'], 'lagsta': r['lagsta'],
                    'lagsta_ar': r['lagsta_ar'], 'total': r['total'],
                },
            }

        # Hover-tooltip: visa hela fördelningen (alla försäkringar) för raden
        tree.bind('<Motion>', self._on_mix_row_hover)
        tree.bind('<Leave>', self._on_mix_row_leave)
        # Högerklick: kopiera fördelningstexten till urklipp
        tree.bind('<Button-3>', self._on_mix_row_right_click)
        self._mix_tree = tree

        note = ttk.Label(
            self.mix_body,
            text=(
                'Val-kolumnerna visar försäkringens uttagsstart och längd '
                '(YYYY-MM · N år). "Antal" = hur många kombinationer som gav '
                'samma slutvärde (inom ±50 kr) — bara en representant visas. '
                'När du skapar en ny plan på minpension.se: använd samma '
                'start-datum och uttagslängd som visas för varje försäkring '
                'i bästa raden (#1). Hovra över en rad för att se hela '
                'fördelningen. Högerklick kopierar fördelnings-texten till urklipp.'
            ),
            font=('TkDefaultFont', 9), foreground='#666',
            wraplength=900, justify='left',
        )
        note._mix_result = True
        note.pack(fill='x', pady=(6, 0))

    def _rebuild_ranking(self):
        """Rankning-tabell: sortera valda planer efter valt kvar-mått.
        Metrikerna klämmas mot valt slutår (Spinbox). Ingen approximation —
        bara verkliga siffror från redan beräknade planer."""
        for w in self.rank_body.winfo_children():
            w.destroy()

        kvar_idx = self.rank_kvar_var.get()
        kvar_attr = ('kvar_1', 'kvar_2', 'kvar_3')[kvar_idx]
        metric = self.rank_metric_var.get()

        tab_by_name = {}
        for tab in self.app.tabs:
            if tab.parsed and tab.parsed.plan_name:
                tab_by_name[tab.parsed.plan_name] = tab

        # Uppdatera Spinbox-intervall utifrån valda planers år-täckning
        selected_calcs = []
        for name, var in self.plan_vars.items():
            if not var.get():
                continue
            tab = tab_by_name.get(name)
            if tab and tab.calc and tab.calc.yearly:
                selected_calcs.append(tab.calc)
        if selected_calcs:
            all_years = [y.year for c in selected_calcs for y in c.yearly]
            min_y, max_y = min(all_years), max(all_years)
            self._rank_min_year = min_y
            self._rank_max_year = max_y
            try:
                self.rank_end_year_spin.config(from_=min_y, to=max_y)
                # Sätt default endast om ingen tidigare värde finns (0) eller
                # om värdet är utanför nya intervallet
                cur = self.rank_end_year_var.get()
                if cur == 0 or cur < min_y or cur > max_y:
                    self.rank_end_year_var.set(max_y)
            except tk.TclError:
                pass
        try:
            end_year = int(self.rank_end_year_var.get())
        except (ValueError, tk.TclError):
            end_year = self._rank_max_year
        # Kläm mot giltigt intervall
        end_year = max(self._rank_min_year, min(self._rank_max_year, end_year))
        # Uppdatera hint med ålder vid slutåret (hämtas från första valda plan)
        age_at_end = None
        if selected_calcs:
            for c in selected_calcs:
                for yy in c.yearly:
                    if yy.year == end_year:
                        age_at_end = yy.age
                        break
                if age_at_end is not None:
                    break
        try:
            if age_at_end is not None:
                self.rank_end_year_hint.config(
                    text=f'Välj slutår mellan {self._rank_min_year} och '
                         f'{self._rank_max_year}; ålder {age_at_end} år '
                         f'vid slutår {end_year}',
                )
            else:
                self.rank_end_year_hint.config(
                    text=f'Välj slutår mellan {self._rank_min_year} och '
                         f'{self._rank_max_year}',
                )
        except tk.TclError:
            pass

        rows = []
        for name, var in self.plan_vars.items():
            if not var.get():
                continue
            tab = tab_by_name.get(name)
            if not tab or not tab.calc or not tab.calc.yearly:
                continue
            all_vals = [getattr(y, kvar_attr) for y in tab.calc.yearly]
            all_years = [y.year for y in tab.calc.yearly]
            # Klipp till år <= end_year
            idx_end = None
            for i, yr in enumerate(all_years):
                if yr <= end_year:
                    idx_end = i
                else:
                    break
            if idx_end is None:
                continue
            vals = all_vals[:idx_end + 1]
            years = all_years[:idx_end + 1]
            slut = vals[-1]
            i_min = min(range(len(vals)), key=lambda i: vals[i])
            lagsta = vals[i_min]
            lagsta_ar = years[i_min]
            total = sum(vals)
            rows.append({
                'name': name, 'slutvarde': slut, 'lagsta': lagsta,
                'lagsta_ar': lagsta_ar, 'total': total,
            })

        if not rows:
            ttk.Label(
                self.rank_body,
                text='Välj minst en plan i vänstra panelen för att se rankning.',
                foreground='#666',
            ).pack(pady=20)
            return

        rows.sort(key=lambda r: r[metric], reverse=True)
        best = rows[0][metric]

        cols = ('pos', 'plan', 'slutvarde', 'lagsta', 'lagsta_ar', 'total', 'diff')
        headers = {
            'pos': '#', 'plan': 'Plan',
            'slutvarde': 'Slutvärde',
            'lagsta': 'Lägsta',
            'lagsta_ar': 'År',
            'total': 'Summa alla år',
            'diff': 'Δ mot #1',
        }
        widths = {
            'pos': 40, 'plan': 200,
            'slutvarde': 110, 'lagsta': 110, 'lagsta_ar': 60,
            'total': 130, 'diff': 110,
        }

        tree = ttk.Treeview(
            self.rank_body, columns=cols, show='headings',
            height=max(6, min(len(rows), 20)),
        )
        for c in cols:
            tree.heading(c, text=headers[c])
            anchor = 'w' if c == 'plan' else 'center' if c in ('pos', 'lagsta_ar') else 'e'
            tree.column(c, width=widths[c], minwidth=widths[c], anchor=anchor, stretch=False)
        tree.tag_configure('row_a', background='#ffffff')
        tree.tag_configure('row_b', background='#f5f5f5')
        tree.tag_configure('best', background='#e0f0d0', font=('TkDefaultFont', 9, 'bold'))
        tree.pack(fill='both', expand=True)

        for i, r in enumerate(rows):
            diff = r[metric] - best
            diff_s = '—' if i == 0 else f'{diff:+,.0f}'.replace(',', ' ') + ' kr'
            tag = 'best' if i == 0 else ('row_a' if i % 2 == 1 else 'row_b')
            tree.insert('', 'end', values=(
                f'#{i + 1}', r['name'],
                f'{fmt_kr(r["slutvarde"])} kr',
                f'{fmt_kr(r["lagsta"])} kr',
                str(r['lagsta_ar']),
                f'{fmt_kr(r["total"])} kr',
                diff_s,
            ), tags=(tag,))

        info = ttk.Label(
            self.rank_body,
            text=(
                'Δ mot #1: skillnad i kronor mot den bäst rankade planen '
                'enligt valt mått. Grön rad = #1.'
            ),
            font=('TkDefaultFont', 9), foreground='#666',
            wraplength=800, justify='left',
        )
        info.pack(fill='x', pady=(6, 0))

    def _rebuild_insurance_matrix(self):
        """Bygg om försäkrings-matrisen: rader = försäkring, kolumner = valda planer.
        Rader där uttagslängd skiljer sig markeras — det är typ av skillnad som
        oftast har verklig effekt på graferna."""
        for w in self.ins_tab.winfo_children():
            w.destroy()

        selected_names = [n for n, v in self.plan_vars.items() if v.get()]
        selected_tabs = []
        for tab in self.app.tabs:
            name = tab.parsed.plan_name if (tab.parsed and tab.parsed.plan_name) else ''
            if name in selected_names:
                selected_tabs.append((name, tab))
        if not selected_tabs:
            ttk.Label(
                self.ins_tab,
                text='Välj minst en plan i vänstra panelen.',
                foreground='#666',
            ).pack(pady=20)
            return

        # För varje plan: lista (namn, förekomst-index inom planen, ins-objekt)
        per_plan = []
        for pname, tab in selected_tabs:
            seen = {}
            rows = []
            for ins in tab.parsed.insurances:
                k = seen.get(ins.name, 0)
                seen[ins.name] = k + 1
                rows.append((ins.name, k, ins))
            per_plan.append((pname, rows))

        # Union av (namn, förekomst) — behåll ordningen från första planen först
        all_keys = []
        seen_keys = set()
        for _, rows in per_plan:
            for name, occ, _ in rows:
                k = (name, occ)
                if k not in seen_keys:
                    all_keys.append(k)
                    seen_keys.add(k)

        # Behöver vi #N-suffix på ett visst namn? Ja om något plan har fler än en
        need_suffix = set()
        for _, rows in per_plan:
            per_name_count = {}
            for name, occ, _ in rows:
                per_name_count[name] = per_name_count.get(name, 0) + 1
            for n, c in per_name_count.items():
                if c > 1:
                    need_suffix.add(n)

        # Bygg treeview
        container = ttk.Frame(self.ins_tab)
        container.pack(fill='both', expand=True, padx=4, pady=4)
        container.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)

        cols = tuple(f'plan{i}' for i in range(len(selected_tabs)))
        tree = ttk.Treeview(
            container, columns=cols, show='tree headings',
            height=max(10, len(all_keys) + 1),
        )
        tree.heading('#0', text='Försäkring')
        tree.column('#0', width=220, minwidth=140, anchor='w', stretch=False)
        for i, (pname, _) in enumerate(selected_tabs):
            tree.heading(cols[i], text=pname)
            tree.column(cols[i], width=220, minwidth=140, anchor='w', stretch=False)
        tree.tag_configure('diff', background='#fff3d0')
        tree.tag_configure('dur_diff', background='#ffd3a0',
                           font=('TkDefaultFont', 9, 'bold'))
        tree.tag_configure('row_a', background='#ffffff')
        tree.tag_configure('row_b', background='#f5f5f5')

        vsb = ttk.Scrollbar(container, orient='vertical', command=tree.yview)
        hsb = ttk.Scrollbar(container, orient='horizontal', command=tree.xview)
        tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        tree.grid(row=0, column=0, sticky='nsew')
        vsb.grid(row=0, column=1, sticky='ns')
        hsb.grid(row=1, column=0, sticky='ew')

        def _fmt_dur(dur):
            if dur is None:
                return 'Livet ut'
            y, m = dur
            if y and m:
                return f'{y}å{m}m'
            if y:
                return f'{y} år'
            if m:
                return f'{m} mån'
            return '—'

        def _first_amt(ins):
            if not ins.amounts_before_tax:
                return None
            for a in ins.amounts_before_tax:
                if a:
                    return a
            return None

        for ri, (name, occ) in enumerate(all_keys):
            disp = name if name not in need_suffix else f'{name} #{occ + 1}'
            cells = []
            data = []  # (start, dur, amt) per plan för diff-detektion
            for _, rows in per_plan:
                match = next(
                    (ins for n, o, ins in rows if n == name and o == occ),
                    None,
                )
                if match is None:
                    cells.append('—')
                    data.append(None)
                    continue
                start_s = (
                    f'{match.start[0]}-{match.start[1]:02d}'
                    if match.start else '—'
                )
                dur_s = _fmt_dur(match.duration)
                amt = _first_amt(match)
                if amt:
                    cells.append(f'{start_s} · {dur_s} · {fmt_kr(amt)} kr/mån')
                else:
                    cells.append(f'{start_s} · {dur_s}')
                data.append((match.start, match.duration, amt))

            # Diff-detektion mot första icke-None
            ref = next((d for d in data if d is not None), None)
            has_dur_diff = False
            any_diff = False
            if ref is not None:
                for d in data:
                    if d is None:
                        any_diff = True
                        continue
                    if d != ref:
                        any_diff = True
                    if d[1] != ref[1]:
                        has_dur_diff = True

            tags = ['row_a' if ri % 2 == 0 else 'row_b']
            if has_dur_diff:
                tags = ['dur_diff']
                disp = '⚠ ' + disp
            elif any_diff:
                tags = ['diff']
            tree.insert('', 'end', text=disp, values=cells, tags=tuple(tags))

        # Legend längst ner
        legend = ttk.Frame(self.ins_tab)
        legend.pack(fill='x', padx=4, pady=(0, 4))
        ttk.Label(
            legend, text='⚠ = olik uttagslängd  ',
            font=('TkDefaultFont', 9), foreground='#a06000',
        ).pack(side='left')
        ttk.Label(
            legend, text='(gul = någon skillnad, orange = längd-skillnad)',
            font=('TkDefaultFont', 9), foreground='#666',
        ).pack(side='left')

    def _sync_preset_combo_to_selection(self):
        """Visa matchande preset-namn i dropdown om nuvarande val råkar matcha."""
        if not hasattr(self, 'preset_combo'):
            return
        current_plans = {n for n, v in self.plan_vars.items() if v.get()}
        current_kvar = [v.get() for v in self.kvar_vars]
        match = ''
        for name, preset in self._get_presets().items():
            preset_plans = set(preset.get('plans') or [])
            preset_kvar = preset.get('show_kvar') or []
            preset_kvar = [bool(preset_kvar[i]) if i < len(preset_kvar) else False
                           for i in range(3)]
            if preset_plans == current_plans and preset_kvar == current_kvar:
                match = name
                break
        self.preset_var.set(match)

    def _on_plan_toggle(self, idx):
        """Vanlig klick på plan-checkbox: uppdatera anchor + ändra urval."""
        self._anchor_idx = idx
        self._on_selection_change()

    def _range_action(self, idx, mark: bool):
        """Shift+klick: sätt alla planer mellan anchor och idx till `mark`."""
        if self._anchor_idx is None:
            self._anchor_idx = idx
        lo, hi = sorted([self._anchor_idx, idx])
        for i in range(lo, hi + 1):
            self.plan_vars[self._plan_names_ordered[i]].set(mark)
        self._anchor_idx = idx
        self._on_selection_change()
        return 'break'  # förhindra default Checkbutton-toggle

    # ---- Presets ----

    def _get_presets(self) -> dict:
        presets = (self.app._compare_state or {}).get('presets', {})
        return presets if isinstance(presets, dict) else {}

    def _refresh_preset_combo(self):
        names = sorted(self._get_presets().keys())
        self.preset_combo['values'] = names
        # Behåll nuvarande val om det fortfarande finns
        if self.preset_var.get() not in names:
            self.preset_var.set('')

    def _on_preset_selected(self, event=None):
        name = self.preset_var.get()
        preset = self._get_presets().get(name)
        if not preset:
            return
        plans_to_mark = set(preset.get('plans') or [])
        for pname, var in self.plan_vars.items():
            var.set(pname in plans_to_mark)
        show_kvar = preset.get('show_kvar') or [True, False, False]
        for i, v in enumerate(self.kvar_vars):
            v.set(bool(show_kvar[i]) if i < len(show_kvar) else False)
        self._on_selection_change()
        # Behåll preset-namnet i comboboxen även om presetens planer inte
        # längre finns i appen — annars går det inte att välja och ta bort
        # en föråldrad preset. _on_selection_change → sync råkar annars rensa
        # varibeln eftersom current_plans != preset_plans.
        self.preset_var.set(name)

    def _save_preset(self):
        from tkinter import simpledialog
        existing = self._get_presets()
        # Föreslå nuvarande val om det matchar en preset, annars tom
        suggestion = self.preset_var.get() or ''
        name = simpledialog.askstring(
            "Spara preset",
            "Namn på preset:",
            parent=self.top,
            initialvalue=suggestion,
        )
        if not name:
            return
        name = name.strip()
        if not name:
            return
        if name in existing:
            if not messagebox.askyesno(
                "Skriv över?",
                f"Det finns redan en preset '{name}'. Skriv över?",
                parent=self.top,
            ):
                return
        # Spara nuvarande val i state
        cs = self.app._compare_state or {}
        presets = dict(self._get_presets())
        presets[name] = {
            'plans': [n for n, v in self.plan_vars.items() if v.get()],
            'show_kvar': [v.get() for v in self.kvar_vars],
        }
        cs['presets'] = presets
        self.app._compare_state = cs
        self.app._remember_state()
        self._refresh_preset_combo()
        self.preset_var.set(name)

    def _delete_preset(self):
        name = self.preset_var.get()
        if not name:
            messagebox.showinfo(
                "Ingen preset vald",
                "Välj en preset i listan först.",
                parent=self.top,
            )
            return
        if not messagebox.askyesno(
            "Ta bort preset?",
            f"Ta bort presetn '{name}'?",
            parent=self.top,
        ):
            return
        cs = self.app._compare_state or {}
        presets = dict(self._get_presets())
        presets.pop(name, None)
        cs['presets'] = presets
        self.app._compare_state = cs
        self.app._remember_state()
        self._refresh_preset_combo()
        self.preset_var.set('')

    def _save_state(self):
        # Behåll befintliga presets när nuvarande urval uppdateras
        existing_presets = self._get_presets()
        self.app._compare_state = {
            'plans': [name for name, v in self.plan_vars.items() if v.get()],
            'show_kvar': [v.get() for v in self.kvar_vars],
            'presets': existing_presets,
        }
        self.app._remember_state()

    def _redraw(self):
        self.ax.clear()
        self._lines = []
        self._line_meta = []
        self.hover_line = None
        self.hover_text = None

        kvar_states = [v.get() for v in self.kvar_vars]
        kvar_attrs = ('kvar_1', 'kvar_2', 'kvar_3')
        plot_count = 0

        # Mappa plan_name → tab för snabb uppslagning
        tab_by_name = {}
        for tab in self.app.tabs:
            if tab.parsed and tab.parsed.plan_name:
                tab_by_name[tab.parsed.plan_name] = tab

        sparande = self.app.get_sparande() or 0.0
        # Stabil färgtilldelning: färgen är knuten till planens position i
        # plan_vars (dvs vänsterpanelens ordning), inte till antalet valda.
        # Så att kvarvarande planer behåller sina färger när man kryssar
        # i/ur en annan plan.
        for plan_idx, (name, var) in enumerate(self.plan_vars.items()):
            if not var.get():
                continue
            tab = tab_by_name.get(name)
            if not tab or not tab.calc or not tab.calc.yearly:
                continue
            color = COMPARE_COLORS[plan_idx % len(COMPARE_COLORS)]
            years = [y.year for y in tab.calc.yearly]
            # Startpunkt året innan första plan-år på ursprungssparandet →
            # planer med olika startår blir visuellt jämförbara
            pre_year = years[0] - 1 if years else None
            for k_idx, attr in enumerate(kvar_attrs):
                if not kvar_states[k_idx]:
                    continue
                y_vals = [getattr(y, attr) for y in tab.calc.yearly]
                if pre_year is not None:
                    x_plot = [pre_year] + years
                    y_plot = [sparande] + y_vals
                else:
                    x_plot = years
                    y_plot = y_vals
                line, = self.ax.plot(
                    x_plot, y_plot,
                    label=f"{name} — {KVAR_LABELS[k_idx]}",
                    color=color, linestyle=self.LINESTYLES[k_idx], linewidth=2,
                )
                self._lines.append(line)
                self._line_meta.append({
                    'plan_name': name, 'k_idx': k_idx, 'calc': tab.calc, 'color': color,
                })
                plot_count += 1

        sparande = self.app.get_sparande()
        title = f"Jämför planer — ursprungligt sparande {fmt_kr(sparande)} kr"
        growth_pct = self.app.get_growth_pct()
        if growth_pct > 0:
            title += f' och {growth_pct:g}%'.replace('.', ',') + ' tillväxt'
        self.ax.set_title(title)
        self.ax.set_xlabel('År')
        self.ax.set_ylabel('Kvar av sparande (kr)')
        self.ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: fmt_kr(v)))
        self.ax.grid(True, alpha=0.3)

        if plot_count:
            self.ax.legend(loc='best', fontsize=8)
        else:
            self.ax.text(
                0.5, 0.5, "Välj minst en plan och en graf att visa",
                transform=self.ax.transAxes, ha='center', va='center',
                color='#888', fontsize=11,
            )

        self.fig.tight_layout()
        self.canvas.draw_idle()

    def _render_monthly_income(self):
        """Rita månad-för-månad pensionsinkomst per vald plan."""
        if not hasattr(self, 'monthly_ax'):
            return
        self.monthly_ax.clear()
        self._monthly_lines = []
        self._monthly_line_meta = []
        self._monthly_hover_line = None
        self._monthly_hover_text = None

        tab_by_name = {}
        for tab in self.app.tabs:
            if tab.parsed and tab.parsed.plan_name:
                tab_by_name[tab.parsed.plan_name] = tab

        # Hämta födelsedatum för ålders-beräkning i hover-tooltipen
        self._monthly_birth_year = None
        self._monthly_birth_month = None
        for tab in self.app.tabs:
            if tab.parsed and tab.parsed.birth_year and tab.parsed.birth_month:
                self._monthly_birth_year = tab.parsed.birth_year
                self._monthly_birth_month = tab.parsed.birth_month
                break

        tax_mode = self.monthly_tax_var.get()  # 'before' eller 'after'
        plot_count = 0
        longest_calc = None  # för utgifts-linjen (bredaste x-täckningen)
        # Stabil färgtilldelning — se _redraw() för samma resonemang.
        for plan_idx, (name, var) in enumerate(self.plan_vars.items()):
            if not var.get():
                continue
            tab = tab_by_name.get(name)
            if not tab or not tab.calc or not tab.calc.monthly:
                continue
            color = COMPARE_COLORS[plan_idx % len(COMPARE_COLORS)]
            x_vals = []
            y_vals = []
            for m in tab.calc.monthly:
                x_vals.append(m.year + (m.month - 1) / 12.0)
                if tax_mode == 'before':
                    y_vals.append(m.before_tax or 0)
                else:
                    y_vals.append(m.after_tax or 0)
            if not x_vals:
                continue
            line, = self.monthly_ax.plot(
                x_vals, y_vals, label=name,
                color=color, linewidth=1.5,
            )
            self._monthly_lines.append(line)
            self._monthly_line_meta.append({
                'plan_name': name, 'calc': tab.calc, 'color': color,
                'parsed': tab.parsed, 'x': x_vals, 'y': y_vals,
            })
            if longest_calc is None or len(tab.calc.monthly) > len(longest_calc.monthly):
                longest_calc = tab.calc
            plot_count += 1

        # Utgifter: en gemensam streckad linje. Ålders-baserat så samma för
        # alla planer — vi tar från planen med bredaste tidsspannet.
        if longest_calc is not None:
            exp_x = []
            exp_y = []
            for m in longest_calc.monthly:
                exp_x.append(m.year + (m.month - 1) / 12.0)
                exp_y.append(m.utgifter or 0)
            self.monthly_ax.plot(
                exp_x, exp_y, label='Utgifter',
                color='#c0392b', linewidth=1.6, linestyle='--',
            )
            self._monthly_exp_x = exp_x
            self._monthly_exp_y = exp_y
        else:
            self._monthly_exp_x = None
            self._monthly_exp_y = None

        # Skatte-sänknings-markeringar på x-axeln: detektera år (januari) där
        # effektiv skatt sjunker markant (typ jobbskatteavdrag vid riktålder+1).
        # Använder longest_calc som referens — samma person i alla planer.
        self._monthly_tax_breaks = []
        if longest_calc is not None:
            prev_ratio = None
            for m in longest_calc.monthly:
                if not m.before_tax or m.after_tax is None or m.before_tax <= 0:
                    continue
                ratio = m.after_tax / m.before_tax  # netto-andel (1 - skatt)
                if (prev_ratio is not None and m.month == 1
                        and ratio - prev_ratio > 0.03):
                    # Netto-andel ökar > 3 procentenheter vid årsskifte → skatte-sänkning
                    year_x = m.year
                    self._monthly_tax_breaks.append((year_x, m.age))
                    self.monthly_ax.axvline(
                        x=year_x, color='#5a7', linestyle=':',
                        alpha=0.6, linewidth=1.2,
                    )
                prev_ratio = ratio
            # Text-etiketter under x-axeln för varje sänkning
            if self._monthly_tax_breaks:
                for year_x, age in self._monthly_tax_breaks:
                    self.monthly_ax.annotate(
                        f'{year_x}: lägre skatt ({age} år)',
                        xy=(year_x, 0), xycoords=('data', 'axes fraction'),
                        xytext=(2, -18), textcoords='offset points',
                        fontsize=7, color='#3a7', rotation=0, ha='left',
                    )

        sparande = self.app.get_sparande()
        tax_label = 'efter skatt' if tax_mode == 'after' else 'före skatt'
        self.monthly_ax.set_title(
            f'Pensionsinkomst per månad ({tax_label})',
        )
        self.monthly_ax.set_xlabel('År')
        self.monthly_ax.set_ylabel('kr / mån')
        self.monthly_ax.yaxis.set_major_formatter(
            FuncFormatter(lambda v, _: fmt_kr(v)),
        )
        self.monthly_ax.grid(True, alpha=0.3)
        if plot_count:
            self.monthly_ax.legend(loc='best', fontsize=8)
        else:
            self.monthly_ax.text(
                0.5, 0.5, 'Välj minst en plan i vänstra panelen',
                transform=self.monthly_ax.transAxes,
                ha='center', va='center', color='#888', fontsize=11,
            )
        self.monthly_fig.tight_layout()
        self.monthly_canvas.draw_idle()

    def _on_monthly_hover(self, event):
        if not self._monthly_lines or event.inaxes != self.monthly_ax or event.xdata is None:
            return
        # Position-läsare under grafen
        if event.ydata is not None:
            year_int = int(event.xdata)
            frac = event.xdata - year_int
            month = min(12, max(1, int(frac * 12) + 1))
            y_rounded = round(event.ydata / 100) * 100
            self.monthly_pos_label.config(
                text=f'År {year_int}.{month:02d}:  {fmt_kr(y_rounded)} kr/mån',
            )
        # Snap till närmaste månad — union av alla planers x-värden så
        # hover fungerar även på månader där bara vissa planer har data
        target_x = event.xdata
        all_x = set()
        for meta in self._monthly_line_meta:
            all_x.update(meta['x'])
        if self._monthly_exp_x is not None:
            all_x.update(self._monthly_exp_x)
        if not all_x:
            return
        sorted_x = sorted(all_x)
        idx = min(range(len(sorted_x)), key=lambda i: abs(sorted_x[i] - target_x))
        snap_x = sorted_x[idx]

        if self._monthly_hover_line is None:
            self._monthly_hover_line = self.monthly_ax.axvline(
                x=snap_x, color='#888888', linestyle='--',
                alpha=0.6, linewidth=1,
            )
        else:
            self._monthly_hover_line.set_xdata([snap_x, snap_x])
            self._monthly_hover_line.set_visible(True)

        # Bygg tooltip: en rad per plan med aktuellt kr/mån-värde
        year_int = int(snap_x + 0.001)
        month = int(round((snap_x - year_int) * 12)) + 1
        month = min(12, max(1, month))
        header = f'{year_int}-{month:02d}'
        # Räkna ut ålder (år + månader) från födelsedata om vi har det
        by = getattr(self, '_monthly_birth_year', None)
        bm = getattr(self, '_monthly_birth_month', None)
        if by and bm:
            total = (year_int - by) * 12 + (month - bm)
            if total >= 0:
                years_old = total // 12
                months_old = total % 12
                if months_old == 0:
                    header += f'   ({years_old} år)'
                else:
                    header += f'   ({years_old} år, {months_old} mån)'
        children = [TextArea(header, textprops=dict(fontsize=9, weight='bold'))]
        # Utgift för samma månad (om tillgänglig) — behövs för diff per plan
        exp_val = None
        if self._monthly_exp_x is not None:
            xs = self._monthly_exp_x
            j = min(range(len(xs)), key=lambda i: abs(xs[i] - snap_x))
            exp_val = self._monthly_exp_y[j]
        tax_mode = getattr(self, 'monthly_tax_var', None)
        tax_mode = tax_mode.get() if tax_mode is not None else 'after'
        amt_key = 'after_mo' if tax_mode == 'after' else 'before_mo'

        # Samla per-plan-data först + räkna hur många rader vi skulle rendera
        per_plan_rows = []  # [(meta, j, is_active, per_ins_list)]
        n_rows = 1  # header-rad
        if exp_val is not None:
            n_rows += 1  # utgifts-rad
        for meta in self._monthly_line_meta:
            xs = meta['x']
            j = min(range(len(xs)), key=lambda i: abs(xs[i] - snap_x))
            is_active = abs(xs[j] - snap_x) <= 1 / 24.0
            if is_active and meta.get('parsed'):
                try:
                    per_ins = _monthly_per_insurance_amounts(
                        meta['parsed'], year_int, month,
                    )
                except Exception:
                    per_ins = []
                per_ins = [p for p in per_ins if p.get(amt_key)]
                per_ins.sort(key=lambda p: p.get(amt_key) or 0, reverse=True)
            else:
                per_ins = []
            per_plan_rows.append((meta, j, is_active, per_ins))
            n_rows += 1 + len(per_ins)

        # Bedöm om alla per-försäkrings-rader ryms i canvas. Om inte:
        # fall tillbaka till bara ändringar-listan per plan (kortare).
        try:
            canvas_h = self.monthly_canvas.get_tk_widget().winfo_height()
        except tk.TclError:
            canvas_h = 500
        # Reservera plats för axel-labels och margins (ca 120 px)
        avail_h = max(200, canvas_h - 120)
        # Font 9pt + padding ≈ 15 px per rad
        max_rows = max(10, avail_h // 15)
        show_detail = n_rows <= max_rows

        for meta, j, is_active, per_ins in per_plan_rows:
            if not is_active:
                label = f"{meta['plan_name']}: ej startad"
            else:
                val = meta['y'][j]
                if exp_val is not None:
                    diff = val - exp_val
                    sign = '+' if diff >= 0 else '−'
                    extra = f' ({sign}{fmt_kr(abs(diff))} vs utg.)'
                else:
                    extra = ''
                label = f"{meta['plan_name']}: {fmt_kr(val)}{extra}"
            da = DrawingArea(10, 10, 0, 0)
            da.add_artist(Circle((5, 5), 4, color=meta['color']))
            row = HPacker(
                children=[
                    da,
                    TextArea(label, textprops=dict(fontsize=9)),
                ],
                align='center', pad=0, sep=4,
            )
            children.append(row)
            if not is_active or not meta.get('parsed'):
                continue
            if show_detail:
                for p in per_ins:
                    amt = p.get(amt_key)
                    if not amt:
                        continue
                    children.append(TextArea(
                        f"   • {p['name']}: {fmt_kr(amt)}",
                        textprops=dict(fontsize=8, color='#333'),
                    ))
            else:
                # Fallback: bara ändringar denna månad vs föregående
                try:
                    changes = _monthly_insurance_changes(
                        meta['parsed'], year_int, month,
                    )
                except Exception:
                    changes = []
                for ins_name, delta in changes:
                    prefix = '   +' if delta > 0 else '   −'
                    children.append(TextArea(
                        f"{prefix}{ins_name} ({'+' if delta >= 0 else '−'}"
                        f"{fmt_kr(abs(delta))})",
                        textprops=dict(fontsize=8, color='#555'),
                    ))
        if exp_val is not None:
            da_e = DrawingArea(20, 6, 0, 0)
            da_e.add_artist(Line2D(
                [0, 20], [3, 3], color='#c0392b', linewidth=2, linestyle='--',
            ))
            row = HPacker(
                children=[
                    da_e,
                    TextArea(
                        f'Utgifter: {fmt_kr(exp_val)}',
                        textprops=dict(fontsize=9, weight='bold'),
                    ),
                ],
                align='center', pad=0, sep=4,
            )
            children.append(row)
        content = VPacker(children=children, align='left', pad=2, sep=2)

        xmin, xmax = self.monthly_ax.get_xlim()
        if snap_x < (xmin + xmax) / 2:
            xybox, box_alignment = (10, -10), (0, 1)
        else:
            xybox, box_alignment = (-10, -10), (1, 1)

        if self._monthly_hover_text is not None:
            try:
                self._monthly_hover_text.remove()
            except (AttributeError, ValueError):
                pass
        self._monthly_hover_text = AnnotationBbox(
            content, xy=(snap_x, 0.98),
            xycoords=('data', 'axes fraction'),
            xybox=xybox, boxcoords='offset points',
            box_alignment=box_alignment,
            bboxprops=dict(boxstyle='round,pad=0.4',
                           fc='#ffffe0', ec='#888888', alpha=0.95),
            zorder=10,
        )
        self.monthly_ax.add_artist(self._monthly_hover_text)
        self.monthly_canvas.draw_idle()

    def _on_monthly_leave(self, event):
        if self._monthly_hover_line is not None:
            self._monthly_hover_line.set_visible(False)
        if self._monthly_hover_text is not None:
            self._monthly_hover_text.set_visible(False)
        self.monthly_pos_label.config(text='')
        self.monthly_canvas.draw_idle()

    def _on_hover(self, event):
        if not self._lines or event.inaxes != self.ax or event.xdata is None:
            return
        # Position-läsare (uppdateras pixel-för-pixel, oberoende av popupen)
        if event.ydata is not None:
            year_int = int(event.xdata)
            frac = event.xdata - year_int
            month = min(12, max(1, int(frac * 12) + 1))
            y_rounded = round(event.ydata / 10_000) * 10_000
            self.pos_label.config(
                text=f'År {year_int}.{month}:  {fmt_kr(y_rounded)} kr',
            )
        # Unionera år från alla planer plus deras startpunkter (år innan
        # första kalkyleråret där kurvan börjar på ursprungssparandet).
        real_years = sorted({
            y.year for meta in self._line_meta for y in meta['calc'].yearly
        })
        if not real_years:
            return
        pre_years = sorted({
            meta['calc'].yearly[0].year - 1 for meta in self._line_meta
            if meta['calc'].yearly
        })
        all_years = sorted(set(real_years) | set(pre_years))
        idx = min(range(len(all_years)), key=lambda i: abs(all_years[i] - event.xdata))
        year = all_years[idx]
        is_start = year in pre_years and year not in real_years

        if self.hover_line is None:
            self.hover_line = self.ax.axvline(
                x=year, color='#888888', linestyle='--', alpha=0.6, linewidth=1,
            )
        else:
            self.hover_line.set_xdata([year, year])
            self.hover_line.set_visible(True)

        # Startpunkt för någon plan → visa ursprungssparande + vilka planer
        # som har sin start här
        if is_start:
            sparande = self.app.get_sparande() or 0.0
            starting_plans = [
                meta['plan_name'] for meta in self._line_meta
                if meta['calc'].yearly
                and meta['calc'].yearly[0].year - 1 == year
            ]
            # Dedup medan ordning bevaras
            seen = set()
            unique_plans = []
            for p in starting_plans:
                if p not in seen:
                    seen.add(p)
                    unique_plans.append(p)
            children = [
                TextArea(f'År {year} (start)',
                         textprops=dict(fontsize=9, weight='bold')),
                TextArea(f'Ursprungssparande: {fmt_kr(sparande)}',
                         textprops=dict(fontsize=9)),
            ]
            for p in unique_plans:
                children.append(TextArea(
                    f'Startar: {p}', textprops=dict(fontsize=9, style='italic'),
                ))
            content = VPacker(children=children, align='left', pad=2, sep=2)
            xmin, xmax = self.ax.get_xlim()
            if year < (xmin + xmax) / 2:
                xybox, box_alignment = (10, -10), (0, 1)
            else:
                xybox, box_alignment = (-10, -10), (1, 1)
            if self.hover_text is not None:
                try:
                    self.hover_text.remove()
                except (AttributeError, ValueError):
                    pass
            self.hover_text = AnnotationBbox(
                content, xy=(year, 0.98),
                xycoords=('data', 'axes fraction'),
                xybox=xybox, boxcoords='offset points',
                box_alignment=box_alignment,
                bboxprops=dict(boxstyle='round,pad=0.4',
                               fc='#ffffe0', ec='#888888', alpha=0.95),
                zorder=10,
            )
            self.ax.add_artist(self.hover_text)
            self.canvas.draw_idle()
            return

        # Gruppera linjer per plan_name
        by_plan = {}
        for line, meta in zip(self._lines, self._line_meta):
            by_plan.setdefault(meta['plan_name'], []).append(meta)

        # Plocka åldern från första planen som har data för året (de flesta
        # jämförelser gäller samma person, så åldern är gemensam)
        first_age = None
        for meta in self._line_meta:
            yd = next((y for y in meta['calc'].yearly if y.year == year), None)
            if yd is not None:
                first_age = yd.age
                break
        year_label = f"År {year}, {first_age} år" if first_age is not None else f"År {year}"
        children = [TextArea(year_label, textprops=dict(fontsize=9, weight='bold'))]

        # Samla per-plan data först (utan att bygga textareas) så vi kan räkna
        # ut vilken plan som ligger överst per kvar-strategi. Bygg sedan
        # textareas med ★-markering vid den överst-planens värde.
        sparande = self.app.get_sparande() or 0.0
        plan_values = {}  # plan_name → {k_idx: value}
        plan_blocks = []  # [(name, color, is_plan_start, per_kvar_dict)]
        for name, metas in by_plan.items():
            calc = metas[0]['calc']
            if not calc.yearly:
                continue
            plan_pre_year = calc.yearly[0].year - 1
            year_data = next((y for y in calc.yearly if y.year == year), None)
            # Startpunkt för just DENNA plan? → visa ursprungssparandet
            is_plan_start = year_data is None and year == plan_pre_year
            if year_data is None and not is_plan_start:
                continue
            color = metas[0]['color']
            per_kvar = {}
            for meta in metas:
                k_idx = meta['k_idx']
                if is_plan_start:
                    val = sparande
                else:
                    val = (year_data.kvar_1, year_data.kvar_2, year_data.kvar_3)[k_idx]
                per_kvar[k_idx] = val
            plan_blocks.append((name, color, is_plan_start, per_kvar))
            plan_values[name] = per_kvar

        # För varje k_idx: hitta max- och min-värdet (ignorera startpunkter
        # eftersom alla startar på samma sparande — inte meningsfullt).
        # Avrundning till närmaste krona så att flera planer som råkar dela
        # samma värde (efter tillväxt-räkning) alla markeras.
        max_val_per_k = {}
        min_val_per_k = {}
        for name, _, is_plan_start, per_kvar in plan_blocks:
            if is_plan_start:
                continue
            for k_idx, val in per_kvar.items():
                val_r = round(val)
                if k_idx not in max_val_per_k or val_r > max_val_per_k[k_idx]:
                    max_val_per_k[k_idx] = val_r
                if k_idx not in min_val_per_k or val_r < min_val_per_k[k_idx]:
                    min_val_per_k[k_idx] = val_r

        # Nu bygg textareas i ursprunglig plan-ordning med markering
        for name, color, is_plan_start, per_kvar in plan_blocks:
            da = DrawingArea(10, 10, 0, 0)
            da.add_artist(Circle((5, 5), 4, color=color))
            label_text = f'{name} (start)' if is_plan_start else name
            header = HPacker(
                children=[da, TextArea(label_text,
                                       textprops=dict(fontsize=9, weight='bold'))],
                align='center', pad=0, sep=4,
            )
            children.append(header)
            for k_idx in sorted(per_kvar.keys()):
                val = per_kvar[k_idx]
                max_v = max_val_per_k.get(k_idx)
                min_v = min_val_per_k.get(k_idx)
                # Bara markera när det finns skillnad mellan planerna;
                # om alla är lika är max == min och markering blir menlös.
                has_spread = (max_v is not None and min_v is not None
                              and max_v != min_v)
                val_r = round(val)
                is_top = (has_spread and val_r == max_v and not is_plan_start)
                is_low = (has_spread and val_r == min_v and not is_plan_start)
                if is_top:
                    marker = '  ★ högst'
                elif is_low:
                    diff = val_r - max_v  # negativ
                    diff_s = f'−{fmt_kr(abs(diff))}'
                    marker = f'  ★ lägst ({diff_s})'
                elif has_spread and not is_plan_start:
                    diff = val_r - max_v  # negativ
                    diff_s = f'−{fmt_kr(abs(diff))}'
                    marker = f'  ({diff_s})'
                else:
                    marker = ''
                children.append(TextArea(
                    f"   {KVAR_LABELS[k_idx]}: {fmt_kr(val)}{marker}",
                    textprops=dict(
                        fontsize=9,
                        weight='bold' if (is_top or is_low) else 'normal',
                    ),
                ))

        # Skillnad mellan planer: bara när exakt 2 planer visas i grafen —
        # annars blir det svårläst (N×N-jämförelser)
        if len(plan_values) == 2:
            names = list(plan_values.keys())
            a_name, b_name = names
            a_vals, b_vals = plan_values[a_name], plan_values[b_name]
            common_k = sorted(set(a_vals.keys()) & set(b_vals.keys()))
            if common_k:
                children.append(TextArea(
                    f"Skillnad ({a_name} − {b_name}):",
                    textprops=dict(fontsize=9, weight='bold'),
                ))
                for k_idx in common_k:
                    diff = a_vals[k_idx] - b_vals[k_idx]
                    sign = '+' if diff >= 0 else '−'
                    children.append(TextArea(
                        f"   {KVAR_LABELS[k_idx]}: {sign}{fmt_kr(abs(diff))}",
                        textprops=dict(fontsize=9),
                    ))

        content = VPacker(children=children, align='left', pad=2, sep=2)

        xmin, xmax = self.ax.get_xlim()
        if year < (xmin + xmax) / 2:
            xybox, box_alignment = (10, -10), (0, 1)
        else:
            xybox, box_alignment = (-10, -10), (1, 1)

        if self.hover_text is not None:
            try:
                self.hover_text.remove()
            except Exception:
                pass
        self.hover_text = AnnotationBbox(
            content, xy=(year, 0.98),
            xycoords=('data', 'axes fraction'),
            xybox=xybox, boxcoords='offset points',
            box_alignment=box_alignment,
            bboxprops=dict(boxstyle='round,pad=0.4', fc='#ffffe0', ec='#888888', alpha=0.95),
            zorder=10,
        )
        self.ax.add_artist(self.hover_text)
        self.canvas.draw_idle()

    def _on_leave(self, event):
        if self.hover_line is not None:
            self.hover_line.set_visible(False)
        if self.hover_text is not None:
            try:
                self.hover_text.remove()
            except Exception:
                pass
            self.hover_text = None
        if hasattr(self, 'pos_label'):
            self.pos_label.config(text='')
        self.canvas.draw_idle()

    def _add_plan_row(self, name, idx):
        """Bygg en checkbox-rad för en plan i vänsterspalten (kallas både
        vid init och när nya planer läggs till efter fönstret öppnats)."""
        var = self.plan_vars[name]
        row = ttk.Frame(self._plans_frame)
        row.pack(anchor='w', fill='x')
        swatch = tk.Label(
            row, text='  ', bg=self._plans_neutral_bg, width=2,
            relief='solid', borderwidth=1,
        )
        swatch.pack(side='left', padx=(0, 4))
        self._plan_color_swatches[name] = swatch
        cb = ttk.Checkbutton(
            row, text=name, variable=var,
            command=lambda i=idx: self._on_plan_toggle(i),
        )
        cb.pack(side='left', anchor='w')
        cb.bind('<Shift-Button-1>',
                lambda e, i=idx: self._range_action(i, True))
        cb.bind('<Shift-Button-3>',
                lambda e, i=idx: self._range_action(i, False))
        cb.bind('<Button-3>',
                lambda e, n=name: self._show_plan_context_menu(e, n))

        def updater(*_):
            if self.plan_vars[name].get():
                swatch.configure(
                    bg=COMPARE_COLORS[idx % len(COMPARE_COLORS)],
                )
            else:
                swatch.configure(bg=self._plans_neutral_bg)
        var.trace_add('write', updater)
        updater()

    def _show_plan_context_menu(self, event, plan_name):
        """Kontextmeny vid högerklick på en plan-checkbox. Låter användaren
        ta bort planen (fungerar som Arkiv → Stäng plan i huvudfönstret).
        Bara aktivt för planer som INTE hämtats från minpension.se — hämtade
        planer ska tas bort där de finns för att inte råka radera data."""
        tab = next(
            (t for t in self.app.tabs
             if t.parsed and t.parsed.plan_name == plan_name),
            None,
        )
        if tab is None:
            return
        menu = tk.Menu(self.top, tearoff=False)
        if tab.fetched_at:
            menu.add_command(
                label=f'Hämtad {tab.fetched_at} — ta bort i huvudfönstret',
                state='disabled',
            )
        else:
            menu.add_command(
                label=f'Ta bort "{plan_name}"',
                command=lambda t=tab: self.app.close_tab(t),
            )
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        # Undertryck default Button-3-hantering (som Shift-versionen ovan
        # kan ha triggat) genom att returnera 'break'
        return 'break'

    def _sync_plan_rows_with_tabs(self):
        """Sync plan_vars och UI-rader mot app.tabs. Hanterar både nya och
        stängda planer. Returnerar True om något ändrats."""
        current_names = []
        for tab in self.app.tabs:
            name = tab.parsed.plan_name if (tab.parsed and tab.parsed.plan_name) else ''
            if name:
                current_names.append(name)
        if current_names == self._plan_names_ordered:
            return False

        # Bevara plan_vars för planer som fortfarande finns
        cs = self.app._compare_state or {}
        saved_plans = set(cs.get('plans') or [])
        new_vars = {}
        for name in current_names:
            if name in self.plan_vars:
                new_vars[name] = self.plan_vars[name]
            else:
                default = (name in saved_plans) if saved_plans else True
                new_vars[name] = tk.BooleanVar(value=default)
        self.plan_vars = new_vars
        self._plan_names_ordered = list(current_names)
        self._anchor_idx = None

        # Riv alla existerande rader och bygg om (så att row-index blir rätt
        # efter borttagningar — _on_plan_toggle/_range_action använder idx)
        for child in list(self._plans_frame.winfo_children()):
            child.destroy()
        self._plan_color_swatches.clear()
        for i, name in enumerate(self._plan_names_ordered):
            self._add_plan_row(name, i)
        return True

    def refresh_from_data(self):
        """Anropas av appen när planer/data ändrats; rita om diagrammet,
        bygg om försäkrings-matrisen och rankningen."""
        changed = self._sync_plan_rows_with_tabs()
        self._redraw()
        self._render_monthly_income()
        self._rebuild_insurance_matrix()
        self._rebuild_ranking()
        if changed:
            self._rebuild_mix_families()
        # Sync även Föreslå plan-fönstret om öppet
        sw = getattr(self.app, '_suggest_window', None)
        if sw is not None:
            try:
                sw._refresh_suggest_bases()
                sw._rebuild_suggest_lock_table()
                sw._refresh_manual_bases()
            except tk.TclError:
                pass

    def _apply_initial_geometry(self):
        """Återställ senast använd position/storlek, annars centrera på huvudfönstret."""
        state = self.app._read_state()
        saved = state.get('compare_window_geometry')
        if isinstance(saved, str) and '+' in saved:
            try:
                self.top.geometry(saved)
                return
            except tk.TclError:
                pass
        # Default: centrera på huvudfönstret (samma skärm vid multi-monitor)
        self.app.root.update_idletasks()
        main_x = self.app.root.winfo_x()
        main_y = self.app.root.winfo_y()
        main_w = self.app.root.winfo_width()
        main_h = self.app.root.winfo_height()
        cw, ch = 1200, 700
        x = main_x + max(0, (main_w - cw) // 2)
        y = main_y + max(0, (main_h - ch) // 2)
        self.top.geometry(f'{cw}x{ch}+{x}+{y}')

    def _on_close(self):
        # Spara fönstrets position/storlek inför nästa öppning
        self.app._compare_window_geometry = self.top.geometry()
        self._save_state()
        self.app._compare_window = None
        self.top.destroy()


class SuggestPlanWindow:
    """Fristående fönster "Planera uttag" med två flikar:
      - Föreslå (automatiskt): greedy-optimering
      - Planera manuellt: sätt start+längd per försäkring med grafiskt
        gränssnitt (Gantt-graf), räkna om resultat direkt.

    Båda flikar producerar en syntetisk plan som kan skapas som ny plan-flik
    i huvudfönstret."""

    def __init__(self, app):
        self.app = app
        self.top = tk.Toplevel(app.root)
        self.top.title("Planera uttag")
        self._apply_initial_geometry()
        self.top.protocol('WM_DELETE_WINDOW', self._on_close)

        # Egen slutårs- och kvar-variabel (inte delad med Jämför-fönstret)
        state = self._read_state_safe()
        try:
            saved_end = int(state.get('suggest_end_year', 0) or 0)
        except (ValueError, TypeError):
            saved_end = 0
        self.suggest_end_year_var = tk.IntVar(value=saved_end or 2050)
        self.suggest_end_year_var.trace_add(
            'write', lambda *_: self._save_end_year(),
        )
        try:
            saved_kvar = int(state.get('suggest_kvar', 1))
        except (ValueError, TypeError):
            saved_kvar = 1
        self.suggest_kvar_var = tk.IntVar(value=saved_kvar)
        self.suggest_kvar_var.trace_add(
            'write', lambda *_: self._save_kvar(),
        )

        self._build_layout()
        self._refresh_suggest_bases()
        self._refresh_manual_bases()
        # Trigga initial bygg-av-tabell + graf om saved base fortfarande gäller
        # (trace_add fyrar inte om StringVar.set() sätter samma värde)
        if self.manual_base_var.get():
            self._on_manual_base_changed()

    def _read_state_safe(self):
        try:
            return self.app._read_state()
        except Exception:
            return {}

    def _save_end_year(self):
        try:
            state = self.app._read_state()
            state['suggest_end_year'] = int(self.suggest_end_year_var.get())
            with open(STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(state, f, indent=2)
        except (ValueError, tk.TclError, OSError):
            pass

    def _save_kvar(self):
        try:
            state = self.app._read_state()
            state['suggest_kvar'] = int(self.suggest_kvar_var.get())
            with open(STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(state, f, indent=2)
        except (ValueError, tk.TclError, OSError):
            pass

    def _apply_initial_geometry(self):
        state = self._read_state_safe()
        saved = state.get('suggest_window_geometry')
        if isinstance(saved, str) and '+' in saved:
            try:
                self.top.geometry(saved)
                return
            except tk.TclError:
                pass
        self.app.root.update_idletasks()
        main_x = self.app.root.winfo_x()
        main_y = self.app.root.winfo_y()
        main_w = self.app.root.winfo_width()
        main_h = self.app.root.winfo_height()
        cw, ch = 1100, 800
        x = main_x + max(0, (main_w - cw) // 2)
        y = main_y + max(0, (main_h - ch) // 2)
        self.top.geometry(f'{cw}x{ch}+{x}+{y}')

    def _on_close(self):
        try:
            state = self.app._read_state()
            state['suggest_window_geometry'] = self.top.geometry()
            with open(STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(state, f, indent=2)
        except (tk.TclError, OSError):
            pass
        # Städa upp manuell tooltip om den finns
        try:
            self._hide_manual_tooltip()
        except (AttributeError, tk.TclError):
            pass
        self.app._suggest_window = None
        self.top.destroy()

    def _build_layout(self):
        """Yttre struktur: Notebook med två flikar."""
        self.notebook = ttk.Notebook(self.top)
        self.notebook.pack(fill='both', expand=True)

        self.suggest_tab_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.suggest_tab_frame, text='Föreslå (automatisk)')
        self._build_suggest_content(self.suggest_tab_frame)

        self.manual_tab_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.manual_tab_frame, text='Planera manuellt')
        self._build_manual_content(self.manual_tab_frame)

        # Återställ senast använd flik och spara ändringar
        saved_tab = self._read_state_safe().get('plan_uttag_active_tab', 'suggest')
        if saved_tab == 'manual':
            try:
                self.notebook.select(self.manual_tab_frame)
            except tk.TclError:
                pass
        self.notebook.bind('<<NotebookTabChanged>>',
                           self._on_plan_uttag_tab_changed)

    def _on_plan_uttag_tab_changed(self, event):
        try:
            sel = self.notebook.select()
            name = 'manual' if sel == str(self.manual_tab_frame) else 'suggest'
            state = self.app._read_state()
            state['plan_uttag_active_tab'] = name
            with open(STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(state, f, indent=2)
        except (tk.TclError, OSError):
            pass

    def _build_suggest_content(self, parent):
        # Scrollbar runt hela innehållet — under körning växer body med graf
        # och tabell, och användaren kan behöva scrolla för att se allt.
        container = ttk.Frame(parent)
        container.pack(fill='both', expand=True)
        canvas = tk.Canvas(container, borderwidth=0, highlightthickness=0)
        vscroll = ttk.Scrollbar(
            container, orient='vertical', command=canvas.yview,
        )
        canvas.configure(yscrollcommand=vscroll.set)
        canvas.pack(side='left', fill='both', expand=True)
        vscroll.pack(side='right', fill='y')
        inner = ttk.Frame(canvas)
        inner_id = canvas.create_window((0, 0), window=inner, anchor='nw')

        def _on_inner_configure(event):
            canvas.configure(scrollregion=canvas.bbox('all'))
        inner.bind('<Configure>', _on_inner_configure)

        def _on_canvas_configure(event):
            # Sträck inner-Framen till canvas-bredden så innehållet inte klipps
            canvas.itemconfigure(inner_id, width=event.width)
        canvas.bind('<Configure>', _on_canvas_configure)

        # Musehjul: scrolla bara när markören är inuti fönstret
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), 'units')
        canvas.bind('<Enter>',
                    lambda _e: canvas.bind_all('<MouseWheel>', _on_mousewheel))
        canvas.bind('<Leave>',
                    lambda _e: canvas.unbind_all('<MouseWheel>'))
        self.top.bind('<Destroy>',
                      lambda e: (e.widget is self.top) and canvas.unbind_all('<MouseWheel>'))

        warn = tk.Frame(inner, bg='#fff3cd')
        warn.pack(fill='x')
        tk.Label(
            warn,
            text='⚠  EXPERIMENTELL FUNKTION  —  '
                 'Programmet försöker hitta bästa (start, längd) per försäkring '
                 'via greedy-sökning. Beloppen approximeras genom kapital-bevarande '
                 'linjär skalning. Använd resultatet som en riktlinje för att '
                 'hämta en ny plan från minpension.se och justera därifrån.',
            bg='#fff3cd', fg='#7a5900',
            font=('TkDefaultFont', 9, 'bold'),
            wraplength=900, justify='left', padx=8, pady=6,
        ).pack(fill='x')

        # Inställningar
        ctrl = ttk.Frame(inner, padding=(8, 8, 8, 4))
        ctrl.pack(fill='x')

        ttk.Label(
            ctrl, text='Utgångsplan:',
            font=('TkDefaultFont', 9, 'bold'),
        ).grid(row=0, column=0, sticky='w', padx=(0, 6))
        saved_base = ''
        try:
            saved_base = self.app._read_state().get(
                'suggest_base_name', '',
            ) or ''
        except (AttributeError, tk.TclError):
            saved_base = ''
        self.suggest_base_var = tk.StringVar(value=saved_base)
        self.suggest_base_combo = ttk.Combobox(
            ctrl, textvariable=self.suggest_base_var,
            state='readonly', width=30,
        )
        self.suggest_base_combo.grid(row=0, column=1, sticky='w', columnspan=3)
        self.suggest_base_var.trace_add(
            'write', lambda *_: self._save_suggest_base(),
        )

        ttk.Label(
            ctrl, text='Allmän pension tidigast från (år-mån):',
            font=('TkDefaultFont', 9, 'bold'),
        ).grid(row=1, column=0, sticky='w', padx=(0, 6), pady=(6, 0))
        st_all = self._read_state_safe()
        try:
            sy_saved = int(st_all.get('suggest_allman_year', 2028) or 2028)
        except (ValueError, TypeError):
            sy_saved = 2028
        try:
            sm_saved = int(st_all.get('suggest_allman_month', 5) or 5)
        except (ValueError, TypeError):
            sm_saved = 5
        self.suggest_allman_year_var = tk.IntVar(value=sy_saved)
        self.suggest_allman_month_var = tk.IntVar(value=sm_saved)
        self.suggest_allman_year_var.trace_add(
            'write', lambda *_: self._save_suggest_allman(),
        )
        self.suggest_allman_month_var.trace_add(
            'write', lambda *_: self._save_suggest_allman(),
        )
        ttk.Spinbox(
            ctrl, textvariable=self.suggest_allman_year_var,
            from_=2020, to=2040, width=6,
        ).grid(row=1, column=1, sticky='w', pady=(6, 0))
        ttk.Spinbox(
            ctrl, textvariable=self.suggest_allman_month_var,
            from_=1, to=12, width=4,
        ).grid(row=1, column=2, sticky='w', padx=(4, 0), pady=(6, 0))
        ttk.Label(
            ctrl,
            text='(3 år före riktålder för Inkomst- och Premiepension)',
            font=('TkDefaultFont', 9), foreground='#666',
        ).grid(row=1, column=3, sticky='w', padx=(8, 0), pady=(6, 0))

        # Kvar-strategi (egen — inte delad med Jämför-fönstret)
        ttk.Label(
            ctrl, text='Kvar-strategi:',
            font=('TkDefaultFont', 9, 'bold'),
        ).grid(row=2, column=0, sticky='w', padx=(0, 6), pady=(6, 0))
        for i, lbl in enumerate(KVAR_LABELS):
            ttk.Radiobutton(
                ctrl, text=lbl, variable=self.suggest_kvar_var, value=i,
            ).grid(row=2, column=1 + i, sticky='w', padx=4, pady=(6, 0))

        # Optimera för
        ttk.Label(
            ctrl, text='Optimera för:',
            font=('TkDefaultFont', 9, 'bold'),
        ).grid(row=3, column=0, sticky='w', pady=(6, 0))
        saved_metric = 'min'
        try:
            saved_metric = self.app._read_state().get(
                'suggest_metric', 'min',
            ) or 'min'
        except (AttributeError, tk.TclError):
            pass
        self.suggest_metric_var = tk.StringVar(value=saved_metric)
        self.suggest_metric_var.trace_add(
            'write', lambda *_: self._save_suggest_metric(),
        )
        metrics = [
            ('end', 'Slutvärde'),
            ('min', 'Lägsta värde'),
            ('total', 'Total'),
            ('match', 'Matcha utgifter'),
            ('balanced', 'Balanserad'),
        ]
        for i, (val, lbl) in enumerate(metrics):
            ttk.Radiobutton(
                ctrl, text=lbl, variable=self.suggest_metric_var, value=val,
            ).grid(row=3, column=1 + i, sticky='w', padx=4, pady=(6, 0))
        ttk.Label(
            ctrl,
            text='(Slutvärde: skjuter uttag framåt. Lägsta: jämnar dippar. '
                 'Total: mellanvariant. Matcha: min. månatliga underskott. '
                 'Balanserad: blandar Match + Slutvärde via balans nedan.)',
            font=('TkDefaultFont', 8), foreground='#666',
        ).grid(row=4, column=1, columnspan=5, sticky='w', padx=4)

        # Balans
        saved_balance = 50
        try:
            saved_balance = int(self.app._read_state().get(
                'suggest_balance', 50,
            ) or 50)
        except (ValueError, tk.TclError, AttributeError):
            saved_balance = 50
        ttk.Label(
            ctrl, text='Balans (Match ↔ Slutvärde):',
            font=('TkDefaultFont', 9, 'bold'),
        ).grid(row=5, column=0, sticky='w', pady=(6, 0))
        self.suggest_balance_var = tk.IntVar(value=saved_balance)
        self.suggest_balance_var.trace_add(
            'write', lambda *_: self._save_suggest_balance(),
        )
        ttk.Spinbox(
            ctrl, textvariable=self.suggest_balance_var,
            from_=0, to=100, increment=5, width=6,
        ).grid(row=5, column=1, sticky='w', pady=(6, 0), padx=(6, 0))
        ttk.Label(
            ctrl,
            text='(0 = pure Match, 100 = pure Slutvärde, 50 = balanserad. '
                 'Bara aktiv för "Balanserad"-mode.)',
            font=('TkDefaultFont', 8), foreground='#666',
        ).grid(row=5, column=2, columnspan=4, sticky='w', padx=(8, 0), pady=(6, 0))

        # Sparande-golv
        ttk.Label(
            ctrl, text='Min sparande (kr):',
            font=('TkDefaultFont', 9, 'bold'),
        ).grid(row=6, column=0, sticky='w', pady=(6, 0))
        saved_floor = self._read_state_safe().get('suggest_floor', '') or ''
        self.suggest_floor_var = tk.StringVar(value=str(saved_floor))
        self.suggest_floor_var.trace_add(
            'write', lambda *_: self._save_suggest_floor(),
        )
        ttk.Entry(
            ctrl, textvariable=self.suggest_floor_var, width=12,
        ).grid(row=6, column=1, sticky='w', pady=(6, 0), padx=(6, 0))
        ttk.Label(
            ctrl,
            text='(Sparandet får inte gå under detta någon gång. '
                 'Tomt = ingen gräns.)',
            font=('TkDefaultFont', 8), foreground='#666',
        ).grid(row=6, column=2, columnspan=4, sticky='w', padx=(8, 0), pady=(6, 0))

        # Slutår (egen)
        ttk.Label(
            ctrl, text='Slutår för optimering:',
            font=('TkDefaultFont', 9, 'bold'),
        ).grid(row=7, column=0, sticky='w', pady=(6, 0))
        ttk.Spinbox(
            ctrl, textvariable=self.suggest_end_year_var,
            from_=2020, to=2100, width=8,
        ).grid(row=7, column=1, sticky='w', pady=(6, 0), padx=(6, 0))
        self.suggest_end_year_hint = ttk.Label(
            ctrl, text='', font=('TkDefaultFont', 9), foreground='#666',
        )
        self.suggest_end_year_hint.grid(
            row=7, column=2, columnspan=4, sticky='w',
            padx=(8, 0), pady=(6, 0),
        )
        self.suggest_end_year_var.trace_add(
            'write', lambda *_: self._update_suggest_end_year_hint(),
        )
        self.suggest_base_var.trace_add(
            'write', lambda *_: self._update_suggest_end_year_hint(),
        )

        self.suggest_run_btn = ttk.Button(
            ctrl, text='Föreslå plan', command=self._run_suggest,
        )
        self.suggest_run_btn.grid(row=8, column=0, sticky='w', pady=(8, 0))
        self.suggest_status = ttk.Label(
            ctrl, text='', font=('TkDefaultFont', 9), foreground='#555',
        )
        self.suggest_status.grid(
            row=8, column=1, columnspan=5, sticky='w', padx=(8, 0), pady=(8, 0),
        )

        # Lås-tabell
        lock_wrap = ttk.LabelFrame(
            inner,
            text=' Lås försäkringar (behåller start + längd oförändrat) ',
            padding=(8, 4, 8, 6),
        )
        lock_wrap.pack(fill='x', padx=8, pady=(4, 0))
        self.suggest_lock_frame = ttk.Frame(lock_wrap)
        self.suggest_lock_frame.pack(fill='x')
        self.suggest_lock_vars = {}
        saved_locked = []
        try:
            saved_locked = self.app._read_state().get(
                'suggest_locked_names', []
            ) or []
        except (AttributeError, tk.TclError):
            saved_locked = []
        self._suggest_saved_locked = set(saved_locked)
        self.suggest_base_var.trace_add(
            'write', lambda *_: self._on_suggest_base_changed(),
        )

        # Body
        self.suggest_body = ttk.Frame(inner)
        self.suggest_body.pack(fill='both', expand=True, padx=8, pady=4)

        # State
        self._suggest_cancel = False
        self._suggest_running = False
        self._suggest_last_result = None
        self._update_suggest_end_year_hint()
        # Bygg lås-tabellen vid öppning (trace_add på base_var triggas inte
        # vid första set() innan tracen bundits)
        self._rebuild_suggest_lock_table()

    # Alla suggest-metoderna nedan är kopierade från CompareWindow
    # och lätt anpassade (self.top pekar nu på egen Toplevel).

    def _on_suggest_base_changed(self):
        self._suggest_last_result = None
        for w in list(self.suggest_body.winfo_children()):
            w.destroy()
        self._rebuild_suggest_lock_table()
        self._update_suggest_end_year_hint()

    def _rebuild_suggest_lock_table(self):
        for w in list(self.suggest_lock_frame.winfo_children()):
            w.destroy()
        base_name = self.suggest_base_var.get()
        base_tab = next(
            (t for t in self.app.tabs
             if t.parsed and t.parsed.plan_name == base_name),
            None,
        )
        if base_tab is None or base_tab.parsed is None:
            ttk.Label(
                self.suggest_lock_frame,
                text='(Välj en utgångsplan för att se försäkringar)',
                font=('TkDefaultFont', 9), foreground='#888',
            ).pack(anchor='w')
            return
        prev_locked = {
            name for name, v in self.suggest_lock_vars.items() if v.get()
        }
        if not prev_locked and self._suggest_saved_locked:
            prev_locked = set(self._suggest_saved_locked)
            self._suggest_saved_locked = set()

        self.suggest_lock_vars = {}
        header = ttk.Frame(self.suggest_lock_frame)
        header.pack(fill='x', pady=(0, 4))
        ttk.Button(
            header, text='Lås allmän pension',
            command=self._lock_allman_pension,
        ).pack(side='left')
        ttk.Button(
            header, text='Lås Livet ut',
            command=self._lock_lifetime,
        ).pack(side='left', padx=(6, 0))
        ttk.Button(
            header, text='Lås alla',
            command=lambda: self._lock_all(True),
        ).pack(side='left', padx=(6, 0))
        ttk.Button(
            header, text='Lås inga',
            command=lambda: self._lock_all(False),
        ).pack(side='left', padx=(6, 0))

        grid = ttk.Frame(self.suggest_lock_frame)
        grid.pack(fill='x')
        n = len(base_tab.parsed.insurances)
        half = (n + 1) // 2
        for i, ins in enumerate(base_tab.parsed.insurances):
            col = 0 if i < half else 1
            row = i if col == 0 else (i - half)
            var = tk.BooleanVar(value=ins.name in prev_locked)
            var.trace_add('write', lambda *_: self._save_suggest_locked())
            self.suggest_lock_vars[ins.name] = var
            start = f'{ins.start[0]}-{ins.start[1]:02d}' if ins.start else '?'
            if ins.duration is None:
                dur = 'Livet ut'
            else:
                dur = f'{ins.duration[0]} år'
                if ins.duration[1]:
                    dur += f' {ins.duration[1]} mån'
            label = f'{ins.name} ({start} · {dur})'
            ttk.Checkbutton(
                grid, text=label, variable=var,
            ).grid(row=row, column=col, sticky='w', padx=(0, 20), pady=1)
        self._save_suggest_locked()

    def _lock_allman_pension(self):
        for name, var in self.suggest_lock_vars.items():
            if _is_allman_pension(name):
                var.set(True)

    def _lock_lifetime(self):
        base_name = self.suggest_base_var.get()
        base_tab = next(
            (t for t in self.app.tabs
             if t.parsed and t.parsed.plan_name == base_name),
            None,
        )
        if base_tab is None:
            return
        livet_ut = {ins.name for ins in base_tab.parsed.insurances
                    if ins.duration is None}
        for name, var in self.suggest_lock_vars.items():
            if name in livet_ut:
                var.set(True)

    def _lock_all(self, value):
        for var in self.suggest_lock_vars.values():
            var.set(value)

    def _save_suggest_locked(self):
        locked = [name for name, v in self.suggest_lock_vars.items()
                  if v.get()]
        try:
            state = self.app._read_state()
            state['suggest_locked_names'] = locked
            with open(STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(state, f, indent=2)
        except OSError:
            pass

    def _update_suggest_end_year_hint(self):
        lbl = getattr(self, 'suggest_end_year_hint', None)
        if lbl is None:
            return
        try:
            end_year = int(self.suggest_end_year_var.get())
        except (ValueError, tk.TclError):
            end_year = None
        base_name = self.suggest_base_var.get()
        base_tab = next(
            (t for t in self.app.tabs
             if t.parsed and t.parsed.plan_name == base_name),
            None,
        )
        age_txt = ''
        if base_tab and base_tab.parsed and end_year:
            by = base_tab.parsed.birth_year
            if by:
                age = end_year - by
                age_txt = f'ålder {age} år vid slutår {end_year}   ·   '
        try:
            lbl.config(
                text=f'{age_txt}skattesatser hämtas från utgångsplanens '
                     f'breakpoints (skattesänkning året efter riktåldern '
                     f'fångas automatiskt)',
            )
        except tk.TclError:
            pass

    def _save_suggest_metric(self):
        try:
            state = self.app._read_state()
            state['suggest_metric'] = self.suggest_metric_var.get() or 'min'
            with open(STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(state, f, indent=2)
        except OSError:
            pass

    def _save_suggest_balance(self):
        try:
            state = self.app._read_state()
            state['suggest_balance'] = int(self.suggest_balance_var.get())
            with open(STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(state, f, indent=2)
        except (ValueError, tk.TclError, OSError):
            pass

    def _save_suggest_base(self):
        try:
            state = self.app._read_state()
            state['suggest_base_name'] = self.suggest_base_var.get() or ''
            with open(STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(state, f, indent=2)
        except (tk.TclError, OSError):
            pass

    def _save_manual_base(self):
        try:
            state = self.app._read_state()
            state['manual_base_name'] = self.manual_base_var.get() or ''
            with open(STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(state, f, indent=2)
        except (tk.TclError, OSError):
            pass

    def _save_suggest_allman(self):
        try:
            state = self.app._read_state()
            state['suggest_allman_year'] = int(self.suggest_allman_year_var.get())
            state['suggest_allman_month'] = int(self.suggest_allman_month_var.get())
            with open(STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(state, f, indent=2)
        except (ValueError, tk.TclError, OSError):
            pass

    def _save_suggest_floor(self):
        try:
            state = self.app._read_state()
            state['suggest_floor'] = self.suggest_floor_var.get() or ''
            with open(STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(state, f, indent=2)
        except (tk.TclError, OSError):
            pass

    def _save_manual_configs(self, base_name):
        """Spara per-försäkring-configs (start/dur/livet) i state, nycklat
        per utgångsplan-namn så olika planer får egna sparade configs."""
        if not base_name or not self._manual_configs:
            return
        configs = []
        for c in self._manual_configs:
            try:
                sy = int(c['sy_var'].get())
                sm = int(c['sm_var'].get())
                dy = int(c['dy_var'].get())
                dm = int(c['dm_var'].get())
                livet = bool(c['livet_var'].get())
            except (ValueError, tk.TclError):
                continue
            configs.append({
                'name': c['name'],
                'sy': sy, 'sm': sm, 'dy': dy, 'dm': dm, 'livet': livet,
            })
        try:
            state = self.app._read_state()
            all_saved = state.get('manual_configs_by_base') or {}
            if not isinstance(all_saved, dict):
                all_saved = {}
            all_saved[base_name] = configs
            state['manual_configs_by_base'] = all_saved
            with open(STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(state, f, indent=2)
        except (tk.TclError, OSError):
            pass

    def _delete_manual_configs(self, base_name):
        """Ta bort sparad config för given utgångsplan ur state (om finns)."""
        if not base_name:
            return
        try:
            state = self.app._read_state()
            all_saved = state.get('manual_configs_by_base') or {}
            if isinstance(all_saved, dict) and base_name in all_saved:
                del all_saved[base_name]
                state['manual_configs_by_base'] = all_saved
                with open(STATE_FILE, 'w', encoding='utf-8') as f:
                    json.dump(state, f, indent=2)
        except (tk.TclError, OSError):
            pass

    def _load_manual_configs(self, base_name):
        """Läs sparade configs för given utgångsplan; returnerar dict {name: cfg}
        eller None om inget sparat finns."""
        try:
            state = self.app._read_state()
            all_saved = state.get('manual_configs_by_base') or {}
            saved = all_saved.get(base_name)
        except (tk.TclError, OSError):
            return None
        if not isinstance(saved, list):
            return None
        return {sc['name']: sc for sc in saved if isinstance(sc, dict)}

    def _save_manual_allman(self):
        try:
            state = self.app._read_state()
            state['manual_allman_year'] = int(self.manual_allman_year_var.get())
            state['manual_allman_month'] = int(self.manual_allman_month_var.get())
            with open(STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(state, f, indent=2)
        except (ValueError, tk.TclError, OSError):
            pass

    def _refresh_suggest_bases(self):
        combo = getattr(self, 'suggest_base_combo', None)
        if combo is None:
            return
        names = [
            t.parsed.plan_name for t in self.app.tabs
            if t.parsed is not None and t.parsed.plan_name
        ]
        cur = self.suggest_base_var.get()
        combo['values'] = names
        if names:
            if cur not in names:
                self.suggest_base_var.set(names[0])
        else:
            self.suggest_base_var.set('')

    def _run_suggest(self):
        if getattr(self, '_suggest_running', False):
            self._suggest_cancel = True
            return
        base_name = self.suggest_base_var.get()
        base_tab = next(
            (t for t in self.app.tabs
             if t.parsed and t.parsed.plan_name == base_name),
            None,
        )
        if base_tab is None or base_tab.parsed is None:
            self.suggest_status.config(
                text='Välj en utgångsplan.', foreground='#a00',
            )
            return
        sparande = self.app.get_sparande() or 0.0
        growth = self.app.get_growth_pct()
        growth_mode = self.app.get_growth_mode()
        brackets = self.app.get_brackets()
        try:
            end_year = int(self.suggest_end_year_var.get())
        except (tk.TclError, ValueError):
            end_year = None
        try:
            allman_min = (
                int(self.suggest_allman_year_var.get()),
                int(self.suggest_allman_month_var.get()),
            )
        except (tk.TclError, ValueError):
            allman_min = None
        kvar_idx = self.suggest_kvar_var.get()
        if kvar_idx not in (0, 1, 2):
            kvar_idx = 1
        metric_type = self.suggest_metric_var.get() or 'end'
        floor_txt = (self.suggest_floor_var.get() or '').replace(' ', '').replace('\xa0', '')
        try:
            min_kvar_floor = float(floor_txt) if floor_txt else None
        except ValueError:
            min_kvar_floor = None

        for w in list(self.suggest_body.winfo_children()):
            w.destroy()
        prog_frame = ttk.Frame(self.suggest_body)
        prog_frame.pack(fill='x', pady=8)
        prog_lbl = ttk.Label(prog_frame, text='Startar…', font=('TkDefaultFont', 9))
        prog_lbl.pack(anchor='w')
        self._suggest_running = True
        self._suggest_cancel = False
        self.suggest_run_btn.config(text='Avbryt')

        def progress(pass_i, ci, name, evals, best):
            def upd():
                try:
                    prog_lbl.config(
                        text=f'Pass {pass_i}  ·  {evals} beräkningar  ·  '
                             f'bäst så här långt: {best:,.0f} kr  ({name[:40]})',
                    )
                except tk.TclError:
                    pass
            self.top.after(0, upd)
            return not self._suggest_cancel

        import threading
        locked_names = {
            name for name, v in self.suggest_lock_vars.items() if v.get()
        }
        try:
            balance = int(self.suggest_balance_var.get()) / 100.0
        except (ValueError, tk.TclError):
            balance = 0.5
        balance = max(0.0, min(1.0, balance))

        def worker():
            try:
                result = optimize_plan_greedy(
                    base_tab.parsed, sparande, brackets, end_year,
                    growth, growth_mode,
                    allman_min_start=allman_min,
                    kvar_idx=kvar_idx,
                    metric_type=metric_type,
                    min_kvar_floor=min_kvar_floor,
                    locked_names=locked_names,
                    balance=balance,
                    progress_cb=progress,
                )
            except Exception as e:
                result = ('error', e)
            def done():
                self._suggest_running = False
                self._suggest_cancel = False
                self.suggest_run_btn.config(text='Föreslå plan')
                if isinstance(result, tuple) and result and result[0] == 'error':
                    self.suggest_status.config(
                        text=f'Fel: {result[1]}', foreground='#a00',
                    )
                    return
                synth_check = result[0] if isinstance(result, tuple) else None
                warn = ''
                if synth_check is not None and min_kvar_floor is not None:
                    tmp_calc = calculate_plan(
                        synth_check, sparande, brackets, end_year,
                        growth, growth_mode,
                    )
                    if tmp_calc.yearly:
                        attr = f'kvar_{kvar_idx + 1}'
                        min_val = min(getattr(y, attr) for y in tmp_calc.yearly)
                        if min_val < min_kvar_floor:
                            warn = (
                                f'  ⚠ Golvet {fmt_kr(min_kvar_floor)} kr '
                                f'kunde inte uppfyllas '
                                f'(lägsta i förslag: {fmt_kr(min_val)} kr)'
                            )
                self.suggest_status.config(
                    text=f'Klart.{warn}',
                    foreground='#a00' if warn else '#080',
                )
                try:
                    self._show_suggest_result(
                        base_tab, result, sparande, brackets, end_year,
                        growth, growth_mode, allman_min,
                        kvar_idx, metric_type,
                    )
                except Exception as e:
                    import traceback
                    tb = traceback.format_exc()
                    self.suggest_status.config(
                        text=f'Fel vid rendering: {e}', foreground='#a00',
                    )
                    try:
                        with open('suggest_debug.log', 'a', encoding='utf-8') as f:
                            f.write(tb + '\n')
                    except OSError:
                        pass
            self.top.after(0, done)

        threading.Thread(target=worker, daemon=True).start()

    def _show_suggest_result(self, base_tab, result, sparande, brackets,
                             end_year, growth, growth_mode, allman_min,
                             kvar_idx, metric_type):
        synth, configs, best_score, history = result
        self._suggest_last_result = {
            'base': base_tab.parsed.plan_name,
            'synth': synth,
            'configs': configs,
            'best_score': best_score,
            'history': history,
            'sparande': sparande,
            'end_year': end_year,
            'growth': growth,
            'allman_min': allman_min,
            'kvar_idx': kvar_idx,
            'metric_type': metric_type,
        }
        # Aktivera "Utgå från förslag"-knappen på manuell-fliken
        if hasattr(self, '_manual_use_suggest_btn'):
            try:
                self._manual_use_suggest_btn.configure(state='normal')
            except tk.TclError:
                pass
        for w in list(self.suggest_body.winfo_children()):
            w.destroy()
        base_calc = calculate_plan(
            base_tab.parsed, sparande, brackets, end_year, growth, growth_mode,
        )
        synth_calc = calculate_plan(
            synth, sparande, brackets, end_year, growth, growth_mode,
        )
        attr = f'kvar_{kvar_idx + 1}'
        kvar_label = KVAR_LABELS[kvar_idx] if kvar_idx < len(KVAR_LABELS) else f'Kvar #{kvar_idx+1}'
        base_end = getattr(base_calc.yearly[-1], attr) if base_calc.yearly else 0
        synth_end = getattr(synth_calc.yearly[-1], attr) if synth_calc.yearly else 0
        # Totalt månatligt underskott (mest meningsfullt för Match/Balanserad)
        def _sum_deficit(calc):
            return sum(-m.overskott_man for m in calc.monthly
                       if m.overskott_man is not None and m.overskott_man < 0)
        base_deficit = _sum_deficit(base_calc)
        synth_deficit = _sum_deficit(synth_calc)

        metric_label_map = {
            'end': 'slutvärde',
            'min': 'lägsta värde',
            'total': 'summa',
            'match': 'matcha utgifter',
            'balanced': 'balanserad',
        }
        metric_label = metric_label_map.get(metric_type, 'slutvärde')

        summary = ttk.Frame(self.suggest_body)
        summary.pack(fill='x', pady=(4, 4))
        header_txt = (f'Utgångsplan: {base_tab.parsed.plan_name}   ·   '
                      f'Optimerat för: {kvar_label} ({metric_label})')
        ttk.Label(
            summary, text=header_txt,
            font=('TkDefaultFont', 10, 'bold'),
        ).pack(anchor='w')

        if metric_type in ('match', 'balanced'):
            # Två rader: totalt underskott + slutvärde, med diff mot original
            def_diff = synth_deficit - base_deficit
            end_diff = synth_end - base_end
            ttk.Label(
                summary,
                text=f'Totalt underskott mot utgifter:   '
                     f'Original {fmt_kr(base_deficit)} kr   ·   '
                     f'Förslag {fmt_kr(synth_deficit)} kr   ·   '
                     f'Delta {fmt_kr(def_diff)} kr '
                     f'({"bättre" if def_diff < 0 else "sämre" if def_diff > 0 else "oförändrat"})',
                font=('TkDefaultFont', 10),
            ).pack(anchor='w')
            ttk.Label(
                summary,
                text=f'Slutvärde ({kvar_label}) vid {end_year or "slutet"}:   '
                     f'Original {fmt_kr(base_end)} kr   ·   '
                     f'Förslag {fmt_kr(synth_end)} kr   ·   '
                     f'Delta {fmt_kr(end_diff)} kr',
                font=('TkDefaultFont', 10),
            ).pack(anchor='w')
        else:
            # Räkna motsvarande metric-värde för originalplanen
            base_vals = [getattr(y, attr) for y in base_calc.yearly]
            if metric_type == 'end':
                base_metric = base_vals[-1] if base_vals else 0
            elif metric_type == 'min':
                base_metric = min(base_vals) if base_vals else 0
            elif metric_type == 'total':
                base_metric = sum(base_vals) if base_vals else 0
            else:
                base_metric = base_end
            ttk.Label(
                summary,
                text=f'Original {metric_label}: {fmt_kr(base_metric)} kr   ·   '
                     f'Förslag: {fmt_kr(best_score)} kr   ·   '
                     f'Delta: {fmt_kr(best_score - base_metric)} kr',
                font=('TkDefaultFont', 10),
            ).pack(anchor='w')

        btn_frame = ttk.Frame(self.suggest_body)
        btn_frame.pack(fill='x', pady=(0, 6))
        ttk.Button(
            btn_frame, text='Kopiera förslag till urklipp',
            command=self._copy_suggest_to_clipboard,
        ).pack(side='left')
        ttk.Button(
            btn_frame, text='Skapa som plan-flik i huvudfönstret',
            command=self._create_sim_plan_tab,
        ).pack(side='left', padx=(8, 0))

        base_years = [y.year for y in base_calc.yearly]
        base_vals = [getattr(y, attr) for y in base_calc.yearly]
        synth_years = [y.year for y in synth_calc.yearly]
        synth_vals = [getattr(y, attr) for y in synth_calc.yearly]
        fig = Figure(figsize=(9, 3.2), dpi=100)
        ax = fig.add_subplot(111)
        ax.plot(base_years, base_vals, color='#1f77b4', linewidth=1.6,
                marker='o', markersize=3, label=f'Original ({base_tab.parsed.plan_name})')
        ax.plot(synth_years, synth_vals, color='#2ca02c', linewidth=1.8,
                marker='o', markersize=3, label='Förslag')
        ax.axhline(0, color='#888', linewidth=0.8, linestyle='--')
        if synth_years:
            pre = synth_years[0] - 1
            ax.plot([pre], [sparande], 'k.', markersize=4)
            ax.annotate(f'Start: {fmt_kr(sparande)}', xy=(pre, sparande),
                        xytext=(4, 4), textcoords='offset points',
                        fontsize=8, color='#555')
        ax.set_xlabel('År')
        ax.set_ylabel(f'{kvar_label} (kr)')
        ax.set_title('Sparande kvar över tid')
        ax.grid(True, linestyle=':', alpha=0.5)
        ax.legend(loc='best', fontsize=8)
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: fmt_kr(v)))
        fig.tight_layout()
        graph_frame = ttk.Frame(self.suggest_body)
        graph_frame.pack(fill='x', pady=(0, 6))
        canvas = FigureCanvasTkAgg(fig, master=graph_frame)
        canvas.get_tk_widget().pack(fill='x', expand=False)
        canvas.draw()

        cols = ('name', 'orig_start', 'orig_dur', 'new_start', 'new_dur', 'monthly', 'note')
        headers = {
            'name': 'Försäkring', 'orig_start': 'Original start',
            'orig_dur': 'Original längd', 'new_start': 'Ny start',
            'new_dur': 'Ny längd', 'monthly': 'Belopp/mån (approx)',
            'note': 'Not',
        }
        widths = {
            'name': 320, 'orig_start': 110, 'orig_dur': 110,
            'new_start': 110, 'new_dur': 110, 'monthly': 130, 'note': 200,
        }
        tree_frame = ttk.Frame(self.suggest_body)
        tree_frame.pack(fill='both', expand=True)
        tree = ttk.Treeview(tree_frame, columns=cols, show='headings', height=14)
        for c in cols:
            tree.heading(c, text=headers[c])
            tree.column(c, width=widths[c], anchor='center' if c != 'name' else 'w')
        for c in configs:
            orig = c['orig_ins']
            orig_start = f'{orig.start[0]}-{orig.start[1]:02d}' if orig.start else ''
            orig_dur = 'Livet ut' if orig.duration is None else (
                f'{orig.duration[0]} år' + (
                    f' {orig.duration[1]} mån' if orig.duration[1] else ''
                )
            )
            new_start = f'{c["start"][0]}-{c["start"][1]:02d}' if c['start'] else ''
            new_dur = 'Livet ut' if c['dur'] is None else (
                f'{c["dur"][0]} år' + (
                    f' {c["dur"][1]} mån' if c['dur'][1] else ''
                )
            )
            notes = []
            if (orig.start, orig.duration) != (c['start'], c['dur']):
                notes.append('ändrad')
            if _is_allman_pension(c['name']):
                notes.append('allmän')
            tree.insert('', 'end', values=(
                c['name'], orig_start, orig_dur,
                new_start, new_dur, fmt_kr(round(c['monthly'])),
                ', '.join(notes),
            ))
        vsb = ttk.Scrollbar(tree_frame, orient='vertical', command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side='left', fill='both', expand=True)
        vsb.pack(side='right', fill='y')

    def _copy_suggest_to_clipboard(self):
        info = getattr(self, '_suggest_last_result', None)
        if not info:
            return
        lines = []
        lines.append(f'Föreslagen plan (utgår från: {info["base"]})')
        allman = info.get('allman_min')
        if allman:
            lines.append(
                f'Allmän pension tidigast: {allman[0]}-{allman[1]:02d}'
            )
        lines.append(
            f'Slutår: {info.get("end_year", "")}   '
            f'Sparande: {fmt_kr(info.get("sparande", 0))} kr   '
            f'Tillväxt: {info.get("growth", 0)} %'
        )
        lines.append('')
        lines.append(f'{"Försäkring":<45} {"Start":<10} {"Längd":<12} {"Belopp/mån":>12}')
        lines.append('-' * 82)
        for c in info['configs']:
            new_start = f'{c["start"][0]}-{c["start"][1]:02d}' if c['start'] else ''
            new_dur = 'Livet ut' if c['dur'] is None else (
                f'{c["dur"][0]} år' + (
                    f' {c["dur"][1]} mån' if c['dur'][1] else ''
                )
            )
            note = ''
            orig = c['orig_ins']
            if (orig.start, orig.duration) != (c['start'], c['dur']):
                note = '  *'
            lines.append(
                f'{c["name"][:45]:<45} {new_start:<10} {new_dur:<12} '
                f'{fmt_kr(round(c["monthly"])):>12}{note}'
            )
        lines.append('')
        lines.append(f'Slutvärde (approx): {fmt_kr(info["best_score"])} kr')
        lines.append('* = ändrad från originalet')
        text = '\n'.join(lines)
        try:
            self.top.clipboard_clear()
            self.top.clipboard_append(text)
        except tk.TclError:
            pass
        self.suggest_status.config(
            text='Kopierat till urklipp.', foreground='#080',
        )

    def _create_sim_plan_tab(self):
        info = getattr(self, '_suggest_last_result', None)
        if not info:
            self.suggest_status.config(
                text='Kör Föreslå plan först.', foreground='#a00',
            )
            return
        current_base = self.suggest_base_var.get()
        if current_base and info.get('base') != current_base:
            self.suggest_status.config(
                text=f'Resultatet är för "{info.get("base")}", inte '
                     f'"{current_base}". Kör Föreslå plan igen.',
                foreground='#a00',
            )
            return
        source = info['base']
        new_name = f'SIM_FROM_{source}'
        existing_names = {
            t.parsed.plan_name for t in self.app.tabs
            if t.parsed is not None
        }
        final_name = new_name
        n = 2
        while final_name in existing_names:
            final_name = f'{new_name} #{n}'
            n += 1
        import copy
        synth = copy.deepcopy(info['synth'])
        synth.plan_name = final_name
        tab = self.app.add_tab(select=True)
        try:
            tab.apply_parsed(synth)
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            self.suggest_status.config(
                text=f'Fel: {e}', foreground='#a00',
            )
            try:
                with open('suggest_debug.log', 'a', encoding='utf-8') as f:
                    f.write(tb + '\n')
            except OSError:
                pass
            return
        self.suggest_status.config(
            text=f'Skapade fliken "{final_name}" i huvudfönstret.',
            foreground='#080',
        )
        # Uppdatera dropdown så nya planen är valbar direkt
        self._refresh_suggest_bases()

    # ================================================================
    # Planera manuellt-fliken
    # ================================================================
    def _build_manual_content(self, parent):
        """Bygg UI för manuell plan-planering."""
        # State
        self._manual_configs = []  # [{name, orig_ins, start_y_var, start_m_var, dur_y_var, dur_m_var, livet_var}]
        self._manual_debounce_id = None
        self._manual_last_synth = None

        # Kontroller överst
        ctrl = ttk.Frame(parent, padding=(8, 8, 8, 4))
        ctrl.pack(fill='x')
        ttk.Label(
            ctrl, text='Utgångsplan:',
            font=('TkDefaultFont', 9, 'bold'),
        ).grid(row=0, column=0, sticky='w', padx=(0, 6))
        saved_manual_base = ''
        try:
            saved_manual_base = self.app._read_state().get(
                'manual_base_name', '',
            ) or ''
        except (AttributeError, tk.TclError):
            saved_manual_base = ''
        self.manual_base_var = tk.StringVar(value=saved_manual_base)
        self.manual_base_combo = ttk.Combobox(
            ctrl, textvariable=self.manual_base_var,
            state='readonly', width=30,
        )
        self.manual_base_combo.grid(row=0, column=1, sticky='w')
        self.manual_base_var.trace_add(
            'write', lambda *_: (self._on_manual_base_changed(),
                                 self._save_manual_base()),
        )
        # Utgå från förslag-knapp: disabled tills Föreslå plan körts
        self._manual_use_suggest_btn = ttk.Button(
            ctrl, text='Utgå från förslag',
            command=self._manual_copy_from_suggest,
            state='disabled',
        )
        self._manual_use_suggest_btn.grid(
            row=0, column=2, sticky='w', padx=(6, 0),
        )
        # Skapa- och Nollställ-knapparna är bara aktiva när raderna avviker
        # från utgångsplanen (styrs av _update_manual_diff_indicator). Utan
        # avvikelse finns planen redan som flik och det finns inget att
        # nollställa.
        self._manual_create_btn = ttk.Button(
            ctrl, text='Skapa som plan-flik i huvudfönstret',
            command=self._manual_create_plan_tab,
            state='disabled',
        )
        self._manual_create_btn.grid(row=0, column=3, sticky='w', padx=(6, 0))
        self._manual_reset_btn = ttk.Button(
            ctrl, text='Nollställ till original',
            command=self._manual_reset_to_original,
            state='disabled',
        )
        self._manual_reset_btn.grid(row=0, column=4, sticky='w', padx=(6, 0))
        self.manual_diff_label = ttk.Label(
            ctrl, text='', foreground='#b35900',
            font=('TkDefaultFont', 9, 'bold'),
        )
        self.manual_diff_label.grid(row=0, column=5, sticky='w', padx=(10, 0))

        ttk.Label(
            ctrl, text='Allmän pension tidigast från (år-mån):',
            font=('TkDefaultFont', 9, 'bold'),
        ).grid(row=1, column=0, sticky='w', padx=(0, 6), pady=(6, 0))
        st_m = self._read_state_safe()
        try:
            my_saved = int(st_m.get('manual_allman_year', 2028) or 2028)
        except (ValueError, TypeError):
            my_saved = 2028
        try:
            mm_saved = int(st_m.get('manual_allman_month', 5) or 5)
        except (ValueError, TypeError):
            mm_saved = 5
        self.manual_allman_year_var = tk.IntVar(value=my_saved)
        self.manual_allman_month_var = tk.IntVar(value=mm_saved)
        self.manual_allman_year_var.trace_add(
            'write', lambda *_: self._save_manual_allman(),
        )
        self.manual_allman_month_var.trace_add(
            'write', lambda *_: self._save_manual_allman(),
        )
        allman_frame = ttk.Frame(ctrl)
        allman_frame.grid(row=1, column=1, sticky='w', pady=(6, 0), columnspan=3)
        ttk.Spinbox(
            allman_frame, textvariable=self.manual_allman_year_var,
            from_=2020, to=2040, width=6,
            command=self._schedule_manual_recalc,
        ).pack(side='left')
        ttk.Label(allman_frame, text='-').pack(side='left', padx=1)
        ttk.Spinbox(
            allman_frame, textvariable=self.manual_allman_month_var,
            from_=1, to=12, width=4, format='%02.0f',
            command=self._schedule_manual_recalc,
        ).pack(side='left')

        ttk.Label(
            ctrl, text='Kvar-strategi (för graf):',
            font=('TkDefaultFont', 9, 'bold'),
        ).grid(row=2, column=0, sticky='w', padx=(0, 6), pady=(6, 0))
        kvar_frame = ttk.Frame(ctrl)
        kvar_frame.grid(row=2, column=1, sticky='w', pady=(6, 0), columnspan=3)
        for i, lbl in enumerate(KVAR_LABELS):
            ttk.Radiobutton(
                kvar_frame, text=lbl, variable=self.suggest_kvar_var, value=i,
                command=self._schedule_manual_recalc,
            ).pack(side='left', padx=(0 if i == 0 else 4, 0))

        # Två-kolumns layout: vänster = försäkrings-tabell, höger = grafer
        body = ttk.Frame(parent)
        body.pack(fill='both', expand=True, padx=8, pady=(4, 4))
        body.columnconfigure(0, weight=0)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        left_wrap = ttk.LabelFrame(
            body, text=' Försäkringar (ändra start & längd) ',
            padding=(6, 4, 6, 6),
        )
        left_wrap.grid(row=0, column=0, sticky='nsw', padx=(0, 6))

        # Snapshot-slots (spara/återställ config, max 3 + auto-undo)
        self._manual_snapshots = {1: None, 2: None, 3: None, 'last': None}
        snap_bar = ttk.Frame(left_wrap)
        snap_bar.pack(fill='x', pady=(0, 4))
        ttk.Label(
            snap_bar, text='Snapshots:',
            font=('TkDefaultFont', 9, 'bold'),
        ).pack(side='left', padx=(0, 4))
        self._manual_snap_buttons = {}
        for slot in (1, 2, 3):
            btn = ttk.Button(
                snap_bar, text=f'Slot {slot}: tom', width=18,
                command=lambda s=slot: self._on_snapshot_slot_click(s),
            )
            btn.pack(side='left', padx=(0, 4))
            self._manual_snap_buttons[slot] = btn
        self._manual_undo_btn = ttk.Button(
            snap_bar, text='⏪ Ångra', width=10,
            command=self._undo_snapshot_restore,
        )
        self._manual_undo_btn.pack(side='left', padx=(6, 0))
        self._update_snapshot_buttons()

        # Scroll för försäkrings-tabellen (kan bli många rader)
        self.manual_ins_frame = ttk.Frame(left_wrap)
        self.manual_ins_frame.pack(fill='both', expand=True)

        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky='nsew')
        # grid-baserad layout så vi kan viktera raderna: info/status utan
        # weight, alla tre grafer får weight=1 så de delar tillgänglig höjd
        # och skalar när fönstret ändras.
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=0)  # info
        right.rowconfigure(1, weight=1)  # gantt
        right.rowconfigure(2, weight=1)  # kvar
        right.rowconfigure(3, weight=1)  # månadsinkomst
        right.rowconfigure(4, weight=0)  # pos-label
        right.rowconfigure(5, weight=0)  # status

        # Info-rad
        self.manual_info_var = tk.StringVar(value='Välj utgångsplan för att börja.')
        ttk.Label(
            right, textvariable=self.manual_info_var,
            font=('TkDefaultFont', 10, 'bold'),
        ).grid(row=0, column=0, sticky='ew', pady=(0, 4))

        # Gantt-graf (utbetalningsperioder per försäkring)
        self.manual_gantt_fig = Figure(figsize=(9, 3.8), dpi=100)
        self.manual_gantt_ax = self.manual_gantt_fig.add_subplot(111)
        self.manual_gantt_canvas = FigureCanvasTkAgg(
            self.manual_gantt_fig, master=right,
        )
        self.manual_gantt_canvas.get_tk_widget().grid(
            row=1, column=0, sticky='nsew',
        )
        # Vänsterklick på en stapel → fokusera motsvarande start-år-spinbox
        self.manual_gantt_canvas.mpl_connect(
            'button_press_event', self._on_manual_gantt_click,
        )
        # Högerklick → kontextmeny med val för övriga fält + Livet ut-toggle.
        self.manual_gantt_canvas.get_tk_widget().bind(
            '<Button-3>', self._on_manual_gantt_right_click,
        )

        # Kvar-graf
        self.manual_kvar_fig = Figure(figsize=(9, 3.4), dpi=100)
        self.manual_kvar_ax = self.manual_kvar_fig.add_subplot(111)
        self.manual_kvar_canvas = FigureCanvasTkAgg(
            self.manual_kvar_fig, master=right,
        )
        self.manual_kvar_canvas.get_tk_widget().grid(
            row=2, column=0, sticky='nsew', pady=(6, 0),
        )

        # Månadsinkomst-graf (netto per månad vs utgifter)
        self.manual_income_fig = Figure(figsize=(9, 3.4), dpi=100)
        self.manual_income_ax = self.manual_income_fig.add_subplot(111)
        self.manual_income_canvas = FigureCanvasTkAgg(
            self.manual_income_fig, master=right,
        )
        self.manual_income_canvas.get_tk_widget().grid(
            row=3, column=0, sticky='nsew', pady=(6, 0),
        )

        # Bind resize_event så vänstermarginalen räknas om när fönstret
        # ändrar storlek — annars åker y-labels utanför rutan vid smala
        # fönster (subplots_adjust:s fractions krymper med figure width).
        # ALLA tre grafer använder samma left_inches så x-axlarna alignas
        # lodrätt (år-tick-positionerna radas upp).
        for cvs, fig, top in (
            (self.manual_gantt_canvas, self.manual_gantt_fig, 0.90),
            (self.manual_kvar_canvas, self.manual_kvar_fig, 0.95),
            (self.manual_income_canvas, self.manual_income_fig, 0.90),
        ):
            def _make_handler(_fig, _top, _cvs):
                def _h(_evt):
                    self._apply_manual_fig_margins(
                        _fig, top=_top, bottom=0.17,
                    )
                    _cvs.draw_idle()
                return _h
            cvs.mpl_connect('resize_event', _make_handler(fig, top, cvs))

        # Position-läsare under sista grafen — visar år+månad vid muspekaren
        pos_row = ttk.Frame(right)
        pos_row.grid(row=4, column=0, sticky='ew', pady=(2, 0))
        self.manual_pos_label = ttk.Label(
            pos_row, text='', font=('TkDefaultFont', 9), foreground='#555',
            anchor='w',
        )
        self.manual_pos_label.pack(side='left', fill='x', expand=True)
        # Checkbox som togglar flytande popup-tooltip vid musen
        try:
            saved_tt = bool(self._read_state_safe().get(
                'manual_tooltip_enabled', False
            ))
        except Exception:
            saved_tt = False
        self.manual_tooltip_var = tk.BooleanVar(value=saved_tt)
        self.manual_tooltip_var.trace_add(
            'write', lambda *_: self._save_manual_tooltip(),
        )
        ttk.Checkbutton(
            pos_row, text='Visa som popup',
            variable=self.manual_tooltip_var,
        ).pack(side='right')
        self._manual_tooltip_win = None  # lazy-skapad Toplevel

        # Bind motion/leave på alla tre canvasar
        for cvs, ax in (
            (self.manual_gantt_canvas, self.manual_gantt_ax),
            (self.manual_kvar_canvas, self.manual_kvar_ax),
            (self.manual_income_canvas, self.manual_income_ax),
        ):
            cvs.mpl_connect(
                'motion_notify_event',
                lambda e: self._on_manual_graph_hover(e),
            )
            cvs.mpl_connect(
                'axes_leave_event',
                lambda e: self._on_manual_graph_leave(),
            )
            cvs.mpl_connect(
                'figure_leave_event',
                lambda e: self._on_manual_graph_leave(),
            )

        # Statusrad (Skapa-knappen ligger nu uppe bredvid Utgå-knappen)
        self.manual_status = ttk.Label(
            right, text='', font=('TkDefaultFont', 9), foreground='#555',
            anchor='w',
        )
        self.manual_status.grid(row=5, column=0, sticky='ew', pady=(4, 0))

    def _manual_year_ticks(self, x_lo, x_hi):
        """Ticks vid första året + var 5:e år (2030, 2035, ...). Första året
        utelämnas om det är för nära nästa 5-tal (< 2 år) för att undvika
        överlappande labels."""
        import math
        start = int(math.ceil(x_lo))
        end = int(math.floor(x_hi))
        if end < start:
            return []
        first_5 = start + (5 - start % 5) % 5  # nästa 5-tal >= start
        if first_5 > end:
            return [start]
        fives = list(range(first_5, end + 1, 5))
        if start not in fives and (first_5 - start) >= 2:
            return [start] + fives
        return fives

    def _on_manual_graph_hover(self, event):
        """Uppdatera position-label med år + månad vid muspekaren.
        Räknar också ålder om utgångsplan har birth_year, och om musen är
        över en Gantt-stapel visas också försäkringens namn."""
        if event.xdata is None:
            self.manual_pos_label.config(text='')
            return
        yr = int(event.xdata)
        frac = event.xdata - yr
        month = int(frac * 12) + 1
        if month < 1:
            month = 1
        if month > 12:
            month = 12
        # Ålder om vi känner födelseår
        age_txt = ''
        base = self._get_manual_base_tab()
        if base and base.parsed and base.parsed.birth_year:
            by = base.parsed.birth_year
            bm = base.parsed.birth_month or 1
            total_m = (yr - by) * 12 + (month - bm)
            if total_m >= 0:
                y_old = total_m // 12
                m_old = total_m % 12
                if m_old == 0:
                    age_txt = f'  ·  ålder {y_old} år'
                else:
                    age_txt = f'  ·  ålder {y_old} år, {m_old} mån'
        # Kontextberoende sista fält: försäkringsnamn (Gantt) eller Y-värde
        # (kvar/månadsinkomst)
        extra = ''
        if event.inaxes == self.manual_gantt_ax:
            cfg = self._manual_cfg_from_gantt_ydata(
                event.ydata, event.inaxes,
            )
            if cfg is not None:
                try:
                    sy = int(cfg['sy_var'].get())
                    sm = int(cfg['sm_var'].get())
                    start_s = f'{sy}-{sm:02d}'
                except (ValueError, tk.TclError):
                    start_s = '?'
                if cfg['livet_var'].get():
                    dur_s = 'Livet ut'
                else:
                    try:
                        dy = int(cfg['dy_var'].get())
                        dm = int(cfg['dm_var'].get())
                        dur_s = f'{dy} år'
                        if dm:
                            dur_s += f' {dm} mån'
                    except (ValueError, tk.TclError):
                        dur_s = '?'
                # Uppskattat totalt kapital (före skatt) från originalets amounts
                cap_kr_months, _ = _approx_capital_kr_months(cfg['orig_ins'])
                cap_s = f', ~{fmt_kr(cap_kr_months)} kr totalt före skatt'
                extra = f'  ·  {cfg["name"]} ({start_s}, {dur_s}{cap_s})'
        elif event.inaxes == self.manual_kvar_ax and event.ydata is not None:
            val = round(event.ydata / 1000) * 1000
            extra = f'  ·  {fmt_kr(val)} kr'
        elif event.inaxes == self.manual_income_ax and event.ydata is not None:
            val = round(event.ydata / 100) * 100
            extra = f'  ·  {fmt_kr(val)} kr/mån'
            # Diff netto - utgift för denna månad
            calc = getattr(self, '_manual_last_synth_calc', None)
            if calc:
                mrow = next(
                    (m for m in calc.monthly
                     if m.year == yr and m.month == month),
                    None,
                )
                if mrow is not None:
                    net = mrow.after_tax or 0
                    utg = mrow.utgifter or 0
                    diff = net - utg
                    sign = '+' if diff >= 0 else '−'
                    extra += (f'   (netto {fmt_kr(net)} − utg {fmt_kr(utg)} = '
                              f'{sign}{fmt_kr(abs(diff))})')
        text = f'{yr}-{month:02d}{age_txt}{extra}'
        self.manual_pos_label.config(text=text)
        # Om popup-tooltip är aktiverat: visa/flytta flytande fönster
        if self.manual_tooltip_var.get() and event.guiEvent is not None:
            try:
                x_root = event.guiEvent.x_root
                y_root = event.guiEvent.y_root
            except AttributeError:
                x_root = y_root = None
            if x_root is not None:
                self._show_manual_tooltip(text, x_root + 12, y_root + 12)
        else:
            self._hide_manual_tooltip()

    def _on_manual_graph_leave(self):
        self.manual_pos_label.config(text='')
        self._hide_manual_tooltip()

    def _show_manual_tooltip(self, text, x, y):
        tw = self._manual_tooltip_win
        if tw is None:
            tw = tk.Toplevel(self.top)
            tw.wm_overrideredirect(True)
            tw.configure(bg='#ffffe0', bd=1, relief='solid')
            lbl = ttk.Label(
                tw, text='', background='#ffffe0',
                font=('TkDefaultFont', 9),
                padding=(6, 3),
            )
            lbl.pack()
            tw._label = lbl
            self._manual_tooltip_win = tw
        try:
            tw._label.config(text=text)
            tw.geometry(f'+{x}+{y}')
            tw.deiconify()
            tw.lift()
        except tk.TclError:
            self._manual_tooltip_win = None

    def _hide_manual_tooltip(self):
        tw = self._manual_tooltip_win
        if tw is not None:
            try:
                tw.withdraw()
            except tk.TclError:
                self._manual_tooltip_win = None

    def _save_manual_tooltip(self):
        try:
            state = self.app._read_state()
            state['manual_tooltip_enabled'] = bool(self.manual_tooltip_var.get())
            with open(STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(state, f, indent=2)
        except (tk.TclError, OSError):
            pass
        # Om användaren just stängde av: dölj eventuellt öppen tooltip
        if not self.manual_tooltip_var.get():
            self._hide_manual_tooltip()

    def _apply_manual_year_axis(self, ax, x_lo, x_hi):
        """Sätt major ticks = år-etiketter, minor ticks = varje år för grid."""
        ax.xaxis.set_major_locator(FixedLocator(self._manual_year_ticks(x_lo, x_hi)))
        ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f'{int(v)}'))
        ax.xaxis.set_minor_locator(MultipleLocator(1))
        ax.tick_params(axis='x', labelsize=8, rotation=0)
        ax.grid(True, which='major', axis='x', linestyle=':', alpha=0.55)
        ax.grid(True, which='minor', axis='x', linestyle=':', alpha=0.25)

    def _apply_manual_fig_margins(
        self, fig, top=0.95, bottom=0.17, left_inches=0.95, right_inches=0.05,
    ):
        """Sätt fig-marginaler som fasta pixlar (via inches) istället för
        fractions, så att y-labels alltid får plats även när fönstret krymps.
        subplots_adjust:s left/right är fractions av figure width — vid smala
        fönster blir 0.07 för få pixlar och labels åker utanför rutan.
        left_inches=0.95 (~95 px vid 100 dpi) rymmer 'kr / mån'-ylabel,
        tick-siffror upp till '10 000 000' och en liten paddning."""
        w = fig.get_size_inches()[0]
        if w <= 0:
            return
        # Klampa så vi aldrig går över halva bredden (skulle bli konstigt)
        left = min(0.45, max(0.02, left_inches / w))
        right = max(0.5, min(0.999, 1.0 - right_inches / w))
        fig.subplots_adjust(left=left, right=right, top=top, bottom=bottom)

    def _capture_manual_snapshot(self):
        """Bygg en snapshot-dict från nuvarande spinbox-värden per försäkring.
        Returnerar (timestamp_str, list_of_configs) eller None om inga rader."""
        if not self._manual_configs:
            return None
        import datetime
        configs = []
        for c in self._manual_configs:
            try:
                sy = int(c['sy_var'].get())
                sm = int(c['sm_var'].get())
                dy = int(c['dy_var'].get())
                dm = int(c['dm_var'].get())
                livet = bool(c['livet_var'].get())
            except (ValueError, tk.TclError):
                sy = c['orig_ins'].start[0] if c['orig_ins'].start else 2027
                sm = c['orig_ins'].start[1] if c['orig_ins'].start else 1
                dur = c['orig_ins'].duration or (5, 0)
                dy, dm = dur
                livet = c['orig_ins'].duration is None
            configs.append({
                'name': c['name'],
                'sy': sy, 'sm': sm, 'dy': dy, 'dm': dm, 'livet': livet,
            })
        ts = datetime.datetime.now().strftime('%H:%M')
        return {'ts': ts, 'configs': configs}

    def _apply_manual_snapshot(self, snap):
        """Sätt spinbox-värden från en snapshot. Matchar på försäkringsnamn."""
        if not snap:
            return
        by_name = {sc['name']: sc for sc in snap['configs']}
        for c in self._manual_configs:
            sc = by_name.get(c['name'])
            if sc is None:
                continue
            try:
                c['livet_var'].set(sc['livet'])
                c['sy_var'].set(sc['sy'])
                c['sm_var'].set(sc['sm'])
                c['dy_var'].set(sc['dy'])
                c['dm_var'].set(sc['dm'])
            except tk.TclError:
                pass
        self._schedule_manual_recalc()

    def _update_snapshot_buttons(self):
        """Uppdatera slot-knapparnas etiketter + undo-knappens state."""
        for slot in (1, 2, 3):
            btn = self._manual_snap_buttons.get(slot)
            if btn is None:
                continue
            snap = self._manual_snapshots.get(slot)
            if snap is None:
                btn.config(text=f'Slot {slot}: tom')
            else:
                btn.config(text=f'Slot {slot}: {snap["ts"]}')
        try:
            last = self._manual_snapshots.get('last')
            self._manual_undo_btn.state(
                ['!disabled'] if last is not None else ['disabled']
            )
        except tk.TclError:
            pass

    def _on_snapshot_slot_click(self, slot):
        """Klick på slot-knapp. Tom → spara direkt. Fylld → visa meny."""
        snap = self._manual_snapshots.get(slot)
        if snap is None:
            new = self._capture_manual_snapshot()
            if new is None:
                return
            self._manual_snapshots[slot] = new
            self._update_snapshot_buttons()
            self.manual_status.config(
                text=f'Sparade till Slot {slot} ({new["ts"]}).',
                foreground='#080',
            )
            return
        # Fylld — visa meny
        btn = self._manual_snap_buttons[slot]
        menu = tk.Menu(self.top, tearoff=False)
        menu.add_command(
            label=f'Slot {slot}: {snap["ts"]}',
            state='disabled',
        )
        menu.add_separator()
        menu.add_command(
            label='Återställ',
            command=lambda s=slot: self._restore_snapshot(s),
        )
        menu.add_command(
            label='Skriv över',
            command=lambda s=slot: self._overwrite_snapshot(s),
        )
        menu.add_command(
            label='Rensa',
            command=lambda s=slot: self._clear_snapshot(s),
        )
        try:
            x = btn.winfo_rootx()
            y = btn.winfo_rooty() + btn.winfo_height()
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _restore_snapshot(self, slot):
        snap = self._manual_snapshots.get(slot)
        if snap is None:
            return
        # Spara nuvarande config som "last" så användaren kan ångra
        current = self._capture_manual_snapshot()
        if current is not None:
            self._manual_snapshots['last'] = current
        self._apply_manual_snapshot(snap)
        self._update_snapshot_buttons()
        self.manual_status.config(
            text=f'Återställde från Slot {slot} ({snap["ts"]}). '
                 f'Ångra med ⏪.',
            foreground='#080',
        )

    def _overwrite_snapshot(self, slot):
        new = self._capture_manual_snapshot()
        if new is None:
            return
        self._manual_snapshots[slot] = new
        self._update_snapshot_buttons()
        self.manual_status.config(
            text=f'Skrev över Slot {slot} ({new["ts"]}).',
            foreground='#080',
        )

    def _clear_snapshot(self, slot):
        self._manual_snapshots[slot] = None
        self._update_snapshot_buttons()
        self.manual_status.config(
            text=f'Rensade Slot {slot}.', foreground='#555',
        )

    def _undo_snapshot_restore(self):
        last = self._manual_snapshots.get('last')
        if last is None:
            return
        # Spara nuvarande som ny 'last' så användaren kan ångra ångrandet
        current = self._capture_manual_snapshot()
        self._apply_manual_snapshot(last)
        self._manual_snapshots['last'] = current
        self.manual_status.config(
            text=f'Ångrade återställning (åter till {last["ts"]}).',
            foreground='#080',
        )

    def _manual_colors(self):
        """Delad färgpalett för Gantt-staplar och tabell-swatches."""
        return [
            '#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd',
            '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf',
            '#4682b4', '#daa520',
        ]

    def _on_manual_gantt_click(self, event):
        """Vänsterklick på en stapel → fokusera motsvarande start-år-spinbox
        i tabellen. Sekundärknappen hanteras separat via tk-bindning."""
        if event.button != 1:
            return
        cfg = self._manual_cfg_from_gantt_ydata(event.ydata, event.inaxes)
        if cfg is None:
            return
        try:
            cfg['sy_sb'].focus_set()
            cfg['sy_sb'].selection_range(0, 'end')
        except (tk.TclError, KeyError):
            pass

    def _manual_cfg_from_gantt_ydata(self, ydata, inaxes=None):
        """Hjälpare: konvertera y-data-koord till en config-dict (eller None)."""
        if inaxes is not None and inaxes != self.manual_gantt_ax:
            return None
        if ydata is None:
            return None
        n = len(self._manual_configs)
        if n == 0:
            return None
        y_idx = int(round(ydata))
        if y_idx < 0 or y_idx >= n:
            return None
        cfg_idx = n - 1 - y_idx
        return self._manual_configs[cfg_idx]

    def _on_manual_gantt_right_click(self, event):
        """Högerklick → kontextmeny med val för start (år/mån), längd
        (år/mån om ej Livet ut) samt toggla Livet ut för icke-låsta."""
        # Konvertera tk-widget-pixlar till matplotlib data-koord.
        canvas_widget = self.manual_gantt_canvas.get_tk_widget()
        canvas_h = canvas_widget.winfo_height()
        try:
            # Matplotlib har y från botten; tk från toppen — invertera
            x_data, y_data = self.manual_gantt_ax.transData.inverted().transform(
                (event.x, canvas_h - event.y)
            )
        except Exception:
            return
        cfg = self._manual_cfg_from_gantt_ydata(y_data)
        if cfg is None:
            return

        menu = tk.Menu(self.top, tearoff=False)
        menu.add_command(label=f'{cfg["name"]}', state='disabled')

        # Start-sektion
        menu.add_separator()
        menu.add_command(
            label='Fokusera start-år',
            command=lambda c=cfg: self._manual_focus_widget(c, 'sy_sb'),
        )
        menu.add_command(
            label='Fokusera start-månad',
            command=lambda c=cfg: self._manual_focus_widget(c, 'sm_sb'),
        )
        # Längd-sektion (bara om ej Livet ut)
        is_livet = cfg['livet_var'].get()
        if not is_livet:
            menu.add_separator()
            menu.add_command(
                label='Fokusera längd-år',
                command=lambda c=cfg: self._manual_focus_widget(c, 'dy_sb'),
            )
            menu.add_command(
                label='Fokusera längd-månad',
                command=lambda c=cfg: self._manual_focus_widget(c, 'dm_sb'),
            )
        # Livet ut-toggle (bara för icke-allmän — allmän är tvingad Livet ut)
        if not cfg['is_allman']:
            menu.add_separator()
            if is_livet:
                menu.add_command(
                    label='Ta bort Livet ut (sätt fast längd)',
                    command=lambda c=cfg: c['livet_var'].set(False),
                )
            else:
                menu.add_command(
                    label='Sätt Livet ut',
                    command=lambda c=cfg: c['livet_var'].set(True),
                )
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return 'break'

    def _manual_focus_widget(self, cfg, key):
        try:
            wgt = cfg.get(key)
            if wgt is not None:
                wgt.focus_set()
                try:
                    wgt.selection_range(0, 'end')
                except tk.TclError:
                    pass
        except tk.TclError:
            pass

    def _on_manual_base_changed(self):
        """Ny utgångsplan vald — bygg försäkrings-tabellen från originalet
        och auto-fyll Allmän-pension-tidigast baserat på personens riktålder."""
        base = self._get_manual_base_tab()
        if base is None or base.parsed is None:
            for w in list(self.manual_ins_frame.winfo_children()):
                w.destroy()
            self._manual_configs = []
            self._update_manual_diff_indicator(())
            self.manual_info_var.set('Välj utgångsplan för att börja.')
            self._draw_manual_graphs()
            return
        # Auto-fyll Allmän pension tidigast från riktålder (- 3 år)
        earliest = _earliest_allman_from_birth(
            base.parsed.birth_year, base.parsed.birth_month,
        )
        if earliest is not None:
            try:
                self.manual_allman_year_var.set(earliest[0])
                self.manual_allman_month_var.set(earliest[1])
            except tk.TclError:
                pass
        self._build_manual_ins_rows(base.parsed)
        # Applicera sparade configs för denna utgångsplan (om det finns)
        saved = self._load_manual_configs(base.parsed.plan_name)
        if saved:
            for c in self._manual_configs:
                sc = saved.get(c['name'])
                if sc is None:
                    continue
                try:
                    c['livet_var'].set(bool(sc.get('livet', False)))
                    c['sy_var'].set(int(sc.get('sy', c['sy_var'].get())))
                    c['sm_var'].set(int(sc.get('sm', c['sm_var'].get())))
                    c['dy_var'].set(int(sc.get('dy', c['dy_var'].get())))
                    c['dm_var'].set(int(sc.get('dm', c['dm_var'].get())))
                except (ValueError, tk.TclError):
                    pass
        self._schedule_manual_recalc()

    def _manual_reset_to_original(self):
        """Nollställ raderna till originalvärdena och rensa sparad config
        för denna utgångsplan. Efter recalc sparas de nya (=original)
        värdena så nästa gång planen öppnas visas Manuell = Original."""
        base = self._get_manual_base_tab()
        if base is None or base.parsed is None:
            return
        self._delete_manual_configs(base.parsed.plan_name)
        # Bygg om raderna färskt från originalet
        self._build_manual_ins_rows(base.parsed)
        self._schedule_manual_recalc()
        # Rensa ev. kvarliggande status-text (t.ex. "Kopierat från Föreslå")
        try:
            self.manual_status.config(text='', foreground='#555')
        except tk.TclError:
            pass

    def _get_manual_base_tab(self):
        name = self.manual_base_var.get()
        return next(
            (t for t in self.app.tabs
             if t.parsed and t.parsed.plan_name == name),
            None,
        )

    def _refresh_manual_bases(self):
        """Uppdatera manuellt-flikens utgångsplan-dropdown."""
        combo = getattr(self, 'manual_base_combo', None)
        if combo is None:
            return
        names = [
            t.parsed.plan_name for t in self.app.tabs
            if t.parsed is not None and t.parsed.plan_name
        ]
        cur = self.manual_base_var.get()
        combo['values'] = names
        if names:
            if cur not in names:
                self.manual_base_var.set(names[0])
        else:
            self.manual_base_var.set('')

    def _build_manual_ins_rows(self, parsed):
        """Rensa och bygg om försäkrings-tabellen med spinboxar per försäkring."""
        for w in list(self.manual_ins_frame.winfo_children()):
            w.destroy()
        # Header — kolumn 0 tom för färg-swatchen, försäkringsnamn på kolumn 1
        hdr = ttk.Frame(self.manual_ins_frame)
        hdr.pack(fill='x', pady=(0, 3))
        ttk.Label(hdr, text='', width=2).grid(row=0, column=0)
        ttk.Label(hdr, text='Försäkring',
                  font=('TkDefaultFont', 9, 'bold'), width=32).grid(row=0, column=1, sticky='w')
        ttk.Label(hdr, text='Start (år-mån)',
                  font=('TkDefaultFont', 9, 'bold')).grid(row=0, column=2, padx=(4, 0), sticky='w')
        ttk.Label(hdr, text='Längd (om ej Livet ut)',
                  font=('TkDefaultFont', 9, 'bold')).grid(row=0, column=3, padx=(8, 0), sticky='w')
        ttk.Label(hdr, text='Livet ut',
                  font=('TkDefaultFont', 9, 'bold')).grid(row=0, column=4, padx=(10, 0), sticky='w')

        self._manual_configs = []
        try:
            allman_min = (
                int(self.manual_allman_year_var.get()),
                int(self.manual_allman_month_var.get()),
            )
        except (ValueError, tk.TclError):
            allman_min = (2028, 5)

        palette = self._manual_colors()
        for i, ins in enumerate(parsed.insurances):
            row = ttk.Frame(self.manual_ins_frame)
            row.pack(fill='x', pady=1)
            is_allman = _is_allman_pension(ins.name)

            # Färg-swatch (samma färg som stapeln i Gantt)
            color = palette[i % len(palette)]
            tk.Label(
                row, text='  ', bg=color, width=2,
                relief='solid', borderwidth=1,
            ).grid(row=0, column=0, padx=(0, 4))

            name_lbl = ttk.Label(row, text=ins.name, width=32)
            name_lbl.grid(row=0, column=1, sticky='w')

            # Start: år + månad i egen sub-frame så båda spinboxar syns.
            # Behåll originalets start oförändrat (även om det bryter mot
            # allmän-pension-constraint) — annars räknas försäkringen som
            # "ändrad" och approximation används trots att inget ändrats.
            start = ins.start or (2027, 1)
            # Allmän pension: kläm upp till allmän-min om orig är tidigare
            # (regeln får inte brytas ens av originalet i manuell planering)
            if is_allman and allman_min and start < allman_min:
                start = allman_min
            sy_var = tk.IntVar(value=start[0])
            sm_var = tk.IntVar(value=start[1])
            start_frame = ttk.Frame(row)
            start_frame.grid(row=0, column=2, padx=(4, 0), sticky='w')
            sy_sb = ttk.Spinbox(
                start_frame, textvariable=sy_var, from_=2020, to=2050, width=6,
                command=self._schedule_manual_recalc,
            )
            sy_sb.pack(side='left')
            ttk.Label(start_frame, text='-').pack(side='left', padx=1)
            sm_sb = ttk.Spinbox(
                start_frame, textvariable=sm_var, from_=1, to=12, width=4,
                format='%02.0f',
                command=self._schedule_manual_recalc,
            )
            sm_sb.pack(side='left')
            sy_var.trace_add('write', lambda *_: self._schedule_manual_recalc())
            sm_var.trace_add('write', lambda *_: self._schedule_manual_recalc())
            # Backup-events: trace fires ibland inte förrän focus lämnar fältet
            for wgt in (sy_sb, sm_sb):
                wgt.bind('<KeyRelease>',
                         lambda e: self._schedule_manual_recalc())
                wgt.bind('<FocusOut>',
                         lambda e: self._schedule_manual_recalc())
                wgt.bind('<Return>',
                         lambda e: self._schedule_manual_recalc())

            # Längd: år + månad i egen sub-frame
            dur = ins.duration if ins.duration else (5, 0)
            livet_default = ins.duration is None
            dy_var = tk.IntVar(value=dur[0])
            dm_var = tk.IntVar(value=dur[1])
            livet_var = tk.BooleanVar(value=livet_default)
            dur_frame = ttk.Frame(row)
            dur_frame.grid(row=0, column=3, padx=(8, 0), sticky='w')
            dy_sb = ttk.Spinbox(
                dur_frame, textvariable=dy_var, from_=0, to=40, width=4,
                command=self._schedule_manual_recalc,
            )
            dy_sb.pack(side='left')
            ttk.Label(dur_frame, text='år').pack(side='left', padx=(2, 4))
            dm_sb = ttk.Spinbox(
                dur_frame, textvariable=dm_var, from_=0, to=11, width=4,
                command=self._schedule_manual_recalc,
            )
            dm_sb.pack(side='left')
            ttk.Label(dur_frame, text='mån').pack(side='left', padx=(2, 0))
            dy_var.trace_add('write', lambda *_: self._schedule_manual_recalc())
            dm_var.trace_add('write', lambda *_: self._schedule_manual_recalc())
            for wgt in (dy_sb, dm_sb):
                wgt.bind('<KeyRelease>',
                         lambda e: self._schedule_manual_recalc())
                wgt.bind('<FocusOut>',
                         lambda e: self._schedule_manual_recalc())
                wgt.bind('<Return>',
                         lambda e: self._schedule_manual_recalc())

            livet_cb = ttk.Checkbutton(
                row, variable=livet_var,
                command=self._schedule_manual_recalc,
            )
            livet_cb.grid(row=0, column=4, padx=(10, 0), sticky='w')

            def _update_dur_state(*_, dy=dy_sb, dm=dm_sb, lv=livet_var):
                try:
                    if lv.get():
                        dy.state(['disabled'])
                        dm.state(['disabled'])
                    else:
                        dy.state(['!disabled'])
                        dm.state(['!disabled'])
                except tk.TclError:
                    pass

            livet_var.trace_add(
                'write',
                lambda *_, upd=_update_dur_state: (upd(), self._schedule_manual_recalc()),
            )
            _update_dur_state()  # sätt initialt tillstånd

            # Constraints för allmän pension: Livet ut tvingas
            if is_allman:
                livet_var.set(True)
                try:
                    livet_cb.state(['disabled'])
                except tk.TclError:
                    pass

            self._manual_configs.append({
                'name': ins.name,
                'orig_ins': ins,
                'sy_var': sy_var, 'sm_var': sm_var,
                'dy_var': dy_var, 'dm_var': dm_var,
                'livet_var': livet_var,
                'is_allman': is_allman,
                'sy_sb': sy_sb, 'sm_sb': sm_sb,
                'dy_sb': dy_sb, 'dm_sb': dm_sb,
                'name_lbl': name_lbl,
            })

    def _update_manual_diff_indicator(self, changed_names):
        """Visa om det manuella förslaget avviker från utgångsplanen.
        changed_names = namn på försäkringar vars start/längd skiljer sig
        från originalet. Avvikande rader färgas, en text visas bredvid
        'Nollställ till original', och knapparna 'Nollställ till original'
        och 'Skapa som plan-flik i huvudfönstret' är bara aktiva vid
        avvikelse."""
        changed = set(changed_names or ())
        for c in self._manual_configs:
            lbl = c.get('name_lbl')
            if lbl is None:
                continue
            try:
                lbl.configure(
                    foreground='#b35900' if c['name'] in changed else '',
                )
            except tk.TclError:
                pass
        n = len(changed)
        if n == 0:
            text = ''
        elif n == 1:
            text = '● Avviker från utgångsplanen (1 försäkring ändrad)'
        else:
            text = f'● Avviker från utgångsplanen ({n} försäkringar ändrade)'
        try:
            self.manual_diff_label.configure(text=text)
            btn_state = ['!disabled'] if n else ['disabled']
            self._manual_reset_btn.state(btn_state)
            self._manual_create_btn.state(btn_state)
        except (AttributeError, tk.TclError):
            pass

    def _schedule_manual_recalc(self, *_):
        """Debounced omräkning så snabba spinbox-ändringar inte hackar."""
        if self._manual_debounce_id is not None:
            try:
                self.top.after_cancel(self._manual_debounce_id)
            except tk.TclError:
                pass
        self._manual_debounce_id = self.top.after(150, self._recalc_manual)

    def _recalc_manual(self):
        self._manual_debounce_id = None
        base = self._get_manual_base_tab()
        if base is None or base.parsed is None or not self._manual_configs:
            self._update_manual_diff_indicator(())
            self._draw_manual_graphs()
            return
        try:
            allman_min = (
                int(self.manual_allman_year_var.get()),
                int(self.manual_allman_month_var.get()),
            )
        except (ValueError, tk.TclError):
            allman_min = None

        configs = []
        changed_names = []
        for c in self._manual_configs:
            # Kläm start-månad till 1-12 och start-år till rimligt spann
            try:
                sy = int(c['sy_var'].get())
                sm = int(c['sm_var'].get())
            except (ValueError, tk.TclError):
                sy, sm = c['orig_ins'].start or (2027, 1)
            sy = max(2020, min(2050, sy))
            sm_clamped = max(1, min(12, sm))
            if sm != sm_clamped:
                try:
                    c['sm_var'].set(sm_clamped)
                except tk.TclError:
                    pass
            start = (sy, sm_clamped)
            # Kläm allmän pension till allman_min (regeln får inte brytas)
            if c['is_allman'] and allman_min and start < allman_min:
                start = allman_min
                try:
                    c['sy_var'].set(start[0])
                    c['sm_var'].set(start[1])
                except tk.TclError:
                    pass
            if c['livet_var'].get():
                dur = None
            else:
                # Kläm längd-år 0-40, längd-mån 0-11
                try:
                    dy = int(c['dy_var'].get())
                    dm = int(c['dm_var'].get())
                except (ValueError, tk.TclError):
                    dy, dm = c['orig_ins'].duration or (5, 0)
                dy_clamped = max(0, min(40, dy))
                dm_clamped = max(0, min(11, dm))
                if dy != dy_clamped:
                    try:
                        c['dy_var'].set(dy_clamped)
                    except tk.TclError:
                        pass
                if dm != dm_clamped:
                    try:
                        c['dm_var'].set(dm_clamped)
                    except tk.TclError:
                        pass
                dur = (dy_clamped, dm_clamped)
                if dur[0] == 0 and dur[1] == 0:
                    dur = (5, 0)  # 0 tid meningslöst, ge minst 5 år
            growth = self.app.get_growth_pct() or 0.0
            monthly = _monthly_from_new_config(
                c['orig_ins'], start, dur, growth,
            )
            # Oförändrat = använd originalets faktiska amounts_before_tax
            # per bp-datum istället för approximerat konstant snitt. Det
            # ger exakt match mot originalplanen om inget ändrats.
            unchanged = (
                start == c['orig_ins'].start
                and dur == c['orig_ins'].duration
            )
            if not unchanged:
                changed_names.append(c['name'])
            configs.append({
                'name': c['name'],
                'orig_ins': c['orig_ins'],
                'start': start,
                'dur': dur,
                'monthly': monthly,
                'use_original_amounts': unchanged,
            })

        synth = _synth_optimized_plan(configs, base.parsed)
        synth.plan_name = f'MAN ({base.parsed.plan_name})'
        self._manual_last_synth = synth
        self._update_manual_diff_indicator(changed_names)
        self._draw_manual_graphs()
        # Spara configs i state så de överlever fönster-stängning.
        # Sparade per utgångsplan-namn så olika planer har egna configs.
        self._save_manual_configs(base.parsed.plan_name)

    def _draw_manual_graphs(self):
        """Rita Gantt-graf över utbetalningsperioder och kvar-graf."""
        # ---- Gantt ----
        ax = self.manual_gantt_ax
        ax.clear()
        cfgs = self._manual_configs
        if not cfgs:
            ax.text(0.5, 0.5, 'Ingen data', ha='center', va='center',
                    transform=ax.transAxes, fontsize=10, color='#888')
            ax.set_xticks([])
            ax.set_yticks([])
            self.manual_gantt_canvas.draw_idle()
            self.manual_kvar_ax.clear()
            self.manual_kvar_canvas.draw_idle()
            self.manual_income_ax.clear()
            self.manual_income_canvas.draw_idle()
            return

        # end_display = max(slutår-spinbox, birth+END_AGE) så grafen alltid sträcker
        # sig till slutet av planen ("Räkna till ålder") även om optimerings-slutåret är
        # kortare
        end_display = 2050
        try:
            end_display = int(self.suggest_end_year_var.get())
        except (ValueError, tk.TclError):
            pass
        base_tab = self._get_manual_base_tab()
        if base_tab and base_tab.parsed and base_tab.parsed.birth_year:
            end_display = max(end_display, base_tab.parsed.birth_year + END_AGE)
        colors = self._manual_colors()
        n = len(cfgs)
        for i, c in enumerate(cfgs):
            try:
                sy = int(c['sy_var'].get())
                sm = int(c['sm_var'].get())
            except (ValueError, tk.TclError):
                continue
            start_frac = sy + (sm - 1) / 12.0
            if c['livet_var'].get():
                end_frac = end_display + 1
                arrow = True
            else:
                try:
                    dy = int(c['dy_var'].get())
                    dm = int(c['dm_var'].get())
                except (ValueError, tk.TclError):
                    dy, dm = 5, 0
                end_frac = start_frac + dy + dm / 12.0
                arrow = False
            color = colors[i % len(colors)]
            # y = n-1-i så första försäkringen (i tabellen) hamnar överst
            y = n - 1 - i
            # bar-höjd = 0.7 av rad-avståndet (matplotlib gör redan
            # proportionell skalning av barh när axes-höjden ändras)
            ax.barh(y, end_frac - start_frac, left=start_frac,
                    height=0.7, color=color, edgecolor='#333',
                    linewidth=0.5)
            if arrow:
                ax.annotate('→', xy=(end_frac - 0.5, y), ha='right', va='center',
                            fontsize=12, color=color)
        ax.set_yticks([])
        ax.set_yticklabels([])
        ax.set_ylim(-0.6, n - 0.4)
        # Delad x-range för alla tre grafer så åren alignas lodrätt.
        # Start = tidigaste start bland configs - 0.3 så första stapeln
        # inte klistrar mot vänster kant. End = end_display + 0.5.
        starts = []
        for c in cfgs:
            try:
                sy = int(c['sy_var'].get()); sm = int(c['sm_var'].get())
                starts.append(sy + (sm - 1) / 12.0)
            except (ValueError, tk.TclError):
                pass
        # -1.3 (inte -0.3) så pre_year (år före första utbetalning) ryms i
        # kvar-grafens startpunkt (ursprungssparande visas där)
        x_lo = (min(starts) if starts else 2027) - 1.3
        x_hi = end_display + 0.5
        self._manual_shared_xlim = (x_lo, x_hi)
        ax.set_xlim(x_lo, x_hi)
        ax.set_xlabel('År')
        ax.set_title('Utbetalningsperioder')
        self._apply_manual_year_axis(ax, x_lo, x_hi)
        # Trängre marginaler så graferna får mer plats. Samma left_inches
        # för alla tre grafer så deras x-axlar alignas lodrätt.
        self._apply_manual_fig_margins(
            self.manual_gantt_fig, top=0.90, bottom=0.17,
        )
        self.manual_gantt_canvas.draw_idle()

        # ---- Kvar-graf ----
        base = self._get_manual_base_tab()
        synth = self._manual_last_synth
        if base is None or synth is None:
            self.manual_kvar_ax.clear()
            self.manual_kvar_canvas.draw_idle()
            self.manual_income_ax.clear()
            self.manual_income_canvas.draw_idle()
            return
        sparande = self.app.get_sparande() or 0.0
        brackets = self.app.get_brackets() or []
        growth = self.app.get_growth_pct() or 0.0
        growth_mode = self.app.get_growth_mode()
        try:
            end_year = int(self.suggest_end_year_var.get())
        except (ValueError, tk.TclError):
            end_year = None
        # Räkna hela vägen till planens slut (END_AGE) för graferna även om
        # optimerings-slutåret är kortare — annars slutar kurvorna för
        # tidigt medan x-axeln sträcker sig längre
        graph_end = end_year
        if base.parsed and base.parsed.birth_year:
            plan_end = base.parsed.birth_year + END_AGE
            if graph_end is None or plan_end > graph_end:
                graph_end = plan_end
        try:
            base_calc = calculate_plan(base.parsed, sparande, brackets,
                                       graph_end, growth, growth_mode)
            synth_calc = calculate_plan(synth, sparande, brackets,
                                        graph_end, growth, growth_mode)
        except Exception:
            return
        # Spara för hover-lookup (månadsinkomst-graf visar diff netto vs utg)
        self._manual_last_synth_calc = synth_calc
        kvar_idx = self.suggest_kvar_var.get()
        if kvar_idx not in (0, 1, 2):
            kvar_idx = 1
        attr = f'kvar_{kvar_idx + 1}'
        kvar_label = KVAR_LABELS[kvar_idx]
        b_years = [y.year for y in base_calc.yearly]
        b_vals = [getattr(y, attr) for y in base_calc.yearly]
        s_years = [y.year for y in synth_calc.yearly]
        s_vals = [getattr(y, attr) for y in synth_calc.yearly]
        # Prepend startpunkt vid pre_year (år före första kalkyleråret) med
        # ursprungssparandet — samma stil som huvudfönstret/jämför planer
        if b_years:
            pre_b = b_years[0] - 1
            b_years = [pre_b] + b_years
            b_vals = [sparande] + b_vals
        if s_years:
            pre_s = s_years[0] - 1
            s_years = [pre_s] + s_years
            s_vals = [sparande] + s_vals
        ax2 = self.manual_kvar_ax
        ax2.clear()
        ax2.plot(b_years, b_vals, color='#1f77b4', linewidth=1.4,
                 marker='o', markersize=3,
                 label=f'Original ({base.parsed.plan_name})')
        ax2.plot(s_years, s_vals, color='#2ca02c', linewidth=1.8,
                 marker='o', markersize=3, label='Manuellt förslag')
        ax2.axhline(0, color='#888', linewidth=0.8, linestyle='--')
        ax2.set_xlabel('År')
        ax2.set_ylabel(f'{kvar_label} (kr)')
        ax2.grid(True, axis='y', linestyle=':', alpha=0.5)
        xlim_now = getattr(self, '_manual_shared_xlim', None)
        if xlim_now:
            self._apply_manual_year_axis(ax2, xlim_now[0], xlim_now[1])
        ax2.legend(loc='best', fontsize=8)
        ax2.yaxis.set_major_formatter(FuncFormatter(lambda v, _: fmt_kr(v)))
        # Delad x-range + margin så åren alignas med Gantt/månadsinkomst
        xlim = getattr(self, '_manual_shared_xlim', None)
        if xlim:
            ax2.set_xlim(*xlim)
        self._apply_manual_fig_margins(
            self.manual_kvar_fig, top=0.95, bottom=0.17,
        )
        self.manual_kvar_canvas.draw_idle()

        # ---- Månadsinkomst-graf (netto per månad vs utgifter) ----
        ax3 = self.manual_income_ax
        ax3.clear()
        m_x = [m.year + (m.month - 1) / 12.0 for m in synth_calc.monthly]
        m_income = [m.after_tax or 0 for m in synth_calc.monthly]
        m_expense = [m.utgifter or 0 for m in synth_calc.monthly]
        if m_x:
            ax3.plot(m_x, m_income, color='#2ca02c', linewidth=1.4,
                     label='Netto-inkomst manuellt förslag')
            ax3.plot(m_x, m_expense, color='#c0392b', linewidth=1.4,
                     linestyle='--', label='Utgifter')
            # Skatte-sänkning-markeringar vid årsskiften där netto-andelen
            # ökar markant (samma logik som i månadsinkomst-fliken)
            prev_ratio = None
            for m in synth_calc.monthly:
                if not m.before_tax or m.after_tax is None or m.before_tax <= 0:
                    continue
                ratio = m.after_tax / m.before_tax
                if (prev_ratio is not None and m.month == 1
                        and ratio - prev_ratio > 0.03):
                    ax3.axvline(x=m.year, color='#5a7', linestyle=':',
                                alpha=0.6, linewidth=1.2)
                    ax3.annotate(
                        f'{m.year}: lägre skatt ({m.age} år)',
                        xy=(m.year, 0), xycoords=('data', 'axes fraction'),
                        xytext=(2, -14), textcoords='offset points',
                        fontsize=7, color='#3a7', ha='left',
                    )
                prev_ratio = ratio
        ax3.set_xlabel('År')
        ax3.set_ylabel('kr / mån')
        ax3.set_title('Månadsinkomst (efter skatt) vs utgifter')
        ax3.grid(True, axis='y', linestyle=':', alpha=0.5)
        xlim_now = getattr(self, '_manual_shared_xlim', None)
        if xlim_now:
            self._apply_manual_year_axis(ax3, xlim_now[0], xlim_now[1])
        ax3.legend(loc='best', fontsize=8)
        ax3.yaxis.set_major_formatter(FuncFormatter(lambda v, _: fmt_kr(v)))
        # Delad x-range + margin så åren alignas med Gantt/kvar
        xlim = getattr(self, '_manual_shared_xlim', None)
        if xlim:
            ax3.set_xlim(*xlim)
        self._apply_manual_fig_margins(
            self.manual_income_fig, top=0.90, bottom=0.17,
        )
        self.manual_income_canvas.draw_idle()

        # Info-rad
        base_end = b_vals[-1] if b_vals else 0
        synth_end = s_vals[-1] if s_vals else 0
        def _sum_def(c):
            return sum(-m.overskott_man for m in c.monthly
                       if m.overskott_man is not None and m.overskott_man < 0)
        base_def = _sum_def(base_calc)
        synth_def = _sum_def(synth_calc)
        self.manual_info_var.set(
            f'Manuellt förslag  ·  Slutvärde: {fmt_kr(synth_end)} kr '
            f'(orig {fmt_kr(base_end)})  ·  '
            f'Totalt underskott: {fmt_kr(synth_def)} kr '
            f'(orig {fmt_kr(base_def)})'
        )

    def _manual_copy_from_suggest(self):
        """Fyll manuellt-fliken med configs från senaste Föreslå-körningen."""
        info = getattr(self, '_suggest_last_result', None)
        if not info:
            self.manual_status.config(
                text='Kör Föreslå plan först.', foreground='#a00',
            )
            return
        # Sätt utgångsplan
        self.manual_base_var.set(info['base'])
        # Vänta lite så _on_manual_base_changed har byggt UI, sen skriv över
        def _apply():
            src_by_name = {c['name']: c for c in info['configs']}
            for mc in self._manual_configs:
                sc = src_by_name.get(mc['name'])
                if sc is None:
                    continue
                start = sc.get('start') or mc['orig_ins'].start or (2027, 1)
                dur = sc.get('dur')
                try:
                    mc['sy_var'].set(start[0])
                    mc['sm_var'].set(start[1])
                    if dur is None:
                        mc['livet_var'].set(True)
                    else:
                        mc['livet_var'].set(False)
                        mc['dy_var'].set(dur[0])
                        mc['dm_var'].set(dur[1])
                except tk.TclError:
                    pass
            self.manual_status.config(
                text='Kopierat från Föreslå.', foreground='#080',
            )
            self._schedule_manual_recalc()
        self.top.after(50, _apply)

    def _manual_create_plan_tab(self):
        """Skapa MAN_FROM_XYZ som ny plan-flik i huvudfönstret."""
        base = self._get_manual_base_tab()
        synth = self._manual_last_synth
        if base is None or synth is None:
            self.manual_status.config(
                text='Välj utgångsplan och ange configs.', foreground='#a00',
            )
            return
        new_name = f'MAN_FROM_{base.parsed.plan_name}'
        existing = {
            t.parsed.plan_name for t in self.app.tabs
            if t.parsed is not None
        }
        final_name = new_name
        n = 2
        while final_name in existing:
            final_name = f'{new_name} #{n}'
            n += 1
        import copy
        synth_copy = copy.deepcopy(synth)
        synth_copy.plan_name = final_name
        tab = self.app.add_tab(select=True)
        try:
            tab.apply_parsed(synth_copy)
        except Exception as e:
            self.manual_status.config(
                text=f'Fel: {e}', foreground='#a00',
            )
            return
        self.manual_status.config(
            text=f'Skapade fliken "{final_name}" i huvudfönstret.',
            foreground='#080',
        )
        self._refresh_manual_bases()


class PensionPlannerApp:
    """Huvudfönster med globala inställningar (Sparande, brytpunkter) och fliknotebook."""

    def __init__(self, root: tk.Tk):
        self.root = root
        # Versionen läses från git (.py) eller fallback (.exe) — används i titel + Om
        self._app_version = get_app_version()
        self.root.title(f"Pension Planner {self._app_version[0]}")
        # Återställ senast använd fönsterstorlek (sätts efter state-läsning nedan)
        self.root.geometry("1800x950")
        self.tabs = []

        # Globala state-variabler
        self.sparande_var = tk.StringVar(value="400000")
        # Tillväxt på kvarvarande sparande (compounderas månadsvis)
        self.growth_enabled_var = tk.BooleanVar(value=False)
        self.growth_pct_var = tk.StringVar(value="0.0")
        # Simuleringsmodell: 'Månadsvis' = diff+ränta per månad,
        # 'Årsvis' = diff vid årets slut (ränta fortfarande månadsvis)
        self.growth_mode_var = tk.StringVar(value='Månadsvis')
        # "Räkna till ålder": simuleringen slutar året personen fyller detta
        self.end_age_var = tk.StringVar(value=str(DEFAULT_END_AGE))
        self.end_age_var.trace_add('write', self._sync_end_age)
        self.age_vars = [tk.StringVar(value=str(age)) for age, _ in DEFAULT_BRACKETS]
        self.cost_sets = [
            [tk.StringVar(value=str(c)) for _, c in DEFAULT_BRACKETS]
            for _ in range(NUM_COST_SETS)
        ]
        self.active_set_var = tk.IntVar(value=0)
        self.bracket_entries = []
        # Visa/dölj Kvar #1/#2/#3 i diagrammet — globalt för alla flikar
        self.show_kvar_vars = [tk.BooleanVar(value=True) for _ in range(3)]

        # Dirty-tracking för osparade ändringar
        self._settings_dirty = False
        self._plans_dirty = False
        self._suppress_dirty = True  # under init undertryckts dirty-flaggor
        # Omräkning: chunked + generation-counter (ny ändring → ny gen → äldre avbryts)
        self._recalc_gen = 0
        # Scroll-synk mellan flikar: minns vilken år/månad som visas överst
        self._prev_tab_frame = None
        self._scroll_yearly = None   # year eller None
        self._current_file = None  # sökväg till senast öppnad/sparad fil
        self._compare_state = {}  # {plans: [namn], show_kvar: [bool, bool, bool]}
        self._compare_window = None  # öppet jämför-fönster (eller None)
        self._compare_window_geometry = None  # senaste geometri "WxH+X+Y"
        self._suggest_window = None  # öppet Föreslå plan-fönster (eller None)
        self._closed_tabs = []  # LIFO-stack med save-entries för Ångra stängning
        self._initial_load_in_progress = False  # skippa chart-render under filladd
        self._minpension_wizard = None  # öppen scraper-wizard (eller None)
        self._insurance_income_window = None  # öppen inkomst-vy (eller None)
        self._insurance_income_window_geometry = None  # senaste geometri
        self._monthly_window = None  # öppet Månadsvis detalj-fönster (eller None)
        self._monthly_window_geometry = None
        self._minpension_wizard_geometry = None  # senaste wizard-geometri
        self._expense_window = None  # öppet utgifts-fönster (eller None)
        self._expense_window_geometry = None  # senaste geometri
        self._expense_categories = None  # utgiftskategorier (list) eller None
        # Härledningsregler för Set 2-5: index-parallell med cost_sets.
        # None = manuell, annars {'mode': 'kr'|'pct', 'value': float}
        # där value är delta kr eller multiplikator (t.ex. 1.02 för +2%).
        self._derive_rules = [None] * NUM_COST_SETS

        # Läs state-filen tidigt så preferenser kan tillämpas i UI-bygget
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                _init_state = json.load(f)
        except Exception:
            _init_state = {}
        # Expert: visa tillämpnings-kontroll i Sparande-rutan?
        self.expert_growth_mode_visible_var = tk.BooleanVar(
            value=bool(_init_state.get('expert_growth_mode_visible', False)),
        )
        # Senast använda mappar för Spara som / Exportera PDF
        self._last_save_dir = _init_state.get('last_save_dir') or None
        self._last_pdf_dir = _init_state.get('last_pdf_dir') or None
        # Återställ huvudfönstrets geometri om sparad
        # _main_position_restored: True om även positionen återställdes
        # (main() låter då inte --display flytta fönstret).
        self._main_position_restored = False
        saved_geom = _init_state.get('main_window_geometry')
        if isinstance(saved_geom, str) and 'x' in saved_geom:
            try:
                if _geometry_on_some_monitor(saved_geom):
                    self.root.geometry(saved_geom)
                    self._main_position_restored = True
                else:
                    # Skärmen finns inte längre: behåll storleken, låt
                    # positionen bli standard (eller --display)
                    self.root.geometry(saved_geom.split('+', 1)[0])
            except tk.TclError:
                pass
        # Återställ inkomst-vyns geometri (tillämpas när vyn öppnas)
        inc_geom = _init_state.get('insurance_income_window_geometry')
        if isinstance(inc_geom, str) and 'x' in inc_geom:
            self._insurance_income_window_geometry = inc_geom
        # Återställ Månadsvis detalj-fönstrets geometri
        mon_geom = _init_state.get('monthly_window_geometry')
        if isinstance(mon_geom, str) and 'x' in mon_geom:
            self._monthly_window_geometry = mon_geom
        # Återställ wizard-fönstrets geometri (tillämpas när wizarden öppnas)
        wiz_geom = _init_state.get('minpension_wizard_geometry')
        if isinstance(wiz_geom, str) and 'x' in wiz_geom:
            self._minpension_wizard_geometry = wiz_geom
        # Återställ utgifts-fönstrets geometri
        exp_geom = _init_state.get('expense_window_geometry')
        if isinstance(exp_geom, str) and 'x' in exp_geom:
            self._expense_window_geometry = exp_geom
        # Utgiftskategorier laddas per dokument (plan-JSON), inte här

        # Registrera validering för numeriska Entry-fält (tillåter tomma eller bara siffror)
        self._digits_only_vcmd = (self.root.register(self._validate_digits), '%P')
        # Validering för procent (siffror + max en decimal, max 99.9)
        self._percent_vcmd = (self.root.register(self._validate_percent), '%P')

        _startup_log('build_menu')
        self._build_menu()
        _startup_log('build_layout')
        self._build_layout()
        _startup_log('setup_traces')
        self._setup_traces()
        _startup_log('try_auto_load')
        if not self._try_auto_load():
            _startup_log('add_tab (no auto-load)')
            # Ingen fil att återöppna (t.ex. första starten): utgå från
            # grundinställningarna i demo-filen så att en plan som hämtas
            # direkt får samma sparande, utgifter m.m. som demo-planerna.
            self._apply_default_settings()
            self.add_tab()  # första planen om ingen fil kunde laddas
        _startup_log('update_set1_lock')
        # Justera Set 1-lock efter att både layout och ev. fil-laddning körts
        self._update_set1_lock_state()
        self._update_set_diff_labels()
        _startup_log('init done')

        # <<Modified>>-events från Text-widgeten kan vara köade och fyra först efter
        # __init__. Vi defererar dirty-rensningen tills alla idle-events processats.
        self.root.after_idle(self._finish_init)

    def _finish_init(self):
        self._suppress_dirty = False
        self._settings_dirty = False
        self._plans_dirty = False
        for tab in self.tabs:
            tab.paste_text.edit_modified(False)
        self._update_window_title()
        self.root.protocol('WM_DELETE_WINDOW', self._on_close)

    def _mark_settings_dirty(self):
        if not self._suppress_dirty:
            self._settings_dirty = True
            self._update_window_title()

    def _update_window_title(self):
        """Uppdatera fönstertiteln med filnamn + dirty-markering (*)."""
        version_str = self._app_version[0]
        dirty = self._settings_dirty or self._plans_dirty
        base = f"Pension Planner {version_str}"
        if self._current_file:
            name = os.path.basename(self._current_file)
            if name.lower().endswith('.json'):
                name = name[:-5]
            title = f"{name}{'*' if dirty else ''} — {base}"
        else:
            title = f"{base}{' *' if dirty else ''}"
        self.root.title(title)

    @staticmethod
    def _validate_digits(new_value: str) -> bool:
        """Tillåt bara tomma eller helt numeriska värden i Entry-fält."""
        return new_value == '' or new_value.isdigit()

    @staticmethod
    def _validate_percent(new_value: str) -> bool:
        """Tillåt tomt, siffror, eller siffror med max en decimal (punkt/komma)."""
        if new_value == '':
            return True
        s = new_value.replace(',', '.')
        if s.count('.') > 1:
            return False
        try:
            float(s)
        except ValueError:
            return False
        # Max en decimal
        if '.' in s and len(s.split('.', 1)[1]) > 1:
            return False
        return True

    def _mark_plans_dirty(self):
        if not self._suppress_dirty:
            self._plans_dirty = True
            self._update_window_title()

    def _build_menu(self):
        menubar = tk.Menu(self.root)
        self.file_menu = tk.Menu(
            menubar, tearoff=False,
            postcommand=self._update_file_menu_state,
        )
        self.file_menu.add_command(label="Ny", command=self._new_document)
        self.file_menu.add_command(label="Stäng", command=self._close_document)
        self.file_menu.add_command(label="Öppna...", command=self.on_load)
        self.file_menu.add_command(label="Spara", command=self.on_save_quick)
        self.file_menu.add_command(label="Spara som...", command=self.on_save_as)
        self.file_menu.add_separator()
        self.file_menu.add_command(label="Stäng plan", command=self._close_current_tab)
        self.file_menu.add_command(
            label="Ångra stängning",
            accelerator='Ctrl+Shift+T',
            command=self._undo_close_tab,
        )
        self._undo_close_menu_index = self.file_menu.index('end')
        self.file_menu.add_separator()
        self.file_menu.add_command(label="Om...", command=self._show_about)
        self.file_menu.add_separator()
        self.file_menu.add_command(label="Avsluta", command=self._on_close)
        menubar.add_cascade(label="Arkiv", menu=self.file_menu)

        self.view_menu = tk.Menu(menubar, tearoff=False)
        self.view_menu.add_command(
            label="Jämför planer...", command=self._open_compare_window,
        )
        self.view_menu.add_command(
            label="Inkomst per försäkring...", command=self._open_income_window,
        )
        self.view_menu.add_command(
            label="Utgifts-detaljer...", command=self._open_expense_window,
        )
        menubar.add_cascade(label="Vy", menu=self.view_menu)

        self.tools_menu = tk.Menu(menubar, tearoff=False)
        self.tools_menu.add_command(
            label="Hämta från minpension.se...",
            command=self._open_minpension_wizard,
        )
        self.tools_menu.add_command(
            label="Klistra in kopierad text från minpension.se...",
            command=self._open_paste_dialog,
        )
        self.tools_menu.add_command(
            label="🎯 Planera uttag (Föreslå/Manuellt)...",
            command=self._open_suggest_window,
        )
        menubar.add_cascade(label="Verktyg", menu=self.tools_menu)

        self.expert_menu = tk.Menu(menubar, tearoff=False)
        self.expert_menu.add_checkbutton(
            label="Tillämpning av tillväxt (årsvis/månadsvis)",
            variable=self.expert_growth_mode_visible_var,
            command=self._on_toggle_growth_mode_visibility,
        )
        menubar.add_cascade(label="Expert", menu=self.expert_menu)

        # Hjälp-meny: en post per H2-sektion i README.md
        self.help_menu = tk.Menu(menubar, tearoff=False)
        try:
            for title in self._parse_readme_h2_sections():
                self.help_menu.add_command(
                    label=title,
                    command=lambda t=title: self._open_help_viewer(t),
                )
            self.help_menu.add_separator()
        except OSError:
            pass
        self.help_menu.add_command(
            label='Hela README...',
            command=lambda: self._open_help_viewer(None),
        )
        menubar.add_cascade(label="Hjälp", menu=self.help_menu)
        self._help_viewer = None
        self._help_viewer_marks = {}

        self.root.config(menu=menubar)
        # Global genväg för Ångra stängning
        self.root.bind_all('<Control-Shift-T>',
                           lambda e: self._undo_close_tab())
        self.root.bind_all('<Control-Shift-t>',
                           lambda e: self._undo_close_tab())
        self._update_undo_close_menu_state()

    def _update_file_menu_state(self):
        tab = self._current_tab()
        can_close = True
        if len(self.tabs) <= 1 and tab is not None:
            text = tab.paste_text.get('1.0', 'end').strip()
            if tab.parsed is None and not text:
                can_close = False
        try:
            self.file_menu.entryconfigure(
                "Stäng plan", state=('normal' if can_close else 'disabled'),
            )
        except tk.TclError:
            pass

    def _current_tab(self):
        selected = self.notebook.select()
        if not selected:
            return None
        for tab in self.tabs:
            if str(tab.frame) == selected:
                return tab
        return None

    def _on_tab_changed(self, event):
        """När användaren byter flik: spara förgående flikens scroll, applicera på ny."""
        new_selected = self.notebook.select()
        # Spara scroll från förgående flik
        if self._prev_tab_frame and self._prev_tab_frame != new_selected:
            prev = next(
                (t for t in self.tabs if str(t.frame) == self._prev_tab_frame),
                None,
            )
            if prev:
                self._save_scroll(prev)
        # Applicera på nya fliken (efter att UI hunnit rendera)
        new = next(
            (t for t in self.tabs if str(t.frame) == new_selected),
            None,
        )
        if new:
            # Rita diagrammet om det är dirty (lazy-rendering)
            new.ensure_chart_rendered()
            self.root.after_idle(lambda t=new: self._apply_scroll(t))
            # Uppdatera Inkomst per försäkring-vyn om den är öppen
            if (self._insurance_income_window is not None
                    and self._insurance_income_window.tab is not new):
                try:
                    self._insurance_income_window.refresh_for_tab(new)
                except tk.TclError:
                    self._insurance_income_window = None
            self._refresh_monthly_window(new)
        self._prev_tab_frame = new_selected
        self._update_active_plan_label()

    def _save_scroll(self, tab):
        """Spara översta synliga år i flikens årstabell."""
        children = tab.result_tree.get_children()
        if children:
            first_frac = tab.result_tree.yview()[0]
            idx = max(0, min(len(children) - 1, int(first_frac * len(children))))
            values = tab.result_tree.item(children[idx], 'values')
            if values:
                try:
                    self._scroll_yearly = int(values[0])  # year är första data-kolumnen
                except (ValueError, TypeError):
                    pass

    def _apply_scroll(self, tab):
        """Scrolla flikens årstabell så samma år hamnar i topp."""
        if self._scroll_yearly is not None:
            y = self._scroll_yearly
            children = tab.result_tree.get_children()
            for i, child_id in enumerate(children):
                values = tab.result_tree.item(child_id, 'values')
                if not values:
                    continue
                try:
                    if int(values[0]) == y:
                        tab.result_tree.yview_moveto(
                            i / len(children) if children else 0,
                        )
                        break
                except (ValueError, TypeError):
                    continue

    def _on_toggle_growth_mode_visibility(self):
        """Expert-meny: visa/dölj 'Tillämpning'-raden och räkna om."""
        visible = self.expert_growth_mode_visible_var.get()
        if visible:
            self._growth_mode_row.pack(pady=(2, 0), fill='x')
        else:
            self._growth_mode_row.pack_forget()
        self._remember_state()
        # När raden döljs/visas kan get_growth_mode() byta värde → räkna om
        self._start_recalc()

    def _open_income_window(self):
        """Öppna 'Inkomst per försäkring'-vyn för aktiv flik. Om vyn redan är
        öppen — uppdatera den till aktiv flik istället för att öppna en ny."""
        tab = self._current_tab()
        if tab is None:
            messagebox.showinfo(
                "Ingen plan vald", "Välj en plan-flik först.",
                parent=self.root,
            )
            return
        if self._insurance_income_window is not None:
            try:
                if self._insurance_income_window.tab is not tab:
                    self._insurance_income_window.refresh_for_tab(tab)
                self._insurance_income_window.top.lift()
                self._insurance_income_window.top.focus_force()
                return
            except tk.TclError:
                self._insurance_income_window = None
        try:
            from insurance_income_window import InsuranceIncomeWindow
        except ImportError as e:
            messagebox.showerror(
                "Modul saknas",
                f"Kunde inte ladda insurance_income_window: {e}",
                parent=self.root,
            )
            return
        self._insurance_income_window = InsuranceIncomeWindow(self, tab)

    def _open_monthly_window(self):
        """Öppna 'Månadsvis detalj' för aktiv flik, eller lyft det befintliga."""
        tab = self._current_tab()
        if tab is None:
            messagebox.showinfo(
                "Ingen plan vald", "Välj en plan-flik först.",
                parent=self.root,
            )
            return
        if self._monthly_window is not None:
            try:
                if self._monthly_window.tab is not tab:
                    self._monthly_window.refresh_for_tab(tab)
                self._monthly_window.top.deiconify()
                self._monthly_window.top.lift()
                self._monthly_window.top.focus_force()
                return
            except tk.TclError:
                self._monthly_window = None
        try:
            from monthly_detail_window import MonthlyDetailWindow
        except ImportError as e:
            messagebox.showerror(
                "Modul saknas",
                f"Kunde inte ladda monthly_detail_window: {e}",
                parent=self.root,
            )
            return
        self._monthly_window = MonthlyDetailWindow(self, tab)

    def _refresh_monthly_window(self, tab):
        """Uppdatera Månadsvis detalj-fönstret om det är öppet och `tab` är
        den aktiva fliken (fönstret visar alltid aktiv plan)."""
        win = getattr(self, '_monthly_window', None)
        if win is None or tab is not self._current_tab():
            return
        try:
            win.refresh_for_tab(tab)
        except tk.TclError:
            self._monthly_window = None

    def _open_expense_window(self):
        """Öppna 'Utgifts-detaljer'-fönstret eller lyft det befintliga."""
        if self._expense_window is not None:
            try:
                self._expense_window.top.lift()
                self._expense_window.top.focus_force()
                return
            except tk.TclError:
                self._expense_window = None
        try:
            from expense_window import ExpenseWindow
        except ImportError as e:
            messagebox.showerror(
                "Modul saknas",
                f"Kunde inte ladda expense_window: {e}",
                parent=self.root,
            )
            return
        self._expense_window = ExpenseWindow(self)

    def _expenses_model_active(self) -> bool:
        """Utgifts-modellen är aktiv om det finns minst en kategori."""
        cats = self._expense_categories
        return isinstance(cats, list) and len(cats) > 0

    def _update_set1_from_expenses(self):
        """Räkna summan per åldersspann (kr/mån) och skriv till Set 1-fälten."""
        if not self._expenses_model_active():
            return
        n = min(len(self.age_vars), len(self.cost_sets[0]))
        totals = [0.0] * n
        for cat in self._expense_categories:
            for item in (cat.get('items') or []):
                amounts = item.get('amounts') or []
                period = item.get('period', 'månad')
                for j in range(n):
                    a = amounts[j] if j < len(amounts) else 0
                    if not a:
                        continue
                    if period == 'år':
                        totals[j] += a / 12.0
                    else:
                        totals[j] += a
        # Skriv utan att markera dokumentet dirty av just detta
        prev = self._suppress_dirty
        self._suppress_dirty = True
        try:
            for j in range(n):
                self.cost_sets[0][j].set(str(int(round(totals[j]))))
        finally:
            self._suppress_dirty = prev

    def _update_set1_lock_state(self):
        """Grå-låser Set 1-fält och radioknappstext när utgifts-modellen är aktiv."""
        locked = self._expenses_model_active()
        state = 'readonly' if locked else 'normal'
        if hasattr(self, 'bracket_entries'):
            for row in self.bracket_entries:
                # row = [age_entry, set0_entry, set1_entry, ...]
                if len(row) >= 2:
                    try:
                        row[1].configure(state=state)
                    except tk.TclError:
                        pass
        if hasattr(self, '_radio_buttons') and self._radio_buttons:
            try:
                self._radio_buttons[0].configure(
                    text='Set 1 🔒' if locked else 'Set 1',
                )
            except tk.TclError:
                pass

    def _on_expenses_changed(self, trigger_recalc: bool = True):
        """Kallas när expense_categories ändrats — sync + lås + recalc."""
        self._update_set1_from_expenses()
        self._apply_derive_rules()  # ripple till Set 2-5 med lagrade regler
        self._update_set1_lock_state()
        self._update_set_diff_labels()
        self._mark_settings_dirty()
        if trigger_recalc:
            self._start_recalc()

    def _open_paste_dialog(self):
        """Fristående dialog för att klistra in kopierad text från
        minpension.se. Ingen flik påverkas förrän användaren trycker Spara —
        då läggs planen i en ny flik (eller ersätter en flik med samma
        plannamn om användaren väljer det)."""
        existing = getattr(self, '_paste_dialog', None)
        if existing is not None:
            try:
                existing.deiconify()
                existing.lift()
                existing.focus_force()
                return
            except tk.TclError:
                self._paste_dialog = None

        dlg = tk.Toplevel(self.root)
        dlg.withdraw()  # visas först när allt är byggt och storleken satt
        dlg.title('Klistra in text från minpension.se')
        dlg.minsize(400, 300)

        frm = ttk.Frame(dlg, padding=8)
        frm.pack(fill='both', expand=True)
        frm.columnconfigure(0, weight=1)
        frm.rowconfigure(1, weight=1)

        ttk.Label(
            frm,
            text=(
                'Logga in på minpension.se, öppna en uttagsplan, markera allt '
                '(Ctrl+A), kopiera (Ctrl+C) och klistra in här (Ctrl+V eller '
                'högerklick). Tryck sedan Spara — planen läggs i en ny flik.'
                '\n\n'
                'Expandera raderna Privat pension, Tjänstepension och Allmän '
                'pension på sidan innan du kopierar, så att beloppen per '
                'försäkring syns. Annars fungerar inte Inkomst per försäkring '
                'som tänkt.'
            ),
            wraplength=680, foreground='#555', justify='left',
        ).grid(row=0, column=0, columnspan=2, sticky='ew', pady=(0, 6))

        text = tk.Text(
            frm, height=18, wrap='word', width=80,
            bd=1, relief='solid', highlightthickness=0,
        )
        text.grid(row=1, column=0, sticky='nsew')
        vsb = ttk.Scrollbar(frm, orient='vertical', command=text.yview)
        text.configure(yscrollcommand=vsb.set)
        vsb.grid(row=1, column=1, sticky='ns')

        info_var = tk.StringVar(value='')
        info_label = ttk.Label(frm, textvariable=info_var, foreground='#555')
        info_label.grid(row=2, column=0, columnspan=2, sticky='w', pady=(6, 0))

        def _parse_current():
            content = text.get('1.0', 'end-1c')
            if not content.strip():
                return content, None
            try:
                parsed = parse_minpension_text(content)
            except Exception:
                return content, None
            if not parsed.insurances and not parsed.breakpoints:
                return content, None
            return content, parsed

        def _refresh_info(_evt=None):
            if not text.edit_modified():
                return
            text.edit_modified(False)
            content, parsed = _parse_current()
            if not content.strip():
                info_var.set('')
                save_btn.state(['disabled'])
            elif parsed is None:
                info_var.set('Texten kunde inte tolkas som en uttagsplan.')
                info_label.configure(foreground='#a00')
                save_btn.state(['disabled'])
            else:
                info_var.set(
                    f'Tolkad plan: {parsed.plan_name or "<utan namn>"}  ·  '
                    f'{len(parsed.insurances)} försäkringar'
                )
                info_label.configure(foreground='#080')
                save_btn.state(['!disabled'])
        text.bind('<<Modified>>', _refresh_info)

        def _select_all(_evt=None):
            text.tag_add('sel', '1.0', 'end-1c')
            text.mark_set('insert', '1.0')
            return 'break'
        text.bind('<Control-a>', _select_all)
        text.bind('<Control-A>', _select_all)
        # Ny inklistring ersätter befintlig text (läggs inte till efter den)
        text.bind('<<Paste>>', lambda e: text.delete('1.0', 'end'), add='+')
        menu = tk.Menu(dlg, tearoff=False)
        menu.add_command(
            label='Klistra in',
            command=lambda: text.event_generate('<<Paste>>'),
        )

        def _show_menu(event):
            text.focus_set()
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()
        text.bind('<Button-3>', _show_menu)

        def _close():
            self._paste_dialog = None
            try:
                dlg.destroy()
            except tk.TclError:
                pass

        def _save():
            content, parsed = _parse_current()
            if parsed is None:
                messagebox.showwarning(
                    'Klistra in',
                    'Texten kunde inte tolkas som en uttagsplan.',
                    parent=dlg,
                )
                return
            if self._save_pasted_plan(content, parsed, parent=dlg):
                _close()

        btn_row = ttk.Frame(frm)
        btn_row.grid(row=3, column=0, columnspan=2, sticky='ew', pady=(8, 0))
        ttk.Button(
            btn_row, text='Rensa',
            command=lambda: (text.delete('1.0', 'end'),
                             text.edit_modified(True)),
        ).pack(side='left')
        ttk.Button(btn_row, text='Stäng', command=_close).pack(
            side='right', padx=(6, 0),
        )
        save_btn = ttk.Button(btn_row, text='Spara', command=_save)
        save_btn.pack(side='right')
        save_btn.state(['disabled'])
        dlg.protocol('WM_DELETE_WINDOW', _close)

        dlg.update_idletasks()
        self.root.update_idletasks()
        px = self.root.winfo_rootx()
        py = self.root.winfo_rooty()
        pw = self.root.winfo_width()
        ph = self.root.winfo_height()
        w, h = 720, 500
        dlg.geometry(f'{w}x{h}+{px + (pw - w) // 2}+{py + (ph - h) // 2}')
        dlg.deiconify()
        dlg.lift()
        text.focus_set()
        self._paste_dialog = dlg

    def _ask_overwrite_or_new(self, name, new_name, parent=None):
        """Modal fråga när plannamnet redan finns i en flik. Returnerar
        True (skriv över), False (lägg in som ny plan) eller None (avbryt)."""
        owner = parent or self.root
        dlg = tk.Toplevel(owner)
        dlg.withdraw()
        dlg.title('Planen finns redan')
        dlg.transient(owner)
        dlg.resizable(False, False)
        result = [None]

        frm = ttk.Frame(dlg, padding=14)
        frm.pack(fill='both', expand=True)
        ttk.Label(
            frm,
            text=(
                f'Planen "{name}" finns redan i en flik.\n\n'
                f'Vill du skriva över planen i den fliken, eller lägga in '
                f'den som ny plan "{new_name}"?'
            ),
            wraplength=420, justify='left',
        ).pack(anchor='w')

        def _choose(value):
            result[0] = value
            dlg.destroy()

        btn_row = ttk.Frame(frm)
        btn_row.pack(fill='x', pady=(14, 0))
        ttk.Button(
            btn_row, text='Avbryt', command=lambda: _choose(None),
        ).pack(side='right')
        ttk.Button(
            btn_row, text='Lägg in som ny plan',
            command=lambda: _choose(False),
        ).pack(side='right', padx=(0, 6))
        overwrite_btn = ttk.Button(
            btn_row, text='Skriv över', command=lambda: _choose(True),
        )
        overwrite_btn.pack(side='right', padx=(0, 6))
        dlg.protocol('WM_DELETE_WINDOW', lambda: _choose(None))
        dlg.bind('<Escape>', lambda e: _choose(None))

        dlg.update_idletasks()
        ox, oy = owner.winfo_rootx(), owner.winfo_rooty()
        ow, oh = owner.winfo_width(), owner.winfo_height()
        w, h = dlg.winfo_reqwidth(), dlg.winfo_reqheight()
        dlg.geometry(f'+{ox + (ow - w) // 2}+{oy + (oh - h) // 2}')
        dlg.deiconify()
        dlg.grab_set()
        overwrite_btn.focus_set()
        dlg.wait_window()
        return result[0]

    def _save_pasted_plan(self, content, parsed, parent=None) -> bool:
        """Lägg en inklistrad plan i en flik. Finns plannamnet redan i en
        annan flik får användaren välja: skriv över den fliken, eller lägg
        planen i en ny flik med löpnummer (' #2', ' #3', ...). Returnerar
        False om användaren avbröt."""
        name = parsed.plan_name
        target = None
        if name:
            match = next(
                (t for t in self.tabs
                 if t.parsed and t.parsed.plan_name == name),
                None,
            )
            if match is not None:
                taken = {
                    t.parsed.plan_name for t in self.tabs if t.parsed
                }
                n = 2
                while f'{name} #{n}' in taken:
                    n += 1
                new_name = f'{name} #{n}'
                answer = self._ask_overwrite_or_new(name, new_name, parent)
                if answer is None:
                    return False
                if answer:
                    target = match
                else:
                    content = _rename_plan_in_text(content, name, new_name)
        if target is None:
            # Återanvänd en tom flik om det finns en, annars ny flik
            def _is_empty(t):
                return t.parsed is None and (
                    t._placeholder_visible
                    or not t.paste_text.get('1.0', 'end-1c').strip()
                )
            cur = self._current_tab()
            if cur is not None and _is_empty(cur):
                target = cur
            else:
                target = next((t for t in self.tabs if _is_empty(t)), None)
            if target is None:
                target = self.add_tab(select=True)
        target._clear_paste_placeholder()
        target.paste_text.delete('1.0', 'end')
        target.paste_text.insert('1.0', content)
        target.on_parse()
        # Innehållet kommer nu från en inklistring, inte från wizarden
        target.fetched_at = None
        self._mark_plans_dirty()
        try:
            self.notebook.select(target.frame)
            self._update_active_plan_label()
        except (AttributeError, tk.TclError):
            pass
        return True

    def _open_minpension_wizard(self):
        """Öppna scraper-wizarden (eller höj befintlig)."""
        if self._minpension_wizard is not None:
            try:
                self._minpension_wizard.top.lift()
                self._minpension_wizard.top.focus_force()
                return
            except tk.TclError:
                self._minpension_wizard = None
        try:
            from minpension_wizard import MinpensionWizard
        except ImportError as e:
            messagebox.showerror(
                "Modul saknas",
                f"Kunde inte ladda minpension_wizard: {e}",
                parent=self.root,
            )
            return
        self._minpension_wizard = MinpensionWizard(self)

    def _show_about(self):
        """Visa Om-dialogen med version, git-sha, byggdatum och körningsläge."""
        version_str, sha, commit_date, runtime_kind = self._app_version
        runtime_label = {
            'exe': 'Fristående exe',
            'py': 'Python-script',
        }.get(runtime_kind, runtime_kind)
        msg = (
            f"Pension Planner {version_str}\n"
            f"Build: {sha}\n"
            f"Datum: {commit_date}\n"
            f"Körs som: {runtime_label}"
        )
        messagebox.showinfo("Om Pension Planner", msg, parent=self.root)

    def _parse_readme_h2_sections(self):
        """Läs README.md och returnera lista av H2-titlar (## Header)."""
        path = os.path.join(_app_dir(), 'README.md')
        titles = []
        with open(path, encoding='utf-8') as f:
            for line in f:
                if line.startswith('## '):
                    titles.append(line[3:].strip())
        return titles

    def _open_help_viewer(self, scroll_to=None):
        """Öppna Hjälp-fönster som visar README.md renderat. Om scroll_to
        anges, scrolla till motsvarande H2-sektion. Om läsaren visar en
        annan fil (t.ex. BERAKNINGAR.md) laddas README om först."""
        top = getattr(self, '_help_viewer', None)
        readme_path = os.path.join(_app_dir(), 'README.md')
        if top is not None:
            try:
                top.lift()
                top.focus_force()
                # Ladda README om läsaren visar annan fil
                current = getattr(self, '_help_viewer_current_file', None)
                if current != readme_path and os.path.exists(readme_path):
                    self._load_md_into_help_viewer(readme_path)
                    top.title('Hjälp — Pension Planner')
                if scroll_to:
                    self._help_viewer_scroll_to(scroll_to)
                return
            except tk.TclError:
                self._help_viewer = None

        top = tk.Toplevel(self.root)
        top.title('Hjälp — Pension Planner')
        top.geometry('900x700')
        top.protocol('WM_DELETE_WINDOW', self._close_help_viewer)
        self._help_viewer = top

        frame = ttk.Frame(top)
        frame.pack(fill='both', expand=True)
        txt = tk.Text(
            frame, wrap='word', font=('TkDefaultFont', 10),
            padx=12, pady=8, spacing1=1, spacing3=2, borderwidth=0,
        )
        txt.pack(side='left', fill='both', expand=True)
        scr = ttk.Scrollbar(frame, orient='vertical', command=txt.yview)
        scr.pack(side='right', fill='y')
        txt.config(yscrollcommand=scr.set)

        path = os.path.join(_app_dir(), 'README.md')
        try:
            with open(path, encoding='utf-8') as f:
                md = f.read()
        except OSError as e:
            txt.insert('1.0', f'Kan inte läsa README.md: {e}')
            txt.config(state='disabled')
            return
        self._help_viewer_marks = {}
        self._help_link_targets = {}
        self._help_viewer_images = []  # behåll PhotoImage-refs
        self._help_viewer_text = txt
        self._help_viewer_current_file = path
        self._render_markdown(txt, md)
        txt.config(state='disabled')
        # Högerklick → kontextmeny med alla H2-sektioner
        txt.bind('<Button-3>', self._show_help_context_menu)

        if scroll_to:
            self._help_viewer_scroll_to(scroll_to)

    def _close_help_viewer(self):
        try:
            self._help_viewer.destroy()
        except (tk.TclError, AttributeError):
            pass
        self._help_viewer = None
        self._help_viewer_marks = {}

    def _show_help_context_menu(self, event):
        """Högerklick i Hjälp-läsaren visar en meny med alla H2-sektioner
        + 'Tillbaka till Hjälp' när man är i annan fil än README."""
        menu = tk.Menu(self._help_viewer, tearoff=False)
        readme_path = os.path.join(_app_dir(), 'README.md')
        current = getattr(self, '_help_viewer_current_file', None)
        in_other_file = current is not None and current != readme_path

        # Överst: "Tillbaka till Hjälp" om vi är i annan fil
        if in_other_file:
            menu.add_command(
                label='← Tillbaka till Hjälp',
                command=self._go_back_to_readme,
            )
            menu.add_separator()

        if self._help_viewer_marks:
            menu.add_command(label='Gå till avsnitt:', state='disabled')
            menu.add_separator()
            for title in self._help_viewer_marks.keys():
                menu.add_command(
                    label=title,
                    command=lambda t=title: self._help_viewer_scroll_to(t),
                )
            menu.add_separator()
        menu.add_command(
            label='Överst',
            command=lambda: self._help_viewer_text.yview_moveto(0.0),
        )
        menu.add_separator()
        menu.add_command(
            label='Stäng läsare',
            command=self._close_help_viewer,
        )
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return 'break'

    def _insert_help_image(self, txt, alt, path):
        """Ladda och rendera en bild i Hjälp-läsaren. Sökväg är relativ till
        projektroten (samma katalog som README.md)."""
        full_path = os.path.join(_app_dir(), path)
        if not os.path.exists(full_path):
            txt.insert('end', f'[Bild saknas: {path}]\n', 'italic')
            return
        try:
            from PIL import Image, ImageTk
        except ImportError:
            txt.insert('end', f'[Bild: {alt or path}]\n', 'italic')
            return
        try:
            img = Image.open(full_path)
            # Skala ner om bilden är bredare än läsarens rimliga max
            max_w = 820
            if img.width > max_w:
                ratio = max_w / img.width
                img = img.resize(
                    (max_w, int(img.height * ratio)), Image.LANCZOS,
                )
            photo = ImageTk.PhotoImage(img)
            self._help_viewer_images.append(photo)  # behåll ref!
            txt.insert('end', '\n')
            txt.image_create('end', image=photo)
            txt.insert('end', '\n')
            if alt:
                txt.insert('end', alt + '\n\n', 'italic')
            else:
                txt.insert('end', '\n')
        except Exception as e:
            txt.insert('end', f'[Kan inte visa bild {path}: {e}]\n', 'italic')

    def _on_help_link_click(self, target):
        """Klick på länk i Hjälp-läsaren. Hanterar:
        - #anchor    → scrollar inom dokumentet
        - http/https → öppnas i standardwebbläsaren
        - filväg.md  → laddas i samma läsare
        - annan fil  → öppnas med systemets standardapp"""
        if target.startswith('#'):
            self._help_viewer_scroll_to(target[1:])
            return
        if target.startswith(('http://', 'https://')):
            import webbrowser
            try:
                webbrowser.open(target)
            except Exception:
                pass
            return
        # Lokal fil relativt projektroten
        full = os.path.join(_app_dir(), target)
        if not os.path.exists(full):
            return
        if full.lower().endswith('.md'):
            self._load_md_into_help_viewer(full)
        else:
            try:
                _open_with_default_app(full)
            except Exception:
                pass

    def _load_md_into_help_viewer(self, md_path):
        """Ladda och rendera en annan markdown-fil i den befintliga
        Hjälp-läsaren."""
        try:
            with open(md_path, encoding='utf-8') as f:
                md = f.read()
        except OSError:
            return
        txt = self._help_viewer_text
        # Spara scroll-position för README innan vi laddar annan fil, så
        # "Tillbaka till Hjälp" kan återställa exakt läge
        readme_path = os.path.join(_app_dir(), 'README.md')
        current = getattr(self, '_help_viewer_current_file', None)
        if current == readme_path and md_path != readme_path:
            try:
                self._help_viewer_back_scroll = txt.yview()[0]
            except tk.TclError:
                self._help_viewer_back_scroll = 0.0
        try:
            self._help_viewer_marks = {}
            self._help_link_targets = {}
            self._help_viewer_images = []
            self._help_viewer_current_file = md_path
            txt.config(state='normal')
            txt.delete('1.0', 'end')
            self._render_markdown(txt, md)
            txt.config(state='disabled')
            fname = os.path.basename(md_path)
            self._help_viewer.title(f'Hjälp — {fname}')
            txt.yview_moveto(0.0)
        except tk.TclError:
            pass

    def _go_back_to_readme(self):
        """Ladda README i läsaren och återställ scroll-position från när
        användaren lämnade README."""
        readme_path = os.path.join(_app_dir(), 'README.md')
        if not os.path.exists(readme_path):
            return
        self._load_md_into_help_viewer(readme_path)
        try:
            self._help_viewer.title('Hjälp — Pension Planner')
            back = getattr(self, '_help_viewer_back_scroll', 0.0)
            self._help_viewer_text.yview_moveto(back)
        except tk.TclError:
            pass

    def _help_viewer_scroll_to(self, title):
        """Scrolla Hjälp-fönstret till angiven H2-sektion (eller anchor-slug)."""
        marks = self._help_viewer_marks
        mark = marks.get(title)
        if mark is None:
            # Prova som slug (t.ex. 'planera-uttag-fönstret')
            # _md_slug normaliserar även en färdig slug (GitHub-länkar har
            # dubbla bindestreck där rubriken har tankstreck)
            slug_title = self._md_slug(title)
            for key, m in marks.items():
                if self._md_slug(key) == slug_title:
                    mark = m
                    break
        if mark is None:
            return
        try:
            txt = self._help_viewer_text
            # yview(index) lägger rubrikens rad exakt överst. (En fraction
            # baserad på radnummer hamnar fel: rader är olika höga p.g.a.
            # bilder och radbrytning.)
            txt.update_idletasks()
            txt.yview(f'{mark} linestart')
        except tk.TclError:
            pass

    def _md_slug(self, title):
        """README-style anchor-slug: lowercase, spaces → hyphens, kollaps."""
        import re
        s = title.lower().strip()
        s = re.sub(r'[^\w\s-]', '', s, flags=re.UNICODE)
        s = re.sub(r'\s+', '-', s)
        s = re.sub(r'-+', '-', s)  # kollaps dubbla dashes (t.ex. från em-dash)
        return s

    def _render_markdown(self, txt, md):
        """Enkel markdown → tkinter Text med tags. Stöder headers (# ## ###),
        fetstil (**), inline-kod (`), listor (- och 1.), kodblock (```),
        och länkar [text](url) (visas som klickbar text)."""
        import re
        f = 'TkDefaultFont'
        txt.tag_configure('h1', font=(f, 16, 'bold'),
                          spacing1=8, spacing3=6)
        txt.tag_configure('h2', font=(f, 13, 'bold'),
                          spacing1=14, spacing3=4, foreground='#2c3e50')
        txt.tag_configure('h3', font=(f, 11, 'bold'),
                          spacing1=8, spacing3=2, foreground='#34495e')
        txt.tag_configure('bold', font=(f, 10, 'bold'))
        txt.tag_configure('italic', font=(f, 10, 'italic'))
        txt.tag_configure('code', font=('Courier', 9),
                          background='#f4f4f4')
        txt.tag_configure('codeblock', font=('Courier', 9),
                          background='#f4f4f4', lmargin1=20, lmargin2=20,
                          spacing1=2, spacing3=2)
        txt.tag_configure('bullet', lmargin1=20, lmargin2=40)
        txt.tag_configure('link', foreground='#0645ad', underline=True)
        txt.tag_configure('hr', font=(f, 6), foreground='#ccc',
                          spacing1=4, spacing3=8)

        # Tag för tabellrader — monospace så kolumner alignas
        txt.tag_configure('table', font=('Courier', 9),
                          background='#f8f8f8',
                          lmargin1=12, lmargin2=12)
        txt.tag_configure('table_header', font=('Courier', 9, 'bold'),
                          background='#e8e8e8',
                          lmargin1=12, lmargin2=12)

        # Underpunkter (indragna listrader) och citatrutor ("> ...")
        txt.tag_configure('bullet2', lmargin1=44, lmargin2=64)
        txt.tag_configure('quote', lmargin1=24, lmargin2=24, rmargin=24,
                          foreground='#3a4a5a')
        txt.tag_configure('quote_bullet', lmargin1=36, lmargin2=56,
                          rmargin=24, foreground='#3a4a5a')

        item_re = re.compile(r'^(\s*)(-|\d+\.) +(.*)$')
        image_re = re.compile(r'^!\[([^\]]*)\]\(([^)]+)\)\s*$')
        header_re = re.compile(r'^#{1,3} ')

        def starts_block(l):
            """True om raden inleder något eget (eller är tom) och därför
            inte kan vara en fortsättning på föregående stycke/punkt."""
            st = l.strip()
            if not st or st == '---':
                return True
            if st.startswith(('```', '>', '|')):
                return True
            return bool(header_re.match(l) or item_re.match(l)
                        or image_re.match(l))

        def gather(lines, i, first):
            """Slå ihop first med efterföljande fortsättningsrader till ett
            stycke, som i vanlig markdown. Källfilens radbrytningar inom
            ett stycke försvinner, så texten bryts efter fönstrets bredd.
            Returnerar (text, index för nästa obehandlade rad)."""
            parts = [first.strip()]
            while i < len(lines) and not starts_block(lines[i]):
                parts.append(lines[i].strip())
                i += 1
            return ' '.join(parts), i

        def render(lines, quote=False):
            para_base = ('quote',) if quote else ()
            in_code = False
            i = 0
            while i < len(lines):
                line = lines[i]
                if line.startswith('```'):
                    in_code = not in_code
                    i += 1
                    continue
                if in_code:
                    txt.insert('end', line + '\n', 'codeblock')
                    i += 1
                    continue
                if not line.strip():
                    txt.insert('end', '\n', para_base)
                    i += 1
                    continue
                # Bild som eget block: ![alt](path)
                img_m = image_re.match(line)
                if img_m:
                    self._insert_help_image(txt, img_m.group(1), img_m.group(2))
                    i += 1
                    continue
                # Tabell: rad börjar med '|' och nästa rad är separator (|---|)
                if (line.lstrip().startswith('|') and i + 1 < len(lines)
                        and re.match(r'^\s*\|[\s\-:|]+\|\s*$', lines[i + 1])):
                    # Samla alla efterföljande tabellrader
                    table_lines = []
                    while i < len(lines) and lines[i].lstrip().startswith('|'):
                        table_lines.append(lines[i])
                        i += 1
                    self._render_md_table(txt, table_lines)
                    continue
                # Citatruta: samla raderna, ta bort "> " och rendera innehållet
                if line.lstrip().startswith('>'):
                    inner = []
                    while i < len(lines) and lines[i].lstrip().startswith('>'):
                        inner.append(re.sub(r'^\s*> ?', '', lines[i]))
                        i += 1
                    render(inner, quote=True)
                    continue
                if line.startswith('# '):
                    txt.insert('end', line[2:] + '\n', 'h1')
                elif line.startswith('## '):
                    title = line[3:].strip()
                    mark_name = f'sec_{len(self._help_viewer_marks)}'
                    self._help_viewer_marks[title] = mark_name
                    txt.mark_set(mark_name, 'end-1c')
                    txt.mark_gravity(mark_name, 'left')
                    txt.insert('end', title + '\n', 'h2')
                elif line.startswith('### '):
                    txt.insert('end', line[4:] + '\n', 'h3')
                elif line.strip() == '---':
                    txt.insert('end', '─' * 60 + '\n', 'hr')
                else:
                    m = item_re.match(line)
                    if m:
                        indent, marker, first = m.groups()
                        text, i = gather(lines, i + 1, first)
                        if quote:
                            tag = 'quote_bullet'
                        elif len(indent) >= 2:
                            tag = 'bullet2'
                        else:
                            tag = 'bullet'
                        prefix = '  • ' if marker == '-' else f'  {marker} '
                        self._render_md_inline(
                            txt, text + '\n', base=(tag,), prefix=prefix,
                        )
                    else:
                        text, i = gather(lines, i + 1, line)
                        self._render_md_inline(
                            txt, text + '\n', base=para_base,
                        )
                    continue
                i += 1

        render(md.split('\n'))

    def _render_md_table(self, txt, table_lines):
        """Rendera en GFM-tabell med monospace + kolumn-padding så det
        ser tabell-aktigt ut i tk.Text."""
        import re
        rows = []
        for tline in table_lines:
            t = tline.strip()
            if t.startswith('|'):
                t = t[1:]
            if t.endswith('|'):
                t = t[:-1]
            cells = [c.strip() for c in t.split('|')]
            # Hoppa över separator-rad (bara --- eller :--:)
            if all(re.match(r'^:?-{2,}:?$', c) for c in cells if c):
                continue
            rows.append(cells)
        if not rows:
            return
        ncols = max(len(r) for r in rows)
        widths = [0] * ncols
        for r in rows:
            for i, c in enumerate(r):
                if i < ncols:
                    widths[i] = max(widths[i], len(c))
        for row_idx, r in enumerate(rows):
            parts = []
            for i in range(ncols):
                cell = r[i] if i < len(r) else ''
                parts.append(cell.ljust(widths[i]))
            line = ' │ '.join(parts) + '\n'
            tag = 'table_header' if row_idx == 0 else 'table'
            txt.insert('end', line, tag)

    def _render_md_inline(self, txt, line, base=(), prefix=''):
        """Rendera en rad med inline-formatering (**bold**, `code`,
        [text](url))."""
        import re
        if prefix:
            txt.insert('end', prefix, base)
        pos = 0
        while pos < len(line):
            rest = line[pos:]
            # Fet länk: **[text](url)** — rendera länken, i fetstil
            m = re.match(r'\*\*(\[[^\]]+\]\([^)]+\))\*\*', rest)
            if m:
                self._render_md_inline(
                    txt, m.group(1), base=tuple(base) + ('bold',),
                )
                pos += m.end()
                continue
            m = re.match(r'\*\*([^*]+)\*\*', rest)
            if m:
                txt.insert('end', m.group(1), tuple(base) + ('bold',))
                pos += m.end()
                continue
            m = re.match(r'`([^`]+)`', rest)
            if m:
                txt.insert('end', m.group(1), tuple(base) + ('code',))
                pos += m.end()
                continue
            m = re.match(r'\*([^*\n]+)\*', rest)
            if m and not rest.startswith('**'):
                txt.insert('end', m.group(1), tuple(base) + ('italic',))
                pos += m.end()
                continue
            m = re.match(r'\[([^\]]+)\]\(([^)]+)\)', rest)
            if m:
                link_text = m.group(1)
                target = m.group(2)
                # Unik tag per länk så vi kan bind:a klick till rätt target
                link_tag = f'link_{id(self)}_{len(self._help_link_targets)}'
                start_idx = txt.index('end-1c')
                txt.insert('end', link_text, tuple(base) + ('link', link_tag))
                self._help_link_targets[link_tag] = target
                # Aktivera klick + hover-cursor på taggen
                txt.tag_bind(
                    link_tag, '<Button-1>',
                    lambda e, t=target: self._on_help_link_click(t),
                )
                txt.tag_bind(
                    link_tag, '<Enter>',
                    lambda e, w=txt: w.config(cursor='hand2'),
                )
                txt.tag_bind(
                    link_tag, '<Leave>',
                    lambda e, w=txt: w.config(cursor=''),
                )
                pos += m.end()
                continue
            txt.insert('end', rest[0], tuple(base))
            pos += 1

    def _open_suggest_window(self):
        """Öppna Föreslå plan-fönstret (eller lyft det om redan öppet)."""
        if self._suggest_window is not None:
            try:
                self._suggest_window.top.lift()
                self._suggest_window.top.focus_force()
                return
            except tk.TclError:
                self._suggest_window = None
        # Kräver minst en hämtad plan att utgå ifrån
        has_plan = any(
            t.parsed is not None and t.parsed.plan_name
            for t in self.tabs
        )
        if not has_plan:
            messagebox.showinfo(
                'Planera uttag',
                'Du behöver hämta minst en plan från minpension.se först '
                'som utgångspunkt.\n\n'
                'Använd Verktyg → Hämta från minpension.se... eller '
                'knappen "Hämta plan" till höger.',
                parent=self.root,
            )
            return
        self._suggest_window = SuggestPlanWindow(self)

    def _open_compare_window(self):
        """Öppna jämför-fönstret (eller höj det om redan öppet)."""
        if self._compare_window is not None:
            try:
                self._compare_window.top.lift()
                self._compare_window.top.focus_force()
                return
            except tk.TclError:
                self._compare_window = None
        self._compare_window = CompareWindow(self)

    def _refresh_compare_window(self):
        """Anropas när planer/data ändrats så jämför-fönstret hänger med."""
        if self._compare_window is not None:
            try:
                self._compare_window.refresh_from_data()
            except tk.TclError:
                self._compare_window = None

    def _update_chart_visibility(self, *_):
        """Synka Kvar-linjernas synlighet över alla flikar enligt checkbox-state."""
        states = [v.get() for v in self.show_kvar_vars]
        for tab in self.tabs:
            lines = getattr(tab, '_kvar_lines', None)
            if not lines:
                # Uppdatera ändå min-etiketten även om grafen inte är ritad än
                if hasattr(tab, 'chart_min_frame'):
                    tab._update_chart_min_label()
                continue
            for i, line in enumerate(lines):
                if i < len(states):
                    line.set_visible(states[i])
            # Bygg om legenden så bara synliga linjer listas
            visible = [ln for ln in lines if ln.get_visible()]
            if visible:
                tab.ax.legend(
                    visible, [ln.get_label() for ln in visible],
                    loc=tab._best_legend_loc(),
                )
            else:
                leg = tab.ax.get_legend()
                if leg is not None:
                    leg.remove()
            tab.canvas.draw_idle()
            tab._update_chart_min_label()

    def _update_active_plan_label(self):
        """Uppdatera 'Aktiv plan'-rutan med nuvarande fliks plan-namn + knapp-state."""
        if not hasattr(self, '_active_plan_label'):
            return
        tab = self._current_tab()
        name = ''
        if tab and tab.parsed and tab.parsed.plan_name:
            name = tab.parsed.plan_name
        self._active_plan_label.config(text=name)
        state = 'normal' if len(self.tabs) > 1 else 'disabled'
        self._prev_plan_btn.config(state=state)
        self._next_plan_btn.config(state=state)
        # Hämtningsdatum för aktiv flik (om planen kom från wizarden)
        if hasattr(self, '_fetched_at_label'):
            fetched = tab.fetched_at if tab else None
            self._fetched_at_label.config(
                text=f'Hämtad: {fetched}' if fetched else '',
            )
        # Inkomst per försäkring-knapp — aktiv bara om data är inläst
        if hasattr(self, '_insurance_data_btn'):
            has_data = False
            if tab and tab.parsed and tab.parsed.insurances:
                has_data = any(
                    ins.amounts_before_tax for ins in tab.parsed.insurances
                )
            self._insurance_data_btn.config(
                state='normal' if has_data else 'disabled',
            )

    def _prev_plan(self):
        if len(self.tabs) <= 1:
            return
        idx = self._current_tab_index()
        new_idx = (idx - 1) % len(self.tabs)
        self.notebook.select(self.tabs[new_idx].frame)

    def _next_plan(self):
        if len(self.tabs) <= 1:
            return
        idx = self._current_tab_index()
        new_idx = (idx + 1) % len(self.tabs)
        self.notebook.select(self.tabs[new_idx].frame)

    def _close_current_tab(self):
        tab = self._current_tab()
        if tab is not None:
            self.close_tab(tab)

    def _new_document(self):
        """Skapa ett nytt tomt dokument. Frågar om osparade ändringar först."""
        if self._settings_dirty or self._plans_dirty:
            if not self._confirm_unsaved_before_load(
                discard_label='Stäng utan att spara'
            ):
                return
        self.root.config(cursor='watch')
        try:
            self._clear_all_tabs()
            self.root.update_idletasks()
            self.add_tab()
            self._current_file = None
            self._compare_state = {}
            self._expense_categories = None
            self._update_set1_lock_state()
            if self._expense_window is not None:
                try:
                    self._expense_window.reload_from_app()
                except tk.TclError:
                    self._expense_window = None
            self._settings_dirty = False
            self._plans_dirty = False
            self._update_window_title()
            self.root.after_idle(self._post_load_cleanup)
        finally:
            self.root.config(cursor='')

    def _close_document(self):
        """Stäng aktuellt dokument — funktionellt samma som Nytt dokument."""
        self._new_document()

    def _build_layout(self):
        # Topprad: globala inställningar
        top = ttk.Frame(self.root, padding=(8, 8, 8, 4))
        top.pack(fill='x')
        self._build_global_controls(top)

        # Style: aktiv plan-flik får fet text så den står ut tydligare
        style = ttk.Style()
        style.configure('TNotebook.Tab', font=('TkDefaultFont', 9))
        style.map('TNotebook.Tab', font=[('selected', ('TkDefaultFont', 9, 'bold'))])

        # Resten: fliknotebook
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill='both', expand=True)
        self.notebook.bind('<Button-3>', self._on_notebook_right_click)
        self.notebook.bind('<<NotebookTabChanged>>', self._on_tab_changed)
        # Hover-tooltip på flik med hämtningsdatum
        self._tab_tooltip_win = None
        self._tab_tooltip_index = None
        self.notebook.bind('<Motion>', self._on_notebook_hover, add='+')
        self.notebook.bind('<Leave>', self._hide_tab_tooltip, add='+')
        # Drag-och-släpp för att ordna om flikar
        self._drag_tab_index = None
        self.notebook.bind('<ButtonPress-1>', self._on_tab_press)
        self.notebook.bind('<B1-Motion>', self._on_tab_drag)
        self.notebook.bind('<ButtonRelease-1>', self._on_tab_release)

        # Visas i stället för flikarna när ingen plan är inläst (den tomma
        # fliken finns kvar internt men syns inte)
        self._no_plan_frame = ttk.Frame(self.root)
        ttk.Label(
            self._no_plan_frame,
            text=NO_PLAN_TEXT, justify='center',
            font=('TkDefaultFont', 11), foreground='#555',
        ).place(relx=0.5, rely=0.4, anchor='center')
        self._no_plan_shown = False

        # Inga input-event-bindningar behövs längre för debounce-timer — chunked recalc
        # bygger in responsiveness via after() mellan flikarna.

    def _update_no_plan_view(self):
        """Visa flikarna om minst en plan är inläst, annars en förklarande
        text i samma yta."""
        frame = getattr(self, '_no_plan_frame', None)
        if frame is None:
            return
        show_text = not any(t.parsed is not None for t in self.tabs)
        if show_text == self._no_plan_shown:
            return
        self._no_plan_shown = show_text
        try:
            if show_text:
                self.notebook.pack_forget()
                frame.pack(fill='both', expand=True)
            else:
                frame.pack_forget()
                self.notebook.pack(fill='both', expand=True)
        except tk.TclError:
            pass

    def _build_global_controls(self, parent):
        # Kolumnbeskrivning längst till vänster
        desc_frame = ttk.LabelFrame(parent, text="Kolumnbeskrivning", padding=8)
        desc_frame.pack(side='left', anchor='n', padx=(0, 12), fill='y')
        for i, (term, desc) in enumerate(COLUMN_DESCRIPTIONS):
            ttk.Label(
                desc_frame, text=term,
                font=('TkDefaultFont', 9, 'bold'),
            ).grid(row=i, column=0, sticky='w', padx=(0, 8))
            ttk.Label(
                desc_frame, text=desc,
                font=('TkDefaultFont', 9),
            ).grid(row=i, column=1, sticky='w')

        # Sparande i egen grupp
        sp_frame = ttk.LabelFrame(parent, text="Sparande", padding=8)
        sp_frame.pack(side='left', anchor='n', padx=(0, 12), fill='y')
        sp_row = ttk.Frame(sp_frame)
        sp_row.pack(pady=4)
        sp_entry = ttk.Entry(
            sp_row, textvariable=self.sparande_var, width=15,
            validate='key', validatecommand=self._digits_only_vcmd,
        )
        sp_entry.pack(side='left')
        ttk.Label(sp_row, text="kr").pack(side='left', padx=(4, 0))
        self._bind_entry_recalc(sp_entry)

        # Tillväxt: checkbox + procent-input (compounderas månadsvis)
        growth_row = ttk.Frame(sp_frame)
        growth_row.pack(pady=(2, 0), fill='x')
        growth_cb = ttk.Checkbutton(
            growth_row, text="Tillväxt",
            variable=self.growth_enabled_var,
            command=self._on_global_change,
        )
        growth_cb.pack(side='left')
        pct_entry = ttk.Entry(
            growth_row, textvariable=self.growth_pct_var, width=5,
            validate='key', validatecommand=self._percent_vcmd,
        )
        pct_entry.pack(side='left', padx=(4, 0))
        ttk.Label(growth_row, text="%/år").pack(side='left', padx=(2, 0))
        self._bind_entry_recalc(pct_entry)

        # Räkna till ålder: sista året i simuleringen = året man fyller detta
        end_age_row = ttk.Frame(sp_frame)
        end_age_row.pack(pady=(6, 0), fill='x')
        ttk.Label(end_age_row, text="Räkna till ålder:").pack(side='left')
        end_age_sb = ttk.Spinbox(
            end_age_row, textvariable=self.end_age_var, width=4,
            from_=MIN_END_AGE, to=MAX_END_AGE, increment=1,
            validate='key', validatecommand=self._digits_only_vcmd,
            command=self._on_global_change,
        )
        end_age_sb.pack(side='left', padx=(4, 0))
        ttk.Label(end_age_row, text="år").pack(side='left', padx=(2, 0))
        self._bind_entry_recalc(end_age_sb)
        _Tooltip(
            end_age_sb,
            lambda: (
                f"Simuleringen räknar till och med året du fyller denna "
                f"ålder ({MIN_END_AGE}–{MAX_END_AGE}, normalt {DEFAULT_END_AGE})."
            ),
        )

        # Simuleringsmodell: hur diff tillämpas relativt räntan.
        # Synlig endast om Expert → "Tillämpning av tillväxt" är ikryssad.
        self._growth_mode_row = ttk.Frame(sp_frame)
        ttk.Label(self._growth_mode_row, text="Tillämpning:").pack(side='left')
        mode_combo = ttk.Combobox(
            self._growth_mode_row, textvariable=self.growth_mode_var,
            values=['Månadsvis', 'Årsvis'],
            state='readonly', width=10,
        )
        mode_combo.pack(side='left', padx=(4, 0))
        mode_combo.bind('<<ComboboxSelected>>', lambda e: self._on_global_change())
        # Visa rad nu om expert-läget är på
        if self.expert_growth_mode_visible_var.get():
            self._growth_mode_row.pack(pady=(2, 0), fill='x')

        # Kostnadsbrytpunkter i egen grupp
        br_frame = ttk.LabelFrame(parent, text="Utgiftsbrytpunkter (välj aktivt set)", padding=8)
        br_frame.pack(side='left', anchor='n')

        # Contextmeny för kostnadsfält — justera värdet med fasta delta
        self._cost_context_target = None
        self._cost_context_menu = tk.Menu(self.root, tearoff=False)
        for delta in (-2000, -1500, -1000, -500, -250, 250, 500, 1000, 1500, 2000):
            label = f"{delta:+,d}".replace(',', '\xa0')
            self._cost_context_menu.add_command(
                label=label,
                command=lambda d=delta: self._apply_cost_delta(d),
            )

        ttk.Label(br_frame, text="T.o.m.\nålder", justify='center').grid(row=0, column=0, padx=2)
        self._radio_buttons = []
        for s in range(NUM_COST_SETS):
            cell = ttk.Frame(br_frame)
            cell.grid(row=0, column=1 + s, padx=1)
            rb = ttk.Radiobutton(
                cell, text=f"Set {s+1}",
                variable=self.active_set_var, value=s,
            )
            rb.pack(side='left')
            rb.bind('<Left>', lambda e: self._set_nav(-1))
            rb.bind('<Right>', lambda e: self._set_nav(1))
            rb.bind('<Up>', lambda e: 'break')    # konsumera Up (radio är översta raden)
            rb.bind('<Down>', lambda e, s=s: self._radio_down(s))
            # Härled-knapp för Set 2–5 (Set 1 är källa och behöver ingen)
            if s > 0:
                btn = ttk.Button(
                    cell, text='▾', width=2,
                    command=lambda ts=s: self._open_derive_dialog(ts),
                )
                btn.pack(side='left', padx=(2, 0))
            self._radio_buttons.append(rb)

        for i in range(len(DEFAULT_BRACKETS)):
            row_entries = []
            age_entry = ttk.Entry(
                br_frame, textvariable=self.age_vars[i], width=5,
                validate='key', validatecommand=self._digits_only_vcmd,
            )
            age_entry.grid(row=i + 1, column=0, padx=2, pady=1)
            # Tooltip som visar åldersintervallet för raden
            _Tooltip(age_entry, lambda i=i: self._age_range_text(i))
            row_entries.append(age_entry)
            for s in range(NUM_COST_SETS):
                cost_entry = ttk.Entry(
                    br_frame, textvariable=self.cost_sets[s][i], width=7,
                    validate='key', validatecommand=self._digits_only_vcmd,
                )
                cost_entry.grid(row=i + 1, column=1 + s, padx=1, pady=1)
                cost_entry.bind('<Button-3>', self._show_cost_context)
                row_entries.append(cost_entry)
            self.bracket_entries.append(row_entries)

        # Pilnavigering + recalc-bindning som bara triggar vid faktisk värdeändring
        for r, row in enumerate(self.bracket_entries):
            for c, entry in enumerate(row):
                entry.bind('<Left>', lambda e, r=r, c=c: self._nav_left(e, r, c))
                entry.bind('<Right>', lambda e, r=r, c=c: self._nav_right(e, r, c))
                entry.bind('<Up>', lambda e, r=r, c=c: self._nav_up(e, r, c))
                entry.bind('<Down>', lambda e, r=r, c=c: self._nav_down(e, r, c))
                self._bind_entry_recalc(entry)

        # Diff-etikett under varje Set 2-5-kolumn: visar hur setten förhåller
        # sig till Set 1 (t.ex. "+500/rad", "×110 %" eller "Manuell")
        self._set_diff_labels = [None] * NUM_COST_SETS
        for s in range(1, NUM_COST_SETS):
            lbl = ttk.Label(
                br_frame, text='', font=('TkDefaultFont', 8),
                foreground='#666', anchor='center', justify='center',
            )
            lbl.grid(
                row=len(DEFAULT_BRACKETS) + 1, column=1 + s,
                padx=1, pady=(2, 0), sticky='ew',
            )
            self._set_diff_labels[s] = lbl

        # Genväg till Utgifts-detaljer (Vy-menyns motsvarighet)
        details_row = ttk.Frame(br_frame)
        details_row.grid(
            row=len(DEFAULT_BRACKETS) + 2, column=0,
            columnspan=1 + NUM_COST_SETS, sticky='w', pady=(6, 0),
        )
        ttk.Button(
            details_row, text='Utgifts-detaljer...',
            command=self._open_expense_window,
        ).pack(side='left')

        # Visa/dölj Kvar-grafer
        viz_frame = ttk.LabelFrame(parent, text="Visa i diagram", padding=8)
        viz_frame.pack(side='left', anchor='n', padx=(12, 0), fill='y')
        for i in range(3):
            cb = ttk.Checkbutton(
                viz_frame, text=KVAR_LABELS[i],
                variable=self.show_kvar_vars[i],
                command=self._update_chart_visibility,
            )
            cb.pack(anchor='w', pady=1)

        # Aktiv plan-info med navigeringspilar (visas till höger om kostnadsbrytpunkter)
        plan_info_frame = ttk.LabelFrame(parent, text="Aktiv plan", padding=8)
        plan_info_frame.pack(side='left', anchor='n', padx=(12, 0), fill='y')
        nav_row = ttk.Frame(plan_info_frame)
        nav_row.pack(pady=4, padx=4)
        self._prev_plan_btn = ttk.Button(
            nav_row, text='◀', width=3, command=self._prev_plan,
        )
        self._prev_plan_btn.pack(side='left')
        self._active_plan_label = ttk.Label(
            nav_row, text='',
            font=('TkDefaultFont', 12, 'bold'),
        )
        self._active_plan_label.pack(side='left', padx=8)
        self._next_plan_btn = ttk.Button(
            nav_row, text='▶', width=3, command=self._next_plan,
        )
        self._next_plan_btn.pack(side='left')
        # "Hämtad: YYYY-MM-DD HH:MM" direkt under plan-namnet (tom om manuellt)
        self._fetched_at_label = ttk.Label(
            plan_info_frame, text='',
            font=('TkDefaultFont', 9), foreground='#666',
        )
        self._fetched_at_label.pack(anchor='w', padx=4, pady=(2, 0))
        self._insurance_data_btn = ttk.Button(
            plan_info_frame, text='Inkomst per försäkring...',
            command=self._open_income_window,
        )
        self._insurance_data_btn.pack(anchor='w', padx=4, pady=(4, 0))
        ttk.Button(
            plan_info_frame, text='Månadsvis detalj...',
            command=self._open_monthly_window,
        ).pack(anchor='w', padx=4, pady=(2, 0))
        ttk.Button(
            plan_info_frame, text='Jämför planer...',
            command=self._open_compare_window,
        ).pack(anchor='w', padx=4, pady=(2, 0))

        # Genvägsknapp till minpension.se-wizarden
        minp_frame = ttk.LabelFrame(parent, text="minpension.se", padding=8)
        minp_frame.pack(side='left', anchor='n', padx=(12, 0), fill='y')
        ttk.Button(
            minp_frame, text='Hämta plan',
            command=self._open_minpension_wizard,
        ).pack(pady=4, padx=4)
        ttk.Button(
            minp_frame, text='Planera uttag',
            command=self._open_suggest_window,
        ).pack(pady=(0, 4), padx=4)

    def _setup_traces(self):
        # Radio-knappen byter set i ett klick → recalc direkt
        self.active_set_var.trace_add('write', self._on_global_change)
        # Textfält: trace markerar bara dirty per tangenttryck — recalc triggas via FocusOut
        self.sparande_var.trace_add('write', self._on_var_dirty)
        self.growth_enabled_var.trace_add('write', self._on_var_dirty)
        self.growth_pct_var.trace_add('write', self._on_var_dirty)
        self.growth_mode_var.trace_add('write', self._on_var_dirty)
        self.end_age_var.trace_add('write', self._on_var_dirty)
        for age_var in self.age_vars:
            age_var.trace_add('write', self._on_var_dirty)
        for cost_set in self.cost_sets:
            for cost_var in cost_set:
                cost_var.trace_add('write', self._on_var_dirty)

    def _on_var_dirty(self, *_):
        """Trace-handler för textfält — bara dirty, ingen recalc."""
        self._mark_settings_dirty()

    # ---- Recalc-binding för Entry: bara vid faktisk värdeändring ----

    def _bind_entry_recalc(self, entry):
        """Lägg till FocusIn/FocusOut/Return så recalc bara körs om värdet ändrats."""
        entry.bind('<FocusIn>', self._on_entry_focus_in)
        entry.bind('<FocusOut>', self._on_entry_focus_out)
        entry.bind('<Return>', self._on_entry_return)

    def _on_entry_focus_in(self, event):
        event.widget._initial_value = event.widget.get()

    def _on_entry_focus_out(self, event):
        initial = getattr(event.widget, '_initial_value', None)
        if initial is not None and initial != event.widget.get():
            self._on_global_change(event)
            event.widget._initial_value = event.widget.get()

    def _on_entry_return(self, event):
        self._on_entry_focus_out(event)

    def _on_global_change(self, *_):
        # Visa det värde som faktiskt används om fältet är tomt/utanför intervallet
        if self.end_age_var.get().strip() != str(self.get_end_age()):
            self.end_age_var.set(str(self.get_end_age()))
        self._mark_settings_dirty()
        # Kolla om användaren har manuellt redigerat något Set med lagrad regel
        # → rensa den regeln (så vi inte skriver över manuell ändring senare)
        self._invalidate_stale_derive_rules()
        self._update_set_diff_labels()
        self._start_recalc()

    def _start_recalc(self):
        """Schemalägg chunked recalc med ny gen (avbryter eventuell pågående)."""
        if not any(tab.parsed for tab in self.tabs):
            return
        self._recalc_gen += 1
        my_gen = self._recalc_gen
        self.root.config(cursor='watch')
        self.root.after(0, lambda: self._recalc_step(my_gen, 0))

    def _recalc_step(self, gen: int, idx: int):
        """Beräkna en flik åt gången. Avbryter om en nyare gen påbörjats."""
        if gen != self._recalc_gen:
            return  # avbruten av en senare ändring
        if idx >= len(self.tabs):
            self.root.config(cursor='')
            self._refresh_compare_window()
            # Uppdatera Planera manuellt-grafen så utgifts-set-ändringar syns
            if self._suggest_window is not None:
                try:
                    self._suggest_window._schedule_manual_recalc()
                except tk.TclError:
                    self._suggest_window = None
            # Uppdatera Inkomst per försäkring-fönstret så inkomst/utgifter-
            # grafen speglar det nya utgifts-setet
            if self._insurance_income_window is not None:
                try:
                    win = self._insurance_income_window
                    win.refresh_for_tab(win.tab)
                except tk.TclError:
                    self._insurance_income_window = None
            return
        tab = self.tabs[idx]
        if tab.parsed:
            tab.recalculate()
        # Yielda till event-loopen så input/klick processas mellan flikarna
        self.root.after(1, lambda: self._recalc_step(gen, idx + 1))

    # ---- Pilnavigering mellan bracket-fält ----

    def _focus_entry(self, entry, at_end=True):
        entry.focus_set()
        entry.icursor('end' if at_end else 0)

    def _set_nav(self, delta):
        """Byt aktivt set via Left/Right på radioknappen."""
        current = self.active_set_var.get()
        new = max(0, min(NUM_COST_SETS - 1, current + delta))
        if new != current:
            self.active_set_var.set(new)
            self._radio_buttons[new].focus_set()
        return 'break'

    def _age_range_text(self, row_idx: int) -> str:
        """Beskrivning av åldersintervallet för en brytpunkt-rad."""
        try:
            current = int(self.age_vars[row_idx].get())
        except ValueError:
            return ''
        if row_idx == 0:
            return f"Utgifter gäller t.o.m {current} år"
        try:
            prev = int(self.age_vars[row_idx - 1].get())
        except ValueError:
            return f"Utgifter gäller upp till {current} år"
        return f"Utgifter gäller {prev + 1} – {current} år"

    def _radio_down(self, set_index):
        """Down på radioknapp → fokusera översta kostnadscellen i samma kolumn."""
        col = set_index + 1  # kolumn 0 är ålder, kolumn 1+ är kostnadsset
        if self.bracket_entries and col < len(self.bracket_entries[0]):
            self._focus_entry(self.bracket_entries[0][col], at_end=True)
        return 'break'

    def _show_cost_context(self, event):
        self._cost_context_target = event.widget
        try:
            self._cost_context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._cost_context_menu.grab_release()

    def _apply_cost_delta(self, delta: int):
        entry = self._cost_context_target
        if entry is None:
            return
        try:
            current = int(entry.get())
        except ValueError:
            current = 0
        new_value = max(0, current + delta)
        entry.delete(0, 'end')
        entry.insert(0, str(new_value))
        self._on_global_change()

    def _apply_derive_rules(self):
        """Tillämpa lagrade härledningsregler för Set 2-5 baserat på Set 1.
        Set med regel None hoppas över (manuellt inmatade). Suppress dirty
        under skrivningen — den anropande koden ansvarar för recalc."""
        n = len(self.age_vars)
        try:
            base = [int(self.cost_sets[0][j].get() or '0') for j in range(n)]
        except (ValueError, AttributeError):
            return
        prev = self._suppress_dirty
        self._suppress_dirty = True
        try:
            for s in range(1, NUM_COST_SETS):
                rule = self._derive_rules[s]
                if not rule:
                    continue
                mode = rule.get('mode')
                val = rule.get('value')
                if val is None:
                    continue
                for j in range(n):
                    if mode == 'kr':
                        nv = int(round(base[j] + val))
                    elif mode == 'pct':
                        nv = int(round(base[j] * val))
                    else:
                        continue
                    self.cost_sets[s][j].set(str(max(0, nv)))
        finally:
            self._suppress_dirty = prev

    def _invalidate_stale_derive_rules(self):
        """Rensa reglar för Set 2-5 där nuvarande värden inte längre matchar
        (dvs användaren har manuellt redigerat setet). Anropas efter user-
        triggade global-change-events."""
        n = len(self.age_vars)
        try:
            base = [int(self.cost_sets[0][j].get() or '0') for j in range(n)]
        except (ValueError, AttributeError):
            return
        for s in range(1, NUM_COST_SETS):
            rule = self._derive_rules[s]
            if not rule:
                continue
            mode = rule.get('mode')
            val = rule.get('value')
            if val is None:
                self._derive_rules[s] = None
                continue
            try:
                actual = [int(self.cost_sets[s][j].get() or '0') for j in range(n)]
            except (ValueError, AttributeError):
                continue
            matches = True
            for j in range(n):
                if mode == 'kr':
                    expected = max(0, int(round(base[j] + val)))
                elif mode == 'pct':
                    expected = max(0, int(round(base[j] * val)))
                else:
                    matches = False
                    break
                if actual[j] != expected:
                    matches = False
                    break
            if not matches:
                self._derive_rules[s] = None

    def _update_set_diff_labels(self):
        """Visa hur Set 2-5 förhåller sig till Set 1 (delta i kr, procent
        eller 'Manuell'). Kallas när kostnadsvärden ändras."""
        if not hasattr(self, '_set_diff_labels'):
            return
        try:
            base = [int(v.get() or '0') for v in self.cost_sets[0]]
        except (ValueError, AttributeError):
            return
        n = len(base)
        for s in range(1, NUM_COST_SETS):
            lbl = self._set_diff_labels[s]
            if lbl is None:
                continue
            try:
                vals = [int(v.get() or '0') for v in self.cost_sets[s]]
            except (ValueError, AttributeError):
                lbl.config(text='')
                continue

            # Suffix ⟳ när lagrad regel finns → auto-uppdateras vid Set 1-ändring
            has_rule = self._derive_rules[s] is not None
            suffix = '  ⟳' if has_rule else ''

            # Testa: konstant delta per rad?
            deltas = [vals[i] - base[i] for i in range(n)]
            if all(d == deltas[0] for d in deltas):
                d = deltas[0]
                if d == 0:
                    text = '= Set 1'
                else:
                    sign = '+' if d > 0 else '−'
                    text = f'Set 1 {sign} {abs(d):,}'.replace(',', '\xa0')
                lbl.config(text=text + suffix, foreground='#556677')
                continue

            # Testa: konstant procent-faktor?
            if all(b > 0 for b in base):
                factors = [vals[i] / base[i] for i in range(n)]
                first = factors[0]
                if all(abs(f - first) < 0.005 for f in factors):
                    pct = round(first * 100)
                    text = f'Set 1 × {pct} %'
                    lbl.config(text=text + suffix, foreground='#556677')
                    continue

            lbl.config(text='Manuell', foreground='#888888')

    def _open_derive_dialog(self, target_set_idx: int):
        """Öppna dialog för att härleda Set (target_set_idx+1) från Set 1."""
        dialog = tk.Toplevel(self.root)
        dialog.title(f"Härled Set {target_set_idx + 1} från Set 1")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.grab_set()

        main = ttk.Frame(dialog, padding=16)
        main.pack()

        ttk.Label(
            main,
            text=f"Beräkna alla värden i Set {target_set_idx + 1} från Set 1:",
            font=('TkDefaultFont', 9, 'bold'),
        ).grid(row=0, column=0, columnspan=2, sticky='w', pady=(0, 8))

        # Förfyll med tidigare sparad regel om sådan finns för detta set
        existing = self._derive_rules[target_set_idx]
        default_mode = 'kr'
        default_val = '0'
        if existing:
            if existing.get('mode') == 'kr':
                default_mode = 'kr'
                default_val = f"{existing.get('value', 0):g}"
            elif existing.get('mode') == 'pct':
                default_mode = 'pct'
                pct = (existing.get('value', 1.0) - 1.0) * 100.0
                default_val = f'{pct:g}'
        mode_var = tk.StringVar(value=default_mode)
        value_var = tk.StringVar(value=default_val)

        ttk.Radiobutton(
            main, text='Lägg till/dra av kronor per rad (negativt = minska)',
            variable=mode_var, value='kr',
        ).grid(row=1, column=0, columnspan=2, sticky='w')
        ttk.Radiobutton(
            main, text='Procent-förändring (t.ex. -10 = 10 % lägre, 25 = 25 % högre)',
            variable=mode_var, value='pct',
        ).grid(row=2, column=0, columnspan=2, sticky='w', pady=(0, 8))

        entry_row = ttk.Frame(main)
        entry_row.grid(row=3, column=0, columnspan=2, sticky='w', pady=(0, 12))
        ttk.Label(entry_row, text='Värde:').pack(side='left')
        entry = ttk.Entry(entry_row, textvariable=value_var, width=10)
        entry.pack(side='left', padx=(6, 0))
        entry.focus_set()
        entry.select_range(0, 'end')

        # Kaskad-alternativ: fyll också Set (X+1)..Set 5 med en inkrementell
        # ökning från föregående set. Visas bara när det finns senare set att
        # fylla (dvs för Set 2-4, ej för Set 5).
        # Förfyll kaskad-fält om alla efterföljande set har regler som matchar
        # en konstant inkrementell ökning från detta set.
        cascade_default = False
        inc_default = '0'
        if existing and target_set_idx < NUM_COST_SETS - 1:
            later = self._derive_rules[target_set_idx + 1:NUM_COST_SETS]
            if all(r is not None and r.get('mode') == existing.get('mode') for r in later):
                if existing.get('mode') == 'kr':
                    prev_val = existing.get('value', 0)
                    inc_vals = []
                    for r in later:
                        inc_vals.append(r.get('value', 0) - prev_val)
                        prev_val = r.get('value', 0)
                    if inc_vals and all(abs(iv - inc_vals[0]) < 0.001 for iv in inc_vals):
                        cascade_default = True
                        inc_default = f'{inc_vals[0]:g}'
                elif existing.get('mode') == 'pct':
                    prev_val = existing.get('value', 1.0)
                    inc_pcts = []
                    for r in later:
                        cur = r.get('value', 1.0)
                        if prev_val > 0:
                            inc_pcts.append((cur / prev_val - 1.0) * 100.0)
                        prev_val = cur
                    if inc_pcts and all(abs(p - inc_pcts[0]) < 0.01 for p in inc_pcts):
                        cascade_default = True
                        inc_default = f'{inc_pcts[0]:g}'
        cascade_var = tk.BooleanVar(value=cascade_default)
        inc_var = tk.StringVar(value=inc_default)
        cascade_frame = ttk.Frame(main)
        cascade_entry_row = ttk.Frame(cascade_frame)
        has_later_sets = target_set_idx < NUM_COST_SETS - 1
        if has_later_sets:
            later_names = ', '.join(
                f'Set {i + 1}' for i in range(target_set_idx + 1, NUM_COST_SETS)
            )
            cb = ttk.Checkbutton(
                cascade_frame,
                text=f'Fyll även efterföljande set ({later_names}) '
                     'med inkrementell ökning från föregående set',
                variable=cascade_var,
            )
            cb.pack(anchor='w')
            ttk.Label(
                cascade_entry_row, text='Inkrementell ökning per set:',
            ).pack(side='left')
            inc_entry = ttk.Entry(cascade_entry_row, textvariable=inc_var, width=10)
            inc_entry.pack(side='left', padx=(6, 0))
            ttk.Label(
                cascade_entry_row, text='(samma enhet som ovan)',
                foreground='#666',
            ).pack(side='left', padx=(6, 0))
            cascade_entry_row.pack(anchor='w', padx=(22, 0), pady=(2, 0))
            cascade_frame.grid(
                row=4, column=0, columnspan=2, sticky='w', pady=(0, 12),
            )

            def _toggle_inc(*_):
                state = 'normal' if cascade_var.get() else 'disabled'
                inc_entry.configure(state=state)
            cascade_var.trace_add('write', _toggle_inc)
            _toggle_inc()

        def _apply_op(src_vals, val, mode):
            out = []
            for src in src_vals:
                if mode == 'kr':
                    nv = int(round(src + val))
                else:
                    nv = int(round(src * (1.0 + val / 100.0)))
                out.append(max(0, nv))
            return out

        def apply():
            raw = value_var.get().strip().replace(',', '.')
            try:
                val = float(raw)
            except ValueError:
                messagebox.showerror(
                    "Ogiltigt värde", "Ange ett tal.", parent=dialog,
                )
                return
            mode = mode_var.get()

            n = len(self.age_vars)
            src_vals = []
            for i in range(n):
                s = self.cost_sets[0][i].get().strip()
                try:
                    src_vals.append(int(s) if s else 0)
                except ValueError:
                    src_vals.append(0)

            # Först: Set X från Set 1
            new_vals = _apply_op(src_vals, val, mode)
            for i, nv in enumerate(new_vals):
                self.cost_sets[target_set_idx][i].set(str(nv))
            # Lagra regel: Set X = Set 1 (+ val kr) eller × faktor
            if mode == 'kr':
                self._derive_rules[target_set_idx] = {'mode': 'kr', 'value': val}
            else:
                self._derive_rules[target_set_idx] = {
                    'mode': 'pct', 'value': 1.0 + val / 100.0,
                }

            # Kaskad: Set (X+1)..Set 5 från föregående set med inkrement
            if has_later_sets and cascade_var.get():
                raw_inc = inc_var.get().strip().replace(',', '.')
                try:
                    inc = float(raw_inc)
                except ValueError:
                    messagebox.showerror(
                        "Ogiltig inkrementell ökning",
                        "Ange ett tal för inkrementell ökning.", parent=dialog,
                    )
                    return
                prev_vals = new_vals
                for s in range(target_set_idx + 1, NUM_COST_SETS):
                    prev_vals = _apply_op(prev_vals, inc, mode)
                    for i, nv in enumerate(prev_vals):
                        self.cost_sets[s][i].set(str(nv))
                    # Lagra regel för cascade-setet — kumulativt från Set 1
                    prev_rule = self._derive_rules[s - 1]
                    if mode == 'kr' and prev_rule and prev_rule.get('mode') == 'kr':
                        self._derive_rules[s] = {
                            'mode': 'kr',
                            'value': prev_rule['value'] + inc,
                        }
                    elif mode == 'pct' and prev_rule and prev_rule.get('mode') == 'pct':
                        self._derive_rules[s] = {
                            'mode': 'pct',
                            'value': prev_rule['value'] * (1.0 + inc / 100.0),
                        }

            dialog.destroy()
            self._on_global_change()

        btn_row = ttk.Frame(main)
        btn_row.grid(row=5, column=0, columnspan=2, sticky='e')
        ttk.Button(btn_row, text='Avbryt', command=dialog.destroy).pack(
            side='right', padx=(6, 0),
        )
        ttk.Button(btn_row, text='Verkställ', command=apply).pack(side='right')

        dialog.bind('<Return>', lambda e: apply())
        dialog.bind('<Escape>', lambda e: dialog.destroy())

        center_dialog_on_parent(dialog, self.root)

    def _nav_left(self, event, row, col):
        if event.widget.index('insert') == 0 and col > 0:
            self._focus_entry(self.bracket_entries[row][col - 1], at_end=True)
            return 'break'

    def _nav_right(self, event, row, col):
        entry = event.widget
        if entry.index('insert') >= len(entry.get()) and col < len(self.bracket_entries[row]) - 1:
            self._focus_entry(self.bracket_entries[row][col + 1], at_end=False)
            return 'break'

    def _nav_up(self, event, row, col):
        if row > 0:
            self._focus_entry(self.bracket_entries[row - 1][col], at_end=True)
            return 'break'
        # Översta raden + kostnadskolumn (col >= 1) → flytta upp till radioknappen
        # och aktivera samtidigt motsvarande set
        if col >= 1 and (col - 1) < len(self._radio_buttons):
            new_set = col - 1
            self.active_set_var.set(new_set)
            self._radio_buttons[new_set].focus_set()
            return 'break'

    def _nav_down(self, event, row, col):
        if row < len(self.bracket_entries) - 1:
            self._focus_entry(self.bracket_entries[row + 1][col], at_end=True)
            return 'break'

    # ---- Globala värden ----

    def get_sparande(self):
        try:
            return float(self.sparande_var.get().replace(' ', '').replace('\xa0', ''))
        except ValueError:
            return None

    def get_end_age(self) -> int:
        """Ålder som simuleringen räknar till. Ogiltigt/tomt fält ger
        standardvärdet; värdet hålls inom MIN_END_AGE–MAX_END_AGE."""
        try:
            v = int(self.end_age_var.get().strip())
        except (ValueError, tk.TclError):
            return DEFAULT_END_AGE
        return max(MIN_END_AGE, min(MAX_END_AGE, v))

    def _sync_end_age(self, *_):
        """Håll modul-värdet END_AGE (som calculate_plan m.fl. läser) i synk
        med fältet."""
        global END_AGE
        END_AGE = self.get_end_age()

    def get_growth_pct(self):
        """Returnerar årlig tillväxt i procent, 0.0 om checkboxen är av eller fältet ogiltigt."""
        if not self.growth_enabled_var.get():
            return 0.0
        try:
            s = self.growth_pct_var.get().replace(',', '.').replace(' ', '').strip()
            if not s:
                return 0.0
            return max(0.0, float(s))
        except ValueError:
            return 0.0

    def get_growth_mode(self) -> str:
        """Returnerar 'monthly' eller 'yearly'.

        När Expert-läget för tillämpning är av tvingas 'monthly'. När det är på
        respekteras dropdownens värde ('Månadsvis' → 'monthly', annars 'yearly').
        """
        if not self.expert_growth_mode_visible_var.get():
            return 'monthly'
        return 'monthly' if self.growth_mode_var.get() == 'Månadsvis' else 'yearly'

    def get_brackets(self):
        active = self.active_set_var.get()
        brackets = []
        for age_var, cost_var in zip(self.age_vars, self.cost_sets[active]):
            try:
                age = int(age_var.get())
                c = int(cost_var.get())
                brackets.append((age, c))
            except ValueError:
                return None
        brackets.sort(key=lambda x: x[0])
        return brackets

    # ---- Planhantering ----

    def add_tab(self, select: bool = True):
        """Skapar och lägger till en ny flik. select=False används under load
        för att undvika att varje ny flik blir momentant aktiv (vilket annars
        triggar onödig matplotlib-rendering)."""
        tab = PlanTab(self, self.notebook)
        self.tabs.append(tab)
        if select:
            self.notebook.select(tab.frame)
        self._mark_plans_dirty()
        self._update_no_plan_view()
        return tab

    def close_tab(self, tab):
        """Stäng en plan. Om det är sista planen: rensa innehållet istället.
        Data läggs på _closed_tabs-stacken så användaren kan ångra."""
        # Push undo-data innan vi förlorar tab-referensen (bara om tab har
        # nåt att komma ihåg — parsed eller paste_text)
        has_data = (
            (tab.parsed is not None and tab.parsed.plan_name)
            or bool(tab.paste_text.get('1.0', 'end-1c').strip())
        )
        if has_data:
            self._closed_tabs.append(self._build_plan_save_entry(tab))
            self._update_undo_close_menu_state()

        if len(self.tabs) <= 1:
            tab._placeholder_visible = False  # delete tar bort placeholder också
            tab.paste_text.delete('1.0', 'end')
            tab._clear_outputs()
            tab._show_paste_placeholder()
        else:
            self.notebook.forget(tab.frame)
            self.tabs.remove(tab)
        self._mark_plans_dirty()
        self._update_no_plan_view()
        # Sync Jämför-fönstret så borttagen plan försvinner från listan
        self._refresh_compare_window()
        # Sync Föreslå plan-fönstret så nya/borttagna planer syns i dropdown
        if self._suggest_window is not None:
            try:
                self._suggest_window._refresh_suggest_bases()
                self._suggest_window._rebuild_suggest_lock_table()
                self._suggest_window._refresh_manual_bases()
            except tk.TclError:
                self._suggest_window = None

    def _undo_close_tab(self):
        """Återupprätta senast stängda plan från _closed_tabs-stacken."""
        if not self._closed_tabs:
            return
        entry = self._closed_tabs.pop()
        self._update_undo_close_menu_state()

        # Om enda fliken är tom (placeholder), återanvänd den; annars ny tab
        cur = self._current_tab()
        if (len(self.tabs) == 1 and cur is not None
                and cur.parsed is None
                and (cur._placeholder_visible
                     or not cur.paste_text.get('1.0', 'end-1c').strip())):
            tab = cur
        else:
            tab = self.add_tab(select=True)

        # Återupprätta antingen från sim_parsed (SIM-planer) eller paste_text
        sim_data = entry.get('sim_parsed')
        if sim_data:
            try:
                parsed = _dict_to_parsed_plan(sim_data)
            except Exception:
                parsed = None
            if parsed is not None:
                tab.apply_parsed(parsed)
        else:
            text = entry.get('paste_text', '')
            if text:
                prev_suppress = self._suppress_dirty
                self._suppress_dirty = True
                try:
                    tab._clear_paste_placeholder()
                    tab.paste_text.delete('1.0', 'end')
                    tab.paste_text.insert('1.0', text)
                finally:
                    self._suppress_dirty = prev_suppress
                tab.on_parse()
        fetched_at = entry.get('fetched_at')
        if isinstance(fetched_at, str) and fetched_at:
            tab.fetched_at = fetched_at
        self._mark_plans_dirty()
        self._refresh_compare_window()
        if self._suggest_window is not None:
            try:
                self._suggest_window._refresh_suggest_bases()
                self._suggest_window._rebuild_suggest_lock_table()
                self._suggest_window._refresh_manual_bases()
            except tk.TclError:
                self._suggest_window = None

    def _update_undo_close_menu_state(self):
        """Enable/disable Ångra stängning-menyposten beroende på stacken."""
        idx = getattr(self, '_undo_close_menu_index', None)
        if idx is None:
            return
        try:
            state = 'normal' if self._closed_tabs else 'disabled'
            self.file_menu.entryconfigure(idx, state=state)
        except tk.TclError:
            pass

    # ---- Drag-och-släpp av flikar ----

    def _on_tab_press(self, event):
        if self.notebook.identify(event.x, event.y) == 'label':
            try:
                self._drag_tab_index = self.notebook.index(f'@{event.x},{event.y}')
            except tk.TclError:
                self._drag_tab_index = None
        else:
            self._drag_tab_index = None

    def _on_tab_drag(self, event):
        if self._drag_tab_index is None:
            return
        self.notebook.config(cursor='exchange')

    def _on_tab_release(self, event):
        self.notebook.config(cursor='')
        src = self._drag_tab_index
        self._drag_tab_index = None
        if src is None:
            return
        try:
            target = self.notebook.index(f'@{event.x},{event.y}')
        except tk.TclError:
            return
        if target == src or not (0 <= target < len(self.tabs)):
            return
        tab = self.tabs.pop(src)
        self.tabs.insert(target, tab)
        self.notebook.insert(target, tab.frame)
        self._mark_plans_dirty()

    def _on_notebook_hover(self, event):
        """Visa tooltip med hämtningsdatum när muspekaren är över en plan-flik."""
        try:
            idx = self.notebook.index(f'@{event.x},{event.y}')
        except tk.TclError:
            idx = -1
        if idx < 0 or idx >= len(self.tabs):
            self._hide_tab_tooltip()
            return
        if idx == self._tab_tooltip_index and self._tab_tooltip_win is not None:
            return  # samma flik — behåll tooltipen
        self._hide_tab_tooltip()
        tab = self.tabs[idx]
        fetched = getattr(tab, 'fetched_at', None)
        if not fetched:
            return
        tw = tk.Toplevel(self.notebook)
        tw.wm_overrideredirect(True)
        tk.Label(
            tw, text=f'Hämtad: {fetched}',
            background='#ffffe0', relief='solid', borderwidth=1,
            font=('TkDefaultFont', 9), padx=6, pady=3,
        ).pack()
        tw.wm_geometry(f'+{event.x_root + 12}+{event.y_root + 18}')
        self._tab_tooltip_win = tw
        self._tab_tooltip_index = idx

    def _hide_tab_tooltip(self, event=None):
        if self._tab_tooltip_win is not None:
            try:
                self._tab_tooltip_win.destroy()
            except tk.TclError:
                pass
            self._tab_tooltip_win = None
        self._tab_tooltip_index = None

    def _on_notebook_right_click(self, event):
        # Identifiera om högerklicket träffade en plan
        clicked_tab = None
        try:
            idx = self.notebook.index(f'@{event.x},{event.y}')
            if 0 <= idx < len(self.tabs):
                clicked_tab = self.tabs[idx]
        except tk.TclError:
            pass

        if clicked_tab is None:
            return  # klick bredvid flikarna — inget att visa
        menu = tk.Menu(self.root, tearoff=False)
        menu.add_command(
            label="Stäng plan",
            command=lambda t=clicked_tab: self.close_tab(t),
        )
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    # ---- Spara / Öppna (unified format med både inställningar och planer) ----

    def _apply_settings(self, data):
        prev_suppress = self._suppress_dirty
        self._suppress_dirty = True
        try:
            if 'sparande' in data:
                self.sparande_var.set(str(data['sparande']))
            if 'growth_enabled' in data:
                self.growth_enabled_var.set(bool(data['growth_enabled']))
            if 'growth_pct' in data:
                self.growth_pct_var.set(str(data['growth_pct']))
            # Äldre filer saknar end_age → standardvärdet (inte föregående fils)
            self.end_age_var.set(str(data.get('end_age', DEFAULT_END_AGE)))
            if 'growth_mode' in data:
                mode = str(data['growth_mode'])
                if mode in ('Månadsvis', 'Årsvis'):
                    self.growth_mode_var.set(mode)
            if 'ages' in data:
                for var, val in zip(self.age_vars, data['ages']):
                    var.set(str(val))
            if 'cost_sets' in data:
                for cost_set, vals in zip(self.cost_sets, data['cost_sets']):
                    for var, val in zip(cost_set, vals):
                        var.set(str(val))
            if 'active_set' in data:
                try:
                    self.active_set_var.set(int(data['active_set']))
                except (ValueError, TypeError):
                    pass
            # Utgiftskategorier — nollställ om filen saknar dem (annars läcker
            # kategorier från föregående öppnat dokument)
            if 'expense_categories' in data:
                exp = data['expense_categories']
                self._expense_categories = exp if isinstance(exp, list) else None
            else:
                self._expense_categories = None
            # Härledningsregler för Set 2-5
            rules = data.get('derive_rules')
            if isinstance(rules, list):
                self._derive_rules = [
                    r if isinstance(r, dict) and 'mode' in r and 'value' in r else None
                    for r in (rules + [None] * NUM_COST_SETS)[:NUM_COST_SETS]
                ]
            else:
                self._derive_rules = [None] * NUM_COST_SETS
            # Sync Set 1 och lås/lås upp entries (ingen recalc — caller kör en)
            self._update_set1_from_expenses()
            self._update_set1_lock_state()
            # Uppdatera ev. öppet utgifts-fönster till nya dokumentets data
            if self._expense_window is not None:
                try:
                    self._expense_window.reload_from_app()
                except tk.TclError:
                    self._expense_window = None
        finally:
            self._suppress_dirty = prev_suppress

    def _apply_plans(self, plans):
        """plans = lista med {'paste_text': str}."""
        prev_suppress = self._suppress_dirty
        self._suppress_dirty = True
        # Töm undo-stacken — den skulle referera till planer från en annan fil
        self._closed_tabs = []
        self._update_undo_close_menu_state()
        try:
            for tab in list(self.tabs):
                self.notebook.forget(tab.frame)
            self.tabs.clear()
            self._initial_load_in_progress = True
            for i, tab_data in enumerate(plans):
                # Bara första fliken får auto-väljas; övriga annars
                # blir momentant aktiva och triggar onödig chart-rendering
                tab = self.add_tab(select=(i == 0))
                sim_data = tab_data.get('sim_parsed')
                if sim_data:
                    # SIM-plan från greedy-optimering — rekonstruera direkt.
                    try:
                        parsed = _dict_to_parsed_plan(sim_data)
                    except Exception:
                        parsed = None
                    if parsed is not None:
                        tab.apply_parsed(parsed)
                else:
                    text = tab_data.get('paste_text', '')
                    if text:
                        tab._clear_paste_placeholder()
                        tab.paste_text.insert('1.0', text)
                        tab.on_parse()
                fetched_at = tab_data.get('fetched_at')
                if isinstance(fetched_at, str) and fetched_at:
                    tab.fetched_at = fetched_at
            if not self.tabs:
                self.add_tab()
            self._initial_load_in_progress = False
        finally:
            self._suppress_dirty = prev_suppress

    def _current_tab_index(self) -> int:
        selected = self.notebook.select()
        if not selected:
            return 0
        for i, tab in enumerate(self.tabs):
            if str(tab.frame) == selected:
                return i
        return 0

    def _current_plan_name(self) -> str:
        """Returnerar plan-namnet för nuvarande aktiv flik, eller tom sträng."""
        tab = self._current_tab()
        if tab and tab.parsed and tab.parsed.plan_name:
            return tab.parsed.plan_name
        return ''

    def _select_tab_by_plan_name(self, plan_name: str) -> bool:
        """Aktivera flik vars plan_name matchar. Returnerar True om hittad."""
        if not plan_name:
            return False
        for tab in self.tabs:
            if tab.parsed and tab.parsed.plan_name == plan_name:
                self.notebook.select(tab.frame)
                tab.ensure_chart_rendered()
                return True
        return False

    def _render_current_chart(self):
        """Rendera diagrammet för aktuell flik om det är dirty."""
        selected = self.notebook.select()
        for tab in self.tabs:
            if str(tab.frame) == selected:
                tab.ensure_chart_rendered()
                return

    def _remember_state(self):
        """Skriv state-filen: last_file + active_plans + compare-state per fil."""
        state = self._read_state()
        active_plans = state.get('active_plans', {})
        if not isinstance(active_plans, dict):
            active_plans = {}
        if self._current_file:
            active_plans[self._current_file] = self._current_plan_name()
        state['last_file'] = self._current_file
        state['active_plans'] = active_plans

        # Compare-state: per fil → {plans: [namn], show_kvar: [bool, bool, bool]}
        compare = state.get('compare', {})
        if not isinstance(compare, dict):
            compare = {}
        if self._current_file and self._compare_state:
            compare[self._current_file] = self._compare_state
        state['compare'] = compare

        # Compare-fönstrets position/storlek (global, oberoende av öppen fil)
        if self._compare_window_geometry:
            state['compare_window_geometry'] = self._compare_window_geometry

        # Inkomst-vyns position/storlek
        if self._insurance_income_window_geometry:
            state['insurance_income_window_geometry'] = self._insurance_income_window_geometry

        # Månadsvis detalj-fönstrets position/storlek
        if self._monthly_window_geometry:
            state['monthly_window_geometry'] = self._monthly_window_geometry

        # Wizard-fönstrets position/storlek
        if self._minpension_wizard_geometry:
            state['minpension_wizard_geometry'] = self._minpension_wizard_geometry

        # Utgifts-fönstrets position/storlek (kategorier ligger i plan-JSON)
        if self._expense_window_geometry:
            state['expense_window_geometry'] = self._expense_window_geometry

        # Expert-läge: visa tillämpnings-dropdown (global)
        state['expert_growth_mode_visible'] = bool(
            self.expert_growth_mode_visible_var.get()
        )

        # Senast använda mappar för Spara som / Exportera PDF
        if self._last_save_dir:
            state['last_save_dir'] = self._last_save_dir
        if self._last_pdf_dir:
            state['last_pdf_dir'] = self._last_pdf_dir

        # Huvudfönstrets geometri (storlek + position)
        try:
            geom = self.root.winfo_geometry()
            if geom and 'x' in geom:
                state['main_window_geometry'] = geom
        except tk.TclError:
            pass

        # Ta bort legacy-nycklar
        state.pop('active_plan', None)
        state.pop('active_tab', None)
        try:
            with open(STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(state, f, indent=2)
        except Exception:
            pass

    def _load_compare_state(self):
        """Läs compare-state för aktuell fil från state-filen."""
        if not self._current_file:
            self._compare_state = {}
            return
        state = self._read_state()
        compare = state.get('compare', {})
        if isinstance(compare, dict):
            entry = compare.get(self._current_file, {})
            if isinstance(entry, dict):
                self._compare_state = entry
                return
        self._compare_state = {}

    def _read_state(self) -> dict:
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}

    def _build_save_data(self) -> dict:
        settings = {
            'sparande': self.sparande_var.get(),
            'growth_enabled': self.growth_enabled_var.get(),
            'growth_pct': self.growth_pct_var.get(),
            'growth_mode': self.growth_mode_var.get(),
            'end_age': self.get_end_age(),
            'ages': [v.get() for v in self.age_vars],
            'cost_sets': [[v.get() for v in s] for s in self.cost_sets],
            'active_set': self.active_set_var.get(),
            'active_plan': self._current_plan_name(),
        }
        if self._expense_categories is not None:
            settings['expense_categories'] = self._expense_categories
        if any(r is not None for r in self._derive_rules):
            settings['derive_rules'] = self._derive_rules
        return {
            'version': 2,
            'settings': settings,
            'plans': [
                self._build_plan_save_entry(tab) for tab in self.tabs
            ],
        }

    def _build_plan_save_entry(self, tab):
        """Bygg en {paste_text, ...} dict för en flik. Om paste_text är
        en icke-parse:bar stub (tom, eller börjar med '#' som kommentar)
        sparas även parsed som 'sim_parsed' så planen kan rekonstrueras
        vid nästa load. Detta täcker SIM_FROM_-, MAN_FROM_- och demo-planer."""
        entry = {
            'paste_text': tab.paste_text.get('1.0', 'end-1c'),
        }
        if tab.fetched_at:
            entry['fetched_at'] = tab.fetched_at
        name = tab.parsed.plan_name if tab.parsed else ''
        if tab.parsed is not None and name:
            paste_stripped = entry['paste_text'].lstrip()
            is_stub = (
                not paste_stripped
                or paste_stripped.startswith('#')
                or name.startswith('SIM_FROM_')
                or name.startswith('MAN_FROM_')
            )
            if is_stub:
                entry['sim_parsed'] = _parsed_plan_to_dict(tab.parsed)
        return entry

    def _save_to(self, filename: str) -> bool:
        try:
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(self._build_save_data(), f, indent=2, ensure_ascii=False)
            self._current_file = filename
            self._remember_state()
            self._settings_dirty = False
            self._plans_dirty = False
            self._update_window_title()
            return True
        except Exception as e:
            messagebox.showerror("Fel vid sparande", str(e))
            return False

    def _apply_file_data(self, data: dict):
        """Applicera unified eller gammalt format-data på state — utan flikval."""
        if 'settings' in data:
            self._apply_settings(data['settings'])
        elif 'sparande' in data or 'ages' in data:
            self._apply_settings(data)  # gammalt settings-format
        if 'plans' in data:
            self._apply_plans(data['plans'])
        elif 'tabs' in data:
            self._apply_plans(data['tabs'])  # gammalt plans-format

    def _clear_all_tabs(self):
        """Ta bort alla flikar utan att markera dirty."""
        prev = self._suppress_dirty
        self._suppress_dirty = True
        try:
            for tab in list(self.tabs):
                self.notebook.forget(tab.frame)
            self.tabs.clear()
        finally:
            self._suppress_dirty = prev

    def _load_from(self, filename: str) -> bool:
        # Visa watch-cursor under hela laddningen (kan ta tid om många planer)
        self.root.config(cursor='watch')
        self.root.update_idletasks()
        try:
            # Försök läsa filen först — om det misslyckas behåller vi befintliga flikar
            try:
                with open(filename, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except Exception as e:
                messagebox.showerror("Fel vid läsning", str(e))
                return False
            # Spara state för den GAMLA filen så dess nuvarande flik kommer ihåg
            self._remember_state()
            # Rensa skärmen och uppdatera till tomt läge innan nya data laddas
            self._clear_all_tabs()
            self.root.update_idletasks()
            # Applicera laddad data
            self._apply_file_data(data)
            self._current_file = filename
            self._load_compare_state()
            # Aktivera flik: state-filens minne för just denna fil prioriteras,
            # annars datafilens settings.active_plan
            state = self._read_state()
            remembered = state.get('active_plans', {}).get(filename, '')
            fallback = data.get('settings', {}).get('active_plan', '')
            active_plan = remembered or fallback
            if active_plan:
                self._select_tab_by_plan_name(active_plan)
            else:
                self._render_current_chart()
            # Spara state med nya filen och dess aktiva flik
            self._remember_state()
            self._settings_dirty = False
            self._plans_dirty = False
            self._update_window_title()
            # Köade <<Modified>>-events kan annars sätta dirty efter att vi rensat.
            self.root.after_idle(self._post_load_cleanup)
            return True
        finally:
            self.root.config(cursor='')

    def _post_load_cleanup(self):
        self._settings_dirty = False
        self._plans_dirty = False
        for tab in self.tabs:
            tab.paste_text.edit_modified(False)
        self._update_window_title()

    def _apply_default_settings(self) -> bool:
        """Läs in grundinställningarna (bara inställningar, inga planer) från
        demo-filen som följer med appen. Används när ingen fil återöppnas vid
        start. Filen blir inte aktuellt dokument. Returnerar True om
        inställningar hittades."""
        base = _app_dir()
        candidates = (
            os.path.join(base, 'demo_planer.json'),           # exe-distribution
            os.path.join(base, 'Saved', 'demo_planer.json'),  # källkod, byggd
            os.path.join(base, 'templates', 'demo_settings.json'),
        )
        for path in candidates:
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            settings = data.get('settings', data)
            if not isinstance(settings, dict) or 'sparande' not in settings:
                continue
            self._apply_settings(settings)
            return True
        return False

    def _try_auto_load(self) -> bool:
        state = self._read_state()
        path = state.get('last_file')
        if not path or not os.path.exists(path):
            return False
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            return False
        self._apply_file_data(data)
        self._current_file = path
        self._load_compare_state()
        # Bestäm aktiv flik från state.active_plans[path], annars datafilens, annars legacy
        active_plans = state.get('active_plans', {})
        if isinstance(active_plans, dict):
            remembered = active_plans.get(path, '')
        else:
            remembered = ''
        active_plan = (
            remembered
            or data.get('settings', {}).get('active_plan', '')
            or state.get('active_plan', '')  # legacy single-value
        )
        if active_plan and self._select_tab_by_plan_name(active_plan):
            return True
        # Backward-compat: gammalt index-baserat active_tab
        active_tab = state.get('active_tab')
        if isinstance(active_tab, int) and 0 <= active_tab < len(self.tabs):
            self.notebook.select(self.tabs[active_tab].frame)
        self._render_current_chart()
        return True

    def on_save_as(self):
        initial_dir = self._last_save_dir
        if not initial_dir and self._current_file:
            initial_dir = os.path.dirname(self._current_file)
        filename = filedialog.asksaveasfilename(
            defaultextension='.json',
            filetypes=[('JSON', '*.json')],
            initialfile=os.path.basename(self._current_file) if self._current_file else 'min_plan.json',
            initialdir=initial_dir if initial_dir and os.path.isdir(initial_dir) else None,
        )
        if not filename:
            return False
        if self._save_to(filename):
            self._last_save_dir = os.path.dirname(filename)
            self._remember_state()
            messagebox.showinfo("Sparat", f"Sparad:\n{filename}")
            return True
        return False

    def on_save_quick(self):
        """Spara till nuvarande fil. Om ingen är öppen, kör 'Spara som...'."""
        if not self._current_file:
            return self.on_save_as()
        return self._save_to(self._current_file)

    def on_load(self):
        # Fråga om osparade ändringar innan ny fil laddas
        if self._settings_dirty or self._plans_dirty:
            if not self._confirm_unsaved_before_load():
                return
        filename = filedialog.askopenfilename(
            filetypes=[('JSON', '*.json'), ('Alla filer', '*.*')],
        )
        if not filename:
            return
        self._load_from(filename)

    def _confirm_unsaved_before_load(
        self, discard_label: str = 'Öppna utan att spara',
    ) -> bool:
        """Hantera osparade ändringar innan ny fil laddas/skapas/stängs.
        Returnerar True om det är OK att fortsätta, False om operationen avbryts."""
        if self._settings_dirty and self._plans_dirty:
            what = "Inställningar och planer"
        elif self._settings_dirty:
            what = "Inställningar"
        else:
            what = "Planer"
        file_part = ''
        if self._current_file:
            name = os.path.basename(self._current_file)
            if name.lower().endswith('.json'):
                name = name[:-5]
            file_part = f"\nFil: {name}"
        msg = (
            f"{what} har osparade ändringar.{file_part}\n\n"
            "Vad vill du göra?"
        )
        choice = self._confirm_exit_dialog(
            msg,
            save_label='Spara först',
            discard_label=discard_label,
        )
        if choice == 'cancel':
            return False
        if choice == 'save':
            if not self.on_save_quick():
                return False  # save avbruten
        return True

    # ---- Stängningshantering med osparade ändringar ----

    def _confirm_exit_dialog(
        self,
        message: str,
        save_label: str = 'Spara och stäng',
        discard_label: str = 'Stäng utan att spara',
    ) -> str:
        """Visa modal dialog och returnera 'save', 'discard' eller 'cancel'."""
        dialog = tk.Toplevel(self.root)
        dialog.title("Osparade ändringar")
        dialog.transient(self.root)
        dialog.resizable(False, False)

        result = {'value': 'cancel'}

        main = ttk.Frame(dialog, padding=20)
        main.pack()
        ttk.Label(main, text=message, wraplength=420, justify='left').pack(
            anchor='w', pady=(0, 16),
        )

        btn_frame = ttk.Frame(main)
        btn_frame.pack(fill='x')

        def choose(value):
            result['value'] = value
            dialog.destroy()

        ttk.Button(btn_frame, text=save_label,
                   command=lambda: choose('save')).pack(side='left', padx=4)
        ttk.Button(btn_frame, text=discard_label,
                   command=lambda: choose('discard')).pack(side='left', padx=4)
        ttk.Button(btn_frame, text="Avbryt",
                   command=lambda: choose('cancel')).pack(side='right', padx=4)

        dialog.bind('<Escape>', lambda e: choose('cancel'))
        dialog.protocol('WM_DELETE_WINDOW', lambda: choose('cancel'))
        dialog.grab_set()

        center_dialog_on_parent(dialog, self.root)

        dialog.focus_set()
        self.root.wait_window(dialog)
        return result['value']

    def _on_close(self):
        """Fönsterstängningshandler — fråga om osparade ändringar."""
        # Fånga sub-fönsters aktuella position/storlek om de fortfarande är
        # öppna, så geometrin sparas även när appen avslutas direkt
        if self._compare_window is not None:
            try:
                self._compare_window_geometry = self._compare_window.top.geometry()
            except tk.TclError:
                pass
        if self._insurance_income_window is not None:
            try:
                self._insurance_income_window_geometry = self._insurance_income_window.top.geometry()
            except tk.TclError:
                pass
        if self._monthly_window is not None:
            try:
                self._monthly_window_geometry = self._monthly_window.top.geometry()
            except tk.TclError:
                pass
        if self._minpension_wizard is not None:
            try:
                self._minpension_wizard_geometry = self._minpension_wizard.top.geometry()
            except tk.TclError:
                pass
        if self._expense_window is not None:
            try:
                self._expense_window_geometry = self._expense_window.top.geometry()
            except tk.TclError:
                pass
        if self._suggest_window is not None:
            # Suggest sparar sin geometri i state direkt vid close, men vi
            # anropar _on_close här också för att spara aktuellt läge om
            # användaren stänger appen med Föreslå-fönstret fortfarande öppet.
            try:
                self._suggest_window._on_close()
            except tk.TclError:
                pass

        if not self._settings_dirty and not self._plans_dirty:
            self._remember_state()
            self.root.destroy()
            return

        if self._settings_dirty and self._plans_dirty:
            what = "Inställningar och planer"
        elif self._settings_dirty:
            what = "Inställningar"
        else:
            what = "Planer"
        file_part = ''
        if self._current_file:
            name = os.path.basename(self._current_file)
            if name.lower().endswith('.json'):
                name = name[:-5]
            file_part = f"\nFil: {name}"
        msg = f"{what} har osparade ändringar.{file_part}\n\nVad vill du göra?"

        choice = self._confirm_exit_dialog(msg)

        if choice == 'cancel':
            return  # avbryt stängning

        if choice == 'save':
            if not self.on_save_quick():
                return  # användaren avbröt save → avbryt stängning

        # 'discard' eller save lyckades
        self._remember_state()
        self.root.destroy()


def _get_windows_monitors():
    """Return dict {display_num: (left, top, right, bottom)} from Windows, or {} on other OS."""
    if sys.platform != 'win32':
        return {}

    import ctypes
    from ctypes import wintypes

    class MONITORINFOEX(ctypes.Structure):
        _fields_ = [
            ('cbSize', wintypes.DWORD),
            ('rcMonitor', wintypes.RECT),
            ('rcWork', wintypes.RECT),
            ('dwFlags', wintypes.DWORD),
            ('szDevice', wintypes.WCHAR * 32),
        ]

    user32 = ctypes.windll.user32
    monitors = {}

    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_int,
        wintypes.HMONITOR,
        wintypes.HDC,
        ctypes.POINTER(wintypes.RECT),
        ctypes.c_double,
    )

    def callback(hmonitor, hdc, lprect, lparam):
        info = MONITORINFOEX()
        info.cbSize = ctypes.sizeof(MONITORINFOEX)
        if user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
            m = re.search(r'DISPLAY(\d+)', info.szDevice)
            if m:
                rect = info.rcMonitor
                monitors[int(m.group(1))] = (rect.left, rect.top, rect.right, rect.bottom)
        return 1

    user32.EnumDisplayMonitors(0, 0, MONITORENUMPROC(callback), 0)
    return monitors


def _geometry_on_some_monitor(geom: str) -> bool:
    """True om fönstrets titelrad (enligt 'WxH+X+Y', där X/Y får vara
    negativa) hamnar på någon ansluten skärm. Kan skärmarna inte räknas
    upp (annat OS) antas positionen vara giltig."""
    import re
    m = re.match(r'^(\d+)x(\d+)\+(-?\d+)\+(-?\d+)$', geom.strip())
    if not m:
        return False
    w, _h, x, y = (int(v) for v in m.groups())
    monitors = _get_windows_monitors()
    if not monitors:
        return True
    # Minst 100 px av titelraden ska synas på en skärm
    for left, top, right, bottom in monitors.values():
        if x + w - 100 >= left and x + 100 <= right and top - 10 <= y <= bottom - 40:
            return True
    return False


def _print_monitor_list():
    """Lista alla skärmar med deras nummer, position och storlek."""
    monitors = _get_windows_monitors()
    if not monitors:
        print("Kunde inte räkna upp skärmar (eller annat OS än Windows).")
        return
    print(f"Hittade {len(monitors)} skärmar:")
    # Sortera även efter position (vänster-till-höger, topp-till-botten)
    by_pos = sorted(
        monitors.items(),
        key=lambda kv: (kv[1][1], kv[1][0]),  # (top, left)
    )
    pos_idx = {disp_num: i + 1 for i, (disp_num, _) in enumerate(by_pos)}
    for disp_num in sorted(monitors):
        left, top, right, bottom = monitors[disp_num]
        w, h = right - left, bottom - top
        pos = pos_idx[disp_num]
        print(
            f"  --display {disp_num}  =  Windows DISPLAY{disp_num}  |  "
            f"position ({left},{top}), storlek {w}x{h}  |  "
            f"position-sorterad #{pos}"
        )
    print()
    print("Använd --display <nummer> för att starta på den skärmen.")


def _move_to_display(root, display_num):
    monitors = _get_windows_monitors()
    if not monitors:
        print("Kunde inte räkna upp skärmar (eller annat OS än Windows).", file=sys.stderr)
        return
    rect = monitors.get(display_num)
    if rect is None:
        print(
            f"Skärm {display_num} hittades inte. Tillgängliga: {sorted(monitors)}",
            file=sys.stderr,
        )
        print("Kör med --list-displays för att se alla skärmar.", file=sys.stderr)
        return
    left, top, right, bottom = rect
    w, h = right - left, bottom - top
    print(
        f"Flyttar till skärm {display_num}: position ({left},{top}), "
        f"storlek {w}x{h}",
        file=sys.stderr,
    )
    root.geometry(f'+{left + 50}+{top + 50}')


def _ensure_std_streams():
    """När appen startas med pythonw.exe eller från en genväg finns ingen
    konsol och sys.stdout/stderr kan vara None. Alla print()-anrop kraschar
    då tyst vid startup. Redirect:a till /dev/null (os.devnull) så att
    programmet kör vidare oberoende av hur det startats."""
    import os
    devnull = None
    for stream in ('stdout', 'stderr'):
        if getattr(sys, stream, None) is None:
            if devnull is None:
                devnull = open(os.devnull, 'w', encoding='utf-8')
            setattr(sys, stream, devnull)


def _install_crash_logger():
    """Under pythonw.exe finns ingen konsol — tysta krascher är omöjliga
    att debugga. Logga oväntade undantag till en fil bredvid appen så
    användaren ser vad som gick fel."""
    import os
    import traceback
    log_path = os.path.join(_app_dir(), 'pension_planner_error.log')

    def _hook(exc_type, exc_value, exc_tb):
        try:
            from datetime import datetime
            with open(log_path, 'a', encoding='utf-8') as f:
                f.write(f'--- {datetime.now().isoformat()} ---\n')
                traceback.print_exception(
                    exc_type, exc_value, exc_tb, file=f,
                )
                f.write('\n')
        except Exception:
            pass
        # Visa också som popup så användaren märker att något gick fel
        try:
            import tkinter as _tk
            from tkinter import messagebox as _mb
            _root = _tk.Tk()
            _root.withdraw()
            _mb.showerror(
                'Pension Planner: fel',
                f'Ett fel inträffade. Loggat till:\n{log_path}\n\n'
                f'{exc_type.__name__}: {exc_value}',
            )
            _root.destroy()
        except Exception:
            pass

    sys.excepthook = _hook


def _startup_log(msg):
    """Skriv startup-progress till fil så vi kan diagnostisera hängande
    processer utan konsol."""
    import os
    try:
        p = os.path.join(_app_dir(), 'pension_planner_startup.log')
        from datetime import datetime
        with open(p, 'a', encoding='utf-8') as f:
            f.write(f'{datetime.now().isoformat()}  {msg}\n')
    except Exception:
        pass


def main():
    _ensure_std_streams()
    _install_crash_logger()
    _startup_log(f'START pid={__import__("os").getpid()} argv={sys.argv}')
    parser = argparse.ArgumentParser(description='Pension Planner')
    parser.add_argument(
        '--display', type=int, default=None, metavar='N',
        help='Skärmnummer att starta på när ingen fönsterposition finns '
             'sparad (Windows DISPLAY-nummer; kör --list-displays för att '
             'se vilka som finns)',
    )
    parser.add_argument(
        '--list-displays', action='store_true',
        help='Skriv ut alla skärmar med deras nummer och position, avsluta sedan',
    )
    args = parser.parse_args()
    _startup_log(f'args parsed: display={args.display}')

    if args.list_displays:
        _print_monitor_list()
        return

    _startup_log('creating Tk root')
    root = tk.Tk()
    _startup_log('creating PensionPlannerApp')
    app = PensionPlannerApp(root)
    _startup_log('app created')
    # --display gäller bara när det inte finns någon ihågkommen position
    # (första start, eller om den sparade skärmen inte längre finns) —
    # annars skulle fönstret alltid hamna på samma ställe.
    if args.display is not None and not app._main_position_restored:
        _startup_log(f'moving to display {args.display}')
        _move_to_display(root, args.display)
    _startup_log('entering mainloop')
    root.mainloop()
    _startup_log('END')


if __name__ == '__main__':
    main()
