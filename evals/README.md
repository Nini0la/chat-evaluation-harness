# Evaluation Suites

Place reviewed, versioned JSONL suites in this directory. ChatEval validates and consumes suites but
does not create clinical cases, training data, expected behaviors, or rubric content.

Each non-empty line is one JSON object. The required v1 fields are `id`, `suite_version`,
`category`, `subcategory`, `prompt`, `expected_behavior`, `required_points`, `forbidden_points`,
`severity`, `source_of_truth`, and `leakage_group`.

Additional top-level metadata fields are accepted, preserved in judged records, and available for
audit or downstream filtering. Optional `metric_tags` provide cross-cutting reporting dimensions
such as `over_refusal`, `under_refusal`, `tiny_assessment`, `danger_sign`, `branch_local`, and
`negation`. Optional deterministic-check fields include `expected_output_mode`,
`required_literals`, `forbidden_literals`, `refusal_expectation`, `min_chars`, and `max_chars`.

Only `prompt` is sent to candidate models. The judge receives only `prompt`, candidate response,
`expected_behavior`, `required_points`, and `forbidden_points`. Provenance and aggregation fields,
including `source_of_truth`, `leakage_group`, `severity`, category fields, `metric_tags`, and other
metadata, are never included in either model's rubric context.

Suite identity is the SHA-256 of the complete file. Keep suites immutable after they are used in a
run; publish corrections under a new versioned filename.
