#!/usr/bin/env python3
"""
Multi-agent RFC reviewer.

Usage:
    export ANTHROPIC_API_KEY=...
    export CONFLUENCE_BASE_URL=https://yourcompany.atlassian.net/wiki
    export CONFLUENCE_EMAIL=you@company.com
    export CONFLUENCE_API_TOKEN=...

    python review_rfc.py --page-id 123456789
    python review_rfc.py --page-id 123456789 --post-comment   # writes result back to Confluence
"""

import argparse
import concurrent.futures
import json
import os
import re
import sys

import requests
import yaml
from anthropic import Anthropic
from bs4 import BeautifulSoup, NavigableString

MODEL = "claude-sonnet-4-6"
SPECIALISTS_CONFIG = os.path.join(os.path.dirname(__file__), "specialists.yaml")

# ---------------------------------------------------------------------------
# 1. Confluence ingestion
# ---------------------------------------------------------------------------

rfc = """
# ADR-023: Adopt Auth0 as Strategic Identity Provider

## Status

Accepted

## Date

2026-06-19

## Context

The platform currently provides authentication and authorization capabilities through a combination of internally developed services and custom partner integrations.

As the number of external partners increases, several challenges have emerged:

- Security reviews are required for most authentication-related changes.
- Onboarding new partners requires significant engineering effort.
- Support for federated identity providers varies between integrations.
- Operational ownership of authentication infrastructure is creating increasing platform team overhead.

The organisation expects significant growth in external integrations over the next three years and requires a scalable approach to identity management.

## Decision

Auth0 will be adopted as the strategic identity platform for:

- Partner authentication
- Customer authentication
- Federated identity integrations
- OAuth2 and OpenID Connect support

New integrations will use Auth0 by default.

Existing integrations will be migrated incrementally where there is a clear business benefit.

## Options Considered

### Option 1: Continue with Existing Internal Platform

#### Advantages

- No additional licensing costs.
- Existing engineering familiarity.
- Full control over implementation.

#### Disadvantages

- Significant operational ownership.
- Increased security and compliance burden.
- Slow delivery of new identity features.
- Requires ongoing investment to remain secure and compliant.

### Option 2: Self-Hosted Keycloak

#### Advantages

- Open source.
- Avoids commercial licensing costs.
- Supports OAuth2, OIDC and SAML.

#### Disadvantages

- Requires platform ownership and maintenance.
- Upgrades and security patching remain internal responsibilities.
- Additional operational complexity.

### Option 3: Auth0

#### Advantages

- Managed service.
- Enterprise federation support.
- Strong ecosystem and documentation.
- Reduced operational ownership.
- Built-in support for modern identity standards.

#### Disadvantages

- Vendor dependency.
- Commercial licensing costs.
- Potential migration effort.

## Consequences

### Positive

- Reduced operational burden on platform teams.
- Faster onboarding of new partners.
- Improved consistency across integrations.
- Reduced security implementation risk.
- Access to enterprise identity features without bespoke development.

### Negative

- Increased vendor dependency.
- Annual licensing commitment.
- Migration effort for existing integrations.
- Platform engineers require Auth0-specific knowledge.

## Risks

| Risk | Mitigation |
|--------|------------|
| Vendor lock-in | Use standard OAuth2 and OIDC protocols where possible |
| Licensing costs increase | Conduct annual supplier review |
| Migration complexity | Migrate incrementally and prioritise new integrations |
| Service outage | Ensure platform degrades gracefully and review Auth0 resilience guarantees |

## Success Measures

- Partner onboarding time reduced by 50%.
- No new custom authentication implementations created.
- Authentication-related operational incidents reduced year-on-year.
- Security audit findings related to identity management reduced.

## Review Date

2027-06-19
"""


