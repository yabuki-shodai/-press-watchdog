from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "exchanges.yml"
SEEN_PATH = ROOT / "data" / "seen.json"
DOCS_DIR = ROOT / "docs"
README_PATH = ROOT / "README.md"
TIMEZONE = timezone(timedelta(hours=9))
USER_AGENT = "press-watchdog/0.1 (+https://github.com/yabuki-shodai/-press-watchdog)"
REQUEST_TIMEOUT = 20
MAX_ITEMS_PER_SOURCE = 30
README_START = "<!-- press-watchdog:today:start -->"
README_END = "<!-- press-watchdog:today:end -->"

AI_SUMMARY_ENABLED = os.environ.get("AI_SUMMARY_ENABLED", "false").lower() == "true"
AI_API_KEY = os.environ.get("AI_API_KEY", "")
AI_API_URL = os.environ.get("AI_API_URL", "https://api.openai.com/v1/chat/completions")
AI_MODEL = os.environ.get("AI_MODEL", "gpt-4o-mini")

IGNORE_HREF_PREFIXES = ("#", "mailto:", "tel:", "javascript:")
TRACKING_QUERY_PREFIXES = ("utm_",)
TRACKING_QUERY_KEYS = {"fbclid", "gclid", "yclid", "mc_cid", "mc_eid"}

ARTICLE_KEYWORDS = (
    "news",
    "info",
    "notice",
    "press",
    "release",
    "announcement",
    "maintenance",
    "important",
    "お知らせ",
    "ニュース",
    "プレス",
    "リリース",
    "メンテナンス",
    "障害",
    "重要",
    "取扱",
    "上場",
)

NAVIGATION_KEYWORDS = (
    "一覧",
    "カテゴリ",
    "タグ",
    "キャンペーン一覧",
    "ニュース一覧",
    "お知らせ一覧",
    "メンテナンス情報",
    "報道関係者",
    "用語集",
    "暗号資産とは",
    "取扱暗号資産",
    "キャンペーン",
    "next",
    "次へ",
    "prev",
    "previous",
    "back",
    "more",
    "news",
    "info",
)

EXCLUDED_PATH_PARTS = (
    "/login",
    "/signup",
    "/register",
    "/contact",
    "/privacy",
    "/terms",
    "/policy",
    "/about/press",
    "/campaign",
    "/campaigns",
    "/guide",
    "/knowledge",
    "/columns",
    "/services/",
    "/crypto-assets/",
    "/tag/",
    "/tags/",
    "/category/",
    "/categories/",
    "/page/",
)

DATE_PATTERNS = (
    re.compile(r"/20\d{2}[/-]?\d{2}[/-]?\d{2}(?:\D|$)"),
    re.compile(r"/20\d{6}(?:\D|$)"),
    re.compile(r"\b20\d{2}[./-]\d{1,2}[./-]\d{1,2}\b"),
    re.compile(r"\b20\d{2}\s*/\s*\d{1,2}\s*/\s*\d{1,2}\b"),
)


@dataclass(frozen=True)
class LinkItem:
    exchange_id: str
    exchange_name: str
    title: str
    url: str
    source_url: str


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_seen() -> tuple[dict[str, Any], bool]:
    if not SEEN_PATH.exists():
        return {"seen_urls": {}}, True
    with SEEN_PATH.open("r", encoding="utf-8") as f:
        return json.load(f), False


