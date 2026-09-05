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


@router.get("/build/tracker", response_class=HTMLResponse, response_model=None)
async def tracker() -> FileResponse | HTMLResponse:
    return _doc("tracker.html")


@router.get("/build/architecture", response_class=HTMLResponse, response_model=None)
async def architecture() -> FileResponse | HTMLResponse:
    return _doc("architecture.html")


@router.get("/build/screens", response_class=HTMLResponse, response_model=None)
async def screens() -> FileResponse | HTMLResponse:
    return _doc("screens.html")


@router.get("/build", response_class=HTMLResponse)
async def build_status(request: Request) -> HTMLResponse:
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
    needs = _needs_count()
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
<a class="btn pri" href="/build/tracker">Task tracker</a>
<a class="btn" href="/build/architecture">Architecture</a>
<a class="btn" href="/build/screens">Key screens</a>
<a class="btn" href="/build/needs-rupash">Needs you ({needs})</a>
<a class="btn" href="/api/status.json">status.json</a>
<h3 style="font-family:Poppins,sans-serif;font-size:15px;margin:30px 0 6px">Recently closed</h3>
<ul>{recent}</ul>
<p class="note">Every figure here is computed from commits merged to main, never entered by
hand. A task counts as done when a commit naming its id is on main and CI passed, so this
page cannot show progress that does not exist. Generated at build time from
{s.get("commit", "?")}, so it always describes the code actually running.</p>
</div></body></html>""")


# --------------------------------------------------------------- product URLs
# These are where the system itself will live. They are reserved and answered now, so the
# addresses never move: a link sent today keeps working when the real screen replaces the
# placeholder. Each says honestly which wave builds it rather than 404ing, because "not
# yet" and "wrong address" are different problems and should not look the same.

COMING: dict[str, tuple[str, int, str]] = {
    "/admin": (
        "Admin console",
        3,
        "Company overview, people and grants, agents, knowledge, learning, connectors, models",
    ),
    "/me": (
        "My workspace",
        4,
        "Your agents, your knowledge, what it learned about you, your usage",
    ),
    "/ask": ("Ask", 2, "The chat. One box, no agent picker - the router chooses"),
    "/login": ("Sign in", 1, "Single sign-on through Keycloak"),
}


def _placeholder(path: str) -> HTMLResponse:
    name, wave, detail = COMING[path]
    s = _read_status()
    waves = {w["wave"]: w for w in s.get("waves", [])}
    w = waves.get(wave, {})
    pct = w.get("percent", 0)
    done, total = w.get("done", 0), w.get("total", 0)
    return HTMLResponse(
        f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{name} - not built yet</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@400;600&family=Poppins:wght@600&display=swap">
<style>
:root{{--brand:#F47936;--ink:#231F20;--ground:#F6F4F1;--line:#E3DDD7;--dim:#7A716C}}
@media(prefers-color-scheme:dark){{:root{{--ground:#14110F;--line:#332C25;--ink:#F5F1ED;--dim:#948A83}}}}
body{{margin:0;background:var(--ground);color:var(--ink);font:15px/1.6 "IBM Plex Sans",system-ui,sans-serif}}
.w{{max-width:620px;margin:0 auto;padding:14vh 22px 60px}}
.eyebrow{{font-family:"IBM Plex Mono",monospace;font-size:10.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--brand)}}
h1{{font-family:Poppins,sans-serif;font-size:32px;margin:8px 0 14px;letter-spacing:-.02em}}
p{{color:var(--dim);max-width:52ch}}
.bar{{height:6px;background:var(--line);border-radius:99px;overflow:hidden;margin:20px 0 8px}}
.bar>span{{display:block;height:100%;background:var(--brand);border-radius:99px;width:{pct}%}}
.n{{font-family:"IBM Plex Mono",monospace;font-size:12px;color:var(--dim)}}
a{{color:var(--brand);font-weight:600}}
</style></head><body><div class="w">
<div class="eyebrow">Not built yet</div>
<h1>{name}</h1>
<p>{detail}.</p>
<p>This address is reserved. It is built in <strong>wave {wave}</strong>, and this page becomes
the real screen when that lands - the link will not move.</p>
<div class="bar"><span></span></div>
<div class="n">Wave {wave}: {done} of {total} tasks - {pct}%</div>
<p style="margin-top:28px"><a href="/build">See what is built &rarr;</a></p>
</div></body></html>""",
        status_code=200,
    )


@router.get("/admin", response_class=HTMLResponse)
async def admin_placeholder() -> HTMLResponse:
    return _placeholder("/admin")


