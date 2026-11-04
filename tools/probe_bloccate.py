"""Prova di sola lettura: per ogni fonte in errore, prova gli indirizzi candidati con requests (UA del bot) e con un
browser vero (Chromium headless, UA predefinito). Non scrive nulla, non usa Gemini."""
import re, sys, time
sys.path.insert(0, ".")
import neurogrant as ng
from bs4 import BeautifulSoup
from urllib.parse import urljoin

CAND = {
 "NIHR": ["https://www.nihr.ac.uk/", "https://www.nihr.ac.uk/researchers/funding-opportunities", "https://www.nihr.ac.uk/funding-opportunities"],
 "Stroke Association": ["https://www.stroke.org.uk/", "https://www.stroke.org.uk/research/funding", "https://www.stroke.org.uk/research"],
 "American Heart Association – Research": ["https://professional.heart.org/", "https://professional.heart.org/en/research-programs/aha-funding-opportunities"],
 "NHMRC": ["https://www.nhmrc.gov.au/funding", "https://www.nhmrc.gov.au/funding/calendar"],
 "BMBF – Gesundheitsforschung (bandi)": ["https://www.gesundheitsforschung-bmbf.de/de/bekanntmachungen.php", "https://www.gesundheitsforschung-bmftr.de/de/bekanntmachungen.php", "https://www.gesundheitsforschung-bmbf.de/", "https://www.bmftr.bund.de/DE/Service/Bekanntmachungen/bekanntmachungen_node.html"],
 "ZonMw": ["https://www.zonmw.nl/en", "https://www.zonmw.nl/en/subsidies", "https://www.zonmw.nl/nl/subsidies"],
 "FWO – Research Foundation Flanders": ["https://www.fwo.be/en/fellowships-funding/", "https://www.fwo.be/en/", "https://www.fwo.be/"],
 "Health Research Board Ireland": ["https://www.hrb.ie/funding/", "https://www.hrb.ie/funding/funding-schemes/"],
 "Lundbeck Foundation": ["https://lundbeckfonden.com/en/research", "https://lundbeckfonden.com/en/grants", "https://lundbeckfonden.com/en"],
 "Human Frontier Science Program": ["https://www.hfsp.org/funding", "https://www.hfsp.org/funding/research-grants"],
 "Chan Zuckerberg Initiative": ["https://chanzuckerberg.com/rfa/", "https://apply.chanzuckerberg.com/", "https://chanzuckerberg.com/science/programs-resources/"],
 "Cure Parkinson's": ["https://cureparkinsons.org.uk/research/", "https://cureparkinsons.org.uk/research/apply-for-funding/"],
 "ANID Chile": ["https://anid.cl/concursos/", "https://www.anid.cl/concursos/"],
 "SAMRC – South African Medical Research Council": ["https://www.samrc.ac.za/funding", "https://www.samrc.ac.za/funding/funding-opportunities", "https://www.samrc.ac.za/"],
 "MRFF – Medical Research Future Fund": ["https://www.health.gov.au/initiatives-and-programs/medical-research-future-fund/mrff-grant-opportunities-calendar", "https://www.health.gov.au/our-work/mrff"],
 "GrantConnect – Australian Government grants": ["https://www.grants.gov.au/Go/List", "https://www.grants.gov.au/"],
 "DOST-PCHRD Philippines – Calls": ["https://www.pchrd.dost.gov.ph/calls_and_events/", "https://www.pchrd.dost.gov.ph/"],
 "fundsforNGOs – Health": ["https://www2.fundsforngos.org/category/health/"],
 "NAFOSTED Vietnam": ["https://nafosted.gov.vn/en/", "https://nafosted.gov.vn/en/chuyen-muc/news-events/funding-announcement"],
 "Fundação Calouste Gulbenkian – Concursos": ["https://gulbenkian.pt/en/grants/", "https://gulbenkian.pt/en/"],
 "NKFIH – Hungary": ["https://nkfih.gov.hu/english", "https://nkfih.gov.hu/english/funding"],
}
RX_CHALLENGE = re.compile(r"captcha|verify you are human|are you a robot|cf-chl|just a moment|access denied|attention required|request unsuccessful|incapsula", re.I)

def links(html, base):
    soup = BeautifulSoup(html, "lxml")
    out = set()
    for a in soup.find_all("a", href=True):
        h = urljoin(base, a["href"]); t = a.get_text(" ", strip=True)
        if h.startswith("http") and ng._stesso_sito(base, h) and (ng.RX_LINK_BANDO.search(t) or ng.RX_LINK_BANDO.search(h)):
            out.add(h)
    txt = re.sub(r"\s+", " ", soup.get_text(" "))
    return len(out), len(txt), (soup.title.get_text(strip=True) if soup.title else "")[:60], txt

http = ng.Http(pausa=1)
from playwright.sync_api import sync_playwright
pw = sync_playwright().start(); br = pw.chromium.launch(); ctx = br.new_context(locale="en-GB")
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
        print(f"{nome[:34]:34} | robots={'ok' if rob else 'NO'} | {u[:70]:70} | {a:32} | {b}", flush=True)
br.close(); pw.stop()
