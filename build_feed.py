#!/usr/bin/env python3
"""
Build a complete "Ask Pastor John" RSS feed.

What it does:
1. Downloads the current official RSS feed from Desiring God.
2. Keeps every official item exactly as published.
3. Crawls the official Ask Pastor John archive on desiringgod.org to find
   episodes that are no longer in the 1,000-item RSS window.
4. Resolves/verifies the original audio file for historical episodes.
5. Keeps a persistent catalog so that once an episode has entered this
   complete feed, it never disappears when it later falls out of the
   official 1,000-item feed.
6. Produces one RSS file suitable for "Follow a Show by URL" in Apple Podcasts.

This project does NOT re-host audio. Enclosures point to Desiring God's
original media URLs.
"""

from __future__ import annotations

import argparse
import copy
import html
import json
import re
import sys
import time
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime, format_datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from lxml import etree

OFFICIAL_RSS = "https://feed.desiringgod.org/ask-pastor-john.rss"
SITE = "https://www.desiringgod.org"
RECENT_URL = SITE + "/ask-pastor-john/recent?page={page}"
YEAR_ARCHIVE_URL = SITE + "/dates/{year}/interviews?page={page}"
AUDIO_BASE = "https://audio.desiringgod.org/"
USER_AGENT = (
    "Mozilla/5.0 (compatible; APJCompleteArchive/1.0; "
    "+personal podcast archive; respects source hosting)"
)

ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM_NS = "http://www.w3.org/2005/Atom"
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
DC_NS = "http://purl.org/dc/elements/1.1/"

MONTH_RE = (
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)"
)
DATE_RE = re.compile(rf"\b({MONTH_RE})\.?\s+(\d{{1,2}}),\s+(\d{{4}})\b", re.I)
EP_RE = re.compile(r"\bEpisode\s+(\d+)\b", re.I)
SPECIAL_RE = re.compile(r"\bSpecial\s+Episode\b", re.I)
MP3_RE = re.compile(
    r'https?:(?:\\?/\\?/|//)[^"\'<>\s\\]+?\.mp3(?:\?[^"\'<>\s\\]*)?',
    re.I,
)

APOS = "’'‘‛`´"
DASHES = "–—−"


def log(msg: str) -> None:
    print(msg, flush=True)


_thread_local = threading.local()


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    adapter = requests.adapters.HTTPAdapter(max_retries=4, pool_connections=20, pool_maxsize=20)
    s.mount("https://", adapter)
    return s


def worker_session() -> requests.Session:
    s = getattr(_thread_local, "http", None)
    if s is None:
        s = session()
        _thread_local.http = s
    return s


def get_text(s: requests.Session, url: str, timeout: int = 30) -> str:
    last = None
    for attempt in range(5):
        try:
            r = s.get(url, timeout=timeout)
            r.raise_for_status()
            return r.text
        except Exception as e:
            last = e
            if attempt == 4:
                raise
            time.sleep(min(8, 1.2 * (2 ** attempt)))
    raise last  # pragma: no cover


def get_bytes(s: requests.Session, url: str, timeout: int = 45) -> bytes:
    last = None
    for attempt in range(5):
        try:
            r = s.get(url, timeout=timeout)
            r.raise_for_status()
            return r.content
        except Exception as e:
            last = e
            if attempt == 4:
                raise
            time.sleep(min(8, 1.2 * (2 ** attempt)))
    raise last  # pragma: no cover


def clean_slug(value: str) -> str:
    value = value.strip().strip("/")
    return value.split("/")[-1].split("?")[0].split("#")[0]


def slug_from_item(item: etree._Element) -> str:
    link = (item.findtext("link") or "").strip()
    if link:
        slug = clean_slug(urlparse(link).path)
        if slug and slug not in {"link"}:
            return slug
    guid = (item.findtext("guid") or "").strip()
    title = (item.findtext("title") or "").strip()
    pub = (item.findtext("pubDate") or "").strip()
    return stable_slug(title) + "-" + re.sub(r"\D", "", pub)[:8]


