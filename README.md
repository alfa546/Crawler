<div align="center">

# 🚀 Crawler 
**Free, Self-Hosted SEO Crawler, Audit & Live Ranking Tool**

[![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20macOS%20%7C%20Windows-blue)](#installation)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![UI: Modern](https://img.shields.io/badge/UI-Modern_Redesign-success)](#)
[![Feature: Live SEO](https://img.shields.io/badge/Feature-Live_SEO_Ranking-success)](#)

</div>

<br />

**Crawler** is an enterprise-grade, open-source technical SEO crawler that runs locally on your machine. We've completely **redesigned the UI** and introduced **Live SEO Ranking** to make it the ultimate, drop-in alternative to tools like Screaming Frog, Sitebulb, and Ahrefs Site Audit. No accounts, no cloud, no per-URL limits—just unlimited SEO auditing power.

### 🎥 See it in Action

<div align="center">
  <video src="https://github.com/alfa546/Crawler/raw/main/demo.mp4" width="100%" controls="controls"></video>
</div>

---

## ✨ What's New

- 🎨 **Completely Redesigned UI**: A modern, sleek, and intuitive interface with a dark mode toggle that makes analyzing data easier than ever.
- 📈 **Live SEO Ranking**: Track website rankings in real-time right alongside your technical audit.
- 🔗 **Malformed Link Detection**: Prevent phantom 404s caused by plain text pasted into `href` tags.
- 🤖 **JS vs HTML Compare**: See exactly what content is hidden from AI crawlers like GPTBot and Google-Extended.
- 💰 **Crawl Budget Analysis**: Automatically detect infinite URL traps and generate ready-to-paste `robots.txt` rules.

## 🚀 Key Features

### 🕸️ Powerful Crawling Engine
- **Unlimited URLs**: Crawl 10 or 10,000 pages concurrently without paying a cent.
- **CMS-Aware**: Auto-detects Shopify, WordPress, Webflow, Wix, Squarespace, and more.
- **Smart Retry & Rate Limiting**: Exponential backoff prevents you from being blocked by servers.
- **Headless JS Rendering**: Optional Playwright integration to render React, Vue, and SPA frameworks.

### 📊 Deep SEO Auditing
- **On-Page SEO**: Validate Titles, Meta Descriptions, H1 tags, and Canonical tags.
- **Structured Data**: Detects JSON-LD and Microdata (Schema.org).
- **Technical Health**: Uncover broken links, redirect chains, 4xx/5xx errors, and mixed content.
- **Indexability**: Checks `noindex`, `robots.txt`, and AI-crawler blocking.
- **Content Quality**: Flags thin content and near-duplicate pages.

### 📋 Bulk Export & Reporting
- Generate Excel (`.xlsx`) reports for Titles, H1s, Missing Alt Texts, Hreflang, and Redirect Chains.
- Group issues by severity (Errors / Warnings / Info) to prioritize the highest-impact fixes.

## 🆚 How it Compares

| Feature | Crawler | Screaming Frog (Free) | Screaming Frog (Paid) | Ahrefs |
|---------|---------|-----------------------|-----------------------|--------|
| **URL Limit** | **Unlimited** | 500 | Unlimited | Per-credit |
| **Price** | **Free** | Free | £199 / yr | $129+ / mo |
| **Live SEO Ranking** | ✅ **Yes** | ❌ No | ❌ No | ❌ No |
| **Self-Hosted** | ✅ Yes | ✅ Yes | ✅ Yes | ❌ Cloud |
| **Open Source** | ✅ Yes | ❌ No | ❌ No | ❌ No |

## ⚙️ Quick Install

Requires Python 3.10+.

```bash
git clone https://github.com/alfa546/Crawler.git
cd Crawler
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
python3 app.py
```
Then, open [http://localhost:5002/](http://localhost:5002/) in your browser.

### 📦 One-Line Auto-Installers

We provide automated setup scripts that register Crawler to start on boot and auto-update daily.

**Linux (Ubuntu/Debian/Mint)**
```bash
curl -fsSL https://raw.githubusercontent.com/alfa546/Crawler/main/install.sh -o install.sh && chmod +x install.sh && ./install.sh
```

**macOS**
```bash
curl -fsSL https://raw.githubusercontent.com/alfa546/Crawler/main/install-macos.sh -o install-macos.sh && chmod +x install-macos.sh && ./install-macos.sh
```

**Windows 10/11 (PowerShell)**
```powershell
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; iwr https://raw.githubusercontent.com/alfa546/Crawler/main/install-windows.ps1 -OutFile install.ps1; powershell -ExecutionPolicy Bypass -File .\install.ps1
```

*(For SPA rendering support, install Playwright: `pip install playwright && playwright install chromium`)*

## 💡 Usage Guide

1. Enter a website URL.
2. Click **Apply recommendations** for your detected CMS to automatically configure settings.
3. Tweak parameters if needed (Max Pages, Workers, Render JS).
4. Hit **Start crawl**. 

The **Summary Dashboard** will provide live results. Once completed, download bulk reports via XLSX.

## 🛠️ Architecture & Tech Stack
- **Backend**: Python 3.10+, Flask, requests, BeautifulSoup4, lxml.
- **Frontend**: Custom redesigned HTML/CSS/JS (Jinja2 Templates).
- **Concurrency**: ThreadPoolExecutor for fast, scalable concurrent crawls.
- **Data Export**: Openpyxl for direct `.xlsx` generation.

## 🤝 Contributing

Contributions are warmly welcome! Whether you are reporting bugs, improving the new UI, or adding new features (like expanding the Live SEO ranking tool), please read our [CONTRIBUTING.md](CONTRIBUTING.md).

## 🛡️ License & Privacy

**Privacy-First**: Crawler runs 100% locally. We do not track you, and your crawl data never leaves your machine. 

This project is open-source software licensed under the [MIT License](LICENSE). Copyright (c) 2026 Nouman Sajid.

---
<div align="center">
  <i>Built with ❤️ by Nouman Sajid and the Open Source Community.</i>
</div>
