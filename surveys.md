# IPEDS Surveys

IPEDS data is collected through several surveys throughout the year. Each survey collects specific data on institutions, students, and educational offerings. The survey descriptions below hint at what the tables belonging to that survey contain, so they can be used to decide which survey to explore further instead of bloating the agent's context with all table descriptions.

An efficient way to get relevant context would be to select relevant surveys based on the user's question to explore further and then request table descriptions within those surveys. Table descriptions can then be used to decide what tables are necessary to answer the user's question. If table descriptions are not sufficient, the agent can also get schema information for each table to understand the data structure and fields. Since column names can be short forms of field names, the schema information can be used to map column names to their full descriptions.

Each entry lists the survey, what it covers, and the breakdowns (dimensions) its tables are typically reported by.

## Fall Collection

### Institutional Characteristics (IC)

Institution directory and profile: name, address, location/geography, control (public / private nonprofit / for-profit), level, degree-granting status, calendar system, mission, educational offerings, distance education availability, open vs. selective admission, student services, services for servicemembers and veterans, athletic association, and library screening questions.

Use for: identifying/filtering institutions, joining institution attributes onto any other survey, "what kind of institution is this" questions. Also determines which other IPEDS components apply to an institution.

### Completions (C)

Degrees and other recognized postsecondary credentials conferred July 1–June 30.

Breakdowns: award level, 6-digit CIP code (2020 CIP) and 2-digit CIP summaries, race/ethnicity, gender, distance-education program flag, and multiple-major degrees. Also includes counts of *students* receiving credentials by gender, race/ethnicity, age, and award level.

Use for: degrees awarded, program/major mix, credentials by field of study.

### 12-month Enrollment (E12)

Unduplicated headcount and instructional activity for the 12-month period July 1–June 30 (contrast with EF, which is a fall-census snapshot).

- Part A: unduplicated headcount by race/ethnicity, gender, student level (undergrad/grad), enrollment status (full-/part-time), first-time / transfer-in / continuing / degree-seeking status, and distance-education participation (exclusively / some / none).
- Part B: instructional activity (credit and contact hours) used to derive undergraduate and graduate FTE, plus separate FTE for doctor's degrees–professional practice students.
- Part C: unduplicated count of dual-enrolled high school students by gender and race/ethnicity.

Use for: annual enrollment totals, FTE, dual enrollment.

### Cost I (CST)

Student charges and cost of attendance (COA).

Covers: tuition and required fees, food/meal plans, housing for all undergraduates; tuition and fees for part-time undergraduates and full- and part-time graduate students; full COA (tuition and fees, books and supplies, food and housing, other expenses) for full-time, first-time degree/certificate-seeking undergraduates.

Note: full-time undergraduate charges are averages across all full-time undergraduates and may differ from COA, which covers only full-time, first-time degree/certificate-seeking students. Reopens in winter (see Cost II) to add aid data for average net price.

Use for: tuition, sticker price, cost of attendance.

## Winter Collection

### Admissions (ADM)

Admissions selectivity for entering first-time degree/certificate-seeking undergraduates, most recent fall term: admissions considerations (secondary school records, test scores, etc.), number applied, number admitted, number enrolled, and admission test score distributions.

Collected only from institutions without an open admissions policy.

Use for: acceptance/yield rates, selectivity, test scores.

### Graduation Rates (GR)

150%-of-normal-time completion for a cohort of full-time, first-time degree/certificate-seeking undergraduates.

Covers: cohort size by race/ethnicity and gender, completers within 150% and within 100% of normal time, transfer-outs, and cohort exclusions. Also reports 150% completions for two subcohorts: Pell Grant recipients, and Direct Subsidized Loan recipients who did not receive a Pell Grant.

Cohort timing: 4-year institutions report the cohort that entered 6 years ago; 2-year and less-than-2-year institutions, 3 years ago. Standard-term institutions use a fall cohort; others use a full 12-month (Sep 1–Aug 31) cohort.

Use for: graduation rates, Student Right-to-Know compliance figures.

### 200 Percent Graduation Rates (GR200)

Extends GR for the same cohort out to 200% of normal time. Carries forward prepopulated GR values (cohort size, 100% and 150% completers, exclusions) and adds completers between 151% and 200% of normal time plus additional exclusions.