def save_seen(seen: dict[str, Any]) -> None:
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SEEN_PATH.open("w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    path = re.sub(r"/{2,}", "/", parsed.path)
    normalized = parsed._replace(path=path, fragment="").geturl()
    return normalized.rstrip("/")


def canonicalize_url(url: str) -> str:
    parsed = urlparse(normalize_url(url))
    query_pairs = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        key_lower = key.lower()
        if key_lower in TRACKING_QUERY_KEYS:
            continue
        if any(key_lower.startswith(prefix) for prefix in TRACKING_QUERY_PREFIXES):
            continue
        query_pairs.append((key, value))

    normalized_query = urlencode(sorted(query_pairs), doseq=True)
    canonical = parsed._replace(
        scheme=parsed.scheme.lower(),
        netloc=parsed.netloc.lower(),
        query=normalized_query,
        fragment="",
    )
    return urlunparse(canonical).rstrip("/")


def title_key(title: str) -> str:
    return re.sub(r"\W+", "", title.lower())


def deduplicate_links(items: list[LinkItem]) -> list[LinkItem]:
    unique: list[LinkItem] = []
    seen_url_keys: set[str] = set()
    seen_title_keys: set[tuple[str, str]] = set()

    for item in items:
        url_key = canonicalize_url(item.url)
        title_dedupe_key = (item.exchange_id, title_key(item.title))
        if url_key in seen_url_keys:
            continue
        if title_dedupe_key in seen_title_keys:
            continue

        seen_url_keys.add(url_key)
        seen_title_keys.add(title_dedupe_key)
        unique.append(item)

    return unique


def same_site_or_subdomain(candidate_url: str, source_url: str) -> bool:
    candidate_host = urlparse(candidate_url).hostname or ""
    source_host = urlparse(source_url).hostname or ""
    if not candidate_host or not source_host:
        return False
    return candidate_host == source_host or candidate_host.endswith(f".{source_host}") or source_host.endswith(f".{candidate_host}")


def has_date_signal(url: str, title: str) -> bool:
    target = f"{url} {title}"
    return any(pattern.search(target) for pattern in DATE_PATTERNS)


def has_article_keyword(url: str, title: str) -> bool:
    path = urlparse(url).path.lower()
    title_lower = title.lower()
    return any(keyword.lower() in path or keyword.lower() in title_lower for keyword in ARTICLE_KEYWORDS)


def is_navigation_link(url: str, title: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.lower().rstrip("/")
    title_lower = title.lower()

    if any(part in path for part in EXCLUDED_PATH_PARTS):
        return True
    if any(title_lower == keyword.lower() for keyword in NAVIGATION_KEYWORDS):
        return True
    if re.search(r"/(news|info|notice|announcement|press|release)$", path):
        return True
    return False


def is_probably_article(url: str, title: str, source_url: str) -> bool:
    parsed = urlparse(url)

    if not parsed.scheme.startswith("http"):
        return False
    if not same_site_or_subdomain(url, source_url):
        return False
    if len(title) < 6:
        return False
    if is_navigation_link(url, title):
        return False

    # A date in the URL or title is the strongest general signal for Japanese release pages.
    if has_date_signal(url, title):
        return True

    # Some services use opaque IDs such as /newsview/abc123. Keep those only when the
    # title clearly looks like an actual announcement, not a navigation label.
    path = parsed.path.lower()
    if any(part in path for part in ("/newsview/", "/news/", "/info/", "/announcement/")):
        return has_article_keyword(url, title) and len(title) >= 12

    return False


def fetch_html(url: str) -> str | None:
    try:
        response = requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "ja,en;q=0.8"},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        print(f"[warn] failed to fetch {url}: {exc}")
        return None

    content_type = response.headers.get("content-type", "")
    if "text/html" not in content_type and "application/xhtml" not in content_type:
        print(f"[warn] skipped non-html content {url}: {content_type}")
        return None

    response.encoding = response.apparent_encoding or response.encoding
    return response.text


def extract_links(exchange: dict[str, Any], source_url: str) -> list[LinkItem]:
    html = fetch_html(source_url)
    if html is None:
        return []

    soup = BeautifulSoup(html, "html.parser")
    items: list[LinkItem] = []
    seen_urls: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href", "")).strip()
        if not href or href.startswith(IGNORE_HREF_PREFIXES):
            continue

        title = normalize_space(anchor.get_text(" "))
        absolute_url = normalize_url(urljoin(source_url, href))
        canonical_url = canonicalize_url(absolute_url)
        if canonical_url in seen_urls:
            continue
        if not is_probably_article(absolute_url, title, source_url):
            continue

        seen_urls.add(canonical_url)
        items.append(
            LinkItem(
                exchange_id=str(exchange["id"]),
                exchange_name=str(exchange["name"]),
                title=title,
                url=canonical_url,
                source_url=source_url,
            )
        )

    return items[:MAX_ITEMS_PER_SOURCE]


def collect_links(config: dict[str, Any]) -> list[LinkItem]:
    collected: list[LinkItem] = []
    for exchange in config.get("exchanges", []):
        if not exchange.get("enabled", True):
            continue
        for source_url in exchange.get("press_urls", []) or []:
            collected.extend(extract_links(exchange, source_url))
    return deduplicate_links(collected)


def generate_ai_summary(new_items: list[LinkItem], is_initial_run: bool) -> str | None:
    if is_initial_run or not new_items:
        return None
    if not AI_SUMMARY_ENABLED:
        return None
    if not AI_API_KEY:
        print("[warn] AI_SUMMARY_ENABLED is true but AI_API_KEY is not set")
        return None

    input_lines = [f"- {item.exchange_name}: {item.title} ({item.url})" for item in new_items[:50]]
    prompt = "\n".join(
        [
            "以下は暗号資産交換業者のお知らせ・プレスリリースの新着一覧です。",
            "重要度の高い順に、重複を避けて日本語で簡潔に要約してください。",
            "出力はMarkdownで、最大5件の箇条書きにしてください。",
            "価格予測や投資助言は書かないでください。",
            "",
            *input_lines,
        ]
    )

    try:
        response = requests.post(
            AI_API_URL,
            headers={
                "Authorization": f"Bearer {AI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": AI_MODEL,
                "messages": [
                    {"role": "system", "content": "You summarize crypto exchange press releases for monitoring. Be concise and factual."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.2,
            },
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
        return payload["choices"][0]["message"]["content"].strip()
    except Exception as exc:  # noqa: BLE001 - keep the watcher alive if AI fails.
        print(f"[warn] failed to generate AI summary: {exc}")
        return None


def build_markdown(
    date_text: str,
    new_items: list[LinkItem],
    all_count: int,
    is_initial_run: bool,
    ai_summary: str | None,
) -> str:
    lines = [
        f"# Press release watch: {date_text}",
        "",
        f"- Initial baseline: {'yes' if is_initial_run else 'no'}",
        f"- New items: {0 if is_initial_run else len(new_items)}",
        f"- Collected links: {all_count}",
        f"- AI summary: {'enabled' if AI_SUMMARY_ENABLED else 'disabled'}",
        "",
    ]

    if ai_summary:
        lines.extend(["## AI Summary", "", ai_summary, ""])

    if is_initial_run:
        lines.append("Initial baseline created. Existing links were saved to data/seen.json and are not reported as new items.")
        lines.append("")
        return "\n".join(lines)

    if not new_items:
        lines.append("No new press release links detected.")
        lines.append("")
        return "\n".join(lines)

    current_exchange = None
    for item in new_items:
        if item.exchange_name != current_exchange:
            current_exchange = item.exchange_name
            lines.extend(["", f"## {current_exchange}", ""])
        lines.append(f"- [{item.title}]({item.url})")
        lines.append(f"  - source: {item.source_url}")

    lines.append("")
    return "\n".join(lines)


def build_summary(
    date_text: str,
    new_items: list[LinkItem],
    all_count: int,
    is_initial_run: bool,
    ai_summary: str | None,
) -> str:
    report_path = f"docs/{date_text}.md"
    lines = [
        f"# Press release watch: {date_text}",
        "",
        f"- Report: [{report_path}]({report_path})",
        f"- Initial baseline: {'yes' if is_initial_run else 'no'}",
        f"- New items: {0 if is_initial_run else len(new_items)}",
        f"- Collected links: {all_count}",
        f"- AI summary: {'enabled' if AI_SUMMARY_ENABLED else 'disabled'}",
        "",
    ]

    if ai_summary:
        lines.extend(["## AI Summary", "", ai_summary, ""])

    if is_initial_run:
        lines.append("Initial baseline created. Existing links are not shown as new items.")
        lines.append("")
        return "\n".join(lines)

    if not new_items:
        lines.append("No new press release links detected.")
        lines.append("")
        return "\n".join(lines)

    lines.append("## New items")
    lines.append("")
    for item in new_items[:20]:
        lines.append(f"- **{item.exchange_name}**: [{item.title}]({item.url})")
    if len(new_items) > 20:
        lines.append(f"- ...and {len(new_items) - 20} more")
    lines.append("")
    return "\n".join(lines)


def write_github_step_summary(summary: str) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    with open(summary_path, "a", encoding="utf-8") as f:
        f.write(summary)
        f.write("\n")


def update_readme_today_link(date_text: str) -> None:
    report_path = f"docs/{date_text}.md"
    block = "\n".join(
        [
            README_START,
            "## Today's Report",
            "",
            f"- [{date_text}]({report_path})",
            README_END,
        ]
    )

    if README_PATH.exists():
        content = README_PATH.read_text(encoding="utf-8")
    else:
        content = "# -press-watchdog\n"

    pattern = re.compile(f"{re.escape(README_START)}.*?{re.escape(README_END)}", re.DOTALL)
    if pattern.search(content):
        content = pattern.sub(block, content)
    else:
        lines = content.splitlines()
        if lines and lines[0].startswith("# "):
            content = "\n".join([lines[0], "", block, "", *lines[1:]])
        else:
            content = f"{block}\n\n{content}"

    README_PATH.write_text(content.rstrip() + "\n", encoding="utf-8")


def main() -> None:
    now = datetime.now(TIMEZONE)
    date_text = now.strftime("%Y-%m-%d")
    config = load_config()
    seen, is_initial_run = load_seen()
    seen_urls: dict[str, str] = seen.setdefault("seen_urls", {})

    all_items = collect_links(config)
    new_items: list[LinkItem] = []

    for item in all_items:
        seen_key = canonicalize_url(item.url)
        if not is_initial_run and seen_key not in seen_urls:
            new_items.append(item)
        seen_urls[seen_key] = now.isoformat()

    new_items = deduplicate_links(new_items)
    ai_summary = generate_ai_summary(new_items, is_initial_run)

    seen["updated_at"] = now.isoformat()
    seen["source"] = "scripts/watch_press_releases.py"
    seen["baseline_created_at"] = seen.get("baseline_created_at") or (now.isoformat() if is_initial_run else None)
    save_seen(seen)

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    markdown = build_markdown(date_text, new_items, len(all_items), is_initial_run, ai_summary)
    (DOCS_DIR / f"{date_text}.md").write_text(markdown, encoding="utf-8")

    summary = build_summary(date_text, new_items, len(all_items), is_initial_run, ai_summary)
    write_github_step_summary(summary)
    update_readme_today_link(date_text)

    print(f"Initial baseline: {'yes' if is_initial_run else 'no'}")
    print(f"Collected links: {len(all_items)}")
    print(f"New links: {0 if is_initial_run else len(new_items)}")
    print(f"AI summary: {'enabled' if AI_SUMMARY_ENABLED else 'disabled'}")


if __name__ == "__main__":
    main()
