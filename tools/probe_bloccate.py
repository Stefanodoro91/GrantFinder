"""Prova di sola lettura: esplora_fonte (con il ripiego del browser) sulle fonti che erano in errore. Niente Gemini."""
import sys, time
sys.path.insert(0, ".")
import neurogrant as ng
NOMI = ["NIHR", "Stroke Association", "American Heart Association – Research", "NHMRC", "BMBF – Gesundheitsforschung (bandi)",
        "ZonMw", "FWO – Research Foundation Flanders", "Health Research Board Ireland", "Lundbeck Foundation",
        "Human Frontier Science Program", "Chan Zuckerberg Initiative", "Cure Parkinson's", "ANID Chile",
        "SAMRC – South African Medical Research Council", "MRFF – Medical Research Future Fund",
        "GrantConnect – Australian Government grants", "DOST-PCHRD Philippines – Calls", "NAFOSTED Vietnam",
        "Fundação Calouste Gulbenkian – Concursos", "NKFIH – Hungary"]
fonti = {f["nome"]: f for f in ng.leggi_fonti_web(ng.BASE / "fonti_web.csv")}
http = ng.Http(pausa=1)
for n in NOMI:
    f = fonti.get(n)
    if not f:
        print(f"{n:45} | NON NEL CSV"); continue
    t = time.time()
    try:
        recs, vis, err = ng.esplora_fonte(http, f, 15)
    except Exception as e:
        recs, vis, err = [], 0, f"eccezione {e}"
    print(f"{n[:45]:45} | pagine={vis:3} candidati={len(recs):3} | {int(time.time()-t):4}s | {err or 'OK'}", flush=True)
    for r in recs[:4]:
        print("      -", (r.get("titolo") or "")[:90], "|", r.get("url", "")[:90], flush=True)
ng.BROWSER.chiudi()
