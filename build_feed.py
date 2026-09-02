from datetime import datetime, timezone, timedelta
from pathlib import Path
import xml.etree.ElementTree as ET
from xml.dom import minidom
import feedparser

FEEDS_FILE = Path("feeds.txt")


def load_feed_urls(file_path: Path) -> list[str]:
    """Load feed URLs from a plain text file, skipping blank lines and comments."""
    if not file_path.exists():
        print(f"Warning: {file_path} not found.")
        return []

    urls = []
    for line in file_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        # Skip empty lines and comments
        if not line or line.startswith("#"):
            continue
        urls.append(line)
    return urls


# Load active feeds from feeds.txt
FEEDS = load_feed_urls(FEEDS_FILE)

# RSS namespace setup for iTunes / Podcast compatibility
NAMESPACES = {
    "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
    "content": "http://purl.org/rss/1.0/modules/content/",
}

for prefix, uri in NAMESPACES.items():
    ET.register_namespace(prefix, uri)

# Base RSS element setup
rss = ET.Element("rss", version="2.0")
channel = ET.SubElement(rss, "channel")

ET.SubElement(channel, "title").text = "My Personal News Digest"
ET.SubElement(channel, "description").text = (
    "Latest audio news bulletins updated hourly (within 24h)."
)
ET.SubElement(channel, "link").text = "https://github.com"
ET.SubElement(channel, "language").text = "en"

now = datetime.now(timezone.utc)
cutoff = now - timedelta(hours=24)

for feed_url in FEEDS:
    parsed = feedparser.parse(feed_url)
    if not parsed.entries:
        continue

    # Check entries for the newest valid post within 24 hours
    for entry in parsed.entries:
        published_struct = entry.get("published_parsed") or entry.get("updated_parsed")

        if published_struct:
            pub_date = datetime(*published_struct[:6], tzinfo=timezone.utc)
        else:
            continue

        # Filter: Skip entries older than 24 hours
        if pub_date < cutoff:
            continue

        # Find audio enclosure tag
        enclosure_data = None
        for enc in entry.get("enclosures", []):
            if "audio" in enc.get("type", ""):
                enclosure_data = enc
                break

        # Fallback to the first enclosure if explicit audio type is missing
        if not enclosure_data and entry.get("enclosures"):
            enclosure_data = entry.get("enclosures")[0]

        if not enclosure_data:
            continue  # Skip entries without audio streams

        # Construct item
        item = ET.SubElement(channel, "item")

        source_title = parsed.feed.get("title", "News Feed")
        ep_title = entry.get("title", "Audio Bulletin")
        ET.SubElement(item, "title").text = f"[{source_title}] {ep_title}"

        ET.SubElement(item, "link").text = entry.get("link", "")
        ET.SubElement(item, "pubDate").text = pub_date.strftime(
            "%a, %d %b %Y %H:%M:%S +0000"
        )
        ET.SubElement(item, "guid").text = entry.get("id", entry.get("link", ep_title))

        summary = entry.get("summary") or entry.get("description") or ""
        ET.SubElement(item, "description").text = summary

        ET.SubElement(
            item,
            "enclosure",
            url=enclosure_data.get("href", ""),
            length=str(enclosure_data.get("length", 0)),
            type=enclosure_data.get("type", "audio/mpeg"),
        )

        # Take strictly the newest matching entry per podcast feed
        break

# Format clean XML output
xml_string = minidom.parseString(ET.tostring(rss)).toprettyxml(indent="  ")

with open("feed.xml", "w", encoding="utf-8") as f:
    f.write(xml_string)