def fetch_rfc(page_id: str) -> dict:
    # base = os.environ["CONFLUENCE_BASE_URL"].rstrip("/")
    # auth = (os.environ["CONFLUENCE_EMAIL"], os.environ["CONFLUENCE_API_TOKEN"])
    # resp = requests.get(
    #     f"{base}/rest/api/content/{page_id}",
    #     params={"expand": "body.storage,version"},
    #     auth=auth,
    #     timeout=30,
    # )
    # resp.raise_for_status()
    # data = resp.json()
    # html = data["body"]["storage"]["value"]
    # text = html_to_text(html)
    return {"title": "ADR-023: Adopt Auth0 as Strategic Identity Provider"
, "text": rfc, "version": "v1"}


def html_to_text(html: str) -> str:
    """
    Convert Confluence storage-format XHTML to markdown, preserving tables
    (which is where most RFC trade-off / cost data lives) and common
    Confluence macros (code blocks, info/warning/note panels, expand sections).
    """
    soup = BeautifulSoup(html, "html.parser")
    _unwrap_confluence_macros(soup)
    text = _node_to_md(soup).strip()
    return re.sub(r"\n{3,}", "\n\n", text)


def _unwrap_confluence_macros(soup: BeautifulSoup):
    """Turn ac:structured-macro blocks into something the markdown walker understands."""
    for macro in soup.find_all("ac:structured-macro"):
        name = macro.get("ac:name", "")
        body = macro.find("ac:plain-text-body") or macro.find("ac:rich-text-body")
        content = body.get_text("\n") if body else macro.get_text("\n")

        if name == "code":
            lang_param = macro.find("ac:parameter", attrs={"ac:name": "language"})
            lang = lang_param.get_text() if lang_param else ""
            replacement = soup.new_tag("pre")
            replacement.string = f"```{lang}\n{content.strip()}\n```"
        elif name in ("info", "note", "warning", "tip"):
            replacement = soup.new_tag("p")
            replacement.string = f"[{name.upper()}] {content.strip()}"
        elif name == "expand":
            title_param = macro.find("ac:parameter", attrs={"ac:name": "title"})
            title = title_param.get_text() if title_param else "Details"
            replacement = soup.new_tag("p")
            replacement.string = f"[{title}] {content.strip()}"
        else:
            # Unknown macro: keep its text content, drop the wrapper
            replacement = soup.new_tag("p")
            replacement.string = content.strip()

        macro.replace_with(replacement)


def _node_to_md(node) -> str:
    if isinstance(node, NavigableString):
        return str(node)

    if not hasattr(node, "children"):
        return ""

    name = getattr(node, "name", None)

    if name in ("html", "[document]", "body", None):
        return "".join(_node_to_md(c) for c in node.children)

    if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
        level = int(name[1])
        text = node.get_text(strip=True)
        return f"\n\n{'#' * level} {text}\n\n"

    if name == "p":
        inner = "".join(_node_to_md(c) for c in node.children).strip()
        return f"\n\n{inner}\n\n" if inner else ""

    if name == "br":
        return "\n"

    if name in ("strong", "b"):
        return f"**{node.get_text(strip=True)}**"

    if name in ("em", "i"):
        return f"*{node.get_text(strip=True)}*"

    if name == "a":
        text = node.get_text(strip=True)
        href = node.get("href", "")
        return f"[{text}]({href})" if href else text

    if name == "pre":
        return f"\n\n{node.get_text()}\n\n"

    if name in ("ul", "ol"):
        items = []
        for i, li in enumerate(node.find_all("li", recursive=False), 1):
            prefix = "-" if name == "ul" else f"{i}."
            items.append(f"{prefix} {li.get_text(strip=True)}")
        return "\n\n" + "\n".join(items) + "\n\n"

    if name == "table":
        return _table_to_md(node)

    # Fallback: recurse into children
    return "".join(_node_to_md(c) for c in node.children)


def _table_to_md(table) -> str:
    rows = table.find_all("tr")
    if not rows:
        return ""

    def row_cells(row):
        return [c.get_text(" ", strip=True).replace("|", "\\|") for c in row.find_all(["th", "td"])]

    md_rows = [row_cells(r) for r in rows]
    if not md_rows:
        return ""

    width = max(len(r) for r in md_rows)
    md_rows = [r + [""] * (width - len(r)) for r in md_rows]

    out = ["\n"]
    out.append("| " + " | ".join(md_rows[0]) + " |")
    out.append("| " + " | ".join(["---"] * width) + " |")
    for r in md_rows[1:]:
        out.append("| " + " | ".join(r) + " |")
    out.append("\n")
    return "\n".join(out)


