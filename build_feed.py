#!/usr/bin/env python3
"""Build custom podcast feeds from a list of upstream sources.

Ordering, in priority order:
  1. Sections, in the order they are declared under [[feed.sections]].
  2. Within a section: sources with an explicit `order` first, ascending.
  3. Then the remaining sources, by language order, then newest first.

Each source contributes at most one item: its newest episode inside that
source's max_age window. This is not configurable.

Every source produces exactly one diagnostic line so it is always visible why
a source did or did not contribute an episode.

Usage:
    python build_feed.py                     # write feeds to repo root
    python build_feed.py --out-dir out       # local test run
    python build_feed.py --dry-run           # diagnose only, write nothing
    python build_feed.py --verbose           # per-entry rejection detail
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import sys
import time
import tomllib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.dom import minidom

import feedparser
import requests

from scrapers import SCRAPERS, ScrapeError

CONFIG_FILE = Path("feeds.toml")

# Several broadcasters (ARD, BBC, NPR, RAI) return 403 to the default
# feedparser/urllib user agent, especially from datacentre IPs like GitHub
# Actions runners. Always send a browser-ish UA.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36 NewsDigestBot/1.0"
)
DEFAULT_TIMEOUT = 20
DEFAULT_MAX_AGE_HOURS = 24
DEFAULT_RETRIES = 2
DEFAULT_LANG_ORDER = ["de", "en", "he", "it"]

NAMESPACES = {
    "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
    "content": "http://purl.org/rss/1.0/modules/content/",
}
for _prefix, _uri in NAMESPACES.items():
    ET.register_namespace(_prefix, _uri)

log = logging.getLogger("build_feed")


# --------------------------------------------------------------------------
# Diagnostics
# --------------------------------------------------------------------------

class Status:
    OK = "OK"
    DISABLED = "DISABLED"            # enabled = false in config
    CONFIG_ERROR = "CONFIG_ERR"      # bad section name, missing url, etc.
    HTTP_ERROR = "HTTP_ERROR"        # non-2xx response
    TIMEOUT = "TIMEOUT"              # request timed out
    DNS_ERROR = "DNS_ERROR"          # host does not resolve
    CONNECTION_ERROR = "CONN_ERROR"  # TLS / refused / reset
    NOT_A_FEED = "NOT_A_FEED"        # body did not parse as RSS/Atom
    EMPTY_FEED = "EMPTY_FEED"        # parsed, but zero entries
    NO_DATES = "NO_DATES"            # entries exist, none has a parseable date
    NO_AUDIO = "NO_AUDIO"            # entries exist, none has an audio enclosure
    TOO_OLD = "TOO_OLD"              # newest usable entry is older than max_age
    SCRAPE_ERROR = "SCRAPE_ERR"      # page fetched, but nothing usable extracted
    ERROR = "ERROR"                  # anything unexpected


FAILURE_HINTS = {
    Status.HTTP_ERROR: "URL is probably wrong, moved, or the host blocks bots",
    Status.DNS_ERROR: "hostname does not exist; check for a typo or a dead service",
    Status.CONNECTION_ERROR: "network/TLS problem, or the host blocks datacentre IPs",
    Status.NOT_A_FEED: "URL returns HTML or JSON, not RSS; find the real feed URL",
    Status.EMPTY_FEED: "feed parses but has no <item>; likely a stub or region-locked",
    Status.NO_DATES: "entries have no pubDate/updated; cannot apply an age filter",
    Status.NO_AUDIO: "this is an article feed, not a podcast feed; no <enclosure>",
    Status.TOO_OLD: "raise max_age_hours for this source if it publishes weekly",
    Status.SCRAPE_ERROR: "the scraped page's layout likely changed; see scrapers.py",
}


@dataclass
class SourceResult:
    feed_file: str
    name: str
    url: str
    lang: str
    section: str
    status: str
    order: int | None = None
    detail: str = ""
    http_status: int | None = None
    elapsed_s: float = 0.0
    entries_seen: int = 0
    newest_age_hours: float | None = None
    max_age_hours: int | None = None
    episodes_added: int = 0
    rejections: dict[str, int] = field(default_factory=dict)

    def line(self) -> str:
        bits = [f"[{self.status:<10}] {self.feed_file} :: {self.section} :: {self.name}"]
        if self.order is not None:
            bits.append(f"order={self.order}")
        if self.http_status is not None:
            bits.append(f"http={self.http_status}")
        if self.entries_seen:
            bits.append(f"entries={self.entries_seen}")
        if self.newest_age_hours is not None:
            bits.append(f"newest={self.newest_age_hours:.1f}h")
        if self.max_age_hours is not None:
            bits.append(f"max_age={self.max_age_hours}h")
        if self.episodes_added:
            bits.append(f"added={self.episodes_added}")
        if self.elapsed_s:
            bits.append(f"{self.elapsed_s:.1f}s")
        head = " | ".join(bits)
        if self.detail:
            head += f"\n             -> {self.detail}"
        if self.rejections:
            counts = ", ".join(f"{k}={v}" for k, v in sorted(self.rejections.items()))
            head += f"\n             -> rejected: {counts}"
        hint = FAILURE_HINTS.get(self.status)
        if hint:
            head += f"\n             -> hint: {hint}"
        return head


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def fetch_feed(url: str, timeout: int, retries: int, result: SourceResult):
    """Fetch and parse a feed. Returns a feedparser dict, or None on failure.

    Sets status/detail on `result` when it fails.
    """
    last_exc: Exception | None = None

    for attempt in range(1, retries + 1):
        started = time.monotonic()
        try:
            resp = requests.get(
                url,
                timeout=timeout,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/rss+xml, application/xml, text/xml, */*",
                    "Accept-Language": "de,en;q=0.8",
                },
                allow_redirects=True,
            )
            result.elapsed_s = time.monotonic() - started
            result.http_status = resp.status_code

            if not resp.ok:
                result.status = Status.HTTP_ERROR
                result.detail = f"{resp.status_code} {resp.reason} (final URL: {resp.url})"
                return None

            parsed = feedparser.parse(resp.content)

            # bozo means the XML did not parse cleanly. That is often harmless
            # (stray entity, bad encoding declaration), so only fail hard when
            # it also produced no entries.
            if parsed.bozo and not parsed.entries:
                exc = parsed.get("bozo_exception")
                ctype = resp.headers.get("Content-Type", "unknown")
                result.status = Status.NOT_A_FEED
                result.detail = f"content-type={ctype}; parser said: {exc}"
                return None

            if parsed.bozo:
                log.debug("%s: parsed with warnings: %s", result.name,
                          parsed.get("bozo_exception"))

            return parsed

        except requests.exceptions.Timeout as exc:
            last_exc = exc
            result.status = Status.TIMEOUT
            result.detail = f"no response within {timeout}s (attempt {attempt}/{retries})"
        except requests.exceptions.SSLError as exc:
            last_exc = exc
            result.status = Status.CONNECTION_ERROR
            result.detail = f"TLS error: {exc}"
            break  # retrying a cert failure is pointless
        except requests.exceptions.ConnectionError as exc:
            last_exc = exc
            if isinstance(exc.__cause__, socket.gaierror) or "NameResolution" in str(exc):
                result.status = Status.DNS_ERROR
                result.detail = f"cannot resolve host: {exc}"
                break
            result.status = Status.CONNECTION_ERROR
            result.detail = f"{type(exc).__name__}: {exc} (attempt {attempt}/{retries})"
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            result.status = Status.ERROR
            result.detail = f"{type(exc).__name__}: {exc}"
            break

        if attempt < retries:
            time.sleep(1.5 * attempt)

    log.debug("%s: giving up after %s", result.name, last_exc)
    return None


# --------------------------------------------------------------------------
# Entry selection
# --------------------------------------------------------------------------

def entry_datetime(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        struct = entry.get(key)
        if struct:
            # feedparser already normalises these structs to UTC.
            return datetime(*struct[:6], tzinfo=timezone.utc)
    return None


def audio_enclosure(entry) -> dict | None:
    enclosures = list(entry.get("enclosures") or [])
    for enc in enclosures:
        if "audio" in (enc.get("type") or "").lower():
            return enc
    # Some feeds mislabel the type but the href is obviously audio.
    for enc in enclosures:
        href = (enc.get("href") or "").lower().split("?")[0]
        if href.endswith((".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav")):
            return enc
    # media:content fallback (RAI, some WordPress feeds).
    for media in entry.get("media_content") or []:
        if "audio" in (media.get("type") or "").lower() or media.get("medium") == "audio":
            return {
                "href": media.get("url", ""),
                "type": media.get("type", "audio/mpeg"),
                "length": media.get("filesize", 0),
            }
    return None


def pick_latest_episode(parsed, now: datetime, max_age_hours: int,
                        result: SourceResult) -> tuple[datetime, dict, object] | None:
    """Return the single newest usable entry, or None.

    A source contributes at most one item to a feed. That is a fixed rule, not
    a configurable one: the whole point of the digest is the latest bulletin
    from each source. `max_age_hours` decides whether that latest bulletin is
    recent enough to include, nothing more."""
    entries = parsed.entries or []
    result.entries_seen = len(entries)

    if not entries:
        result.status = Status.EMPTY_FEED
        result.detail = f"feed title: {parsed.feed.get('title', '(none)')!r}"
        return None

    cutoff = now - timedelta(hours=max_age_hours)
    candidates: list[tuple[datetime, dict, object]] = []
    dated_count = 0
    audio_count = 0
    newest_seen: datetime | None = None

    for entry in entries:
        published = entry_datetime(entry)
        if published is None:
            result.rejections["no_date"] = result.rejections.get("no_date", 0) + 1
            log.debug("%s: entry %r has no parseable date",
                      result.name, entry.get("title", "?"))
            continue
        dated_count += 1
        if newest_seen is None or published > newest_seen:
            newest_seen = published

        enc = audio_enclosure(entry)
        if enc is None:
            result.rejections["no_audio"] = result.rejections.get("no_audio", 0) + 1
            log.debug("%s: entry %r has no audio enclosure",
                      result.name, entry.get("title", "?"))
            continue
        audio_count += 1

        if published < cutoff:
            result.rejections["too_old"] = result.rejections.get("too_old", 0) + 1
            log.debug("%s: entry %r is %.1fh old (limit %dh)", result.name,
                      entry.get("title", "?"),
                      (now - published).total_seconds() / 3600, max_age_hours)
            continue

        candidates.append((published, enc, entry))

    if newest_seen is not None:
        result.newest_age_hours = (now - newest_seen).total_seconds() / 3600

    if dated_count == 0:
        result.status = Status.NO_DATES
        result.detail = f"{len(entries)} entries, none with pubDate/updated"
        return None
    if audio_count == 0:
        result.status = Status.NO_AUDIO
        result.detail = f"{dated_count} dated entries, none with an audio enclosure"
        return None
    if not candidates:
        result.status = Status.TOO_OLD
        result.detail = (
            f"newest audio entry is {result.newest_age_hours:.1f}h old, "
            f"max_age_hours is {max_age_hours}"
        )
        return None

    # Upstream feeds are not reliably ordered newest-first, so pick the newest
    # explicitly rather than trusting position 0.
    return max(candidates, key=lambda c: c[0])


# --------------------------------------------------------------------------
# Scraped sources
# --------------------------------------------------------------------------

def make_fetcher(timeout: int):
    """HTTP callable handed to scrapers, sharing the UA and timeout policy."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "he,en;q=0.8",
    })

    def fetch(url: str, method: str = "get"):
        resp = session.request(method.upper(), url, timeout=timeout,
                               allow_redirects=True)
        resp.raise_for_status()
        return resp

    return fetch


