#!/usr/bin/env python3
"""
NeuroGrant – monitoraggio settimanale dei bandi per la riabilitazione
(ictus, Parkinson, invecchiamento, tecnologie per la riabilitazione).

Fonti:
  * EU Funding & Tenders Portal (tutti i programmi UE) – API pubblica
  * Grants.gov (tutte le agenzie federali USA, incluso NIH) – API pubblica
  * Siti web di ministeri, regioni, fondazioni bancarie, charity e
    fondazioni internazionali elencati in fonti_web.csv – lettura delle pagine

Flusso:
  1. raccoglie i bandi aperti o in apertura;
  2. tiene quelli pertinenti alla riabilitazione (filtro ampio a parole chiave);
  3. con un LLM gratuito in locale (Ollama) legge il testo della call, giudica
     la pertinenza e scrive titolo e sommario in italiano;
  4. calcola copertura finanziaria, progetti finanziabili e competitivita';
  5. genera l'Excel (fogli BANDI, FONTI, LOG) e lo invia per email.

Uso locale:   pip install -r requirements.txt && python neurogrant.py --no-email
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import logging
import math
import os
import re
import smtplib
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag
from urllib.robotparser import RobotFileParser

import requests

log = logging.getLogger("neurogrant")
BASE = Path(__file__).resolve().parent
OGGI = date.today()

USER_AGENT = ("NeuroGrantBot/0.1 (monitoraggio bandi di ricerca sulla riabilitazione; "
              "rispetta robots.txt)")

# ---------------------------------------------------------------------------
# Filtro tematico (volutamente ampio: meglio un falso positivo che un bando perso)
# Ogni voce: (gruppo, espressione regolare, specifica)
# Le voci non specifiche contano solo se il testo ha anche un contesto sanitario.
# ---------------------------------------------------------------------------

TERMINI = [
    # Ictus
    ("Ictus", r"\bstrokes?\b(?!\s+(of|count|width|engine))", True),
    ("Ictus", r"\bictus\b", True),
    ("Ictus", r"cerebrovascula|cerebrovascolar", True),
    ("Ictus", r"\baphasia|\bafasia", True),
    ("Ictus", r"hemipare|emipare|hemipleg|emipleg", True),
    ("Ictus", r"(acquired|traumatic) brain injur|cerebrolesion|grave cerebrolesione", True),
    # Parkinson
    ("Parkinson", r"parkinson", True),
    ("Parkinson", r"movement disorder|disturbi del movimento", True),
    ("Parkinson", r"deep brain stimulation|stimolazione cerebrale profonda", True),
    # Invecchiamento
    ("Invecchiamento", r"\bdementi|\bdemenz", True),
    ("Invecchiamento", r"alzheimer", True),
    ("Invecchiamento", r"cognitive (decline|impairment)|declino cognitivo|decadimento cognitivo", True),
    ("Invecchiamento", r"healthy (ageing|aging)|active (and|&) assisted living|invecchiamento attivo", True),
    ("Invecchiamento", r"older (adults|people|persons|population)|elderly|anzian|terza eta", True),
    ("Invecchiamento", r"\bfrailty|fragilita", True),
    ("Invecchiamento", r"geriatr|gerontolog", True),
    ("Invecchiamento", r"\b(ageing|aging)\b|invecchiamento|longevit", False),
    ("Invecchiamento", r"falls? prevention|prevenzione (delle )?cadute", True),
    ("Invecchiamento", r"neurodegenerat", True),
    # Riabilitazione (generale)
    ("Riabilitazione", r"rehabilitat|riabilitaz|\brehab\b", True),
    ("Riabilitazione", r"physiotherap|fisioterap|physical therap", True),
    ("Riabilitazione", r"occupational therap|terapia occupazionale|ergoterap", True),
    ("Riabilitazione", r"motor (recovery|function|learning)|recupero (motorio|funzionale)", True),
    ("Riabilitazione", r"speech (and language )?therap|logoped", True),
    ("Riabilitazione", r"\bdisabilit|disabled persons|persons with disabilities", False),
    ("Riabilitazione", r"functional (recovery|independence)|autonomia funzionale", False),
    # Tecnologie per la riabilitazione (contano solo con contesto sanitario)
    ("Tecnologie riabilitazione", r"assistive (technolog|device)|tecnologie assistive|\bausili", True),
    ("Tecnologie riabilitazione", r"exoskeleton|esoschelet", True),
    ("Tecnologie riabilitazione", r"neuroprosthe|neuroprotes|\bprosthe|\bprotesi", False),
    ("Tecnologie riabilitazione", r"brain[- ]computer interface|\bbci\b|brain[- ]machine interface", True),
    ("Tecnologie riabilitazione", r"tele-?rehabilit|teleriabilit", True),
    ("Tecnologie riabilitazione", r"\brobot", False),
    ("Tecnologie riabilitazione", r"wearable|indossabil", False),
    ("Tecnologie riabilitazione", r"virtual reality|realta virtuale|exergam|serious game", False),
    ("Tecnologie riabilitazione", r"neuromodulat|neurostimulat|\btms\b|\btdcs\b|transcranial", False),
    ("Tecnologie riabilitazione", r"functional electrical stimulation|elettrostimolazione funzionale", True),
    ("Tecnologie riabilitazione", r"digital therapeut|terapie digitali", False),
    ("Tecnologie riabilitazione", r"\bgait\b|mobility aid|\bdeambulaz", False),
    ("Tecnologie riabilitazione", r"smart home|domotica|ambient assisted", False),
]
_TERMINI_C = [(g, re.compile(p, re.I), s) for g, p, s in TERMINI]
CONTESTO_SANITARIO = re.compile(
    r"\b(health|healthcare|clinical|clinic|patient|medical|medicine|disease|disorder|"
    r"therap|hospital|neurolog|brain|salute|sanitari|pazient|medic|malatti|cura\b|care\b|"
    r"ospedal|cervello|disabil|older|elderly|anzian|wellbeing|benessere)", re.I)

GRUPPI_ORDINE = ["Ictus", "Parkinson", "Invecchiamento", "Tecnologie riabilitazione", "Riabilitazione"]


def norm(t: str | None) -> str:
    if not t:
        return ""
    t = unicodedata.normalize("NFKD", t)
    return "".join(c for c in t if not unicodedata.combining(c))


def classifica(testo: str) -> tuple[list[str], list[str]]:
    """Restituisce (macrogruppi, parole chiave trovate)."""
    t = norm(testo)
    contesto = bool(CONTESTO_SANITARIO.search(t))
    gruppi, parole = set(), []
    for gruppo, rx, specifica in _TERMINI_C:
        m = rx.search(t)
        if m and (specifica or contesto):
            gruppi.add(gruppo)
            parola = m.group(0).strip().lower()
            if parola not in parole:
                parole.append(parola)
    # "Riabilitazione" generica solo se non c'e' gia' un gruppo piu' specifico
    if len(gruppi) > 1 and "Riabilitazione" in gruppi:
        gruppi.discard("Riabilitazione")
    return [g for g in GRUPPI_ORDINE if g in gruppi], parole[:12]


# ---------------------------------------------------------------------------
# Utilita' HTTP, testo e date
# ---------------------------------------------------------------------------

class Http:
    def __init__(self, pausa: float = 1.0):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": USER_AGENT,
                               "Accept-Language": "it,en;q=0.8"})
        self.pausa = pausa
        self._ultimo: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}

    def _attendi(self, url: str):
        dom = urlparse(url).netloc
        trascorso = time.time() - self._ultimo.get(dom, 0)
        if trascorso < self.pausa:
            time.sleep(self.pausa - trascorso)
        self._ultimo[dom] = time.time()

    def consentito(self, url: str) -> bool:
        p = urlparse(url)
        radice = f"{p.scheme}://{p.netloc}"
        if radice not in self._robots:
            rp = RobotFileParser()
            try:
                r = self.s.get(radice + "/robots.txt", timeout=20)
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except requests.RequestException:
                rp.parse([])
            self._robots[radice] = rp
        rp = self._robots[radice]
        return rp is None or rp.can_fetch(USER_AGENT, url)

    def get(self, url, tentativi=3, cortese=False, **kw) -> requests.Response:
        for i in range(tentativi):
            if cortese:
                self._attendi(url)
            try:
                r = self.s.get(url, timeout=kw.pop("timeout", 45), **kw)
                if r.status_code in (429, 500, 502, 503, 504):
                    raise requests.HTTPError(f"HTTP {r.status_code}")
                return r
            except requests.RequestException as e:
                if i == tentativi - 1:
                    raise
                time.sleep(2 ** (i + 1))
        raise RuntimeError("irraggiungibile")

    def post(self, url, tentativi=3, **kw) -> requests.Response:
        for i in range(tentativi):
            try:
                r = self.s.post(url, timeout=kw.pop("timeout", 60), **kw)
                if r.status_code in (429, 500, 502, 503, 504):
                    raise requests.HTTPError(f"HTTP {r.status_code}")
                r.raise_for_status()
                return r
            except requests.RequestException:
                if i == tentativi - 1:
                    raise
                time.sleep(2 ** (i + 1))
        raise RuntimeError("irraggiungibile")


def html_a_testo(h: str | None) -> str:
    if not h:
        return ""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(h, "lxml")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        testo = soup.get_text(" ")
    except Exception:
        testo = re.sub(r"<[^>]+>", " ", h)
    return re.sub(r"\s+", " ", html.unescape(testo)).strip()


def numero(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if v > 0 else None
    s = re.sub(r"[^\d.,]", "", str(v))
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    elif s.count(",") >= 1 and len(s.split(",")[-1]) == 3:
        s = s.replace(",", "")
    elif s.count(".") > 1:
        s = s.replace(".", "")
    s = s.replace(",", ".")
    try:
        x = float(s)
        return x if x > 0 else None
    except ValueError:
        return None


MESI = {
    "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4, "maggio": 5, "giugno": 6, "luglio": 7,
    "agosto": 8, "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
    "oct": 10, "nov": 11, "dec": 12, "gen": 1, "mag": 5, "giu": 6, "lug": 7, "ago": 8,
    "set": 9, "ott": 10, "dic": 12,
}
_M = "|".join(sorted(MESI, key=len, reverse=True))
RX_DATE = [
    (re.compile(r"\b(20\d\d)-(\d{1,2})-(\d{1,2})"), "ymd"),
    (re.compile(r"\b(\d{1,2})[/.](\d{1,2})[/.](20\d\d)\b"), "dmy"),
    (re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th|°)?\s+({_M})\.?,?\s+(20\d\d)\b", re.I), "d_mese_y"),
    (re.compile(rf"\b({_M})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(20\d\d)\b", re.I), "mese_d_y"),
]
RX_SCADENZA = re.compile(
    r"(scadenz|deadline|closing date|close date|due date|entro (il|e non oltre)|termine (ultimo|per la presentazione)|"
    r"submission|apply by|applications? (close|due)|clos(ed|es|ing)|chiusura|chius[oa]|scadut|until|fino al)", re.I)


def data_da_testo(s: str, formato_usa: bool = False) -> date | None:
    for rx, tipo in RX_DATE:
        m = rx.search(s)
        if not m:
            continue
        try:
            if tipo == "ymd":
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if tipo == "dmy":
                a, b = int(m.group(1)), int(m.group(2))
                g, mese = (b, a) if formato_usa else (a, b)
                return date(int(m.group(3)), mese, g)
            if tipo == "d_mese_y":
                return date(int(m.group(3)), MESI[m.group(2).lower()], int(m.group(1)))
            if tipo == "mese_d_y":
                return date(int(m.group(3)), MESI[m.group(1).lower()], int(m.group(2)))
        except (ValueError, KeyError):
            continue
    return None


def date_nel_testo(testo: str) -> list[tuple[int, date]]:
    trovate = []
    for rx, tipo in RX_DATE:
        for m in rx.finditer(testo):
            d = data_da_testo(m.group(0))
            if d and 2000 < d.year < 2100:
                trovate.append((m.start(), d))
    return trovate


def scadenza_da_testo(testo: str) -> tuple[date | None, bool]:
    """(prima scadenza futura vicina a parole come 'scadenza'/'deadline', ci sono solo date passate?)"""
    date_ = date_nel_testo(testo)
    if not date_:
        return None, False
    vicine = []
    for pos, d in date_:
        finestra = testo[max(0, pos - 160):pos + 40]
        if RX_SCADENZA.search(finestra):
            vicine.append(d)
    future = sorted(d for d in vicine if d >= OGGI)
    if future:
        return future[0], False
    solo_passate = bool(vicine) and all(d < OGGI for d in vicine)
    return None, solo_passate


def iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


def da_iso(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def nuovo_record(**kw) -> dict:
    base = dict(id=None, fonte=None, programma=None, ente=None, titolo=None, titolo_it=None,
                macrogruppi=[], parole_chiave=[], rilevanza=None, motivo=None,
                budget_totale=None, budget_progetto=None, valuta=None,
                progetti_finanziabili=None, competitivita=None, copertura=None,
                scadenza=None, apertura=None, durata=None, tipo_bando=None, chi_puo=None,
                partenariato=None, paesi=None, lingua=None, url=None, sommario=None,
                stato=None, aggiornato=iso(OGGI), prima_vista=None, note=None,
                _testo="", _origine="api")
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# Fonte 1 – EU Funding & Tenders Portal
# ---------------------------------------------------------------------------

EU_SEARCH = "https://api.tech.ec.europa.eu/search-api/prod/rest/search"
EU_TOPIC = "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/topic-details/{id}"
EU_STATI = {"31094501": "In apertura", "31094502": "Aperto"}
EU_PROGRAMMI = {
    "HORIZON": "Horizon Europe", "EU4H": "EU4Health", "DIGITAL": "Digital Europe",
    "ERASMUS": "Erasmus+", "CERV": "CERV", "ESF": "FSE+", "SMP": "Single Market Programme",
    "I3": "Interregional Innovation Investments", "LIFE": "LIFE", "CEF": "CEF", "EDF": "European Defence Fund",
    "EIC": "European Innovation Council", "ERC": "European Research Council", "MSCA": "Marie Skłodowska-Curie",
    "EIE": "European Innovation Ecosystems", "CREA": "Europa Creativa", "AGRIP": "Promozione agricola",
    "EMFAF": "FEAMPA", "RFCS": "Research Fund for Coal and Steel", "EUAF": "EU Anti-Fraud",
    "JUST": "Giustizia", "SOCPL": "Social prerogatives", "UCPM": "Protezione civile UE",
}


def _meta(m: dict, k: str):
    v = m.get(k)
    if isinstance(v, list):
        return v[0] if v else None
    return v


def scarica_eu(http: Http) -> tuple[list[dict], int]:
    query = {"bool": {"must": [{"terms": {"type": ["1", "2", "8"]}},
                               {"terms": {"status": ["31094501", "31094502"]}}]}}
    files = {
        "query": ("blob", json.dumps(query), "application/json"),
        "languages": ("blob", json.dumps(["en"]), "application/json"),
        "sort": ("blob", json.dumps({"field": "sortStatus", "order": "ASC"}), "application/json"),
    }
    risultati, pagina, esaminati = [], 1, 0
    visti = set()
    while pagina <= 60:
        r = http.post(EU_SEARCH, params={"apiKey": "SEDIA", "text": "***",
                                         "pageSize": 100, "pageNumber": pagina}, files=files)
        dati = r.json()
        voci = dati.get("results") or []
        for v in voci:
            esaminati += 1
            rec = converti_eu(v)
            if rec and rec["id"] not in visti:
                visti.add(rec["id"])
                risultati.append(rec)
        totale = int(dati.get("totalResults") or 0)
        if not voci or pagina * 100 >= totale:
            break
        pagina += 1
        time.sleep(0.5)
    return risultati, esaminati


def converti_eu(v: dict) -> dict | None:
    m = v.get("metadata") or {}
    ident = _meta(m, "identifier") or v.get("reference")
    titolo = _meta(m, "title") or v.get("title") or ident
    if not ident:
        return None
    descr = html_a_testo(_meta(m, "descriptionByte") or v.get("summary") or "")
    parole = " ".join(m.get("keywords") or []) + " " + " ".join(m.get("tags") or [])
    testo = f"{titolo}. {parole}. {descr}"
    gruppi, chiavi = classifica(testo)
    if not gruppi:
        return None

    stato = EU_STATI.get(str(_meta(m, "status") or ""), "Aperto")
    scadenze = sorted(d for d in (data_da_testo(x) for x in (m.get("deadlineDate") or [])) if d)
    future = [d for d in scadenze if d >= OGGI]
    scad = future[0] if future else (scadenze[-1] if scadenze else None)
    apertura = data_da_testo(_meta(m, "startDate") or "")

    budget_tot = budget_prog = progetti = None
    try:
        bo = json.loads(_meta(m, "budgetOverview") or "{}")
        tot, maxc, minc, attesi = 0.0, None, None, 0
        for azioni in (bo.get("budgetTopicActionMap") or {}).values():
            for a in azioni:
                if ident not in (a.get("action") or ident):
                    continue
                tot += sum(numero(x) or 0 for x in (a.get("budgetYearMap") or {}).values())
                maxc = numero(a.get("maxContribution")) or maxc
                minc = numero(a.get("minContribution")) or minc
                attesi += int(numero(a.get("expectedGrants")) or 0)
        budget_tot = tot or None
        budget_prog = maxc or minc
        progetti = attesi or None
    except (ValueError, TypeError, AttributeError):
        pass

    prefisso = ident.split("-")[0].upper()
    tipi = " ".join(m.get("typesOfAction") or [])
    tipo_voce = str((m.get("type") or [""])[0])
    url = EU_TOPIC.format(id=ident)
    if tipo_voce not in ("1", "2") and str(v.get("url", "")).startswith("http"):
        url = v["url"]              # bandi a cascata (cascade funding): pagina esterna

    return nuovo_record(
        id=f"EU-{ident}", fonte="EU Funding & Tenders Portal",
        programma=EU_PROGRAMMI.get(prefisso, prefisso) + (f" – {tipi}" if tipi else ""),
        ente="Commissione europea", titolo=titolo, macrogruppi=gruppi, parole_chiave=chiavi,
        budget_totale=budget_tot, budget_progetto=budget_prog, valuta="EUR" if (budget_tot or budget_prog) else None,
        progetti_finanziabili=progetti, scadenza=iso(scad), apertura=iso(apertura),
        tipo_bando=tipo_da_azione_eu(ident, tipi), copertura=copertura_eu(ident, tipi),
        partenariato=partenariato_eu(ident, tipi),
        paesi="Stati membri UE e paesi associati (altri paesi secondo le condizioni del topic)",
        lingua="Inglese", url=url, stato=stato, _testo=testo[:8000])


def copertura_eu(ident: str, tipi: str) -> str | None:
    t, i = tipi.lower(), ident.upper()
    if "cofund" in t or "COFUND" in i:
        return "Co-finanziamento richiesto (azione Cofund: quota UE parziale)"
    if i.startswith("EU4H"):
        return "Co-finanziamento: di norma 60% UE (fino all'80% in casi di utilità eccezionale)"
    if "research and innovation" in t or "-RIA" in i or "coordination and support" in t or "-CSA" in i:
        return "100% – finanziato per intero"
    if "innovation action" in t or "-IA" in i:
        return "70% per le imprese, 100% per gli enti non profit"
    if i.startswith(("ERC", "HORIZON-ERC", "HORIZON-MSCA", "MSCA")):
        return "100% – finanziato per intero"
    if i.startswith("DIGITAL") and "simple grant" in t:
        return "Co-finanziamento: di norma 50% UE"
    return None


def tipo_da_azione_eu(ident: str, tipi: str) -> str | None:
    t, i = tipi.lower(), ident.upper()
    if "MSCA" in i or "training" in t or "doctoral" in t:
        return "Dottorato / formazione / mobilità"
    if "research and innovation" in t or "-RIA" in i or "ERC" in i:
        return "Ricerca"
    if "innovation action" in t or "-IA" in i or "EIC" in i:
        return "Innovazione"
    if "coordination and support" in t or "-CSA" in i:
        return "Coordinamento e supporto"
    if "infrastructure" in t or "-INFRA" in i:
        return "Infrastruttura"
    return None


def partenariato_eu(ident: str, tipi: str) -> str | None:
    t, i = tipi.lower(), ident.upper()
    if "ERC" in i:
        return "Singolo PI con ente ospitante"
    if "MSCA" in i and ("PF" in i or "postdoctoral" in t):
        return "Singolo ricercatore con ente ospitante"
    if i.startswith("HORIZON") and ("research and innovation" in t or "innovation action" in t or "-RIA" in i or "-IA" in i):
        return "Consorzio: min. 3 enti indipendenti da 3 paesi UE/associati (regola standard, verificare il topic)"
    if "coordination and support" in t or "-CSA" in i:
        return "Anche un singolo ente (regola standard CSA, verificare il topic)"
    return None


# ---------------------------------------------------------------------------
# Fonte 2 – Grants.gov (tutte le agenzie federali USA, NIH compreso)
# ---------------------------------------------------------------------------

GG_SEARCH = "https://api.grants.gov/v1/api/search2"
GG_FETCH = "https://api.grants.gov/v1/api/fetchOpportunity"
GG_URL = "https://www.grants.gov/search-results-detail/{id}"
GG_PAROLE = ["rehabilitation", "stroke", "Parkinson", "aging", "older adults", "dementia",
             "Alzheimer", "assistive technology", "disability", "neurorehabilitation",
             "physical therapy", "exoskeleton", "mobility", "falls", "neurodegenerative",
             "brain injury", "prosthetic", "telerehabilitation", "geriatric", "cognitive decline",
             "frailty", "robotics", "motor recovery", "aphasia", "wearable"]


def _primo(d: dict, *chiavi):
    for k in chiavi:
        if isinstance(d, dict) and d.get(k) not in (None, "", [], "none", "None"):
            return d[k]
    return None


def scarica_grants_gov(http: Http) -> tuple[list[dict], int]:
    ids: dict[str, dict] = {}
    for parola in GG_PAROLE:
        inizio = 0
        while True:
            r = http.post(GG_SEARCH, json={"keyword": parola, "oppStatuses": "forecasted|posted",
                                           "rows": 500, "startRecordNum": inizio})
            dati = (r.json() or {}).get("data") or {}
            hits = dati.get("oppHits") or []
            for h in hits:
                if h.get("id"):
                    ids.setdefault(str(h["id"]), h)
            inizio += len(hits)
            if not hits or inizio >= int(dati.get("hitCount") or 0):
                break
        time.sleep(0.3)
    risultati = []
    for oid, hit in ids.items():
        try:
            r = http.post(GG_FETCH, json={"opportunityId": int(oid)})
            dettaglio = (r.json() or {}).get("data") or {}
        except Exception as e:
            log.debug("Grants.gov %s: %s", oid, e)
            dettaglio = {}
        rec = converti_grants_gov(hit, dettaglio)
        if rec:
            risultati.append(rec)
        time.sleep(0.2)
    return risultati, len(ids)


def converti_grants_gov(hit: dict, det: dict) -> dict | None:
    syn = det.get("synopsis") or det.get("forecast") or {}
    titolo = hit.get("title") or det.get("opportunityTitle")
    descr = html_a_testo(_primo(syn, "synopsisDesc", "forecastDesc", "description") or "")
    testo = f"{titolo}. {descr}"
    gruppi, chiavi = classifica(testo)
    if not gruppi:
        return None
    stato = "In apertura" if (hit.get("oppStatus") or "").lower() == "forecasted" else "Aperto"
    scad = data_da_testo(hit.get("closeDate") or "", formato_usa=True) or \
        data_da_testo(str(_primo(syn, "responseDate", "estApplicationResponseDate") or ""), formato_usa=True)
    apertura = data_da_testo(hit.get("openDate") or "", formato_usa=True)
    if scad and scad < OGGI:
        return None

    tipi_ente = [t.get("description", "") for t in (_primo(syn, "applicantTypes") or []) if isinstance(t, dict)]
    stranieri = any(re.search(r"foreign|non-domestic", t, re.I) for t in tipi_ente)
    cost = _primo(syn, "costSharing", "costSharingOrMatchingRequirement")
    if isinstance(cost, str):
        cost = cost.strip().lower() in ("true", "yes", "y")
    copertura = None
    if cost is True:
        copertura = "Co-finanziamento richiesto (cost sharing)"
    elif cost is False:
        copertura = "100% – nessun cost sharing richiesto"
    agenzia = hit.get("agency") or _primo(syn, "agencyName") or hit.get("agencyCode")
    progetti = numero(_primo(syn, "expectedNumberOfAwards", "numberOfAwards"))

    return nuovo_record(
        id=f"GG-{hit.get('number') or hit.get('id')}", fonte="Grants.gov",
        programma=f"{agenzia} – {hit.get('number') or ''}".strip(" –"), ente=agenzia, titolo=titolo,
        macrogruppi=gruppi, parole_chiave=chiavi,
        budget_totale=numero(_primo(syn, "estimatedFunding", "estimatedFundingFormatted")),
        budget_progetto=numero(_primo(syn, "awardCeiling", "awardCeilingFormatted")),
        valuta="USD", progetti_finanziabili=int(progetti) if progetti else None,
        scadenza=iso(scad), apertura=iso(apertura), copertura=copertura,
        chi_puo="; ".join(tipi_ente)[:400] or None,
        paesi="Anche enti non statunitensi" if stranieri else "Solo enti statunitensi (salvo diversa indicazione)",
        lingua="Inglese", url=GG_URL.format(id=hit.get("id")), stato=stato, _testo=testo[:8000])


# ---------------------------------------------------------------------------
# Fonte 3 – Siti web (ministeri, regioni, fondazioni, charity) da fonti_web.csv
# ---------------------------------------------------------------------------

RX_LINK_BANDO = re.compile(
    r"(bando|bandi|avvis|call|grant|funding|finanziament|opportunit|fellowship|award|"
    r"research[-_ ]?(programme|program|fund)|ricerca|apply|contribut|premi|programm|scheme)", re.I)
RX_PAGINA_BANDO = re.compile(
    r"(bando|avviso|call for (proposals|applications)|grant|funding (opportunity|call|round)|"
    r"fellowship|award|finanziament|application deadline|how to apply|come partecipare|scadenza)", re.I)
ESTENSIONI_SALTA = (".jpg", ".jpeg", ".png", ".gif", ".svg", ".zip", ".doc", ".docx", ".xls",
                    ".xlsx", ".ppt", ".pptx", ".mp4", ".mp3", ".ics")


def leggi_fonti_web(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.DictReader(f, delimiter=";") if (r.get("url") or "").startswith("http")]


def _stesso_sito(a: str, b: str) -> bool:
    da = urlparse(a).netloc.lower().removeprefix("www.")
    db = urlparse(b).netloc.lower().removeprefix("www.")
    radice = ".".join(da.split(".")[-2:])
    return db == da or db.endswith("." + radice) or db == radice


def _testo_pdf(contenuto: bytes) -> str:
    try:
        from pypdf import PdfReader
        lettore = PdfReader(io.BytesIO(contenuto))
        return " ".join((p.extract_text() or "") for p in lettore.pages[:10])
    except Exception:
        return ""


def esplora_fonte(http: Http, fonte: dict, max_pagine: int) -> tuple[list[dict], int, str | None]:
    """Visita la pagina iniziale e i link che sembrano bandi (profondita' 2)."""
    from bs4 import BeautifulSoup

    inizio = fonte["url"].strip()
    coda: list[tuple[str, int]] = [(inizio, 0)]
    visti: set[str] = set()
    candidati: list[dict] = []
    visitate, errore = 0, None
    while coda and visitate < max_pagine:
        url, prof = coda.pop(0)
        url = urldefrag(url)[0]
        if url in visti:
            continue
        visti.add(url)
        if not http.consentito(url):
            log.debug("robots.txt non consente %s", url)
            continue
        try:
            r = http.get(url, cortese=True, timeout=30)
        except Exception as e:
            if prof == 0:
                errore = f"irraggiungibile: {e}"
            continue
        visitate += 1
        if r.status_code >= 400:
            if prof == 0:
                errore = f"HTTP {r.status_code}"
            continue
        tipo = r.headers.get("content-type", "").lower()
        if "pdf" in tipo or url.lower().endswith(".pdf"):
            if len(r.content) <= 8_000_000:
                testo = _testo_pdf(r.content)
                titolo = Path(urlparse(url).path).stem.replace("-", " ").replace("_", " ")
                rec = valuta_pagina(fonte, url, titolo, testo, pdf=True)
                if rec:
                    candidati.append(rec)
            continue
        if "html" not in tipo:
            continue
        soup = BeautifulSoup(r.text, "lxml")
        titolo = (soup.find("h1").get_text(" ", strip=True) if soup.find("h1") else "") or \
                 (soup.title.get_text(" ", strip=True) if soup.title else url)
        link_bando = []
        for a in soup.find_all("a", href=True):
            href = urljoin(url, a["href"])
            if not href.startswith("http") or not _stesso_sito(inizio, href):
                continue
            if urlparse(href).path.lower().endswith(ESTENSIONI_SALTA):
                continue
            etichetta = a.get_text(" ", strip=True)
            if RX_LINK_BANDO.search(etichetta) or RX_LINK_BANDO.search(href):
                link_bando.append(href)
        for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "form"]):
            tag.decompose()
        testo = re.sub(r"\s+", " ", soup.get_text(" ")).strip()
        # le pagine elenco (molti link a bandi) non sono un bando: servono solo per proseguire
        if prof > 0 and len(set(link_bando)) <= 8:
            rec = valuta_pagina(fonte, url, titolo, testo)
            if rec:
                candidati.append(rec)
        if prof < 2:
            for href in dict.fromkeys(link_bando):
                if href not in visti:
                    coda.append((href, prof + 1))
    return candidati, visitate, errore