def post_comment(page_id: str, markdown_body: str):
    base = os.environ["CONFLUENCE_BASE_URL"].rstrip("/")
    # auth = (os.environ["CONFLUENCE_EMAIL"], os.environ["CONFLUENCE_API_TOKEN"])
    auth = ("email", "token")
    # Confluence comments take storage-format HTML, not markdown. Minimal conversion:
    html_body = markdown_to_storage_html(markdown_body)
    resp = requests.post(
        f"{base}/rest/api/content",
        auth=auth,
        json={
            "type": "comment",
            "container": {"id": page_id, "type": "page"},
            "body": {"storage": {"value": html_body, "representation": "storage"}},
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def markdown_to_storage_html(md: str) -> str:
    # Minimal: wrap paragraphs and bold markers. Replace with a real MD->Confluence
    # converter later if you want headings/tables to render properly.
    html = md.replace("\n\n", "</p><p>")
    html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html)
    return f"<p>{html}</p>"


# ---------------------------------------------------------------------------
# 2. Specialist agents
# ---------------------------------------------------------------------------

def load_specialists() -> list[dict]:
    with open(SPECIALISTS_CONFIG) as f:
        config = yaml.safe_load(f)
    return config["specialists"]


SEVERITIES = ["blocking", "major", "minor", "nit"]

OUTPUT_SCHEMA_INSTRUCTIONS = """
Respond with ONLY valid JSON, no markdown fences, no preamble, matching this schema:
{
  "summary": "one sentence overall take from this lens",
  "findings": [
    {
      "severity": "blocking | major | minor | nit",
      "section": "which part of the RFC this refers to",
      "comment": "the finding, written for the RFC author",
      "question": "an optional clarifying question, or null"
    }
  ]
}
If there are no findings, return an empty findings array.
"""


def classify_relevant_specialists(client: Anthropic, rfc_text: str, specialists: list[dict]) -> list[dict]:
    """
    One cheap call that decides which specialists actually apply to this RFC,
    instead of fanning out to every agent every time. Returns the decision
    (with reasoning) for every specialist, applicable or not, so the caller
    has an audit trail of what was skipped and why.
    """
    catalogue = [{"name": s["name"], "trigger": s["trigger"]} for s in specialists]
    system = """You are triaging an engineering RFC to decide which specialist reviewers should
look at it. You will be given a list of specialists with the condition under which each applies,
and the RFC content. For each specialist, decide whether it applies to THIS RFC.
Be conservative about including a specialist — only mark it relevant if its trigger condition is
clearly met by the content. A specialist whose trigger says it always applies should always be
marked relevant.

Respond with ONLY valid JSON, no markdown fences, no preamble:
{
  "decisions": [
    {"name": "<specialist name>", "relevant": true|false, "reason": "one sentence"}
  ]
}
"""
    msg = client.messages.create(
        model=MODEL,
        max_tokens=1000,
        system=system,
        messages=[{
            "role": "user",
            "content": f"Specialists:\n{json.dumps(catalogue, indent=2)}\n\nRFC content:\n\n{rfc_text}",
        }],
    )
    raw = re.sub(r"^```json|```$", "", msg.content[0].text.strip(), flags=re.MULTILINE).strip()
    try:
        decisions = json.loads(raw)["decisions"]
        print(decisions)
    except (json.JSONDecodeError, KeyError):
        # Fail open: if classification breaks, run everything rather than silently skipping review.
        return [{"name": s["name"], "relevant": True, "reason": "classifier error — running by default"}
                 for s in specialists]
    return decisions


def run_specialist(client: Anthropic, spec: dict, rfc_text: str) -> dict:
    msg = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=spec["system_prompt"] + OUTPUT_SCHEMA_INSTRUCTIONS,
        messages=[{"role": "user", "content": f"RFC content:\n\n{rfc_text}"}],
    )
    raw = msg.content[0].text.strip()
    raw = re.sub(r"^```json|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = {"summary": "PARSE ERROR", "findings": [], "raw": raw}
    parsed["lens"] = spec["name"]
    return parsed


def run_all_specialists(rfc_text: str, force_all: bool = False) -> tuple[list[dict], list[dict]]:
    """Returns (specialist_results, classification_decisions)."""
    client = Anthropic()
    specialists = load_specialists()

    if force_all:
        decisions = [{"name": s["name"], "relevant": True, "reason": "--all flag"} for s in specialists]
    else:
        decisions = classify_relevant_specialists(client, rfc_text, specialists)

    relevant_names = {d["name"] for d in decisions if d.get("relevant")}
    to_run = [s for s in specialists if s["name"] in relevant_names]

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(len(to_run), 1)) as pool:
        futures = {pool.submit(run_specialist, client, s, rfc_text): s["name"] for s in to_run}
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())

    order = {s["name"]: i for i, s in enumerate(specialists)}
    results.sort(key=lambda r: order.get(r["lens"], 99))
    return results, decisions