def stable_slug(text: str) -> str:
    # Website-style slug: possessive apostrophes disappear.
    s = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s


def audio_slug_from_title(text: str) -> str:
    # Desiring God's audio filenames normally turn apostrophes into a separator:
    # "God’s" -> "god-s", "don’t" -> "don-t".
    for ch in APOS:
        text = text.replace(ch, "-")
    for ch in DASHES:
        text = text.replace(ch, "-")
    s = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s


def parse_date_text(text: str) -> Optional[datetime]:
    m = DATE_RE.search(text)
    if not m:
        return None
    mon, day, year = m.groups()
    mon = "Sep" if mon.lower().startswith("sept") else mon[:3].title()
    try:
        return datetime.strptime(f"{mon} {day} {year}", "%b %d %Y").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def item_datetime(item: etree._Element) -> datetime:
    raw = (item.findtext("pubDate") or "").strip()
    d = parsedate_to_datetime(raw)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def normalize_mp3_url(u: str) -> str:
    u = html.unescape(u)
    u = u.replace("\\/", "/")
    if u.startswith("https://media.blubrry.com/") and "audio.desiringgod.org/" in u:
        return u
    return u


def extract_mp3_from_html(page_html: str) -> Optional[str]:
    # Unescape common JSON encodings first.
    h = html.unescape(page_html).replace("\\/", "/")
    candidates = []
    for m in re.finditer(r'https?://[^"\'<>\s\\]+?\.mp3(?:\?[^"\'<>\s\\]*)?', h, re.I):
        u = normalize_mp3_url(m.group(0))
        candidates.append(u)
    if not candidates:
        return None

    # Prefer original Desiring God audio URLs, then Blubrry wrappers around them.
    def score(u: str):
        return (
            0 if "audio.desiringgod.org/" in u and "media.blubrry.com/" not in u else
            1 if "audio.desiringgod.org/" in u else
            2
        )
    candidates = sorted(dict.fromkeys(candidates), key=score)
    return candidates[0]


def verify_audio(s: requests.Session, url: str) -> tuple[bool, int, str]:
    """
    Verify that an enclosure is actually reachable without downloading the full MP3.
    Returns (ok, content_length, final_url).
    """
    headers = {"Range": "bytes=0-0", "Accept": "audio/mpeg,*/*;q=0.8"}
    try:
        with s.get(url, headers=headers, stream=True, timeout=30, allow_redirects=True) as r:
            if r.status_code in (200, 206):
                ctype = (r.headers.get("Content-Type") or "").lower()
                # Some CDNs return application/octet-stream for MP3s; accept that.
                if "audio" in ctype or "mpeg" in ctype or "octet-stream" in ctype or not ctype:
                    length = 0
                    cr = r.headers.get("Content-Range") or ""
                    m = re.search(r"/(\d+)$", cr)
                    if m:
                        length = int(m.group(1))
                    elif (r.headers.get("Content-Length") or "").isdigit():
                        # For a 206 Range response this may be 1, so only trust a sizeable 200 response.
                        if r.status_code == 200:
                            length = int(r.headers["Content-Length"])
                    return True, length, r.url
    except requests.RequestException:
        pass
    return False, 0, url


@dataclass
class HistoricEpisode:
    title: str
    slug: str
    page_url: str
    pubdate: str
    episode_number: Optional[int]
    special: bool
    description: str
    audio_url: str
    audio_length: int = 0

    @property
    def dt(self) -> datetime:
        return datetime.fromisoformat(self.pubdate.replace("Z", "+00:00"))