def valuta_pagina(fonte: dict, url: str, titolo: str, testo: str, pdf: bool = False) -> dict | None:
    if len(testo) < 300:
        return None
    if not (RX_PAGINA_BANDO.search(titolo) or RX_PAGINA_BANDO.search(url) or
            RX_PAGINA_BANDO.search(testo[:3000])):
        return None
    gruppi, chiavi = classifica(f"{titolo}. {testo[:20000]}")
    if not gruppi:
        return None
    scad, solo_passate = scadenza_da_testo(testo[:30000])
    if solo_passate:
        return None                      # bando chiuso
    lingua = "Italiano" if re.search(r"\b(della|degli|delle|bando|ricerca|scadenza)\b", testo[:3000], re.I) else "Inglese"
    budget = None
    m = re.search(r"(?:€|eur(?:o|os)?|\$|usd|£|gbp)\s?([\d.,]{4,})|([\d.,]{4,})\s?(?:€|euro|eur\b)", testo[:20000], re.I)
    if m:
        budget = numero(m.group(1) or m.group(2))
    valuta = None
    if budget:
        frammento = m.group(0).lower()
        valuta = "USD" if ("$" in frammento or "usd" in frammento) else "GBP" if ("£" in frammento or "gbp" in frammento) else "EUR"
    cof = None
    if re.search(r"co-?financ|co-?finanziament|cofinanziament|matching fund|cost[- ]shar|quota a carico", testo[:30000], re.I):
        cof = "Possibile co-finanziamento: verificare"
    return nuovo_record(
        id="WEB-" + hashlib.sha1(url.encode()).hexdigest()[:12], fonte=fonte.get("nome"),
        programma=titolo[:200], ente=fonte.get("nome"), titolo=titolo[:300],
        macrogruppi=gruppi, parole_chiave=chiavi, budget_totale=budget, valuta=valuta,
        scadenza=iso(scad), copertura=cof, lingua=lingua,
        paesi=fonte.get("copertura") or None, url=url,
        stato="Aperto" if scad else "Scadenza da verificare",
        _testo=f"{titolo}. {testo}"[:8000], _origine="web" + ("-pdf" if pdf else ""))


