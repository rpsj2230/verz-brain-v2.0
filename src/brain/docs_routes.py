"""Serving the project documents from the deployed application.

The tracker and the architecture live at a fixed path on the server rather than in a
chat, so "how far along is it" is a URL that is always current, and progress is read from
merged commits rather than from anyone's browser.

These routes are public-by-deployment but carry no company data — they describe the build,
not the client's records. Nothing here touches the gate.

Task ids: M38.3.5, M38.3.6
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

DOCS = Path(__file__).resolve().parents[2] / "docs"

router = APIRouter(tags=["docs"])


def _read_status() -> dict[str, Any]:
    """Status is baked at image build time, so it always matches the running code.

    Computing it at request time would mean shipping the git history into the image and
    would let the page disagree with the binary that serves it.
    """
    path = DOCS / "status.json"
    if not path.exists():
        return {"total": 0, "done": 0, "percent": 0.0, "commit": "unknown", "waves": []}
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


@router.get("/api/status.json", response_class=JSONResponse)
async def status_json() -> JSONResponse:
    """What the tracker page reads. Also the machine-readable progress feed."""
    return JSONResponse(_read_status(), headers={"cache-control": "no-store"})


def _doc(name: str) -> FileResponse | HTMLResponse:
    path = DOCS / name
    if not path.exists():
        return HTMLResponse(f"<h1>{name} not published</h1>", status_code=404)
    return FileResponse(path, media_type="text/html", headers={"cache-control": "no-store"})


@router.get("/tracker", response_class=HTMLResponse, response_model=None)
async def tracker() -> FileResponse | HTMLResponse:
    return _doc("tracker.html")


@router.get("/architecture", response_class=HTMLResponse, response_model=None)
async def architecture() -> FileResponse | HTMLResponse:
    return _doc("architecture.html")


@router.get("/screens", response_class=HTMLResponse, response_model=None)
async def screens() -> FileResponse | HTMLResponse:
    return _doc("screens.html")


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """A one-screen answer to 'where is this up to', with links to the full documents."""
    s = _read_status()
    waves = s.get("waves", [])
    pct = s.get("percent", 0.0)
    current = s.get("current_wave")
    current_name = next((w["name"] for w in waves if w["wave"] == current), "all waves complete")
    wave_rows = "".join(
        f"<tr><td>Wave {w['wave']}</td><td>{w['name']}</td>"
        f'<td class="n">{w["done"]}/{w["total"]}</td>'
        f'<td class="b"><span style="width:{w["percent"]}%"></span></td>'
        f'<td class="n">{w["percent"]}%</td></tr>'
        for w in waves
    )
    recent = "".join(
        f"<li><code>{r['sha']}</code> {r['subject']}</li>" for r in s.get("recent", [])[:6]
    )
    return HTMLResponse(f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Company Brain · build status</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@400;600&family=Poppins:wght@600&display=swap">
<style>
:root{{--brand:#F47936;--ink:#231F20;--ground:#F6F4F1;--panel:#fff;--line:#E3DDD7;--dim:#7A716C;--ok:#14724A}}
@media(prefers-color-scheme:dark){{:root{{--ground:#14110F;--panel:#1D1916;--line:#332C25;--ink:#F5F1ED;--dim:#948A83;--ok:#57BE8C}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--ground);color:var(--ink);font:15px/1.55 "IBM Plex Sans",system-ui,sans-serif}}
.w{{max-width:860px;margin:0 auto;padding:44px 22px 80px}}
h1{{font-family:Poppins,sans-serif;font-size:34px;margin:0 0 4px;letter-spacing:-.02em}}
.sub{{font-family:"IBM Plex Mono",monospace;font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--brand)}}
.big{{display:flex;align-items:baseline;gap:14px;margin:26px 0 6px}}
.big .p{{font-size:64px;font-weight:600;line-height:1;letter-spacing:-.03em;font-variant-numeric:tabular-nums}}
.big .c{{font-family:"IBM Plex Mono",monospace;font-size:12px;color:var(--dim)}}
.track{{height:8px;background:var(--line);border-radius:99px;overflow:hidden;margin:10px 0 22px}}
.track>span{{display:block;height:100%;background:var(--brand);border-radius:99px}}
table{{width:100%;border-collapse:collapse;font-size:13px;margin-bottom:26px}}
th{{font-family:"IBM Plex Mono",monospace;font-size:9px;letter-spacing:.1em;text-transform:uppercase;color:var(--dim);text-align:left;padding:6px 8px 5px 0;border-bottom:1px solid var(--line)}}
td{{padding:7px 8px 7px 0;border-bottom:1px solid var(--line)}}
td.n{{font-family:"IBM Plex Mono",monospace;text-align:right;font-variant-numeric:tabular-nums}}
td.b{{width:150px}}
td.b>span{{display:block;height:5px;background:var(--ok);border-radius:99px}}
a.btn{{display:inline-block;border:1px solid var(--line);background:var(--panel);border-radius:4px;padding:9px 15px;margin:0 8px 8px 0;color:var(--ink);text-decoration:none;font-weight:600;font-size:13.5px}}
a.btn.pri{{background:var(--brand);border-color:var(--brand);color:#231F20}}
ul{{padding-left:18px;font-size:13px;color:var(--dim)}}
code{{font-family:"IBM Plex Mono",monospace;font-size:11.5px;color:var(--brand)}}
.note{{font-family:"IBM Plex Mono",monospace;font-size:10.5px;color:var(--dim);border-left:2px solid var(--brand);padding-left:11px;margin-top:30px;line-height:1.7}}
</style></head><body><div class="w">
<div class="sub">Verz Company Brain · build status</div>
<h1>{current_name}</h1>
<div class="big"><span class="p">{pct}%</span>
<span class="c">{s.get("done", 0)} of {s.get("total", 0)} tasks · commit {s.get("commit", "?")}</span></div>
<div class="track"><span style="width:{pct}%"></span></div>
<table><thead><tr><th>Wave</th><th>Name</th><th style="text-align:right">Done</th><th></th><th style="text-align:right">%</th></tr></thead>
<tbody>{wave_rows}</tbody></table>
<a class="btn pri" href="/tracker">Task tracker</a>
<a class="btn" href="/architecture">Architecture</a>
<a class="btn" href="/screens">Key screens</a>
<a class="btn" href="/api/status.json">status.json</a>
<h3 style="font-family:Poppins,sans-serif;font-size:15px;margin:30px 0 6px">Recently closed</h3>
<ul>{recent}</ul>
<p class="note">Every figure here is computed from commits merged to main, never entered by
hand. A task counts as done when a commit naming its id is on main and CI passed, so this
page cannot show progress that does not exist. Generated at build time from
{s.get("commit", "?")}, so it always describes the code actually running.</p>
</div></body></html>""")