def run_scraper(source: dict, timeout: int, now: datetime, max_age_hours: int,
                result: SourceResult):
    """Run a registered scraper and apply the same age filter as RSS sources."""
    name = source.get("scraper")
    if name not in SCRAPERS:
        result.status = Status.CONFIG_ERROR
        result.detail = (
            f"unknown scraper {name!r}; registered: {', '.join(SCRAPERS) or 'none'}"
        )
        return None

    started = time.monotonic()
    try:
        items = SCRAPERS[name](source, make_fetcher(timeout))
    except ScrapeError as exc:
        result.status = Status.SCRAPE_ERROR
        result.detail = str(exc)
        return None
    except requests.exceptions.Timeout:
        result.status = Status.TIMEOUT
        result.detail = f"page did not respond within {timeout}s"
        return None
    except requests.exceptions.HTTPError as exc:
        result.status = Status.HTTP_ERROR
        result.detail = str(exc)
        return None
    except requests.exceptions.RequestException as exc:
        result.status = Status.CONNECTION_ERROR
        result.detail = f"{type(exc).__name__}: {exc}"
        return None
    except Exception as exc:  # noqa: BLE001 - a broken scraper must not kill the run
        result.status = Status.SCRAPE_ERROR
        result.detail = f"scraper raised {type(exc).__name__}: {exc}"
        return None
    finally:
        result.elapsed_s = time.monotonic() - started

    result.entries_seen = len(items)
    if not items:
        result.status = Status.EMPTY_FEED
        result.detail = "scraper returned no items"
        return None

    items.sort(key=lambda i: i.published, reverse=True)
    result.newest_age_hours = (now - items[0].published).total_seconds() / 3600

    cutoff = now - timedelta(hours=max_age_hours)
    fresh = [i for i in items if i.published >= cutoff]
    if not fresh:
        result.rejections["too_old"] = len(items)
        result.status = Status.TOO_OLD
        result.detail = (
            f"newest scraped item is {result.newest_age_hours:.1f}h old, "
            f"max_age_hours is {max_age_hours}"
        )
        return None

    newest = fresh[0]
    return newest.published, newest.as_enclosure(), newest.as_entry()


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------

