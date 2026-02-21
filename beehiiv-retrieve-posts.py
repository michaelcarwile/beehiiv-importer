#!/usr/bin/env python3
"""Fetch posts from a Beehiiv newsletter site and save as Markdown.

Usage: ./beehiiv-retrieve-posts.py <site-url> [--output <path>] [--delay <seconds>] [--split] [--images]

Examples:
    ./beehiiv-retrieve-posts.py https://example.beehiiv.com
    ./beehiiv-retrieve-posts.py https://example.beehiiv.com --split --images
    ./beehiiv-retrieve-posts.py https://example.beehiiv.com -o export --split
"""

import os
import subprocess
import sys

_DEPS = ["requests", "beautifulsoup4", "html2text", "pyyaml"]
_DIR = os.path.dirname(os.path.abspath(__file__))
_VENV = os.path.join(_DIR, ".venv")
_VENV_PYTHON = os.path.join(_VENV, "bin", "python3")

# Bootstrap: create venv and install deps on first run, then re-exec
if os.path.realpath(sys.executable) != os.path.realpath(_VENV_PYTHON):
    if not os.path.exists(_VENV_PYTHON):
        print("First run — setting up environment...")
        import venv
        venv.create(_VENV, with_pip=True)
        subprocess.check_call([_VENV_PYTHON, "-m", "pip", "install", "-q"] + _DEPS)
        print("Done.\n")
    else:
        subprocess.check_call(
            [_VENV_PYTHON, "-m", "pip", "install", "-q"] + _DEPS,
            stdout=subprocess.DEVNULL,
        )
    os.execv(_VENV_PYTHON, [_VENV_PYTHON] + sys.argv)

import argparse
import json
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlparse, unquote

import html2text
import requests
import yaml
from bs4 import BeautifulSoup

converter = html2text.HTML2Text()
converter.body_width = 0
converter.ignore_images = False
converter.ignore_links = False

session = requests.Session()
session.headers["User-Agent"] = "Beehiiv-Importer/1.0"


# ---------------------------------------------------------------------------
# Step 1: Probe site — fetch sitemap and confirm Beehiiv
# ---------------------------------------------------------------------------

def fetch_sitemap(base_url):
    """Fetch /sitemap.xml and return the raw XML text, or exit on failure."""
    url = f"{base_url}/sitemap.xml"
    try:
        resp = session.get(url, timeout=30)
    except requests.exceptions.RequestException as e:
        print(f"Error: Could not connect to {base_url} — {e}", file=sys.stderr)
        sys.exit(1)

    if resp.status_code != 200:
        print(f"Error: {url} returned HTTP {resp.status_code}.", file=sys.stderr)
        sys.exit(1)

    content_type = resp.headers.get("content-type", "")
    if "xml" not in content_type and not resp.text.strip().startswith("<?xml"):
        print(f"Error: {url} does not appear to be XML.", file=sys.stderr)
        sys.exit(1)

    return resp.text


def parse_sitemap_urls(xml_text):
    """Parse a sitemap XML and return a list of (url, lastmod|None) tuples."""
    # Strip namespace for easier parsing
    xml_text = re.sub(r'\s+xmlns="[^"]+"', "", xml_text, count=1)
    root = ET.fromstring(xml_text)

    urls = []
    for url_elem in root.iter("url"):
        loc = url_elem.findtext("loc")
        lastmod = url_elem.findtext("lastmod")
        if loc:
            urls.append((loc.strip(), lastmod.strip() if lastmod else None))
    return urls


# ---------------------------------------------------------------------------
# Step 2: Discover posts — filter for /p/{slug} URLs
# ---------------------------------------------------------------------------

def discover_post_urls(sitemap_urls):
    """Filter sitemap URLs to only post URLs (matching /p/{slug} pattern).

    Returns list of (url, lastmod) sorted by lastmod (oldest first), then URL.
    """
    posts = []
    for url, lastmod in sitemap_urls:
        # Match /p/something but not /p/something/something (avoid sub-pages)
        if re.search(r"/p/[^/]+/?$", url):
            posts.append((url, lastmod))

    # Sort by lastmod (oldest first), URLs without dates go to the end
    posts.sort(key=lambda x: (x[1] or "9999", x[0]))
    return posts


# ---------------------------------------------------------------------------
# Step 3: Fetch and extract each post
# ---------------------------------------------------------------------------

