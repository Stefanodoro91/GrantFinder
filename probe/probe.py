"""Prova delle fonti candidate con il lettore di GrantFinder; risultati in probe-out/."""
import csv, json, sys, time, collections, importlib.util
spec = importlib.util.spec_from_file_location("ng", "neurogrant.py"); ng = importlib.util.module_from_spec(spec)
sys.argv = ["ng"]; spec.loader.exec_module(ng)
http = ng.Http(pausa=1.0)
out = {"web": [], "eu": {}}
for row in csv.DictReader(open("probe/candidati.csv", encoding="utf-8"), delimiter=";"):
    url = row["url"]; t0 = time.time()
    info = {"nome": row["nome"], "url": url}
    try:
        r = http.get(url, tentativi=1, timeout=40)
        info["status"] = r.status_code; info["final_url"] = r.url; info["len"] = len(r.text)
    except Exception as e:
        info["status"] = f"ERR {e}"[:200]
    try:
        info["robots_ok"] = http.consentito(url)
        recs, n, err = ng.esplora_fonte(http, {"nome": row["nome"], "url": url, "copertura": "Europa", "italia": "si"}, 25)
        info.update(pagine=n, errore=err, candidati=[{"titolo": x["titolo"][:120], "url": x["url"], "scadenza": x["scadenza"],
                    "gruppi": x["macrogruppi"]} for x in recs])
    except Exception as e:
        info["errore"] = str(e)[:300]
    info["sec"] = round(time.time() - t0)
    out["web"].append(info); print(info["nome"], info.get("status"), info.get("pagine"), len(info.get("candidati", [])), flush=True)
recs, n, err = ng.esplora_eralearn(http)
out["eralearn"] = {"pagine": n, "errore": err, "candidati": [{"titolo": x["titolo"][:120], "url": x["url"], "scadenza": x["scadenza"], "gruppi": x["macrogruppi"], "stato": x["stato"]} for x in recs]}
print("ERA-LEARN", n, err, len(recs), flush=True)
from bs4 import BeautifulSoup
r = http.get(ng.ERA_LEARN); soup = BeautifulSoup(r.text, "lxml")
out["eralearn"]["link_networks"] = sorted({a["href"] for a in soup.find_all("a", href=True) if "/networks/" in a["href"]})[:120]
# EU: tutti i topic aperti/in apertura, per prefisso, e quelli pertinenti col nuovo filtro
ALL = []
_orig = ng.converti_eu
def _wrap(v):
    m = v.get("metadata") or {}
    ALL.append(str(ng._meta(m, "identifier") or v.get("reference")))
    return _orig(v)
ng.converti_eu = _wrap
recs, esam = ng.scarica_eu(http)
pref = collections.Counter("-".join(i.split("-")[:2]) for i in ALL)
out["eu"]["prefissi"] = pref.most_common(80)
out["eu"]["erc_msca_eic_miss_ihi"] = sorted({i for i in ALL if any(k in i for k in ("ERC", "MSCA", "EIC", "MISS", "IHI", "EU4H"))})
out["eu"]["esaminati"] = esam
out["eu"]["pertinenti"] = [{"id": r["id"], "titolo": r["titolo"][:110], "gruppi": r["macrogruppi"], "scadenza": r["scadenza"], "stato": r["stato"]} for r in recs]
json.dump(out, open("probe-out/risultati.json", "w"), indent=1, ensure_ascii=False)
print("EU pertinenti:", len(recs), "di", esam)
