"""Build a single searchable index.html over an existing Reddit Stash backup.

What it does for you: turn the folder of markdown files into one page you can
open in any browser and type into to find a saved post, instead of grepping
folders by hand. Why it matters: the backup is only as useful as your ability
to find things in it, and this needs no server, no build step, and no network.
How it works: walk the save directory, read the YAML front matter each markdown
file already carries, and emit a self-contained index.html with a client-side
filter box. Re-runnable any time (also against a synced Dropbox/S3 copy).

    python generate_index.py               # uses save_directory from settings.ini
    python generate_index.py reddit/       # explicit directory
    python generate_index.py --selftest    # runnable check, no repo needed
"""
import os
import sys
import json
import glob


def _read_front_matter(md_path):
    """Parse the flat `--- key: value ---` header + first `# title` line.

    Returns a dict, or None if the file has no front matter block.
    """
    meta = {}
    title = ""
    try:
        with open(md_path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return None

    if not lines or lines[0].strip() != "---":
        return None

    i = 1
    while i < len(lines) and lines[i].strip() != "---":
        key, sep, value = lines[i].partition(":")  # split on FIRST colon only
        if sep:
            meta[key.strip()] = value.strip()
        i += 1

    # First markdown heading after the front matter is the title.
    for line in lines[i + 1:]:
        if line.startswith("# "):
            title = line[2:].strip()
            break

    meta["title"] = title
    return meta


def _category(filename):
    """Coarse item type from the filename prefix Reddit Stash writes."""
    name = filename.upper()
    for prefix in ("SAVED_POST", "SAVED_COMMENT", "UPVOTE_POST", "UPVOTE_COMMENT",
                   "GDPR_POST", "GDPR_COMMENT", "POST", "COMMENT"):
        if name.startswith(prefix):
            return prefix.replace("_", " ").title()
    return "Item"


def collect_items(save_directory):
    """Walk the save directory and return one dict per markdown file."""
    items = []
    for md_path in glob.iglob(os.path.join(save_directory, "**", "*.md"), recursive=True):
        meta = _read_front_matter(md_path)
        if meta is None:
            continue
        rel = os.path.relpath(md_path, start=save_directory)
        items.append({
            "title": meta.get("title") or os.path.basename(md_path),
            "subreddit": meta.get("subreddit", ""),
            "author": meta.get("author", ""),
            "timestamp": meta.get("timestamp", ""),
            "flair": meta.get("flair", ""),
            "permalink": meta.get("permalink", ""),
            "category": _category(os.path.basename(md_path)),
            "file": rel,
        })
    items.sort(key=lambda x: x["timestamp"], reverse=True)
    return items


# ponytail: data is injected as JSON and rendered via textContent in the browser,
# so untrusted Reddit content can't inject markup. Only the </script> escape and
# the http(s)-link guard below are needed.
_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reddit Stash Index</title>
<style>
  body{{font-family:system-ui,Arial,sans-serif;margin:0;background:#f6f7f8;color:#1a1a1b}}
  header{{position:sticky;top:0;background:#ff4500;color:#fff;padding:12px 16px}}
  header h1{{margin:0 0 8px;font-size:18px}}
  #q{{width:100%;padding:10px;font-size:15px;border:none;border-radius:6px;box-sizing:border-box}}
  #count{{padding:8px 16px;color:#666;font-size:13px}}
  ul{{list-style:none;margin:0;padding:0 8px 40px}}
  li{{background:#fff;margin:8px;padding:12px;border:1px solid #ddd;border-radius:6px}}
  .t{{font-weight:600}}
  .t a{{color:#1a1a1b;text-decoration:none}} .t a:hover{{color:#ff4500}}
  .m{{color:#666;font-size:12px;margin-top:4px}}
  .m a{{color:#0079d3;text-decoration:none}}
  .b{{display:inline-block;background:#eef;border-radius:3px;padding:0 5px;font-size:11px;color:#334}}
  @media(prefers-color-scheme:dark){{
    body{{background:#0b0b0c;color:#d7dadc}} li{{background:#161617;border-color:#343536}}
    .t a{{color:#d7dadc}} #q{{background:#161617;color:#d7dadc}} .b{{background:#22303c;color:#9fd}}
  }}
</style></head><body>
<header><h1>Reddit Stash &mdash; {n} items</h1>
<input id="q" placeholder="Filter by title, subreddit, author, flair, type&hellip;" autofocus></header>
<div id="count"></div><ul id="list"></ul>
<script>
const DATA = {data};
const list = document.getElementById('list'), count = document.getElementById('count');
function render(items){{
  list.textContent = '';
  count.textContent = items.length + ' shown';
  for (const it of items){{
    const li = document.createElement('li');
    const t = document.createElement('div'); t.className = 't';
    const a = document.createElement('a'); a.textContent = it.title;
    a.href = it.file; a.target = '_blank'; t.appendChild(a);
    const m = document.createElement('div'); m.className = 'm';
    const badge = document.createElement('span'); badge.className = 'b'; badge.textContent = it.category;
    m.appendChild(badge);
    m.appendChild(document.createTextNode(' ' + [it.subreddit, it.author, it.timestamp, it.flair].filter(Boolean).join(' \\u2022 ')));
    if (/^https?:\\/\\//.test(it.permalink)){{
      const p = document.createElement('a'); p.href = it.permalink; p.target = '_blank';
      p.textContent = 'reddit'; m.appendChild(document.createTextNode(' \\u2022 ')); m.appendChild(p);
    }}
    li.appendChild(t); li.appendChild(m); list.appendChild(li);
  }}
}}
document.getElementById('q').addEventListener('input', e => {{
  const q = e.target.value.toLowerCase();
  render(!q ? DATA : DATA.filter(it =>
    (it.title+' '+it.subreddit+' '+it.author+' '+it.flair+' '+it.category).toLowerCase().includes(q)));
}});
render(DATA);
</script></body></html>
"""


def build_html(items):
    data = json.dumps(items, ensure_ascii=False).replace("</", "<\\/")  # keep </script> from closing the tag
    return _PAGE.format(n=len(items), data=data)


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    if args:
        save_directory = args[0]
    else:
        import configparser
        cp = configparser.ConfigParser()
        cp.read("settings.ini")
        save_directory = cp.get("Settings", "save_directory", fallback="reddit/")

    if not os.path.isdir(save_directory):
        print(f"Directory not found: {save_directory}")
        return 1

    items = collect_items(save_directory)
    out = os.path.join(save_directory, "index.html")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(build_html(items))
    print(f"Indexed {len(items)} items -> {out}")
    return 0


def _selftest():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "apple"))
        with open(os.path.join(d, "apple", "SAVED_POST_abc.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nid: abc\nsubreddit: /r/apple\ntimestamp: 2020-10-11 04:48:04\n"
                     "author: /u/nstrm\npermalink: https://reddit.com/r/apple/comments/abc/x/\n---\n\n"
                     "# A saved title with </script> and <b>markup</b>\n\nbody\n")
        # a file with no front matter must be skipped
        with open(os.path.join(d, "notes.md"), "w", encoding="utf-8") as fh:
            fh.write("# just a note\n")

        items = collect_items(d)
        assert len(items) == 1, items
        it = items[0]
        assert it["title"] == "A saved title with </script> and <b>markup</b>"
        assert it["subreddit"] == "/r/apple"
        assert it["category"] == "Saved Post"
        assert it["file"] == os.path.join("apple", "SAVED_POST_abc.md")

        page = build_html(items)
        assert "</script>" not in page.split("<\\/script>")[0].split("DATA = ")[1]  # data literal is escaped
        assert "<b>markup</b>" not in page  # untrusted markup never lands as raw HTML
        assert "A saved title" in page      # ...but the text is present (rendered via textContent)
    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        sys.exit(main(sys.argv[1:]))
