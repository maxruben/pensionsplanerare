# Beräkningar — så här räknas Kvar och tillväxt

Detaljer om hur `pension_planner.py` räknar fram värdena i tabellerna och
diagrammet. Främst för referens när du undrar varför ett enskilt värde inte
matchar din intuition.

## Tre Kvar-grafer

Varje plan visar tre Kvar-kurvor som modellerar olika antaganden om vad som
händer med varje månads överskott (inkomst efter skatt minus utgifter):

| Kurva | Antagande | Effekt på kvar-saldot |
|---|---|---|
| **Spara allt överskott** (#1) | 100% av månadens överskott läggs till sparandet | Bästa fall — kapitalet bevaras maximalt |
| **Spara inget** (#2) | Överskott "konsumeras", bara underskott dras från sparandet | Sämsta fall — bara underskott äter på kapitalet |
| **Spara hälften av överskottet** (#3) | 50% av överskott sparas; underskott dras fullt | Realistisk mellanvariant |

`Ack diff #1/#2/#3` i tabellen är den **rena summan** av diffar enligt
respektive regel (ingen tillväxt). `Kvar #1/#2/#3` = `Sparande + Ack diff`
*plus* eventuell tillväxt (se nedan).

## Tillväxt — alltid månadsvis sammansättning

När checkboxen **Tillväxt** är ikryssad och en procent är angiven sammansätts
räntan **månadsvis** på kvarvarande saldo. Detta gäller oavsett om
"Tillämpning" står på Månadsvis eller Årsvis.

### Nominell vs effektiv ränta

Inmatningen tolkas som **nominell årsränta (APR)**. Den effektiva årsräntan
blir något högre p.g.a. månadsvis sammansättning:

```
effektiv_årsränta = (1 + nominell/12)^12 - 1
```

| Du anger | Effektiv årsränta |
|---|---|
| 2,0% | 2,018% |
| 2,5% | 2,529% |
| 5,0% | 5,116% |
| 7,0% | 7,229% |

Det är samma konvention som banker använder. Vill du att ditt angivna värde
ska bli **exakt** den effektiva räntan måste du räkna baklänges:
`nominell = 12 × ((1 + effektiv)^(1/12) - 1)`. T.ex. för effektiv 2,0%:
ange ungefär 1,98%.

### Konkret exempel

Om kvar-saldot är 358 805 kr vid årets början och inget överskott/underskott
händer under året, blir kvar-saldot efter ett år:

```
358 805 × (1 + 0,02/12)^12 = 358 805 × 1,02018 = 366 048
```

…inte 358 805 × 1,02 = 365 981 som man kanske intuitivt tänker.

## Tillämpning: Månadsvis vs Årsvis

Detta är synligt under Expert-menyn → "Tillämpning av tillväxt
(årsvis/månadsvis)". Båda modes använder månadsvis sammansättning av räntan
— skillnaden ligger i **när månadens överskott/underskott läggs till
kvar-saldot** och **vilken regel som tillämpas på vad**.

### Månadsvis (standard, mest realistiskt)

För varje månad i tur och ordning:

1. **Sammansätt ränta**: `kvar = kvar × (1 + r/12)`
2. **Lägg på månadens diff** enligt regeln för respektive Kvar-kurva:
   - Kvar #1: `kvar += diff` (alltid)
   - Kvar #2: `kvar += diff` om `diff < 0` (annars inget)
   - Kvar #3: `kvar += diff` om `diff < 0`, annars `kvar += diff / 2`

Regeln utvärderas alltså **per månad**.

### Årsvis (alternativ)

För varje år:

1. **Sammansätt ränta för hela året**: `kvar = kvar × (1 + r/12)^12`
2. **Vid årets slut** lägg på årssumman av diffar enligt regeln:
   - Kvar #1: `kvar += year_sum`
   - Kvar #2: `kvar += year_sum` om `year_sum < 0` (annars inget)
   - Kvar #3: `kvar += year_sum` om `year_sum < 0`, annars `kvar += year_sum / 2`

Regeln utvärderas **per år** (på årssumman).

### Skillnaden i praktiken

Modellerna ger **olika resultat** även utan tillväxt eftersom regeln för
Kvar #2 och #3 utvärderas på olika tidsskalor:

- Ett år med tre underskottsmånader á -2 000 kr och nio överskottsmånader á
  +1 000 kr har `year_sum = +3 000`. Månadsvis räknar Kvar #2 som -6 000
  (de tre underskottsmånaderna); Årsvis räknar 0 (positiv årssumma).

Kvar #1 ger däremot samma slutsumma i båda modes (saldot påverkas av
samtliga diffar oavsett tidsskala). Vid tillväxt > 0 blir Kvar #1 dock
*något högre* i Månadsvis eftersom överskott tjänat i tidiga månader hinner
sammansättas under resten av året.

**Slutsats**: Månadsvis är närmare verkligheten (pension och utgifter
händer faktiskt månadsvis), men ger något lägre värden för Kvar #2 och #3
eftersom underskott äter på kapitalet tidigare. Skillnaden är typiskt några
tusen kronor över decenniumlånga prognoser.

## Inflation

Modellen tar **inte** hänsyn till inflation. Värdena är i nominella kronor
för det framtida året — köpkraften är lägre än samma summa idag.

**Rekommendation**: Tolka tillväxt-procenten som **real avkastning** (efter
inflation). Pension från minpension.se är delvis indexerad via
inkomstindex/balansindex, så att tolka den som "ungefär konstant i dagens
kronor" är en rimlig approximation.

| Antagande | Sätt tillväxt till |
|---|---|
| 5% nominell avkastning, 2% inflation | 3% |
| 2,5% nominell, 2,5% inflation | 0% |
| 7% nominell, 2% inflation | 5% |

Då är "kronor kvar" i dagens kronor och kan direkt jämföras med dagens
utgifter.