def extract_json_ld(soup):
    """Extract the first JSON-LD script block as a dict, or return {}."""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string)
            # Handle @graph arrays — find the Article/NewsArticle/BlogPosting
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("@type") in (
                        "Article", "NewsArticle", "BlogPosting"
                    ):
                        return item
                return data[0] if data else {}
            return data
        except (json.JSONDecodeError, TypeError):
            continue
    return {}


def extract_remix_context(soup):
    """Extract the __remixContext data from a <script> tag, or return {}."""
    for script in soup.find_all("script"):
        if script.string and "window.__remixContext" in script.string:
            # Extract the JSON object assigned to window.__remixContext
            match = re.search(
                r"window\.__remixContext\s*=\s*(\{.+\})\s*;?\s*$",
                script.string,
                re.DOTALL,
            )
            if match:
                try:
                    return json.loads(match.group(1))
                except json.JSONDecodeError:
                    pass
    return {}


def extract_authors(remix_ctx):
    """Extract author names from __remixContext loader data."""
    authors = []
    try:
        loader_data = remix_ctx.get("state", {}).get("loaderData", {})
        # Try common route keys for post data
        for key in loader_data:
            if "p/$slug" in key or "p/" in key:
                post_data = loader_data[key]
                if isinstance(post_data, dict):
                    post_obj = post_data.get("post", post_data)
                    for author in post_obj.get("authors", []):
                        name = author.get("name") or author.get("display_name")
                        if name:
                            authors.append(name)
                break
    except (AttributeError, TypeError):
        pass
    return authors


def extract_tags(remix_ctx):
    """Extract tag names from __remixContext loader data."""
    tags = []
    try:
        loader_data = remix_ctx.get("state", {}).get("loaderData", {})
        for key in loader_data:
            if "p/$slug" in key or "p/" in key:
                post_data = loader_data[key]
                if isinstance(post_data, dict):
                    post_obj = post_data.get("post", post_data)
                    for tag in post_obj.get("content_tags", []):
                        name = tag.get("name")
                        if name:
                            tags.append(name)
                break
    except (AttributeError, TypeError):
        pass
    return tags


def get_meta(soup, property_name=None, name=None):
    """Get content of a meta tag by property or name attribute."""
    if property_name:
        tag = soup.find("meta", property=property_name)
    elif name:
        tag = soup.find("meta", attrs={"name": name})
    else:
        return None
    return tag.get("content") if tag else None


