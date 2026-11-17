"""Toplevel-fönster: visar kr/mån före skatt per försäkring för aktiv plan.

Data kommer från `Insurance.amounts_before_tax` (parallell till
`ParsedPlan.breakpoints`) som fylls i av parsern när planen hämtas via
minpension-scraper-wizarden. Manuellt inklistrade planer saknar denna data
— då visas ett informativt meddelande istället.
"""

import tkinter as tk
from tkinter import ttk

from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.ticker import MultipleLocator

# Import lokala helpers från pension_planner — undvik cirkulär import med late binding
def _imports():
    from pension_planner import INSURANCE_COLORS, _make_dot_image, fmt_kr
    return INSURANCE_COLORS, _make_dot_image, fmt_kr


class InsuranceIncomeWindow:
    """Toplevel som visar kr/mån-belopp per försäkring vid varje brytpunktsdatum."""

    def __init__(self, app, tab):
        self.app = app
        self.tab = tab
        self.top = tk.Toplevel(app.root)
        plan_name = tab.parsed.plan_name if tab.parsed and tab.parsed.plan_name else '<okänd>'
        self.top.title(f'Inkomst per försäkring — Plan: {plan_name}')
        self.top.protocol('WM_DELETE_WINDOW', self._on_close)
        self._apply_initial_geometry()
        self._dot_images = []  # behåll PhotoImage-referenser
        # Tooltip-state för cell-hover
        self._tip = None  # Toplevel
        self._tip_key = None  # (tree, row_id, col_idx) för dedup
        # Per-rad data för tooltips: lista av {'name', 'cum_before', 'cum_after'}
        self._row_data_before = []  # raderna i before_tree, i ordning
        self._row_data_after = []   # raderna i after_tree
        # Gantt-tooltip (flytande popup vid muspekare, likt Planera manuellt)
        self._gantt_tip = None
        self._gantt_insurances = []  # spara för hover-lookup
        # Aktuell ljusgrå period-markering (från cell-hover i tabellerna).
        # Ritas i BÅDE månadsinkomst- och Gantt-grafen så användaren ser
        # samma period markerad i båda.
        self._income_span_patch = None
        self._gantt_span_patch = None
        self._highlighted_col_idx = None
        self._build_ui()
        self._populate()

    def _apply_initial_geometry(self):
        """Återställ senast använd geometri, annars centrera på huvudfönstret."""
        saved = getattr(self.app, '_insurance_income_window_geometry', None)
        if isinstance(saved, str) and 'x' in saved:
            try:
                self.top.geometry(saved)
                return
            except tk.TclError:
                pass
        # Default: centrerad på huvudfönstret
        self.app.root.update_idletasks()
        main_x = self.app.root.winfo_x()
        main_y = self.app.root.winfo_y()
        main_w = self.app.root.winfo_width()
        main_h = self.app.root.winfo_height()
        cw, ch = 1280, 1080
        x = main_x + max(0, (main_w - cw) // 2)
        y = main_y + max(0, (main_h - ch) // 2)
        self.top.geometry(f'{cw}x{ch}+{x}+{y}')

    def refresh_for_tab(self, new_tab):
        """Byt aktiv flik och repopulera vyn med den nya planens data."""
        # Dölj ev. öppen cell-tooltip från gamla data
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None
        self._tip_key = None
        self._hide_gantt_tip()
        self.tab = new_tab
        plan_name = (
            new_tab.parsed.plan_name
            if new_tab.parsed and new_tab.parsed.plan_name
            else '<okänd>'
        )
        try:
            self.top.title(f'Inkomst per försäkring — Plan: {plan_name}')
        except tk.TclError:
            return
        # Rensa gammalt tabell-innehåll + PhotoImage-cache innan repop
        for w in self.tree_frame.winfo_children():
            w.destroy()
        self._dot_images = []
        self._row_data_before = []
        self._row_data_after = []
        self._populate()

    def _build_ui(self):
        outer = ttk.Frame(self.top, padding=8)
        outer.pack(fill='both', expand=True)
        outer.columnconfigure(0, weight=1)
        # rad 5 (tabellerna) expanderar; graf-raderna har fast höjd via figsize
        outer.rowconfigure(5, weight=1)

        self.header_label = ttk.Label(
            outer, text='', font=('TkDefaultFont', 9), foreground='#555',
        )
        self.header_label.grid(row=0, column=0, sticky='w', pady=(0, 4))

        # Gantt-rubrik i fetstil (matchar 'Före skatt'/'Efter skatt'-labels)
        ttk.Label(
            outer, text='Utbetalningsperioder',
            font=('TkDefaultFont', 9, 'bold'),
        ).grid(row=1, column=0, sticky='w', pady=(0, 2))

        # Gantt-graf över utbetalningsperioder per försäkring, inramad.
        # padx=(0, 17) reserverar plats motsvarande den vertikala scrollbaren
        # som ligger till höger om tabellerna nedan, så rutan blir exakt lika
        # bred som Före/Efter skatt-tabellerna.
        # highlightbackground='#a0a0a0' matchar ttk-themats gråa Treeview-kant.
        gantt_frame = tk.Frame(
            outer, bd=0, highlightthickness=1, highlightbackground='#a0a0a0',
        )
        gantt_frame.grid(row=2, column=0, sticky='ew', pady=(0, 6), padx=(0, 17))
        self.gantt_fig = Figure(figsize=(9, 1.7), dpi=100)
        self.gantt_ax = self.gantt_fig.add_subplot(111)
        self.gantt_canvas = FigureCanvasTkAgg(self.gantt_fig, master=gantt_frame)
        self.gantt_canvas.get_tk_widget().pack(fill='both', expand=True)
        self.gantt_canvas.mpl_connect(
            'motion_notify_event', self._on_gantt_motion,
        )
        self.gantt_canvas.mpl_connect(
            'axes_leave_event', lambda e: self._hide_gantt_tip(),
        )
        self.gantt_canvas.mpl_connect(
            'figure_leave_event', lambda e: self._hide_gantt_tip(),
        )

        # Månadsinkomst-rubrik + graf (netto efter skatt vs utgifter)
        ttk.Label(
            outer, text='Månadsinkomst (efter skatt) vs utgifter',
            font=('TkDefaultFont', 9, 'bold'),
        ).grid(row=3, column=0, sticky='w', pady=(0, 2))
        income_frame = tk.Frame(
            outer, bd=0, highlightthickness=1, highlightbackground='#a0a0a0',
        )
        income_frame.grid(row=4, column=0, sticky='ew', pady=(0, 6), padx=(0, 17))
        self.income_fig = Figure(figsize=(9, 1.7), dpi=100)
        self.income_ax = self.income_fig.add_subplot(111)
        self.income_canvas = FigureCanvasTkAgg(self.income_fig, master=income_frame)
        self.income_canvas.get_tk_widget().pack(fill='both', expand=True)
        # Popup vid hover, samma mönster som Planera manuellt
        self.income_canvas.mpl_connect(
            'motion_notify_event', self._on_income_motion,
        )
        self.income_canvas.mpl_connect(
            'axes_leave_event', lambda e: self._hide_gantt_tip(),
        )
        self.income_canvas.mpl_connect(
            'figure_leave_event', lambda e: self._hide_gantt_tip(),
        )

        self.tree_frame = ttk.Frame(outer)
        self.tree_frame.grid(row=5, column=0, sticky='nsew')
        self.tree_frame.columnconfigure(0, weight=1)
        self.tree_frame.rowconfigure(0, weight=1)

        footer = ttk.Frame(outer)
        footer.grid(row=6, column=0, sticky='ew', pady=(6, 0))
        ttk.Button(footer, text='Stäng', command=self._on_close).pack(side='right')
        ttk.Label(
            footer,
            text=(
                'Skatten är endast baserad på data från minpension.se och '
                'kan vara annorlunda om det finns inkomster utöver pension.'
            ),
            font=('TkDefaultFont', 9), foreground='#555',
            wraplength=900, justify='left',
        ).pack(side='left', anchor='w')

    def _populate(self):
        INSURANCE_COLORS, _make_dot_image, fmt_kr = _imports()

        if not self.tab.parsed or not self.tab.parsed.insurances:
            self.header_label.config(text='Ingen plan-data hittades.')
            return

        insurances = self.tab.parsed.insurances
        all_breakpoints = self.tab.parsed.breakpoints
        earliest = self.tab.parsed.earliest_start

        # Rita Gantt + månadsinkomst tidigt så även manuellt-inklistrade
        # planer (utan per-månad-belopp) visar utbetalningsperioderna
        self._draw_gantt(insurances)
        self._draw_income_graph()

        # Filtrera bort brytpunkter FÖRE första pensionsutbetalningen (Lön-period
        # där alla pensioner är 0). Detta gör att första kolumnen i denna vy
        # matchar första månaden i "Månadsvis detalj"-tabellen.
        if earliest:
            start_idx = next(
                (i for i, bp in enumerate(all_breakpoints) if bp.date >= earliest), 0,
            )
        else:
            start_idx = 0
        breakpoints = all_breakpoints[start_idx:]

        def _amounts_for(ins):
            """Hämta amounts_before_tax slicad enligt start_idx."""
            if not ins.amounts_before_tax:
                return []
            return ins.amounts_before_tax[start_idx:]

        # Saknas per-försäkring-belopp? (manuell paste-flöde)
        has_amounts = any(ins.amounts_before_tax for ins in insurances)
        if not has_amounts:
            self.header_label.config(text='')
            ttk.Label(
                self.tree_frame, justify='center',
                font=('TkDefaultFont', 11),
                text=(
                    'Inga per-försäkring-belopp tillgängliga för denna plan.\n\n'
                    'Den datan kommer bara med planer hämtade via\n'
                    '"Arkiv → Hämta från minpension.se…" (wizard).'
                ),
            ).place(relx=0.5, rely=0.5, anchor='center')
            return

        n_dates = len(breakpoints)

        # Beräkna skattesats per brytpunkt. minpension.se utelämnar ibland
        # after-tax för de första brytpunkterna (innan full uttagsstart) — då
        # backward-fyller vi raten från nästa kända brytpunkt (samma logik som
        # calculate_plan använder via first_after-fallback för Månadsvis detalj).
        tax_rates = [None] * n_dates
        for i, bp in enumerate(breakpoints):
            if (bp.before_tax and bp.after_tax is not None and bp.before_tax > 0):
                tax_rates[i] = 1.0 - (bp.after_tax / bp.before_tax)
        # Backward-fill: för varje None, använd nästa kända rate
        last_known = None
        for i in reversed(range(n_dates)):
            if tax_rates[i] is not None:
                last_known = tax_rates[i]
            elif last_known is not None:
                tax_rates[i] = last_known
        net_multipliers = [
            (1.0 - r) if r is not None else None for r in tax_rates
        ]

        # Spara pre-collapse-data för kumulativ-beräkning (om vi droppar
        # första kolumnen måste vi ändå inkludera dess utbetalning i kumulativen)
        breakpoints_pre_collapse = breakpoints
        tax_rates_pre_collapse = tax_rates
        net_multipliers_pre_collapse = net_multipliers
        start_idx_pre_collapse = start_idx

        # Droppa första kolumnen om den är identisk med nästa (samma före/efter
        # skatt och samma per-försäkring-belopp). Vanligt fall: första
        # pensionsutbetalningsmånaden (2026-12) följs av nytt skatteår (2027-01)
        # där minpension.se backward-fyller bakåt → kolumnerna blir lika.
        collapsed = False
        if n_dates >= 2:
            bp0, bp1 = breakpoints[0], breakpoints[1]
            # Effektiv efter-skatt med backward-fill
            def _eff_after(bp, mult):
                if bp.after_tax is not None:
                    return bp.after_tax
                if bp.before_tax and mult is not None:
                    return round(bp.before_tax * mult)
                return None
            a0 = _eff_after(bp0, net_multipliers[0])
            a1 = _eff_after(bp1, net_multipliers[1])
            same_totals = (bp0.before_tax == bp1.before_tax and a0 == a1)
            same_per_ins = True
            if same_totals:
                for ins in insurances:
                    if not ins.amounts_before_tax:
                        continue
                    s = start_idx
                    if s + 1 >= len(ins.amounts_before_tax):
                        continue
                    if ins.amounts_before_tax[s] != ins.amounts_before_tax[s + 1]:
                        same_per_ins = False
                        break
            if same_totals and same_per_ins:
                start_idx += 1
                breakpoints = all_breakpoints[start_idx:]
                tax_rates = tax_rates[1:]
                net_multipliers = net_multipliers[1:]
                n_dates = len(breakpoints)
                collapsed = True

        # Sätt kolumn-ID:n EFTER eventuell collapse av första kolumnen
        date_cols = tuple(f'd{i}' for i in range(n_dates))

        # Gemensam kolumn-setup för bägge tabeller
        def _setup_columns(tree, height_rows):
            tree.heading('#0', text='Försäkring')
            tree.column('#0', width=240, minwidth=140, anchor='w', stretch=False)
            for i, bp in enumerate(breakpoints):
                year, month = bp.date
                tree.heading(date_cols[i], text=f'{year}-{month:02d}')
                tree.column(
                    date_cols[i], width=72, minwidth=55, anchor='e', stretch=False,
                )
            tree.tag_configure('row_a', background='#ffffff')
            tree.tag_configure('row_b', background='#f5f5f5')
            tree.tag_configure(
                'summary', background='#e8e8e8', font=('TkDefaultFont', 9, 'bold'),
            )
            tree.tag_configure(
                'taxrate', background='#fff3d0', font=('TkDefaultFont', 9, 'italic'),
            )

        # Layout: två rubricerade tabeller stackade vertikalt
        # tree_frame: [row 0: 'Före skatt'] [row 1: top-tree] [row 2: spacer]
        #             [row 3: 'Efter skatt'] [row 4: bottom-tree] [row 5: hsb]
        self.tree_frame.rowconfigure(0, weight=0)
        self.tree_frame.rowconfigure(1, weight=1)
        self.tree_frame.rowconfigure(3, weight=0)
        self.tree_frame.rowconfigure(4, weight=1)

        ttk.Label(
            self.tree_frame, text='Före skatt (faktiska belopp från minpension.se)',
            font=('TkDefaultFont', 9, 'bold'),
        ).grid(row=0, column=0, sticky='w', pady=(0, 2))

        self.before_tree = ttk.Treeview(
            self.tree_frame, columns=date_cols, show='tree headings', height=12,
        )
        self.before_tree.grid(row=1, column=0, sticky='nsew')
        _setup_columns(self.before_tree, 12)
        vsb1 = ttk.Scrollbar(
            self.tree_frame, orient='vertical', command=self.before_tree.yview,
        )
        self.before_tree.configure(yscrollcommand=vsb1.set)
        vsb1.grid(row=1, column=1, sticky='ns')

        ttk.Label(
            self.tree_frame,
            text='Efter skatt (estimat: före-skatt × planens skattefaktor per brytpunkt)',
            font=('TkDefaultFont', 9, 'bold'),
        ).grid(row=3, column=0, sticky='w', pady=(10, 2))

        self.after_tree = ttk.Treeview(
            self.tree_frame, columns=date_cols, show='tree headings', height=12,
        )
        self.after_tree.grid(row=4, column=0, sticky='nsew')
        _setup_columns(self.after_tree, 12)
        vsb2 = ttk.Scrollbar(
            self.tree_frame, orient='vertical', command=self.after_tree.yview,
        )
        self.after_tree.configure(yscrollcommand=vsb2.set)
        vsb2.grid(row=4, column=1, sticky='ns')

        # En gemensam horisontell scrollbar längst ner som scrollar bägge trees
        def _both_xview(*args):
            self.before_tree.xview(*args)
            self.after_tree.xview(*args)

        hsb = ttk.Scrollbar(
            self.tree_frame, orient='horizontal', command=_both_xview,
        )

        def _set_xscroll(first, last):
            hsb.set(first, last)
            # Synka även den andra
            self.after_tree.xview_moveto(first)

        def _set_xscroll_after(first, last):
            hsb.set(first, last)
            self.before_tree.xview_moveto(first)

        self.before_tree.configure(xscrollcommand=_set_xscroll)
        self.after_tree.configure(xscrollcommand=_set_xscroll_after)
        hsb.grid(row=5, column=0, sticky='ew')

        # Hjälpare för kumulativ beräkning: cum[k] = totalt utbetalat INKLUSIVE
        # periodens utbetalningar från d_k till d_{k+1}. För sista perioden
        # används planens antagna slut (födelseår + Räkna till ålder) som ändpunkt så att
        # livsvariga försäkringars sista period räknas med.
        def _months_between(d1, d2):
            return (d2[0] - d1[0]) * 12 + (d2[1] - d1[1])

        birth_year = self.tab.parsed.birth_year
        if birth_year:
            plan_end_date = (birth_year + self.app.get_end_age(), 12)
        else:
            # Fallback: 30 år efter sista brytpunkten
            last_bp = breakpoints[-1].date
            plan_end_date = (last_bp[0] + 30, last_bp[1])
        self._plan_end_date = plan_end_date

        def _compute_cumulative_for(amounts, bps):
            """Returnerar lista där cum[k] = totalt utbetalat genom period k
            (period k löper från d_k till d_{k+1}, eller till planens slut för
            sista perioden)."""
            cum = []
            running = 0
            n = len(bps)
            for k in range(n):
                amt = amounts[k] if k < len(amounts) and amounts[k] is not None else 0
                if k < n - 1:
                    months = _months_between(bps[k].date, bps[k + 1].date)
                else:
                    # Sista perioden — sträcker sig till plan_end
                    months = max(0, _months_between(bps[k].date, plan_end_date))
                running += amt * months
                cum.append(round(running))
            return cum

        # Slice-offset för att droppa cum[0] om vi collapsat första kolumnen
        collapse_offset = 1 if collapsed else 0
        n_pre = len(breakpoints_pre_collapse)

        # Fyll tabellerna: per försäkring, beräkna efter-skatt = före * net_multiplier
        sums_before = [0] * n_dates
        sums_after = [0.0] * n_dates
        for i, ins in enumerate(insurances):
            color = INSURANCE_COLORS[i % len(INSURANCE_COLORS)]
            dot_b = _make_dot_image(color, size=12)
            dot_a = _make_dot_image(color, size=12)
            self._dot_images.extend([dot_b, dot_a])
            ins_amounts = _amounts_for(ins)
            before_values = []
            after_values = []
            for j in range(n_dates):
                amt = None
                if ins_amounts and j < len(ins_amounts):
                    amt = ins_amounts[j]
                if amt is not None:
                    sums_before[j] += amt
                    before_values.append(fmt_kr(amt))
                    if net_multipliers[j] is not None:
                        est = round(amt * net_multipliers[j])
                        sums_after[j] += est
                        after_values.append(fmt_kr(est))
                    else:
                        after_values.append('–')
                else:
                    before_values.append('–')
                    after_values.append('–')
            tag = 'row_a' if i % 2 == 0 else 'row_b'
            self.before_tree.insert(
                '', 'end', text=' ' + ins.name, image=dot_b,
                values=before_values, tags=(tag,),
            )
            self.after_tree.insert(
                '', 'end', text=' ' + ins.name, image=dot_a,
                values=after_values, tags=(tag,),
            )
            # Spara kumulativa data för tooltipen. Använd PRE-collapse data
            # så att en eventuellt droppad första kolumns utbetalning ändå
            # ingår i summan när vi slicar cum[1:] efter beräkningen.
            pre_amounts_before = ins.amounts_before_tax[
                start_idx_pre_collapse:
            ] if ins.amounts_before_tax else []
            # Padda till n_pre om kortare
            pre_amounts_before = list(pre_amounts_before) + [None] * (
                n_pre - len(pre_amounts_before)
            )
            pre_amounts_after = [
                (round(pre_amounts_before[j] * net_multipliers_pre_collapse[j])
                 if pre_amounts_before[j] is not None
                 and net_multipliers_pre_collapse[j] is not None else None)
                for j in range(n_pre)
            ]
            cum_b_pre = _compute_cumulative_for(
                pre_amounts_before, breakpoints_pre_collapse,
            )
            cum_a_pre = _compute_cumulative_for(
                pre_amounts_after, breakpoints_pre_collapse,
            )
            start_s = (
                f'{ins.start[0]}-{ins.start[1]:02d}' if ins.start else '?'
            )
            dur_s = (
                f'{ins.duration[0]} år, {ins.duration[1]} mån'
                if ins.duration else 'Livsvarigt'
            )
            self._row_data_before.append({
                'name': ins.name,
                'start': start_s,
                'dur': dur_s,
                'cum': cum_b_pre[collapse_offset:],
            })
            self._row_data_after.append({
                'name': ins.name,
                'start': start_s,
                'dur': dur_s,
                'cum': cum_a_pre[collapse_offset:],
            })

        # Summa-rad på före-tabellen
        self.before_tree.insert(
            '', 'end', text='SUMMA',
            values=[fmt_kr(s) for s in sums_before],
            tags=('summary',),
        )
        # Kumulativ för SUMMA-raden (för tooltip) — använd pre-collapse-summa
        sums_before_pre = [0] * n_pre
        for ins in insurances:
            arr = ins.amounts_before_tax[start_idx_pre_collapse:] if ins.amounts_before_tax else []
            for j in range(min(n_pre, len(arr))):
                if arr[j] is not None:
                    sums_before_pre[j] += arr[j]
        cum_sum_b_pre = _compute_cumulative_for(sums_before_pre, breakpoints_pre_collapse)
        self._row_data_before.append({
            'name': 'SUMMA (alla försäkringar)',
            'cum': cum_sum_b_pre[collapse_offset:],
        })

        # Skattesats-rad (slut på före-tabellen, syns precis ovanför efter-tabellen)
        tax_values = []
        for r in tax_rates:
            if r is None:
                tax_values.append('–')
            else:
                tax_values.append(f'{r*100:.1f}%'.replace('.', ','))
        self.before_tree.insert(
            '', 'end', text='Skattesats',
            values=tax_values, tags=('taxrate',),
        )

        # Summa-rad på efter-tabellen: faktiska after_tax-värden från planen
        # där tillgängliga, annars before × (1 - rate) för konsistens med
        # per-försäkring-estimaten
        sums_after_actual = []
        for i, bp in enumerate(breakpoints):
            if bp.after_tax is not None:
                sums_after_actual.append(bp.after_tax)
            elif bp.before_tax is not None and net_multipliers[i] is not None:
                sums_after_actual.append(round(bp.before_tax * net_multipliers[i]))
            else:
                sums_after_actual.append(None)
        self.after_tree.insert(
            '', 'end', text='SUMMA',
            values=[fmt_kr(s) if s is not None else '–' for s in sums_after_actual],
            tags=('summary',),
        )
        # Kumulativ för SUMMA-raden i efter-tabellen — också via pre-collapse
        sums_after_pre = []
        for j in range(n_pre):
            bp = breakpoints_pre_collapse[j]
            if bp.after_tax is not None:
                sums_after_pre.append(bp.after_tax)
            elif bp.before_tax is not None and net_multipliers_pre_collapse[j] is not None:
                sums_after_pre.append(round(bp.before_tax * net_multipliers_pre_collapse[j]))
            else:
                sums_after_pre.append(0)
        cum_sum_a_pre = _compute_cumulative_for(sums_after_pre, breakpoints_pre_collapse)
        self._row_data_after.append({
            'name': 'SUMMA (alla försäkringar)',
            'cum': cum_sum_a_pre[collapse_offset:],
        })

        # Bind hover till båda tabellerna för tooltips
        self.before_tree.bind(
            '<Motion>', lambda e: self._on_table_motion(e, self.before_tree, 'before'),
        )
        self.after_tree.bind(
            '<Motion>', lambda e: self._on_table_motion(e, self.after_tree, 'after'),
        )
        self.before_tree.bind(
            '<Leave>',
            lambda e: (self._hide_tip(), self._clear_income_highlight()),
        )
        self.after_tree.bind(
            '<Leave>',
            lambda e: (self._hide_tip(), self._clear_income_highlight()),
        )
        # Spara breakpoints för tooltip-formatering
        self._breakpoints = breakpoints

        # Header-text
        n_insurances = len(insurances)
        n_with_amounts = sum(1 for ins in insurances if ins.amounts_before_tax)
        self.header_label.config(
            text=f'kr/mån per försäkring vid varje brytpunktsdatum  •  '
                 f'{n_with_amounts}/{n_insurances} försäkringar har belopp',
        )


    def _draw_gantt(self, insurances):
        """Rita Gantt-graf över utbetalningsperioder per försäkring, i samma
        ordning och med samma färger som tabellerna nedan."""
        INSURANCE_COLORS, _, _ = _imports()
        ax = self.gantt_ax
        ax.clear()
        # Spara för hover-lookup
        self._gantt_insurances = list(insurances)

        if not insurances:
            ax.text(0.5, 0.5, 'Ingen data', ha='center', va='center',
                    transform=ax.transAxes, fontsize=10, color='#888')
            ax.set_xticks([])
            ax.set_yticks([])
            self.gantt_canvas.draw_idle()
            return

        # Bestäm x-axelns slut: max(birth + Räkna till ålder, sista bp + 2) så att både
        # livsvariga försäkringar (t.o.m. plan_end) och långa fasta uttag
        # ryms i grafen
        birth_year = self.tab.parsed.birth_year
        last_bp_year = (
            self.tab.parsed.breakpoints[-1].date[0]
            if self.tab.parsed.breakpoints else 2050
        )
        if birth_year:
            end_display = max(birth_year + self.app.get_end_age(), last_bp_year + 2)
        else:
            end_display = last_bp_year + 10

        n = len(insurances)
        starts = []
        for i, ins in enumerate(insurances):
            if not ins.start:
                continue
            sy, sm = ins.start
            start_frac = sy + (sm - 1) / 12.0
            starts.append(start_frac)
            if ins.duration is None:
                end_frac = end_display + 1
                arrow = True
            else:
                dy, dm = ins.duration
                end_frac = start_frac + dy + dm / 12.0
                arrow = False
            color = INSURANCE_COLORS[i % len(INSURANCE_COLORS)]
            # y = n-1-i så första försäkringen (i tabellen) hamnar överst
            y = n - 1 - i
            ax.barh(y, end_frac - start_frac, left=start_frac,
                    height=0.7, color=color, edgecolor='#333',
                    linewidth=0.5, zorder=2)
            if arrow:
                ax.annotate(
                    '→', xy=(end_frac - 0.5, y), ha='right', va='center',
                    fontsize=12, color=color,
                )

        ax.set_yticks([])
        ax.set_yticklabels([])
        ax.set_ylim(-0.6, n - 0.4)
        x_lo = (min(starts) if starts else 2027) - 0.3
        x_hi = end_display + 0.5
        ax.set_xlim(x_lo, x_hi)
        ax.set_xlabel('År')
        # Årsnätade x-ticks: 1-årsintervall om spann <= 30 år, annars 5
        span = x_hi - x_lo
        major = 1 if span <= 30 else 5
        ax.xaxis.set_major_locator(MultipleLocator(major))
        ax.grid(True, axis='x', linestyle=':', alpha=0.4)
        # Formatera tick-labels som heltal
        ax.tick_params(axis='x', labelsize=9)
        # Titel finns i ttk.Label ovan; ge staplarna hela figurhöjden.
        # left=0.01 istället för 0.04 eftersom vi inte har några y-labels
        # och användaren vill att grafen fyller rutan mer åt vänster.
        # left=0.09 för att alignas med inkomst-grafen nedan (som behöver
        # plats för 'kr'-tick-siffror). Utan alignment blir x-axlarnas
        # år-positioner förskjutna mellan de två graferna.
        self.gantt_fig.subplots_adjust(
            left=0.09, right=0.995, top=0.97, bottom=0.22,
        )
        self.gantt_canvas.draw_idle()

    def _draw_income_graph(self):
        """Rita månadsinkomst (netto) vs utgifter för aktiv plan. Data
        kommer från tab.calc.monthly som appen redan har beräknat."""
        ax = self.income_ax
        ax.clear()
        calc = getattr(self.tab, 'calc', None)
        if calc is None or not calc.monthly:
            ax.text(0.5, 0.5, 'Ingen beräknad plan-data',
                    ha='center', va='center', transform=ax.transAxes,
                    fontsize=10, color='#888')
            ax.set_xticks([])
            ax.set_yticks([])
            self.income_canvas.draw_idle()
            return

        # Alignera x-axel med Gantt: samma start/slut
        _, _, fmt_kr = _imports()
        m_x = [m.year + (m.month - 1) / 12.0 for m in calc.monthly]
        m_income = [m.after_tax or 0 for m in calc.monthly]
        m_expense = [m.utgifter or 0 for m in calc.monthly]
        ax.plot(m_x, m_income, color='#2ca02c', linewidth=1.4,
                label='Netto-inkomst')
        ax.plot(m_x, m_expense, color='#c0392b', linewidth=1.4,
                linestyle='--', label='Utgifter')
        ax.set_xlabel('År')
        ax.grid(True, axis='y', linestyle=':', alpha=0.5)

        # Vertikala streck vid varje händelse (bp-datum som visas som
        # kolumner i Före/Efter skatt-tabellerna). Filtreras på samma sätt:
        # bp:s från earliest_start och framåt.
        if self.tab.parsed and self.tab.parsed.breakpoints:
            earliest = self.tab.parsed.earliest_start
            event_dates = [
                bp.date for bp in self.tab.parsed.breakpoints
                if earliest is None or bp.date >= earliest
            ]
            first_line = True
            for (yr, mo) in event_dates:
                x = yr + (mo - 1) / 12.0
                ax.axvline(
                    x=x, color='#5a7fa5', linestyle=':', alpha=0.55,
                    linewidth=0.9,
                    label='Händelse (bp)' if first_line else None,
                )
                first_line = False
        ax.legend(loc='best', fontsize=8)

        # Formatera y-tick som kr-belopp
        from matplotlib.ticker import FuncFormatter
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: fmt_kr(v)))
        ax.tick_params(axis='both', labelsize=8)

        # Alignera x-axel med Gantt: samma x_lo/x_hi
        birth_year = self.tab.parsed.birth_year if self.tab.parsed else None
        last_bp_year = (
            self.tab.parsed.breakpoints[-1].date[0]
            if self.tab.parsed and self.tab.parsed.breakpoints else 2050
        )
        if birth_year:
            end_display = max(birth_year + self.app.get_end_age(), last_bp_year + 2)
        else:
            end_display = last_bp_year + 10
        # Använd samma starts-logik som Gantt (första insurance-starten - 0.3)
        starts = []
        for ins in (self.tab.parsed.insurances if self.tab.parsed else []):
            if ins.start:
                sy, sm = ins.start
                starts.append(sy + (sm - 1) / 12.0)
        x_lo = (min(starts) if starts else 2027) - 0.3
        x_hi = end_display + 0.5
        ax.set_xlim(x_lo, x_hi)
        span = x_hi - x_lo
        major = 1 if span <= 30 else 5
        ax.xaxis.set_major_locator(MultipleLocator(major))

        # Samma marginaler som Gantt så x-axlar och rutbredder alignas
        self.income_fig.subplots_adjust(
            left=0.09, right=0.995, top=0.95, bottom=0.28,
        )
        self.income_canvas.draw_idle()

    def _on_gantt_motion(self, event):
        """Visa en popup med år-månad, ålder och (om musen är över en
        stapel) försäkringens namn + start + längd, likt Planera manuellt-
        fönstret."""
        if event.inaxes != self.gantt_ax or event.xdata is None:
            self._hide_gantt_tip()
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
        birth_year = self.tab.parsed.birth_year if self.tab.parsed else None
        birth_month = self.tab.parsed.birth_month if self.tab.parsed else None
        if birth_year:
            bm = birth_month or 1
            total_m = (yr - birth_year) * 12 + (month - bm)
            if total_m >= 0:
                y_old = total_m // 12
                m_old = total_m % 12
                if m_old == 0:
                    age_txt = f'\nÅlder: {y_old} år'
                else:
                    age_txt = f'\nÅlder: {y_old} år, {m_old} mån'
        # Hitta försäkring under muspekaren via ydata
        ins_txt = ''
        status_txt = ''
        ins = self._insurance_from_gantt_y(event.ydata)
        if ins is not None:
            start_s = (
                f'{ins.start[0]}-{ins.start[1]:02d}' if ins.start else '?'
            )
            if ins.duration is None:
                dur_s = 'Livet ut'
            else:
                dy, dm = ins.duration
                dur_s = f'{dy} år'
                if dm:
                    dur_s += f' {dm} mån'
            ins_txt = f'\n{ins.name}\nStart: {start_s}   Längd: {dur_s}'
            # Status vid muspekar-datumet
            status = self._insurance_status_at(ins, yr, month)
            if status:
                status_txt = f'  ·  {status}'
        text = f'{yr}-{month:02d}{status_txt}{age_txt}{ins_txt}'
        # Positionera popup vid muspekaren
        try:
            x_root = event.guiEvent.x_root
            y_root = event.guiEvent.y_root
        except AttributeError:
            return
        self._show_gantt_tip(text, x_root + 14, y_root + 14)

    def _highlight_income_period(self, col_idx):
        """Rita en ljusgrå axvspan i BÅDE månadsinkomst- och Gantt-grafen
        som markerar perioden för brytpunkts-kolumnen col_idx (från
        breakpoints[col_idx] till nästa bp eller plan_end). Idempotent —
        samma col_idx renderar inte om."""
        if col_idx == self._highlighted_col_idx:
            return
        if not hasattr(self, '_breakpoints') or col_idx < 0 or \
                col_idx >= len(self._breakpoints):
            return
        # Ta bort ev. tidigare spans
        for attr in ('_income_span_patch', '_gantt_span_patch'):
            patch = getattr(self, attr, None)
            if patch is not None:
                try:
                    patch.remove()
                except (ValueError, AttributeError):
                    pass
                setattr(self, attr, None)
        # Beräkna periodens x-range
        bp = self._breakpoints[col_idx]
        x_lo = bp.date[0] + (bp.date[1] - 1) / 12.0
        if col_idx + 1 < len(self._breakpoints):
            end = self._breakpoints[col_idx + 1].date
        else:
            end = getattr(self, '_plan_end_date', None) or (bp.date[0] + 30, 12)
        x_hi = end[0] + (end[1] - 1) / 12.0
        # Rita spans BAKOM linjer/staplar via zorder=0 (bar-patches default 1)
        self._income_span_patch = self.income_ax.axvspan(
            x_lo, x_hi, color='#c8c8c8', alpha=0.45, zorder=0,
        )
        self._gantt_span_patch = self.gantt_ax.axvspan(
            x_lo, x_hi, color='#c8c8c8', alpha=0.45, zorder=0,
        )
        self._highlighted_col_idx = col_idx
        self.income_canvas.draw_idle()
        self.gantt_canvas.draw_idle()

    def _clear_income_highlight(self):
        if (self._income_span_patch is None
                and self._gantt_span_patch is None):
            self._highlighted_col_idx = None
            return
        for attr in ('_income_span_patch', '_gantt_span_patch'):
            patch = getattr(self, attr, None)
            if patch is not None:
                try:
                    patch.remove()
                except (ValueError, AttributeError):
                    pass
                setattr(self, attr, None)
        self._highlighted_col_idx = None
        self.income_canvas.draw_idle()
        self.gantt_canvas.draw_idle()

    def _on_income_motion(self, event):
        """Popup vid hover över månadsinkomst-grafen: år-månad + ålder +
        netto/utgifter för hovrad månad + diff. Delar Toplevel med Gantt."""
        if event.inaxes != self.income_ax or event.xdata is None:
            self._hide_gantt_tip()
            return
        _, _, fmt_kr = _imports()
        yr = int(event.xdata)
        frac = event.xdata - yr
        month = int(frac * 12) + 1
        if month < 1:
            month = 1
        if month > 12:
            month = 12
        # Ålder
        age_txt = ''
        birth_year = self.tab.parsed.birth_year if self.tab.parsed else None
        birth_month = self.tab.parsed.birth_month if self.tab.parsed else None
        if birth_year:
            bm = birth_month or 1
            total_m = (yr - birth_year) * 12 + (month - bm)
            if total_m >= 0:
                y_old = total_m // 12
                m_old = total_m % 12
                if m_old == 0:
                    age_txt = f'\nÅlder: {y_old} år'
                else:
                    age_txt = f'\nÅlder: {y_old} år, {m_old} mån'
        # Slå upp motsvarande månad i tab.calc.monthly
        detail_txt = ''
        calc = getattr(self.tab, 'calc', None)
        if calc and calc.monthly:
            mrow = next(
                (m for m in calc.monthly if m.year == yr and m.month == month),
                None,
            )
            if mrow is not None:
                net = mrow.after_tax or 0
                utg = mrow.utgifter or 0
                diff = net - utg
                sign = '+' if diff >= 0 else '−'
                detail_txt = (
                    f'\nNetto: {fmt_kr(net)} kr/mån'
                    f'\nUtgifter: {fmt_kr(utg)} kr/mån'
                    f'\nDiff: {sign}{fmt_kr(abs(diff))} kr/mån'
                )
        text = f'{yr}-{month:02d}{age_txt}{detail_txt}'
        try:
            x_root = event.guiEvent.x_root
            y_root = event.guiEvent.y_root
        except AttributeError:
            return
        self._show_gantt_tip(text, x_root + 14, y_root + 14)

    def _insurance_status_at(self, ins, year, month):
        """Returnerar 'Ej startad', 'Pågående' eller 'Avslutad' beroende på
        var muspekar-datumet (year, month) ligger relativt försäkringens
        start och längd. Livet ut = alltid Pågående efter start."""
        if not ins.start:
            return ''
        cur_m = year * 12 + (month - 1)
        sy, sm = ins.start
        start_m = sy * 12 + (sm - 1)
        if cur_m < start_m:
            return 'Ej startad'
        if ins.duration is None:
            return 'Pågående'
        dy, dm = ins.duration
        end_m = start_m + dy * 12 + dm  # exklusivt: sista utbetalda månad = end_m - 1
        if cur_m < end_m:
            return 'Pågående'
        return 'Avslutad'

    def _insurance_from_gantt_y(self, ydata):
        """Mappa event.ydata till en Insurance. Staplarna ritas med
        y = n-1-i, höjd 0.7 (±0.35). Returnerar None om utanför stapel."""
        if ydata is None or not self._gantt_insurances:
            return None
        n = len(self._gantt_insurances)
        # Rundning till närmaste heltal ger radindex; kolla att vi är inom
        # stapelhöjden (±0.35 från mittlinjen)
        row = round(ydata)
        if abs(ydata - row) > 0.35:
            return None
        if row < 0 or row >= n:
            return None
        i = n - 1 - row
        return self._gantt_insurances[i]

    def _show_gantt_tip(self, text, x, y):
        tw = self._gantt_tip
        if tw is None:
            tw = tk.Toplevel(self.top)
            tw.wm_overrideredirect(True)
            tw.configure(bg='#ffffe0', bd=1, relief='solid')
            lbl = ttk.Label(
                tw, text='', background='#ffffe0',
                font=('TkDefaultFont', 9), padding=(6, 3),
                justify='left',
            )
            lbl.pack()
            tw._label = lbl
            self._gantt_tip = tw
        try:
            tw._label.config(text=text)
            tw.geometry(f'+{x}+{y}')
            tw.deiconify()
            tw.lift()
        except tk.TclError:
            self._gantt_tip = None

    def _hide_gantt_tip(self):
        tw = self._gantt_tip
        if tw is not None:
            try:
                tw.withdraw()
            except tk.TclError:
                self._gantt_tip = None

    def _on_table_motion(self, event, tree, kind):
        """Visa tooltip: uttagsstart/längd på försäkrings-kolumnen (#0),
        kumulativ utbetalning på belopps-cellerna."""
        _, _, fmt_kr = _imports()
        region = tree.identify_region(event.x, event.y)
        if region not in ('cell', 'tree'):
            self._hide_tip()
            self._clear_income_highlight()
            return
        row_id = tree.identify_row(event.y)
        if not row_id:
            self._hide_tip()
            self._clear_income_highlight()
            return
        col_str = tree.identify_column(event.x)  # t.ex. '#0', '#1', '#2'
        if not col_str.startswith('#'):
            self._hide_tip()
            self._clear_income_highlight()
            return

        # Hitta radens position i tree → motsvarar index i row_data
        children = tree.get_children()
        try:
            row_idx = children.index(row_id)
        except ValueError:
            self._hide_tip()
            self._clear_income_highlight()
            return

        row_data = (
            self._row_data_before if kind == 'before' else self._row_data_after
        )
        if row_idx >= len(row_data):
            # Skattesats-raden eller annan extra rad — ingen tooltip
            self._hide_tip()
            self._clear_income_highlight()
            return

        # Försäkrings-kolumnen: visa uttagsstart + uttagslängd
        if col_str == '#0':
            # Ingen period-cell → ta bort ev. ljusgrå markering
            self._clear_income_highlight()
            key = (id(tree), row_id, -1)
            if key == self._tip_key:
                return
            self._tip_key = key
            data = row_data[row_idx]
            if 'start' in data and 'dur' in data:
                text = (
                    f'{data["name"]}\n'
                    f'Uttagsstart: {data["start"]}\n'
                    f'Uttagslängd: {data["dur"]}'
                )
            else:
                # SUMMA-raden — bara namnet
                text = data['name']
            self._show_tip(event, text)
            return

        # Belopps-celler: kumulativ utbetalning
        try:
            col_idx = int(col_str[1:]) - 1
        except ValueError:
            self._hide_tip()
            self._clear_income_highlight()
            return
        if col_idx < 0 or col_idx >= len(self._breakpoints):
            self._hide_tip()
            self._clear_income_highlight()
            return

        # Ljusgrå bakgrund i månadsinkomst-grafen för denna period
        self._highlight_income_period(col_idx)

        key = (id(tree), row_id, col_idx)
        if key == self._tip_key:
            return  # samma cell, behåll tooltip
        self._tip_key = key

        data = row_data[row_idx]
        cum_kr = data['cum'][col_idx]
        bp = self._breakpoints[col_idx]
        date_str = f'{bp.date[0]}-{bp.date[1]:02d}'
        # Beskriv perioden från d_k till d_{k+1} eller till planens slut
        if col_idx < len(self._breakpoints) - 1:
            next_bp = self._breakpoints[col_idx + 1]
            period_end_str = f'{next_bp.date[0]}-{next_bp.date[1]:02d}'
            period_note = f'period: {date_str} → {period_end_str}'
        else:
            end = self._plan_end_date
            period_end_str = f'{end[0]}-{end[1]:02d}'
            end_age = self.app.get_end_age()
            if tuple(bp.date) > tuple(end):
                # "Räkna till ålder" är satt lägre än sista brytpunkten
                period_note = (
                    f'period: från {date_str} '
                    f'(efter planens slut {period_end_str}, ålder {end_age})'
                )
            else:
                period_note = (
                    f'period: {date_str} → {period_end_str} '
                    f'(planens slut, ålder {end_age})'
                )
        tax_label = 'före skatt' if kind == 'before' else 'efter skatt (estimat)'
        text = (
            f'{data["name"]}\n'
            f'{period_note}\n'
            f'Hittills utbetalt t.o.m. periodens slut ({tax_label}):\n'
            f'{fmt_kr(cum_kr)} kr'
        )
        self._show_tip(event, text)

    def _show_tip(self, event, text: str):
        self._destroy_tip()
        tw = tk.Toplevel(self.top)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f'+{event.x_root + 14}+{event.y_root + 10}')
        tw.configure(bg='#ffffe0', highlightbackground='#888', highlightthickness=1)
        tk.Label(
            tw, text=text, bg='#ffffe0',
            font=('TkDefaultFont', 9), justify='left',
            padx=6, pady=4,
        ).pack()
        self._tip = tw

    def _hide_tip(self):
        self._destroy_tip()
        self._tip_key = None

    def _destroy_tip(self):
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None

    def _on_close(self):
        self._destroy_tip()
        # Riv Gantt-tooltip
        if self._gantt_tip is not None:
            try:
                self._gantt_tip.destroy()
            except tk.TclError:
                pass
            self._gantt_tip = None
        # Spara geometri inför nästa öppning
        try:
            self.app._insurance_income_window_geometry = self.top.geometry()
        except tk.TclError:
            pass
        # Persistera direkt till state-filen (precis som CompareWindow)
        try:
            self.app._remember_state()
        except Exception:
            pass
        try:
            self.top.destroy()
        except tk.TclError:
            pass
        self.app._insurance_income_window = None
