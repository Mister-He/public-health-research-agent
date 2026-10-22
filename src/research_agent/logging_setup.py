"""Human-readable console logs + machine-readable JSON lines, with secret redaction."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

_SECRET_ENV = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "NCBI_API_KEY", "OPENALEX_API_KEY", "DEEPSEEK_API_KEY",
               "SEMANTIC_SCHOLAR_API_KEY", "GITHUB_TOKEN", "GH_TOKEN")
_PATTERNS = [re.compile(r"(api_key|apikey|token|authorization)(=|:\s*)([^\s&\"']+)", re.I),
             re.compile(r"sk-ant-[A-Za-z0-9_\-]+"), re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")]
_STD = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime"}


def redact(text: str) -> str:
    for name in _SECRET_ENV:
        v = os.environ.get(name)
        if v and len(v) >= 8:
            text = text.replace(v, "***")
    text = _PATTERNS[0].sub(lambda m: f"{m[1]}{m[2]}***", text)
    for p in _PATTERNS[1:]:
        text = p.sub("***", text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"), "level": record.levelname,
                   "logger": record.name, "msg": record.getMessage()}
        payload.update({k: v for k, v in vars(record).items() if k not in _STD})
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return redact(json.dumps(payload, default=str, ensure_ascii=False))


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        extra = {k: v for k, v in vars(record).items() if k not in _STD}
        tail = (" " + " ".join(f"{k}={v}" for k, v in extra.items())) if extra else ""
        return redact(f"{record.levelname:<7} {record.name.removeprefix('research_agent.')}: {record.getMessage()}{tail}")


def setup_logging(log_dir: Path | None, verbose: bool = False) -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    ch = logging.StreamHandler()
    ch.setFormatter(ConsoleFormatter())
    root.addHandler(ch)
    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_dir / "run.jsonl", encoding="utf-8")
        fh.setFormatter(JsonFormatter())
        root.addHandler(fh)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
