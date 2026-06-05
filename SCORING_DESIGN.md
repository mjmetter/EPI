# EPI Scoring Design

This document records the rationale behind the EPI metric definitions, band thresholds, and scoring mechanics. It answers the recurring question: *"What does each band mean, and how are metric scores computed?"*

> **Apr 2026:** The composite EPI score (0–100, computed by combining 13 metrics through 4 weighted components) was retired. The `score-epi` pipeline now returns per-metric results — each metric's value, band, and month-over-month change is reported individually. No single number collapses the nuance. The band thresholds and scoring mechanics below are unchanged.

---

## Band thresholds

Bands map metric values to scores: **Elite (90–100)**, **Happy (70–89)**, **Acceptable (50–69)**, **Concerning (0–49)**.

| Metric | Concerning | Acceptable | Happy | Elite |
|---|---|---|---|---|
| Deployment frequency (releases/month) | < 1 | 1–4 | 4–8 | ≥ 8 |
| MRs per engineer (per month) | < 2 | 2–4 | 4–8 | ≥ 8 |
| Features shipped (per month) | < 5 | 5–15 | 15–30 | ≥ 30 |
| Commit intentionality (%) | < 25% | 25–40% | 40–55% | ≥ 55% |
| Change failure rate (%) | > 30% | 15–30% | 5–15% | < 5% |
| Post-release defect rate (%) | > 35% | 20–35% | 10–20% | < 10% |
| Rework rate (%) | > 35% | 25–35% | 15–25% | < 15% |
| Lead time (days) | > 21 | 7–21 | 3–7 | < 3 |
| Cycle delivery accuracy (%) | < 55% | 55–70% | 70–85% | ≥ 85% |
| MTTR (hours) | > 12h | 4–12h | 1–4h | < 1h |
| Bus factor | ≤ 1 | 2 | 3 | ≥ 4 |
| Knowledge distribution (%) | < 40% | 40–60% | 60–80% | ≥ 80% |

**Threshold sources:**
- **Deployment frequency, lead time, CFR, MTTR**: aligned with DORA 2024 industry benchmarks (elite performers ship multiple times per day; we use monthly granularity so bands are compressed accordingly).
- **MRs per engineer, rework rate**: calibrated from 14 months of real-world git data across four product groups.
- **Commit intentionality**: pre-AI baseline from the same 14-month dataset. Bands are expected to shift upward as AI adoption matures — Elite threshold of 55% reflects early-AI maturity, not a ceiling.
- **Cycle delivery accuracy**: 85% Elite threshold is intentionally demanding. Teams that routinely hit 85%+ have genuinely reliable sprint forecasting; 70–85% (Happy) reflects normal healthy variance.
- **Bus factor, knowledge distribution**: set from first principles. A bus factor of 1 is objectively a risk regardless of team size; Elite (≥ 4) represents a team where knowledge loss from any individual departure is manageable.

---

## Scoring mechanics

**Linear interpolation:** scores within a band are interpolated linearly between the band's min and max score. A metric at the midpoint of the Happy band scores ~79, not 70 or 89.

**Confidence weighting (extract_metric_values):** metrics with small sample sizes (e.g., cycle_delivery_accuracy with only 3 committed items gets confidence 3/15 = 0.2) have their effective weight reduced. This is recorded in the raw metrics output for transparency but no longer affects a composite score — it exists as an audit trail.

**Missing data:** null metrics are excluded from the per-metric output dict. If a metric cannot be computed (no git data, no manual input), it simply does not appear in `score_product()["metrics"]`.

---

## Metric groupings (informational)

These groupings informed the former component weights (retired in Apr 2026). They are preserved here as a conceptual guide to which metrics measure related things — useful when reading per-metric output side by side.

| Group | Metrics |
|---|---|
| Delivery Velocity | Deployment frequency, MRs per engineer, Features shipped, Commit intentionality |
| Delivery Quality | Change failure rate, Post-release defect rate, Rework rate |
| Engineering Efficiency | Lead time, Cycle delivery accuracy |
| Engineering Health | MTTR, Bus factor, Knowledge distribution |

---

## Revision history

| Date | Change | Reason |
|---|---|---|
| Feb 2026 | Initial 4-component model established | First board-facing EPI report |
| Mar 2026 | Added `commit_intentionality` to Delivery Velocity; rebalanced sub-weights | AI adoption is changing commit patterns; raw MR/commit volume is no longer sufficient as a velocity proxy |
| Apr 2026 | This document created | Recurring stakeholder questions about weight rationale |
| Apr 2026 | Composite EPI score retired; per-metric output only | Stakeholders wanted metric-level transparency instead of a single collapsed number |
