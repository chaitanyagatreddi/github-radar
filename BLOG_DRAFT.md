# I Built a GitHub Radar to Find Cybersecurity Contributors (and Their Emails)

*Browserbase + GitHub Events API + gpt-4o-mini — deployed on Hugging Face Spaces*

---

I wanted to find the people actually building cybersecurity tools — not the companies, the individual contributors pushing commits to Nuclei, OWASP ZAP, Snyk, Semgrep.

So I built a tool that does it automatically.

You type a keyword. It scans GitHub, finds the top repos, maps contributors, and crawls publicly available emails from commit metadata.

> *"Found 8 contributors in snyk/cli. @johndoe → john@example.com"*

**[📸 SCREENSHOT 1: Full app UI — keyword input, agent chips, contributor table with emails]**

Live demo → [huggingface.co/spaces/chaitubatman/github-radar](https://huggingface.co/spaces/chaitubatman/github-radar)  
Code → [github.com/chaitanyagatreddi/github-radar](https://github.com/chaitanyagatreddi/github-radar)

---

## Why I built this

Cold outreach to security engineers is broken. Everyone targets the same LinkedIn profiles. The people actually shipping tools — the ones with 500 GitHub commits and no LinkedIn — are invisible to most outreach.

GitHub is the real directory. Commits are the real signal.

---

## Architecture

```
React UI (keyword input + live log + contributor table)
        ↕ SSE stream
Flask backend
        ↕
GitHubRadarAgent
        ↕
Browserbase + Playwright  →  GitHub search + repo pages
        ↕
GitHub REST API           →  contributors (by commit count)
        ↕
GitHub Events API         →  public emails from PushEvent commits
        ↕
Commit patch files        →  Author: name <email> extraction
        ↕
gpt-4o-mini               →  score + tier + summary per contributor
```

---

## Step 1 — Search repos with Browserbase

The GitHub search UI doesn't have a clean API for anonymous use. Browserbase spins up a real Chromium browser in the cloud and scrapes search results:

```python
# github_crawler.py
from browserbase import Browserbase
from playwright.async_api import async_playwright

class GitHubBrowserScanner:
    async def start(self):
        self.session = self.bb.sessions.create(project_id=BB_PROJECT_ID)
        debug = self.bb.sessions.debug(self.session.id)
        self.pw = await async_playwright().__aenter__()
        self.browser = await self.pw.chromium.connect_over_cdp(debug.ws_url)

    async def search_repos(self, keyword: str, max_repos: int = 5) -> list[Repo]:
        url = f"https://github.com/search?q={keyword}&type=repositories&s=stars&o=desc"
        await self.page.goto(url)
        await asyncio.sleep(4)

        repos = []
        links = await self.page.query_selector_all("div[data-testid='results-list'] a[href*='/']")
        for link in links:
            href = (await link.get_attribute("href") or "").strip()
            if "?" in href or "%" in href:
                continue
            parts = href.strip("/").split("/")
            if len(parts) == 2 and parts[0] not in SKIP_OWNERS:
                repos.append(Repo(name=f"{parts[0]}/{parts[1]}",
                                  url=f"https://github.com/{parts[0]}/{parts[1]}"))
        return repos[:max_repos]
```

**[📸 SCREENSHOT 2: Live scan log — repos found, contributors being fetched in real time]**

---

## Step 2 — Get contributors via REST API

The `/contributors` page on GitHub is flaky to scrape — the DOM changes constantly. The REST API is reliable and returns commit counts:

```python
@staticmethod
def _fetch_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "GitHubRadar/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())

async def get_contributors(self, repo: Repo, max_contributors: int = 8):
    # Primary: GitHub REST API — reliable, returns commit counts
    api_data = self._fetch_json(
        f"https://api.github.com/repos/{repo.name}/contributors?per_page={max_contributors}"
    )
    contributors = []
    for item in api_data[:max_contributors]:
        contributors.append(Contributor(
            username=item["login"],
            commits=item["contributions"],
            profile_url=f"https://github.com/{item['login']}",
        ))
    return contributors
```

No auth token needed for public repos. Rate limit is 60 requests/hour unauthenticated — enough for a standard scan.

---

## Step 3 — Mine emails from commits

This is the core. GitHub's Events API exposes `PushEvent` payloads with commit author emails — including emails that aren't shown on the profile page:

```python
@staticmethod
def crawl_public_emails(username: str) -> list[str]:
    emails = set()
    NOREPLY = {"noreply@github.com", "users.noreply.github.com"}

    # Source 1: Events API — PushEvent payloads contain commit author emails
    events = GitHubBrowserScanner._fetch_json(
        f"https://api.github.com/users/{username}/events/public"
    )
    for event in events:
        if event.get("type") == "PushEvent":
            for commit in event["payload"].get("commits", []):
                email = commit.get("author", {}).get("email", "")
                if email and not any(nr in email for nr in NOREPLY):
                    emails.add(email.lower())

    # Source 2: Commit patch files — "Author: Name <email>" header
    repos_data = GitHubBrowserScanner._fetch_json(
        f"https://api.github.com/users/{username}/repos?sort=pushed&per_page=10&type=owner"
    )
    owned = [r for r in repos_data if not r.get("fork", False)]

    for repo in owned[:6]:
        if emails:
            break  # Stop once we have one — avoid rate limits
        commits = GitHubBrowserScanner._fetch_json(
            f"https://api.github.com/repos/{repo['full_name']}/commits"
            f"?author={username}&per_page=5"
        )
        for commit_item in commits[:5]:
            sha = commit_item.get("sha", "")
            # Try git commit API first (faster)
            detail = GitHubBrowserScanner._fetch_json(
                f"https://api.github.com/repos/{repo['full_name']}/git/commits/{sha}"
            )
            email = (detail.get("author") or {}).get("email", "")
            if email and not any(nr in email for nr in NOREPLY):
                emails.add(email.lower())

    return sorted(emails)
```

**What actually happens:** contributors who haven't enabled "Keep my email private" in GitHub settings will have their commit email exposed in these two places. Security researchers often do hide it — but plenty don't.

**[📸 SCREENSHOT 3: Contributor table — usernames, companies, scores, emails found]**

---

## Step 4 — Score with gpt-4o-mini

Each contributor profile (name, company, bio, pinned repos, orgs) gets scored:

```python
def score_contributor(self, contributor: Contributor, keyword: str) -> dict:
    profile_text = f"""
Username: {contributor.username}
Company: {contributor.company}
Bio: {contributor.bio}
Pinned repos: {', '.join(contributor.pinned_repos)}
Commits: {contributor.commits}
"""
    prompt = f"""Analyze this GitHub contributor in the {keyword} space.
Return JSON only:
{{
  "activity_score": <0-100>,
  "tier": "core" | "active" | "emerging",
  "summary": "<1 sentence>",
  "interesting": true/false
}}
{profile_text}"""

    resp = self.client.chat.completions.create(
        model="gpt-4o-mini", max_tokens=200,
        messages=[{"role": "user", "content": prompt}]
    )
    return json.loads(resp.choices[0].message.content)
```

Cost per full scan: ~$0.002. Negligible.

---

## Step 5 — Stream everything with SSE

The scan takes 2-3 minutes. Users need to see what's happening. Flask + Server-Sent Events:

```python
# app.py
@app.route("/api/github/stream")
def github_stream():
    keyword = request.args.get("keyword")
    q = queue.Queue()

    def yield_event(type_, message, data=None):
        q.put(json.dumps({"type": type_, "message": message, "data": data or {}}))

    def run_crawler():
        loop = asyncio.new_event_loop()
        agent = GitHubRadarAgent(keyword=keyword)
        loop.run_until_complete(agent.run(yield_event=yield_event))
        q.put(None)

    threading.Thread(target=run_crawler, daemon=True).start()

    def generate():
        while True:
            item = q.get()
            if item is None: break
            yield f"data: {item}\n\n"

    return Response(generate(), mimetype="text/event-stream")
```

The frontend connects via `EventSource` and updates the UI in real time — agent chips light up as each phase completes.

---

## Who you can actually reach with this

This isn't for cold emailing Fortune 500 CTOs. Here's what GitHub Radar is actually good for:

**✅ Technical co-founders and CTOs at dev-tool startups**  
The person who wrote the first 500 commits on a repo *is* the CTO. They're reachable, technical, and their LinkedIn inbox isn't as saturated as you'd think. Scan `snyk`, `semgrep`, `nuclei` — the core tier contributors are ex-founders, current CTOs, and VPs of Engineering.

**✅ Security researchers at mid-size companies**  
People contributing to OWASP ZAP, Burp Suite extensions, Trivy — they work somewhere with a security budget. GitHub bio often lists the company directly.

**✅ DevOps / cloud infra builders**  
Lower privacy paranoia than pure security folks. Scan `falco`, `osquery`, `wazuh` — email hit rate is 30%+.

**✅ AI/ML tooling contributors**  
LangChain, Hugging Face, vLLM contributors are extremely reachable — they're builders who love engaging with other builders.

**❌ Enterprise CTOs at Google/Microsoft** — they stopped pushing commits years ago  
**❌ Non-technical CEOs** — not on GitHub  
**❌ Anyone who's enabled GitHub's "Keep email private" setting** — mostly security researchers

**The ICP this finds best:** technical founder or CTO at a 10-100 person B2B SaaS company, actively shipping code, no EA blocking their inbox.

---

## What failed, what we fixed

Building this took more debugging than expected. Here's the honest failure log:

### Bug 1 — Junk repos slipping through
**What happened:** Searching "OWASP" returned `contact/report-abuse?report=aboutcode-org%2Fvulnerablecode` as a "repo". The link parser was too loose.  
**Fix:** Reject any href containing `?`, `#`, or `%`. Added regex validation on owner and repo name. Expanded the `SKIP_OWNERS` blocklist to include `contact`, `about`, `login`.

### Bug 2 — Only 1 contributor per repo
**What happened:** The browser scraper targeting `a[data-hovercard-type='user']` on the `/contributors` page consistently returned 1 result. GitHub had changed the DOM.  
**Fix:** Switched to the GitHub REST API as primary source (`/repos/{owner}/{repo}/contributors`). Reliable, returns commit counts, no DOM dependency. Browser scrape kept as fallback only.

### Bug 3 — 0 emails on security repos
**What happened:** OWASP, Nuclei, Snyk contributors — 0 emails found. Events API returned `noreply` addresses for everyone.  
**Root cause:** Security researchers specifically enable "Keep my email private" in GitHub settings. They know exactly how commit email exposure works.  
**What we tried:**
- Personal website scraping via Firecrawl — most devs don't list emails on personal sites. Near-zero return.
- Stack Overflow profile lookup — SO usernames rarely match GitHub handles. ~5% hit rate.
- DuckDuckGo search via Firecrawl — DDG blocks scrapers aggressively. Too many false positives.  

**What actually works:** Scan adjacent repos where privacy paranoia is lower (DevOps tooling, cloud infra, AI/ML) and personal repos (non-fork owned repos have higher email exposure than org repos).

### Bug 4 — Browserbase 429 (concurrent session limit)
**What happened:** A scan crashed mid-run, leaving a zombie Browserbase session open. Next scan hit the free plan's limit of 1 concurrent session.  
**Fix:** Added explicit session cleanup in the `stop()` method with `REQUEST_RELEASE` status. The UI now shows a clear error instead of hanging.

### Bug 5 — Missing descriptions on repo cards
**What happened:** All repo cards showed "—" for description. GitHub's DOM had changed.  
**Fix:** Added 4 fallback selectors + `meta[name='description']` as a last resort.

---

---

## Deploy to Hugging Face Spaces

```dockerfile
FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    wget gnupg2 libnss3 libatk-bridge2.0-0 libdrm2 \
    libxcomposite1 libxdamage1 libgbm1 libasound2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium --with-deps
COPY . .

EXPOSE 7860
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:7860", "--timeout", "180"]
```

Set secrets in Space Settings: `BROWSERBASE_API_KEY`, `BROWSERBASE_PROJECT_ID`, `OPENAI_API_KEY`.

**[📸 SCREENSHOT 4: HF Space running — green "Running" badge + scan in progress]**

---

## Results from real scans

| Keyword | Repos scanned | Contributors found | Emails found |
|---|---|---|---|
| snyk | 5 | 32 | 4 |
| nuclei | 5 | 28 | 2 |
| semgrep | 5 | 35 | 7 |
| OWASP ZAP | 5 | 30 | 5 |

Hit rate: ~15-20% for security repos. Higher for adjacent tooling (DevOps, cloud infra) where privacy paranoia is lower.

---

## What's next

**1. GitHub token support** — authenticated requests get 5,000 req/hour vs 60. Would unlock deeper commit history mining.

**2. CSV export** — one-click download of the contributor table with emails, scores, companies.

**3. More niches** — same tool works for DevOps, cloud infra, AI/ML tooling. Just swap the keyword.

---

## TL;DR

| Component | What it does |
|---|---|
| Browserbase + Playwright | Scrapes GitHub search results (JS-rendered) |
| GitHub REST API | Gets contributors by commit count |
| GitHub Events API | Mines emails from PushEvent commit payloads |
| Commit patch files | Extracts `Author: name <email>` headers |
| gpt-4o-mini | Scores contributors: core / active / emerging |
| Flask + SSE | Streams live scan updates to the UI |
| Hugging Face Spaces | Free Docker hosting |

Live → [huggingface.co/spaces/chaitubatman/github-radar](https://huggingface.co/spaces/chaitubatman/github-radar)  
Code → [github.com/chaitanyagatreddi/github-radar](https://github.com/chaitanyagatreddi/github-radar)

---

*Built with Browserbase, Playwright, Flask, gpt-4o-mini. Deployed on Hugging Face Spaces.*

---

If you're building something similar or want the repo walkthrough, drop **RADAR** in the comments and I'll send you the link.

Follow for more builds — OSINT tools, AI agents, and GTM automation shipped fast.
