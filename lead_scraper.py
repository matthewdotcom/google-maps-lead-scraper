"""
Google Maps Lead Scraper

Scrapes business leads (Name, Phone, Rating, Website, Address) for a search
query such as 'Coffee Shops in Jakarta' and exports cleaned results to CSV.

Usage:
    python lead_scraper.py "Coffee Shops in Jakarta" --max-results 50
"""
from __future__ import annotations

import argparse
import logging
import random
import re
import time
from dataclasses import asdict, dataclass
from functools import wraps
from typing import Callable, Optional
from urllib.parse import quote_plus

import pandas as pd
from playwright.sync_api import Browser, BrowserContext, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger('lead_scraper')

MAPS_SEARCH_URL = 'https://www.google.com/maps/search/{query}?hl=en'
DEFAULT_OUTPUT = 'leads_output.csv'
COLUMNS = ['Name', 'Phone', 'Rating', 'Website', 'Address']

USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
]

VIEWPORTS = [
    {'width': 1366, 'height': 768},
    {'width': 1440, 'height': 900},
    {'width': 1920, 'height': 1080},
]

# Hides common automation fingerprints before any page script runs.
STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
window.chrome = window.chrome || { runtime: {} };
"""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class Lead:
    name: str = ''
    phone: str = ''
    rating: Optional[float] = None
    website: str = ''
    address: str = ''


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------
def retry(max_attempts: int = 3, base_delay: float = 2.0,
          exceptions: tuple = (Exception,)) -> Callable:
    """Retry a function with exponential backoff and jitter."""
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    if attempt == max_attempts:
                        logger.error('%s failed after %d attempts: %s',
                                     func.__name__, attempt, exc)
                        raise
                    delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 1)
                    logger.warning('%s attempt %d/%d failed (%s). Retrying in %.1fs',
                                   func.__name__, attempt, max_attempts, exc, delay)
                    time.sleep(delay)
        return wrapper
    return decorator


def human_delay(min_s: float = 0.8, max_s: float = 2.0) -> None:
    """Sleep a random, human-like interval."""
    time.sleep(random.uniform(min_s, max_s))


def build_stealth_headers() -> dict:
    """Realistic browser headers sent with every request."""
    # Only headers that are safe on EVERY request (scripts, XHR, images).
    # Navigation-only headers (Accept: text/html, Sec-Fetch-*) break page loads.
    return {
        'Accept-Language': 'en-US,en;q=0.9',
        'DNT': '1',
    }


def parse_rating(raw: str) -> Optional[float]:
    """Convert '4,6' or '4.6' into a float, else None."""
    match = re.search(r'[0-9]+(?:[.,][0-9]+)?', raw or '')
    if not match:
        return None
    try:
        value = float(match.group().replace(',', '.'))
    except ValueError:
        return None
    return value if 0 <= value <= 5 else None


# ---------------------------------------------------------------------------
# Browser setup
# ---------------------------------------------------------------------------
def launch_browser(playwright, headless: bool = True) -> Browser:
    return playwright.chromium.launch(
        headless=headless,
        args=['--disable-blink-features=AutomationControlled', '--no-sandbox'],
    )


def create_stealth_context(browser: Browser) -> BrowserContext:
    # Use the browser's real user agent: a spoofed UA that doesn't match the
    # actual Chrome version is an easy bot signal and can break Google pages.
    context = browser.new_context(
        viewport=random.choice(VIEWPORTS),
        locale='en-US',
        timezone_id='Asia/Jakarta',
        extra_http_headers=build_stealth_headers(),
    )
    context.add_init_script(STEALTH_INIT_SCRIPT)
    context.set_default_timeout(30_000)
    return context


# ---------------------------------------------------------------------------
# Page helpers
# ---------------------------------------------------------------------------
def dismiss_consent(page: Page) -> None:
    """Accept Google's cookie consent dialog if it appears (EU/region specific)."""
    for label in ('Accept all', 'Reject all'):
        button = page.get_by_role('button', name=label)
        try:
            if button.count():
                button.first.click(timeout=3_000)
                human_delay(0.5, 1.0)
                return
        except PlaywrightTimeoutError:
            continue


def safe_get(page: Page, selector: str, attr: Optional[str] = None) -> str:
    """Return element text/attribute, or '' if missing. Never raises on absence."""
    locator = page.locator(selector).first
    try:
        if locator.count() == 0:
            return ''
        value = locator.get_attribute(attr, timeout=3_000) if attr \
            else locator.inner_text(timeout=3_000)
        return (value or '').strip()
    except PlaywrightTimeoutError:
        return ''


# ---------------------------------------------------------------------------
# Scraping steps
# ---------------------------------------------------------------------------
@retry(max_attempts=3, exceptions=(PlaywrightTimeoutError,))
def open_search(page: Page, query: str) -> None:
    url = MAPS_SEARCH_URL.format(query=quote_plus(query))
    logger.info('Opening search: %s', query)
    page.goto(url, wait_until='domcontentloaded', timeout=60_000)
    dismiss_consent(page)
    page.wait_for_selector('div[role="feed"], h1', timeout=20_000)


