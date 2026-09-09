"""
Geopolitical / breaking-news risk heuristic.

Two scoring paths:
  - score_risk(): the original, deliberately simple and explainable
    approach -- keyword match on free headline feeds, cross-checked
    against an observable market proxy (overnight futures gap + VIX
    overnight change). No external dependency beyond the RSS feeds
    themselves.
  - score_risk_with_claude(): optional upgrade, used automatically by
    score_news_risk() below when an ANTHROPIC_API_KEY is set. Instead of
    matching a fixed keyword list, it asks Claude to actually read the
    fetched headlines and judge SPX-relevant risk -- catches things no
    keyword list anticipated (a keyword list is only ever as good as
    whoever wrote it), at the cost of a small per-call API charge and a
    live dependency on the Anthropic API being reachable. If the call
    fails for ANY reason (no key, network, bad response), it falls back
    to the keyword approach rather than blocking the trading decision --
    see score_news_risk().

Either way: treat the output as a second opinion, review flagged
headlines yourself, especially early on.
"""
from __future__ import annotations
from dataclasses import dataclass
import json
import os
import re
import config

try:
    import feedparser  # type: ignore
except ImportError:
    feedparser = None

try:
    import anthropic  # type: ignore
except ImportError:
    anthropic = None


@dataclass
class NewsRiskResult:
    score: int                  # 0-3
    matched_headlines: list[str]
    matched_keywords: list[str]
    market_confirms: bool
    note: str


def fetch_headlines(feed_urls: list[str] | None = None, limit_per_feed: int = 25) -> list[str]:
    """Live use only. Requires the `feedparser` package and outbound network
    access to the configured feeds -- neither is guaranteed in every
    deployment environment (e.g. this was built in a sandbox with no
    general internet egress), so callers must handle an empty result."""
    if feedparser is None:
        return []
    feed_urls = feed_urls or config.NEWS_RSS_FEEDS
    headlines: list[str] = []
    for url in feed_urls:
        try:
            parsed = feedparser.parse(url)
            for entry in parsed.entries[:limit_per_feed]:
                title = getattr(entry, "title", None)
                if title:
                    headlines.append(title)
        except Exception:
            continue
    return headlines


def keyword_scan(headlines: list[str], keywords: list[str] | None = None):
    keywords = keywords or config.GEOPOLITICAL_KEYWORDS
    matched_headlines = []
    matched_keywords = set()
    for h in headlines:
        h_lower = h.lower()
        for kw in keywords:
            if re.search(re.escape(kw.lower()), h_lower):
                matched_headlines.append(h)
                matched_keywords.add(kw)
                break
    return matched_headlines, sorted(matched_keywords)


def score_risk(headlines: list[str], overnight_futures_gap_pct: float,
               overnight_vix_change_pct: float) -> NewsRiskResult:
    matched_headlines, matched_keywords = keyword_scan(headlines)
    keyword_hit = len(matched_headlines) > 0

    # Market-observable confirmation: a real risk event usually shows up as
    # either a meaningful futures gap or a VIX jump overnight.
    market_confirms = abs(overnight_futures_gap_pct) >= 0.5 or overnight_vix_change_pct >= 8.0

    if not keyword_hit and not market_confirms:
        score = 0
        note = "No geopolitical keyword hits and no confirming overnight market move."
    elif keyword_hit and market_confirms:
        score = 3
        note = "Headline risk AND market action both confirm elevated risk -- treat as a skip day."
    elif keyword_hit and not market_confirms:
        score = 1
        note = "Headlines flagged but market shrugged it off overnight -- likely noise, downweighted."
    else:  # market moved but no keyword hit -- something is happening we didn't catch in headlines
        score = 2
        note = "Unexplained overnight futures/VIX move with no matching headline -- treat cautiously."

    return NewsRiskResult(score=score, matched_headlines=matched_headlines,
                           matched_keywords=matched_keywords, market_confirms=market_confirms,
                           note=note)


