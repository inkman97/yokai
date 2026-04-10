"""Branch name generation from a configurable template.

The template supports these placeholders:
- {issue_key}    the story key as-is (e.g. NOVA-101)
- {issue_key_lc} the story key lowercased
- {timestamp}    unix timestamp at generation time
- {slug}         a slugified version of the story title (lowercase, hyphens)

Example templates:
- "feature/{issue_key}-ai-{timestamp}"
- "ai/{issue_key_lc}-{slug}"
- "bot/{issue_key}"
"""

from __future__ import annotations

import re
import time


_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")


def slugify(text: str, max_length: int = 40) -> str:
    text = text.lower()
    text = _SLUG_PATTERN.sub("-", text).strip("-")
    if len(text) > max_length:
        text = text[:max_length].rstrip("-")
    return text or "story"


def render_branch_name(
    template: str,
    issue_key: str,
    title: str = "",
    timestamp: int | None = None,
) -> str:
    ts = timestamp if timestamp is not None else int(time.time())
    return template.format(
        issue_key=issue_key,
        issue_key_lc=issue_key.lower(),
        timestamp=ts,
        slug=slugify(title),
    )
