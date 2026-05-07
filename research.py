#!/usr/bin/env python3
"""SaaS opportunity research runner using Playwright."""
import asyncio
import json
import sys
sys.path.insert(0, '/Users/chiaclaw/.hermes/hermes-agent')

from playwright.async_api import async_playwright

SEARCH_QUERIES = [
    ("reddit_saas", "site:reddit.com SaaS business ideas 2025 small business pain points"),
    ("reddit_micro_saas", "site:reddit.com micro SaaS underrated opportunity 2025"),
    ("reddit_boring_saas", "site:reddit.com boring profitable SaaS business 2025"),
    ("reddit_complaints", "site:reddit.com software complaints expensive unreliable 2025"),
    ("twitter_saas", "site:x.com micro SaaS opportunity 2025"),
    ("news_saas", "micro SaaS underserved niche B2B 2025 recurring pain"),
    ("compliance", "compliance deadline tracking software SMB 2025 opportunity"),
    ("home_service", "home service software HVAC plumbing field service gaps 2025"),
    ("document_workflow", "document workflow automation small business 2025"),
    ("construction_ops", "construction project management software complaints 2025"),
]

async def search_browser(query: str, engine: str = "google") -> list:
    """Use Playwright to search via browser."""
    results = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        if engine == "google":
            url = f"https://www.google.com/search?q={query.replace(' ', '+')}&num=10"
        elif engine == "bing":
            url = f"https://www.bing.com/search?q={query.replace(' ', '+')}&count=10"
        elif engine == "ddg":
            url = f"https://duckduckgo.com/?q={query.replace(' ', '+')}&ia=web"
        else:
            url = f"https://search.yahoo.com/search?p={query.replace(' ', '+')}&n=10"
        
        try:
            await page.goto(url, timeout=15000, wait_until="domcontentloaded")
            await asyncio.sleep(2)
            
            # Extract search results
            items = await page.query_selector_all("div.g, li.sr, div.result")
            for item in items[:8]:
                try:
                    title_el = await item.query_selector("h3, h2")
                    url_el = await item.query_selector("a")
                    snippet_el = await item.query_selector("span.st, div.snippet, p")
                    
                    title = await title_el.inner_text() if title_el else ""
                    href = await url_el.get_attribute("href") if url_el else ""
                    snippet = await snippet_el.inner_text() if snippet_el else ""
                    
                    if title and href:
                        results.append({"title": title.strip(), "url": href.strip(), "snippet": snippet.strip()[:200]})
                except:
                    pass
                    
        except Exception as e:
            results.append({"error": str(e), "url": url})
        
        await browser.close()
    return results

async def main():
    all_results = {}
    for key, query in SEARCH_QUERIES:
        print(f"Searching: {query[:60]}...", file=sys.stderr)
        for engine in ["ddg", "bing"]:
            try:
                results = await search_browser(query, engine)
                if results and "error" not in results[0]:
                    all_results[key] = results
                    print(f"  -> Got {len(results)} results from {engine}", file=sys.stderr)
                    break
                else:
                    print(f"  -> No results from {engine}, trying next...", file=sys.stderr)
            except Exception as e:
                print(f"  -> Error with {engine}: {e}", file=sys.stderr)
                continue
    
    print(json.dumps(all_results, indent=2))

if __name__ == "__main__":
    asyncio.run(main())
