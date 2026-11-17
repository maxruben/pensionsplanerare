"""Utgifts-detaljer: fönster där utgiftsposter i kategorier redigeras.

Modell:
- expense_categories: lista av {'name': str, 'items': [expense_item, ...]}
- expense_item:       {'name': str, 'period': 'månad'|'år',
                        'amounts': [int, int, int, int, int, int]}

Amounts har alltid 6 värden — ett per åldersspann. Period gäller alla 6.
Kategorier och utgifter kan läggas till/tas bort. Datan sparas i state-filen
via appens _remember_state.

Denna första version är ett fristående verktyg — Set 1-integrationen görs
i nästa steg.
"""

import json
import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# Sen import — pension_planner refererar tillbaka till detta modul
def _center_dialog_on_parent(dialog, parent):
    from pension_planner import center_dialog_on_parent
    center_dialog_on_parent(dialog, parent)


NUM_SPANS = 6


def default_categories():
    """Standard-mall — kan tas bort/ändras direkt i UI:t."""
    return [
        {
            'name': 'Boende',
            'items': [
                _empty_item('Hyra/lån', period='månad'),
                _empty_item('El', period='månad'),
                _empty_item('Bredband/mobil', period='månad'),
                _empty_item('Hemförsäkring', period='år'),
            ],
        },
        {
            'name': 'Mat',
            'items': [
                _empty_item('Mat hemma', period='månad'),
                _empty_item('Restaurang', period='månad'),
            ],
        },
        {
            'name': 'Transport',
            'items': [
                _empty_item('Bränsle', period='månad'),
                _empty_item('Bilservice', period='år'),
                _empty_item('Bilförsäkring', period='år'),
            ],
        },
    ]


def _empty_item(name: str, period: str = 'månad'):
    return {'name': name, 'period': period, 'amounts': [0] * NUM_SPANS}


