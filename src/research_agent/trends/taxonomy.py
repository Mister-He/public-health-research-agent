"""Trend-tag taxonomy: config/trends.yaml is authoritative for user decisions (status, pinned,
manual_override, names, match rules); the DB mirrors it and stores statistics, history and
Claude proposals. User operations edit the YAML (versioned in Git) and log a tag event."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from research_agent.metadata.normalization import normalize_text_for_match, phrase_in
from research_agent.settings import Settings, TrendTag
from research_agent.storage.database import Database, dumps
from research_agent.storage.repositories import json_list, log_tag_event
from research_agent.timeutil import iso_utc

HEADER = """# Trend taxonomy and trend-estimation parameters.
# Tags here are USER-AUTHORITATIVE: status/pinned/manual_override are never changed
# automatically. Claude proposals are stored separately (DB table tag_proposals) until
# accepted with `research-agent tags accept <proposal_id>`.
# Seed tags are provisional: they are NOT empirically validated trends.
"""


def tag_matches(tag: TrendTag, normalized_text: str) -> bool:
    terms = [[tag.name]] + [[a] for a in tag.aliases] + tag.match
    return any(conj and all(phrase_in(p, normalized_text) for p in conj) for conj in terms)


def lexical_tags_for(text: str, tags: list[TrendTag]) -> dict[str, float]:
    nt = normalize_text_for_match(text)
    return {t.id: 1.0 for t in tags if t.status != "archived" and tag_matches(t, nt)}


def sync_tags(db: Database, settings: Settings) -> None:
    """Mirror config tags into topic_tags; config wins for user-controlled fields."""
    now = iso_utc()
    ids = set()
    for t in settings.trends.tags:
        ids.add(t.id)
        row = db.one("SELECT * FROM topic_tags WHERE id=?", (t.id,))
        if row is None:
            db.execute("INSERT INTO topic_tags(id, name, description, status, pinned, seed, origin, aliases, parent_tags,"
                       " match_rules, manual_override, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (t.id, t.name, t.description, t.status, int(t.pinned), int(t.seed),
                        "config", dumps(t.aliases), dumps(t.parent_tags), dumps(t.match), t.manual_override,
                        t.created_at or now, now))
            log_tag_event(db, t.id, "created", {"from": "config"})
            continue
        changes = {}
        for col, val in (("name", t.name), ("description", t.description), ("status", t.status),
                         ("pinned", int(t.pinned)), ("aliases", dumps(t.aliases)), ("parent_tags", dumps(t.parent_tags)),
                         ("match_rules", dumps(t.match)), ("manual_override", t.manual_override)):
            if row[col] != val:
                changes[col] = val
        if changes:
            db.execute(f"UPDATE topic_tags SET {', '.join(f'{k}=?' for k in changes)}, updated_at=? WHERE id=?",
                       (*changes.values(), now, t.id))
            log_tag_event(db, t.id, "updated_from_config", {k: str(v)[:200] for k, v in changes.items()})
    for row in db.query("SELECT id FROM topic_tags WHERE status != 'merged'"):
        if row["id"] not in ids:
            db.execute("UPDATE topic_tags SET status='archived', updated_at=? WHERE id=?", (now, row["id"]))
            log_tag_event(db, row["id"], "archived", {"reason": "removed from config"})


# --------------------------------------------------------------------- YAML editing (user ops)

def _path(settings: Settings) -> Path:
    return settings.root / "config" / "trends.yaml"


def _load(settings: Settings) -> dict:
    return yaml.safe_load(_path(settings).read_text(encoding="utf-8"))


def _save(settings: Settings, data: dict) -> None:
    data["taxonomy_version"] = int(data.get("taxonomy_version", 0)) + 1
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=110)
    tmp = _path(settings).with_suffix(".yaml.tmp")
    tmp.write_text(HEADER + body, encoding="utf-8")
    tmp.replace(_path(settings))


def _find(data: dict, tag_id: str) -> dict:
    for t in data["tags"]:
        if t["id"] == tag_id:
            return t
    raise KeyError(f"unknown tag {tag_id!r}")


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.casefold()).strip("_")[:48]


def add_tag(settings: Settings, name: str, description: str, match: list[list[str]], *, tag_id: str | None = None,
            status: str = "active", pinned: bool = False) -> str:
    data = _load(settings)
    tid = tag_id or slug(name)
    if any(t["id"] == tid for t in data["tags"]):
        raise ValueError(f"tag {tid!r} already exists")
    TrendTag(id=tid, name=name, description=description, match=match, status=status, pinned=pinned)  # validate
    data["tags"].append({"id": tid, "name": name, "description": description, "aliases": [], "parent_tags": [],
                         "match": match, "status": status, "pinned": pinned, "seed": False,
                         "created_at": iso_utc()})
    _save(settings, data)
    return tid


def update_tag(settings: Settings, tag_id: str, **fields) -> None:
    data = _load(settings)
    t = _find(data, tag_id)
    for k, v in fields.items():
        if k not in ("name", "description", "status", "pinned", "manual_override", "aliases", "match", "parent_tags"):
            raise ValueError(f"field {k!r} is not editable")
        t[k] = v
    TrendTag.model_validate({k: v for k, v in t.items()})
    _save(settings, data)


def merge_tags(settings: Settings, source_id: str, target_id: str) -> None:
    """Merge `source` into `target`: target inherits source's name as alias and its match rules;
    source is archived (its history remains in the DB)."""
    data = _load(settings)
    src, tgt = _find(data, source_id), _find(data, target_id)
    tgt.setdefault("aliases", [])
    tgt["aliases"] = sorted(set(tgt["aliases"]) | {src["name"], *src.get("aliases", [])})
    tgt["match"] = tgt.get("match", []) + [m for m in src.get("match", []) if m not in tgt.get("match", [])]
    src["status"] = "archived"
    src["description"] = (src.get("description") or "") + f" [merged into {target_id}]"
    _save(settings, data)


def accept_proposal(db: Database, settings: Settings, proposal_id: int, *, tag_id: str | None = None) -> str:
    row = db.one("SELECT * FROM tag_proposals WHERE id=?", (proposal_id,))
    if row is None or row["status"] != "proposed":
        raise ValueError(f"proposal {proposal_id} not found or already decided")
    tid = add_tag(settings, row["name"], row["description"], json_list(row["match_rules"]), tag_id=tag_id,
                  status="watchlist")
    db.execute("UPDATE tag_proposals SET status='accepted', decided_at=? WHERE id=?", (iso_utc(), proposal_id))
    log_tag_event(db, tid, "accepted_proposal", {"proposal_id": proposal_id})
    return tid


def reject_proposal(db: Database, proposal_id: int) -> None:
    db.execute("UPDATE tag_proposals SET status='rejected', decided_at=? WHERE id=? AND status='proposed'",
               (iso_utc(), proposal_id))
