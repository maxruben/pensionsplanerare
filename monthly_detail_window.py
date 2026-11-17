"""Toplevel-fönster: 'Månadsvis detalj' för den aktiva planen.

Tabellen låg tidigare i mitten av varje plan-flik. Nu finns ett enda fönster
som följer aktiv flik: huvudappen anropar `refresh_for_tab` vid flikbyte och
efter varje omräkning. Data hämtas från `tab.calc.monthly`.
"""

import tkinter as tk
from tkinter import ttk


# Late binding — undvik cirkulär import med pension_planner
def _imports():
    from pension_planner import _auto_hide_scrollbar, fmt_kr
    return _auto_hide_scrollbar, fmt_kr


class MonthlyDetailWindow:
    COLS = ('year', 'month', 'age', 'fore', 'efter', 'utgifter', 'osk_man', 'osk_ar')
    LABELS = ('År', 'Månad', 'Ålder', 'Före skatt', 'Efter skatt', 'Utgifter',
              'Diff/mån', 'Diff/år')
    WIDTHS = (45, 45, 40, 70, 70, 60, 65, 70)

    def __init__(self, app, tab):
        self.app = app
        self.tab = tab
        self.top = tk.Toplevel(app.root)
        self.top.protocol('WM_DELETE_WINDOW', self._on_close)
        self._apply_initial_geometry()
        self._build_ui()
        self.refresh_for_tab(tab)

    def _apply_initial_geometry(self):
        """Återställ senast använd geometri, annars centrera på huvudfönstret."""
        saved = getattr(self.app, '_monthly_window_geometry', None)
        if isinstance(saved, str) and 'x' in saved:
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
        cw, ch = 540, 700
        x = main_x + max(0, (main_w - cw) // 2)
        y = main_y + max(0, (main_h - ch) // 2)
        self.top.geometry(f'{cw}x{ch}+{x}+{y}')

    def _build_ui(self):
        _auto_hide_scrollbar, _ = _imports()
        outer = ttk.Frame(self.top, padding=8)
        outer.pack(fill='both', expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)

        self.tree = ttk.Treeview(
            outer, columns=self.COLS, show='headings', height=20,
        )
        for c, label, w in zip(self.COLS, self.LABELS, self.WIDTHS):
            self.tree.heading(c, text=label)
            anchor = 'center' if c in ('year', 'month', 'age') else 'e'
            self.tree.column(c, width=w, minwidth=w, anchor=anchor, stretch=False)
        ysb = ttk.Scrollbar(outer, orient='vertical', command=self.tree.yview)
        xsb = ttk.Scrollbar(outer, orient='horizontal', command=self.tree.xview)
        self.tree.configure(
            yscrollcommand=ysb.set,
            xscrollcommand=_auto_hide_scrollbar(xsb),
        )
        # Zebra-rand per år (12 rader per band)
        self.tree.tag_configure('row_a', background='#ffffff')
        self.tree.tag_configure('row_b', background='#f0f0f0')
        self.tree.grid(row=0, column=0, sticky='nsew')
        ysb.grid(row=0, column=1, sticky='ns')
        xsb.grid(row=1, column=0, sticky='ew')
        # Tooltip med aktiva försäkringar för raden under muspekaren —
        # ritas av fliken (den äger plan-datan och tooltip-koden)
        self.tree.bind(
            '<Motion>',
            lambda e: self.tab._on_monthly_row_motion(e, self.tree),
        )
        self.tree.bind('<Leave>', lambda e: self.tab._hide_table_tooltip())

    def _top_visible_month(self):
        """(år, månad) för översta synliga raden, eller None om tabellen är tom."""
        children = self.tree.get_children()
        if not children:
            return None
        first_frac = self.tree.yview()[0]
        idx = max(0, min(len(children) - 1, int(first_frac * len(children))))
        values = self.tree.item(children[idx], 'values')
        try:
            return int(values[0]), int(values[1])
        except (ValueError, TypeError, IndexError):
            return None

    def refresh_for_tab(self, tab):
        """Visa `tab`s månadsdata. Behåller scroll-läget (samma år/månad
        överst) så det går att bläddra mellan planer och jämföra."""
        _, fmt_kr = _imports()
        keep = self._top_visible_month()
        try:
            self.tab._hide_table_tooltip()
        except (AttributeError, tk.TclError):
            pass
        self.tab = tab
        plan_name = (
            tab.parsed.plan_name if tab.parsed and tab.parsed.plan_name
            else '<ingen plan>'
        )
        self.top.title(f'Månadsvis detalj — Plan: {plan_name}')

        self.tree.delete(*self.tree.get_children())
        monthly = tab.calc.monthly if tab.calc else []
        target_iid = None
        for m in monthly:
            # Samma år får alltid samma bakgrundsfärg
            tag = 'row_a' if m.year % 2 == 0 else 'row_b'
            iid = self.tree.insert('', 'end', values=(
                m.year, m.month, m.age,
                fmt_kr(m.before_tax),
                fmt_kr(m.after_tax),
                fmt_kr(m.utgifter),
                fmt_kr(m.overskott_man),
                fmt_kr(m.overskott_ar),
            ), tags=(tag,))
            if keep and target_iid is None and (m.year, m.month) >= keep:
                target_iid = iid
        if target_iid is not None:
            children = self.tree.get_children()
            self.tree.yview_moveto(children.index(target_iid) / len(children))

    def _on_close(self):
        try:
            self.tab._hide_table_tooltip()
        except (AttributeError, tk.TclError):
            pass
        # Spara geometri inför nästa öppning och skriv den till state-filen
        try:
            self.app._monthly_window_geometry = self.top.geometry()
        except tk.TclError:
            pass
        try:
            self.app._remember_state()
        except Exception:
            pass
        try:
            self.top.destroy()
        except tk.TclError:
            pass
        self.app._monthly_window = None