# ---------------------------------------------------------------------------
# LLM gratuito in locale (Ollama)
# ---------------------------------------------------------------------------

ISTRUZIONI_LLM = """Sei un esperto di bandi di ricerca. Leggi il testo di una call e rispondi
SOLO con un oggetto JSON (nessun altro testo) con queste chiavi:
{
 "rilevanza": "Alta" | "Media" | "Bassa",
 "motivo": "una frase in italiano: perche' e' (o non e') pertinente alla riabilitazione",
 "titolo_it": "titolo tradotto in italiano, max 15 parole",
 "sommario_it": "100-150 parole in italiano: obiettivi, chi puo' partecipare, cosa si finanzia",
 "aperto": true | false | null,
 "scadenza": "AAAA-MM-GG" oppure null,
 "budget_totale": numero oppure null,
 "budget_progetto": numero oppure null,
 "valuta": "EUR" | "USD" | "GBP" | altro codice | null,
 "progetti_finanziabili": numero intero oppure null,
 "copertura": "100%" oppure "co-finanziamento X%" oppure null,
 "partenariato": "requisiti di consorzio in breve, in italiano" oppure null,
 "chi_puo": "tipi di enti o persone ammessi, in italiano" oppure null,
 "paesi": "paesi ammessi, in italiano" oppure null,
 "durata_mesi": numero oppure null,
 "tipo_bando": "Ricerca" | "Innovazione" | "Dottorato / formazione / mobilità" | "Infrastruttura" | "Altro"
}
Pertinenza: Alta se il bando riguarda esplicitamente riabilitazione, ictus, Parkinson,
invecchiamento/demenze o tecnologie per la riabilitazione; Media se il tema e' ammesso tra
altri; Bassa se e' generico o non c'entra. Usa solo informazioni presenti nel testo:
se un dato manca scrivi null. Non inventare importi o date."""


