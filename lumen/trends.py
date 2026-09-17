"""Optional Google Trends lookup for the F2 trend picker.

Only usable when the optional `pytrends-modern` dependency is installed
(`pip install lumen-cli[trends]`); callers should check `AVAILABLE` before
offering the picker at all.
"""

try:
    from pytrends_modern import TrendsRSS
    AVAILABLE = True
except ImportError:
    TrendsRSS = None
    AVAILABLE = False


def fetch_trends(geos):
    """Fetch trending searches for each geo (e.g. ["US", "GB"]).

    Returns a list of {"geo", "title", "traffic"} dicts, geos in the given
    order and each geo's trends sorted by traffic (highest first).
    """
    if not AVAILABLE:
        raise RuntimeError("pytrends-modern is not installed")
    rss = TrendsRSS()
    results = []
    for geo in geos:
        trends = rss.get_trends(geo=geo, include_images=False, include_articles=False)
        trends.sort(key=lambda t: t.get("traffic") or 0, reverse=True)
        for t in trends:
            results.append({"geo": geo, "title": t.get("title", ""), "traffic": t.get("traffic")})
    return results
