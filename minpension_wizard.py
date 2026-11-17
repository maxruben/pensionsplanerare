"""Guidad wizard för att hämta data från minpension.se via Chrome remote-debugging.

Kompletterar det manuella paste-flödet i pension_planner. Wizarden styr
användaren steg-för-steg:

1. Starta Chrome (med remote-debugging-port=9222 + isolerad profil)
2. Logga in + navigera till plan + expandera detaljer (manuellt)
3. Hämta data (Selenium attach via DevTools Protocol → body.innerText)
4. Bekräfta preview + klistra in i ny eller aktiv flik

Endast en knapp är aktiv åt gången enligt aktuellt steg i state-maskinen.
"""

import os
import socket
import subprocess
import sys
import tkinter as tk
from tkinter import messagebox, ttk

REMOTE_DEBUG_PORT = 9222


def _app_dir() -> str:
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def find_chrome_path() -> str:
    """Hitta chrome.exe via standardplatser på Windows. Returnerar tom sträng om ej hittad."""
    if sys.platform != 'win32':
        # Linux/macOS — Selenium hittar Chrome själv när vi launchar via Service
        return ''
    candidates = [
        os.path.expandvars(r'%PROGRAMFILES%\Google\Chrome\Application\chrome.exe'),
        os.path.expandvars(r'%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe'),
        os.path.expandvars(r'%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe'),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return ''


def is_port_open(port: int, host: str = '127.0.0.1') -> bool:
    """Snabb-check: lyssnar något på given port?"""
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except (OSError, ConnectionError):
        return False


def _show_error_copyable(parent, title: str, text: str):
    """Modal felmeddelandedialog där texten är markerbar+kopierbar.

    Ersätter messagebox.showerror för wizardens fel — användbart vid
    Selenium-trace eller långa exception-texter som man vill kopiera till
    en bug-rapport eller felsökning.
    """
    top = tk.Toplevel(parent)
    top.title(title)
    top.transient(parent)
    outer = ttk.Frame(top, padding=12)
    outer.pack(fill='both', expand=True)
    outer.columnconfigure(0, weight=1)
    outer.rowconfigure(1, weight=1)

    # Rubrik-rad med ikon + titel
    header = ttk.Frame(outer)
    header.grid(row=0, column=0, sticky='w', pady=(0, 8))
    ttk.Label(
        header, text='⚠', font=('TkDefaultFont', 22),
        foreground='#c33',
    ).pack(side='left', padx=(0, 8))
    ttk.Label(
        header, text=title, font=('TkDefaultFont', 11, 'bold'),
    ).pack(side='left')

    # Text-widget med fel-innehållet (markerbar, kopierbar)
    text_widget = tk.Text(
        outer, wrap='word', height=8, width=64,
        relief='solid', borderwidth=1,
        font=('TkDefaultFont', 9),
        background='#fafafa',
    )
    text_widget.insert('1.0', text)
    text_widget.grid(row=1, column=0, sticky='nsew', pady=(0, 8))
    sb = ttk.Scrollbar(outer, orient='vertical', command=text_widget.yview)
    sb.grid(row=1, column=1, sticky='ns')
    text_widget.configure(yscrollcommand=sb.set)

    # Knapp-rad
    btns = ttk.Frame(outer)
    btns.grid(row=2, column=0, columnspan=2, sticky='ew')

    def copy_to_clipboard():
        top.clipboard_clear()
        top.clipboard_append(text)
        top.update()

    ttk.Button(btns, text='Kopiera allt', command=copy_to_clipboard).pack(side='left')
    ttk.Button(btns, text='OK', command=top.destroy).pack(side='right')

    # Centrera på parent
    top.update_idletasks()
    parent.update_idletasks()
    try:
        px = parent.winfo_rootx() + (parent.winfo_width() // 2) - (top.winfo_width() // 2)
        py = parent.winfo_rooty() + (parent.winfo_height() // 2) - (top.winfo_height() // 2)
        top.geometry(f'+{max(0, px)}+{max(0, py)}')
    except tk.TclError:
        pass

    # Modal
    top.grab_set()
    text_widget.focus_set()
    # Bind Ctrl+C för att kopiera markering (även om Text-widgeten är "skrivbar")
    text_widget.bind('<Escape>', lambda e: top.destroy())
    parent.wait_window(top)


def _expand_all_details(driver) -> int:
    """Klicka alla kollapsade rader på minpension.se så detaljerna blir synliga.

    minpension.se använder Angular-driven uppvikning utan `aria-expanded`.
    Klickbara rader är `<div class="table-inner-row table-inner-row--clickable">`
    och bredvid finns ett `.table-row-content`-div med `height: 0px;
    visibility: hidden` när kollapsad — använd det som öppen/stängd-signal.

    Vi använder Selenium `.click()` (inte JS-click) eftersom Angular ofta
    ignorerar synthetic events med `isTrusted: false`. Loopar flera varv
    eftersom inre försäkringsrader bara finns i DOM:en efter att huvudkategorin
    expanderats.
    """
    import time
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.action_chains import ActionChains
    from selenium.common.exceptions import (
        StaleElementReferenceException, ElementNotInteractableException,
        ElementClickInterceptedException, WebDriverException,
    )

    def _try_click(driver, row):
        """Försök klicka raden via flera metoder — returnerar metoden som lyckades."""
        try:
            row.click()
            return 'selenium.click'
        except (ElementNotInteractableException, ElementClickInterceptedException):
            pass
        try:
            ActionChains(driver).move_to_element(row).click().perform()
            return 'ActionChains'
        except WebDriverException:
            pass
        try:
            driver.execute_script("arguments[0].click();", row)
            return 'js.click'
        except WebDriverException:
            pass
        return None

    # ÄNDRAD STRATEGI: leta direkt efter expand-knapparna istället för rader.
    # `.table-inner-row.table-inner-row--clickable` matchar flera olika typer
    # av element (header-rader, etc.) — bara vissa har faktiska expand-knappar.
    # Knappen `button.table-cell__button` med aria-label innehållande "Fäll ut"
    # finns ENDAST på kollapsade expanderbara rader.

    total = 0
    rounds_log = []
    methods_used = {}  # metod → räknare för diagnostik
    seen_btn_ids = set()  # undvik dubbel-klick på samma knapp

    # JS-filter: räkna `<app-prognos-table-row>` ancestors.
    # Kategori-knappar: 1 ancestor. Inre försäkringsknappar: 2+.
    js_find_category_expand_buttons = r"""
    function tableRowDepth(el) {
        let depth = 0;
        let cur = el;
        while (cur && cur !== document.body) {
            if (cur.tagName === 'APP-PROGNOS-TABLE-ROW') depth++;
            cur = cur.parentElement;
        }
        return depth;
    }
    const all = document.querySelectorAll(
        'button.table-cell__button[aria-label*="Fäll ut"]'
    );
    const filtered = Array.from(all).filter(btn => tableRowDepth(btn) <= 1);
    // Ingen fallback — om filtret returnerar 0 betyder det att inga
    // kategori-knappar är kollapsade (kategorierna är redan expanderade från
    // tidigare hämtning eller manuellt). Vi ska INTE klicka inre knappar
    // som fallback eftersom deras detaljer inte behövs.
    const result = filtered;
    const usedFallback = false;
    // Diagnostik om första knapparna
    const debug = Array.from(all).slice(0, 3).map((btn, i) => {
        const row = btn.closest('.table-inner-row--clickable');
        const label = row ? row.querySelector('app-prognos-table-row-label') : null;
        return {
            i: i,
            depth: tableRowDepth(btn),
            labelText: label ? label.textContent.replace(/\s+/g, ' ').trim().slice(0, 40) : '<no-label>',
        };
    });
    return {
        buttons: result,
        totalFound: all.length,
        afterFilter: filtered.length,
        usedFallback: usedFallback,
        debug: debug,
    };
    """

    # Bara ETT varv — kategori-nivåns expansion räcker. Efter expansion finns
    # individuella försäkringsrader synliga med uttagsstart/uttagslängd, vilket
    # är all data parsern behöver. Att expandera enskilda försäkringar avslöjar
    # bara extra detaljer (försäkringsnummer, kapital idag) som inte används.
    for round_no in range(1):
        try:
            res = driver.execute_script(js_find_category_expand_buttons)
        except WebDriverException:
            break
        buttons = res.get('buttons', [])
        if round_no == 0:
            rounds_log.append(
                f'init: {res.get("totalFound", 0)} totala "Fäll ut"-knappar, '
                f'{res.get("afterFilter", 0)} efter filter'
                + (' (fallback aktiv)' if res.get('usedFallback') else '')
            )
            for d in res.get('debug') or []:
                rounds_log.append(
                    f'btn[{d["i"]}]: depth={d["depth"]}, '
                    f'label="{d["labelText"]}"'
                )
        n_found = len(buttons)
        clicked_this_round = 0
        for btn in buttons:
            try:
                bid = btn.id
            except Exception:
                continue
            if bid in seen_btn_ids:
                continue
            try:
                # Scrolla in centrerat så klick träffar
                driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});", btn,
                )
                # Hitta klickbara förälder (raden med role="button") — knappen
                # själv har tabindex=-1 och kan ge ElementNotInteractable
                clicked = False
                # Försök först klicka knappen direkt
                method = _try_click(driver, btn)
                if method:
                    clicked = True
                else:
                    # Fall tillbaka på row-clicket — leta upp förfaderns roll=button
                    try:
                        row = btn.find_element(
                            By.XPATH,
                            "./ancestor::div[contains(@class,'table-inner-row--clickable')][1]",
                        )
                        method = _try_click(driver, row)
                        if method:
                            clicked = True
                    except Exception:
                        pass
                if clicked and method:
                    seen_btn_ids.add(bid)
                    methods_used[method] = methods_used.get(method, 0) + 1
                    clicked_this_round += 1
                    time.sleep(0.15)
            except (StaleElementReferenceException,
                    ElementNotInteractableException,
                    ElementClickInterceptedException):
                continue
            except WebDriverException:
                continue
        rounds_log.append(
            f'varv {round_no + 1}: {n_found} expand-knappar, '
            f'{clicked_this_round} klickade'
        )
        if clicked_this_round == 0:
            break
        total += clicked_this_round
        time.sleep(0.5)  # Vänta på animation + nya knappar
    if methods_used:
        methods_summary = ', '.join(f'{k}={v}' for k, v in methods_used.items())
        rounds_log.append(f'metoder: {methods_summary}')
    return total, rounds_log


