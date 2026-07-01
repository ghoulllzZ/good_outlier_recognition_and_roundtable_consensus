# Overleaf project — English submission for Springer *Requirements Engineering* (RE)

English, double-anonymous manuscript prepared for the Springer Nature `sn-jnl` template (RE journal), translated from the current draft with the 5 tables placed per the agreed layout and the model formulas filled in.

## Files
```
main.tex                 # Anonymized manuscript (\documentclass[sn-mathphys-num]{sn-jnl})
title_page.tex           # SEPARATE non-anonymous title page (submit separately)
tables/table_01..05.tex  # Table 1 Dataset & GT, 2 Treatments, 3 Treatment-level Perf, 4 Model Profile, 5 Cases
figures/fig1..5*.png     # 5 figures
README_OVERLEAF_EN.md
```

## How to compile on Overleaf (important)
`sn-jnl.cls` and `sn-basic.bst` are **not** in Overleaf's base TeX Live; they ship with the official Springer template. Do this once:

1. Overleaf → **New Project → Templates** → search **"Springer Nature LaTeX Template"** → create a project from it (this gives you `sn-jnl.cls`, `sn-basic.bst`, `sn-article.tex`, etc.).
2. **Upload** the files from this zip into that project, replacing/adding: `main.tex`, the `tables/` folder, the `figures/` folder, and `title_page.tex`.
3. Set the main document to `main.tex`. Compiler: **pdfLaTeX** (sn-jnl works with pdfLaTeX; the manuscript is English, so XeLaTeX is not required).
4. Recompile.

> Alternatively, open the Springer Nature template directly on Overleaf and paste the body of `main.tex` into its sample article, keeping its preamble.

## RE journal compliance checklist (verified against current submission guidelines)
- **Class**: Springer Nature `sn-jnl`; numeric citations `[1]` (option `sn-mathphys-num`). ✓
- **Abstract**: 150–250 words. ✓ (~210 words)
- **Keywords**: 4–6. ✓ (5)
- **Headings**: decimal, ≤ 3 levels. ✓
- **Double-anonymous**: author/affiliation removed from `main.tex`; real info in `title_page.tex`. ✓
- **Data Availability Statement**: included. ✓
- **Figures**: named `Fig..`/provided as PNG. Note: RE prefers EPS/TIFF at line-art ≥1200 dpi, halftone ≥300 dpi. Replace the PNGs with high-resolution EPS/TIFF before final submission if required.
- **References**: numeric list. NOTE — currently a `thebibliography` list with **titles only**; before submission, complete each entry with authors, year, venue, and DOI, ideally converting to a `.bib` file used with `sn-basic.bst`.

## Items you still need to finalize
1. **Author/affiliation/ORCID/funding/competing interests** in `title_page.tex`; check whether some must be entered in the SNAPP system fields.
2. **References**: fill full bibliographic details (authors/year/venue/DOI).
3. **Table 5 (representative cases)**: replace the bracketed placeholders `[doc--item]`, `[score]/[median]`, `[short requirement excerpt]`, `[evidence span]` with real cases.
4. **Figures**: if RE requires EPS/TIFF resolution, regenerate at the required dpi.
5. Re-check any statistic marked in the text; exact test values already filled where available (RQ1 Kruskal–Wallis $H=10.7601, df=3, p=0.0131$; RQ2 Friedman $\chi^2=2.0400, p=0.5641, W=0.0680$); qualitative significances kept as $p<0.05$ where exact values were not in the source.

## Length
The manuscript is a full-length RE research article; RE sets **no hard page/word limit** (only the 150–250-word abstract). Verbose passages were tightened during translation without removing research content.
