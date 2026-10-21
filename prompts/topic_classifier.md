<!-- prompt_version: 1.0 | task: topic_classification -->
# Topic classification

Assign `topic_tags`: zero or more tag ids from the manifest's `taxonomy` (active and
watchlist tags). Assign a tag only when the record clearly supports it; give each a
`confidence` in [0, 1] and a short verbatim `evidence` quote.

These are AI-assigned classifications. They are stored separately from author keywords
and MeSH terms and are always labelled as AI-assigned in reports.

If the paper fits a recurring theme that is NOT in the taxonomy, you may add up to two
short phrases in `untagged_themes` (free text, no ids). Do not create tag ids.
