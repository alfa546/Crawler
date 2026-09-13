<div align="center">

<img src="static/logo.png" width="180" alt="Crawler Logo" />

# 🕷️ Crawler

**Enterprise-Grade, Self-Hosted SEO Crawler & Technical Auditing Platform**

An open-source alternative to Screaming Frog, Sitebulb & Ahrefs — unlimited URLs, zero subscription, 100% local.

[![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20macOS%20%7C%20Windows-blue)](#-installation)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white)](#-docker)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)
[![UI: Modern](https://img.shields.io/badge/UI-Modern_Redesign-success)](#)

[Features](#-key-features) · [Installation](#-installation) · [Usage](#-usage-guide) · [Configuration](#%EF%B8%8F-configuration) · [Contributing](#-contributing)

</div>

<br />

## 🎥 See It In Action

<div align="center">
  <img src="demo_v2.gif" width="100%" alt="Crawler Live Demo" />
</div>

---

## 📖 About The Project

**Crawler** is an enterprise-grade, self-hosted technical SEO platform built for SEO professionals, developers, and website owners. It provides limitless crawling and deep technical auditing without subscription fees or per-URL limits.

At its core, Crawler is driven by a multithreaded engine capable of analyzing thousands of URLs concurrently — with per-host politeness, adaptive rate-limit backoff, and WAF detection baked in. It goes beyond basic HTML parsing by offering headless JavaScript rendering for modern frameworks (React, Vue, SPAs) and intelligent CMS detection with one-click recommended settings.

Unlike cloud-based SEO platforms, **Crawler runs 100% locally**. Your website data, crawl histories, and architectural insights never leave your machine, ensuring absolute privacy and security.

---

## ✨ Key Features

### 🕸️ Powerful Crawling Engine
- **Unlimited URLs** — crawl 10 or 10,000+ pages concurrently, no paywall.
- **Multithreaded** — `ThreadPoolExecutor`-based workers (1–20) with per-host politeness delays.
- **Adaptive Rate Limiting** — automatic slow-down and host pause on `429` / `403` / `503`, with WAF detection (Wordfence, Cloudflare, Sucuri, SiteGround).
- **Smart Retries** — exponential backoff, `Retry-After` support, and User-Agent fallback.
- **robots.txt Aware** — full Google wildcard spec (`*`, `$`, longest-match-wins), with an opt-out toggle.
- **Headless JS Rendering** — optional Playwright integration for React, Vue, and SPA frameworks, plus a **JS vs non-JS content diff** that shows what AI crawlers (GPTBot, ClaudeBot…) would miss.
- **Bot-Challenge Solver** — headed-browser fallback (Linux/Xvfb) for hosts behind Cloudflare "Just a moment…" interstitials.
- **Crawl Identity** — crawl as Chrome, Firefox, Googlebot, bingbot, or a custom User-Agent.

### 📊 Deep SEO Auditing
- **On-Page SEO** — Titles, Meta Descriptions, H1/H2, Canonicals, Hreflang, and anchor-text quality.
- **Images** — missing/empty alt classification (W3C accessible-name aware), decorative & tracker filtering.
- **Structured Data** — JSON-LD and Microdata (Schema.org) detection.
- **Indexability** — `noindex`, `X-Robots-Tag`, robots.txt rules, and **AI-crawler blocking detection** (GPTBot, ClaudeBot, PerplexityBot, Bytespider…).
- **Technical Health** — broken links, redirect chains, 4xx/5xx, mixed content, and SSL-chain issues.
- **Content Quality** — thin content, near-duplicate pages (n-gram shingling), URL-trap / soft-404 probing.
- **Performance** — per-page estimates (TTFB, document weight, render-blocking resources, DOM size) + **real Lighthouse audits** via Google PageSpeed Insights.
- **SEO Score** — a live 0–100 score per crawl, computed from the weighted issue results.

### 📈 Reports, Diffing & Export
- **Issue Prioritization** — every issue grouped by severity (Errors / Warnings / Info) with expert "why it matters" context and sources.
- **Aggregated Reports** — duplicate titles / metas / H1s / bodies, redirect chains, orphan pages (sitemap diff), depth distribution, response codes.
- **Crawl Compare** — diff two crawls: added / removed / changed URLs, aggregate metrics, and structure shifts.
- **Saved History** — automatic 30-day crawl history with save / load / delete, plus **suspend & resume** and continue-at-page-cap support.
- **Bulk Export** — `CSV`, styled multi-sheet `.xlsx` (Excel), and standards-compliant `sitemap.xml` (auto split into a sitemap index beyond 50,000 URLs).

### 🖥️ Operational Extras
- **In-App Auto-Update** — one click reconciles your checkout to `origin/main` (Windows-safe).
- **Proxy Support** — round-robin proxy pool via `proxies.txt`.
- **Docker Ready** — single `docker compose up` deployment.

---

## 🆚 How It Compares

| Feature | Crawler (This Project) | Screaming Frog (Free) | Screaming Frog (Paid) | Ahrefs |
|---------|:---:|:---:|:---:|:---:|
| **URL Limit** | **Unlimited** | 500 | Unlimited | Per-credit |
| **Price** | **Free** | Free | £199 / yr | $129+ / mo |
| **Headless JS Rendering** | ✅ **Yes** | ✅ Yes | ✅ Yes | ❌ No |
| **AI-Crawler Visibility Report** | ✅ **Yes** | ❌ No | ❌ No | ❌ No |
| **Data Export** | ✅ **CSV, XLSX & XML** | ✅ CSV/Excel | ✅ CSV/Excel | ✅ CSV |
| **Self-Hosted** | ✅ Yes | ✅ Yes | ✅ Yes | ❌ Cloud |
| **Open Source** | ✅ **Yes** | ❌ No | ❌ No | ❌ No |

---

## ⚙️ Installation

**Prerequisite:** [Python 3.10+](https://www.python.org/downloads/)

### Quick Start (pip)

```bash
git clone https://github.com/alfa546/Crawler.git
cd Crawler
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
python3 app.py
```

Then open **[http://localhost:5002/](http://localhost:5002/)** in your browser.

> 💡 *For JavaScript-rendered sites (React / Vue / SPAs), also install Playwright:*
> ```bash
> pip install playwright && playwright install chromium
> ```

### 🐳 Docker

```bash
git clone https://github.com/alfa546/Crawler.git
cd Crawler
docker compose up -d
```

The container listens on port **5002** (JS rendering included).

### 📦 One-Line Auto-Installers

Automated setup scripts that register Crawler to start on boot and auto-update daily:

**Linux (Ubuntu / Debian / Mint)**
```bash
curl -fsSL https://raw.githubusercontent.com/alfa546/Crawler/main/install.sh -o install.sh && chmod +x install.sh && ./install.sh
```

**macOS**
```bash
curl -fsSL https://raw.githubusercontent.com/alfa546/Crawler/main/install-macos.sh -o install-macos.sh && chmod +x install-macos.sh && ./install.sh
```

**Windows 10/11 (PowerShell)**
```powershell
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; iwr https://raw.githubusercontent.com/alfa546/Crawler/main/install-windows.ps1 -OutFile install.ps1; powershell -ExecutionPolicy Bypass -File .\install.ps1
```

---

## 💡 Usage Guide

1. Enter a website URL in the main dashboard.
2. Click **Apply recommendations** for your detected CMS to auto-configure settings.
3. Tweak parameters if needed (Max Pages, Workers, Crawl Delay, Render JS).
4. Hit **Start crawl** — the Summary Dashboard streams live results as pages are audited.
5. Review reports (Issues, Duplicates, Redirects, Images, Sitemap diff), then download bulk reports via **`.csv`**, **`.xlsx`**, or **`sitemap.xml`**.

**Handy mid-crawl controls:** pause & resume, continue past the page cap, adjust include/exclude rules and crawl delay live, and re-crawl any single URL for a fresh audit.

---

## ⚙️ Configuration

Crawler works with zero configuration. Optional settings:

| Setting | How | Purpose |
|---------|-----|---------|
| `PAGESPEED_API_KEY` | `.env` file | Raises Google PageSpeed Insights quota for real Lighthouse audits. [Get a free key](https://developers.google.com/speed/docs/insights/v5/get-started). |
| `FLASK_DEBUG=1` | Environment variable | Enables Flask debug mode (development only — **never** expose publicly). |
| `proxies.txt` | Project root | One proxy per line (`IP:PORT`, `IP:PORT:USER:PASS`, or `http://…`). Round-robin rotation. |
| `SITE_CRAWLER_EXTRA_CRAWL_DIRS` | Environment variable | `:`-separated extra read-only folders for shared crawl history. |

> 🔒 **Privacy:** crawls are stored locally in `~/.site-crawler-crawls/` and are never uploaded anywhere. The `.env` file is excluded from the repository and Docker images.

---

## 🛠️ Architecture & Tech Stack

| Layer | Technology |
|-------|------------|
| **Backend** | Python 3.10+, Flask, Requests |
| **Parsing** | BeautifulSoup4, lxml |
| **Concurrency** | `ThreadPoolExecutor`, per-host politeness locks, adaptive backoff |
| **JS Rendering** | Playwright (headless Chromium; headed mode for challenge solving) |
| **Data Export** | Openpyxl (`.xlsx`), stdlib (`.csv`, `.xml`, `.zip`) |
| **Frontend** | Vanilla HTML/CSS/JS (Jinja2 templates), Server-Sent Events for live streaming |

```
Crawler/
├── app.py                   # Flask entrypoint
├── crawler/
│   ├── engine.py            # Concurrent crawl engine + SSE streaming
│   ├── routes.py            # REST API & UI routes
│   ├── seo_analyzer.py      # On-page audit rules, CMS detection
│   ├── performance.py       # Perf estimates + PageSpeed Insights client
│   ├── export_utils.py      # XLSX / sitemap.xml exports
│   ├── utils.py             # URL normalization, robots.txt, sitemap walk
│   ├── proxy_manager.py     # Proxy pool
│   └── globals.py           # Shared crawl state
├── challenge_browser.py     # Headed-browser Cloudflare fallback
├── static/                  # Frontend assets
└── templates/               # Jinja2 UI
```

---

## 🤝 Contributing

Contributions are warmly welcome! Whether you are reporting bugs, improving the UI, or adding new features (like Core Web Vitals site-wide passes or custom XPath extraction), please read our [CONTRIBUTING.md](CONTRIBUTING.md).

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

---

## 🛡️ License & Privacy

This project is open-source software licensed under the [MIT License](LICENSE). Copyright (c) 2026 Nouman Sajid.

**Your data stays yours.** Crawler runs entirely on your machine — no telemetry, no cloud sync, no per-URL limits.

---

<div align="center">

If you find Crawler useful, please consider giving it a ⭐ — it helps other SEO professionals discover the project.

<i>Built with ❤️ by Nouman Sajid and the Open Source Community.</i>

</div>
