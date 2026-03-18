#!/usr/bin/env python3
# export_slack.py
#
# Exports cached Slack messages to ~/Desktop/SlackExport/<workspace>/<channel>.md
# Includes thread replies grouped under parent messages.
#
# Prerequisites:
#   pip3 install websockets --break-system-packages
#   Slack must be running with debug port:
#     pkill -x Slack && open /Applications/Slack.app --args --remote-debugging-port=9223
#
# Run: python3 export_slack.py

import json
import re
import asyncio
import websockets
import urllib.request
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

DEBUG_URL   = "http://localhost:9223"
EXPORT_BASE = Path.home() / "Desktop/SlackExport"

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


def sanitize(name: str, max_len: int = 60) -> str:
    name = re.sub(r'[/:\\*?"<>|]', '_', name)
    name = name.strip(". ")
    return name[:max_len] if name else "Untitled"


def ts_to_dt(ts: str) -> datetime:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc)
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def ts_to_str(ts: str) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return ts


def resolve_text(text: str, users: dict, chan_names: dict) -> str:
    if not text:
        return ""
    text = re.sub(r'<@([A-Z0-9]+)>', lambda m: f"@{users.get(m.group(1), m.group(1))}", text)
    text = re.sub(r'<#([A-Z0-9]+)\|?([^>]*)>', lambda m: f"#{m.group(2) or chan_names.get(m.group(1), m.group(1))}", text)
    text = re.sub(r'<!channel>', '@channel', text)
    text = re.sub(r'<!here>', '@here', text)
    text = re.sub(r'<!everyone>', '@everyone', text)
    text = re.sub(r'<(https?://[^|>]+)\|([^>]+)>', r'\2 (\1)', text)
    text = re.sub(r'<(https?://[^>]+)>', r'\1', text)
    return text


def format_message(msg: dict, users: dict, chan_names: dict, indent: str = "") -> list:
    lines = []
    sender   = msg.get("sender", "?")
    time_str = ts_to_str(msg.get("ts", ""))[11:19]
    text     = resolve_text(msg.get("text", ""), users, chan_names)
    files    = msg.get("files", []) or msg.get("attachments", []) or []

    lines.append(f"{indent}**{sender}** `{time_str}`")
    if text:
        for line in text.split("\n"):
            lines.append(f"{indent}{line}")
    for f in files:
        if not isinstance(f, dict):
            continue
        fname  = f.get("name") or f.get("title") or f.get("fallback") or "file"
        ftype  = f.get("filetype") or f.get("mimetype") or ""
        fperma = f.get("permalink") or f.get("from_url") or f.get("url_private") or ""
        lines.append(f"{indent}📎 [{fname}]({fperma})" + (f" `{ftype}`" if ftype else ""))

    return lines


def format_channel(chan_name: str, top_level: list, replies_by_parent: dict,
                   users: dict, chan_names: dict, files_by_channel: dict) -> str:
    lines = []
    lines.append(f"# {chan_name}")
    lines.append("")
    lines.append(f"*Exported {datetime.now().strftime('%Y-%m-%d %H:%M')}*")
    lines.append("")

    if not top_level:
        lines.append("*(no messages cached)*")
        lines.append("")
        return "\n".join(lines)

    current_date = None
    for msg in top_level:
        date = ts_to_str(msg["ts"])[:10]
        if date != current_date:
            lines.append(f"## {date}")
            lines.append("")
            current_date = date

        lines.extend(format_message(msg, users, chan_names))

        # Thread replies
        thread_replies = replies_by_parent.get(msg["ts"], [])
        if thread_replies:
            lines.append("")
            lines.append("> **Thread replies:**")
            for reply in thread_replies:
                for line in format_message(reply, users, chan_names, indent="> "):
                    lines.append(line)
                lines.append(">")
        elif msg.get("reply_count", 0):
            uncached = msg["reply_count"] - len(thread_replies)
            if uncached > 0:
                lines.append(f"*↩ {uncached} repl{'y' if uncached == 1 else 'ies'} not cached — open thread in Slack to load*")

        lines.append("")

    # Files shared in this channel
    chan_files = files_by_channel.get(chan_name, [])
    if chan_files:
        lines.append("---")
        lines.append("## Files Shared in This Channel")
        lines.append("")
        for f in chan_files:
            fname  = f.get("title") or f.get("name") or "file"
            ftype  = f.get("pretty_type") or f.get("filetype") or ""
            fperma = f.get("permalink") or ""
            fsize  = f.get("size") or 0
            created = ts_to_str(str(f.get("created", "")))[:10]
            owner  = users.get(f.get("user", ""), f.get("user", ""))
            lines.append(f"- **{fname}**" + (f" `{ftype}`" if ftype else "") +
                         f" — {fsize:,} bytes, {created}, by {owner}")
            if fperma:
                lines.append(f"  {fperma}")
        lines.append("")

    return "\n".join(lines)


