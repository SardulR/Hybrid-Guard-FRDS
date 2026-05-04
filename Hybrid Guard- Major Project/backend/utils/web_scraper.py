"""
Universal Product Review Scraper
Supports: Flipkart, Amazon (IN/COM), Myntra, Meesho, and any generic site.

For unknown sites it applies heuristics to extract review-like text blocks
and star ratings automatically — no hardcoded selectors needed.
"""

import os
import re
import time
import json
import pandas as pd
from datetime import datetime
from urllib.parse import urlparse, parse_qs, urlencode

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
)
from bs4 import BeautifulSoup
from webdriver_manager.chrome import ChromeDriverManager

SCRAPED_FILES_FOLDER = "scraped_files"
os.makedirs(SCRAPED_FILES_FOLDER, exist_ok=True)

DEBUG = True


# ─────────────────────────────────────────────────────────────────────────────
# Driver
# ─────────────────────────────────────────────────────────────────────────────

def make_driver():
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()), options=opts
    )
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"},
    )
    return driver


def get_soup(driver):
    return BeautifulSoup(driver.page_source, "html.parser")


def safe_text(el):
    try:
        return el.text.strip() if hasattr(el, "text") else el.get_text(strip=True)
    except StaleElementReferenceException:
        return ""


def debug_dump(driver, tag):
    driver.save_screenshot(os.path.join(SCRAPED_FILES_FOLDER, f"{tag}.png"))
    with open(
        os.path.join(SCRAPED_FILES_FOLDER, f"{tag}.html"), "w", encoding="utf-8"
    ) as f:
        f.write(driver.page_source)
    print(f"  [debug] snapshot → scraped_files/{tag}.png")


# ─────────────────────────────────────────────────────────────────────────────
# Site detection
# ─────────────────────────────────────────────────────────────────────────────

def detect_site(url):
    host = urlparse(url).netloc.lower()
    if "flipkart" in host: return "flipkart"
    if "amazon"   in host: return "amazon"
    if "myntra"   in host: return "myntra"
    if "meesho"   in host: return "meesho"
    return "generic"


# ─────────────────────────────────────────────────────────────────────────────
# Flipkart
# ─────────────────────────────────────────────────────────────────────────────

def _flipkart_reviews_url(url, page=1):
    """
    Builds the correct Flipkart reviews URL.

    Handles:
      - marketplace param (NOT the wrong 'mid')
      - bare &lid with no value (keep_blank_values=True, skip if empty)
      - page number injection
      - preserves slug in path so Flipkart doesn't 500
    """
    parsed = urlparse(url)
    parts  = parsed.path.strip("/").split("/")
    params = parse_qs(parsed.query, keep_blank_values=True)

    def first(k): return params.get(k, [""])[0]

    slug        = parts[0]    # e.g. "bullstorm-bs-ultrapood-bluetooth-gaming"
    item_id     = parts[-1]   # e.g. "itm58a679ce7e4ca"
    pid         = first("pid") or item_id
    lid         = first("lid")          # empty string if bare &lid
    marketplace = first("marketplace")  # e.g. "FLIPKART"

    q = {"pid": pid, "page": page}

    # Only include lid if it actually has a value
    if lid and lid.strip():
        q["lid"] = lid

    # Use marketplace (correct param), never mid
    if marketplace and marketplace.strip():
        q["marketplace"] = marketplace

    return f"https://www.flipkart.com/{slug}/product-reviews/{item_id}?{urlencode(q)}"


def _flipkart_parse(soup):
    """
    Extracts reviews from Flipkart's React-rendered HTML.

    Selectors confirmed from live HTML:
      Card root : div containing both classes r-z2wwpe and r-eqz5dr
      Rating    : div.css-146c3p1 whose text matches [1-5](.\d)?
      Title     : div.css-146c3p1 with margin-left:8px + inter_regular in style
      Body      : span.css-1jxf684
    """
    reviews, ratings, titles = [], [], []

    cards = soup.find_all(
        "div",
        class_=lambda c: c and "r-z2wwpe" in c and "r-eqz5dr" in c
    )

    for card in cards:
        # Rating
        rating = 0.0
        for div in card.find_all("div", class_="css-146c3p1"):
            t = div.get_text(strip=True)
            if re.fullmatch(r"[1-5](\.\d)?", t):
                rating = float(t)
                break

        # Title
        title = ""
        for div in card.find_all("div", class_="css-146c3p1"):
            style = div.get("style", "")
            t = div.get_text(strip=True)
            if (
                "margin-left: 8px" in style
                and "inter_regular" in style
                and len(t) > 2
                and t != "•"
                and not re.fullmatch(r"[1-5](\.\d)?", t)
            ):
                title = t
                break

        # Body
        span = card.find("span", class_="css-1jxf684")
        body = span.get_text(strip=True) if span else ""

        if body or title:
            reviews.append(body or title)
            ratings.append(rating)
            titles.append(title)

    return reviews, ratings, titles


