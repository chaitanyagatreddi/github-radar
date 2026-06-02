#!/usr/bin/env python3
"""
GitHub Radar — Standalone Web App
===================================
Cybersecurity contributor crawler with Browserbase + gpt-4o-mini analysis.
Crawls repos, profiles, and publicly available emails.
"""

import asyncio
import json
import os
import sys
import queue
import threading

try:
    from flask import Flask, render_template_string, request, Response
except ImportError:
    print("pip3 install flask")
    sys.exit(1)

app = Flask(__name__)

GITHUB_RADAR_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GitHub Radar | Cybersecurity Contributors</title>
<meta name="description" content="Find cybersecurity contributors worth knowing — scans GitHub for security tool repos, maps top contributors, scores them with gpt-4o-mini.">

<!-- Open Graph -->
<meta property="og:type" content="website">
<meta property="og:title" content="GitHub Radar — Cybersecurity contributor radar">
<meta property="og:description" content="Find cybersecurity contributors worth knowing.">
<meta property="og:site_name" content="GitHub Radar">
<meta property="og:image" content="/og.png">

<!-- Twitter / X -->
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="GitHub Radar — Cybersecurity contributor radar">
<meta name="twitter:description" content="Find cybersecurity contributors worth knowing.">
<meta name="twitter:image" content="/og.png">

