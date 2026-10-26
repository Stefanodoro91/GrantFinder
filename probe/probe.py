"""Prova delle fonti candidate (parte 0/1: bandi; 2: posizioni + debug ERA-LEARN). Risultati in probe-out/."""
import csv, json, sys, time, re, importlib.util, os
part = int(sys.argv[1])
spec = importlib.util.spec_from_file_location("ng", "neurogrant.py"); ng = importlib.util.module_from_spec(spec)
sys.argv = ["ng"]; spec.loader.exec_module(ng)
http = ng.Http(pausa=1.0)
out = []
if part in (0, 1):
    rows = list(csv.DictReader(open("probe/grant_candidates.csv", encoding="utf-8"), delimiter=";"))
    rows = rows[part::2]
    for row in rows:
        t0 = time.time(); info = {"nome": row["nome"], "url": row["url"]}
        try:
            r = http.get(row["url"], tentativi=1, timeout=40)
            info.update(status=r.status_code, final_url=r.url, len=len(r.text))
        except Exception as e:
            info["status"] = f"ERR {e}"[:200]
        try:
            info["robots_ok"] = http.consentito(row["url"])
            recs, n, err = ng.esplora_fonte(http, row, 20)
            info.update(pagine=n, errore=err, candidati=[{"titolo": x["titolo"][:120], "url": x["url"], "scadenza": x["scadenza"],
                        "gruppi": x["macrogruppi"]} for x in recs])
        except Exception as e:
            info["errore"] = str(e)[:300]
        info["sec"] = round(time.time() - t0); out.append(info)
        print(info["nome"], info.get("status"), info.get("pagine"), len(info.get("candidati", [])), flush=True)
else:
    os.system("git clone -q --depth 1 https://github.com/Stefanodoro91/Clinical-Chaos /tmp/cc && pip install -q -r /tmp/cc/requirements.txt")
    sys.path.insert(0, "/tmp/cc"); os.chdir("/tmp/cc")
    import positions, requests
    for row in csv.DictReader(open(os.path.join(os.environ["GITHUB_WORKSPACE"], "probe/pos_candidates.csv"), encoding="utf-8"), delimiter=";"):
        info = {"nome": row["nome"], "url": row["url"]}
        try:
            r = requests.get(row["url"], headers=positions.HEADERS, timeout=45)
            info.update(status=r.status_code, len=len(r.text), final_url=r.url)
            items = positions._html_items(r.text, row["url"]) if r.ok else []
            info["link_posizioni"] = len(items)
            found, rep = positions.collect([row], guided=False)
            info["tenuti"] = len(found); info["report"] = rep
            info["esempi"] = [(v["title"][:90], v["url"][:120]) for v in list(found.values())[:6]]
            info["esempi_tutti"] = [(i["title"][:90]) for i in items[:5]]
        except Exception as e:
            info["errore"] = str(e)[:200]
        out.append(info); print(info["nome"], info.get("status"), info.get("link_posizioni"), info.get("tenuti"), flush=True)
    os.chdir(os.environ["GITHUB_WORKSPACE"])
    # debug ERA-LEARN: perché le pagine delle call non passano
    from bs4 import BeautifulSoup
    for u in ["https://www.era-learn.eu/network-information/networks/rare-diseases/erdera-clinical-trial-call-2026-pre-announcement",
              "https://www.era-learn.eu/network-information/networks/innovative-health-initiative/ihi-call-13"]:
        r = http.get(u); soup = BeautifulSoup(r.text, "lxml")
        h1 = soup.find("h1").get_text(" ", strip=True) if soup.find("h1") else ""
        for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "form"]): tag.decompose()
        testo = re.sub(r"\s+", " ", soup.get_text(" ")).strip()
        out.append({"debug": u, "h1": h1, "len": len(testo), "testo": testo[:3500],
                    "scad": str(ng.scadenza_da_testo(testo[:30000])), "pagina_bando": bool(ng.RX_PAGINA_BANDO.search(h1) or ng.RX_PAGINA_BANDO.search(u) or ng.RX_PAGINA_BANDO.search(testo[:3000])),
                    "aperto": bool(ng.RX_APERTO.search(testo[:30000])), "classifica": ng.classifica(h1, testo[:20000])})
os.makedirs("probe-out", exist_ok=True)
json.dump(out, open(f"probe-out/parte{part}.json", "w"), indent=1, ensure_ascii=False)