# ---------------------------------------------------------------------------
# 3. Synthesis
# ---------------------------------------------------------------------------

def synthesize(rfc_title: str, specialist_results: list[dict], skipped: list[dict]) -> str:
    client = Anthropic()
    payload = json.dumps(specialist_results, indent=2)
    skipped_note = (
        "\n\nThe following lenses were judged not relevant to this RFC and did NOT run "
        f"(list them at the end under 'Not reviewed', do not invent findings for them):\n"
        f"{json.dumps(skipped, indent=2)}" if skipped else ""
    )
    system = """You are synthesizing multiple specialist reviews of an engineering RFC into a single,
readable review for the RFC author. Group by severity (blocking first), not by lens. Deduplicate
overlapping findings across lenses. Be direct and concise. Use markdown. Start with a one-paragraph
overall verdict (approve / approve with changes / needs rework), then a 'Blocking' section, then
'Major', then 'Minor / nits'. Note which lens(es) raised each point in parentheses. End with a
'Not reviewed' section listing any lenses that were skipped and why, if applicable."""
    msg = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=system,
        messages=[{
            "role": "user",
            "content": f"RFC title: {rfc_title}\n\nSpecialist findings (JSON):\n{payload}{skipped_note}",
        }],
    )
    return msg.content[0].text.strip()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--page-id", required=True, help="Confluence page ID for the RFC")
    parser.add_argument("--post-comment", action="store_true", help="Post the review back to Confluence")
    parser.add_argument("--save", help="Path to save the review markdown locally")
    parser.add_argument("--all", action="store_true", help="Run every specialist regardless of relevance")
    args = parser.parse_args()

    print(f"Fetching RFC page {args.page_id}...", file=sys.stderr)
    rfc = fetch_rfc(args.page_id)

    print("Classifying which specialists apply...", file=sys.stderr)
    specialist_results, decisions = run_all_specialists(rfc["text"], force_all=args.all)

    ran = [d["name"] for d in decisions if d.get("relevant")]
    skipped = [d for d in decisions if not d.get("relevant")]
    print(f"Running: {', '.join(ran) or 'none'}", file=sys.stderr)
    for d in skipped:
        print(f"Skipped {d['name']}: {d.get('reason', '')}", file=sys.stderr)

    print("Synthesizing...", file=sys.stderr)
    review = synthesize(rfc["title"], specialist_results, skipped)

    print("\n" + "=" * 80)
    print(f"REVIEW: {rfc['title']}")
    print("=" * 80 + "\n")
    print(review)

    if args.save:
        with open(args.save, "w") as f:
            f.write(review)
        print(f"\nSaved to {args.save}", file=sys.stderr)

    if args.post_comment:
        print("Posting comment to Confluence...", file=sys.stderr)
        post_comment(args.page_id, review)
        print("Posted.", file=sys.stderr)


if __name__ == "__main__":
    main()
