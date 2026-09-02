from datetime import datetime, timezone, timedelta
from pathlib import Path
import tomllib
import xml.etree.ElementTree as ET
from xml.dom import minidom
import feedparser

CONFIG_FILE = Path("feeds.toml")

NAMESPACES = {
    "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
    "content": "http://purl.org/rss/1.0/modules/content/",
}

for prefix, uri in NAMESPACES.items():
    ET.register_namespace(prefix, uri)


def load_config(file_path: Path) -> dict:
    if not file_path.exists():
        raise FileNotFoundError(f"Missing configuration file: {file_path}")
    with open(file_path, "rb") as f:
        return tomllib.load(f)


def build_single_feed(feed_config: dict) -> None:
    meta = feed_config.get("meta", {})

    # Check feed-level enabled flag
    if not meta.get("enabled", True):
        filename = meta.get("filename", "unknown.xml")
        print(f"Skipping disabled feed: {filename}")
        return

    filename = meta.get("filename", "feed.xml")
    title = meta.get("title", "Daily News Digest")
    description = meta.get("description", "Daily news bulletins.")
    image_url = meta.get("image_url")

    all_sources = feed_config.get("sources", [])

    # Filter enabled sources and split into priority groups
    primary_sources = [
        s
        for s in all_sources
        if s.get("enabled", True) and s.get("priority") == "primary"
    ]
    secondary_sources = [
        s
        for s in all_sources
        if s.get("enabled", True) and s.get("priority") == "secondary"
    ]

    # Order: Primary sources first, then Secondary sources
    ordered_sources = primary_sources + secondary_sources

    # Build XML tree
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")

    ET.SubElement(channel, "title").text = title
    ET.SubElement(channel, "description").text = description
    ET.SubElement(channel, "link").text = "https://github.com"
    ET.SubElement(channel, "language").text = "en"

    if image_url:
        img_element = ET.SubElement(channel, "image")
        ET.SubElement(img_element, "url").text = image_url
        ET.SubElement(img_element, "title").text = title
        ET.SubElement(img_element, "link").text = "https://github.com"

        ET.SubElement(channel, f"{{{NAMESPACES['itunes']}}}image", href=image_url)

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)

    for source in ordered_sources:
        feed_url = source.get("url")
        custom_name = source.get("name")
        priority_label = source.get("priority", "primary").upper()
        lang_code = source.get("lang", "").upper()

        parsed = feedparser.parse(feed_url)
        if not parsed.entries:
            continue

        for entry in parsed.entries:
            published_struct = entry.get("published_parsed") or entry.get(
                "updated_parsed"
            )
            if not published_struct:
                continue

            pub_date = datetime(*published_struct[:6], tzinfo=timezone.utc)
            if pub_date < cutoff:
                continue

            # Find audio enclosure
            enclosure_data = None
            for enc in entry.get("enclosures", []):
                if "audio" in enc.get("type", ""):
                    enclosure_data = enc
                    break
            if not enclosure_data and entry.get("enclosures"):
                enclosure_data = entry.get("enclosures")[0]

            if not enclosure_data:
                continue

            item = ET.SubElement(channel, "item")

            ep_title = entry.get("title", "Audio Bulletin")
            prefix = (
                f"[{lang_code}|{priority_label}] {custom_name}: "
                if lang_code
                else f"[{priority_label}] {custom_name}: "
            )
            ET.SubElement(item, "title").text = f"{prefix}{ep_title}"

            ET.SubElement(item, "link").text = entry.get("link", "")
            ET.SubElement(item, "pubDate").text = pub_date.strftime(
                "%a, %d %b %Y %H:%M:%S +0000"
            )
            ET.SubElement(item, "guid").text = entry.get(
                "id", entry.get("link", ep_title)
            )

            summary = entry.get("summary") or entry.get("description") or ""
            ET.SubElement(item, "description").text = summary

            ET.SubElement(
                item,
                "enclosure",
                url=enclosure_data.get("href", ""),
                length=str(enclosure_data.get("length", 0)),
                type=enclosure_data.get("type", "audio/mpeg"),
            )

            # Take strictly the newest valid episode per source
            break

    # Save output file
    xml_string = minidom.parseString(ET.tostring(rss)).toprettyxml(indent="  ")
    with open(filename, "w", encoding="utf-8") as f:
        f.write(xml_string)
    print(f"Successfully generated: {filename}")


def main():
    config = load_config(CONFIG_FILE)
    feed_list = config.get("feed", [])

    for feed_cfg in feed_list:
        build_single_feed(feed_cfg)


if __name__ == "__main__":
    main()
