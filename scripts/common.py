"""
Shared helpers for the daily-tech-news skill.
- Config loading (single source of truth = config.yaml)
- Normalized item schema (every source maps into this shape)
- Stable content hashing for dedup
"""
import os
import re
import hashlib
import yaml

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(SCRIPT_DIR)
DEFAULT_CONFIG_PATH = os.path.join(SKILL_DIR, "config.yaml")


def load_global_env():
    """Load global env from ~/.hermes/env (non-interactive shell compatible).
    
    Parses simple KEY=VALUE lines and sets them in os.environ.
    Skips lines that are already set (allow project .env to override).
    """
    env_path = os.path.expanduser("~/.hermes/env")
    if not os.path.isfile(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            match = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)=(.*)', line)
            if match:
                key, value = match.group(1), match.group(2).strip()
                if key not in os.environ:
                    # Strip surrounding quotes if present
                    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
                        value = value[1:-1]
                    elif len(value) >= 2 and value[0] == "'" and value[-1] == "'":
                        value = value[1:-1]
                    os.environ.setdefault(key, value)


# Load global env on import — all scripts that import common.py get access.
load_global_env()


def load_config(path=None):
    """Load the central config. All tunables live here."""
    path = path or DEFAULT_CONFIG_PATH
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def resolve_path(p):
    """Resolve a config-relative path against the skill dir."""
    if os.path.isabs(p):
        return p
    return os.path.normpath(os.path.join(SKILL_DIR, p))


def strip_html(text):
    """Cheap, dependency-free HTML stripping for feed summaries."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def content_hash(*parts):
    """Stable hash used as dedup key. Built from normalized parts."""
    joined = "||".join((p or "").strip().lower() for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def make_item(
    *,
    source,           # source name, e.g. "OpenAI"
    stream,           # "rss" | "following" | "keywords"
    title,
    url,
    published=None,   # ISO8601 string if available
    summary="",       # raw summary/description from the source (may be empty)
    author=None,      # for tweets: the handle
    metrics=None,     # for tweets: dict of engagement counts
):
    """
    Normalize any source's record into one common dict.
    `dedup_key` is derived here so all downstream code agrees on identity.
    """
    title = (title or "").strip()
    url = (url or "").strip()
    # For tweets, url is unique per tweet. For articles, url is the canonical id.
    key = content_hash(url) if url else content_hash(source, title)
    return {
        "dedup_key": key,
        "source": source,
        "stream": stream,
        "title": title,
        "url": url,
        "published": published,
        "raw_summary": strip_html(summary),
        "author": author,
        "metrics": metrics or {},
    }