def card_info(anchor) -> tuple[Optional[datetime], Optional[int], bool, str]:
    """
    Pull date / episode label / short description from the APJ "recent" card.
    The code deliberately walks only a small ancestor radius, avoiding the whole page.
    """
    node = anchor
    best_text = ""
    best_node = None
    for _ in range(8):
        node = getattr(node, "parent", None)
        if node is None:
            break
        text = " ".join(node.stripped_strings)
        if len(text) > len(best_text):
            best_text = text
            best_node = node
        if parse_date_text(text) and (EP_RE.search(text) or SPECIAL_RE.search(text)):
            best_text = text
            best_node = node
            break

    dt = parse_date_text(best_text)
    m = EP_RE.search(best_text)
    num = int(m.group(1)) if m else None
    special = bool(SPECIAL_RE.search(best_text))

    desc = ""
    if best_node is not None:
        for p in best_node.find_all(["p"], recursive=True):
            t = " ".join(p.stripped_strings).strip()
            if len(t) >= 20 and not DATE_RE.search(t) and not EP_RE.search(t):
                desc = t
                break
    return dt, num, special, desc



def archive_title_and_context(anchor) -> tuple[str, str]:
    """
    Desiring God's yearly interview archive renders each resource as a card.
    Prefer a heading inside the card; otherwise extract the text between the
    "Ask Pastor John" label and the first date.
    """
    node = anchor
    context = " ".join(anchor.stripped_strings)
    for _ in range(5):
        if "Ask Pastor John" in context:
            break
        node = getattr(node, "parent", None)
        if node is None:
            break
        context = " ".join(node.stripped_strings)

    heading = None
    card = node if node is not None else anchor
    for tagname in ("h2", "h3", "h4", "h5"):
        h = card.find(tagname)
        if h:
            t = " ".join(h.stripped_strings).strip()
            if t and t != "Ask Pastor John":
                heading = t
                break

    if heading:
        return heading, context

    pos = context.find("Ask Pastor John")
    after = context[pos + len("Ask Pastor John"):] if pos >= 0 else context
    dm = DATE_RE.search(after)
    if dm:
        candidate = after[:dm.start()].strip(" -–—•\n\t")
        if candidate:
            return candidate, context

    return " ".join(anchor.stripped_strings).strip(), context


def crawl_historical_archive(
    s: requests.Session,
    cutoff: datetime,
    first_year: int = 2013,
    max_pages_per_year: int = 50,
) -> list[dict]:
    """
    Crawl Desiring God's official yearly Interview archives. These archive cards
    explicitly label APJ resources as "Ask Pastor John", including special
    episodes. Only resources older than the current RSS window are returned.

    Important: a page can legitimately add zero historical APJ items (especially
    in the cutoff year, where newer entries are intentionally filtered out). We
    therefore stop only on a real 404/empty page or an actually repeated page.
    """
    out: dict[str, dict] = {}

    for year in range(first_year, cutoff.year + 1):
        year_seen_before = len(out)
        seen_page_fingerprints: set[tuple[str, ...]] = set()

        for page in range(1, max_pages_per_year + 1):
            url = YEAR_ARCHIVE_URL.format(year=year, page=page)

            # Desiring God returns HTTP 404 after the final valid page for a
            # given year's archive. Treat that as the normal end of pagination
            # rather than as a build failure. Other HTTP errors remain fatal so
            # we never silently publish an incomplete archive.
            try:
                body = get_text(s, url)
            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code == 404:
                    log(f"Archive {year}: page {page} does not exist; year complete.")
                    break
                raise

            soup = BeautifulSoup(body, "lxml")

            interview_links = [
                a for a in soup.find_all("a", href=True)
                if re.match(r"^/interviews/[^/?#]+/?$", a.get("href", ""))
            ]
            if not interview_links:
                log(f"Archive {year}: page {page} is empty; year complete.")
                break

            # Guard against a site that redirects out-of-range pagination back
            # to the final page. This detects a genuine repeated page without
            # confusing a valid page that simply contributes zero OLD APJ items.
            fingerprint = tuple(sorted({clean_slug(a.get("href", "")) for a in interview_links}))
            if fingerprint in seen_page_fingerprints:
                log(f"Archive {year}: page {page} repeats an earlier page; year complete.")
                break
            seen_page_fingerprints.add(fingerprint)

            before = len(out)
            for a in interview_links:
                title, context = archive_title_and_context(a)
                if "Ask Pastor John" not in context:
                    continue

                href = a.get("href", "")
                slug = clean_slug(href)
                dt = parse_date_text(context)
                if not dt:
                    continue
                # The current RSS already owns the cutoff day and everything newer.
                if dt.date() >= cutoff.date():
                    continue

                out[slug] = {
                    "title": title,
                    "slug": slug,
                    "page_url": urljoin(SITE, href),
                    "date": dt.isoformat().replace("+00:00", "Z"),
                    "episode_number": None,
                    "special": False,
                    "description": "",
                }

            gained = len(out) - before
            log(f"Archive {year} page {page}: +{gained} APJ (total {len(out)})")
            time.sleep(0.08)
        else:
            raise RuntimeError(
                f"Archive {year} reached the safety limit of {max_pages_per_year} pages "
                "without finding an end. Increase max_pages_per_year rather than publishing "
                "a potentially incomplete archive."
            )

        log(f"Archive {year}: +{len(out) - year_seen_before} APJ items")

    if len(out) < 1200:
        raise RuntimeError(
            f"Historical archive crawl found only {len(out)} APJ items; expected >1200 "
            "before the January 2019 RSS cutoff. Aborting instead of publishing an incomplete feed."
        )

    required = {
        "reflections-from-john-piper-on-his-birthday",
        "why-is-god-withholding-marriage-from-me",
        "john-pipers-prayer-at-planned-parenthood",
    }
    absent = sorted(required - set(out))
    if absent:
        raise RuntimeError(
            "Historical archive did not reach the complete range / specials. "
            f"Missing expected page(s): {absent}"
        )

    return list(out.values())