@router.get("/me", response_class=HTMLResponse)
async def me_placeholder() -> HTMLResponse:
    return _placeholder("/me")


@router.get("/ask", response_class=HTMLResponse)
async def ask_placeholder() -> HTMLResponse:
    return _placeholder("/ask")


@router.get("/login", response_class=HTMLResponse)
async def login_placeholder() -> HTMLResponse:
    return _placeholder("/login")


@router.get("/", response_class=HTMLResponse)
async def root() -> HTMLResponse:
    """The product owns the root. Until it exists, say so and point at what does."""
    s = _read_status()
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Verz Company Brain</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@400;600&family=Poppins:wght@600&display=swap">
<style>
:root{{--brand:#F47936;--ink:#231F20;--ground:#F6F4F1;--panel:#fff;--line:#E3DDD7;--dim:#7A716C}}
@media(prefers-color-scheme:dark){{:root{{--ground:#14110F;--panel:#1D1916;--line:#332C25;--ink:#F5F1ED;--dim:#948A83}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--ground);color:var(--ink);font:15px/1.6 "IBM Plex Sans",system-ui,sans-serif}}
.w{{max-width:680px;margin:0 auto;padding:11vh 22px 70px}}
.eyebrow{{font-family:"IBM Plex Mono",monospace;font-size:10.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--brand)}}
h1{{font-family:Poppins,sans-serif;font-size:36px;margin:8px 0 12px;letter-spacing:-.025em}}
p{{color:var(--dim);max-width:56ch}}
table{{width:100%;border-collapse:collapse;font-size:13.5px;margin:26px 0 10px}}
th{{font-family:"IBM Plex Mono",monospace;font-size:9px;letter-spacing:.1em;text-transform:uppercase;color:var(--dim);text-align:left;padding:6px 10px 5px 0;border-bottom:1px solid var(--line)}}
td{{padding:9px 10px 9px 0;border-bottom:1px solid var(--line)}}
td a{{color:var(--ink);font-weight:600;text-decoration:none}}
td a:hover{{color:var(--brand)}}
.tag{{font-family:"IBM Plex Mono",monospace;font-size:9.5px;padding:2px 6px;border-radius:2px;background:var(--line);color:var(--dim);white-space:nowrap}}
.tag.on{{background:var(--brand);color:#231F20;font-weight:600}}
.n{{font-family:"IBM Plex Mono",monospace;font-size:11px;color:var(--dim);margin-top:26px;border-left:2px solid var(--brand);padding-left:11px;line-height:1.8}}
</style></head><body><div class="w">
<div class="eyebrow">Verz Design</div>
<h1>Company Brain</h1>
<p>The system is being built. These are its addresses - each one already answers, and each
becomes the real screen when its wave lands, so a link you save today keeps working.</p>
<table>
<thead><tr><th>Address</th><th>What it is</th><th>State</th></tr></thead>
<tbody>
<tr><td><a href="/ask">/ask</a></td><td>Ask a question</td><td><span class="tag">wave 2</span></td></tr>
<tr><td><a href="/admin">/admin</a></td><td>Admin console</td><td><span class="tag">wave 3</span></td></tr>
<tr><td><a href="/me">/me</a></td><td>My workspace</td><td><span class="tag">wave 4</span></td></tr>
<tr><td><a href="/login">/login</a></td><td>Sign in</td><td><span class="tag">wave 1</span></td></tr>
<tr><td><a href="/build">/build</a></td><td>Build progress</td><td><span class="tag on">live</span></td></tr>
<tr><td><a href="/build/tracker">/build/tracker</a></td><td>Task tracker</td><td><span class="tag on">live</span></td></tr>
<tr><td><a href="/build/architecture">/build/architecture</a></td><td>Architecture</td><td><span class="tag on">live</span></td></tr>
<tr><td><a href="/build/screens">/build/screens</a></td><td>Key screens (designs)</td><td><span class="tag on">live</span></td></tr>
<tr><td><a href="/build/needs-rupash">/build/needs-rupash</a></td><td>Decisions waiting on you</td><td><span class="tag on">live</span></td></tr>
</tbody></table>
<p class="n">{s.get("done", 0)} of {s.get("total", 0)} tasks built &middot; {s.get("percent", 0)}% &middot; commit {s.get("commit", "?")}<br>
The screens page shows <strong>designs</strong>, not working software. Nothing on this box can
answer a question yet.</p>
</div></body></html>""")


NEEDS_FILE = "needs-rupash.md"


def _needs_count() -> int:
    """How many items are waiting on a decision. Shown on the status page so it is not
    something anyone has to remember to go and look for."""
    path = DOCS / NEEDS_FILE
    if not path.exists():
        return 0
    return sum(
        1 for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("## ")
    )


@router.get("/build/needs-rupash", response_class=HTMLResponse)
async def needs_rupash() -> HTMLResponse:
    """Decisions waiting on a human, rendered from the markdown the repo carries."""
    path = DOCS / NEEDS_FILE
    if not path.exists():
        return HTMLResponse("<h1>Nothing waiting</h1>", status_code=200)
    import html as _html

    raw = path.read_text(encoding="utf-8")
    body: list[str] = []
    in_table = False
    for line in raw.splitlines():
        esc = _html.escape(line)
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells):
                continue
            tag = "th" if not in_table else "td"
            in_table = True
            row = "".join(f"<{tag}>{_html.escape(c)}</{tag}>" for c in cells)
            body.append(f"<tr>{row}</tr>")
            continue
        if in_table:
            body.append("</table>")
            in_table = False
        if line.startswith("## "):
            body.append(f"<h2>{esc[3:]}</h2>")
        elif line.startswith("# "):
            body.append(f"<h1>{esc[2:]}</h1>")
        elif line.startswith("---"):
            body.append("<hr>")
        elif line.strip():
            body.append(f"<p>{esc}</p>")
    if in_table:
        body.append("</table>")
    html_body = "\n".join(body).replace("<tr><th>", "<table><tr><th>")
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Needs Rupash</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@400;600&family=Poppins:wght@600&display=swap">
<style>
:root{{--brand:#F47936;--ink:#231F20;--ground:#F6F4F1;--line:#E3DDD7;--dim:#7A716C}}
@media(prefers-color-scheme:dark){{:root{{--ground:#14110F;--line:#332C25;--ink:#F5F1ED;--dim:#948A83}}}}
body{{margin:0;background:var(--ground);color:var(--ink);font:15px/1.65 "IBM Plex Sans",system-ui,sans-serif}}
.w{{max-width:760px;margin:0 auto;padding:48px 22px 80px}}
h1{{font-family:Poppins,sans-serif;font-size:32px;margin:0 0 10px;letter-spacing:-.02em}}
h2{{font-family:Poppins,sans-serif;font-size:19px;margin:34px 0 8px;letter-spacing:-.01em;border-left:3px solid var(--brand);padding-left:11px}}
p{{color:var(--dim);max-width:64ch}}
hr{{border:0;border-top:1px solid var(--line);margin:26px 0}}
table{{width:100%;border-collapse:collapse;font-size:13px;margin:12px 0 18px}}
th{{font-family:"IBM Plex Mono",monospace;font-size:9px;letter-spacing:.1em;text-transform:uppercase;color:var(--dim);text-align:left;padding:7px 12px 6px 0;border-bottom:1px solid var(--line)}}
td{{padding:8px 12px 8px 0;border-bottom:1px solid var(--line);vertical-align:top;color:var(--ink)}}
a{{color:var(--brand);font-weight:600}}
</style></head><body><div class="w">{html_body}
<p style="margin-top:34px"><a href="/build">&larr; Build progress</a></p>
</div></body></html>""")


@router.get("/api/audit/anchor", response_class=JSONResponse)
async def audit_anchor() -> JSONResponse:
    """The audit ledger's head, for an external anchor to record (M24.1.2).

    Read on a schedule by `.github/workflows/anchor.yml`, which commits it to the
    repository. The direction matters: this endpoint is read, never a push. Having the
    server write its own anchor to an external store would need a write credential on the
    one machine the anchor exists to be independent of, and then whoever could delete audit
    entries could also write an anchor agreeing with the deletion.

    **It returns a digest and a length and nothing else.** Not an entry, not an actor, not
    an action. A reader learns that the ledger exists and how long it is, which is the
    minimum that makes an anchor work, and is why this can sit beside the other unauthenticated
    build routes rather than behind the gate.

    The ledger table exists and nothing writes to it yet, so today this reports an empty
    chain. That is deliberately not an error: an anchor taken before the first entry proves
    the ledger started empty on that date, which is what makes "there were no entries before
    Tuesday" checkable rather than assertable.
    """
    from datetime import UTC, datetime

    from brain.audit.anchor import take_anchor
    from brain.audit.ledger import AuditChain

    anchor = take_anchor(AuditChain(), name="main", now=datetime.now(UTC))
    return JSONResponse(anchor.to_public(), headers={"cache-control": "no-store"})
