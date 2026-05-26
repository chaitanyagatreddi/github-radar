#!/usr/bin/env python3
"""
GitHub Radar — Cybersecurity Contributor Crawler
=================================================
Uses Browserbase + Playwright to crawl GitHub for cybersecurity repos
and map who is contributing to what.

Input:  keyword (e.g. "vulnerability scanner", "SIEM", "pentest")
Output: top repos, top contributors, contributor profiles, activity scores

Usage:
  python3 github_crawler.py --keyword "vulnerability scanner" --repos 5
"""

import asyncio
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Optional

from browserbase import Browserbase
from playwright.async_api import async_playwright
import openai

# ── Config ────────────────────────────────────────────────────────

BB_API_KEY     = os.environ.get("BROWSERBASE_API_KEY", "")
BB_PROJECT_ID  = os.environ.get("BROWSERBASE_PROJECT_ID", "")
OPENAI_KEY     = os.environ.get("OPENAI_API_KEY", "")

# ── Data structures ───────────────────────────────────────────────

@dataclass
class Repo:
    name: str           # owner/repo
    url: str
    stars: int = 0
    description: str = ""
    language: str = ""
    contributors: list = field(default_factory=list)

@dataclass
class Contributor:
    username: str
    profile_url: str
    commits: int = 0
    avatar: str = ""
    name: str = ""
    company: str = ""
    location: str = ""
    bio: str = ""
    pinned_repos: list = field(default_factory=list)
    orgs: list = field(default_factory=list)
    email: str = ""
    website: str = ""
    activity_score: int = 0
    repos_contributed: list = field(default_factory=list)


# ── Browser Scanner ───────────────────────────────────────────────

