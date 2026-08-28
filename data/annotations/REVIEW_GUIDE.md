# Second-Reviewer Guide

## Goal

Confirm that each structured fact is supported by the cited primary-source PDF page and that the organizational boundary is described accurately.

## Procedure

For every row in `second_review_template.csv`:

1. Open `source_file` and navigate to `pdf_page` using the PDF viewer's physical page number.
2. Confirm the metric identity: Scope 1, Scope 2, Scope 3 category, or green-loan balance.
3. Confirm the reporting year and table header.
4. Confirm `raw_value` and `raw_unit` from `verified_metrics_v1.jsonl`.
5. Recalculate `normalized_value` only when unit conversion is required.
6. Check whether a later report restates an earlier value.
7. Check the organizational boundary and whether it changed across years.
8. Record `decision` as `accept`, `correct`, or `reject`.

## Decision rules

- **accept**: value, year, metric, unit, page and boundary all agree.
- **correct**: the record is usable after a documented value/page/boundary correction.
- **reject**: the report does not support a sufficiently precise or separable fact.
- Do not split a combined Scope 1+2 value.
- Do not treat a slash, blank cell or unavailable value as zero.
- Prefer an explicitly restated historical value in the latest report, but retain the original-report value in the audit history if the difference is discussed.

## Finalization

After review, apply accepted corrections through a small review-application script or a reviewed patch. Set `needs_second_reviewer=false` only for accepted records, and add reviewer metadata. Preserve the completed CSV as an experiment artefact.