def clean_content(soup_content):
    """Remove beehiiv boilerplate from the #content-blocks element.

    Strips tracking pixels, subscribe CTAs, share blocks, referral blocks,
    unsubscribe links, read-online links, hidden elements, and empty paragraphs.
    """
    if soup_content is None:
        return ""

    # Remove hidden elements (display:none)
    for el in soup_content.find_all(style=re.compile(r"display\s*:\s*none", re.I)):
        el.decompose()

    # Remove tracking pixels — tiny images (1x1, 0x0) and known tracking patterns
    for img in soup_content.find_all("img"):
        src = img.get("src", "")
        width = img.get("width", "")
        height = img.get("height", "")
        # 1x1 or 0-width/height tracking pixels
        if (width in ("0", "1") or height in ("0", "1")):
            img.decompose()
            continue
        # Known tracking domains
        if any(t in src for t in [
            "open.substack.com", "pixel", "track", "beacon",
            "email-analytics", "/o/", "sp.beehiiv.com",
        ]):
            img.decompose()

    # Remove referral program blocks (contain {{rp_ template markers)
    for el in soup_content.find_all(string=re.compile(r"\{\{rp_")):
        # Walk up to find the containing block
        parent = el.parent
        for _ in range(5):
            if parent and parent.parent and parent.parent != soup_content:
                parent = parent.parent
            else:
                break
        if parent and parent != soup_content:
            parent.decompose()

    # Patterns to identify boilerplate blocks
    boilerplate_patterns = [
        # Subscribe CTAs
        re.compile(r"subscribe", re.I),
        # Unsubscribe links
        re.compile(r"unsubscribe", re.I),
        # Share blocks
        re.compile(r"share\s+(this|post|article)", re.I),
        # Read online
        re.compile(r"read\s+(this\s+)?(online|in\s+browser|in\s+your\s+browser)", re.I),
        # "Forwarded this email" / "Was this forwarded"
        re.compile(r"forward(ed)?\s+this", re.I),
    ]

    # Remove links/buttons matching boilerplate patterns
    for a in soup_content.find_all("a"):
        text = a.get_text(strip=True)
        href = a.get("href", "")
        if any(p.search(text) for p in boilerplate_patterns):
            # Check if this is in a larger block — remove the block
            parent = a.parent
            # Walk up to a table cell, div, or section that contains only boilerplate
            for _ in range(3):
                if parent and parent.parent and parent.parent != soup_content:
                    siblings_text = parent.get_text(strip=True)
                    if len(siblings_text) < 200:
                        parent = parent.parent
                    else:
                        break
                else:
                    break
            if parent and parent != soup_content and len(parent.get_text(strip=True)) < 300:
                parent.decompose()
            else:
                a.decompose()

    # Remove beehiiv-specific boilerplate containers
    # Common class patterns used by beehiiv for boilerplate
    boilerplate_classes = [
        "share-links", "social-links", "subscribe-cta", "referral",
        "footer", "email-footer",
    ]
    for cls in boilerplate_classes:
        for el in soup_content.find_all(class_=re.compile(cls, re.I)):
            el.decompose()

    # Remove common newsletter footer/CTA sections
    # These are recurring blocks at the end like "Whenever you're ready, there are N ways..."
    footer_patterns = [
        re.compile(r"whenever you.re ready", re.I),
        re.compile(r"ways? (we|I) can help you", re.I),
        re.compile(r"what did you think of (today|this)", re.I),
        re.compile(r"how did you like (today|this)", re.I),
        re.compile(r"join \d[\d,]* (readers|subscribers|others)", re.I),
    ]
    for pattern in footer_patterns:
        for el in soup_content.find_all(string=pattern):
            # Walk up until we reach a node whose parent has multiple
            # element children (i.e. we're in the content flow, not a
            # sole-child wrapper)
            node = el.parent
            for _ in range(10):
                if not node or not node.parent or node.parent == soup_content:
                    break
                parent_el_children = [
                    c for c in node.parent.children
                    if hasattr(c, "name") and c.name
                ]
                if len(parent_el_children) > 1:
                    break
                node = node.parent
            if node and node != soup_content and node.parent and node.parent != soup_content:
                # Remove this element and all subsequent siblings
                to_remove = []
                sibling = node
                while sibling:
                    to_remove.append(sibling)
                    sibling = sibling.next_sibling
                for s in to_remove:
                    if hasattr(s, "decompose"):
                        s.decompose()
                    elif hasattr(s, "extract"):
                        s.extract()

    # Remove footer/CTA images (common beehiiv newsletter pattern)
    for img in soup_content.find_all("img"):
        src = img.get("src", "")
        # Images with CTA/footer in filename are boilerplate
        if re.search(r"(cta|footer|divider).*\.(png|jpg|gif)", src, re.I):
            # Remove the image and its container if it's a simple wrapper
            parent = img.parent
            if parent and parent != soup_content:
                text = parent.get_text(strip=True)
                if not text:
                    parent.decompose()
                else:
                    img.decompose()
            else:
                img.decompose()

    # Remove empty paragraphs and nbsp-only cells
    for p in soup_content.find_all(["p", "td"]):
        text = p.get_text(strip=True)
        if not text or text in ("\xa0", "&nbsp;"):
            # Only remove if it has no meaningful children (like images)
            if not p.find(["img", "video", "iframe"]):
                p.decompose()

    return str(soup_content)


