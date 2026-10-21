# research-data branch

Machine-managed state for the research agent: a consistent SQLite snapshot serialized as
partitioned, gzip-compressed NDJSON (`state/`), containing ingestion checkpoints, the analysis
queue, evaluation cache, trend history and recommendation history. Unchanged partitions are
byte-identical, so Git stores them once.

Do not edit by hand. Restore locally with `research-agent state restore`.
