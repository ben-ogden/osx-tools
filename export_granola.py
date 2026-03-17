#!/usr/bin/env python3
# export_granola.py
#
# Prerequisites:
#   pip3 install websockets --break-system-packages
#   Granola must be running with debug port:
#     pkill -x Granola && open /Applications/Granola.app --args --remote-debugging-port=9222
#
# Run: python3 export_granola.py

import json
import re
import asyncio
import websockets
import urllib.request
from pathlib import Path

DEBUG_URL   = "http://localhost:9222"
EXPORT_BASE = Path.home() / "Desktop/GranolaExport"


def find_cache_path() -> Path:
    granola_dir = Path.home() / "Library/Application Support/Granola"
    candidates = sorted(granola_dir.glob("cache-v*.json"), reverse=True)
    if not candidates:
        raise FileNotFoundError(f"No cache-v*.json found in {granola_dir}")
    return candidates[0]


_id = 0

async def run_js(ws, expr, await_promise=False, timeout=30000):
    global _id
    _id += 1
    mid = _id
    await ws.send(json.dumps({
        "id": mid,
        "method": "Runtime.evaluate",
        "params": {
            "expression": expr,
            "returnByValue": True,
            "awaitPromise": await_promise,
            "timeout": timeout
        }
    }))
    while True:
        r = json.loads(await ws.recv())
        if r.get("id") == mid:
            if r.get("result", {}).get("exceptionDetails"):
                raise RuntimeError(r["result"]["exceptionDetails"].get("text", "JS error"))
            return r.get("result", {}).get("result", {}).get("value")


async def get_db_from_app(ws) -> tuple[str, str]:
    """
    Find the cacheStore bundle URL and db export key.
    Reads the main entry script to find the cacheStore chunk filename,
    then imports it and scans exports for the Kysely db instance.
    """
    # Get the main entry script URL from the page's only module script tag
    main_script_url = await run_js(ws, """
    (() => {
        const s = document.querySelector('script[type="module"][src]');
        return s ? s.src : null;
    })()
    """)
    if not main_script_url:
        raise RuntimeError("Could not find main script URL in page")
    print(f"  Main script: {main_script_url}")

    # The main script is already loaded; fetch it and grep for the cacheStore chunk name
    base_url = main_script_url.rsplit('/', 1)[0]
    bundle_url = await run_js(ws, f"""
    (async () => {{
        const resp = await fetch('{main_script_url}');
        const text = await resp.text();
        const m = text.match(/cacheStore-[A-Za-z0-9_-]+\\.js/);
        if (!m) return null;
        return '{base_url}/' + m[0];
    }})()
    """, await_promise=True, timeout=15000)

    if not bundle_url:
        raise RuntimeError("Could not find cacheStore chunk reference in main script")
    print(f"  cacheStore bundle: {bundle_url}")

    # Import the bundle and find the Kysely db export
    db_key = await run_js(ws, f"""
    (async () => {{
        const mod = await import('{bundle_url}');
        for (const [k, v] of Object.entries(mod)) {{
            if (v && typeof v === 'object' && typeof v.selectFrom === 'function') {{
                return k;
            }}
        }}
        return null;
    }})()
    """, await_promise=True, timeout=30000)

    if not db_key:
        raise RuntimeError(f"Could not find Kysely db export in {bundle_url}")

    return bundle_url, db_key


def sanitize(name: str, max_len: int = 80) -> str:
    name = re.sub(r'[/:\\*?"<>|]', '_', name)
    name = name.strip(". ")
    return name[:max_len] if name else "Untitled"


def extract_person(p) -> str:
    if isinstance(p, str):
        return p
    if isinstance(p, dict):
        return p.get('name') or p.get('email') or ''
    return str(p)


def prosemirror_to_markdown(node: dict, depth: int = 0) -> str:
    node_type = node.get("type", "")
    content   = node.get("content", [])
    marks     = node.get("marks", [])

    if node_type == "text":
        text = node.get("text", "")
        for mark in marks:
            mt = mark.get("type", "")
            if mt == "bold":
                text = f"**{text}**"
            elif mt == "italic":
                text = f"*{text}*"
            elif mt == "link":
                href = mark.get("attrs", {}).get("href", "")
                text = f"[{text}]({href})"
        return text

    if node_type == "heading":
        level = node.get("attrs", {}).get("level", 1)
        text  = "".join(prosemirror_to_markdown(c, depth) for c in content)
        return "#" * level + " " + text + "\n"

    if node_type == "paragraph":
        text = "".join(prosemirror_to_markdown(c, depth) for c in content)
        return text + "\n"

    if node_type == "hardBreak":
        return "\n"

    if node_type == "horizontalRule":
        return "\n---\n"

    if node_type in ("bulletList", "orderedList"):
        items = []
        for i, child in enumerate(content):
            prefix = f"{i+1}." if node_type == "orderedList" else "-"
            item_text = prosemirror_to_markdown(child, depth + 1).strip()
            indented = "\n".join(
                ("  " * depth + line) if j > 0 else line
                for j, line in enumerate(item_text.split("\n"))
            )
            items.append(f"{'  ' * depth}{prefix} {indented}")
        return "\n".join(items) + "\n"

    if node_type == "listItem":
        return "".join(prosemirror_to_markdown(c, depth) for c in content)

    return "".join(prosemirror_to_markdown(c, depth) for c in content)