def scrape_flipkart(driver, url, max_pages):
    parsed    = urlparse(url)
    parts     = parsed.path.strip("/").split("/")
    params    = parse_qs(parsed.query, keep_blank_values=True)

    prod_name = parts[0].replace("-", " ").title()
    prod_id   = params.get("pid", [parts[-1]])[0]

    all_reviews, all_ratings, all_titles = [], [], []

    for page_num in range(1, max_pages + 1):
        page_url = _flipkart_reviews_url(url, page=page_num)
        print(f"  [Flipkart] Page {page_num} → {page_url}")
        driver.get(page_url)

        # Wait for React to render review cards OR error page
        try:
            WebDriverWait(driver, 15).until(
                lambda d: (
                    len(d.find_elements(By.CSS_SELECTOR, "div[class*='r-z2wwpe']")) > 0
                    or "moved or deleted" in d.page_source
                )
            )
        except TimeoutException:
            print("  ✗ Timed out waiting for reviews.")
            if DEBUG: debug_dump(driver, f"flipkart_p{page_num}_timeout")
            break

        if "moved or deleted" in driver.page_source:
            print("  ✗ Error page — stopping.")
            if DEBUG: debug_dump(driver, f"flipkart_p{page_num}_error")
            break

        # Scroll to trigger lazy-loaded content
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(1.5)

        soup = get_soup(driver)
        reviews, ratings, titles = _flipkart_parse(soup)
        print(f"     Found {len(reviews)} reviews")

        if not reviews:
            if DEBUG: debug_dump(driver, f"flipkart_p{page_num}_empty")
            break

        all_reviews.extend(reviews)
        all_ratings.extend(ratings)
        all_titles.extend(titles)

    return prod_id, prod_name, all_reviews, all_ratings, all_titles


# ─────────────────────────────────────────────────────────────────────────────
# Amazon
# ─────────────────────────────────────────────────────────────────────────────

def _amazon_reviews_url(url, page=1):
    parsed = urlparse(url)
    parts  = parsed.path.strip("/").split("/")

    asin = ""
    for i, p in enumerate(parts):
        if p in ("dp", "product-reviews") and i + 1 < len(parts):
            asin = parts[i + 1]
            break
    if not asin:
        asin = parts[-1]

    base = f"{parsed.scheme}://{parsed.netloc}"
    return f"{base}/product-reviews/{asin}?pageNumber={page}&reviewerType=all_reviews"


def _amazon_parse(soup):
    reviews, ratings, titles = [], [], []

    cards = soup.find_all("div", {"data-hook": "review"})

    for card in cards:
        # Rating
        rating = 0.0
        star_el = card.find("span", {"data-hook": re.compile("review-star-rating")})
        if star_el:
            m = re.search(r"([1-5])(\.\d)?", star_el.get_text())
            if m: rating = float(m.group(0))

        # Title
        title_el = (
            card.find("a", {"data-hook": "review-title"}) or
            card.find("span", {"data-hook": "review-title"})
        )
        title = title_el.get_text(strip=True) if title_el else ""
        title = re.sub(r"^\d(\.\d)? out of \d stars?\s*", "", title).strip()

        # Body
        body_el = card.find("span", {"data-hook": "review-body"})
        body = body_el.get_text(strip=True) if body_el else ""

        if body or title:
            reviews.append(body or title)
            ratings.append(rating)
            titles.append(title)

    return reviews, ratings, titles


def scrape_amazon(driver, url, max_pages):
    parsed    = urlparse(url)
    parts     = parsed.path.strip("/").split("/")
    prod_name = "Unknown Product"
    prod_id   = ""

    for i, p in enumerate(parts):
        if p in ("dp", "product-reviews") and i + 1 < len(parts):
            prod_id = parts[i + 1]
            break

    all_reviews, all_ratings, all_titles = [], [], []

    for page_num in range(1, max_pages + 1):
        page_url = _amazon_reviews_url(url, page=page_num)
        print(f"  [Amazon] Page {page_num} → {page_url}")
        driver.get(page_url)
        time.sleep(4)

        if page_num == 1:
            try:
                title_el = driver.find_element(By.CSS_SELECTOR, "a.product-title")
                prod_name = title_el.text.strip()
            except Exception:
                prod_name = driver.title.split(":")[0].strip() or "Amazon Product"

        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(1.5)

        soup = get_soup(driver)
        reviews, ratings, titles = _amazon_parse(soup)
        print(f"     Found {len(reviews)} reviews")

        if not reviews:
            if DEBUG: debug_dump(driver, f"amazon_p{page_num}_empty")
            break

        all_reviews.extend(reviews)
        all_ratings.extend(ratings)
        all_titles.extend(titles)

    return prod_id, prod_name, all_reviews, all_ratings, all_titles