def fetch_post(url, delay):
    """Fetch a single post URL and extract metadata + content.

    Returns a dict with keys: title, date, date_modified, description,
    featured_image, url, slug, authors, tags, content_html
    """
    resp = session.get(url, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    json_ld = extract_json_ld(soup)
    remix_ctx = extract_remix_context(soup)

    # Slug from URL
    path = urlparse(url).path
    slug_match = re.search(r"/p/([^/]+)/?$", path)
    slug = slug_match.group(1) if slug_match else path.rsplit("/", 1)[-1]

    # Title
    title = (
        json_ld.get("headline")
        or get_meta(soup, property_name="og:title")
        or (soup.title.string if soup.title else slug)
    )

    # Date
    date = json_ld.get("datePublished")
    date_modified = json_ld.get("dateModified")

    # Description
    description = (
        json_ld.get("description")
        or get_meta(soup, property_name="og:description")
        or get_meta(soup, name="description")
    )

    # Featured image
    image_data = json_ld.get("image")
    if isinstance(image_data, dict):
        featured_image = image_data.get("url")
    elif isinstance(image_data, str):
        featured_image = image_data
    else:
        featured_image = get_meta(soup, property_name="og:image")

    # Authors
    authors = extract_authors(remix_ctx)
    if not authors:
        # Fallback to publisher name from JSON-LD
        publisher = json_ld.get("publisher", {})
        pub_name = publisher.get("name") if isinstance(publisher, dict) else None
        if not pub_name:
            pub_name = get_meta(soup, property_name="og:site_name")
        if pub_name:
            authors = [pub_name]

    # Tags
    tags = extract_tags(remix_ctx)

    # Content
    content_div = soup.find("div", id="content-blocks")
    content_html = clean_content(content_div) if content_div else ""

    return {
        "title": title,
        "date": date,
        "date_modified": date_modified,
        "description": description,
        "featured_image": featured_image,
        "url": url,
        "slug": slug,
        "authors": authors,
        "tags": tags,
        "content_html": content_html,
    }


# ---------------------------------------------------------------------------
# Step 4: Image handling
# ---------------------------------------------------------------------------

def collect_image_urls(post_data):
    """Collect all image URLs from a post (featured + inline)."""
    urls = []
    if post_data.get("featured_image"):
        urls.append(post_data["featured_image"])

    # Parse inline images from content HTML
    if post_data.get("content_html"):
        soup = BeautifulSoup(post_data["content_html"], "html.parser")
        for img in soup.find_all("img"):
            src = img.get("src")
            if src and src.startswith("http"):
                urls.append(src)

    return urls


def download_images(image_urls, images_dir, delay):
    """Download images to disk. Returns {original_url: local_filename}.

    Skips existing files. Handles filename collisions.
    """
    os.makedirs(images_dir, exist_ok=True)
    result = {}
    used_filenames = set()

    for url in image_urls:
        if url in result:
            continue

        # Derive filename from URL path
        parsed_path = unquote(urlparse(url).path)
        filename = parsed_path.rsplit("/", 1)[-1]
        if not filename or filename == "/":
            filename = "image"

        # Ensure we have an extension
        if "." not in filename:
            filename += ".jpg"

        # Handle collisions
        if filename in used_filenames:
            name, ext = os.path.splitext(filename)
            counter = 1
            while f"{name}-{counter}{ext}" in used_filenames:
                counter += 1
            filename = f"{name}-{counter}{ext}"
        used_filenames.add(filename)

        filepath = os.path.join(images_dir, filename)
        if os.path.exists(filepath):
            print(f"  Skipping (exists): {filename}")
            result[url] = filename
            continue

        try:
            resp = session.get(url, stream=True, timeout=60)
            resp.raise_for_status()
            with open(filepath, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            print(f"  Downloaded: {filename}")
            result[url] = filename
        except requests.exceptions.RequestException as e:
            print(f"  Warning: failed to download {url} — {e}", file=sys.stderr)
        time.sleep(delay)

    return result


def rewrite_content_images(content_html, image_map, images_prefix):
    """Rewrite inline image src attributes to local paths."""
    if not content_html or not image_map:
        return content_html
    soup = BeautifulSoup(content_html, "html.parser")
    for img in soup.find_all("img"):
        src = img.get("src")
        if src and src in image_map:
            img["src"] = f"{images_prefix}/{image_map[src]}"
    return str(soup)


# ---------------------------------------------------------------------------
# Step 5: Output — Markdown with YAML frontmatter
# ---------------------------------------------------------------------------

def post_to_markdown(post_data, image_map=None, images_prefix="images"):
    """Convert extracted post data to Markdown with YAML frontmatter."""
    frontmatter = {"title": post_data["title"]}

    if post_data.get("date"):
        # Normalize date to YYYY-MM-DD
        date_str = post_data["date"][:10]
        frontmatter["date"] = date_str

    frontmatter["url"] = post_data["url"]
    frontmatter["slug"] = post_data["slug"]

    if post_data.get("description"):
        frontmatter["description"] = post_data["description"]

    if post_data.get("featured_image"):
        fi = post_data["featured_image"]
        if image_map and fi in image_map:
            fi = f"{images_prefix}/{image_map[fi]}"
        frontmatter["featured_image"] = fi

    if post_data.get("authors"):
        frontmatter["authors"] = post_data["authors"]

    if post_data.get("tags"):
        frontmatter["tags"] = post_data["tags"]

    # Convert content HTML to Markdown
    content_html = post_data.get("content_html", "")
    if image_map:
        content_html = rewrite_content_images(content_html, image_map, images_prefix)

    content_md = converter.handle(content_html).strip() if content_html else ""

    fm_str = yaml.dump(
        frontmatter, sort_keys=False, allow_unicode=True, default_flow_style=False
    ).rstrip()
    return f"---\n{fm_str}\n---\n\n{content_md}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Export Beehiiv newsletter posts to Markdown."
    )
    parser.add_argument(
        "url",
        help="Beehiiv site URL (e.g. https://example.beehiiv.com or https://yourcustomdomain.com)",
    )
    parser.add_argument(
        "--output", "-o", default=None,
        help="Output filename or directory (default: <domain>-articles.md)",
    )
    parser.add_argument(
        "--delay", type=float, default=2,
        help="Seconds between requests (default: 2)",
    )
    parser.add_argument(
        "--split", action="store_true",
        help="Write one Markdown file per post into an output directory",
    )
    parser.add_argument(
        "--images", action="store_true",
        help="Download images locally (featured + inline)",
    )
    args = parser.parse_args()

    base_url = args.url.rstrip("/")
    domain = urlparse(base_url).hostname.replace("www.", "")

    print("Note: This tool is for exporting content you own or have permission to use.")
    print("      Respect copyright and the site's terms of service.\n")

    # --- Step 1: Probe site ---
    print(f"Fetching sitemap from {base_url}...")
    xml_text = fetch_sitemap(base_url)
    sitemap_urls = parse_sitemap_urls(xml_text)

    if not sitemap_urls:
        print("Error: Sitemap is empty or could not be parsed.", file=sys.stderr)
        sys.exit(1)

    print(f"  Found {len(sitemap_urls)} URLs in sitemap.")

    # --- Step 2: Discover posts ---
    post_urls = discover_post_urls(sitemap_urls)

    if not post_urls:
        print("Error: No post URLs (matching /p/{slug}) found in sitemap.", file=sys.stderr)
        sys.exit(1)

    print(f"  Found {len(post_urls)} posts.\n")

    # --- Determine output paths ---
    if args.split:
        output_dir = args.output or f"{domain}-articles"
    else:
        output_file = args.output or f"{domain}-articles.md"

    # --- Step 3: Fetch each post ---
    print(f"Fetching {len(post_urls)} posts...")
    posts = []
    for i, (url, _lastmod) in enumerate(post_urls, 1):
        print(f"  [{i}/{len(post_urls)}] {url}")
        try:
            post_data = fetch_post(url, args.delay)
            posts.append(post_data)
        except requests.exceptions.RequestException as e:
            print(f"    Warning: failed to fetch — {e}", file=sys.stderr)
        except Exception as e:
            print(f"    Warning: error processing — {e}", file=sys.stderr)

        if i < len(post_urls):
            time.sleep(args.delay)

    if not posts:
        print("No posts could be fetched.", file=sys.stderr)
        sys.exit(1)

    print(f"\n  Successfully fetched {len(posts)} posts.\n")

    # Detect publication name from first post
    pub_name = None
    for p in posts:
        if p.get("authors"):
            pub_name = p["authors"][0]
            break
    if pub_name:
        print(f"Publication: {pub_name}")

    # --- Step 4: Image handling ---
    image_map = {}
    images_prefix = "images"

    if args.images:
        all_image_urls = []
        for post_data in posts:
            all_image_urls.extend(collect_image_urls(post_data))
        # Deduplicate while preserving order
        seen = set()
        unique_urls = []
        for u in all_image_urls:
            if u not in seen:
                seen.add(u)
                unique_urls.append(u)

        if unique_urls:
            if args.split:
                images_dir = os.path.join(output_dir, "images")
            else:
                images_dir = f"{domain}-images"
                images_prefix = f"{domain}-images"

            print(f"\nDownloading {len(unique_urls)} images to {images_dir}/...")
            image_map = download_images(unique_urls, images_dir, args.delay)
            print(f"  {len(image_map)} images ready.\n")

    # --- Step 5: Write output ---
    if args.split:
        os.makedirs(output_dir, exist_ok=True)
        for post_data in posts:
            md = post_to_markdown(post_data, image_map=image_map, images_prefix=images_prefix)
            date_str = (post_data.get("date") or "undated")[:10]
            slug = post_data["slug"]
            filename = os.path.join(output_dir, f"{date_str}-{slug}.md")
            with open(filename, "w", encoding="utf-8") as f:
                f.write(md)
                f.write("\n")
        print(f"Written {len(posts)} files to {output_dir}/")
    else:
        # Single file — all posts sorted by date
        sorted_posts = sorted(posts, key=lambda p: p.get("date") or "9999")
        sections = [
            post_to_markdown(p, image_map=image_map, images_prefix=images_prefix)
            for p in sorted_posts
        ]
        with open(output_file, "w", encoding="utf-8") as f:
            f.write("\n\n".join(sections))
            f.write("\n")
        print(f"Written to {output_file}")


if __name__ == "__main__":
    main()
