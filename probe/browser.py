"""Lettura con browser (Chromium headless) dei portali costruiti con JavaScript: link di posizioni prima e dopo il rendering."""
import csv, json, os, sys, time, urllib.robotparser
from urllib.parse import urlparse
os.system("git clone -q --depth 1 https://github.com/Stefanodoro91/Clinical-Chaos /tmp/cc && pip install -q -r /tmp/cc/requirements.txt")
WS = os.getcwd(); sys.path.insert(0, "/tmp/cc"); os.chdir("/tmp/cc")
import positions, requests
from playwright.sync_api import sync_playwright
UA = positions.HEADERS.get("User-Agent", "")
out = []
rows = list(csv.DictReader(open(os.path.join(WS, "probe/browser_candidates.csv"), encoding="utf-8"), delimiter=";"))
with sync_playwright() as p:
    br = p.chromium.launch()
    ctx = br.new_context(user_agent=UA, locale="en-GB")
    for row in rows:
        url = row["url"]; info = {"nome": row["nome"], "url": url}
        try:
            rp = urllib.robotparser.RobotFileParser(); u = urlparse(url)
            r = requests.get(f"{u.scheme}://{u.netloc}/robots.txt", headers=positions.HEADERS, timeout=20)
            rp.parse(r.text.splitlines() if r.status_code == 200 else []); info["robots_ok"] = rp.can_fetch(UA, url)
        except Exception as e:
            info["robots_ok"] = f"? {e}"[:80]
        try:
            r = requests.get(url, headers=positions.HEADERS, timeout=40)
            info["static_status"] = r.status_code; info["static_links"] = len(positions._html_items(r.text, url))
        except Exception as e:
            info["static_status"] = str(e)[:80]
        t0 = time.time()
        try:
            pg = ctx.new_page()
            resp = pg.goto(url, wait_until="domcontentloaded", timeout=45000)
            try: pg.wait_for_load_state("networkidle", timeout=20000)
            except Exception: pass
            for _ in range(3):
                pg.mouse.wheel(0, 4000); pg.wait_for_timeout(800)
            html = pg.content(); final = pg.url
            # anche i link verso altri siti (portali di candidatura esterni) e il contenuto degli iframe
            anchors = []
            for fr in pg.frames:
                try:
                    anchors += fr.eval_on_selector_all("a[href]", "els => els.map(e => [e.href, (e.innerText||'').trim()])")
                except Exception:
                    pass
            ext = [(h, t_) for h, t_ in anchors if 12 <= len(t_) <= 220 and positions.RX_POSITION.search(t_)]
            info["any_host_links"] = len({h for h, _ in ext})
            info["hosts"] = sorted({urlparse(h).netloc for h, _ in ext})[:6]
            info["frames"] = len(pg.frames)
            info["esempi_ext"] = [t_[:80] for _, t_ in ext[:4]]
            import re as _re
            info["link_bandi"] = sorted({h for h, t_ in anchors if _re.search(r"band|concors|lavora|career|job|assegn|borse|selezion|opportunit", (h + " " + t_).lower())})[:25]
            info.update(browser_status=resp.status if resp else None, final_url=final, html_len=len(html))
            items = positions._html_items(html, final)
            info["browser_links"] = len(items)
            src = {"nome": row["nome"], "url": final, "tipo": "html"}
            topic = bool(positions.RX_REHAB.search(url))
            kept = [it for it in items if positions.RX_POSITION.search(it["title"]) and (topic or positions.RX_REHAB.search(it["title"]))]
            info["browser_kept"] = len(kept)
            info["esempi"] = [i["title"][:90] for i in (kept or items)[:6]]
            pg.close()
        except Exception as e:
            info["browser_err"] = str(e)[:200]
        info["sec"] = round(time.time() - t0)
        out.append(info); print(info["nome"], info.get("static_links"), "->", info.get("browser_links"), info.get("browser_kept"), info.get("robots_ok"), flush=True)
    br.close()
os.chdir(WS); os.makedirs("probe-out", exist_ok=True)
json.dump(out, open("probe-out/browser.json", "w"), indent=1, ensure_ascii=False)