def resolve_sections(feed_config: dict, filename: str) -> list[dict]:
    """Return the declared sections, in declaration order.

    Falls back to deriving sections from the sources themselves so an old-style
    config with only `priority` values still builds.
    """
    sections = feed_config.get("sections")
    if sections:
        return sections

    seen: list[str] = []
    for src in feed_config.get("sources", []):
        key = src.get("section") or src.get("priority") or "default"
        if key not in seen:
            seen.append(key)
    log.warning(
        "%s: no [[feed.sections]] declared; deriving order from sources: %s",
        filename, ", ".join(seen) or "(none)",
    )
    return [{"name": name} for name in seen]


# --------------------------------------------------------------------------
# XML output
# --------------------------------------------------------------------------

def add_item(channel, source: dict, section: dict, published: datetime,
             enc: dict, entry) -> None:
    item = ET.SubElement(channel, "item")

    lang = (source.get("lang") or "").upper()
    section_label = section.get("label") or section["name"].upper()
    label = f"[{lang}|{section_label}]" if lang else f"[{section_label}]"
    ET.SubElement(item, "title").text = (
        f"{label} {source.get('name', 'Source')}: {entry.get('title', 'Audio Bulletin')}"
    )

    ET.SubElement(item, "link").text = entry.get("link", "")
    ET.SubElement(item, "pubDate").text = published.strftime("%a, %d %b %Y %H:%M:%S +0000")

    guid = ET.SubElement(item, "guid")
    guid.text = (
        entry.get("id") or entry.get("link")
        or f"{source.get('name')}-{published.isoformat()}"
    )
    guid.set("isPermaLink", "false")

    ET.SubElement(item, "description").text = (
        entry.get("summary") or entry.get("description") or ""
    )

    ET.SubElement(
        item,
        "enclosure",
        url=enc.get("href", ""),
        length=str(enc.get("length") or 0),
        type=enc.get("type") or "audio/mpeg",
    )