def extract_transcript_url(node: dict) -> str | None:
    if not isinstance(node, dict):
        return None
    for mark in node.get("marks", []):
        if mark.get("type") == "link":
            href = mark.get("attrs", {}).get("href", "")
            if "notes.granola.ai/t/" in href:
                return href
    for child in node.get("content", []):
        result = extract_transcript_url(child)
        if result:
            return result
    return None


def format_document(doc: dict, panels: list) -> str:
    title       = doc.get("title") or "Untitled"
    created     = doc.get("created_at") or ""
    updated     = doc.get("updated_at") or ""
    people      = doc.get("people") or []
    notes_md    = doc.get("notes_markdown") or ""
    notes_plain = doc.get("notes_plain") or ""
    notes_node  = doc.get("notes") or {}

    lines = []
    lines.append(f"# {title}")
    lines.append("")
    if created:
        lines.append(f"**Date:** {created[:10]}")
    if updated:
        lines.append(f"**Updated:** {updated[:10]}")
    if people:
        names = [n for n in (extract_person(p) for p in people) if n]
        if names:
            lines.append(f"**Attendees:** {', '.join(names)}")

    for panel in panels:
        panel_title   = panel.get("title") or "Panel"
        panel_content = panel.get("content") or {}

        transcript_url = extract_transcript_url(panel_content)
        if transcript_url:
            lines.append(f"**Transcript:** {transcript_url}")

        lines.append("")
        lines.append(f"## {panel_title}")
        lines.append("")

        original_html = panel.get("original_content") or ""
        if original_html.strip():
            text = re.sub(r'<[^>]+>', '', original_html)
            text = re.sub(r'\n{3,}', '\n\n', text).strip()
            lines.append(text)
        elif panel_content:
            lines.append(prosemirror_to_markdown(panel_content).strip())
        else:
            panel_plain = panel.get("content_plain") or ""
            if panel_plain.strip():
                lines.append(panel_plain.strip())
        lines.append("")

    lines.append("## Notes")
    lines.append("")
    if notes_md.strip():
        lines.append(notes_md.strip())
    elif notes_plain.strip():
        lines.append(notes_plain.strip())
    elif notes_node:
        lines.append(prosemirror_to_markdown(notes_node).strip())
    else:
        lines.append("*(no notes)*")

    lines.append("")
    return "\n".join(lines)


async def main():
    cache_path = find_cache_path()
    print(f"Loading documents from {cache_path}")
    with open(cache_path) as f:
        cache = json.load(f)
    state = cache.get("cache", {}).get("state") or cache.get("state") or {}
    documents = state.get("documents") or {}
    if isinstance(documents, list):
        documents = {d["id"]: d for d in documents if isinstance(d, dict)}
    print(f"  Found {len(documents)} documents")

    print(f"\nConnecting to Granola debug port...")
    with urllib.request.urlopen(f"{DEBUG_URL}/json") as r:
        pages = json.loads(r.read())
    page = next((p for p in pages if p.get("type") == "page"), None)
    if not page:
        print("ERROR: Granola page not found.")
        print("Run: pkill -x Granola && open /Applications/Granola.app --args --remote-debugging-port=9222")
        return

    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=500*1024*1024) as ws:
        print("  Connected.")

        print("  Finding cacheStore bundle and database key...")
        bundle_url, db_key = await get_db_from_app(ws)
        print(f"  DB key: '{db_key}'")

        print("\nQuerying document_panels...")
        val = await run_js(ws, f"""
        (async () => {{
            const mod = await import('{bundle_url}');
            const db = mod['{db_key}'];
            const rows = await db.selectFrom('document_panels').selectAll().execute();
            return JSON.stringify(rows);
        }})()
        """, await_promise=True, timeout=60000)

        if not val:
            print("ERROR: No data returned from database.")
            return

        panels_raw = json.loads(val)
        print(f"  Found {len(panels_raw)} panels")

        panels_by_doc = {}
        for panel in panels_raw:
            doc_id = panel.get("document_id")
            if doc_id:
                panels_by_doc.setdefault(doc_id, []).append(panel)

        print(f"\nExporting to {EXPORT_BASE}...")
        EXPORT_BASE.mkdir(parents=True, exist_ok=True)

        exported = 0
        skipped  = 0

        for doc_id, doc in documents.items():
            if not isinstance(doc, dict):
                skipped += 1
                continue
            try:
                if doc.get("deleted_at") or doc.get("was_trashed"):
                    skipped += 1
                    continue

                title       = doc.get("title") or "Untitled"
                created     = doc.get("created_at") or ""
                folder_name = created[:7] if created else "unknown-date"

                out_dir = EXPORT_BASE / folder_name
                out_dir.mkdir(parents=True, exist_ok=True)

                filename = sanitize(title) + ".md"
                out_path = out_dir / filename
                counter  = 1
                while out_path.exists():
                    out_path = out_dir / f"{sanitize(title)}_{counter}.md"
                    counter += 1

                doc_panels = panels_by_doc.get(doc_id, [])
                content    = format_document(doc, doc_panels)
                out_path.write_text(content, encoding="utf-8")
                exported += 1
                print(f"  ✓ [{len(doc_panels)} panels] {folder_name}/{out_path.name}")

            except Exception as e:
                print(f"  ✗ {doc_id}: {e}")
                skipped += 1

        print()
        print("Done.")
        print(f"  Exported: {exported}")
        print(f"  Skipped:  {skipped}")
        print(f"  Output:   ~/Desktop/GranolaExport/")


if __name__ == "__main__":
    asyncio.run(main())
