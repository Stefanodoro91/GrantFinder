"""Enti che bloccano le pagine: esistono feed RSS/Atom o API pubbliche? Prova dei percorsi standard con lo User-Agent del bot."""
import json, re, sys, importlib.util, urllib.robotparser
spec = importlib.util.spec_from_file_location("ng", "neurogrant.py"); ng = importlib.util.module_from_spec(spec)
sys.argv = ["ng"]; spec.loader.exec_module(ng)
http = ng.Http(pausa=0.5)
SITI = {"NIHR": "https://www.nihr.ac.uk", "Stroke Association": "https://www.stroke.org.uk", "American Heart Association": "https://professional.heart.org",
        "ZonMw": "https://www.zonmw.nl", "HRB Ireland": "https://www.hrb.ie", "HFSP": "https://www.hfsp.org", "Cure Parkinson's": "https://cureparkinsons.org.uk",
        "ANID": "https://anid.cl", "GrantConnect": "https://www.grants.gov.au", "fundsforNGOs": "https://www2.fundsforngos.org", "la Caixa": "https://fundacionlacaixa.org",
        "NWO": "https://www.nwo.nl", "Swiss Heart": "https://swissheart.ch", "FFG": "https://www.ffg.at", "MND Association": "https://www.mndassociation.org",
        "Muscular Dystrophy UK": "https://www.musculardystrophyuk.org", "Academy of Medical Sciences": "https://acmedsci.ac.uk", "Epilepsy Research Institute": "https://epilepsy-institute.org.uk",
        "Fiocruz": "https://portal.fiocruz.br", "CONACYT Paraguay": "https://www.conacyt.gov.py", "BID Lab": "https://bidlab.org", "Argentina Salud": "https://www.argentina.gob.ar",
        "Ospedale San Raffaele": "https://www.hsr.it", "EURAXESS": "https://euraxess.ec.europa.eu", "AcademicPositions": "https://academicpositions.com",
        "MUR": "https://www.mur.gov.it", "University Affairs": "https://universityaffairs.ca", "NYU IFP (aggregatore)": "https://ifp.nyu.edu"}
PERCORSI = ["/feed/", "/rss", "/rss.xml", "/feed.xml", "/feeds", "/atom.xml", "/index.xml", "/?feed=rss2", "/news/rss", "/rss/news", "/funding/feed/",
            "/en/rss", "/it/rss", "/jobs/rss", "/funding/rss", "/sitemap.xml"]
out = []
for nome, base in SITI.items():
    righe = []
    for p in PERCORSI:
        u = base + p
        try:
            r = http.s.get(u, timeout=25, allow_redirects=True)
            ct = r.headers.get("content-type", "").lower(); t = r.text[:200000]
            xml = t.lstrip()[:400].lower()
            is_feed = r.status_code == 200 and ("<rss" in xml or "<feed" in xml or ("xml" in ct and ("<item" in t or "<entry" in t)))
            n = len(re.findall(r"<item[\s>]|<entry[\s>]", t)) if is_feed else 0
            titoli = re.findall(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", t)[1:5] if is_feed else []
            sm = r.status_code == 200 and "<urlset" in xml
            righe.append({"url": u, "status": r.status_code, "feed": is_feed, "voci": n, "titoli": titoli, "sitemap": sm,
                          "sitemap_urls": len(re.findall(r"<loc>", t)) if sm else 0})
        except Exception as e:
            righe.append({"url": u, "status": str(e)[:60]})
    rob = None
    try:
        rr = http.s.get(base + "/robots.txt", timeout=20); rob = rr.status_code
        feeds = re.findall(r"(?i)sitemap:\s*(\S+)", rr.text)
    except Exception:
        feeds = []
    ok = [x for x in righe if x.get("feed")]
    out.append({"ente": nome, "base": base, "home": next((x["status"] for x in righe if x["url"].endswith("/feed/")), None), "robots": rob,
                "sitemap_in_robots": feeds[:3], "feed": ok, "sitemap": [x for x in righe if x.get("sitemap")]})
    print(nome, len(ok), flush=True)
json.dump(out, open("probe-out/feeds.json", "w"), indent=1, ensure_ascii=False)