def crawl_apj_catalog(s: requests.Session, max_pages: int = 350) -> list[dict]:
    """
    Crawl the official APJ "recent" pagination. It is series-specific and therefore
    captures numbered episodes as well as special episodes.
    """
    out: dict[str, dict] = {}
    empty_streak = 0

    for page in range(1, max_pages + 1):
        url = RECENT_URL.format(page=page)
        body = get_text(s, url)
        soup = BeautifulSoup(body, "lxml")

        before = len(out)
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            if not re.match(r"^/interviews/[^/?#]+/?$", href):
                continue
            title = " ".join(a.stripped_strings).strip()
            if not title or title.lower() in {"ask pastor john", "read more"}:
                continue
            slug = clean_slug(href)
            dt, num, special, desc = card_info(a)
            # Keep the richest version if a slug appears more than once.
            candidate = {
                "title": title,
                "slug": slug,
                "page_url": urljoin(SITE, href),
                "date": dt.isoformat().replace("+00:00", "Z") if dt else None,
                "episode_number": num,
                "special": special,
                "description": desc,
            }
            old = out.get(slug)
            if old is None or (candidate["date"] and not old.get("date")):
                out[slug] = candidate

        gained = len(out) - before
        log(f"Catalog page {page}: +{gained} (total {len(out)})")
        if gained == 0:
            empty_streak += 1
        else:
            empty_streak = 0

        if empty_streak >= 2:
            break
        time.sleep(0.08)

    if len(out) < 2000:
        raise RuntimeError(
            f"Catalog crawl found only {len(out)} APJ pages; expected >2000. "
            "The site layout may have changed. Aborting instead of publishing an incomplete feed."
        )
    return list(out.values())


def fetch_episode_page_details(s: requests.Session, rec: dict) -> dict:
    body = get_text(s, rec["page_url"])
    soup = BeautifulSoup(body, "lxml")

    main = soup.find("main") or soup
    text = " ".join(main.stripped_strings)

    h1 = soup.find("h1")
    title = " ".join(h1.stripped_strings).strip() if h1 else rec["title"]

    # Date
    dt = None
    for tag in soup.find_all("time"):
        raw = tag.get("datetime")
        if raw:
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                dt = dt.astimezone(timezone.utc)
                break
            except ValueError:
                pass
    if not dt:
        dt = parse_date_text(text) or (
            datetime.fromisoformat(rec["date"].replace("Z", "+00:00")) if rec.get("date") else None
        )

    m = EP_RE.search(text)
    num = int(m.group(1)) if m else rec.get("episode_number")
    special = bool(SPECIAL_RE.search(text)) or rec.get("special", False)

    desc = ""
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        desc = meta["content"].strip()
    if not desc:
        desc = rec.get("description") or ""

    audio = extract_mp3_from_html(body)

    return {
        **rec,
        "title": title,
        "date": dt.isoformat().replace("+00:00", "Z") if dt else rec.get("date"),
        "episode_number": num,
        "special": special,
        "description": desc,
        "audio_from_page": audio,
    }