class LLM:
    def __init__(self, url: str, modello: str):
        self.url, self.modello = url.rstrip("/"), modello
        self.s = requests.Session()

    def disponibile(self) -> bool:
        try:
            r = self.s.get(self.url + "/api/tags", timeout=10)
            nomi = [m.get("name", "") for m in r.json().get("models", [])]
            return any(n.startswith(self.modello.split(":")[0]) for n in nomi)
        except Exception:
            return False

    def analizza(self, rec: dict) -> dict | None:
        contenuto = json.dumps({"ente": rec.get("ente"), "programma": rec.get("programma"),
                                "titolo": rec.get("titolo"), "testo": rec["_testo"][:6000]},
                               ensure_ascii=False)
        r = self.s.post(self.url + "/api/chat", timeout=400, json={
            "model": self.modello, "stream": False, "format": "json",
            "options": {"temperature": 0.1, "num_ctx": 4096},
            "messages": [{"role": "system", "content": ISTRUZIONI_LLM},
                         {"role": "user", "content": contenuto}]})
        r.raise_for_status()
        return json.loads(r.json()["message"]["content"])


def impronta(testo: str) -> str:
    return hashlib.sha1(testo.encode("utf-8", "ignore")).hexdigest()[:16]


def applica_llm(rec: dict, out: dict) -> None:
    ril = str(out.get("rilevanza") or "").capitalize()
    rec["rilevanza"] = ril if ril in ("Alta", "Media", "Bassa") else rec.get("rilevanza")
    rec["motivo"] = (out.get("motivo") or "")[:400] or rec.get("motivo")
    rec["titolo_it"] = (out.get("titolo_it") or "")[:250] or rec.get("titolo_it")
    som = out.get("sommario_it") or ""
    if 200 <= len(som) <= 2000:
        rec["sommario"] = som
    # i dati strutturati delle API hanno sempre la precedenza: l'LLM riempie solo i vuoti
    for campo in ("budget_totale", "budget_progetto"):
        if rec.get(campo) is None:
            rec[campo] = numero(out.get(campo))
    if rec.get("valuta") is None and isinstance(out.get("valuta"), str) and len(out["valuta"]) == 3:
        rec["valuta"] = out["valuta"].upper()
    if rec.get("progetti_finanziabili") is None:
        n = numero(out.get("progetti_finanziabili"))
        rec["progetti_finanziabili"] = int(n) if n else None
    for campo, chiave in (("copertura", "copertura"), ("partenariato", "partenariato"),
                          ("chi_puo", "chi_puo"), ("tipo_bando", "tipo_bando")):
        if not rec.get(campo) and isinstance(out.get(chiave), str) and out[chiave].strip():
            rec[campo] = out[chiave].strip()[:300]
    if rec["_origine"].startswith("web") and isinstance(out.get("paesi"), str) and out["paesi"].strip():
        rec["paesi"] = out["paesi"].strip()[:200]
    dm = numero(out.get("durata_mesi"))
    if dm and not rec.get("durata"):
        rec["durata"] = f"{int(dm)} mesi"
    if rec["_origine"].startswith("web"):
        if not rec.get("scadenza"):
            d = da_iso(out.get("scadenza")) if isinstance(out.get("scadenza"), str) else None
            if d and d >= OGGI:
                rec["scadenza"], rec["stato"] = iso(d), "Aperto"
        if out.get("aperto") is False and not rec.get("scadenza"):
            rec["stato"] = "Da verificare (forse chiuso)"


