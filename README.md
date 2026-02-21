# Beehiiv Importer

> **Respect copyright.** This tool is intended for exporting content you own, have authored, or have explicit permission to use. Newsletter content — including text, images, and media — is protected by copyright. Do not use this tool to reproduce, redistribute, or repurpose another publication's content without authorization. You are solely responsible for ensuring your use complies with applicable copyright laws and the site's terms of service.

Export posts from any Beehiiv newsletter to clean Markdown. Works with both `*.beehiiv.com` subdomains and custom vanity domains. The script discovers posts via the site's sitemap (no API key needed), extracts content from server-rendered HTML, and strips newsletter boilerplate automatically.

## Quick Start

```bash
./beehiiv-retrieve-posts.py https://example.beehiiv.com
```

That's it. On first run, the script automatically creates a virtual environment and installs its dependencies.

### Options

```bash
# Custom output file
./beehiiv-retrieve-posts.py https://example.beehiiv.com -o my-posts.md

# One file per post (great for AI tools like NotebookLM)
./beehiiv-retrieve-posts.py https://example.beehiiv.com --split

# Download featured + inline images locally
./beehiiv-retrieve-posts.py https://example.beehiiv.com --split --images

# Adjust rate limiting (default: 2s between requests)
./beehiiv-retrieve-posts.py https://example.beehiiv.com --delay 1

# Custom domain
./beehiiv-retrieve-posts.py https://yourcustomdomain.com --split
```

By default, output is a single Markdown file with all posts sorted by date (oldest first). Use `--split` to write one file per post into a directory instead (default: `<domain>-articles/`). Each post uses YAML frontmatter for metadata:

```markdown
---
title: "Post Title"
date: 2024-01-15
url: https://example.com/p/post-slug
slug: post-slug
description: "Post subtitle"
featured_image: images/photo.jpg
authors:
  - Author Name
tags:
  - Tag Name
---

Post content here...
```

Authors are extracted from Beehiiv's Remix hydration data, with a fallback to the publication name. Tags are included when available. The `featured_image` field contains either the source URL (default) or a local path (with `--images`).

## How It Works

1. **Discover** — Fetches `/sitemap.xml` and filters for `/p/{slug}` post URLs
2. **Extract** — For each post, pulls metadata from JSON-LD and `<meta>` tags, authors from `__remixContext`, and content from `div#content-blocks`
3. **Clean** — Strips newsletter boilerplate: header images, read-time badges, sponsor blocks, tracking pixels, subscribe CTAs, referral blocks, footer sections
4. **Convert** — Transforms cleaned HTML to Markdown via `html2text`
5. **Write** — Outputs Markdown with YAML frontmatter (single file or split)

## Files

| File | Description |
|------|-------------|
| `beehiiv-retrieve-posts.py` | Main script — fetches and exports Beehiiv newsletter posts to Markdown |