CLAUDE_NEWS_MODEL = "claude-sonnet-4-5"  # pin an exact model; update deliberately, not silently

CLAUDE_RISK_PROMPT = """You are a risk filter for a same-day (0DTE) SPX options \
credit-spread trading system. Given today's headlines, judge how much SPX-relevant \
geopolitical/macro risk they represent for TODAY's session -- war/invasion escalation, \
sanctions, tariff shocks, central bank surprises, government/political crises, major \
international market moves, bank failures, or anything else that could cause an outsized \
intraday SPX move.

Score 0-3:
  0 = nothing notable, an ordinary news day
  1 = something flagged but likely noise / already priced in
  2 = a real, developing risk worth reduced size
  3 = serious, acute risk -- skip trading today

Respond with ONLY a JSON object, no other text: \
{"score": <0-3>, "matched_headlines": [<headlines that actually drove the score, exact text>], \
"themes": [<short theme labels, e.g. "tariffs", "FOMC surprise">], "note": "<one sentence, your reasoning>"}

Headlines:
{headlines}"""


def score_risk_with_claude(headlines: list[str], overnight_futures_gap_pct: float,
                            overnight_vix_change_pct: float, api_key: str) -> NewsRiskResult:
    """Sends the fetched headlines to Claude for judgment instead of a
    fixed keyword list. Raises on any failure (missing package, no
    headlines worth sending, bad/unparseable response, API error) --
    score_news_risk() below is what catches that and falls back to
    score_risk(); this function itself stays a clean "do it right or
    raise" so the two paths never silently blend."""
    if anthropic is None:
        raise RuntimeError("anthropic package not installed")
    if not headlines:
        raise RuntimeError("no headlines to score")

    client = anthropic.Anthropic(api_key=api_key)
    prompt = CLAUDE_RISK_PROMPT.format(headlines="\n".join(f"- {h}" for h in headlines[:60]))
    resp = client.messages.create(
        model=CLAUDE_NEWS_MODEL,
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(block.text for block in resp.content if getattr(block, "type", None) == "text").strip()
    # Models occasionally wrap JSON in a fenced code block despite
    # instructions not to -- strip that defensively before parsing.
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    parsed = json.loads(text)

    score = int(parsed["score"])
    if not (0 <= score <= 3):
        raise ValueError(f"Claude returned an out-of-range score: {score}")

    market_confirms = abs(overnight_futures_gap_pct) >= 0.5 or overnight_vix_change_pct >= 8.0
    note = parsed.get("note", "").strip()
    if market_confirms and score < 2:
        note = (note + " [Overnight futures/VIX moved meaningfully but Claude's headline "
                "read didn't flag high risk -- treat as an unexplained move, review manually.]").strip()

    return NewsRiskResult(
        score=score,
        matched_headlines=list(parsed.get("matched_headlines", [])),
        matched_keywords=list(parsed.get("themes", [])),  # reusing this field for Claude's theme labels
        market_confirms=market_confirms,
        note=note or "(Claude returned no reasoning text.)",
    )


def score_news_risk(headlines: list[str], overnight_futures_gap_pct: float,
                     overnight_vix_change_pct: float) -> NewsRiskResult:
    """Entry point run_live.py should call instead of score_risk()
    directly: uses score_risk_with_claude() when ANTHROPIC_API_KEY is set
    in the environment, and transparently falls back to the keyword
    heuristic (score_risk()) if that key is absent OR the Claude call
    fails for any reason -- a bad/missing API key degrades this filter
    back to its original behavior, it never blocks a trading decision or
    raises out of this function."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        try:
            return score_risk_with_claude(headlines, overnight_futures_gap_pct,
                                           overnight_vix_change_pct, api_key)
        except Exception as e:  # noqa: BLE001
            print(f"[warning] Claude-based news scoring failed ({e}) -- falling back to keyword scan.")
    return score_risk(headlines, overnight_futures_gap_pct, overnight_vix_change_pct)