def resolve_historic_episode(s: requests.Session, rec: dict) -> HistoricEpisode:
    """
    Resolve one historical item. First use catalog metadata and the established
    Desiring God audio naming convention. If that doesn't verify, inspect the
    canonical episode page for the actual media URL and try again.
    """
    if not rec.get("date"):
        rec = fetch_episode_page_details(s, rec)

    if not rec.get("date"):
        raise RuntimeError(f"No publication date found for {rec['page_url']}")

    dt = datetime.fromisoformat(rec["date"].replace("Z", "+00:00")).astimezone(timezone.utc)
    title = rec["title"]
    slug = rec["slug"]

    # Candidate 1: filename generated from the publication title.
    title_audio_slug = audio_slug_from_title(title)
    candidate = f"{AUDIO_BASE}{dt:%Y%m%d}-en-{title_audio_slug}.mp3"
    ok, length, final = verify_audio(s, candidate)
    if ok:
        return HistoricEpisode(
            title=title,
            slug=slug,
            page_url=rec["page_url"],
            pubdate=dt.isoformat().replace("+00:00", "Z"),
            episode_number=rec.get("episode_number"),
            special=bool(rec.get("special")),
            description=rec.get("description") or "",
            audio_url=final,
            audio_length=length,
        )

    # Candidate 2: canonical website slug.
    candidate2 = f"{AUDIO_BASE}{dt:%Y%m%d}-en-{slug}.mp3"
    if candidate2 != candidate:
        ok, length, final = verify_audio(s, candidate2)
        if ok:
            return HistoricEpisode(
                title=title,
                slug=slug,
                page_url=rec["page_url"],
                pubdate=dt.isoformat().replace("+00:00", "Z"),
                episode_number=rec.get("episode_number"),
                special=bool(rec.get("special")),
                description=rec.get("description") or "",
                audio_url=final,
                audio_length=length,
            )

    # Candidate 3: inspect canonical page for exact media URL. This handles
    # renamed episodes, publication-date corrections, and unusual filename slugs.
    rich = fetch_episode_page_details(s, rec)
    page_audio = rich.get("audio_from_page")
    if page_audio:
        ok, length, final = verify_audio(s, page_audio)
        if ok:
            dt2 = datetime.fromisoformat(rich["date"].replace("Z", "+00:00")).astimezone(timezone.utc)
            return HistoricEpisode(
                title=rich["title"],
                slug=slug,
                page_url=rec["page_url"],
                pubdate=dt2.isoformat().replace("+00:00", "Z"),
                episode_number=rich.get("episode_number"),
                special=bool(rich.get("special")),
                description=rich.get("description") or "",
                audio_url=final,
                audio_length=length,
            )

    # Last fallback: title may have changed while the old audio filename still
    # reflects an older title. We refuse to guess beyond this point.
    raise RuntimeError(f"Could not resolve a verified MP3 for: {rec['page_url']}")