# ─────────────────────────────────────────────────────────────────────────────
# Myntra
# ─────────────────────────────────────────────────────────────────────────────

def scrape_myntra(driver, url, max_pages):
    parsed    = urlparse(url)
    parts     = parsed.path.strip("/").split("/")
    prod_id   = parts[-1] if parts else "unknown"
    prod_name = parts[0].replace("-", " ").title() if parts else "Myntra Product"

    print(f"  [Myntra] Loading → {url}")
    driver.get(url)
    time.sleep(5)
    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(2)

    soup = get_soup(driver)
    reviews, ratings, titles = [], [], []

    # Strategy 1: JSON in <script> tags
    for script in soup.find_all("script"):
        text = script.string or ""
        if "ratings" in text and "review" in text.lower():
            try:
                m = re.search(r'\{.*"ratings".*\}', text, re.DOTALL)
                if m:
                    data = json.loads(m.group(0))
                    for r in data.get("ratings", []):
                        body = r.get("reviewText") or r.get("title") or ""
                        rat  = float(r.get("ratingValue") or r.get("rating") or 0)
                        if body:
                            reviews.append(body)
                            ratings.append(rat)
                            titles.append(r.get("title", ""))
                    if reviews:
                        break
            except Exception:
                pass

    # Strategy 2: DOM selectors
    if not reviews:
        for card in soup.find_all(
            "div",
            class_=re.compile(r"user-review-main|review-dt-left|detailed-reviews")
        ):
            body_el   = card.find(class_=re.compile(r"user-review-reviewTextWrapper|review-description"))
            rating_el = card.find(class_=re.compile(r"user-review-starRating|index-overallRating"))
            title_el  = card.find(class_=re.compile(r"user-review-title"))

            body   = body_el.get_text(strip=True)  if body_el   else ""
            title  = title_el.get_text(strip=True) if title_el  else ""
            rating = 0.0
            if rating_el:
                m = re.search(r"[1-5](\.\d)?", rating_el.get_text())
                if m: rating = float(m.group(0))

            if body or title:
                reviews.append(body or title)
                ratings.append(rating)
                titles.append(title)

    if not reviews and DEBUG:
        debug_dump(driver, "myntra_empty")

    return prod_id, prod_name, reviews, ratings, titles


# ─────────────────────────────────────────────────────────────────────────────
# Meesho
# ─────────────────────────────────────────────────────────────────────────────

def scrape_meesho(driver, url, max_pages):
    parsed    = urlparse(url)
    parts     = parsed.path.strip("/").split("/")
    prod_id   = parts[-1] if parts else "unknown"
    prod_name = parts[0].replace("-", " ").title() if parts else "Meesho Product"

    print(f"  [Meesho] Loading → {url}")
    driver.get(url)
    time.sleep(5)
    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(2)

    soup = get_soup(driver)
    reviews, ratings, titles = [], [], []

    for card in soup.find_all(
        "div",
        class_=re.compile(r"ReviewCard|review-card|ProductReview")
    ):
        body_el   = card.find(class_=re.compile(r"Text|review-text|body"))
        rating_el = card.find(class_=re.compile(r"rating|star|Rating"))
        title_el  = card.find(class_=re.compile(r"title|Title|heading"))

        body   = body_el.get_text(strip=True)  if body_el   else ""
        title  = title_el.get_text(strip=True) if title_el  else ""
        rating = 0.0
        if rating_el:
            m = re.search(r"[1-5](\.\d)?", rating_el.get_text())
            if m: rating = float(m.group(0))

        if body or title:
            reviews.append(body or title)
            ratings.append(rating)
            titles.append(title)

    if not reviews and DEBUG:
        debug_dump(driver, "meesho_empty")

    return prod_id, prod_name, reviews, ratings, titles


# ─────────────────────────────────────────────────────────────────────────────
# Generic heuristic scraper
# ─────────────────────────────────────────────────────────────────────────────

_REVIEW_KEYWORDS = re.compile(
    r"review|testimon|comment|feedback|rating|opinion|critic",
    re.IGNORECASE
)
_RATING_PATTERN = re.compile(r"\b([1-5])(\.\d{1,2})?\b")


def _score_element(tag, text):
    score = 0
    classes_id = " ".join(tag.get("class", [])) + " " + (tag.get("id") or "")

    if _REVIEW_KEYWORDS.search(classes_id):  score += 4
    if 40 < len(text) < 2000:                score += 3
    if len(text.split()) > 8:                score += 2
    if tag.name in ("p", "span"):            score += 1
    if tag.name in ("nav", "header", "footer", "script", "style"): score -= 10
    return score