def collect_listing_urls(page: Page, max_results: int, max_stale_scrolls: int = 5) -> list[str]:
    """Scroll the results feed and collect unique place URLs."""
    if page.locator('div[role="feed"]').count() == 0:
        logger.info('Single result page detected.')
        return [page.url]

    feed = page.locator('div[role="feed"]')
    urls: list[str] = []
    seen: set[str] = set()
    stale = 0

    while len(urls) < max_results and stale < max_stale_scrolls:
        before = len(urls)
        hrefs = page.eval_on_selector_all('a.hfpxzc', 'els => els.map(e => e.href)')
        for href in hrefs:
            if href and href not in seen:
                seen.add(href)
                urls.append(href)

        if page.get_by_text('reached the end of the list').count():
            logger.info('Reached end of results list.')
            break

        stale = stale + 1 if len(urls) == before else 0
        feed.evaluate('el => el.scrollBy(0, el.scrollHeight)')
        human_delay(1.2, 2.5)
        logger.info('Collected %d listing URLs...', len(urls))

    return urls[:max_results]


@retry(max_attempts=3, exceptions=(PlaywrightTimeoutError,))
def scrape_listing(page: Page, url: str) -> Lead:
    """Open a single place page and extract lead details."""
    page.goto(url, wait_until='domcontentloaded', timeout=45_000)
    page.wait_for_selector('h1', timeout=15_000)
    human_delay(0.8, 1.6)

    return Lead(
        name=safe_get(page, 'h1.DUwDvf') or safe_get(page, 'h1'),
        phone=safe_get(page, 'button[data-item-id^="phone:tel:"]', 'aria-label'),
        rating=parse_rating(safe_get(page, 'div.F7nice span[aria-hidden="true"]')),
        website=safe_get(page, 'a[data-item-id="authority"]', 'href'),
        address=safe_get(page, 'button[data-item-id="address"]', 'aria-label'),
    )


def scrape_leads(query: str, max_results: int = 50, headless: bool = True) -> list[Lead]:
    """End-to-end scrape: search, collect listings, extract each lead."""
    leads: list[Lead] = []
    with sync_playwright() as playwright:
        browser = launch_browser(playwright, headless=headless)
        context = create_stealth_context(browser)
        page = context.new_page()
        try:
            open_search(page, query)
            urls = collect_listing_urls(page, max_results)
            logger.info('Found %d listings. Extracting details...', len(urls))

            for index, url in enumerate(urls, start=1):
                try:
                    lead = scrape_listing(page, url)
                    leads.append(lead)
                    logger.info('[%d/%d] %s', index, len(urls), lead.name or '(no name)')
                except Exception as exc:  # keep going on per-listing failures
                    logger.warning('[%d/%d] Skipped listing: %s', index, len(urls), exc)
        finally:
            context.close()
            browser.close()
    return leads


# ---------------------------------------------------------------------------
# Cleaning & export
# ---------------------------------------------------------------------------
def clean_leads(leads: list[Lead]) -> pd.DataFrame:
    """Normalize fields, drop empty rows and duplicates."""
    df = pd.DataFrame([asdict(lead) for lead in leads])
    if df.empty:
        return pd.DataFrame(columns=COLUMNS)

    df.columns = [col.title() for col in df.columns]
    for col in ('Name', 'Phone', 'Website', 'Address'):
        df[col] = df[col].fillna('').astype(str).str.strip()

    df['Address'] = df['Address'].apply(lambda s: s.removeprefix('Address: ').strip())
    df['Phone'] = df['Phone'].apply(
        lambda s: re.sub(r'[^0-9+]', '', s.removeprefix('Phone: ')))
    df['Website'] = df['Website'].str.rstrip('/')
    df['Rating'] = pd.to_numeric(df['Rating'], errors='coerce')

    df = df[df['Name'] != '']
    df = df.drop_duplicates(subset=['Name', 'Address']).reset_index(drop=True)
    return df[COLUMNS]


def export_csv(df: pd.DataFrame, path: str = DEFAULT_OUTPUT) -> None:
    df.to_csv(path, index=False, encoding='utf-8-sig')
    logger.info('Exported %d leads to %s', len(df), path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Scrape business leads from Google Maps.')
    parser.add_argument('query', help="Search query, e.g. 'Coffee Shops in Jakarta'")
    parser.add_argument('--max-results', type=int, default=50, help='Max listings to scrape')
    parser.add_argument('--output', default=DEFAULT_OUTPUT, help='Output CSV path')
    parser.add_argument('--headful', action='store_true', help='Show the browser window')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        leads = scrape_leads(args.query, args.max_results, headless=not args.headful)
    except KeyboardInterrupt:
        logger.warning('Interrupted by user.')
        return 130
    except Exception:
        logger.exception('Scrape failed.')
        return 1

    if not leads:
        logger.warning('No leads found for query: %s', args.query)
        return 1

    export_csv(clean_leads(leads), args.output)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
