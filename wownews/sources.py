from __future__ import annotations

import html
import ipaddress
import json
import calendar
import re
import socket
import time
from io import BytesIO
import urllib.error
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from .core import canonical, normalized


class FetchError(Exception):
    pass


def validate_url(url, allowed_hosts, resolve=True):
    p = urlsplit(url)
    if (p.scheme != "https" or p.hostname not in allowed_hosts or
            p.username is not None or p.password is not None or p.port not in (None, 443)):
        raise FetchError("URL вне списка разрешённых HTTPS-источников")
    if len(url) > 1600:
        raise FetchError("Слишком длинный URL")
    if resolve:
        try:
            addresses = socket.getaddrinfo(p.hostname, 443, type=socket.SOCK_STREAM)
            if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
                raise FetchError("Источник вернул непубличный IP")
        except OSError:
            raise FetchError("DNS источника недоступен") from None
    return url


class GuardedRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, hosts, resolve=True):
        self.hosts, self.resolve = hosts, resolve

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl, self.hosts, resolve=self.resolve)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Fetcher:
    """No browser impersonation, paywall bypasses or external dependencies."""
    user_agent = "WoWForeverNewsBot/1.0"

    def __init__(self):
        self.robots = {}
        # HTTPS proxies resolve origin names remotely. The host allowlist still applies
        # to every redirect; the proxy operator controls DNS/network policy in that case.
        self.resolve = not bool(urllib.request.getproxies().get("https"))

    def raw(self, url, hosts):
        data, charset = self.raw_bytes(url, hosts, 3_000_000,
                                      "text/html,application/rss+xml,application/atom+xml,application/xml;q=0.9")
        return data.decode(charset, errors="replace")

    def raw_bytes(self, url, hosts, maximum, accept="image/jpeg,image/png,image/webp"):
        validate_url(url, hosts, resolve=self.resolve)
        request = urllib.request.Request(url, headers={
            "User-Agent": self.user_agent,
            "Accept": accept,
        })
        try:
            with urllib.request.build_opener(GuardedRedirect(hosts, self.resolve)).open(request, timeout=25) as response:
                data = response.read(maximum + 1)
                if len(data) > maximum:
                    raise FetchError("Ответ источника превышает лимит")
                return data, (response.headers.get_content_charset() or "utf-8")
        except urllib.error.HTTPError as exc:
            raise FetchError(f"Источник: HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError, LookupError, ValueError):
            raise FetchError("Ошибка подключения к источнику") from None

    def text(self, url, hosts):
        self.check_robots(url, hosts)
        return self.raw(url, hosts)

    def check_robots(self, url, hosts):
        host = urlsplit(url).hostname
        cached = self.robots.get(host)
        if not cached or cached[0] < time.time() - 21600:
            robots_url = f"https://{host}/robots.txt"
            try:
                body = self.raw(robots_url, hosts)
            except FetchError as exc:
                if str(exc) not in {"Источник: HTTP 404", "Источник: HTTP 410"}:
                    raise FetchError("Не удалось проверить robots.txt; источник отложен") from None
                body = "User-agent: *\nDisallow:"
            parser = urllib.robotparser.RobotFileParser()
            parser.parse(body.splitlines())
            cached = (time.time(), parser)
            self.robots[host] = cached
        if not cached[1].can_fetch(self.user_agent, url):
            raise FetchError("robots.txt запрещает автоматический сбор этой страницы")

    def image(self, url, article_url):
        paths = IMAGE_PATHS.get(urlsplit(article_url).hostname, {})
        parts = urlsplit(url)
        if parts.hostname not in paths or not parts.path.startswith(paths[parts.hostname]):
            raise FetchError("Изображение вне разрешённого CDN статьи")
        hosts = {parts.hostname}
        self.check_robots(url, hosts)
        content, _ = self.raw_bytes(url, hosts, 5_000_000)
        try:
            from PIL import Image, ImageOps, UnidentifiedImageError
            Image.MAX_IMAGE_PIXELS = 16_000_000
            with Image.open(BytesIO(content)) as original:
                if original.format not in {"JPEG", "PNG", "WEBP"} or min(original.size) < 250:
                    raise FetchError("Файл не является подходящей иллюстрацией")
                original.thumbnail((1600, 1600))
                image = ImageOps.exif_transpose(original).convert("RGB")
                output = BytesIO()
                image.save(output, format="JPEG", quality=82, optimize=True)
                result = output.getvalue()
                if len(result) > 8_000_000:
                    raise FetchError("Изображение слишком велико для Telegram")
                return result
        except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
            raise FetchError("Изображение не удалось проверить или преобразовать") from None


IMAGE_PATHS = {
    "news.blizzard.com": {"bnetcmsus-a.akamaihd.net": "/cms/"},
    "www.wowhead.com": {"wow.zamimg.com": "/uploads/blog/images/"},
    "www.icy-veins.com": {"static.icy-veins.com": "/wp/wp-content/uploads/"},
    "www.noob-club.ru": {"images.noob-club.ru": "/news/"},
}


def image_from_markup(markup, article_url):
    """Only publisher-supplied article preview images on known editorial CDNs."""
    paths = IMAGE_PATHS.get(urlsplit(article_url).hostname, {})
    document = Document(markup)
    for key in ("og:image", "twitter:image"):
        for node in document.root.walk():
            if node.tag != "meta" or node.attrs.get("property", node.attrs.get("name", "")).lower() != key:
                continue
            url = urljoin(article_url, html.unescape(node.attrs.get("content", "")))
            parts = urlsplit(url)
            if parts.hostname not in paths or not parts.path.startswith(paths[parts.hostname]):
                continue
            try:
                validate_url(url, {parts.hostname}, resolve=False)
            except FetchError:
                continue
            return url
    return ""


def forever_spell_names(markup, fetcher):
    """Resolve only explicit Forever spell IDs; never substitute retail names."""
    result = {}
    if getattr(fetcher, "wowhead_ru_blocked", False):
        return result
    for node in Document(markup).root.walk():
        if node.tag != "a":
            continue
        url = urlsplit(html.unescape(node.attrs.get("href", "")))
        match = re.fullmatch(r"/forever/(?:ru/)?spell=(\d+)(?:/[^/]*)?/?", url.path)
        if url.hostname not in {"www.wowhead.com", "wowhead.com"} or not match:
            continue
        spell_id = match.group(1)
        english = normalized(node.text())
        if not english or not re.fullmatch(r"[A-Za-z][A-Za-z '\-]{2,55}", english):
            continue
        if spell_id in (item["id"] for item in result.values()) or len(result) >= 6:
            continue
        try:
            page = fetcher.text("https://www.wowhead.com/forever/ru/spell=" + spell_id,
                                ["www.wowhead.com"])
        except FetchError as exc:
            if "HTTP 403" in str(exc):
                fetcher.wowhead_ru_blocked = True
                break
            continue
        match_title = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
        if not match_title or "[Forever]" not in html.unescape(match_title.group(1)):
            continue
        russian = normalized(html.unescape(match_title.group(1)).split(" - ", 1)[0])
        if re.search(r"[А-Яа-яЁё]", russian) and len(russian) <= 70:
            result[english] = {"name": russian, "id": spell_id,
                               "url": "https://www.wowhead.com/forever/ru/spell=" + spell_id}
    return result


def launch_times(body, now):
    """Use an explicit UTC column, not a potentially mislabeled regional timezone."""
    rows = [line.split(" | ") for line in body.splitlines()]
    for index, row in enumerate(rows[:-2]):
        if not row or row[-1].strip() != "UTC":
            continue
        clock = rows[index + 1][-1].strip()
        date = rows[index + 2][-1].strip()
        hour = re.fullmatch(r"(\d{1,2}):(\d{2})", clock)
        match = re.fullmatch(r"([A-Za-z]+)\s+(\d{1,2})(?:,?\s+(\d{4}))?\*?", date)
        months = {name.lower(): n for n, name in enumerate(calendar.month_name) if name}
        if not hour or not match or match.group(1).lower() not in months:
            continue
        year = int(match.group(3) or now.year)
        try:
            utc = datetime(year, months[match.group(1).lower()], int(match.group(2)),
                           int(hour.group(1)), int(hour.group(2)), tzinfo=timezone.utc)
        except ValueError:
            continue
        # A year absent from an old article must not turn an archival date into
        # a newly scheduled launch. Fresh current-year announcements are safe.
        if not match.group(3) and abs((utc - now.astimezone(timezone.utc)).days) > 120:
            continue
        return {
            "utc": utc.isoformat(),
            "central_europe": utc.astimezone(ZoneInfo("Europe/Berlin")).isoformat(),
            "moscow": utc.astimezone(ZoneInfo("Europe/Moscow")).isoformat(),
            "year": year,
            "basis": "UTC in article; current year for omitted year" if not match.group(3)
                     else "UTC and year in article",
        }
    return None


VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr"}
DROP = {"script", "style", "nav", "footer", "header", "aside", "form", "noscript", "button"}


@dataclass
class Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def text(self, clean=True):
        if clean and self.tag in DROP:
            return ""
        chunks = [c.text(clean) if isinstance(c, Node) else c for c in self.children]
        if self.tag == "tr":
            return "\n" + " | ".join(c.strip() for c in chunks if c.strip()) + "\n"
        content = "".join(chunks)
        if self.tag in {"p", "div", "li", "h1", "h2", "h3", "h4", "br", "table", "section"}:
            return "\n" + content + "\n"
        return content


class Document(HTMLParser):
    def __init__(self, markup):
        super().__init__(convert_charrefs=True)
        self.root = Node("root")
        self.stack = [self.root]
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def article_body(markup):
    document = Document(markup)
    nodes = list(document.root.walk())
    # Forum topics contain many replies: only the opening editorial post is news.
    if any(node.attrs.get("id") == "forumposts" for node in nodes):
        for node in nodes:
            if (node.tag == "div" and "inner" in node.attrs.get("class", "").split()
                    and re.fullmatch(r"msg_\d+", node.attrs.get("id", ""))):
                text = normalize_body(node.text())
                if len(text) >= 150:
                    return text[:28000]
                raise FetchError("Первая запись темы не содержит текста новости")
        raise FetchError("Не удалось выделить первую запись темы")
    # Prefer explicit publisher-provided article text over navigation-heavy pages.
    for node in nodes:
        if node.tag == "script" and node.attrs.get("type") == "application/ld+json":
            try:
                value = json.loads(node.text(clean=False))
                todo = value if isinstance(value, list) else [value]
                for obj in todo:
                    if isinstance(obj, dict):
                        todo.extend(obj.get("@graph", []))
                        if isinstance(obj.get("articleBody"), str):
                            return normalize_body(Document(obj["articleBody"]).root.text())
            except (ValueError, TypeError):
                pass
    candidates = []
    for node in nodes:
        attrs = " ".join(str(node.attrs.get(k, "")) for k in ("class", "id", "itemprop")).lower()
        if "news-post-content" in attrs:
            # Wowhead embeds article markup as a JSON string argument. Decode data only;
            # do not execute JavaScript or resolve game entity IDs from memory.
            embedded = wowhead_body(node)
            if embedded:
                candidates.append((4, len(embedded), embedded))
        priority = 0
        if re.search(r"article[-_ ]?(body|content)|entry-content|news[-_ ]?(body|content)|articlebody|news-post-content", attrs):
            priority = 3
        elif node.tag == "article":
            priority = 2
        if priority:
            text = normalize_body(node.text())
            if len(text) >= 150:
                candidates.append((priority, len(text), text))
    if not candidates:
        raise FetchError("Текст статьи не выделен; нужен адаптер разметки, заголовок не пересказывается")
    return max(candidates)[2][:28000]


def wowhead_body(node):
    chunks = []
    for child in node.walk():
        if child.tag != "script":
            continue
        script = child.text(clean=False)
        pattern = r'WH\.markup\.printHtml\(\s*("(?:[^"\\]|\\.)*")'
        for match in re.finditer(pattern, script, re.S):
            try:
                value = json.loads(match.group(1))
            except (ValueError, TypeError):
                continue
            if not isinstance(value, str):
                continue
            value = re.sub(r"\[img[^\]]*\].*?\[/img\]", "", value, flags=re.I | re.S)
            value = re.sub(r"\[(?:/?(?:h[1-6]|p|list|ul|ol|li|tr|table)|\*)[^\]]*\]", "\n", value, flags=re.I)
            value = re.sub(r"\[/?(?:td|th)[^\]]*\]", " | ", value, flags=re.I)
            value = re.sub(r"\[/?(?:db|quote|html|url|color|size|b|i|u|s|center|indent|hr|br)[^\]]*\]",
                           "", value, flags=re.I)
            # Keep unresolved [spell=ID]/[item=ID] tokens visible to the editor.
            chunks.append(normalize_body(Document(value).root.text()))
    text = "\n".join(chunks)
    return text[:28000] if len(text) >= 150 else ""


def normalize_body(text):
    return "\n".join(normalized(line) for line in text.splitlines() if normalized(line))[:28000]


def field_text(element, names):
    for child in element:
        if child.tag.rsplit("}", 1)[-1] in names:
            return "".join(child.itertext()).strip()
    return ""


def parse_feed(xml, source):
    if "<!DOCTYPE" in xml.upper() or "<!ENTITY" in xml.upper():
        raise FetchError("XML с DTD не поддерживается")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        raise FetchError("Источник вернул невалидный RSS/Atom") from None
    result = []
    for entry in root.iter():
        if entry.tag.rsplit("}", 1)[-1] not in {"item", "entry"}:
            continue
        title = field_text(entry, {"title"})
        link = field_text(entry, {"link"})
        if not link:
            for child in entry:
                if child.tag.rsplit("}", 1)[-1] == "link" and child.attrib.get("rel", "alternate") == "alternate":
                    link = child.attrib.get("href", "")
                    break
        link = canonical(urljoin(source["url"], link))
        try:
            validate_url(link, source["allowed_hosts"], resolve=False)
        except (FetchError, ValueError):
            continue
        if title and link != canonical(source["url"]):
            result.append({"url": link, "title": html.unescape(title), "source": source["name"]})
    if not result:
        raise FetchError("Лента пуста или формат изменился")
    return unique(result)[:source.get("max_items", 30)]


def unique(items):
    seen = set()
    result = []
    for item in items:
        if item["url"] not in seen:
            result.append(item)
            seen.add(item["url"])
    return result


def discover(fetcher, source):
    markup = fetcher.text(source["url"], source["allowed_hosts"])
    if source["type"] == "rss":
        return parse_feed(markup, source)
    if source["type"] == "noob_board":
        result = []
        for node in Document(markup).root.walk():
            if node.tag != "td" or "subject" not in node.attrs.get("class", "").split():
                continue
            for link_node in node.walk():
                if link_node.tag != "a":
                    continue
                link = canonical(urljoin(source["url"], link_node.attrs.get("href", "")))
                if not re.fullmatch(r"/index\.php", urlsplit(link).path) or not re.fullmatch(
                        r"topic=\d+\.0", urlsplit(link).query):
                    continue
                title = normalized(link_node.text())
                if len(title) < 12:
                    continue
                result.append({"url": link, "title": title[:300], "source": source["name"]})
                break
        if not result:
            raise FetchError("Не найдены темы новостей Noob Club")
        return unique(result)[:source.get("max_items", 30)]
    result = []
    pattern = re.compile(source["article_path_pattern"])
    for node in Document(markup).root.walk():
        if node.tag != "a":
            continue
        link = canonical(urljoin(source["url"], node.attrs.get("href", "")))
        if not pattern.search(urlsplit(link).path):
            continue
        try:
            validate_url(link, source["allowed_hosts"], resolve=False)
        except (FetchError, ValueError):
            continue
        title = normalized(node.text())
        if len(title) >= 12:
            result.append({"url": link, "title": title[:300], "source": source["name"]})
    if not result:
        raise FetchError("Не найдены ссылки на новости; проверьте разметку источника")
    return unique(result)[:source.get("max_items", 30)]