def write_xml(rss, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pretty = minidom.parseString(ET.tostring(rss, encoding="utf-8")).toprettyxml(indent="  ")
    path.write_text(pretty, encoding="utf-8")


# --------------------------------------------------------------------------
# Feed build
# --------------------------------------------------------------------------

def build_single_feed(feed_config: dict, out_dir: Path, dry_run: bool) -> list[SourceResult]:
    meta = feed_config.get("meta", {})
    filename = meta.get("filename", "feed.xml")
    results: list[SourceResult] = []

    if not meta.get("enabled", True):
        log.info("Skipping disabled feed: %s", filename)
        return results

    feed_max_age = int(meta.get("max_age_hours", DEFAULT_MAX_AGE_HOURS))
    timeout = int(meta.get("timeout_seconds", DEFAULT_TIMEOUT))
    retries = int(meta.get("retries", DEFAULT_RETRIES))
    lang_order = [l.lower() for l in meta.get("lang_order", DEFAULT_LANG_ORDER)]
    title = meta.get("title", "Daily News Digest")
    link = meta.get("link", "https://github.com")

    sections = resolve_sections(feed_config, filename)
    section_index = {s["name"]: i for i, s in enumerate(sections)}
    section_by_name = {s["name"]: s for s in sections}

    def lang_rank(code: str) -> int:
        code = (code or "").lower()
        return lang_order.index(code) if code in lang_order else len(lang_order)

    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = title
    ET.SubElement(channel, "description").text = meta.get("description", "")
    ET.SubElement(channel, "link").text = link
    ET.SubElement(channel, "language").text = meta.get("language", "en")
    ET.SubElement(channel, "lastBuildDate").text = datetime.now(timezone.utc).strftime(
        "%a, %d %b %Y %H:%M:%S +0000"
    )

    image_url = meta.get("image_url")
    if image_url:
        img = ET.SubElement(channel, "image")
        ET.SubElement(img, "url").text = image_url
        ET.SubElement(img, "title").text = title
        ET.SubElement(img, "link").text = link
        ET.SubElement(channel, f"{{{NAMESPACES['itunes']}}}image", href=image_url)

    now = datetime.now(timezone.utc)
    collected: list[tuple[tuple, dict, dict, datetime, dict, object]] = []

    for seq, source in enumerate(feed_config.get("sources", [])):
        section_name = source.get("section") or source.get("priority") or "default"
        result = SourceResult(
            feed_file=filename,
            name=source.get("name", "(unnamed)"),
            url=source.get("url", ""),
            lang=source.get("lang", ""),
            section=section_name,
            status=Status.OK,
            order=source.get("order"),
        )
        results.append(result)

        if section_name not in section_index:
            result.status = Status.CONFIG_ERROR
            result.detail = (
                f"section {section_name!r} is not declared in [[feed.sections]] "
                f"(declared: {', '.join(section_index) or 'none'})"
            )
            continue

        if not source.get("enabled", True):
            result.status = Status.DISABLED
            result.detail = "enabled = false in feeds.toml"
            continue

        is_scraped = source.get("type") == "scrape"

        if not result.url and not is_scraped:
            result.status = Status.CONFIG_ERROR
            result.detail = "no url set in feeds.toml"
            continue

        section = section_by_name[section_name]

        # Age and episode count resolve source -> section -> feed -> default.
        max_age = int(
            source.get("max_age_hours", section.get("max_age_hours", feed_max_age))
        )
        result.max_age_hours = max_age

        if is_scraped:
            pick = run_scraper(source, timeout, now, max_age, result)
        else:
            parsed = fetch_feed(result.url, timeout, retries, result)
            if parsed is None:
                continue
            pick = pick_latest_episode(parsed, now, max_age, result)

        if pick is None:
            continue
        published, enc, entry = pick

        order = source.get("order")
        sort_key = (
            section_index[section_name],    # 1. declared section order
            0 if order is not None else 1,  # 2. explicit overrides first
            order if order is not None else 0,
            lang_rank(source.get("lang", "")) if order is None else 0,
            -published.timestamp() if order is None else 0,
            seq,                            # stable tiebreak: config order
        )
        collected.append((sort_key, source, section, published, enc, entry))

        result.status = Status.OK
        result.episodes_added = 1

    collected.sort(key=lambda c: c[0])
    for _key, source, section, published, enc, entry in collected:
        add_item(channel, source, section, published, enc, entry)

    if dry_run:
        log.info("[dry-run] %s would contain %d items", filename, len(collected))
        for _key, source, section, published, _enc, entry in collected:
            log.info("   %-12s %-28s %s", section["name"], source.get("name"),
                     entry.get("title", "")[:60])
    else:
        write_xml(rss, out_dir / filename)
        log.info("Wrote %s (%d items)", out_dir / filename, len(collected))

    return results


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def report(results: list[SourceResult], out_dir: Path, dry_run: bool) -> int:
    ok = [r for r in results if r.status == Status.OK]
    disabled = [r for r in results if r.status == Status.DISABLED]
    failed = [r for r in results
              if r.status not in (Status.OK, Status.DISABLED)]

    print("\n" + "=" * 72)
    print("SOURCE REPORT")
    print("=" * 72)
    for r in results:
        print(r.line())
    print("-" * 72)
    print(f"contributed: {len(ok)}   disabled: {len(disabled)}   failed: {len(failed)}")

    if failed:
        print("\nFAILED SOURCES")
        for r in failed:
            print(f"  {r.status:<10} {r.name}\n             {r.url}")
    print("=" * 72 + "\n")

    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "build_report.json").write_text(
            json.dumps([r.__dict__ for r in results], indent=2, default=str),
            encoding="utf-8",
        )

    return len(failed)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", type=Path, default=CONFIG_FILE)
    ap.add_argument("--out-dir", type=Path, default=Path("."))
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch and diagnose, but write no XML")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="log every rejected entry")
    ap.add_argument("--fail-on-error", action="store_true",
                    help="exit non-zero if any source failed")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(message)s",
        stream=sys.stdout,
    )

    if not args.config.exists():
        log.error("Missing configuration file: %s", args.config)
        return 2

    with open(args.config, "rb") as fh:
        config = tomllib.load(fh)

    feeds = config.get("feed", [])
    if not feeds:
        log.error("No [[feed]] tables found in %s", args.config)
        return 2

    all_results: list[SourceResult] = []
    for feed_cfg in feeds:
        all_results.extend(build_single_feed(feed_cfg, args.out_dir, args.dry_run))

    failures = report(all_results, args.out_dir, args.dry_run)
    if args.fail_on_error and failures:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
