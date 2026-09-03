"""Scrapers for sources that publish audio but no RSS feed.

Each scraper is a function registered in SCRAPERS. It takes the source config
dict and a `fetch` callable, and returns a list of ScrapedItem, newest first.
Raise ScrapeError with a human-readable message when something is wrong; the
builder turns that into a SCRAPE_ERROR line in the report.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timezone

from bs4 import BeautifulSoup


class ScrapeError(RuntimeError):
    pass


@dataclass
class ScrapedItem:
    title: str
    link: str
    audio_url: str
    published: datetime
    summary: str = ""
    audio_type: str = "audio/mp4"
    audio_length: int = 0

    def as_entry(self) -> dict:
        """Shaped like a feedparser entry so the builder can treat it the same."""
        return {
            "title": self.title,
            "link": self.link,
            "id": self.audio_url,
            "summary": self.summary,
        }

    def as_enclosure(self) -> dict:
        return {
            "href": self.audio_url,
            "type": self.audio_type,
            "length": self.audio_length,
        }


# --------------------------------------------------------------------------
# hadshon.education.gov.il - "חדשון בעברית קלה"
# --------------------------------------------------------------------------

HADSHON_URL = "https://hadshon.education.gov.il/"

# Audio filenames are inconsistent between item types:
#   daily    /hadshon/2026/9/2-9-26.m4a
#   weather  /hadshon/2026/9/2.9tachazit.m4a
#   shabbat  /hadshon/2026/8/30.8shabbat.m4a
# So the URLs are read off the page rather than constructed. Do not "optimise"
# this into a date-formatted URL; the next filename will break it.
HADSHON_AUDIO_RE = re.compile(
    r"https://tum-files\.education\.gov\.il/hadshon/\d{4}/\d{1,2}/[^\s\"'<>]+?\.m4a",
    re.IGNORECASE,
)
HADSHON_DATE_RE = re.compile(r"\b(\d{2})/(\d{2})/(\d{4})\b")

HADSHON_VARIANTS = {
    "daily": lambda name: "tachazit" not in name and "shabbat" not in name,
    "weather": lambda name: "tachazit" in name,
    "shabbat": lambda name: "shabbat" in name,
}

# The bulletin goes up in the Israeli morning. The page carries a date but no
# time, so pin it to 05:00 UTC (08:00 local) rather than midnight; that keeps a
# 24h max_age window from expiring the episode mid-morning the next day.
HADSHON_PUBLISH_HOUR = time(5, 0, tzinfo=timezone.utc)


def scrape_hadshon(source: dict, fetch) -> list[ScrapedItem]:
    variant = source.get("variant", "daily")
    if variant not in HADSHON_VARIANTS:
        raise ScrapeError(
            f"unknown variant {variant!r}; expected one of {', '.join(HADSHON_VARIANTS)}"
        )

    resp = fetch(source.get("url") or HADSHON_URL)
    html = resp.text
    soup = BeautifulSoup(html, "html.parser")

    urls = []
    for match in HADSHON_AUDIO_RE.finditer(html):
        if match.group(0) not in urls:
            urls.append(match.group(0))
    if not urls:
        raise ScrapeError(
            "no .m4a links found on the page; the site layout probably changed"
        )

    matches = [u for u in urls if HADSHON_VARIANTS[variant](u.rsplit("/", 1)[-1].lower())]
    if not matches:
        raise ScrapeError(
            f"found {len(urls)} audio link(s) but none matching variant {variant!r}: "
            + ", ".join(u.rsplit("/", 1)[-1] for u in urls)
        )
    audio_url = matches[0]

    # Date the item from its own audio URL, not from the page. The weekly
    # parasha file stays linked all week, so the page date would keep marking
    # a stale file as fresh. The path carries /YYYY/M/ and the filename opens
    # with the day: 2-9-26.m4a, 2.9tachazit.m4a, 30.8shabbat.m4a.
    day = month = year = None
    path_match = re.search(r"/hadshon/(\d{4})/(\d{1,2})/([^/]+)$", audio_url)
    if path_match:
        year, month = int(path_match.group(1)), int(path_match.group(2))
        day_match = re.match(r"(\d{1,2})", path_match.group(3))
        if day_match:
            day = int(day_match.group(1))

    if not (day and month and year):
        # Fall back to the dd/mm/yyyy the page prints beside the day's headline.
        date_match = HADSHON_DATE_RE.search(soup.get_text(" "))
        if not date_match:
            raise ScrapeError(
                f"could not date {audio_url.rsplit('/', 1)[-1]} from its URL, "
                "and found no dd/mm/yyyy date on the page"
            )
        day, month, year = (int(g) for g in date_match.groups())

    try:
        item_date = datetime(year, month, day).date()
    except ValueError as exc:
        raise ScrapeError(f"bad date {day}/{month}/{year} from {audio_url}: {exc}")

    published = datetime.combine(item_date, HADSHON_PUBLISH_HOUR)

    # Headlines: the h3 elements under the main bulletin.
    headlines = [
        h.get_text(" ", strip=True)
        for h in soup.find_all("h3")
        if h.get_text(strip=True)
    ][:12]
    summary = "\n".join(f"• {h}" for h in headlines)

    titles = {
        "daily": "חדשון בעברית קלה",
        "weather": "תחזית מזג האוויר",
        "shabbat": "פרשת השבוע",
    }
    title = f"{titles[variant]} — {day:02d}/{month:02d}/{year}"

    length = 0
    try:
        head = fetch(audio_url, method="head")
        length = int(head.headers.get("Content-Length") or 0)
    except Exception:  # noqa: BLE001 - length is cosmetic, never fail on it
        pass

    return [
        ScrapedItem(
            title=title,
            link=HADSHON_URL,
            audio_url=audio_url,
            published=published,
            summary=summary,
            audio_type="audio/mp4",
            audio_length=length,
        )
    ]


SCRAPERS = {
    "hadshon": scrape_hadshon,
}