async def main():
    print(f"Connecting to Slack debug port at {DEBUG_URL}...")
    with urllib.request.urlopen(f"{DEBUG_URL}/json") as r:
        pages = json.loads(r.read())
    page = next((p for p in pages if p.get("type") == "page"), None)
    if not page:
        print("ERROR: Slack page not found.")
        print("Run: pkill -x Slack && open /Applications/Slack.app --args --remote-debugging-port=9223")
        return
    print(f"  Connected: {page.get('title', '')}")

    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=200*1024*1024) as ws:

        print("Loading state from IndexedDB...")
        val = await run_js(ws, """
        (async () => {
            const allDbs = await indexedDB.databases();
            const persistDb = allDbs.find(d => d.name === 'reduxPersistence');
            if (!persistDb) return JSON.stringify({error: 'reduxPersistence not found'});

            const db = await new Promise((res, rej) => {
                const r = indexedDB.open('reduxPersistence', persistDb.version);
                r.onsuccess = e => res(e.target.result);
                r.onerror = e => rej(e.target.error);
            });
            const keys = await new Promise((res, rej) => {
                const tx = db.transaction('reduxPersistenceStore', 'readonly');
                const r = tx.objectStore('reduxPersistenceStore').getAllKeys();
                r.onsuccess = e => res(e.target.result);
                r.onerror = e => rej(e.target.error);
            });
            const persistKey = keys.find(k => k.startsWith('persist:slack-client-'));
            if (!persistKey) { db.close(); return JSON.stringify({error: 'no persist key'}); }

            const raw = await new Promise((res, rej) => {
                const tx = db.transaction('reduxPersistenceStore', 'readonly');
                const r = tx.objectStore('reduxPersistenceStore').get(persistKey);
                r.onsuccess = e => res(e.target.result);
                r.onerror = e => rej(e.target.error);
            });
            db.close();

            const parse = k => {
                const v = raw[k];
                return typeof v === 'string' ? JSON.parse(v) : (v || {});
            };

            return JSON.stringify({
                channels: parse('channels'),
                members:  parse('members'),
                messages: parse('messages'),
                files:    parse('files'),
                teams:    parse('teams'),
            });
        })()
        """, await_promise=True, timeout=30000)

        if not val:
            print("ERROR: No data returned.")
            return

        data = json.loads(val)
        if "error" in data:
            print(f"ERROR: {data['error']}")
            return

        channels = data.get("channels", {})
        members  = data.get("members", {})
        messages = data.get("messages", {})
        files    = data.get("files", {})
        teams    = data.get("teams", {})

        # Build lookup maps
        users = {}
        for uid, m in members.items():
            profile = m.get("profile") or {}
            users[uid] = (profile.get("display_name") or profile.get("real_name") or
                          m.get("name") or uid)
        users["USLACKBOT"] = "Slackbot"

        chan_names = {}
        for cid, c in channels.items():
            if c.get("is_im"):
                other = users.get(c.get("user"), c.get("user", cid))
                chan_names[cid] = f"DM-{other}"
            elif c.get("is_mpim"):
                chan_names[cid] = "group-dm-" + cid
            else:
                chan_names[cid] = c.get("name", cid)

        workspace_name = "slack"
        for tid, team in teams.items():
            workspace_name = team.get("name") or team.get("domain") or workspace_name
            break

        print(f"  Workspace: {workspace_name}")
        print(f"  Channels:  {len(channels)}")
        print(f"  Members:   {len(members)}")
        print(f"  Files:     {len(files)}")

        # Parse all messages, separate top-level from replies
        # Top-level: thread_ts is null OR thread_ts == ts
        # Reply: thread_ts != ts
        msgs_by_chan   = defaultdict(list)   # cid -> [top-level msgs]
        replies_by_ts  = defaultdict(list)   # parent_ts -> [reply msgs] (per channel)

        for cid, chan_msgs in messages.items():
            for ts, msg in chan_msgs.items():
                if msg.get("type") != "message":
                    continue
                sender = (users.get(msg.get("user")) or
                          msg.get("username") or
                          msg.get("user") or "?")
                m = {
                    "ts":          msg.get("ts", ts),
                    "thread_ts":   msg.get("thread_ts"),
                    "sender":      sender,
                    "text":        msg.get("text", ""),
                    "reply_count": msg.get("reply_count", 0),
                    "files":       msg.get("files", []),
                    "attachments": msg.get("attachments", []),
                    "cid":         cid,
                }
                thread_ts = msg.get("thread_ts")
                if thread_ts and thread_ts != msg.get("ts", ts):
                    # This is a reply — key by (cid, parent_ts)
                    replies_by_ts[(cid, thread_ts)].append(m)
                else:
                    msgs_by_chan[cid].append(m)

        # Sort everything by timestamp
        for cid in msgs_by_chan:
            msgs_by_chan[cid].sort(key=lambda m: float(m["ts"]))
        for key in replies_by_ts:
            replies_by_ts[key].sort(key=lambda m: float(m["ts"]))

        total_top   = sum(len(v) for v in msgs_by_chan.values())
        total_reply = sum(len(v) for v in replies_by_ts.values())
        print(f"  Messages:  {total_top} top-level, {total_reply} replies")

        # Group files by channel
        files_by_channel = defaultdict(list)
        for fid, f in files.items():
            for cid in (f.get("channels") or []) + (f.get("ims") or []) + (f.get("groups") or []):
                cname = chan_names.get(cid, cid)
                files_by_channel[cname].append(f)

        # Export
        out_dir = EXPORT_BASE / sanitize(workspace_name)
        out_dir.mkdir(parents=True, exist_ok=True)
        print(f"\nExporting to {out_dir}...")

        exported = 0
        for cid, top_msgs in msgs_by_chan.items():
            chan_display = chan_names.get(cid, cid)

            # Build per-channel reply lookup by parent ts
            chan_replies = {}
            for ts_msg in top_msgs:
                key = (cid, ts_msg["ts"])
                if key in replies_by_ts:
                    chan_replies[ts_msg["ts"]] = replies_by_ts[key]

            filename = sanitize(chan_display) + ".md"
            out_path = out_dir / filename
            content  = format_channel(chan_display, top_msgs, chan_replies,
                                      users, chan_names, files_by_channel)
            out_path.write_text(content, encoding="utf-8")

            reply_count = sum(len(v) for v in chan_replies.values())
            print(f"  ✓ {filename} ({len(top_msgs)} messages, {reply_count} replies)")
            exported += 1

        # File manifest
        if files:
            manifest = [f"# {workspace_name} — File Manifest", "",
                        f"*{len(files)} files · exported {datetime.now().strftime('%Y-%m-%d %H:%M')}*", ""]
            for fid, f in sorted(files.items(), key=lambda x: x[1].get("created", 0), reverse=True):
                fname    = f.get("title") or f.get("name") or fid
                ftype    = f.get("pretty_type") or f.get("filetype") or ""
                fperma   = f.get("permalink") or ""
                fprivate = f.get("url_private") or ""
                fsize    = f.get("size") or 0
                created  = ts_to_str(str(f.get("created", "")))[:10]
                owner    = users.get(f.get("user", ""), f.get("user", ""))
                manifest.append(f"## {fname}")
                if ftype:    manifest.append(f"**Type:** {ftype}  ")
                manifest.append(f"**Size:** {fsize:,} bytes  ")
                manifest.append(f"**Created:** {created}  ")
                manifest.append(f"**Owner:** {owner}  ")
                if fperma:   manifest.append(f"**Permalink:** {fperma}  ")
                if fprivate and fprivate != fperma:
                    manifest.append(f"**Download:** {fprivate}  ")
                manifest.append("")

            manifest_path = out_dir / "_file_manifest.md"
            manifest_path.write_text("\n".join(manifest), encoding="utf-8")
            print(f"  ✓ _file_manifest.md ({len(files)} files)")

        print()
        print("Done.")
        print(f"  Channels exported: {exported}")
        print(f"  Output: {out_dir}")
        print()
        print("Note: thread replies only cached if you opened them in Slack.")
        print("File contents require internet to download.")


if __name__ == "__main__":
    asyncio.run(main())