<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', monospace;
         background: #0d1117; color: #e6edf3; min-height: 100vh; padding: 24px; }
  .header { display: flex; align-items: center; gap: 12px; margin-bottom: 28px; }
  .header h1 { font-size: 20px; font-weight: 700; color: #f0f6fc; }
  .header .badge { background: #21262d; border: 1px solid #30363d;
                   padding: 3px 10px; border-radius: 20px; font-size: 11px; color: #8b949e; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px;
          padding: 20px; margin-bottom: 16px; }
  .form-row { display: flex; gap: 12px; flex-wrap: wrap; align-items: flex-end; }
  input, select { background: #0d1117; border: 1px solid #30363d; border-radius: 6px;
                  color: #e6edf3; padding: 9px 14px; font-size: 14px; outline: none; }
  input:focus, select:focus { border-color: #58a6ff; }
  input[type=text] { flex: 1; min-width: 220px; }
  button { background: #238636; color: #fff; border: none; border-radius: 6px;
           padding: 9px 20px; font-size: 14px; font-weight: 600; cursor: pointer;
           white-space: nowrap; }
  button:hover { background: #2ea043; }
  button:disabled { background: #21262d; color: #484f58; cursor: not-allowed; }
  .agents { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
  .agent-chip { background: #21262d; border: 1px solid #30363d; border-radius: 20px;
                padding: 4px 12px; font-size: 12px; display: flex; align-items: center; gap: 6px; }
  .agent-chip.active { border-color: #58a6ff; color: #58a6ff; }
  .agent-chip.done   { border-color: #3fb950; color: #3fb950; }
  .log { background: #0d1117; border: 1px solid #21262d; border-radius: 6px;
         padding: 12px; font-size: 12px; font-family: monospace; max-height: 220px;
         overflow-y: auto; margin-top: 12px; }
  .log-line { padding: 2px 0; border-bottom: 1px solid #161b22; color: #8b949e; }
  .log-line.highlight { color: #e6edf3; }
  .repos-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px; }
  .repo-card { background: #0d1117; border: 1px solid #30363d; border-radius: 6px; padding: 14px; }
  .repo-name { font-weight: 600; color: #58a6ff; font-size: 14px; margin-bottom: 6px; }
  .repo-desc { font-size: 12px; color: #8b949e; margin-bottom: 8px; line-height: 1.5; }
  .repo-meta { display: flex; gap: 10px; font-size: 11px; color: #8b949e; }
  .star { color: #d29922; }
  .contributors-table { width: 100%; border-collapse: collapse; font-size: 13px; }
  .contributors-table th { text-align: left; padding: 10px 12px;
                            border-bottom: 1px solid #30363d; color: #8b949e; font-weight: 600; font-size: 11px; text-transform: uppercase; }
  .contributors-table td { padding: 10px 12px; border-bottom: 1px solid #21262d; vertical-align: top; }
  .contributors-table tr:hover td { background: #161b22; }
  .username { font-weight: 600; color: #58a6ff; }
  .tier { display: inline-block; padding: 2px 8px; border-radius: 20px; font-size: 11px; font-weight: 600; }
  .tier-core     { background: rgba(63,185,80,0.15); color: #3fb950; }
  .tier-active   { background: rgba(88,166,255,0.15); color: #58a6ff; }
  .tier-emerging { background: rgba(210,153,34,0.15); color: #d29922; }
  .score-bar { width: 60px; height: 6px; background: #21262d; border-radius: 3px; margin-top: 4px; }
  .score-fill { height: 100%; border-radius: 3px; background: #58a6ff; }
  .hidden { display: none; }
  label { font-size: 12px; color: #8b949e; display: block; margin-bottom: 4px; }
  .tool-chip { background: #21262d; border: 1px solid #30363d; border-radius: 20px;
               padding: 4px 12px; font-size: 12px; color: #8b949e; cursor: pointer;
               transition: all .15s; user-select: none; }
  .tool-chip:hover { border-color: #58a6ff; color: #58a6ff; background: rgba(88,166,255,0.08); }
  .tool-chip.active { border-color: #3fb950; color: #3fb950; background: rgba(63,185,80,0.1); }
  .source-chip { background: #21262d; border: 1px solid #30363d; border-radius: 20px;
                 padding: 5px 14px; font-size: 12px; color: #8b949e; cursor: pointer;
                 transition: all .15s; user-select: none; position: relative; }
  .source-chip.enabled  { border-color: #3fb950; color: #3fb950; background: rgba(63,185,80,0.1); }
  .source-chip.disabled { border-color: #30363d; color: #484f58; background: #161b22; text-decoration: line-through; }
  .source-chip.running  { border-color: #58a6ff; color: #58a6ff; background: rgba(88,166,255,0.1); }
  .source-chip.found    { border-color: #d29922; color: #d29922; background: rgba(210,153,34,0.1); }
</style>
</head>
<body>
<div class="header">
  <h1>🛡️ GitHub Radar</h1>
  <span class="badge">Cybersecurity Contributors</span>
</div>

<div class="card">
  <div style="margin-bottom:12px">
    <label style="margin-bottom:6px">Popular tools</label>
    <div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:4px">
      <span class="tool-chip" onclick="setKeyword('OWASP ZAP')">OWASP ZAP</span>
      <span class="tool-chip" onclick="setKeyword('nuclei')">Nuclei</span>
      <span class="tool-chip" onclick="setKeyword('metasploit')">Metasploit</span>
      <span class="tool-chip" onclick="setKeyword('nmap')">Nmap</span>
      <span class="tool-chip" onclick="setKeyword('burp suite')">Burp Suite</span>
      <span class="tool-chip" onclick="setKeyword('trivy')">Trivy</span>
      <span class="tool-chip" onclick="setKeyword('falco')">Falco</span>
      <span class="tool-chip" onclick="setKeyword('osquery')">osquery</span>
      <span class="tool-chip" onclick="setKeyword('semgrep')">Semgrep</span>
      <span class="tool-chip" onclick="setKeyword('snyk')">Snyk</span>
      <span class="tool-chip" onclick="setKeyword('wazuh SIEM')">Wazuh</span>
      <span class="tool-chip" onclick="setKeyword('openvas vulnerability scanner')">OpenVAS</span>
      <span class="tool-chip" onclick="setKeyword('wireshark')">Wireshark</span>
      <span class="tool-chip" onclick="setKeyword('suricata IDS')">Suricata</span>
      <span class="tool-chip" onclick="setKeyword('mimikatz')">Mimikatz</span>
      <span class="tool-chip" onclick="setKeyword('gobuster')">Gobuster</span>
      <span class="tool-chip" onclick="setKeyword('sqlmap')">sqlmap</span>
      <span class="tool-chip" onclick="setKeyword('hashcat')">Hashcat</span>
    </div>
  </div>
  <div class="form-row">
    <div style="flex:1; min-width:220px">
      <label>Keyword / tool name</label>
      <input type="text" id="keyword" placeholder="OWASP ZAP, nuclei, SIEM..." value="OWASP ZAP" />
    </div>
    <div>
      <label>Repos to scan</label>
      <select id="maxRepos">
        <option value="3">3 repos</option>
        <option value="5" selected>5 repos</option>
        <option value="8">8 repos</option>
      </select>
    </div>
    <div>
      <label>Contributors / repo</label>
      <select id="maxContributors">
        <option value="5">5</option>
        <option value="8" selected>8</option>
        <option value="12">12</option>
      </select>
    </div>
    <button id="scanBtn" onclick="startScan()">🔍 Scan GitHub</button>
  </div>

  <div style="margin-top:14px">
    <div style="font-size:11px;color:#484f58;text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px">Pipeline</div>
    <div class="agents" id="agents">
      <div class="agent-chip" id="chip-browser">🌐 Browser</div>
      <div class="agent-chip" id="chip-search">🔍 Repos</div>
      <div class="agent-chip" id="chip-contributors">👥 Contributors</div>
      <div class="agent-chip" id="chip-profiles">👤 Profiles</div>
      <div class="agent-chip" id="chip-analysis">🤖 Analysis</div>
    </div>
  </div>

  <div style="margin-top:12px">
    <div style="font-size:11px;color:#484f58;text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px">Email Sources</div>
    <div style="display:flex;flex-wrap:wrap;gap:8px">
      <div class="source-chip enabled" id="src-github" title="GitHub events API + commit patches">📧 GitHub Commits</div>
    </div>
  </div>

  <div class="log" id="log" style="margin-top:14px"><div class="log-line">Ready. Enter a keyword and click Scan.</div></div>
</div>

<div class="card hidden" id="reposSection">
  <h3 style="font-size:13px; color:#8b949e; text-transform:uppercase; letter-spacing:.05em; margin-bottom:12px">
    📦 Top Repos
  </h3>
  <div class="repos-grid" id="reposGrid"></div>
</div>

<div class="card hidden" id="contributorsSection">
  <h3 style="font-size:13px; color:#8b949e; text-transform:uppercase; letter-spacing:.05em; margin-bottom:12px">
    👥 Top Contributors
  </h3>
  <table class="contributors-table">
    <thead>
      <tr>
        <th>Contributor</th>
        <th>Tier</th>
        <th>Score</th>
        <th>Email</th>
        <th>Summary</th>
        <th>Repos</th>
      </tr>
    </thead>
    <tbody id="contributorsBody"></tbody>
  </table>
</div>

<script>
function setKeyword(kw) {
  document.getElementById('keyword').value = kw;
  document.querySelectorAll('.tool-chip').forEach(c => c.classList.remove('active'));
  event.target.classList.add('active');
}

function log(msg, highlight=false) {
  const el = document.getElementById('log');
  const line = document.createElement('div');
  line.className = 'log-line' + (highlight ? ' highlight' : '');
  line.textContent = new Date().toLocaleTimeString() + '  ' + msg;
  el.appendChild(line);
  el.scrollTop = el.scrollHeight;
}

function setChip(id, state) {
  const el = document.getElementById('chip-' + id);
  if (!el) return;
  el.className = 'agent-chip ' + state;
}

function setSrc(id, state) {
  const el = document.getElementById('src-' + id);
  if (!el) return;
  el.className = 'source-chip ' + state;
}

function getEnabledSources() {
  return 'github';
}

function startScan() {
  const keyword = document.getElementById('keyword').value.trim();
  if (!keyword) return;
  const maxRepos = document.getElementById('maxRepos').value;
  const maxContributors = document.getElementById('maxContributors').value;
  const sources = getEnabledSources();

  document.getElementById('scanBtn').disabled = true;
  document.getElementById('reposSection').classList.add('hidden');
  document.getElementById('contributorsSection').classList.add('hidden');
  document.getElementById('reposGrid').innerHTML = '';
  document.getElementById('contributorsBody').innerHTML = '';
  document.getElementById('log').innerHTML = '';

  ['browser','search','contributors','profiles','analysis'].forEach(c => setChip(c, ''));
  setSrc('github', 'enabled');

  log('Starting GitHub Radar scan for: ' + keyword, true);
  log('Email sources: ' + (sources || 'none'));

  const es = new EventSource('/api/github/stream?keyword=' + encodeURIComponent(keyword) +
    '&max_repos=' + maxRepos + '&max_contributors=' + maxContributors +
    '&sources=' + encodeURIComponent(sources));

  es.onmessage = function(e) {
    try {
      const msg = JSON.parse(e.data);
      const type = msg.type || '';
      const text = msg.message || '';
      const data = msg.data || {};

      log(text, ['repos_found','contributors_found','complete','email_found'].includes(type));

      if (type === 'agent' && text.includes('Browserbase')) setChip('browser', 'active');
      if (type === 'agent' && text.includes('connected'))   setChip('browser', 'done');
      if (type === 'scanning_repo')                          setChip('search', 'active');
      if (type === 'repos_found')                            setChip('search', 'done');
      if (type === 'contributors_found')                     setChip('contributors', 'active');
      if (type === 'profiling')                              { setChip('contributors', 'done'); setChip('profiles', 'active'); }
      if (type === 'crawling_email' && text.includes('[GitHub]')) setSrc('github', 'running');
      if (type === 'email_found'    && text.includes('[GitHub]')) setSrc('github', 'found');
      if (type === 'analyzing')   { setChip('profiles','done'); setChip('analysis', 'active'); }
      if (type === 'scored')       setChip('analysis', 'active');

      if (type === 'repo_detail' && data.repo) {
        document.getElementById('reposSection').classList.remove('hidden');
        var grid = document.getElementById('reposGrid');
        var card = document.createElement('div');
        card.className = 'repo-card';
        card.innerHTML = '<div class="repo-name"><a href="https://github.com/' + data.repo +
          '" target="_blank" style="color:inherit">' + data.repo + '</a></div>' +
          '<div class="repo-desc">' + (data.description || 'No description') + '</div>' +
          '<div class="repo-meta"><span class="star">Stars: ' + (data.stars||0).toLocaleString() + '</span></div>';
        grid.appendChild(card);
      }

      if (type === 'complete' && data.top_contributors) {
        setChip('analysis', 'done');
        setChip('profiles', 'done');
        document.getElementById('contributorsSection').classList.remove('hidden');
        var tbody = document.getElementById('contributorsBody');
        tbody.innerHTML = '';
        data.top_contributors.forEach(function(c) {
          var tier = c.tier || 'active';
          var score = Math.min(100, c.activity_score || 0);
          var tr = document.createElement('tr');
          var emailCell = c.email ? '<a href="mailto:' + c.email + '" style="color:#58a6ff">' + c.email + '</a>' : '<span style="color:#484f58">none</span>';
          var companyRow = c.company ? '<br><small style="color:#8b949e">' + c.company + '</small>' : '';
          tr.innerHTML =
            '<td><a href="' + c.profile_url + '" target="_blank" class="username">@' + c.username + '</a>' + companyRow + '</td>' +
            '<td><span class="tier tier-' + tier + '">' + tier + '</span></td>' +
            '<td><div style="font-weight:600">' + score + '</div>' +
              '<div class="score-bar"><div class="score-fill" style="width:' + score + '%"></div></div></td>' +
            '<td style="font-size:12px">' + emailCell + '</td>' +
            '<td style="color:#8b949e;font-size:12px">' + (c.summary || c.bio || 'No summary') + '</td>' +
            '<td style="font-size:12px;color:#8b949e">' + (c.repos_contributed || []).join('<br>') + '</td>';
          tbody.appendChild(tr);
        });
        document.getElementById('scanBtn').disabled = false;
        es.close();
      }

      if (type === 'error') {
        log('ERROR: ' + text, true);
        document.getElementById('scanBtn').disabled = false;
        es.close();
      }
    } catch(err) { console.error(err); }
  };

  es.onerror = function() {
    log('Connection closed');
    document.getElementById('scanBtn').disabled = false;
    es.close();
  };
}
</script>
</body>
</html>"""


@app.route("/")
def index():
    return GITHUB_RADAR_HTML


@app.route("/og.png")
def og_image():
    """Serve the social share preview image."""
    from flask import send_from_directory
    return send_from_directory(os.path.dirname(__file__), "og.png")


@app.route("/api/github/stream")
def github_stream():
    keyword = request.args.get("keyword", "vulnerability scanner")
    max_repos = int(request.args.get("max_repos", 5))
    max_contributors = int(request.args.get("max_contributors", 8))
    sources_raw = request.args.get("sources", "github,website,stackoverflow,websearch")
    enabled_sources = set(s.strip() for s in sources_raw.split(",") if s.strip())

    q = queue.Queue()

    def yield_event(type_, message, data=None):
        payload = {"type": type_, "message": message, "data": data or {}}
        q.put(json.dumps(payload))

    def run_crawler():
        from github_crawler import GitHubRadarAgent
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            agent = GitHubRadarAgent(
                keyword=keyword,
                max_repos=max_repos,
                max_contributors=max_contributors,
                enabled_sources=enabled_sources,
            )
            loop.run_until_complete(agent.run(yield_event=yield_event))
        except Exception as e:
            yield_event("error", str(e))
        finally:
            q.put(None)
            loop.close()

    thread = threading.Thread(target=run_crawler, daemon=True)
    thread.start()

    def generate():
        while True:
            item = q.get()
            if item is None:
                break
            yield f"data: {item}\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    print(f"\n🛡️  GitHub Radar")
    print(f"   http://localhost:{port}")
    print(f"   Browserbase: {'✅' if os.environ.get('BROWSERBASE_API_KEY') else '❌'}")
    print(f"   OpenAI: {'✅' if os.environ.get('OPENAI_API_KEY') else '❌'}")
    app.run(host="0.0.0.0", port=port, debug=True)