Cohort timing: 4-year institutions, students who entered 8 years ago (bachelor's-degree-seeking only); less-than-4-year institutions, 4 years ago (entire degree/certificate-seeking cohort).

Use for: long-window completion rates.

### Outcome Measures (OM)

Award and enrollment status for **all** entering undergraduate degree/certificate-seeking students (not just full-time, first-time), from degree-granting institutions. Cohort entry window is July 1–June 30.

Four cohorts — full-time first-time, part-time first-time, full-time non-first-time, part-time non-first-time — each split by Pell recipient status (8 subcohorts total).

Measured: highest award earned at 4, 6, and 8 years after entry; at 8 years, also still enrolled at the reporting institution, subsequently enrolled elsewhere, status unknown, or excluded (death/permanent disability, military service, federal aid service such as the Peace Corps, official church missions).

Use for: outcomes of part-time and transfer students that GR excludes.

### Student Financial Aid (SFA)

Section 1: number of undergraduates awarded aid and amounts awarded, by source of aid, split into grant/scholarship aid vs. loan aid, with emphasis on full-time, first-time degree/certificate-seeking students; non-degree/non-certificate-seeking students reported separately.

Section 2: military and veteran educational benefits (Tuition Assistance Program, Post-9/11 GI Bill) for servicemembers, veterans, and eligible dependents.

Use for: financial aid awards, aid amounts, grant vs. loan mix.

### Cost II (CST)

Winter reopening of the CST component. Adds counts and awarded aid amounts for two subcohorts of full-time, first-time degree/certificate-seeking undergraduates — those awarded any grant aid and those awarded Title IV aid — which combine with COA to produce average net price (ANP). COA elements may be updated in winter; fall student charges may not.

Use for: average net price, net price by income bracket.

## Spring Collection

### Academic Libraries (AL)

Library collections, circulation, interlibrary loans, staffing, and expenditures for the fiscal year (most recent 12-month period ending before October 1). Applicability is set by screening questions in IC.

- Section I (any library expenditures > $0): interlibrary loans, collections and circulation for physical books, media, and serials and for digital/electronic books (including government documents), databases, media, and serials; number and type of library staff.
- Section II (total library expenditures > $100,000): staff wages and fringe benefits, materials and service costs, operations and maintenance.

Use for: library holdings, library spending.

### Fall Enrollment (EF)

Fall census snapshot, six parts. Traditional-calendar institutions report as of the official fall reporting date or October 15; other calendars report students enrolled August 1–October 31.

- Part A: enrollment by race/ethnicity, gender, attendance status (full-/part-time), and student level — first-time degree/certificate-seeking undergrad, degree/certificate-seeking undergrad, total undergrad, total graduate. Also distance education (exclusively / some / none) by level, degree-seeking status, and student residence location (same state, different state, outside the U.S., unknown).
- Part B: enrollment by age category, gender, and attendance status (required in odd-numbered fall years, optional in even).
- Part C: residence (state/jurisdiction) of first-time degree/certificate-seeking undergraduates and how many completed high school in the last 12 months (required in even-numbered fall years, optional in odd).
- Part D: total undergraduates entering for the first time in the fall, degree-seeking or not, including full-time first-time, part-time first-time, and transfer-ins.
- Part E: retention rates — share of the prior fall's first-time students who returned the following fall. 4-year institutions report full-time and part-time first-time bachelor's-seeking separately; less-than-4-year institutions report all first-time degree/certificate-seeking students.
- Part F: estimated undergraduate student-to-faculty ratio.

Use for: point-in-time enrollment, demographics, residency/migration, retention, student-faculty ratio.

### Finance (F)

Institutional financial status for the most recent fiscal year ending before October 1: revenues and expenses by type, changes in net position, scholarships and fellowships, endowment assets, pensions, and financial health indicators.

Form varies by control and accounting standard, so tables differ across institution types:

- Public, GASB standards: financial position (A), revenues (B), expenses by functional and natural classification (C), changes in net position (D), scholarships and fellowships (E), endowment assets (H), pensions (M), financial health (N), plus U.S. Census Bureau items — revenues (J), expenditures (K), debts and assets (L).
- Private nonprofit and FASB-reporting public: financial position (A), changes in net assets (B), scholarships and fellowships (C), revenues by source (D), expenses by functional and natural classification (E), endowment assets (H), financial health (I).
- Private for-profit: balance sheet (A), changes in equity (B), scholarships/fellowships and discounts and allowances (C), revenues by source (D), expenses by functional and natural classification (E), income tax expenses (F), financial health (G). Restricted/unrestricted revenue status is not collected.

Use for: revenues, expenses, endowments, institutional finances — check the institution's control before picking a table.

### Human Resources (HR)

Staff on the institution's payroll as of November 1, in eight parts (A–H).

- Part A: full-time instructional staff by tenure status, academic rank, race/ethnicity, gender.
- Part B: full-time noninstructional staff by occupational category, tenure status, race/ethnicity, gender.
- Part C: full-time staff summary (calculated from A and B).
- Part D: part-time staff by occupational category, race/ethnicity, gender.
- Part E: part-time staff by occupational category, tenure status, medical school status.
- Part F: part-time staff summary (calculated from D and E).
- Part G: salary outlays for full-time nonmedical instructional staff by length of contract and occupational category.
- Part H: newly hired full-time permanent staff by tenure status, race/ethnicity, gender.

Coverage varies: degree-granting institutions with 15+ full-time staff complete all parts; those with fewer than 15 complete Part G plus modified A, B, D, and E without academic rank or tenure status; non-degree-granting institutions complete only the modified A, B, D, and E.

Use for: staff counts, faculty tenure and rank, salaries, new hires.