def make_historical_item(ep: HistoricEpisode) -> etree._Element:
    item = etree.Element("item")
    etree.SubElement(item, "title").text = ep.title
    etree.SubElement(item, "link").text = ep.page_url

    guid = etree.SubElement(item, "guid")
    guid.set("isPermaLink", "false")
    guid.text = f"apj-complete-{ep.slug}"

    etree.SubElement(item, "pubDate").text = format_datetime(ep.dt)

    desc = etree.SubElement(item, "description")
    desc.text = ep.description or (
        f"Ask Pastor John"
        + (f" — Episode {ep.episode_number}" if ep.episode_number else " — Special Episode")
    )

    enc = etree.SubElement(item, "enclosure")
    enc.set("url", ep.audio_url)
    enc.set("length", str(ep.audio_length or 0))
    enc.set("type", "audio/mpeg")

    etree.SubElement(item, f"{{{ITUNES_NS}}}explicit").text = "false"
    etree.SubElement(item, f"{{{ITUNES_NS}}}episodeType").text = "bonus" if ep.special else "full"
    if ep.episode_number:
        etree.SubElement(item, f"{{{ITUNES_NS}}}episode").text = str(ep.episode_number)

    return item


def serialize_item(item: etree._Element) -> str:
    return etree.tostring(item, encoding="unicode")


def deserialize_item(xml: str) -> etree._Element:
    return etree.fromstring(xml.encode("utf-8"))


