"""Konvertera text kopierad från minpension.se till TSV som klistras in i Excel-mall."""

import pyperclip
from parsing import parse_minpension_text

# Födelseår som används för åldersberäkning när planen saknar födelsedatum
# (godtyckligt valt; riktiga planer från minpension.se har alltid ett eget).
FALLBACK_BIRTH_YEAR = 1960


def main():
    text = pyperclip.paste()

    if not text:
        print("Clipboard is empty!")
        return

    parsed = parse_minpension_text(text)
    plan_name = parsed.plan_name
    insurances = parsed.insurances
    breakpoints = parsed.breakpoints
    birth_year = parsed.birth_year
    birth_month = parsed.birth_month
    earliest_start = parsed.earliest_start

    print(f"\nPlan: {plan_name}")
    print(f"Född: {birth_year}-{birth_month:02d}" if birth_year else "Född: okänt")
    print(f"Försäkringar: {len(insurances)}")
    for ins in insurances:
        s = f"{ins.start[0]}, {ins.start[1]}" if ins.start else '?'
        d = f"{ins.duration[0]} år, {ins.duration[1]} mån" if ins.duration else 'Livet ut'
        print(f"  {ins.name} | start: {s} | längd: {d}")
    print(f"Datapunkter: {len(breakpoints)}")
    print(f"Tidigaste uttagsstart: {earliest_start[0]}-{earliest_start[1]:02d}" if earliest_start else "Ingen uttagsstart hittad")

    if not breakpoints:
        print("Could not parse amounts data")
        return

    # --- Calculate Excel row numbers (1-based) ---
    # Row 1: Namn på plan
    # Row 2: empty
    # Row 3: Pensioner header (+ desc for Ösk/år in K3/L3)
    # Rows 4 to 3+pension_rows: insurance rows (with K/L descriptions on rows 4-9)
    # Row 4+pension_rows: empty
    # Row 5+pension_rows: Kostnad vid ålder
    # Row 6+pension_rows: Upp till åder (ages_row)
    # Row 7+pension_rows: Total månadskostnad (costs_row)
    # Row 8+pension_rows: Header row
    # Row 9+pension_rows: First data row
    num_ins = len(insurances)
    num_desc_rows = 6  # description rows 4-9 (Acc #1, Kvar #1, ..., Kvar #3)
    pension_rows = max(num_ins, num_desc_rows)
    sparande_row = 5 + pension_rows
    ages_row = 6 + pension_rows
    costs_row = 7 + pension_rows
    header_row = 8 + pension_rows
    first_data_row = 9 + pension_rows

    start_year = earliest_start[0] if earliest_start else 2027
    end_year = 2053
    num_years = end_year - start_year + 1

    # --- Build TSV output ---
    out = []

    descriptions = [
        ('Ösk/år', 'Totalt överskott för hela året'),
        ('Acc #1', 'Accumulerat överskot efter året #1'),
        ('Kvar #1', 'Kvar av sparande justerat med accumulerat överskott #1 '),
        ('Acc #2', 'Accumulerat överskott efter året #2 (Endast underskott.)'),
        ('Kvar #2', 'Kvar av sparande justerat med accumulerat överskott #2'),
        ('Acc #3', 'Accumulerat överskott evter året #3 (100% av underskott + 50% av överskott)'),
        ('Kvar #3', 'Kvar av sparande justerat med accumulerat överskott #3'),
    ]

    def row_with_desc(cells_ae, desc_idx):
        cells = list(cells_ae)
        while len(cells) < 10:
            cells.append('')
        if desc_idx is not None and desc_idx < len(descriptions):
            cells.append(descriptions[desc_idx][0])
            cells.append(descriptions[desc_idx][1])
        return '\t'.join(str(c) for c in cells)

    out.append(f"Namn på plan\t{plan_name}")
    out.append('')
    out.append(row_with_desc(['Pensioner', 'U å', 'U m', 'L å', 'L m'], 0))
    for i in range(pension_rows):
        if i < num_ins:
            ins = insurances[i]
            start_y = str(ins.start[0]) if ins.start else ''
            start_m = str(ins.start[1]) if ins.start else ''
            if ins.duration:
                cells_ae = [ins.name, start_y, start_m, ins.duration[0], ins.duration[1]]
            else:
                cells_ae = [ins.name, start_y, start_m, 'Livsvarigt']
        else:
            cells_ae = []
        desc_idx = i + 1 if i + 1 < len(descriptions) else None
        out.append(row_with_desc(cells_ae, desc_idx))
    out.append('')
    out.append("Kostnad vid ålder" + "\t" * 12 + "Sparande\t=Common!C49")
    out.append("Upp till åder\t63\t70\t75\t80\t85\t100")
    out.append(f"Total månadskostnad\t=cost(B{ages_row})\t=cost(C{ages_row})\t=cost(D{ages_row})\t=cost(E{ages_row})\t=cost(F{ages_row})\t=cost(G{ages_row})\t\t\tTotal kostnad per år och accumulerat")
    out.append("År\tMånad\tÅlder\tFöre skatt\tEfter skatt\tUtgifter\tÖskott/mån\tÖskott år\t\tÅlder\tÅr\tÖsk/år\tAcc #1\tKvar #1\tAcc #2\tKvar #2\tAcc #3\tKvar #3")

    for year_idx in range(num_years):
        year = start_year + year_idx
        for month in range(1, 13):
            r = first_data_row + year_idx * 12 + (month - 1)

            if birth_year and birth_month:
                if month >= birth_month:
                    age = year - birth_year
                else:
                    age = year - birth_year - 1
            else:
                age = year - FALLBACK_BIRTH_YEAR

            before_val = None
            after_val = None
            for bp in breakpoints:
                if bp.date <= (year, month):
                    if bp.before_tax is not None:
                        before_val = bp.before_tax
                    if bp.after_tax is not None:
                        after_val = bp.after_tax
                else:
                    break

            if earliest_start and (year, month) >= earliest_start:
                if before_val is None:
                    for bp in breakpoints:
                        if bp.before_tax is not None:
                            before_val = bp.before_tax
                            break
                if after_val is None:
                    for bp in breakpoints:
                        if bp.after_tax is not None:
                            after_val = bp.after_tax
                            break

            before_str = str(before_val) if before_val is not None else ''
            after_str = str(after_val) if after_val is not None else ''

            f_formula = f'=cost(C{r})'

            if earliest_start and (year, month) < earliest_start:
                g_formula = '0'
            else:
                g_formula = f'=E{r}-F{r}'

            if month == 12:
                jan_row = r - 11
                h_formula = f'=SUM(G{jan_row}:G{r})'
            else:
                h_formula = ''

            row_within_all = year_idx * 12 + (month - 1)
            if row_within_all < num_years:
                summary_year = start_year + row_within_all
                summary_year_idx = row_within_all
                if birth_year and birth_month:
                    j_val = str(summary_year - birth_year)
                else:
                    j_val = str(summary_year - FALLBACK_BIRTH_YEAR)
                k_val = str(summary_year)
                dec_row = first_data_row + summary_year_idx * 12 + 11
                l_formula = f'=H{dec_row}'
                if summary_year_idx == 0:
                    m_formula = f'=$L{r}'
                    o_formula = f'=IF($L{r}<0;$L{r};0)'
                    q_formula = f'=IF($L{r}<0;$L{r};$L{r}/2)'
                else:
                    prev_row = r - 1
                    m_formula = f'=M{prev_row}+$L{r}'
                    o_formula = f'=O{prev_row}+IF($L{r}<0;$L{r};0)'
                    q_formula = f'=Q{prev_row}+IF($L{r}<0;$L{r};$L{r}/2)'
                n_formula = f'=$N${sparande_row}+$M{r}'
                p_formula = f'=$N${sparande_row}+O{r}'
                r_formula = f'=$N${sparande_row}+Q{r}'
                right_cols = (
                    f'{j_val}\t{k_val}\t{l_formula}\t{m_formula}\t{n_formula}'
                    f'\t{o_formula}\t{p_formula}\t{q_formula}\t{r_formula}'
                )
            else:
                right_cols = '\t' * 8

            out.append(
                f"{year}\t{month}\t{age}\t{before_str}\t{after_str}\t"
                f"{f_formula}\t{g_formula}\t{h_formula}\t\t{right_cols}"
            )

    tsv_output = '\n'.join(out)
    pyperclip.copy(tsv_output)

    print(f"\nExcel rows: ages_row={ages_row}, costs_row={costs_row}, first_data={first_data_row}")
    print(f"Generated {12 * num_years} monthly rows ({start_year}-01 to {end_year}-12)")
    print("Result copied to clipboard - ready to paste into Excel!")


if __name__ == "__main__":
    main()
