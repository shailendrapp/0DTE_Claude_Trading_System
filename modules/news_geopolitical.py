"""
Geopolitical / breaking-news risk heuristic.

Deliberately simple and explainable rather than a black-box NLP model:
keyword match on free headline feeds, cross-checked against an
observable market proxy (overnight futures gap + VIX overnight change).
Treat the output as a second opinion, review flagged headlines yourself.
"""
from __future__ import annotations
from dataclasses import dataclass
import re
import config

try:
    import feedparser  # type: ignore
except ImportError:
    feedparser = None


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