class ExpenseWindow:
    """Toplevel-fönster som visar utgiftskategorier med utgifter per åldersspann."""

    def __init__(self, app):
        self.app = app
        self.top = tk.Toplevel(app.root)
        self.top.title('Utgifts-detaljer')
        self.top.protocol('WM_DELETE_WINDOW', self._on_close)
        self._apply_initial_geometry()

        # Ladda categorier från app-state, annars default
        self.categories = self._load_categories()

        self._build_ui()
        self._render_tree()

    # ---- Geometry ----

    def _apply_initial_geometry(self):
        saved = getattr(self.app, '_expense_window_geometry', None)
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
        cw, ch = 1000, 620
        x = main_x + max(0, (main_w - cw) // 2)
        y = main_y + max(0, (main_h - ch) // 2)
        self.top.geometry(f'{cw}x{ch}+{x}+{y}')

    # ---- State ----

    def _load_categories(self):
        cats = getattr(self.app, '_expense_categories', None)
        if not cats:
            # Första gången: om det finns värden i Set 1, seed en "Övrigt →
            # Total"-post med dem så vi inte råkar skriva över dem när
            # användaren aktiverar utgifts-modellen.
            try:
                set1 = [
                    int(self.app.cost_sets[0][j].get() or '0')
                    for j in range(min(NUM_SPANS, len(self.app.age_vars)))
                ]
            except (AttributeError, ValueError, IndexError):
                set1 = []
            padded = (set1 + [0] * NUM_SPANS)[:NUM_SPANS]
            if any(padded):
                return [{
                    'name': 'Övrigt',
                    'items': [{
                        'name': 'Total (specificera per rad)',
                        'period': 'månad',
                        'amounts': padded,
                    }],
                }]
            return default_categories()
        # Djup-kopia för att undvika oavsiktlig share
        out = []
        for c in cats:
            items = []
            for it in c.get('items', []):
                amts = list(it.get('amounts') or [])
                # Padda/klipp till exakt NUM_SPANS
                if len(amts) < NUM_SPANS:
                    amts = amts + [0] * (NUM_SPANS - len(amts))
                else:
                    amts = amts[:NUM_SPANS]
                items.append({
                    'name': str(it.get('name', '')),
                    'period': str(it.get('period', 'månad')),
                    'amounts': [int(a or 0) for a in amts],
                })
            out.append({'name': str(c.get('name', '')), 'items': items})
        return out

    def _save_categories(self):
        self.app._expense_categories = self.categories
        # Sync till Set 1 + lås entries + trigger recalc
        try:
            self.app._on_expenses_changed()
        except AttributeError:
            self.app._mark_settings_dirty()

    def reload_from_app(self):
        """Ladda om från app-state — anropas när användaren öppnat annat dokument."""
        self.categories = self._load_categories()
        self._render_tree()

    # ---- UI ----

    def _build_ui(self):
        outer = ttk.Frame(self.top, padding=8)
        outer.pack(fill='both', expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)

        # Info-rad överst — rubriker + period-info
        info = ttk.Label(
            outer,
            text=(
                'Utgifter grupperade i kategorier. Varje utgift har 6 åldersspann '
                'och en period (månad/år). Åldersgränserna hämtas från '
                'Utgiftsbrytpunkter på huvudsidan.'
            ),
            font=('TkDefaultFont', 9), foreground='#555',
            wraplength=980, justify='left',
        )
        info.grid(row=0, column=0, sticky='w', pady=(0, 6))

        # Tree med kategorier och utgifter
        tree_frame = ttk.Frame(outer)
        tree_frame.grid(row=1, column=0, sticky='nsew')
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)

        cols = ('period',) + tuple(f's{i}' for i in range(NUM_SPANS))
        self.tree = ttk.Treeview(
            tree_frame, columns=cols, show='tree headings',
        )
        self.tree.heading('#0', text='Kategori / Utgift')
        self.tree.column('#0', width=260, minwidth=180, anchor='w', stretch=False)
        self.tree.heading('period', text='Period')
        self.tree.column('period', width=70, minwidth=60, anchor='center', stretch=False)
        span_labels = self._get_span_labels()
        for i in range(NUM_SPANS):
            self.tree.heading(f's{i}', text=span_labels[i])
            self.tree.column(f's{i}', width=140, minwidth=90, anchor='e', stretch=False)
        self.tree.tag_configure('category', font=('TkDefaultFont', 10, 'bold'),
                                background='#e0e8f0')
        self.tree.tag_configure('summary', font=('TkDefaultFont', 10, 'bold'),
                                background='#d0e0d0')
        self.tree.tag_configure('row_a', background='#ffffff')
        self.tree.tag_configure('row_b', background='#f5f5f5')

        vsb = ttk.Scrollbar(tree_frame, orient='vertical', command=self.tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient='horizontal', command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky='nsew')
        vsb.grid(row=0, column=1, sticky='ns')
        hsb.grid(row=1, column=0, sticky='ew')

        # Dubbelklick / Enter = redigera
        self.tree.bind('<Double-Button-1>', self._on_edit_selected)
        self.tree.bind('<Return>', self._on_edit_selected)
        # Högerklick = kontextmeny
        self.tree.bind('<Button-3>', self._on_tree_right_click)
        self._context_menu = tk.Menu(self.top, tearoff=False)
        # Drag-och-släpp för omordning av kategorier/utgifter
        self._drag_ref = None      # ('category', ci) eller ('item', ci, ii)
        self._drag_start_y = None  # för att skilja klick från drag
        self._drag_started = False
        self.tree.bind('<ButtonPress-1>', self._on_drag_press, add='+')
        self.tree.bind('<B1-Motion>', self._on_drag_motion, add='+')
        self.tree.bind('<ButtonRelease-1>', self._on_drag_release, add='+')

        # Knappar
        btn_row = ttk.Frame(outer)
        btn_row.grid(row=2, column=0, sticky='ew', pady=(8, 0))
        ttk.Button(
            btn_row, text='Ny kategori...', command=self._add_category,
        ).pack(side='left')
        ttk.Button(
            btn_row, text='Ny utgift...', command=self._add_item,
        ).pack(side='left', padx=(6, 0))
        ttk.Button(
            btn_row, text='Redigera...', command=self._on_edit_selected,
        ).pack(side='left', padx=(6, 0))
        ttk.Button(
            btn_row, text='Ta bort', command=self._delete_selected,
        ).pack(side='left', padx=(6, 0))
        ttk.Button(
            btn_row, text='Importera från fil...', command=self._import_from_file,
        ).pack(side='left', padx=(20, 0))
        ttk.Button(
            btn_row, text='Stäng', command=self._on_close,
        ).pack(side='right')

    def _get_span_labels(self):
        """Läs age_vars från appen och bygg 'T.o.m. N år'-etiketter."""
        labels = []
        prev = None
        for i in range(NUM_SPANS):
            try:
                age = int(self.app.age_vars[i].get())
            except (ValueError, IndexError, AttributeError):
                age = None
            if age is None:
                labels.append(f'Span {i + 1}')
            elif prev is None:
                labels.append(f'– {age} år')
            else:
                labels.append(f'{prev + 1}–{age} år')
            prev = age
        return labels

    def _render_tree(self):
        self.tree.delete(*self.tree.get_children())
        self._iid_to_ref = {}  # iid → ('category', ci) eller ('item', ci, ii)

        # Beräkna totala månadsbeloppet per span (används för summa-rad)
        totals = [0] * NUM_SPANS

        for ci, cat in enumerate(self.categories):
            # Räkna kategori-summan (kr/mån) per åldersspann i förväg
            cat_totals = [0.0] * NUM_SPANS
            for item in cat['items']:
                amounts = item['amounts']
                period = item.get('period', 'månad')
                for j in range(NUM_SPANS):
                    a = amounts[j] or 0
                    cat_totals[j] += a / 12.0 if period == 'år' else a
            cat_vals = ('mån',) + tuple(
                _fmt_kr(int(round(v))) if v else '' for v in cat_totals
            )
            cat_iid = self.tree.insert(
                '', 'end', text=cat['name'],
                values=cat_vals,
                tags=('category',), open=True,
            )
            self._iid_to_ref[cat_iid] = ('category', ci)

            for ii, item in enumerate(cat['items']):
                amounts = item['amounts']
                period = item.get('period', 'månad')
                if period == 'år':
                    def _fmt_year(a):
                        if not a:
                            return ''
                        return f'{_fmt_kr(a)} ({_fmt_kr(int(round(a / 12)))}/mån)'
                    vals = (period,) + tuple(
                        _fmt_year(amounts[j]) for j in range(NUM_SPANS)
                    )
                else:
                    vals = (period,) + tuple(
                        _fmt_kr(amounts[j]) for j in range(NUM_SPANS)
                    )
                tag = 'row_a' if ii % 2 == 0 else 'row_b'
                item_iid = self.tree.insert(
                    cat_iid, 'end', text='   ' + item['name'],
                    values=vals, tags=(tag,),
                )
                self._iid_to_ref[item_iid] = ('item', ci, ii)
            # Totalt = summan av alla kategorier (per span)
            for j in range(NUM_SPANS):
                totals[j] += cat_totals[j]

        # Summa-rad (månadsbelopp)
        summa_vals = ('mån',) + tuple(
            _fmt_kr(int(round(v))) for v in totals
        )
        self.tree.insert(
            '', 'end', text='SUMMA (kr/mån)',
            values=summa_vals, tags=('summary',),
        )

    # ---- Actions ----

    def _add_category(self):
        name = _prompt_string(
            self.top, 'Ny kategori', 'Namn på kategori:',
        )
        if not name:
            return
        self.categories.append({'name': name, 'items': []})
        self._save_categories()
        self._render_tree()

    def _on_drag_press(self, event):
        """Start av eventuell drag — kom ihåg referensen."""
        row_id = self.tree.identify_row(event.y)
        if not row_id:
            self._drag_ref = None
            return
        ref = self._iid_to_ref.get(row_id)
        # SUMMA-raden och andra saker utan ref → ingen drag
        if ref is None:
            self._drag_ref = None
            return
        self._drag_ref = ref
        self._drag_start_y = event.y
        self._drag_started = False

    def _on_drag_motion(self, event):
        """Visa dra-cursor så snart användaren flyttat >4 px."""
        if self._drag_ref is None or self._drag_start_y is None:
            return
        if not self._drag_started and abs(event.y - self._drag_start_y) > 4:
            self._drag_started = True
            try:
                self.tree.configure(cursor='hand2')
            except tk.TclError:
                pass
        if self._drag_started:
            # Visa target-highlight via selection
            row_id = self.tree.identify_row(event.y)
            if row_id:
                self.tree.selection_set(row_id)

    def _on_drag_release(self, event):
        """Utför flytten om drag faktiskt startades."""
        try:
            self.tree.configure(cursor='')
        except tk.TclError:
            pass
        started = self._drag_started
        ref = self._drag_ref
        self._drag_ref = None
        self._drag_start_y = None
        self._drag_started = False
        if not started or ref is None:
            return
        target_row = self.tree.identify_row(event.y)
        if not target_row:
            return
        target_ref = self._iid_to_ref.get(target_row)
        if target_ref is None:
            return  # Släppt på SUMMA-raden e.d.
        if ref == target_ref:
            return  # Släppt på sig själv

        moved = False
        if ref[0] == 'category':
            src_ci = ref[1]
            # Målindex: framför targets kategori (om target är utgift → dess parent)
            if target_ref[0] == 'category':
                dst_ci = target_ref[1]
            else:
                dst_ci = target_ref[1]
            cat = self.categories.pop(src_ci)
            # Justera index om vi pop:at innan target-positionen
            if src_ci < dst_ci:
                dst_ci -= 1
            self.categories.insert(dst_ci, cat)
            moved = True
        else:
            # Flytta utgift: målkategori och målposition beror på target-typ
            _, src_ci, src_ii = ref
            item = self.categories[src_ci]['items'].pop(src_ii)
            if target_ref[0] == 'item':
                _, dst_ci, dst_ii = target_ref
                # Om samma kategori och pop:at innan target → justera
                if dst_ci == src_ci and src_ii < dst_ii:
                    dst_ii -= 1
                self.categories[dst_ci]['items'].insert(dst_ii, item)
            else:
                # Släppt på en kategori → läggs sist i den
                dst_ci = target_ref[1]
                self.categories[dst_ci]['items'].append(item)
            moved = True

        if moved:
            self._save_categories()
            self._render_tree()

    def _on_tree_right_click(self, event):
        """Visa kontextmeny anpassad efter vad som är under muspekaren."""
        row_id = self.tree.identify_row(event.y)
        if row_id:
            self.tree.selection_set(row_id)
            self.tree.focus(row_id)
        ref = self._iid_to_ref.get(row_id)
        menu = self._context_menu
        menu.delete(0, 'end')
        if ref and ref[0] == 'category':
            ci = ref[1]
            menu.add_command(
                label='Ny utgift i denna kategori...',
                command=lambda c=ci: self._add_item_to_category(c),
            )
            menu.add_separator()
            menu.add_command(
                label='Byt namn på kategori...', command=self._on_edit_selected,
            )
            menu.add_command(
                label='Ta bort kategori...', command=self._delete_selected,
            )
            menu.add_separator()
            menu.add_command(label='Ny kategori...', command=self._add_category)
        elif ref and ref[0] == 'item':
            menu.add_command(label='Redigera utgift...', command=self._on_edit_selected)
            menu.add_command(label='Ta bort utgift...', command=self._delete_selected)
            menu.add_separator()
            _, ci, _ = ref
            menu.add_command(
                label='Ny utgift i samma kategori...',
                command=lambda c=ci: self._add_item_to_category(c),
            )
            menu.add_command(label='Ny kategori...', command=self._add_category)
        else:
            menu.add_command(label='Ny kategori...', command=self._add_category)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _add_item_to_category(self, ci: int):
        """Lägg till ny utgift i angiven kategori-index."""
        if ci < 0 or ci >= len(self.categories):
            return
        result = _prompt_item(self.top, 'Ny utgift', None, self._get_span_labels())
        if result is None:
            return
        self.categories[ci]['items'].append(result)
        self._save_categories()
        self._render_tree()

    def _add_item(self):
        # Kräv att en kategori (eller ett item i en kategori) är vald
        sel = self.tree.selection()
        target_ci = None
        if sel:
            ref = self._iid_to_ref.get(sel[0])
            if ref:
                target_ci = ref[1]
        if target_ci is None:
            if not self.categories:
                messagebox.showinfo(
                    'Ingen kategori', 'Skapa en kategori först.',
                    parent=self.top,
                )
                return
            # Om ingen vald — välj första
            target_ci = 0

        result = _prompt_item(self.top, 'Ny utgift', None, self._get_span_labels())
        if result is None:
            return
        self.categories[target_ci]['items'].append(result)
        self._save_categories()
        self._render_tree()

    def _on_edit_selected(self, event=None):
        sel = self.tree.selection()
        if not sel:
            return
        ref = self._iid_to_ref.get(sel[0])
        if not ref:
            return
        if ref[0] == 'category':
            ci = ref[1]
            new_name = _prompt_string(
                self.top, 'Byt namn på kategori',
                'Nytt namn:', initial=self.categories[ci]['name'],
            )
            if new_name is None:
                return
            self.categories[ci]['name'] = new_name
        else:
            _, ci, ii = ref
            result = _prompt_item(
                self.top, 'Redigera utgift', self.categories[ci]['items'][ii],
                self._get_span_labels(),
            )
            if result is None:
                return
            self.categories[ci]['items'][ii] = result
        self._save_categories()
        self._render_tree()

    def _import_from_file(self):
        """Ladda utgiftskategorier från ett annat plan-dokument (.json)."""
        initial_dir = getattr(self.app, '_last_save_dir', None) or os.path.expanduser('~')
        filename = filedialog.askopenfilename(
            title='Importera utgifter från plan-dokument',
            initialdir=initial_dir,
            filetypes=[('Plan-JSON', '*.json'), ('Alla filer', '*.*')],
            parent=self.top,
        )
        if not filename:
            return
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception as e:
            messagebox.showerror(
                'Fel vid läsning', f'Kunde inte läsa filen:\n{e}',
                parent=self.top,
            )
            return
        settings = data.get('settings') or {}
        imported = settings.get('expense_categories')
        if not isinstance(imported, list) or not imported:
            messagebox.showinfo(
                'Inga utgifter',
                'Den valda filen innehåller inga sparade utgiftskategorier.',
                parent=self.top,
            )
            return

        n_cats = len(imported)
        n_items = sum(len(c.get('items') or []) for c in imported)
        mode = _prompt_import_mode(self.top, n_cats, n_items)
        if mode is None:
            return
        if mode == 'replace':
            self.categories = imported
        else:  # append
            self.categories = list(self.categories) + imported
        # Kör samma normalisering som vid load så amounts har rätt längd
        self.app._expense_categories = self.categories
        self.categories = self._load_categories()
        self._save_categories()
        self._render_tree()

    def _delete_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        ref = self._iid_to_ref.get(sel[0])
        if not ref:
            return
        if ref[0] == 'category':
            ci = ref[1]
            cat = self.categories[ci]
            if not messagebox.askyesno(
                'Ta bort kategori?',
                f"Ta bort kategorin '{cat['name']}' och dess "
                f"{len(cat['items'])} utgifter?",
                parent=self.top,
            ):
                return
            del self.categories[ci]
        else:
            _, ci, ii = ref
            item = self.categories[ci]['items'][ii]
            if not messagebox.askyesno(
                'Ta bort utgift?', f"Ta bort utgiften '{item['name']}'?",
                parent=self.top,
            ):
                return
            del self.categories[ci]['items'][ii]
        self._save_categories()
        self._render_tree()

    def _on_close(self):
        try:
            self.app._expense_window_geometry = self.top.geometry()
        except tk.TclError:
            pass
        # Notera: vi sparar INTE här — bara vid faktiska ändringar. Annars
        # skulle default-mallen råka skriva över Set 1-värden när man öppnar
        # fönstret första gången och stänger utan att ändra.
        self.top.destroy()
        self.app._expense_window = None


def _fmt_kr(x):
    if not x:
        return ''
    try:
        return f'{int(x):,}'.replace(',', '\xa0')
    except (ValueError, TypeError):
        return str(x)


def _prompt_import_mode(parent, n_cats, n_items):
    """Fråga hur importen ska ske: ersätta befintliga eller lägga till.
    Returnerar 'replace', 'append' eller None."""
    dlg = tk.Toplevel(parent)
    dlg.title('Importera utgifter')
    dlg.transient(parent)
    dlg.resizable(False, False)
    dlg.grab_set()
    result = {'value': None}

    main = ttk.Frame(dlg, padding=16)
    main.pack()
    ttk.Label(
        main,
        text=f'Filen innehåller {n_cats} kategori(er) med {n_items} utgift(er).',
        font=('TkDefaultFont', 9, 'bold'),
    ).grid(row=0, column=0, sticky='w', pady=(0, 8))
    ttk.Label(
        main, text='Hur vill du importera dem?',
    ).grid(row=1, column=0, sticky='w', pady=(0, 8))

    def _pick(v):
        result['value'] = v
        dlg.destroy()

    btns = ttk.Frame(main)
    btns.grid(row=2, column=0, sticky='e')
    ttk.Button(
        btns, text='Ersätt befintliga',
        command=lambda: _pick('replace'),
    ).pack(side='left', padx=(0, 6))
    ttk.Button(
        btns, text='Lägg till efter befintliga',
        command=lambda: _pick('append'),
    ).pack(side='left', padx=(0, 6))
    ttk.Button(btns, text='Avbryt', command=dlg.destroy).pack(side='left')

    dlg.bind('<Escape>', lambda e: dlg.destroy())

    _center_dialog_on_parent(dlg, parent)

    parent.wait_window(dlg)
    return result['value']


def _prompt_string(parent, title, label, initial=''):
    """Enkel modal input-dialog. Returnerar None vid Avbryt/tom sträng."""
    dlg = tk.Toplevel(parent)
    dlg.title(title)
    dlg.transient(parent)
    dlg.resizable(False, False)
    dlg.grab_set()
    result = {'value': None}

    main = ttk.Frame(dlg, padding=16)
    main.pack()
    ttk.Label(main, text=label).grid(row=0, column=0, sticky='w', pady=(0, 6))
    var = tk.StringVar(value=initial)
    entry = ttk.Entry(main, textvariable=var, width=30)
    entry.grid(row=1, column=0, sticky='we')
    entry.focus_set()
    entry.select_range(0, 'end')

    def _ok():
        v = var.get().strip()
        if not v:
            return
        result['value'] = v
        dlg.destroy()

    btns = ttk.Frame(main)
    btns.grid(row=2, column=0, sticky='e', pady=(12, 0))
    ttk.Button(btns, text='OK', command=_ok).pack(side='right')
    ttk.Button(btns, text='Avbryt', command=dlg.destroy).pack(
        side='right', padx=(0, 6),
    )
    dlg.bind('<Return>', lambda e: _ok())
    dlg.bind('<Escape>', lambda e: dlg.destroy())

    # Centrera
    _center_dialog_on_parent(dlg, parent)

    parent.wait_window(dlg)
    return result['value']


def _prompt_item(parent, title, item, span_labels):
    """Modal dialog för att skapa/redigera en utgift.
    item=None → ny; annars redigera befintlig. Returnerar item-dict eller None."""
    dlg = tk.Toplevel(parent)
    dlg.title(title)
    dlg.transient(parent)
    dlg.resizable(False, False)
    dlg.grab_set()
    result = {'value': None}

    main = ttk.Frame(dlg, padding=16)
    main.pack()

    # Namn
    ttk.Label(
        main, text='Namn:', font=('TkDefaultFont', 9, 'bold'),
    ).grid(row=0, column=0, sticky='w', pady=(0, 2))
    name_var = tk.StringVar(value=item['name'] if item else '')
    name_entry = ttk.Entry(main, textvariable=name_var, width=35)
    name_entry.grid(row=1, column=0, columnspan=2, sticky='we', pady=(0, 8))
    name_entry.focus_set()
    if item:
        name_entry.select_range(0, 'end')

    # Period
    ttk.Label(
        main, text='Period:', font=('TkDefaultFont', 9, 'bold'),
    ).grid(row=2, column=0, sticky='w', pady=(0, 2))
    period_var = tk.StringVar(value=(item['period'] if item else 'månad'))
    period_row = ttk.Frame(main)
    period_row.grid(row=3, column=0, columnspan=2, sticky='w', pady=(0, 8))
    ttk.Radiobutton(
        period_row, text='Per månad', variable=period_var, value='månad',
    ).pack(side='left')
    ttk.Radiobutton(
        period_row, text='Per år', variable=period_var, value='år',
    ).pack(side='left', padx=(10, 0))

    # Belopp per åldersspann
    ttk.Label(
        main, text='Belopp per åldersspann:',
        font=('TkDefaultFont', 9, 'bold'),
    ).grid(row=4, column=0, columnspan=2, sticky='w', pady=(0, 4))
    amt_frame = ttk.Frame(main)
    amt_frame.grid(row=5, column=0, columnspan=2, sticky='we')
    amount_vars = []
    amount_entries = []

    # Detektera om alla belopp är samma (vid redigering av befintlig utgift)
    if item and item.get('amounts'):
        _amts = item['amounts']
        same_default = all(a == _amts[0] for a in _amts)
    else:
        same_default = False
    same_var = tk.BooleanVar(value=same_default)

    def _apply_same_state():
        """Aktivera/inaktivera Entry:erna 1..N och kopiera värdet från 0
        till dem om 'Samma' är aktiv."""
        if same_var.get():
            first_val = amount_vars[0].get()
            for j in range(1, NUM_SPANS):
                amount_vars[j].set(first_val)
                amount_entries[j].configure(state='disabled')
        else:
            for j in range(1, NUM_SPANS):
                amount_entries[j].configure(state='normal')

    def _on_first_change(*_):
        if same_var.get():
            v = amount_vars[0].get()
            for j in range(1, NUM_SPANS):
                amount_vars[j].set(v)

    month_labels = []
    for i in range(NUM_SPANS):
        ttk.Label(
            amt_frame, text=span_labels[i], width=14, anchor='w',
        ).grid(row=i, column=0, sticky='w', pady=1)
        v = tk.StringVar(
            value=str(item['amounts'][i]) if item and i < len(item['amounts']) else '0',
        )
        amount_vars.append(v)
        entry = ttk.Entry(amt_frame, textvariable=v, width=12)
        entry.grid(row=i, column=1, sticky='w', pady=1)
        amount_entries.append(entry)
        month_lbl = ttk.Label(amt_frame, text='kr', foreground='#666')
        month_lbl.grid(row=i, column=2, sticky='w', padx=(4, 0))
        month_labels.append(month_lbl)
        # 'Samma för alla' — placeras på första radens högersida
        if i == 0:
            ttk.Checkbutton(
                amt_frame,
                text='Samma belopp för alla åldersspann',
                variable=same_var, command=_apply_same_state,
            ).grid(row=i, column=3, sticky='w', padx=(12, 0))

    def _refresh_month_labels(*_):
        per_year = period_var.get() == 'år'
        for idx, av in enumerate(amount_vars):
            if per_year:
                try:
                    a = int((av.get() or '0').strip() or '0')
                except ValueError:
                    a = 0
                if a:
                    per_mo = int(round(a / 12))
                    text = f'kr  ({per_mo:,}/mån)'.replace(',', '\xa0')
                else:
                    text = 'kr'
            else:
                text = 'kr'
            month_labels[idx].configure(text=text)

    amount_vars[0].trace_add('write', _on_first_change)
    for av in amount_vars:
        av.trace_add('write', _refresh_month_labels)
    period_var.trace_add('write', _refresh_month_labels)
    _apply_same_state()  # sätt initialt disabled-state
    _refresh_month_labels()

    def _ok():
        name = name_var.get().strip()
        if not name:
            messagebox.showerror(
                'Namn saknas', 'Ange ett namn på utgiften.', parent=dlg,
            )
            return
        try:
            amounts = [int(v.get().strip() or '0') for v in amount_vars]
        except ValueError:
            messagebox.showerror(
                'Ogiltigt belopp', 'Alla belopp måste vara heltal.', parent=dlg,
            )
            return
        result['value'] = {
            'name': name, 'period': period_var.get(),
            'amounts': amounts,
        }
        dlg.destroy()

    btns = ttk.Frame(main)
    btns.grid(row=6, column=0, columnspan=2, sticky='e', pady=(12, 0))
    ttk.Button(btns, text='OK', command=_ok).pack(side='right')
    ttk.Button(btns, text='Avbryt', command=dlg.destroy).pack(
        side='right', padx=(0, 6),
    )
    dlg.bind('<Return>', lambda e: _ok())
    dlg.bind('<Escape>', lambda e: dlg.destroy())

    _center_dialog_on_parent(dlg, parent)

    parent.wait_window(dlg)
    return result['value']
