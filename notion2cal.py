#!/usr/bin/env python3
"""Fetch a Notion database and export all dated entries as an .ics calendar file."""

import os
import sys
from datetime import datetime, date, timedelta, timezone

import requests
from icalendar import Calendar, Event


NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID")
OUTPUT_FILE = os.environ.get("OUTPUT_FILE", "notion_calendar.ics")

NOTION_API_VERSION = "2022-06-28"
NOTION_API_BASE = "https://api.notion.com/v1"


def query_database(database_id: str) -> list[dict]:
    """Query all pages from a Notion database, handling pagination."""
    url = f"{NOTION_API_BASE}/databases/{database_id}/query"

    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_API_VERSION,
        "Content-Type": "application/json",
    }

    results = []
    payload: dict = {}

    while True:
        resp = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=30,
        )

        resp.raise_for_status()

        data = resp.json()

        results.extend(data["results"])

        if not data.get("has_more"):
            break

        payload["start_cursor"] = data["next_cursor"]

    return results


def find_date_property(properties: dict) -> tuple[str, dict] | None:
    """Find the first populated date property on a Notion page."""
    for name, prop in properties.items():
        if prop["type"] == "date" and prop.get("date"):
            return name, prop["date"]

    return None


def get_title(properties: dict) -> str:
    """Extract the title from a Notion page."""
    for prop in properties.values():
        if prop["type"] == "title":
            parts = prop.get("title", [])

            title = "".join(
                part.get("plain_text", "")
                for part in parts
            )

            if title:
                return title

    return "Untitled"


def get_rich_text(properties: dict, name: str) -> str:
    """Extract plain text from a rich_text property."""
    prop = properties.get(name)

    if not prop:
        return ""

    if prop["type"] != "rich_text":
        return ""

    return "".join(
        part.get("plain_text", "")
        for part in prop.get("rich_text", [])
    )


def find_description(properties: dict) -> str:
    """Try common Notion property names for a description."""
    for candidate in (
        "Description",
        "Beschreibung",
        "Notes",
        "Notizen",
        "Text",
    ):
        text = get_rich_text(properties, candidate)

        if text:
            return text

    # Fallback:
    # Use the first non-empty rich_text property.
    for prop in properties.values():
        if prop["type"] != "rich_text":
            continue

        text = "".join(
            part.get("plain_text", "")
            for part in prop.get("rich_text", [])
        )

        if text:
            return text

    return ""


def parse_notion_date(value: str) -> datetime | date:
    """
    Parse a Notion date value.

    Date-only values such as:
        2026-09-08

    become Python date objects and are exported as all-day events.

    Timed values such as:
        2026-09-08T18:00:00.000-04:00

    become timezone-aware datetime objects.
    """

    # No "T" means Notion provided a date without a time.
    if "T" not in value:
        return date.fromisoformat(value)

    # Handle normal ISO 8601 values and UTC "Z".
    return datetime.fromisoformat(
        value.replace("Z", "+00:00")
    )


def build_calendar(pages: list[dict]) -> Calendar:
    """Build an iCalendar calendar from Notion pages."""
    cal = Calendar()

    cal.add("prodid", "-//notion2cal//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", "Notion Calendar")

    skipped = 0

    for page in pages:
        properties = page.get("properties", {})

        date_info = find_date_property(properties)

        if not date_info:
            skipped += 1
            continue

        _, date_data = date_info

        start_raw = date_data.get("start")
        end_raw = date_data.get("end")

        if not start_raw:
            skipped += 1
            continue

        title = get_title(properties)
        description = find_description(properties)

        start = parse_notion_date(start_raw)

        end = (
            parse_notion_date(end_raw)
            if end_raw
            else None
        )

        event = Event()

        event.add("summary", title)

        # --------------------------------------------------
        # ALL-DAY EVENTS
        # --------------------------------------------------

        is_all_day = (
            isinstance(start, date)
            and not isinstance(start, datetime)
        )

        if is_all_day:
            # Example:
            #
            # Notion:
            # September 8
            #
            # ICS:
            # DTSTART;VALUE=DATE:20260908
            # DTEND;VALUE=DATE:20260909
            #
            # DTEND is exclusive in iCalendar.

            event.add("dtstart", start)

            if end:
                if isinstance(end, datetime):
                    raise ValueError(
                        f"Event '{title}' has a date-only start "
                        f"but a timed end."
                    )

                # Notion date ranges include the selected end day.
                # iCalendar DTEND is exclusive, so add one day.
                event.add(
                    "dtend",
                    end + timedelta(days=1),
                )

            else:
                # Single-day all-day event.
                event.add(
                    "dtend",
                    start + timedelta(days=1),
                )

        # --------------------------------------------------
        # TIMED EVENTS
        # --------------------------------------------------

        else:
            if start.tzinfo is None:
                raise ValueError(
                    f"Timed event '{title}' has no timezone: "
                    f"{start_raw}"
                )

            # Convert to UTC for maximum compatibility with
            # Outlook, Google Calendar, Apple Calendar, etc.
            start_utc = start.astimezone(timezone.utc)

            event.add(
                "dtstart",
                start_utc,
            )

            if end:
                if not isinstance(end, datetime):
                    raise ValueError(
                        f"Event '{title}' has a timed start "
                        f"but a date-only end."
                    )

                if end.tzinfo is None:
                    raise ValueError(
                        f"Timed event '{title}' has an end "
                        f"without a timezone."
                    )

                end_utc = end.astimezone(timezone.utc)

                event.add(
                    "dtend",
                    end_utc,
                )

            else:
                # If Notion only provides a start time,
                # default the event duration to one hour.
                event.add(
                    "dtend",
                    start_utc + timedelta(hours=1),
                )

        # --------------------------------------------------
        # EVENT METADATA
        # --------------------------------------------------

        if description:
            event.add(
                "description",
                description,
            )

        # Stable UID:
        # keeps the same event identity between calendar refreshes.
        event.add(
            "uid",
            f"{page['id']}@notion2cal",
        )

        # DTSTAMP should always be UTC.
        event.add(
            "dtstamp",
            datetime.now(timezone.utc),
        )

        # Calendar compatibility metadata.
        event.add(
            "sequence",
            0,
        )

        event.add(
            "status",
            "CONFIRMED",
        )

        # OPAQUE means the event normally counts as "busy".
        event.add(
            "transp",
            "OPAQUE",
        )

        page_url = page.get("url")

        if page_url:
            event.add(
                "url",
                page_url,
            )

        cal.add_component(event)

    event_count = len(cal.subcomponents)

    print(
        f"Processed {len(pages)} pages: "
        f"{event_count} events created, "
        f"{skipped} skipped (no date)"
    )

    return cal


def main() -> None:
    """Run the Notion-to-calendar export."""

    if not NOTION_TOKEN:
        print(
            "Error: NOTION_TOKEN environment variable "
            "is not set.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not NOTION_DATABASE_ID:
        print(
            "Error: NOTION_DATABASE_ID environment variable "
            "is not set.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(
        f"Querying Notion database "
        f"{NOTION_DATABASE_ID[:8]}..."
    )

    pages = query_database(
        NOTION_DATABASE_ID
    )

    print(
        f"Fetched {len(pages)} pages from Notion."
    )

    cal = build_calendar(
        pages
    )

    with open(
        OUTPUT_FILE,
        "wb",
    ) as file:
        file.write(
            cal.to_ical()
        )

    print(
        f"Calendar written to {OUTPUT_FILE}"
    )


if __name__ == "__main__":
    main()