def load_catalog(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    obj = json.loads(path.read_text(encoding="utf-8"))
    if obj.get("format") != 1:
        raise RuntimeError("Unsupported catalog.json format.")
    return dict(obj.get("items") or {})


def save_catalog(path: Path, items: dict[str, str]) -> None:
    path.write_text(
        json.dumps(
            {
                "format": 1,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "items": items,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def strip_redirect_metadata(channel: etree._Element) -> None:
    # Do NOT let Apple redirect this custom feed back to the truncated official feed.
    for e in list(channel):
        if e.tag == f"{{{ITUNES_NS}}}new-feed-url":
            channel.remove(e)
        elif e.tag == f"{{{ATOM_NS}}}link" and e.get("rel") == "self":
            channel.remove(e)


def build_output(
    official_root: etree._Element,
    catalog: dict[str, str],
    output: Path,
    public_url: Optional[str],
) -> int:
    official_channel = official_root.find("channel")
    if official_channel is None:
        raise RuntimeError("Official RSS has no channel element.")

    channel = copy.deepcopy(official_channel)
    for i in list(channel.findall("item")):
        channel.remove(i)
    strip_redirect_metadata(channel)

    title = channel.find("title")
    if title is not None:
        title.text = "Ask Pastor John — Complete Archive"
    desc = channel.find("description")
    if desc is not None:
        desc.text = (
            "Complete personal archive feed for Ask Pastor John, preserving the "
            "official Desiring God episodes even after they leave the 1,000-item RSS window."
        )

    if public_url:
        self_link = etree.Element(f"{{{ATOM_NS}}}link")
        self_link.set("href", public_url)
        self_link.set("rel", "self")
        self_link.set("type", "application/rss+xml")
        # Put it near the top.
        channel.insert(0, self_link)

    parsed_items = []
    for key, xml in catalog.items():
        try:
            item = deserialize_item(xml)
            parsed_items.append((item_datetime(item), item))
        except Exception as e:
            raise RuntimeError(f"Bad cached RSS item {key}: {e}") from e
    parsed_items.sort(key=lambda x: x[0], reverse=True)

    for _, item in parsed_items:
        channel.append(item)

    rss = etree.Element(
        "rss",
        nsmap=official_root.nsmap,
        version=official_root.get("version", "2.0"),
    )
    rss.append(channel)
    tree = etree.ElementTree(rss)

    output.parent.mkdir(parents=True, exist_ok=True)
    tree.write(
        str(output),
        encoding="UTF-8",
        xml_declaration=True,
        pretty_print=True,
    )
    return len(parsed_items)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default="public/ask-pastor-john-complete.rss")
    ap.add_argument("--catalog", default="catalog.json")
    ap.add_argument("--seed-rss", default=None, help="Use a local RSS instead of downloading current feed.")
    ap.add_argument("--public-url", default=None)
    ap.add_argument("--max-workers", type=int, default=6)
    ap.add_argument("--min-items", type=int, default=2200)
    ap.add_argument("--force-history-refresh", action="store_true")
    ap.add_argument("--allow-incomplete-for-test", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    s = session()

    if args.seed_rss:
        rss_bytes = Path(args.seed_rss).read_bytes()
        log(f"Using seed RSS: {args.seed_rss}")
    else:
        log("Downloading current official RSS...")
        rss_bytes = get_bytes(s, OFFICIAL_RSS)

    parser = etree.XMLParser(remove_blank_text=False, recover=False)
    official_root = etree.fromstring(rss_bytes, parser)
    official_channel = official_root.find("channel")
    if official_channel is None:
        raise RuntimeError("Official RSS has no channel.")

    current_items = official_channel.findall("item")
    if not current_items:
        raise RuntimeError("Official RSS has no items.")
    log(f"Official feed items: {len(current_items)}")

    catalog_path = Path(args.catalog)
    catalog = load_catalog(catalog_path)
    log(f"Cached complete-feed items before update: {len(catalog)}")

    # Add/refresh all current official items, preserving their exact XML.
    current_slugs = set()
    for item in current_items:
        key = slug_from_item(item)
        current_slugs.add(key)
        catalog[key] = serialize_item(copy.deepcopy(item))

    # On the first run, recover everything older than the current RSS window.
    # On later runs the persistent catalog prevents old episodes from disappearing.
    need_history = args.force_history_refresh or len(catalog) < args.min_items

    if need_history:
        oldest_current = min(item_datetime(i) for i in current_items)
        log(f"Oldest current official item: {oldest_current.isoformat()}")
        log("Crawling official yearly Interview archives for historical APJ items...")
        listing = crawl_historical_archive(s, oldest_current)

        missing = []
        for rec in listing:
            slug = rec["slug"]
            if slug in current_slugs or slug in catalog:
                continue
            missing.append(rec)

        log(f"Historical APJ items to resolve: {len(missing)}")

        errors = []
        completed = 0
        with ThreadPoolExecutor(max_workers=max(1, args.max_workers)) as ex:
            futures = {ex.submit(resolve_historic_episode, worker_session(), rec): rec for rec in missing}
            for fut in as_completed(futures):
                rec = futures[fut]
                try:
                    ep = fut.result()
                    catalog[ep.slug] = serialize_item(make_historical_item(ep))
                    completed += 1
                    if completed % 25 == 0:
                        log(f"Resolved historical audio: {completed}/{len(missing)}")
                        save_catalog(catalog_path, catalog)
                except Exception as e:
                    errors.append({"url": rec["page_url"], "error": str(e)})
                    log(f"ERROR: {rec['page_url']}: {e}")

        save_catalog(catalog_path, catalog)

        if errors:
            problem_path = catalog_path.with_name("unresolved_episodes.json")
            problem_path.write_text(json.dumps(errors, indent=2), encoding="utf-8")
            log(
                f"WARNING: {len(errors)} historical episode(s) could not be verified "
                f"and will be skipped. Details saved to {problem_path}."
            )
            log(
                f"Resolved historical episodes: {completed}/{len(missing)}; "
                f"skipped unresolved: {len(errors)}."
            )

    count = build_output(
        official_root=official_root,
        catalog=catalog,
        output=Path(args.output),
        public_url=args.public_url,
    )
    save_catalog(catalog_path, catalog)

    if count < args.min_items:
        raise RuntimeError(
            f"Complete feed contains only {count} items (< {args.min_items}); refusing to publish."
        )

    # Historical boundary sanity check: APJ began Jan 11, 2013.
    dates = [item_datetime(deserialize_item(x)) for x in catalog.values()]
    oldest = min(dates)
    if (not args.allow_incomplete_for_test) and oldest.date() > datetime(2013, 1, 11, tzinfo=timezone.utc).date():
        raise RuntimeError(
            f"Oldest recovered item is {oldest.date()}, so the 2013 beginning is missing. "
            "Refusing to publish an incomplete feed."
        )

    # Final XML sanity check.
    built = etree.parse(args.output)
    final_count = len(built.getroot().find("channel").findall("item"))
    log(f"SUCCESS: {args.output} contains {final_count} items.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
   