# ---------------------------------------------------------------------------
# Indicatori calcolati
# ---------------------------------------------------------------------------

def leggi_tassi(path: Path) -> list[tuple[str, float, str]]:
    if not path.exists():
        return []
    righe = []
    with path.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f, delimiter=";"):
            t = numero(r.get("tasso_percentuale"))
            if r.get("programma_contiene") and t:
                righe.append((r["programma_contiene"].lower(), t, r.get("fonte_dato") or ""))
    return righe


def calcola_indicatori(rec: dict, tassi) -> None:
    if not rec.get("progetti_finanziabili") and rec.get("budget_totale") and rec.get("budget_progetto"):
        n = math.floor(rec["budget_totale"] / rec["budget_progetto"])
        rec["progetti_finanziabili"] = n if n >= 1 else None
    n = rec.get("progetti_finanziabili")
    chiave = f"{rec.get('programma') or ''} {rec.get('ente') or ''} {rec.get('id')}".lower()
    tasso = next(((t, f) for p, t, f in tassi if p in chiave), None)
    if tasso:
        t = tasso[0]
        livello = "Alta" if t >= 25 else "Media" if t >= 10 else "Bassa"
        dettagli = [f"tasso di successo storico {t:g}%"] + ([f"{n} progetti finanziabili"] if n else [])
        rec["competitivita"] = f"{livello} – " + ", ".join(dettagli) + " (stima)"
    elif n:
        livello = "Alta" if n >= 20 else "Media" if n >= 5 else "Bassa"
        rec["competitivita"] = f"{livello} – {n} progetti finanziabili (stima: tasso storico non disponibile)"
    else:
        rec["competitivita"] = "Non stimabile – dati insufficienti"
    if not rec.get("copertura"):
        rec["copertura"] = "Non indicato"


