"""Prova di sola lettura: per ogni fonte in errore, prova gli indirizzi candidati con requests (UA del bot) e con un
browser vero (Chromium headless, UA predefinito). Non scrive nulla, non usa Gemini."""
import re, sys, time
sys.path.insert(0, ".")
import neurogrant as ng
from bs4 import BeautifulSoup
from urllib.parse import urljoin

CAND = {
 "NHMRC": ["https://www.nhmrc.gov.au/funding", "https://www.nhmrc.gov.au/funding/calendar"],
 "MRFF": ["https://www.health.gov.au/initiatives-and-programs/medical-research-future-fund/mrff-grant-opportunities-calendar", "https://www.health.gov.au/our-work/mrff"],
 "NAFOSTED": ["https://nafosted.gov.vn/", "https://nafosted.gov.vn/en/?p=6969"],
 "BMFTR": ["https://www.gesundheitsforschung-bmftr.de/"],
 "proposalCENTRAL": ["https://proposalcentral.com/GrantOpportunities.asp"],
 "HRB": ["https://grants.hrb.ie/", "https://www.hrb.ie/"],
 "ANID": ["https://anid.cl/feed/", "https://anid.cl/concursos/feed/"],
 "NIHR": ["https://www.nihr.ac.uk/feed", "https://www.nihr.ac.uk/rss.xml"],
 "SAMRC": ["https://www.samrc.ac.za/"],
 "Lundbeck": ["https://lundbeckfonden.com/en"],
 "FWO": ["https://www.fwo.be/en/"],
 "NKFIH": ["https://nkfih.gov.hu/english/funding"],
}
SHOW = {"BMFTR", "SAMRC", "Lundbeck", "FWO", "NKFIH", "NAFOSTED", "proposalCENTRAL"}
RX_CHALLENGE = re.compile(r"captcha|verify you are human|are you a robot|cf-chl|just a moment|access denied|attention required|request unsuccessful|incapsula", re.I)

def links(html, base):
    soup = BeautifulSoup(html, "lxml")
    out = set()
    for a in soup.find_all("a", href=True):
        h = urljoin(base, a["href"]); t = a.get_text(" ", strip=True)
        if h.startswith("http") and ng._stesso_sito(base, h) and (ng.RX_LINK_BANDO.search(t) or ng.RX_LINK_BANDO.search(h)):
            out.add(h)
    txt = re.sub(r"\s+", " ", soup.get_text(" "))
    links.last = sorted(out)
    return len(out), len(txt), (soup.title.get_text(strip=True) if soup.title else "")[:60], txt

http = ng.Http(pausa=1)
from playwright.sync_api import sync_playwright
pw = sync_playwright().start(); br = pw.chromium.launch(args=["--disable-http2"]); ctx = br.new_context(locale="en-GB")
print("UA browser:", ctx.new_page().evaluate("navigator.userAgent"))
for nome, urls in CAND.items():
    for u in urls:
        rob = http.consentito(u)
        try:
            r = http.get(u, tentativi=1, timeout=30)
            a = f"req {r.status_code}"
            if r.status_code < 400 and "html" in r.headers.get("content-type", ""):
                n, l, t, _ = links(r.text, r.url); a += f" link={n} testo={l}"
        except Exception as e:
            a = f"req ERR {str(e)[:60]}"
        pg = ctx.new_page()
        try:
            resp = pg.goto(u, wait_until="domcontentloaded", timeout=45000)
            try: pg.wait_for_load_state("networkidle", timeout=15000)
            except Exception: pass
            html = pg.content(); n, l, t, txt = links(html, pg.url)
            ch = " CHALLENGE" if RX_CHALLENGE.search(txt[:3000]) and l < 3000 else ""
            b = f"br {resp.status if resp else '?'} link={n} testo={l} '{t}'{ch} -> {pg.url[:80]}"
        except Exception as e:
            b = f"br ERR {str(e).splitlines()[0][:80]}"
        finally:
            pg.close()
        if nome in SHOW and getattr(links, "last", None):
            print("   link:", " ".join(links.last[:25]), flush=True)
        print(f"{nome[:34]:34} | robots={'ok' if rob else 'NO'} | {u[:70]:70} | {a:32} | {b}", flush=True)
br.close(); pw.stop()
