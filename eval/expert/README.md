# Expert consistency experiment pack

This pack is ready for two independent ESG reviewers. It is intentionally not pre-filled with model or synthetic labels: Cohen's kappa, MAE and F1 would be invalid without independent human annotations.

- `disclosure_quality_template.csv`: 15 cases for 1--5 disclosure-quality ratings and dimension labels.
- `claim_evidence_mismatch_template.csv`: 30 claim/evidence items for binary mismatch labels.
- `scripts/analyze_expert_agreement.py`: computes agreement after both reviewers fill the files.

Required order: blind Reviewer A and Reviewer B, lock both files, then run the script. Do not use the agent's own labels as a substitute for either reviewer.