class GitHubBrowserScanner:
    """Browserbase + Playwright scanner targeting GitHub pages."""

    def __init__(self):
        self.bb = Browserbase(api_key=BB_API_KEY)
        self.session = None
        self.browser = None
        self.context = None
        self.page = None
        self.pw = None

    async def start(self):
        print("  🌐 Starting Browserbase session...")
        self.session = self.bb.sessions.create(project_id=BB_PROJECT_ID)
        print(f"  ✅ Session: {self.session.id}")

        debug = self.bb.sessions.debug(self.session.id)
        connect_url = debug.ws_url

        self.pw = await async_playwright().__aenter__()
        self.browser = await self.pw.chromium.connect_over_cdp(connect_url)
        self.context = (
            self.browser.contexts[0]
            if self.browser.contexts
            else await self.browser.new_context()
        )
        self.page = (
            self.context.pages[0]
            if self.context.pages
            else await self.context.new_page()
        )
        # Set a realistic user agent
        await self.page.set_extra_http_headers({
            "Accept-Language": "en-US,en;q=0.9",
        })
        print("  ✅ Browser connected")
        return self

    # Blacklisted prefixes — not real repos
    SKIP_OWNERS = {"sponsors", "search", "trending", "explore", "topics",
                   "marketplace", "features", "enterprise", "collections",
                   "contact", "about", "login", "signup", "settings", "orgs"}

    async def search_repos(self, keyword: str, max_repos: int = 5) -> list[Repo]:
        """Search GitHub for repos matching the keyword, sorted by stars."""
        url = (
            f"https://github.com/search?q={keyword.replace(' ', '+')}"
            f"&type=repositories&s=stars&o=desc"
        )
        print(f"  🔍 Searching GitHub: {keyword}")
        repos = []

        try:
            await self.page.goto(url, timeout=20000)
            await self.page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(4)

            seen = set()

            # Strategy 1: GitHub's current search result layout
            # Each result has an <h3> with a link like /owner/repo
            result_links = await self.page.query_selector_all(
                "div[data-testid='results-list'] a[href*='/'], "
                ".search-result-item a[href], "
                "ul.repo-list li a[href]"
            )
            for link in result_links:
                href = (await link.get_attribute("href") or "").strip()
                # Reject anything with query strings, fragments, or encoded chars
                if "?" in href or "#" in href or "%" in href:
                    continue
                parts = href.strip("/").split("/")
                if (len(parts) == 2
                        and parts[0] not in self.SKIP_OWNERS
                        and parts[1]
                        and "." not in parts[0]
                        and not parts[1].startswith(".")
                        and re.match(r'^[a-zA-Z0-9][a-zA-Z0-9\-\.]{0,38}$', parts[0])
                        and re.match(r'^[a-zA-Z0-9][a-zA-Z0-9\-\.\_]{0,99}$', parts[1])):
                    full = f"https://github.com/{parts[0]}/{parts[1]}"
                    if full not in seen:
                        seen.add(full)
                        repos.append(Repo(name=f"{parts[0]}/{parts[1]}", url=full))
                if len(repos) >= max_repos:
                    break

            # Strategy 2: parse all links on the page, filter aggressively
            if len(repos) < max_repos:
                all_links = await self.page.query_selector_all("a[href]")
                for link in all_links:
                    href = (await link.get_attribute("href") or "").strip()
                    if not href.startswith("/"):
                        continue
                    parts = href.strip("/").split("/")
                    if (len(parts) == 2
                            and parts[0] not in self.SKIP_OWNERS
                            and parts[1]
                            and len(parts[0]) > 1
                            and len(parts[1]) > 1
                            and "." not in parts[0]
                            and not any(x in parts[1] for x in ["?", "#", "."])):
                        full = f"https://github.com/{parts[0]}/{parts[1]}"
                        if full not in seen:
                            seen.add(full)
                            repos.append(Repo(name=f"{parts[0]}/{parts[1]}", url=full))
                    if len(repos) >= max_repos:
                        break

        except Exception as e:
            print(f"  ⚠️  Search error: {e}")

        print(f"  ✅ Found {len(repos)} repos")
        return repos[:max_repos]

    async def get_repo_details(self, repo: Repo) -> Repo:
        """Visit a repo page and extract stars, description, language."""
        try:
            await self.page.goto(repo.url, timeout=15000)
            await self.page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(2)

            # Stars
            try:
                star_el = await self.page.query_selector("#repo-stars-counter-star, span[id*='stargazers']")
                if star_el:
                    txt = await star_el.inner_text()
                    txt = txt.replace(",", "").replace("k", "000").strip()
                    repo.stars = int(re.sub(r"[^\d]", "", txt) or "0")
            except:
                pass

            # Description — try multiple selectors across GitHub DOM versions
            try:
                desc_el = await self.page.query_selector(
                    "[data-testid='repo-description'], "
                    "p.f4.my-3, "
                    ".repository-description, "
                    "#repo-content-pjax-container p.color-fg-muted, "
                    "p[class*='description']"
                )
                if desc_el:
                    repo.description = (await desc_el.inner_text()).strip()[:200]
                else:
                    # Fallback: grab meta description tag
                    meta = await self.page.query_selector("meta[name='description']")
                    if meta:
                        repo.description = (await meta.get_attribute("content") or "").strip()[:200]
            except:
                pass

            # Language
            try:
                lang_el = await self.page.query_selector("span[itemprop='programmingLanguage']")
                if lang_el:
                    repo.language = (await lang_el.inner_text()).strip()
            except:
                pass

        except Exception as e:
            print(f"  ⚠️  Repo detail error for {repo.name}: {e}")

        return repo

    async def get_contributors(self, repo: Repo, max_contributors: int = 8) -> list[Contributor]:
        """Get contributors via GitHub REST API (primary) with browser scrape fallback."""
        contributors = []
        seen_users: set = set()

        # Strategy 1: GitHub REST API — reliable, no auth needed for public repos
        api_data = self._fetch_json(
            f"https://api.github.com/repos/{repo.name}/contributors?per_page={max_contributors}&anon=false"
        )
        if isinstance(api_data, list):
            for item in api_data[:max_contributors]:
                username = item.get("login", "")
                if username and username not in seen_users:
                    seen_users.add(username)
                    contributors.append(Contributor(
                        username=username,
                        profile_url=f"https://github.com/{username}",
                        commits=item.get("contributions", 0),
                        repos_contributed=[repo.name],
                    ))

        # Strategy 2: Browser scrape fallback if API returned nothing
        if not contributors:
            url = f"{repo.url}/contributors"
            try:
                await self.page.goto(url, timeout=20000)
                await self.page.wait_for_load_state("domcontentloaded")
                await asyncio.sleep(3)

                # Try multiple selector patterns for GitHub's evolving DOM
                for selector in [
                    "a[data-hovercard-type='user']",
                    "li.contrib-person a[href]",
                    ".contrib-person a[href]",
                    "ol.contrib-data li a[href]",
                ]:
                    links = await self.page.query_selector_all(selector)
                    for link in links:
                        href = (await link.get_attribute("href") or "").strip("/")
                        if (href and "/" not in href
                                and re.match(r'^[a-zA-Z0-9][a-zA-Z0-9\-]{0,37}$', href)
                                and href not in seen_users):
                            seen_users.add(href)
                            contributors.append(Contributor(
                                username=href,
                                profile_url=f"https://github.com/{href}",
                                repos_contributed=[repo.name],
                            ))
                        if len(contributors) >= max_contributors:
                            break
                    if contributors:
                        break

            except Exception as e:
                print(f"  ⚠️  Contributors browser error for {repo.name}: {e}")

        return contributors[:max_contributors]

    async def get_profile(self, contributor: Contributor) -> Contributor:
        """Visit a contributor's GitHub profile and extract details."""
        try:
            await self.page.goto(contributor.profile_url, timeout=15000)
            await self.page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(2)

            # Real name
            try:
                name_el = await self.page.query_selector("span[itemprop='name'], .p-name")
                if name_el:
                    contributor.name = (await name_el.inner_text()).strip()
            except:
                pass

            # Company
            try:
                co_el = await self.page.query_selector("span[itemprop='worksFor'], .p-org")
                if co_el:
                    contributor.company = (await co_el.inner_text()).strip()
            except:
                pass

            # Location
            try:
                loc_el = await self.page.query_selector("span[itemprop='homeLocation'], .p-label")
                if loc_el:
                    contributor.location = (await loc_el.inner_text()).strip()
            except:
                pass

            # Bio
            try:
                bio_el = await self.page.query_selector("div[data-bio-text], .p-note")
                if bio_el:
                    contributor.bio = (await bio_el.inner_text()).strip()[:200]
            except:
                pass

            # Pinned repos
            try:
                pinned = await self.page.query_selector_all("div.pinned-item-list-item a.mr-1")
                contributor.pinned_repos = []
                for p in pinned[:6]:
                    txt = (await p.inner_text()).strip()
                    if txt:
                        contributor.pinned_repos.append(txt)
            except:
                pass

            # Orgs
            try:
                org_els = await self.page.query_selector_all("a[data-hovercard-type='organization']")
                contributor.orgs = []
                for o in org_els[:5]:
                    txt = (await o.get_attribute("href") or "").strip("/")
                    if txt:
                        contributor.orgs.append(txt)
            except:
                pass

            # Email (publicly visible on profile)
            try:
                email_el = await self.page.query_selector(
                    "a[href^='mailto:'], li[itemprop='email'], "
                    ".p-email, span[itemprop='email']"
                )
                if email_el:
                    raw = await email_el.inner_text()
                    contributor.email = raw.strip()
                else:
                    page_text = await self.page.inner_text(".vcard-details") if await self.page.query_selector(".vcard-details") else ""
                    match = re.search(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", page_text)
                    if match:
                        contributor.email = match.group(0)
            except:
                pass

            # Website URL from profile (used later for Fix A email mining)
            try:
                website_el = await self.page.query_selector(
                    "a[itemprop='url'], .p-url, li a[rel='nofollow me']"
                )
                if website_el:
                    contributor.website = (await website_el.get_attribute("href") or "").strip()
            except:
                pass

        except Exception as e:
            print(f"  ⚠️  Profile error for {contributor.username}: {e}")

        return contributor

    # ── Fix A: personal website email mining via Firecrawl ───────

    @staticmethod
    def crawl_website_email(website_url: str) -> str:
        """Scrape a contributor's personal website with Firecrawl and extract email.

        Firecrawl handles JS-rendered sites and returns clean markdown.
        Tries root URL first, then /contact and /about paths.
        Returns the first clean email found, or empty string.
        """
        if not website_url or not website_url.startswith("http"):
            return ""

        fc_key = os.environ.get("FIRECRAWL_API_KEY", "")
        if not fc_key:
            return ""

        try:
            from firecrawl import FirecrawlApp
        except ImportError:
            return ""

        NOREPLY = {"noreply@github.com", "users.noreply.github.com"}
        EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

        app = FirecrawlApp(api_key=fc_key)
        base = website_url.rstrip("/")
        paths_to_try = ["", "/contact", "/about", "/about-me"]

        for path in paths_to_try:
            url = base + path
            try:
                result = app.scrape_url(url, formats=["markdown"])
                text = html.unescape(result.markdown or "")
                for match in EMAIL_RE.finditer(text):
                    email = match.group(0).lower()
                    if not any(nr in email for nr in NOREPLY):
                        return email
            except Exception:
                continue

        return ""

    # ── Fix B: DuckDuckGo search via Firecrawl ───────────────────

    @staticmethod
    def enrich_via_search(name: str, company: str, username: str) -> str:
        """Search DuckDuckGo for the contributor's email using Firecrawl.

        Query: '"name" "company" email contact'
        Scrapes the search results page and extracts any email found.
        Returns the first clean email or empty string.
        """
        fc_key = os.environ.get("FIRECRAWL_API_KEY", "")
        if not fc_key or not name:
            return ""

        try:
            from firecrawl import FirecrawlApp
        except ImportError:
            return ""

        NOREPLY = {"noreply@github.com", "users.noreply.github.com"}
        EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

        app = FirecrawlApp(api_key=fc_key)

        # Build a tight query — name + company or username
        query_parts = [f'"{name}"']
        if company:
            query_parts.append(f'"{company}"')
        else:
            query_parts.append(username)
        query_parts.append("email contact")
        query = " ".join(query_parts)

        search_url = "https://duckduckgo.com/html/?q=" + urllib.parse.quote(query)

        try:
            result = app.scrape_url(search_url, formats=["markdown"])
            text = html.unescape(result.markdown or "")
            for match in EMAIL_RE.finditer(text):
                email = match.group(0).lower()
                if not any(nr in email for nr in NOREPLY) and "duckduckgo" not in email:
                    return email
        except Exception:
            pass

        return ""

    # ── Public email crawling ────────────────────────────────────

    @staticmethod
    def _fetch_json(url: str) -> list | dict | None:
        """Fetch JSON from a public URL (no auth). Returns None on error."""
        req = urllib.request.Request(url, headers={"User-Agent": "GitHubRadar/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except (urllib.error.URLError, json.JSONDecodeError, Exception):
            return None

    @staticmethod
    def crawl_public_emails(username: str) -> list[str]:
        """Crawl publicly available emails for a GitHub user.

        Sources (all public, no auth needed):
        1. GitHub Events API — PushEvent payloads contain commit author emails
        2. GitHub .patch endpoint — commit patches on personal repos expose Author email
        3. Personal repo commit history — scan own repos (not org repos) more aggressively

        Returns deduplicated list of emails, filtering out noreply addresses.
        """
        emails: set[str] = set()
        NOREPLY = {"noreply@github.com", "users.noreply.github.com"}

        def is_clean(email: str) -> bool:
            return bool(email) and not any(nr in email for nr in NOREPLY)

        # Source 1: Events API — /users/{user}/events/public
        events = GitHubBrowserScanner._fetch_json(
            f"https://api.github.com/users/{username}/events/public"
        )
        if isinstance(events, list):
            for event in events:
                if event.get("type") == "PushEvent":
                    for commit in event.get("payload", {}).get("commits", []):
                        email = commit.get("author", {}).get("email", "")
                        if is_clean(email):
                            emails.add(email.lower())

        # Source 2 & 3: Personal repos (owned by user, not forks) — mine commit patches
        # Personal repos have lower privacy paranoia than security org repos.
        # We fetch more repos and more commits per repo to maximize hit rate.
        repos_data = GitHubBrowserScanner._fetch_json(
            f"https://api.github.com/users/{username}/repos?sort=pushed&per_page=10&type=owner"
        )
        if isinstance(repos_data, list):
            # Prioritise non-forked repos first (original work = more likely to have real email)
            owned = [r for r in repos_data if not r.get("fork", False)]
            forked = [r for r in repos_data if r.get("fork", False)]
            ordered = owned[:6] + forked[:2]  # up to 8 repos total

            for repo in ordered:
                if emails:
                    # Stop once we have at least one clean email — avoid rate limits
                    break
                repo_full = repo.get("full_name", "")
                if not repo_full:
                    continue

                # Fetch up to 5 recent commits by this author in their own repo
                commits = GitHubBrowserScanner._fetch_json(
                    f"https://api.github.com/repos/{repo_full}/commits?author={username}&per_page=5"
                )
                if not isinstance(commits, list):
                    continue

                for commit_item in commits[:5]:
                    sha = commit_item.get("sha", "")
                    if not sha:
                        continue

                    # Try commit detail API first (faster, no extra request for patch)
                    commit_detail = GitHubBrowserScanner._fetch_json(
                        f"https://api.github.com/repos/{repo_full}/git/commits/{sha}"
                    )
                    if isinstance(commit_detail, dict):
                        email = (commit_detail.get("author") or {}).get("email", "")
                        if is_clean(email):
                            emails.add(email.lower())
                            continue  # Got it, no need for patch

                    # Fallback: .patch file (richer but slower)
                    patch_url = f"https://github.com/{repo_full}/commit/{sha}.patch"
                    try:
                        req = urllib.request.Request(patch_url, headers={"User-Agent": "GitHubRadar/1.0"})
                        with urllib.request.urlopen(req, timeout=10) as resp:
                            patch_text = resp.read().decode("utf-8", errors="ignore")[:2000]
                        for match in re.finditer(r'(?:From|Author):\s*[^<]*<([^>]+)>', patch_text):
                            email = match.group(1).lower()
                            if is_clean(email):
                                emails.add(email)
                    except Exception:
                        pass

        return sorted(emails)

    async def stop(self):
        try:
            if self.browser:
                await self.browser.close()
            if self.pw:
                await self.pw.stop()
            if self.session:
                self.bb.sessions.update(self.session.id, status="REQUEST_RELEASE")
                print(f"  🛑 Session ended: {self.session.id}")
        except Exception as e:
            print(f"  ⚠️  Cleanup error: {e}")


# ── Analyzer ──────────────────────────────────────────────────────

class GitHubAnalyzer:
    """Uses gpt-4o-mini to score and summarize contributors."""

    def __init__(self):
        self.client = openai.OpenAI(api_key=OPENAI_KEY)

    def score_contributor(self, contributor: Contributor, keyword: str) -> dict:
        """Score and summarize a contributor based on their profile."""
        profile_text = f"""
Username: {contributor.username}
Name: {contributor.name}
Company: {contributor.company}
Location: {contributor.location}
Bio: {contributor.bio}
Pinned repos: {', '.join(contributor.pinned_repos)}
Orgs: {', '.join(contributor.orgs)}
Commits in scanned repos: {contributor.commits}
Repos contributed to: {', '.join(contributor.repos_contributed)}
"""
        prompt = f"""You are analyzing a GitHub contributor in the cybersecurity / {keyword} space.

{profile_text}

Return JSON only. No markdown:
{{
  "activity_score": <0-100, based on commits, repos, engagement>,
  "tier": "core" | "active" | "emerging",
  "summary": "<1 sentence: who they are and what they build>",
  "focus_areas": ["area1", "area2"],
  "interesting": true/false
}}"""
        try:
            resp = self.client.chat.completions.create(
                model="gpt-4o-mini",
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            )
            txt = resp.choices[0].message.content.strip()
            start = txt.find("{")
            end = txt.rfind("}") + 1
            if start >= 0:
                return json.loads(txt[start:end])
        except Exception as e:
            print(f"  ⚠️  Score error for {contributor.username}: {e}")

        return {"activity_score": contributor.commits, "tier": "active", "summary": "", "interesting": True}

    def build_report(self, keyword: str, repos: list[Repo], contributors: list[Contributor], analyses: list[dict]) -> dict:
        """Build the final GitHub radar report."""
        # Deduplicate contributors by username, merge repos_contributed
        contrib_map: dict[str, Contributor] = {}
        for c in contributors:
            if c.username not in contrib_map:
                contrib_map[c.username] = c
            else:
                contrib_map[c.username].repos_contributed = list(set(
                    contrib_map[c.username].repos_contributed + c.repos_contributed
                ))
                contrib_map[c.username].commits += c.commits

        # Merge analysis scores
        analysis_map = {a.get("username", ""): a for a in analyses}
        for username, c in contrib_map.items():
            a = analysis_map.get(username, {})
            c.activity_score = a.get("activity_score", c.commits)

        # Sort by activity score
        sorted_contributors = sorted(contrib_map.values(), key=lambda c: c.activity_score, reverse=True)

        return {
            "keyword": keyword,
            "repos_scanned": len(repos),
            "contributors_found": len(sorted_contributors),
            "repos": [
                {
                    "name": r.name,
                    "url": r.url,
                    "stars": r.stars,
                    "description": r.description,
                    "language": r.language,
                    "contributor_count": len(r.contributors),
                }
                for r in repos
            ],
            "top_contributors": [
                {
                    "username": c.username,
                    "name": c.name,
                    "company": c.company,
                    "location": c.location,
                    "bio": c.bio,
                    "email": c.email,
                    "activity_score": c.activity_score,
                    "commits": c.commits,
                    "repos_contributed": c.repos_contributed,
                    "pinned_repos": c.pinned_repos,
                    "orgs": c.orgs,
                    "profile_url": c.profile_url,
                    **analysis_map.get(c.username, {}),
                }
                for c in sorted_contributors[:20]
            ],
        }


# ── Main Agent ────────────────────────────────────────────────────

class GitHubRadarAgent:
    """Orchestrates the full GitHub crawl pipeline with SSE streaming."""

    def __init__(self, keyword: str, max_repos: int = 5, max_contributors: int = 8):
        self.keyword = keyword
        self.max_repos = max_repos
        self.max_contributors = max_contributors
        self.scanner = GitHubBrowserScanner()
        self.analyzer = GitHubAnalyzer()

    async def run(self, yield_event=None):
        """
        Run the full crawl.
        yield_event(type, message, data=None) — called for SSE streaming.
        """

        def emit(type_, msg, data=None):
            if yield_event:
                yield_event(type_, msg, data)
            else:
                print(f"  [{type_}] {msg}")

        emit("agent", "🚀 GitHub Radar starting...", {"keyword": self.keyword})

        repos: list[Repo] = []
        all_contributors: list[Contributor] = []
        all_analyses: list[dict] = []

        try:
            # Start browser
            emit("agent", "🌐 Starting Browserbase session...")
            await self.scanner.start()
            emit("agent", f"✅ Browser connected — session {self.scanner.session.id}")

            # Step 1: Search repos
            emit("agent", f"🔍 Searching GitHub for: {self.keyword}")
            repos = await self.scanner.search_repos(self.keyword, self.max_repos)
            emit("repos_found", f"Found {len(repos)} repos", {"repos": [r.name for r in repos]})

            # Step 2: Get repo details + contributors
            for i, repo in enumerate(repos):
                emit("scanning_repo", f"📦 Scanning {repo.name} ({i+1}/{len(repos)})")

                repo = await self.scanner.get_repo_details(repo)
                emit("repo_detail", f"⭐ {repo.stars:,} stars — {repo.description[:80]}", {
                    "repo": repo.name, "stars": repo.stars, "description": repo.description
                })

                emit("agent", f"👥 Fetching contributors for {repo.name}...")
                contributors = await self.scanner.get_contributors(repo, self.max_contributors)
                repo.contributors = [c.username for c in contributors]
                emit("contributors_found", f"Found {len(contributors)} contributors in {repo.name}", {
                    "repo": repo.name,
                    "contributors": [c.username for c in contributors]
                })
                all_contributors.extend(contributors)

            # Step 3: Profile top contributors (unique, top 10)
            seen = set()
            unique_contributors = []
            for c in all_contributors:
                if c.username not in seen:
                    seen.add(c.username)
                    unique_contributors.append(c)

            top_to_profile = sorted(unique_contributors, key=lambda c: c.commits, reverse=True)[:10]

            for i, contributor in enumerate(top_to_profile):
                emit("profiling", f"👤 Profiling @{contributor.username} ({i+1}/{len(top_to_profile)})")
                contributor = await self.scanner.get_profile(contributor)

                # ── Email waterfall: GitHub → Fix A → Fix B ──────────
                # Step 0: already have email from profile scrape?
                if not contributor.email:
                    # GitHub events API + personal repo commit patches
                    emit("crawling_email", f"📧 [GitHub] Crawling commits for @{contributor.username}...")
                    crawled_emails = self.scanner.crawl_public_emails(contributor.username)
                    if crawled_emails:
                        all_emails = set(crawled_emails)
                        contributor.email = ", ".join(sorted(all_emails))

                # Fix A: personal website
                if not contributor.email and contributor.website:
                    emit("crawling_email", f"📧 [Website] Checking {contributor.website} for @{contributor.username}...")
                    website_email = self.scanner.crawl_website_email(contributor.website)
                    if website_email:
                        contributor.email = website_email
                        emit("email_found", f"📧 [Website] @{contributor.username} → {contributor.email}")

                # Fix B: DuckDuckGo search via Firecrawl
                if not contributor.email and contributor.name:
                    emit("crawling_email", f"📧 [Search] DuckDuckGo lookup for @{contributor.username}...")
                    search_email = self.scanner.enrich_via_search(contributor.name, contributor.company, contributor.username)
                    if search_email:
                        contributor.email = search_email
                        emit("email_found", f"📧 [Search] @{contributor.username} → {contributor.email}")

                if contributor.email:
                    emit("email_found", f"📧 @{contributor.username} → {contributor.email}")
                else:
                    emit("email_none", f"📧 @{contributor.username} — no email found (all sources exhausted)")

                emit("profile_done", f"@{contributor.username} — {contributor.company or contributor.bio[:50] or 'no bio'}")

            # Stop browser
            await self.scanner.stop()
            emit("agent", "🛑 Browser session closed")

            # Step 4: Analyze with gpt-4o-mini
            emit("agent", f"🧠 Analysing {len(top_to_profile)} contributors with gpt-4o-mini...")
            for contributor in top_to_profile:
                emit("analyzing", f"🤖 Scoring @{contributor.username}...")
                analysis = self.analyzer.score_contributor(contributor, self.keyword)
                analysis["username"] = contributor.username
                all_analyses.append(analysis)
                emit("scored", f"@{contributor.username} → score {analysis.get('activity_score', 0)} ({analysis.get('tier', '?')})", analysis)

            # Step 5: Build report
            emit("agent", "📊 Building report...")
            report = self.analyzer.build_report(self.keyword, repos, all_contributors, all_analyses)
            emit("complete", "✅ GitHub Radar scan complete!", report)
            return report

        except Exception as e:
            import traceback
            emit("error", f"❌ {str(e)}")
            traceback.print_exc()
            try:
                await self.scanner.stop()
            except:
                pass
            return {"error": str(e), "keyword": self.keyword}


# ── CLI ───────────────────────────────────────────────────────────

async def main():
    import argparse
    parser = argparse.ArgumentParser(description="GitHub Radar — Cybersecurity Contributor Crawler")
    parser.add_argument("--keyword", default="vulnerability scanner", help="Search keyword")
    parser.add_argument("--repos", type=int, default=3, help="Max repos to scan")
    parser.add_argument("--contributors", type=int, default=6, help="Max contributors per repo")
    args = parser.parse_args()

    if not BB_API_KEY or not BB_PROJECT_ID or not OPENAI_KEY:
        print("❌ Set BROWSERBASE_API_KEY, BROWSERBASE_PROJECT_ID, OPENAI_API_KEY")
        sys.exit(1)

    agent = GitHubRadarAgent(
        keyword=args.keyword,
        max_repos=args.repos,
        max_contributors=args.contributors,
    )
    report = await agent.run()

    print("\n" + "="*60)
    print(f"  🛡️  GITHUB RADAR — {report.get('keyword', '').upper()}")
    print("="*60)
    print(f"  Repos scanned: {report.get('repos_scanned', 0)}")
    print(f"  Contributors: {report.get('contributors_found', 0)}")
    print("\n📦 TOP REPOS")
    for r in report.get("repos", []):
        print(f"  ⭐ {r['stars']:>6,}  {r['name']:<40}  {r['description'][:60]}")

    print("\n👥 TOP CONTRIBUTORS")
    for c in report.get("top_contributors", [])[:10]:
        tier = c.get("tier", "?")
        score = c.get("activity_score", 0)
        email = c.get("email", "") or "—"
        summary = c.get("summary", c.get("bio", ""))[:70]
        print(f"  [{tier:8}] @{c['username']:<20} score={score:>3}  📧 {email}")
        if summary:
            print(f"             {summary}")

    # Save
    out = f"github_radar_{args.keyword.replace(' ', '_')}.json"
    with open(out, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\n📄 Saved: {out}")


if __name__ == "__main__":
    asyncio.run(main())