class MinpensionWizard:
    """Toplevel-baserad wizard. Skapas via PensionPlannerApp._open_minpension_wizard."""

    STEPS = ('start_chrome', 'wait_login', 'plan_selection', 'fetch', 'confirm')
    STEP_TITLES = {
        'start_chrome': 'Steg 1 av 4: Starta Chrome',
        'wait_login': 'Steg 2 av 4: Logga in och förbered data',
        'plan_selection': 'Steg 2 av 4: Välj plan från listan',
        'fetch': 'Steg 3 av 4: Hämtar data',
        'confirm': 'Steg 4 av 4: Granska och använd',
    }

    def __init__(self, app):
        self.app = app
        self.state = 'start_chrome'
        self.chrome_proc = None  # subprocess.Popen-handle
        self.fetched_text = ''
        self._driver = None  # cachad Selenium-driver mellan fetch-anrop
        self._plan_list = None  # plan-namn från "Dina planer"-sidan om scrapad
        # Chrome-tab-handles när vi använder ny-flik-flödet från plan-listan
        self._list_tab_handle = None
        self._scrape_tab_handle = None
        self.top = tk.Toplevel(app.root)
        self.top.title("Hämta från minpension.se")
        self._apply_initial_geometry()
        self.top.transient(app.root)
        self.top.protocol('WM_DELETE_WINDOW', self._on_close)
        self._build_ui()
        # Hoppa över Steg 1 om Chrome redan körs med debug-porten (återanvänd session)
        if is_port_open(REMOTE_DEBUG_PORT):
            self.state = 'wait_login'
            self._reusing_session = True
        else:
            self._reusing_session = False
        self._update_state()

    def _apply_initial_geometry(self):
        """Återställ sparad geometri om sådan finns, annars centrera på
        huvudfönstrets skärm (viktigt vid multi-monitor)."""
        saved = getattr(self.app, '_minpension_wizard_geometry', None)
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
        cw, ch = 720, 680
        x = main_x + max(0, (main_w - cw) // 2)
        y = main_y + max(0, (main_h - ch) // 2)
        self.top.geometry(f'{cw}x{ch}+{x}+{y}')

    def _build_ui(self):
        outer = ttk.Frame(self.top, padding=12)
        outer.pack(fill='both', expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)
        self._outer = outer  # spara för dynamiska weight-justeringar

        self.step_label = ttk.Label(
            outer, text='', font=('TkDefaultFont', 11, 'bold'),
        )
        self.step_label.grid(row=0, column=0, sticky='w', pady=(0, 6))

        # Instruktionstext (multiline)
        instr_frame = ttk.Frame(outer)
        instr_frame.grid(row=1, column=0, sticky='nsew', pady=(0, 8))
        instr_frame.columnconfigure(0, weight=1)
        instr_frame.rowconfigure(0, weight=1)
        self.instr = tk.Text(
            instr_frame, wrap='word', height=10, relief='flat',
            background=self.top.cget('background'),
            font=('TkDefaultFont', 10),
        )
        self.instr.grid(row=0, column=0, sticky='nsew')
        self.instr.config(state='disabled')

        # Plan-väljare (visas bara i plan_selection-state) — sökfält + stor
        # listbox med alla planer synliga. Sökfältet filtrerar listan medan
        # man skriver (mönster: combobox med synlig lista, gammal Win-stil).
        self.plan_select_frame = ttk.Frame(outer)
        # Sökfält
        filter_row = ttk.Frame(self.plan_select_frame)
        filter_row.pack(fill='x', pady=(0, 4))
        ttk.Label(
            filter_row, text='Sök plan:',
            font=('TkDefaultFont', 9, 'bold'),
        ).pack(side='left')
        self.plan_filter_var = tk.StringVar()
        self.plan_filter_entry = ttk.Entry(
            filter_row, textvariable=self.plan_filter_var,
        )
        self.plan_filter_entry.pack(
            side='left', fill='x', expand=True, padx=(6, 0),
        )
        self.plan_filter_var.trace_add('write', self._on_plan_filter_change)
        # Pil-ner i sökfältet → flytta fokus till listbox
        self.plan_filter_entry.bind(
            '<Down>', lambda e: self._focus_listbox_top(),
        )
        # Enter i sökfältet = välj första matchen
        self.plan_filter_entry.bind(
            '<Return>', lambda e: self._select_and_fetch(),
        )
        # Listbox + scrollbar
        listbox_row = ttk.Frame(self.plan_select_frame)
        listbox_row.pack(fill='both', expand=True, pady=(0, 0))
        self.plan_listbox = tk.Listbox(
            listbox_row, height=20, width=70, selectmode='browse',
            activestyle='dotbox', exportselection=False,
        )
        self.plan_listbox.pack(side='left', fill='both', expand=True)
        plan_sb = ttk.Scrollbar(
            listbox_row, orient='vertical', command=self.plan_listbox.yview,
        )
        plan_sb.pack(side='left', fill='y')
        self.plan_listbox.config(yscrollcommand=plan_sb.set)
        # Dubbelklick / Enter = direkt val
        self.plan_listbox.bind('<Double-Button-1>', lambda e: self._select_and_fetch())
        self.plan_listbox.bind('<Return>', lambda e: self._select_and_fetch())
        # Indexes som mappar listbox-rader till self._plan_list-index
        self._filtered_indices = []
        self.plan_select_frame.grid(row=2, column=0, sticky='nsew', pady=(0, 6))
        outer.rowconfigure(2, weight=1)  # plan-frame får växa
        self.plan_select_frame.grid_remove()  # gömd som default

        # Status-rad
        self.status_label = ttk.Label(
            outer, text='', foreground='#666',
            font=('TkDefaultFont', 9),
        )
        self.status_label.grid(row=3, column=0, sticky='w', pady=(0, 8))

        # Knapp-rad
        btn_row = ttk.Frame(outer)
        btn_row.grid(row=4, column=0, sticky='ew')
        btn_row.columnconfigure(0, weight=1)

        # Sekvens-knapp (en åt gången, ändrar text per steg)
        self.action_btn = ttk.Button(
            btn_row, text='', command=self._on_action,
        )
        self.action_btn.pack(side='left')

        # Confirm-läget har två knappar — visas/döljs efter state
        self.confirm_new_btn = ttk.Button(
            btn_row, text='Använd → ny flik',
            command=lambda: self._apply_to_tab(new=True),
        )
        self.confirm_replace_btn = ttk.Button(
            btn_row, text='Använd → ersätt aktiv flik',
            command=lambda: self._apply_to_tab(new=False),
        )

        # Avbryt-knapp till höger (alltid synlig)
        self.cancel_btn = ttk.Button(
            btn_row, text='Avbryt', command=self._on_close,
        )
        self.cancel_btn.pack(side='right')

    # ---- State machine ----

    def _set_instructions(self, text: str):
        self.instr.config(state='normal')
        self.instr.delete('1.0', 'end')
        self.instr.insert('1.0', text)
        self.instr.config(state='disabled')

    def _update_state(self):
        self.step_label.config(text=self.STEP_TITLES[self.state])
        # Dölj confirm-knapparna som default
        self.confirm_new_btn.pack_forget()
        self.confirm_replace_btn.pack_forget()
        self.action_btn.pack(side='left')
        # Visa plan-väljaren bara i plan_selection-state, och låt den växa då
        if self.state == 'plan_selection':
            self.plan_select_frame.grid()
            self._outer.rowconfigure(1, weight=0)  # instr krymper
            self._outer.rowconfigure(2, weight=1)  # plan-frame växer
        else:
            self.plan_select_frame.grid_remove()
            self._outer.rowconfigure(1, weight=1)  # instr växer
            self._outer.rowconfigure(2, weight=0)
        # Justera instruktionshöjden per state — annars syns onödigt mycket
        # blankt utrymme när texten är kort
        instr_heights = {
            'start_chrome': 5, 'wait_login': 6, 'plan_selection': 4,
            'fetch': 3, 'confirm': 20,
        }
        self.instr.config(height=instr_heights.get(self.state, 10))

        if self.state == 'start_chrome':
            self._set_instructions(
                "Detta öppnar en separat Chrome-instans med en isolerad profil "
                "(påverkar inte din vanliga Chrome). Sidan minpension.se öppnas "
                "automatiskt.\n\n"
                "Klicka 'Starta Chrome' nedan."
            )
            self.action_btn.config(text='Starta Chrome', state='normal')
            self.status_label.config(text='')

        elif self.state == 'wait_login':
            if getattr(self, '_no_plan_error', False):
                # Återgång efter misslyckad hämtning — varken plan eller lista
                self._no_plan_error = False
                self._set_instructions(
                    "❌ Ingen uttagsplan eller lista med uttagsplaner hittades på "
                    "Chrome-flikens nuvarande sida.\n\n"
                    "Navigera antingen till en specifik uttagsplan, eller till "
                    "'Dina planer'-listan (där du ser alla dina sparade planer).\n\n"
                    "Klicka 'Fortsätt till hämtning' när du är på rätt sida."
                )
                self.action_btn.config(text='Fortsätt till hämtning', state='normal')
                self.status_label.config(text='Väntar på plan- eller lista-sida…')
            elif getattr(self, '_reusing_session', False):
                self._set_instructions(
                    "Befintlig Chrome-session hittades på port "
                    f"{REMOTE_DEBUG_PORT} — återanvänder den utan ny inloggning.\n\n"
                    "Navigera till 'Dina planer'-sidan i Chrome om du inte "
                    "redan är där.\n\n"
                    "Klicka 'Fortsätt' så listar wizarden dina planer."
                )
                self.action_btn.config(text='Fortsätt', state='normal')
                self.status_label.config(
                    text=f'Återanvänder Chrome-session på port {REMOTE_DEBUG_PORT}',
                )
            else:
                self._set_instructions(
                    "Logga in med BankID i Chrome-fönstret som öppnades.\n\n"
                    "Du landar på 'Dina planer'-sidan automatiskt efter "
                    "inloggning.\n\n"
                    "Klicka 'Klar med inloggning' så listar wizarden dina planer."
                )
                self.action_btn.config(text='Klar med inloggning', state='normal')
                self.status_label.config(
                    text=f'Lyssnar på Chrome via remote-debugging-port {REMOTE_DEBUG_PORT}…',
                )

        elif self.state == 'plan_selection':
            n = len(self._plan_list or [])
            self._set_instructions(
                f"Hittade {n} planer på sidan. Sök eller välj en i listan "
                "(dubbelklick eller Enter = direkt val). 'Hämta vald plan' "
                "navigerar till planen i Chrome och fortsätter automatiskt."
            )
            self.action_btn.config(text='Hämta vald plan', state='normal')
            self.status_label.config(text='')
            # Rensa filter och fyll listbox
            self.plan_filter_var.set('')
            matched_active = self._populate_plan_listbox()
            # Om aktiv plan hittades i listan → fokus i listboxen så pil
            # upp/ner direkt bläddrar. Annars fokus i sökfältet så man kan
            # börja skriva.
            if matched_active:
                self.top.after(50, self.plan_listbox.focus_set)
            else:
                self.top.after(50, self.plan_filter_entry.focus_set)

        elif self.state == 'fetch':
            self._set_instructions(
                "Hämtar data från aktiv Chrome-flik och expanderar kategorierna…\n\n"
                "Det går nästa gång snabbare när anslutningen redan är etablerad."
            )
            # Knappen behövs inte här — fetch triggas automatiskt efter steg 2
            self.action_btn.pack_forget()
            self.status_label.config(text='')

        elif self.state == 'confirm':
            n = len(self.fetched_text)
            expanded = getattr(self, '_expanded_count', 0)
            # Tolka direkt — sparar inget mer parse-arbete senare och låter
            # oss visa rik sammanfattning istället för rå textsnutt.
            try:
                from parsing import parse_minpension_text
                parsed = parse_minpension_text(self.fetched_text)
            except Exception:
                parsed = None
            plan_name = (parsed.plan_name if parsed else '') or '<okänt>'

            # Hitta ev. befintlig flik med samma plan-namn → visa bara
            # relevant knapp ("Ersätt befintlig" eller "Ny flik")
            self._matching_tab = None
            if plan_name and plan_name != '<okänt>':
                for tab in self.app.tabs:
                    if tab.parsed and tab.parsed.plan_name == plan_name:
                        self._matching_tab = tab
                        break

            # Om inga planer är inlästa alls (t.ex. efter Arkiv → Ny) — använd
            # den tomma fliken direkt istället för att skapa en ny.
            self._empty_tab_to_reuse = None
            if self._matching_tab is None:
                if self.app.tabs and all(t.parsed is None for t in self.app.tabs):
                    self._empty_tab_to_reuse = (
                        self.app._current_tab() or self.app.tabs[0]
                    )

            if self._matching_tab is not None:
                action_msg = (
                    f"Planen '{plan_name}' finns redan i en öppen flik. "
                    "Klicka för att ersätta den."
                )
            elif self._empty_tab_to_reuse is not None:
                action_msg = (
                    "Inga planer är inlästa — klicka för att lägga in "
                    f"'{plan_name}' i den tomma fliken."
                )
            else:
                action_msg = (
                    f"Planen '{plan_name}' finns inte i någon öppen flik. "
                    "Klicka för att skapa en ny flik."
                )

            summary = self._format_plan_summary(parsed, n, expanded)
            self._set_instructions(f"{summary}\n\n{action_msg}")
            self.action_btn.pack_forget()
            self.confirm_new_btn.pack_forget()
            self.confirm_replace_btn.pack_forget()
            if self._matching_tab is not None:
                self.confirm_replace_btn.config(text='Använd → Ersätt befintlig')
                self.confirm_replace_btn.pack(side='left')
            elif self._empty_tab_to_reuse is not None:
                self.confirm_replace_btn.config(text='Använd → Lägg till i tom flik')
                self.confirm_replace_btn.pack(side='left')
            else:
                self.confirm_new_btn.config(text='Använd → Ny flik')
                self.confirm_new_btn.pack(side='left')
            self.status_label.config(text='')

    def _on_action(self):
        if self.state == 'start_chrome':
            self._start_chrome()
        elif self.state == 'wait_login':
            self._check_login_done()
        elif self.state == 'plan_selection':
            self._select_and_fetch()
        elif self.state == 'fetch':
            self._fetch_data()

    # ---- Steg-handlers ----

    def _start_chrome(self):
        chrome_path = find_chrome_path()
        if not chrome_path:
            _show_error_copyable(self.top, "Chrome saknas", "Kunde inte hitta chrome.exe på standardplatserna.\n\n"
                "Installera Chrome eller använd den manuella paste-funktionen istället.")
            return
        if is_port_open(REMOTE_DEBUG_PORT):
            # Befintlig session — hoppa direkt vidare istället för att starta en ny
            self._reusing_session = True
            self.state = 'wait_login'
            self._update_state()
            return
        profile_dir = os.path.join(_app_dir(), '.chrome_minpension')
        os.makedirs(profile_dir, exist_ok=True)
        args = [
            chrome_path,
            f'--remote-debugging-port={REMOTE_DEBUG_PORT}',
            f'--user-data-dir={profile_dir}',
            '--new-window',
            'https://mina-sidor.minpension.se/planera/dina-planer',
        ]
        try:
            self.chrome_proc = subprocess.Popen(args)
        except OSError as e:
            _show_error_copyable(self.top, "Kunde inte starta Chrome", f"Fel: {e}")
            return
        self.state = 'wait_login'
        self._update_state()

    def _check_login_done(self):
        # Verifiera att Chrome fortfarande lyssnar på debug-porten
        if not is_port_open(REMOTE_DEBUG_PORT):
            _show_error_copyable(self.top, "Chrome svarar inte", f"Hittar inte Chrome på port {REMOTE_DEBUG_PORT}. "
                "Är fönstret stängt? Starta om wizarden.")
            return
        # Detektera vilken sidtyp Chrome-fliken visar
        self.action_btn.config(state='disabled')
        self.status_label.config(text='Detekterar sidtyp…')
        self.top.update_idletasks()
        driver = self._get_driver()
        if driver is None:
            self.action_btn.config(state='normal')
            return
        try:
            page_info = driver.execute_script(r"""
                // Plan-sida: har Fäll-ut/Fäll-ihop-knappar
                const btns = document.querySelectorAll(
                    'button.table-cell__button[aria-label]'
                );
                for (const b of btns) {
                    const l = b.getAttribute('aria-label') || '';
                    if (l.includes('Fäll')) {
                        return {type: 'plan'};
                    }
                }
                // Lista-sida: leta efter 'up-plan-card > a.plan-card' (den
                // YTTRE länken per kort — inte de inre knapparna Ändra/Kopiera).
                const cards = document.querySelectorAll('up-plan-card a.plan-card');
                if (cards.length > 0) {
                    const plans = Array.from(cards).map(a => {
                        // Plan-namn: <h3> inuti
                        const h3 = a.querySelector('h3');
                        const name = h3 ? h3.textContent.trim() : '';
                        // Uttagsstart: hitta <dt>Uttagsstart</dt> följt av <dd>
                        let start = '';
                        const dts = a.querySelectorAll('dt');
                        for (const dt of dts) {
                            if (dt.textContent.trim() === 'Uttagsstart') {
                                const dd = dt.nextElementSibling;
                                if (dd && dd.tagName === 'DD') {
                                    start = dd.textContent.trim();
                                }
                                break;
                            }
                        }
                        const display = start
                            ? `${name}  —  Start: ${start}`
                            : name;
                        return { name: display, url: a.getAttribute('href') };
                    });
                    return {type: 'list', plans: plans};
                }
                return {type: 'unknown'};
            """)
        except Exception as e:
            self.action_btn.config(state='normal')
            _show_error_copyable(self.top, "Kunde inte detektera sidan", f"Selenium-fel: {e}")
            return

        page_type = (page_info or {}).get('type', 'unknown')
        self.action_btn.config(state='normal')

        if page_type == 'plan':
            self.state = 'fetch'
            self._update_state()
            self.top.after(50, self._fetch_data)
        elif page_type == 'list':
            self._plan_list = page_info.get('plans') or []
            self.state = 'plan_selection'
            self._update_state()
        else:
            # Varken plan-sida eller lista — felmeddelande som tidigare
            self._no_plan_error = True
            self._update_state()

    def _get_driver(self):
        """Hämta cachad Selenium-driver eller skapa ny anslutning.

        Återanvänder driver mellan fetch-anrop — annars tar varje hämtning
        2-5 sekunder bara på att återansluta till Chrome.
        """
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
        except ImportError:
            _show_error_copyable(self.top, "Selenium saknas", "Selenium är inte installerat. Kör 'pip install selenium>=4.6' "
                "och försök igen.")
            return None
        if self._driver is not None:
            # Verifiera att drivern fortfarande är ansluten
            try:
                self._driver.current_url  # billig sanity-check
                return self._driver
            except Exception:
                self._driver = None
        options = Options()
        options.add_experimental_option(
            'debuggerAddress', f'127.0.0.1:{REMOTE_DEBUG_PORT}',
        )
        try:
            self._driver = webdriver.Chrome(options=options)
        except Exception as e:
            _show_error_copyable(self.top, "Kunde inte ansluta till Chrome", f"Selenium-fel: {e}")
            return None
        return self._driver

    def _populate_plan_listbox(self):
        """Fyll listboxen med (eventuellt filtrerade) planer från self._plan_list.

        Förväljer raden som matchar nuvarande aktiva flikens plan-namn — om
        ingen filter är satt och planen finns i listan. Returnerar True om
        sådan aktiv-plan-matchning hittades (så att caller kan fokusera
        listboxen istället för sökfältet)."""
        pattern = (self.plan_filter_var.get() or '').lower().strip()
        self.plan_listbox.delete(0, 'end')
        self._filtered_indices = []
        for i, p in enumerate(self._plan_list or []):
            name = p.get('name', '')
            if not pattern or pattern in name.lower():
                self.plan_listbox.insert('end', name)
                self._filtered_indices.append(i)
        # Standardval: första raden. Om ingen filter är satt och aktiv flik
        # har en plan som finns i listan — välj den istället.
        preselect_idx = 0
        matched_active = False
        if not pattern:
            active = self.app._current_tab()
            active_name = ''
            if active and active.parsed and active.parsed.plan_name:
                active_name = active.parsed.plan_name
            if active_name:
                for li, fi in enumerate(self._filtered_indices):
                    p = (self._plan_list or [])[fi]
                    raw = p.get('name', '').split('  —  ')[0].strip()
                    if raw == active_name:
                        preselect_idx = li
                        matched_active = True
                        break
        if self.plan_listbox.size() > 0:
            self.plan_listbox.selection_clear(0, 'end')
            self.plan_listbox.selection_set(preselect_idx)
            self.plan_listbox.activate(preselect_idx)
            self.plan_listbox.see(preselect_idx)
        return matched_active

    def _on_plan_filter_change(self, *_):
        self._populate_plan_listbox()

    def _focus_listbox_top(self):
        self.plan_listbox.focus_set()
        if self.plan_listbox.size() > 0:
            self.plan_listbox.selection_clear(0, 'end')
            self.plan_listbox.selection_set(0)
            self.plan_listbox.activate(0)

    def _select_and_fetch(self):
        """Öppna vald plan i en NY Chrome-flik (så plan-listan finns kvar i
        original-fliken), vänta på att den laddas, och fortsätt med fetch.
        Nya fliken stängs efter hämtning så användaren är tillbaka i listan."""
        sel = self.plan_listbox.curselection()
        if not sel or not self._plan_list:
            messagebox.showinfo(
                "Ingen plan vald", "Välj en plan i listan först.",
                parent=self.top,
            )
            return
        listbox_idx = sel[0]
        if listbox_idx >= len(self._filtered_indices):
            _show_error_copyable(self.top, "Fel", "Ogiltigt val.")
            return
        idx = self._filtered_indices[listbox_idx]
        if idx >= len(self._plan_list):
            _show_error_copyable(self.top, "Fel", "Ogiltigt val.")
            return
        plan_url = self._plan_list[idx].get('url', '')

        driver = self._get_driver()
        if driver is None:
            return
        self.action_btn.config(state='disabled')
        self.status_label.config(text='Öppnar ny Chrome-flik…')
        self.top.config(cursor='watch')
        self.top.update_idletasks()
        try:
            # Spara nuvarande flikens handle (plan-listan)
            self._list_tab_handle = driver.current_window_handle
            # Öppna ny flik och navigera till plan-LISTAN på den inloggade
            # subdomänen `mina-sidor.minpension.se` (`www.minpension.se`
            # är den publika marknadssajten och har inte /planera/-rutterna).
            # Vi navigerar till listan istället för direkt till en plan
            # eftersom Angular-SPA:n inte serverar deep links statiskt.
            driver.switch_to.new_window('tab')
            driver.get('https://mina-sidor.minpension.se/planera/dina-planer')
            self._scrape_tab_handle = driver.current_window_handle
        except Exception as e:
            self.top.config(cursor='')
            self.action_btn.config(state='normal')
            _show_error_copyable(
                self.top, "Kunde inte öppna plan", f"Selenium-fel: {e}",
            )
            return
        # Vänta på att plan-listan laddas i nya fliken, sen klicka rätt plan
        self.status_label.config(
            text='Väntar på plan-listan i nya fliken…',
        )
        self.top.update_idletasks()
        self._wait_for_list_and_click(plan_url=plan_url, attempts=30)

    def _wait_for_list_and_click(self, plan_url: str, attempts: int):
        """Poll tills plan-listan är laddad i nya fliken, klicka sen rätt plan.

        Matchar via href för säkrast resultat (om ordning ändras matchar vi
        ändå rätt plan). Fortsätter sen till _wait_for_plan_load.
        """
        if attempts <= 0:
            self.top.config(cursor='')
            self.action_btn.config(state='normal')
            _show_error_copyable(
                self.top, "Tidsgräns",
                "Plan-listan laddades inte i nya fliken i tid.",
            )
            self._close_scrape_tab()
            return
        try:
            driver = self._get_driver()
            from selenium.webdriver.common.by import By
            cards = driver.find_elements(
                By.CSS_SELECTOR, 'up-plan-card a.plan-card',
            )
        except Exception:
            cards = []
        # Hitta rätt kort genom att matcha href
        target_card = None
        for card in cards:
            try:
                href = card.get_attribute('href') or ''
            except Exception:
                continue
            if plan_url and href.endswith(plan_url):
                target_card = card
                break
        if target_card is not None:
            from selenium.common.exceptions import (
                ElementClickInterceptedException,
                ElementNotInteractableException,
            )
            try:
                driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});",
                    target_card,
                )
                try:
                    target_card.click()
                except (ElementClickInterceptedException,
                        ElementNotInteractableException):
                    # Något overlay/banner ligger ovanpå — JS-click bypassar
                    # pointer-events-blockering
                    driver.execute_script(
                        "arguments[0].click();", target_card,
                    )
            except Exception as e:
                self.top.config(cursor='')
                self.action_btn.config(state='normal')
                _show_error_copyable(
                    self.top, "Kunde inte klicka plan", f"Selenium-fel: {e}",
                )
                self._close_scrape_tab()
                return
            self.status_label.config(text='Väntar på att plan-sidan laddas…')
            self.top.update_idletasks()
            self._wait_for_plan_load(attempts=30)
            return
        # Inte hittad än — försök igen
        self.top.after(
            500, lambda: self._wait_for_list_and_click(plan_url, attempts - 1),
        )

    def _wait_for_plan_load(self, attempts: int):
        """Poll tills plan-sidans Fäll-ut-knappar dyker upp (eller timeout)."""
        if attempts <= 0:
            self.top.config(cursor='')
            self.action_btn.config(state='normal')
            _show_error_copyable(
                self.top, "Tidsgräns",
                "Plan-sidan laddades inte i tid. Försök igen eller "
                "navigera manuellt till planen.",
            )
            return
        try:
            driver = self._get_driver()
            has_plan = driver.execute_script(r"""
                const btns = document.querySelectorAll(
                    'button.table-cell__button[aria-label]'
                );
                for (const b of btns) {
                    const l = b.getAttribute('aria-label') || '';
                    if (l.includes('Fäll')) return true;
                }
                return false;
            """) if driver else False
        except Exception:
            has_plan = False
        if has_plan:
            self.top.config(cursor='')
            self.state = 'fetch'
            self._update_state()
            self.top.after(50, self._fetch_data)
            return
        self.top.after(500, lambda: self._wait_for_plan_load(attempts - 1))

    def _fetch_data(self):
        self.action_btn.config(state='disabled')
        self.top.config(cursor='watch')
        # Första gången tar anslutning några sekunder; därefter omedelbart
        first_connect = self._driver is None
        self.status_label.config(
            text='Ansluter till Chrome…' if first_connect else 'Använder befintlig anslutning…',
        )
        self.top.update_idletasks()
        driver = self._get_driver()
        if driver is None:
            self.top.config(cursor='')
            self.action_btn.config(state='normal')
            return
        n_expanded = 0
        try:
            # Verifiera först att en plan är öppen — prognos-tabellen har
            # antingen "Fäll ut" eller "Fäll ihop"-knappar.
            self.status_label.config(text='Kontrollerar att en plan är öppen…')
            self.top.update_idletasks()
            has_plan = driver.execute_script(r"""
                const btns = document.querySelectorAll(
                    'button.table-cell__button[aria-label]'
                );
                for (const b of btns) {
                    const l = b.getAttribute('aria-label') || '';
                    if (l.includes('Fäll ut') || l.includes('Fäll ihop')) {
                        return true;
                    }
                }
                return false;
            """)
            if not has_plan:
                self.top.config(cursor='')
                self.action_btn.config(state='normal')
                self._no_plan_error = True
                self.state = 'wait_login'
                self._update_state()
                return

            # Se till att "Visa detaljer och ändra per pension"-checkboxen är
            # ikryssad — annars är expand-knapparna inte renderade i DOM:en och
            # _expand_all_details hittar inget att klicka på.
            self.status_label.config(text='Kontrollerar detalj-checkboxen…')
            self.top.update_idletasks()
            checkbox_status = driver.execute_script(r"""
                const label = 'Visa detaljer och ändra per pension';
                const walker = document.createTreeWalker(
                    document.body, NodeFilter.SHOW_TEXT, null, false
                );
                let node;
                while (node = walker.nextNode()) {
                    const t = (node.nodeValue || '').trim();
                    if (!t.includes(label)) continue;
                    // Hitta närmaste förälder som innehåller en checkbox
                    let el = node.parentElement;
                    for (let i = 0; i < 8 && el; i++) {
                        const cb = el.querySelector('input[type="checkbox"]');
                        if (cb) {
                            if (!cb.checked) {
                                // Klicka på associerad label om möjligt
                                // (Angular/Material lyssnar ofta på label-klick).
                                const lbl = cb.id
                                    ? document.querySelector('label[for="' + cb.id + '"]')
                                    : cb.closest('label');
                                if (lbl) { lbl.click(); } else { cb.click(); }
                                return 'checked';
                            }
                            return 'already-checked';
                        }
                        el = el.parentElement;
                    }
                }
                return 'not-found';
            """)
            if checkbox_status == 'checked':
                # Ge Angular tid att rendera de nyexponerade rad-elementen
                import time
                time.sleep(0.6)

            self.status_label.config(text='Expanderar försäkrings-detaljer…')
            self.top.update_idletasks()
            n_expanded, expand_log = _expand_all_details(driver)
            self._expand_log = expand_log
            # Liten paus så att de utfällda sektionerna får tid att animera in
            import time
            time.sleep(0.4)
            self.status_label.config(text='Läser sidans text…')
            self.top.update_idletasks()
            # Selection.toString() mimar exakt vad Ctrl+A+Ctrl+C ger —
            # inklusive tab-tecken mellan tabellceller. Detta är vad
            # parsing.py förväntar sig (innerText producerar inga tabs).
            text = driver.execute_script(r"""
                const sel = window.getSelection();
                sel.removeAllRanges();
                const range = document.createRange();
                range.selectNodeContents(document.body);
                sel.addRange(range);
                const t = sel.toString();
                sel.removeAllRanges();
                return t;
            """) or ''
        except Exception as e:
            # Driver kan ha tappat anslutning — släpp cachen så nästa försök
            # gör en ny anslutning
            try:
                self._driver = None
            except Exception:
                pass
            self.top.config(cursor='')
            # Re-packa action-knappen (gömdes i fetch-state) så användaren
            # kan trigga retry
            self.action_btn.pack(side='left')
            self.action_btn.config(state='normal', text='Försök igen')
            _show_error_copyable(self.top, "Kunde inte hämta data", f"Selenium-fel: {e}")
            self._close_scrape_tab()
            return
        self.top.config(cursor='')
        self.fetched_text = text or ''
        self._expanded_count = n_expanded
        if not self.fetched_text.strip():
            self.action_btn.config(state='normal')
            messagebox.showwarning(
                "Tom sida",
                "Hämtade text var tom — kontrollera att du är på en plan-sida.",
                parent=self.top,
            )
            self._close_scrape_tab()
            return
        # Stäng den extra Chrome-fliken om vi öppnade en, så användaren är
        # tillbaka på plan-listan inför ev. nästa hämtning
        self._close_scrape_tab()
        self.state = 'confirm'
        self._update_state()

    def _close_scrape_tab(self):
        """Stäng den nya Chrome-fliken (om öppnad) och växla tillbaka till listan."""
        if not self._scrape_tab_handle:
            return
        try:
            driver = self._driver
            if driver is None:
                return
            current = None
            try:
                current = driver.current_window_handle
            except Exception:
                pass
            # Växla till scrape-fliken om vi inte redan är där
            try:
                driver.switch_to.window(self._scrape_tab_handle)
                driver.close()
            except Exception:
                pass
            # Växla tillbaka till listan
            if self._list_tab_handle:
                try:
                    driver.switch_to.window(self._list_tab_handle)
                except Exception:
                    pass
        finally:
            self._scrape_tab_handle = None

    @staticmethod
    def _extract_plan_name(text: str) -> str:
        """Försök läsa ut plan-namnet via befintliga parsern; fall tillbaka på '<okänt>'."""
        try:
            from parsing import parse_minpension_text
            parsed = parse_minpension_text(text)
            return parsed.plan_name or '<okänt>'
        except Exception:
            return '<okänt>'

    @staticmethod
    def _format_plan_summary(parsed, n_chars: int, n_expanded: int) -> str:
        """Bygg en mer informativ sammanfattning av hämtad plan för confirm-vyn."""
        header = (
            f"Hämtade {n_chars} tecken från Chrome-fliken "
            f"(klickade {n_expanded} expanderingar totalt)."
        )
        if parsed is None or not parsed.plan_name:
            return f"{header}\n\nKunde inte tolka planen — verifiera manuellt."

        def fmt_ym(ym):
            if not ym:
                return '—'
            return f"{ym[0]}-{ym[1]:02d}"

        def fmt_duration(dur):
            if dur is None:
                return 'Livet ut'
            y, m = dur
            if y and m:
                return f"{y} år {m} mån"
            if y:
                return f"{y} år"
            if m:
                return f"{m} mån"
            return '—'

        lines = [header, '', f"Plan: {parsed.plan_name}"]

        if parsed.breakpoints:
            first = parsed.breakpoints[0].date
            last = parsed.breakpoints[-1].date
            lines.append(
                f"Brytpunkter: {len(parsed.breakpoints)} "
                f"({fmt_ym(first)} → {fmt_ym(last)})"
            )

        if parsed.insurances:
            n_total = len(parsed.insurances)
            n_with = sum(
                1 for ins in parsed.insurances if ins.amounts_before_tax
            )
            if n_with == 0:
                lines.append("Inkomst per försäkring: saknas")
            elif n_with == n_total:
                lines.append(
                    f"Inkomst per försäkring: inläst ({n_with}/{n_total})"
                )
            else:
                lines.append(
                    f"Inkomst per försäkring: delvis ({n_with}/{n_total})"
                )

            lines.append('')
            lines.append(f"Försäkringar ({n_total}):")
            name_w = min(
                max((len(ins.name) for ins in parsed.insurances), default=20),
                40,
            )
            for ins in parsed.insurances:
                nm = ins.name
                if len(nm) > name_w:
                    nm = nm[: name_w - 1] + '…'
                start_s = fmt_ym(ins.start)
                dur_s = fmt_duration(ins.duration)
                lines.append(f"  • {nm:<{name_w}}  {start_s}  {dur_s}")

        return '\n'.join(lines)

    def _apply_to_tab(self, new: bool):
        text = self.fetched_text
        if not text:
            return
        if new:
            tab = self.app.add_tab(select=True)
        else:
            # Prioritetsordning för ej-ny: matchande plan-flik → tom flik
            # (inga planer inlästa) → aktiv flik (defensiv backup)
            tab = (
                getattr(self, '_matching_tab', None)
                or getattr(self, '_empty_tab_to_reuse', None)
                or self.app._current_tab()
            )
            if tab is None:
                tab = self.app.add_tab(select=True)
        # Rensa paste-rutan (inklusive placeholder) och klistra in
        tab._clear_paste_placeholder()
        tab.paste_text.delete('1.0', 'end')
        tab.paste_text.insert('1.0', text)
        tab.on_parse()
        # Stämpla när hämtningen gjordes (lokal tid, ISO-format med minuter)
        from datetime import datetime
        tab.fetched_at = datetime.now().strftime('%Y-%m-%d %H:%M')
        try:
            self.app._update_active_plan_label()
        except (AttributeError, tk.TclError):
            pass
        # Växla till den påverkade fliken så användaren ser resultatet direkt
        try:
            self.app.notebook.select(tab.frame)
        except tk.TclError:
            pass
        self._on_close()

    def _on_close(self):
        # Spara fönstrets aktuella position/storlek så det öppnas på samma
        # plats nästa gång
        try:
            self.app._minpension_wizard_geometry = self.top.geometry()
        except tk.TclError:
            pass
        # Stäng ev. öppen scrape-flik först (innan vi släpper drivern)
        self._close_scrape_tab()
        # Selenium's driver.quit() stänger chromedriver-subprocessen (Chrome
        # själva förblir öppen via remote-debug). quit() blockerar 1-3 sek på
        # Windows — kör det i bakgrundstråd så wizard-fönstret stängs direkt.
        driver = self._driver
        self._driver = None
        if driver is not None:
            import threading
            threading.Thread(
                target=self._safe_quit_driver, args=(driver,), daemon=True,
            ).start()
        try:
            self.top.destroy()
        except tk.TclError:
            pass
        self.app._minpension_wizard = None

    @staticmethod
    def _safe_quit_driver(driver):
        """Försök stänga driver. Körs i bakgrundstråd — fel ignoreras tyst."""
        try:
            driver.quit()
        except Exception:
            pass
