# Verified ESG Metric Annotations

`verified_metrics_v1.jsonl` is the primary-source annotation layer for the structured evaluation subset.

## Design principles

- One line represents one company-year-metric fact.
- `normalized_value` is stored in `normalized_unit`; the original report representation is preserved in `raw_value` and `raw_unit`.
- `source_file`, `pdf_page`, and `excerpt` provide provenance.
- `organizational_boundary` and `reporting_basis` prevent unsafe comparisons across changing scopes.
- Restated historical values from a later report are preferred when the later report explicitly says the earlier values were adjusted.
- Combined Scope 1+2 values are not split or guessed.
- A missing value is never treated as zero.

## Review status

The current records were transcribed through machine-assisted primary-source review and therefore set:

```text
review_status = machine_assisted_primary_source_review
needs_second_reviewer = true
```

Before using the dataset as an independently human-labelled dissertation gold set, a second reviewer must inspect the cited PDF page and change `needs_second_reviewer` to `false` with reviewer metadata. The data can already be used as an auditable engineering seed, but it must not be described as independently human annotated yet.

## Scope

Version 1 concentrates on Scope 1 and Scope 2 emissions in the automotive/new-energy sector. It intentionally records boundary warnings for CATL, Changan, Gotion and Huayou, where the report scope changes or is narrower than the consolidated group.