def _extract_generic(soup):
    for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
        tag.decompose()

    candidates = []
    for tag in soup.find_all(["p", "div", "span", "li", "article", "section"]):
        if tag.find(["p", "div", "article"]):
            continue
        text = tag.get_text(strip=True)
        score = _score_element(tag, text)
        if score >= 4:
            candidates.append((score, text, tag))

    candidates.sort(key=lambda x: -x[0])

    seen  = []
    final = []
    for _, text, tag in candidates[:100]:
        if any(text in s or s in text for s in seen):
            continue
        seen.append(text)

        rating = 0.0
        for ancestor in [tag] + list(tag.parents)[:4]:
            block_text = ancestor.get_text()
            m = _RATING_PATTERN.search(block_text)
            if m:
                candidate_r = float(m.group(0))
                if 1.0 <= candidate_r <= 5.0:
                    rating = candidate_r
                    break

        final.append((text, rating))

    return final


def scrape_generic(driver, url, max_pages):
    parsed    = urlparse(url)
    prod_name = parsed.netloc.replace("www.", "").split(".")[0].upper()
    prod_id   = parsed.path.strip("/").split("/")[-1] or "unknown"

    reviews, ratings, titles = [], [], []

    for page_num in range(1, max_pages + 1):
        if page_num == 1:
            page_url = url
        else:
            sep  = "&" if "?" in url else "?"
            base = re.sub(r"[?&]page=\d+", "", url)
            page_url = f"{base}{sep}page={page_num}"

        print(f"  [Generic] Page {page_num} → {page_url}")
        driver.get(page_url)
        time.sleep(4)
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(2)

        soup  = get_soup(driver)
        pairs = _extract_generic(soup)
        print(f"     Found {len(pairs)} review candidates")

        if not pairs:
            if DEBUG: debug_dump(driver, f"generic_p{page_num}_empty")
            break

        for text, rating in pairs:
            reviews.append(text)
            ratings.append(rating)
            titles.append("")

        next_exists = bool(
            soup.find("a", string=re.compile(r"next|›|→", re.I)) or
            soup.find("a", {"aria-label": re.compile(r"next", re.I)})
        )
        if not next_exists:
            break

    if not reviews and DEBUG:
        debug_dump(driver, "generic_empty")

    return prod_id, prod_name, reviews, ratings, titles


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def scrape_reviews(url, max_pages=5):
    """
    Universal review scraper.

    Detects the site automatically and applies the right strategy:
      • Flipkart  — reviews URL with pid/marketplace params + WebDriverWait
      • Amazon    — /product-reviews/<ASIN>?pageNumber=N, data-hook attributes
      • Myntra    — JSON in <script> tag + DOM fallback
      • Meesho    — DOM selectors
      • Any other — heuristic text extraction

    Returns:
      (prod_id, prod_name, csv_path, DataFrame)  on success
      (None,    None,      None,     error_str)  on failure
    """
    site   = detect_site(url)
    print(f"Site detected: {site.upper()}  |  URL: {url}")

    driver = make_driver()

    try:
        if site == "flipkart":
            prod_id, prod_name, reviews, ratings, titles = scrape_flipkart(driver, url, max_pages)
        elif site == "amazon":
            prod_id, prod_name, reviews, ratings, titles = scrape_amazon(driver, url, max_pages)
        elif site == "myntra":
            prod_id, prod_name, reviews, ratings, titles = scrape_myntra(driver, url, max_pages)
        elif site == "meesho":
            prod_id, prod_name, reviews, ratings, titles = scrape_meesho(driver, url, max_pages)
        else:
            prod_id, prod_name, reviews, ratings, titles = scrape_generic(driver, url, max_pages)

        if not reviews:
            return (
                None, None, None,
                f"No reviews found on {site} page.\n"
                "Check scraped_files/ for debug snapshots.\n"
                "For Flipkart: paste the 'All Reviews' URL (pid & marketplace params required).\n"
                "For Amazon: paste the product page URL (/dp/ASIN)."
            )

        df = pd.DataFrame({
            "prod_id":         [prod_id]   * len(reviews),
            "prod_name":       [prod_name] * len(reviews),
            "site":            [site]      * len(reviews),
            "review_title":    titles,
            "customer_review": reviews,
            "customer_rating": ratings,
        })
        df = df[df["customer_review"].str.strip() != ""].reset_index(drop=True)

        ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{prod_name.replace(' ', '_')}_{ts}.csv"
        path     = os.path.join(SCRAPED_FILES_FOLDER, filename)
        df.to_csv(path, index=False)
        print(f"\n✓ Saved {len(df)} reviews → {path}")
        return prod_id, prod_name, path, df

    except Exception:
        import traceback
        return None, None, None, traceback.format_exc()

    finally:
        driver.quit()