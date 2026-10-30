from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path) -> None:
    """Literal dotenv subset: no execution, interpolation or shell expansion."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key.strip()):
            raise ValueError("Некорректная строка в .env")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def canonical(url: str) -> str:
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in
             {"fbclid", "gclid", "ref", "ref_src"}]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(),
                       parts.path.rstrip("/") or "/", urlencode(sorted(query)), ""))


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def normalized(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


CAPTION_LIMIT = 1024


def caption_length(markup: str) -> int:
    """Telegram measures visible caption text, not HTML tags or link URLs."""
    return utf16_len(html.unescape(re.sub(r"<[^>]*>", "", markup)))


def fits_caption(markup: str) -> bool:
    return caption_length(markup) <= CAPTION_LIMIT


class Settings:
    def __init__(self, root: Path = ROOT):
        self.root = root
        load_env(root / ".env")
        self.config = json.loads((root / "config.json").read_text(encoding="utf-8"))
        self.sources = [s for s in self.config["sources"] if s.get("enabled", True)]
        self.terms = json.loads((root / "glossary.json").read_text(encoding="utf-8"))
        self.editorial = (root / "EDITORIAL_RU.txt").read_text(encoding="utf-8")
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.provider = os.getenv("MODEL_PROVIDER", "openai").strip().lower()
        if self.provider not in {"openai", "gemini", "opencode"}:
            raise ValueError("MODEL_PROVIDER должен быть openai, gemini или opencode")
        self.key_variable = "GEMINI_API_KEY" if self.provider == "gemini" else "OPENAI_API_KEY"
        self.api_key = os.getenv(self.key_variable, "").strip()
        self.model = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip() if self.provider == "gemini" else (
            os.getenv("OPENAI_MODEL", "gpt-5.4-mini").strip())
        self.request_gap = 15 if self.provider == "gemini" else 0
        self.opencode_binary = os.getenv("OPENCODE_BIN", "/usr/local/bin/opencode").strip()
        self.opencode_timeout = int(os.getenv("OPENCODE_TIMEOUT", "180"))
        if self.provider == "opencode":
            self.model = os.getenv("OPENCODE_MODEL", "opencode/deepseek-v4.1-flash-free").strip()
            self.api_key = ""
            self.key_variable = ""
            self.request_gap = 15
        self.model_ready = self.provider == "opencode" or bool(self.api_key)
        self.admins = {int(v) for v in os.getenv("ADMIN_USER_IDS", "").split(",") if v.strip()}
        self.review_chat = os.getenv("REVIEW_CHAT_ID", "").strip()
        self.channel = os.getenv("PUBLISH_CHANNEL_ID", "").strip()
        self.allow_auto = os.getenv("ALLOW_AUTOPOST", "false").lower() == "true"
        self.data_dir = Path(os.getenv("DATA_DIR", str(root / "data")))
        self.zone = ZoneInfo(self.config.get("timezone", "Europe/Moscow"))
        self.interval = max(300, int(self.config.get("poll_seconds", 900)))
        self.daily_limit = min(20, max(1, int(self.config.get("max_model_calls_per_day", 20))))
        self.max_scan = max(1, int(self.config.get("max_articles_per_scan", 10)))
        self.hosts = {host for s in self.sources for host in s["allowed_hosts"]}
        for source in self.sources:
            if source["type"] not in {"rss", "listing", "noob_board"}:
                raise ValueError("Неизвестный тип источника")
            if source["type"] == "noob_board" and (source["allowed_hosts"] != ["www.noob-club.ru"]
                    or source["url"] != "https://www.noob-club.ru/index.php?board=268.0"):
                raise ValueError("Разрешён только раздел новостей Classic и Forever Noob Club")
            if source.get("official", False) and not set(source["allowed_hosts"]) <= {
                "news.blizzard.com", "worldofwarcraft.blizzard.com"
            }:
                raise ValueError("Автопостинг разрешён только для новостных доменов Blizzard")
        # Defaults to the first administrator's private chat, no private channel needed.
        self.destination = self.review_chat or (str(min(self.admins)) if self.admins else "")


class Store:
    def __init__(self, path: str | Path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA busy_timeout=5000;
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS articles (
              url TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT NOT NULL,
              body TEXT NOT NULL DEFAULT '', body_hash TEXT NOT NULL DEFAULT '',
              seen_at REAL NOT NULL, failure_hash TEXT NOT NULL DEFAULT '',
              failures INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS drafts (
              id INTEGER PRIMARY KEY, source TEXT NOT NULL, source_url TEXT NOT NULL,
              source_title TEXT NOT NULL, content_hash TEXT NOT NULL,
              title TEXT NOT NULL, body TEXT NOT NULL, kind TEXT NOT NULL,
              needs_review INTEGER NOT NULL, notes TEXT NOT NULL, evidence TEXT NOT NULL,
              state TEXT NOT NULL DEFAULT 'draft', revision INTEGER NOT NULL DEFAULT 1,
              delivery TEXT NOT NULL DEFAULT 'new', review_chat TEXT, review_message INTEGER,
              public_message INTEGER, created_at REAL NOT NULL,
              image_url TEXT NOT NULL DEFAULT '', review_is_photo INTEGER NOT NULL DEFAULT 0,
              UNIQUE(source_url, content_hash)
            );
            CREATE TABLE IF NOT EXISTS edit_sessions (
              user_id INTEGER PRIMARY KEY, chat_id TEXT NOT NULL, prompt_id INTEGER NOT NULL,
              draft_id INTEGER NOT NULL, expires REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS usage (
              day TEXT PRIMARY KEY, calls INTEGER NOT NULL DEFAULT 0,
              input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0
            );
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(drafts)")}
        if "image_url" not in columns:
            self.db.execute("ALTER TABLE drafts ADD COLUMN image_url TEXT NOT NULL DEFAULT ''")
        if "review_is_photo" not in columns:
            self.db.execute("ALTER TABLE drafts ADD COLUMN review_is_photo INTEGER NOT NULL DEFAULT 0")
        # A process may have stopped after Telegram accepted a message but before commit.
        self.db.execute("UPDATE drafts SET state='unknown' WHERE state='publishing'")
        self.db.execute("UPDATE drafts SET delivery='unknown' WHERE delivery='sending'")
        self.db.commit()

    def execute(self, sql, params=()):
        with self.lock, self.db:
            return self.db.execute(sql, params)

    def one(self, sql, params=()):
        with self.lock:
            row = self.db.execute(sql, params).fetchone()
            return dict(row) if row else None

    def all(self, sql, params=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, params).fetchall()]

    def get(self, key: str, default="") -> str:
        row = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return row["value"] if row else default

    def set(self, key, value):
        self.execute("INSERT INTO kv VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, str(value)))

    def remember(self, article, body=""):
        self.execute("""INSERT INTO articles(url,source,title,body,body_hash,seen_at)
            VALUES(?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET title=excluded.title,
            body=excluded.body, body_hash=excluded.body_hash, failure_hash='',
            failures=0, retry_at=0""",
                     (article["url"], article["source"], article["title"], body,
                      digest(body) if body else "", time.time()))

    def can_attempt(self, url, content_hash):
        row = self.one("SELECT * FROM articles WHERE url=?", (url,))
        if not row or row["failure_hash"] != content_hash:
            return True
        return row["failures"] < 5 and row["retry_at"] <= time.time()

    def fail(self, article, content_hash):
        row = self.one("SELECT * FROM articles WHERE url=?", (article["url"],))
        if not row:
            self.remember(article)
        same = row and row["failure_hash"] == content_hash
        count = row["failures"] + 1 if same else 1
        self.execute("""UPDATE articles SET failure_hash=?, failures=?, retry_at=? WHERE url=?""",
                     (content_hash, count, time.time() + min(86400, 900 * 2 ** (count - 1)),
                      article["url"]))

    def reserve_call(self, day: str, maximum: int) -> bool:
        with self.lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO usage(day) VALUES(?)", (day,))
            return self.db.execute("UPDATE usage SET calls=calls+1 WHERE day=? AND calls<?",
                                   (day, maximum)).rowcount == 1

    def add_tokens(self, day, usage):
        self.execute("UPDATE usage SET input_tokens=input_tokens+?, output_tokens=output_tokens+? WHERE day=?",
                     (usage.get("input_tokens", 0), usage.get("output_tokens", 0), day))

    def recent(self):
        return self.all("""SELECT id,title,body,source_title FROM drafts
            WHERE state IN ('draft','editing','published') AND created_at>?
            ORDER BY id DESC LIMIT 25""", (time.time() - 7 * 86400,))

    def draft(self, article, body_hash, result, image_url=""):
        with self.lock, self.db:
            cursor = self.db.execute("""INSERT OR IGNORE INTO drafts
                (source,source_url,source_title,content_hash,title,body,kind,needs_review,
                  notes,evidence,created_at,image_url) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (article["source"], article["url"], article["title"], body_hash,
                 result["title"], result["body"], result["kind"], int(result["needs_review"]),
                 json.dumps(result["review_notes"], ensure_ascii=False),
                  json.dumps(result["evidence"], ensure_ascii=False), time.time(), image_url))
            if cursor.rowcount == 0:
                return None
            return cursor.lastrowid

    def row(self, draft_id):
        return self.one("SELECT * FROM drafts WHERE id=?", (draft_id,))

    def claim_publish(self, draft_id, revision):
        return self.execute("""UPDATE drafts SET state='publishing'
            WHERE id=? AND revision=? AND state='draft'""", (draft_id, revision)).rowcount == 1


LABELS = {
    "official": "", "report": "", "datamining": "Датамайн — информация может измениться",
    "rumor": "Слух — официального подтверждения нет", "community": "Обсуждение сообщества",
}


def render_post(row) -> str:
    label = LABELS.get(row["kind"], "")
    chunks = ([html.escape(label)] if label else []) + [
        "<b>" + html.escape(row["title"]) + "</b>", html.escape(row["body"]),
        '<a href="' + html.escape(row["source_url"], quote=True) + '">Источник: '
        + html.escape(row["source"]) + "</a>"
    ]
    return "\n\n".join(chunks)


def render_preview(row) -> str:
    return render_post(row)


def keyboard(row):
    if row["state"] != "draft":
        return {"inline_keyboard": []}
    key = f'{row["id"]}:{row["revision"]}'
    return {"inline_keyboard": [[
        {"text": "Опубликовать", "callback_data": "p:" + key},
        {"text": "Править", "callback_data": "e:" + key},
        {"text": "Пропустить", "callback_data": "s:" + key},
    ], [{"text": "Оригинал", "url": row["source_url"]}]]}