def calcola_stato(rec: dict) -> None:
    scad, ap = da_iso(rec.get("scadenza")), da_iso(rec.get("apertura"))
    if rec.get("stato", "").startswith("Da verificare"):
        return
    if ap and ap > OGGI:
        rec["stato"] = "In apertura"
    elif scad and (scad - OGGI).days <= 30:
        rec["stato"] = "In scadenza (≤30 gg)"
    elif scad:
        rec["stato"] = "Aperto"
    elif rec.get("stato") not in ("In apertura",):
        rec["stato"] = "Scadenza da verificare"


# ---------------------------------------------------------------------------
# Stato persistente (dati/stato.json)
# ---------------------------------------------------------------------------

def carica_stato(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"bandi": {}, "llm": {}, "esecuzioni": [], "fonti": {}}


def salva_stato(path: Path, stato: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    stato["esecuzioni"] = stato["esecuzioni"][-200:]
    path.write_text(json.dumps(stato, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------

COLONNE = [
    ("ID", "id", 22), ("Fonte", "fonte", 18), ("Programma", "programma", 30),
    ("Ente finanziatore", "ente", 24), ("Titolo", "titolo", 45), ("Titolo IT", "titolo_it", 45),
    ("Macrogruppo", "macrogruppi", 22), ("Parole chiave", "parole_chiave", 28),
    ("Rilevanza LLM", "rilevanza", 12), ("Motivo LLM", "motivo", 40),
    ("Budget totale", "budget_totale", 15), ("Budget per progetto", "budget_progetto", 15),
    ("Valuta", "valuta", 8), ("Progetti finanziabili", "progetti_finanziabili", 12),
    ("Indicatore competitività", "competitivita", 34), ("Copertura finanziaria", "copertura", 30),
    ("Scadenza", "scadenza", 12), ("Giorni alla scadenza", None, 11), ("Apertura", "apertura", 12),
    ("Durata progetto", "durata", 12), ("Tipo di bando", "tipo_bando", 18),
    ("Chi può candidarsi", "chi_puo", 30), ("Struttura partenariato", "partenariato", 34),
    ("Paesi ammessi", "paesi", 26), ("Lingua bando", "lingua", 10), ("URL", "url", 40),
    ("Sommario", "sommario", 70), ("Stato", "stato", 18), ("Aggiornato il", "aggiornato", 12),
    ("Prima vista", "prima_vista", 12), ("Note", "note", 30),
]


def scrivi_excel(path: Path, bandi: list[dict], stato: dict) -> None:
    from openpyxl import Workbook
    from openpyxl.comments import Comment
    from openpyxl.formatting.rule import CellIsRule
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    font, font_b = Font(name="Arial", size=10), Font(name="Arial", size=10, bold=True, color="FFFFFF")
    testata = PatternFill("solid", fgColor="1F4E5A")
    a_capo = Alignment(wrap_text=True, vertical="top")
    in_alto = Alignment(vertical="top")

    ws = wb.active
    ws.title = "BANDI"
    col_scad = get_column_letter([c[0] for c in COLONNE].index("Scadenza") + 1)
    for j, (nome, _, larg) in enumerate(COLONNE, 1):
        c = ws.cell(row=1, column=j, value=nome)
        c.font, c.fill, c.alignment = font_b, testata, Alignment(wrap_text=True, vertical="center")
        ws.column_dimensions[get_column_letter(j)].width = larg
    ws.cell(row=1, column=len(COLONNE)).comment = Comment(
        "Campo libero per i ricercatori. Il file viene rigenerato ogni settimana: "
        "le note scritte qui non vengono conservate nella versione successiva.", "NeuroGrant")
    ws.cell(row=1, column=[c[0] for c in COLONNE].index("Indicatore competitività") + 1).comment = Comment(
        "Stima: combina progetti finanziabili e, se disponibile, il tasso di successo storico "
        "del programma (file tassi_successo.csv). La probabilità reale dipende dal numero di "
        "domande, noto solo a bando chiuso.", "NeuroGrant")

    for i, b in enumerate(bandi, 2):
        for j, (nome, chiave, _) in enumerate(COLONNE, 1):
            if chiave is None:
                val = f'=IF({col_scad}{i}="","",{col_scad}{i}-TODAY())'
            else:
                val = b.get(chiave)
                if isinstance(val, list):
                    val = ", ".join(val)
                if chiave in ("scadenza", "apertura", "aggiornato", "prima_vista"):
                    val = da_iso(val)
            c = ws.cell(row=i, column=j, value=val)
            c.font = font
            c.alignment = a_capo if nome in ("Sommario", "Motivo LLM", "Titolo", "Titolo IT",
                                             "Struttura partenariato", "Chi può candidarsi",
                                             "Indicatore competitività", "Copertura finanziaria") else in_alto
            if chiave in ("scadenza", "apertura", "aggiornato", "prima_vista"):
                c.number_format = "DD/MM/YYYY"
            elif chiave in ("budget_totale", "budget_progetto"):
                c.number_format = "#,##0"
            elif chiave is None:
                c.number_format = "0"
            elif chiave == "url" and val:
                c.hyperlink = val
                c.font = Font(name="Arial", size=10, color="1F5FA8", underline="single")
    ultima = max(2, len(bandi) + 1)
    col_giorni = get_column_letter([c[0] for c in COLONNE].index("Giorni alla scadenza") + 1)
    ws.conditional_formatting.add(
        f"{col_giorni}2:{col_giorni}{ultima}",
        CellIsRule(operator="between", formula=["0", "30"], fill=PatternFill("solid", fgColor="F6D5CF")))
    ws.freeze_panes = "F2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLONNE))}{ultima}"
    ws.row_dimensions[1].height = 32

    # FONTI
    wf = wb.create_sheet("FONTI")
    intest = ["Nome fonte", "Tipo accesso", "Copertura geografica", "Tipo finanziamenti", "URL",
              "Ultimo aggiornamento riuscito", "Bandi trovati (totale)", "Bandi pertinenti (totale)", "Note"]
    larghezze = [34, 14, 22, 22, 45, 20, 14, 14, 50]
    for j, (n, w) in enumerate(zip(intest, larghezze), 1):
        c = wf.cell(row=1, column=j, value=n)
        c.font, c.fill, c.alignment = font_b, testata, Alignment(wrap_text=True, vertical="center")
        wf.column_dimensions[get_column_letter(j)].width = w
    for i, (nome, f) in enumerate(sorted(stato["fonti"].items()), 2):
        valori = [nome, f.get("tipo"), f.get("copertura"), f.get("finanziamenti"), f.get("url"),
                  f.get("ultimo_ok"), f.get("trovati_tot", 0), len(f.get("pertinenti", [])), f.get("note")]
        for j, v in enumerate(valori, 1):
            c = wf.cell(row=i, column=j, value=v)
            c.font, c.alignment = font, in_alto
    wf.freeze_panes = "B2"

    # LOG
    wl = wb.create_sheet("LOG")
    intest = ["Data esecuzione", "Bandi esaminati", "Bandi nuovi aggiunti", "Bandi rimossi", "Errori", "Durata (minuti)"]
    for j, (n, w) in enumerate(zip(intest, [20, 14, 14, 14, 80, 12]), 1):
        c = wl.cell(row=1, column=j, value=n)
        c.font, c.fill = font_b, testata
        wl.column_dimensions[get_column_letter(j)].width = w
    for i, e in enumerate(reversed(stato["esecuzioni"]), 2):
        for j, k in enumerate(["data", "esaminati", "nuovi", "rimossi", "errori", "durata_min"], 1):
            c = wl.cell(row=i, column=j, value=e.get(k))
            c.font, c.alignment = font, a_capo

    wb.calculation.fullCalcOnLoad = True
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def _fmt_data(s):
    d = da_iso(s)
    return d.strftime("%d/%m/%Y") if d else "da verificare"


def _fmt_soldi(v, valuta):
    if not v:
        return "budget n.d."
    simbolo = {"EUR": "€", "USD": "$", "GBP": "£"}.get(valuta or "", valuta or "")
    return f"{v:,.0f}".replace(",", ".") + f" {simbolo}".rstrip()


def componi_email(bandi: list[dict], nuovi: set[str], allegato: Path, mittente: str, destinatario: str) -> EmailMessage:
    gruppi = [
        ("⏰ In scadenza entro 30 giorni", [b for b in bandi if b["stato"].startswith("In scadenza")]),
        ("📂 Aperti", [b for b in bandi if b["stato"] == "Aperto"]),
        ("🔜 In apertura", [b for b in bandi if b["stato"] == "In apertura"]),
        ("❔ Scadenza da verificare", [b for b in bandi if b["stato"].startswith(("Scadenza da", "Da verificare"))]),
    ]
    in_scadenza = len(gruppi[0][1])
    oggetto = (f"NeuroGrant – {len(bandi)} bandi aperti ({len(nuovi)} {'nuovo' if len(nuovi) == 1 else 'nuovi'}, "
               f"{in_scadenza} in scadenza)")

    def riga(b):
        tit = b.get("titolo_it") or b.get("titolo")
        ril = f", pertinenza {b['rilevanza']}" if b.get("rilevanza") else ""
        return (("🆕 " if b["id"] in nuovi else "") + tit, b.get("ente") or b.get("fonte"),
                _fmt_data(b.get("scadenza")), _fmt_soldi(b.get("budget_progetto") or b.get("budget_totale"), b.get("valuta")),
                (b.get("copertura") or "").split("–")[0].strip(), (b.get("competitivita") or "").split("–")[0].strip() + ril,
                b.get("url"))

    testo = [oggetto, ""]
    corpo = [f"<h2 style='font-family:Arial'>{html.escape(oggetto)}</h2>"]
    for titolo, elenco in gruppi:
        if not elenco:
            continue
        testo += [f"{titolo} ({len(elenco)})", "-" * 40]
        corpo.append(f"<h3 style='font-family:Arial;margin-top:24px'>{titolo} ({len(elenco)})</h3><ul style='font-family:Arial;font-size:14px;padding-left:18px'>")
        for b in elenco:
            t, ente, scad, soldi, cop, comp, url = riga(b)
            testo.append(f"• {t} – {ente} – scade {scad} – {soldi} – {cop} – competitività {comp} – {url}")
            corpo.append(
                f"<li style='margin-bottom:6px'><a href='{html.escape(url or '')}'>{html.escape(t)}</a> – "
                f"{html.escape(ente or '')} – <b>scade {scad}</b> – {html.escape(soldi)} – "
                f"{html.escape(cop)} – competitività {html.escape(comp)}</li>")
        corpo.append("</ul>")
        testo.append("")
    conteggio = {g: sum(1 for b in bandi if g in b.get("macrogruppi", [])) for g in GRUPPI_ORDINE}
    riepilogo = " | ".join(f"{g}: {n}" for g, n in conteggio.items())
    testo += ["Riepilogo per macrogruppo: " + riepilogo, f"In allegato: {allegato.name}"]
    corpo.append(f"<p style='font-family:Arial;color:#555'>Riepilogo per macrogruppo: {html.escape(riepilogo)}<br>"
                 f"In allegato il database completo: {html.escape(allegato.name)}. "
                 "Competitività e copertura sono stime automatiche: verificare sempre il testo ufficiale della call.</p>")

    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = oggetto, mittente, destinatario
    msg.set_content("\n".join(testo))
    msg.add_alternative("".join(corpo), subtype="html")
    msg.add_attachment(allegato.read_bytes(), maintype="application",
                       subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet", filename=allegato.name)
    return msg


def invia_email(msg: EmailMessage, utente: str, password: str) -> None:
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as s:
        s.login(utente, password.replace(" ", ""))
        s.send_message(msg)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def registra_fonte(stato, nome, tipo, copertura, finanz, url, recs, esaminati, errore=None):
    f = stato["fonti"].setdefault(nome, {"trovati_tot": 0, "pertinenti": []})
    f.update({"tipo": tipo, "copertura": copertura, "finanziamenti": finanz, "url": url})
    f["trovati_tot"] = int(f.get("trovati_tot", 0)) + int(esaminati or 0)
    f["pertinenti"] = sorted(set(f.get("pertinenti", [])) | {r["id"] for r in recs})[-5000:]
    if errore:
        f["note"] = f"Ultimo errore {OGGI.strftime('%d/%m/%Y')}: {errore}"[:300]
    else:
        f["ultimo_ok"] = datetime.now().strftime("%d/%m/%Y %H:%M")
        f["note"] = f"Ultima esecuzione: {esaminati} elementi esaminati, {len(recs)} pertinenti"


def main() -> None:
    ap = argparse.ArgumentParser(description="NeuroGrant – bandi per la riabilitazione")
    ap.add_argument("--minuti-llm", type=float, default=180, help="tempo massimo per l'LLM (0 = disattivo)")
    ap.add_argument("--ollama-url", default=os.environ.get("OLLAMA_URL", "http://localhost:11434"))
    ap.add_argument("--modello", default=os.environ.get("NEUROGRANT_MODEL", "qwen2.5:3b"))
    ap.add_argument("--fonti", default="eu,gg,web", help="sottoinsieme di eu,gg,web")
    ap.add_argument("--max-pagine-web", type=int, default=35)
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    t0 = time.time()
    percorso_stato = BASE / "dati" / "stato.json"
    stato = carica_stato(percorso_stato)
    ultima_esecuzione = stato["esecuzioni"][-1]["data"][:10] if stato["esecuzioni"] else None
    attivi_prima = {k for k, v in stato["bandi"].items() if v.get("_attivo")}
    http, errori, raccolti, esaminati_tot = Http(), [], [], 0
    fonti = set(args.fonti.split(","))

    if "eu" in fonti:
        try:
            recs, n = scarica_eu(http)
            raccolti += recs
            esaminati_tot += n
            registra_fonte(stato, "EU Funding & Tenders Portal", "API", "UE e paesi associati", "Pubblico",
                           "https://ec.europa.eu/info/funding-tenders/opportunities/portal", recs, n)
            log.info("EU: %d topic esaminati, %d pertinenti", n, len(recs))
        except Exception as e:
            errori.append(f"EU Funding & Tenders: {e}")
            registra_fonte(stato, "EU Funding & Tenders Portal", "API", "UE e paesi associati", "Pubblico",
                           "https://ec.europa.eu/info/funding-tenders/opportunities/portal", [], 0, str(e))
            log.error("EU non disponibile: %s", e)

    if "gg" in fonti:
        try:
            recs, n = scarica_grants_gov(http)
            raccolti += recs
            esaminati_tot += n
            registra_fonte(stato, "Grants.gov (USA, incluso NIH)", "API", "USA (alcuni aperti a enti esteri)",
                           "Pubblico", "https://www.grants.gov", recs, n)
            log.info("Grants.gov: %d opportunità esaminate, %d pertinenti", n, len(recs))
        except Exception as e:
            errori.append(f"Grants.gov: {e}")
            registra_fonte(stato, "Grants.gov (USA, incluso NIH)", "API", "USA", "Pubblico",
                           "https://www.grants.gov", [], 0, str(e))
            log.error("Grants.gov non disponibile: %s", e)

    if "web" in fonti:
        for fonte in leggi_fonti_web(BASE / "fonti_web.csv"):
            nome = fonte.get("nome") or fonte["url"]
            try:
                recs, n, errore = esplora_fonte(http, fonte, args.max_pagine_web)
            except Exception as e:
                recs, n, errore = [], 0, str(e)
            raccolti += recs
            esaminati_tot += n
            if errore:
                errori.append(f"{nome}: {errore}")
            registra_fonte(stato, nome, "Lettura pagine web", fonte.get("copertura"), fonte.get("tipo"),
                           fonte["url"], recs, n, errore)
            log.info("Web %s: %d pagine, %d candidati%s", nome, n, len(recs), f" (errore: {errore})" if errore else "")

    # deduplica (stesso id o stesso titolo normalizzato e scadenza)
    unici: dict[str, dict] = {}
    firme: dict[tuple, str] = {}
    for r in raccolti:
        firma = (re.sub(r"\W+", "", norm(r.get("titolo") or "").lower())[:80], r.get("scadenza"))
        if r["id"] in unici or (firma[0] and firma in firme):
            continue
        unici[r["id"]] = r
        firme[firma] = r["id"]

    # integrazione con lo stato precedente
    for rid, r in unici.items():
        prec = stato["bandi"].get(rid, {})
        r["prima_vista"] = prec.get("prima_vista") or iso(OGGI)
        r["note"] = prec.get("note")

    # fonti non raggiunte in questa esecuzione: si tengono i bandi visti negli ultimi 14 giorni
    for rid, prec in stato["bandi"].items():
        if rid in unici:
            continue
        ultima = da_iso(prec.get("_ultima_vista"))
        scad = da_iso(prec.get("scadenza"))
        if ultima and (OGGI - ultima).days <= 14 and (not scad or scad >= OGGI):
            r = nuovo_record(**{k: v for k, v in prec.items() if not k.startswith("_")})
            r["_origine"] = prec.get("_origine", "api")
            r["_testo"] = ""
            r["_ultima_vista"] = prec.get("_ultima_vista")
            unici[rid] = r

    # LLM
    llm = LLM(args.ollama_url, args.modello) if args.minuti_llm > 0 else None
    if llm and not llm.disponibile():
        log.warning("Ollama non disponibile: si prosegue senza LLM")
        llm = None
    scadenza_llm = time.time() + args.minuti_llm * 60
    da_analizzare = []
    for rid, r in unici.items():
        cache = stato["llm"].get(rid)
        if r["_testo"] and cache and cache.get("hash") == impronta(r["_testo"]):
            applica_llm(r, cache["risultato"])
        elif r["_testo"]:
            da_analizzare.append(r)
        elif cache:
            applica_llm(r, cache["risultato"])
    da_analizzare.sort(key=lambda r: (r.get("scadenza") is None, r.get("scadenza") or ""))
    analizzati = 0
    if llm:
        for r in da_analizzare:
            if time.time() > scadenza_llm:
                log.info("Tempo LLM esaurito: %d bandi rimandati alla prossima esecuzione",
                         len(da_analizzare) - analizzati)
                break
            try:
                out = llm.analizza(r)
                stato["llm"][r["id"]] = {"hash": impronta(r["_testo"]), "risultato": out}
                applica_llm(r, out)
                analizzati += 1
                if analizzati % 10 == 0:
                    log.info("LLM: %d/%d analizzati", analizzati, len(da_analizzare))
            except Exception as e:
                log.warning("LLM non riuscito per %s: %s", r["id"], e)
    for r in unici.values():
        if not r.get("rilevanza"):
            r["rilevanza"] = "Non analizzato"
        r["titolo_it"] = r.get("titolo_it") or r.get("titolo")

    tassi = leggi_tassi(BASE / "tassi_successo.csv")
    attivi = []
    for r in unici.values():
        scad = da_iso(r.get("scadenza"))
        if scad and scad < OGGI:
            continue
        calcola_indicatori(r, tassi)
        calcola_stato(r)
        attivi.append(r)
    ordine_stato = {"In scadenza (≤30 gg)": 0, "Aperto": 1, "In apertura": 2}
    attivi.sort(key=lambda r: (ordine_stato.get(r["stato"], 3), r.get("scadenza") or "9999"))

    nuovi = {r["id"] for r in attivi if not ultima_esecuzione or (r["prima_vista"] or "") > ultima_esecuzione
             or r["id"] not in stato["bandi"]}
    rimossi = len(attivi_prima - {r["id"] for r in attivi})

    # salvataggio stato (senza i testi completi)
    nuovi_bandi = {}
    for r in attivi:
        s = {k: v for k, v in r.items() if not k.startswith("_")}
        s["_origine"] = r.get("_origine", "api")
        s["_ultima_vista"] = r.get("_ultima_vista") if not r["_testo"] else iso(OGGI)
        s["_attivo"] = True
        nuovi_bandi[r["id"]] = s
    stato["bandi"] = nuovi_bandi
    stato["llm"] = {k: v for k, v in stato["llm"].items() if k in nuovi_bandi}
    durata = round((time.time() - t0) / 60, 1)
    stato["esecuzioni"].append({"data": datetime.now().strftime("%Y-%m-%d %H:%M"), "esaminati": esaminati_tot,
                                "nuovi": len(nuovi), "rimossi": rimossi,
                                "errori": "; ".join(errori)[:2000] or "Nessuno", "durata_min": durata,
                                "llm_analizzati": analizzati})

    file_excel = BASE / "report" / f"NeuroGrant_{OGGI.isoformat()}.xlsx"
    scrivi_excel(file_excel, attivi, stato)
    ultimo = BASE / "report" / "NeuroGrant_ultimo.xlsx"
    ultimo.write_bytes(file_excel.read_bytes())
    file_excel.unlink()
    salva_stato(percorso_stato, stato)
    log.info("Excel: %d bandi attivi (%d nuovi, %d rimossi), LLM su %d bandi, %.1f minuti",
             len(attivi), len(nuovi), rimossi, analizzati, durata)

    if args.no_email:
        return
    utente = os.environ.get("GMAIL_USER", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    destinatario = os.environ.get("MAIL_TO", "").strip() or utente
    if not (utente and password):
        log.warning("GMAIL_USER o GMAIL_APP_PASSWORD mancanti: email non inviata (l'Excel è nel repository)")
        return
    allegato = BASE / "report" / f"NeuroGrant_{OGGI.isoformat()}.xlsx"
    allegato.write_bytes(ultimo.read_bytes())
    try:
        invia_email(componi_email(attivi, nuovi, allegato, utente, destinatario), utente, password)
        log.info("Email inviata a %s", destinatario)
    finally:
        allegato.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
