#!/usr/bin/env python3
"""Apply the MCP's patches to upstream LibreCrawl (main.py, src/crawl_db.py, src/crawler.py).

Without this patch LibreCrawl reads `session_id` BEFORE `get_or_create_crawler()`
creates it, so `crawl_id` is always null and crawl results are never written to
the SQLite DB — every audit comes back empty. This moves the `session_id` read to
AFTER the crawler (and its session) is initialised.

Idempotent: safe to run more than once. Usage: python3 patch-librecrawl.py main.py
"""
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "main.py"
content = open(path, encoding="utf-8", newline="").read()  # keep upstream CRLF

old = """    user_id = session.get('user_id')
    session_id = session.get('session_id')
    tier = session.get('tier', 'guest')"""

new = """    user_id = session.get('user_id')
    tier = session.get('tier', 'guest')"""

old2 = """    # Get or create crawler for this session
    crawler = get_or_create_crawler()"""

new2 = """    # Get or create crawler for this session (also initialises session_id)
    crawler = get_or_create_crawler()
    session_id = session.get('session_id')  # Must read AFTER get_or_create_crawler sets it"""

if "\r\n" in content:
    old, new, old2, new2 = (t.replace("\n", "\r\n") for t in (old, new, old2, new2))

if old not in content:
    print("Session persistence patch already applied or not needed — skipping")
elif old2 not in content:
    # Removing the early read without re-adding it later would leave session_id undefined.
    sys.exit("Session persistence patch: upstream code changed, patch did not apply")
else:
    content = content.replace(old, new, 1)
    content = content.replace(old2, new2, 1)
    open(path, "w", encoding="utf-8", newline="").write(content)
    print("Session persistence patch applied")

# ── Patch 2: delete_crawl must remove child rows ────────────────────────────
# Upstream's delete_crawl() runs `DELETE FROM crawls WHERE id = ?` and relies
# on ON DELETE CASCADE, but never enables `PRAGMA foreign_keys`, so SQLite
# ignores the cascade and every crawled page, link and issue of a "deleted"
# crawl stays on disk. The MCP promises ephemeral audits, so delete the child
# tables explicitly.
import os

crawl_db = os.path.join(os.path.dirname(os.path.abspath(path)), "src", "crawl_db.py")
db_src = open(crawl_db, encoding="utf-8", newline="").read()  # keep upstream CRLF

old3 = """            cursor.execute('DELETE FROM crawls WHERE id = ?', (crawl_id,))"""
new3 = """            for _child in ('crawl_queue', 'crawl_issues', 'crawl_links', 'crawled_urls'):
                cursor.execute(f'DELETE FROM {_child} WHERE crawl_id = ?', (crawl_id,))
            cursor.execute('DELETE FROM crawls WHERE id = ?', (crawl_id,))"""

if "\r\n" in db_src:
    new3 = new3.replace("\n", "\r\n")

if "for _child in ('crawl_queue'" in db_src:
    print("delete_crawl child-row patch already applied — skipping")
elif old3 not in db_src:
    sys.exit("delete_crawl child-row patch: upstream code changed, patch did not apply")
else:
    open(crawl_db, "w", encoding="utf-8", newline="").write(db_src.replace(old3, new3, 1))
    print("delete_crawl child-row patch applied")

# ── Patch 3: response_time must be server latency ───────────────────────────
# Upstream stamps response_time after parsing the page and HEAD-checking every
# image on it, so a page the server answered in 2s reads as 18s and nearly
# every page is flagged slow. In the requests path, use the time the server
# took to return its headers (requests' `response.elapsed`).
crawler_py = os.path.join(os.path.dirname(os.path.abspath(path)), "src", "crawler.py")
cr_src = open(crawler_py, encoding="utf-8", newline="").read()  # keep upstream CRLF

old4 = "result['response_time'] = round((time.time() - start_time) * 1000, 2)"
new4 = "result['response_time'] = round(response.elapsed.total_seconds() * 1000, 2)"
start = cr_src.find("def _crawl_url_with_requests")
end = cr_src.find("def _crawl_url_with_javascript")

if start < 0 or end < start:
    sys.exit("response_time patch: upstream code changed, patch did not apply")
section = cr_src[start:end]
if new4 in section:
    print("response_time patch already applied — skipping")
elif section.count(old4) != 1:
    sys.exit("response_time patch: upstream code changed, patch did not apply")
else:
    cr_src = cr_src[:start] + section.replace(old4, new4, 1) + cr_src[end:]
    open(crawler_py, "w", encoding="utf-8", newline="").write(cr_src)
    print("response_time patch applied")
