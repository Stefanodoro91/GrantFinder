#!/usr/bin/env python3
"""
NeuroGrant – monitoraggio settimanale dei bandi di ricerca clinica sulla
riabilitazione (ictus, Parkinson, invecchiamento, tecnologie per la riabilitazione),
aperti a enti italiani.

Fonti:
  * EU Funding & Tenders Portal (tutti i programmi UE) – API pubblica
  * Grants.gov (tutte le agenzie federali USA, incluso NIH) – API pubblica
  * Siti web di ministeri, regioni, fondazioni bancarie, charity e
    fondazioni internazionali elencati in fonti_web.csv – lettura delle pagine

Flusso:
  1. raccoglie i bandi aperti o in apertura;
  2. tiene quelli pertinenti (filtro a parole chiave) e scarta subito quelli
     non aperti all'Italia, oncologici, preclinici o di infrastruttura;
  3. con un LLM (Gemini, API gratuita; in alternativa Ollama in locale) legge la
     call, giudica pertinenza, tipo di ricerca e ammissibilita' dell'Italia,
     scrive titolo e sommario in italiano; scarta i bandi non pertinenti;
  4. calcola copertura finanziaria, progetti finanziabili e competitivita';
  5. ordina (Italia ammessa -> pertinenza -> scadenza), genera l'Excel
     (fogli BANDI, FONTI, LOG) e lo invia per email.

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
from urllib.parse import unquote, urljoin, urlparse, urldefrag
from urllib.robotparser import RobotFileParser

import requests

log = logging.getLogger("neurogrant")
BASE = Path(__file__).resolve().parent
OGGI = date.today()

USER_AGENT = ("NeuroGrantBot/0.1 (monitoraggio bandi di ricerca sulla riabilitazione; "
              "rispetta robots.txt)")

# ---------------------------------------------------------------------------
# Filtro tematico: ampio ma solo sanitario.
# Ogni voce: (gruppo, espressione regolare, tipo)
#   S = specifico: conta sempre
#   N = generico: conta solo con un contesto clinico nel testo
#   D = disabilità: conta solo con un contesto clinico (esclude inclusione
#       sociale, lavoro, accessibilità, urbanistica)
#   C = malattia cronica non trasmissibile: conta solo con riabilitazione
#   R = "riabilitazione": conta solo se vicino c'è un contesto clinico
#       (esclude riabilitazione di edifici, habitat, detenuti, infrastrutture)
# Un bando entra se il punteggio totale è almeno 3: per esempio un termine
# specifico nel titolo, oppure due termini specifici nel testo.
# ---------------------------------------------------------------------------

TERMINI = [
    # Ictus
    ("Ictus", r"\bstrokes?\b(?!\s+(of|count|width|engine|rate|volume))", "S"),
    ("Ictus", r"\bictus\b", "S"),
    ("Ictus", r"cerebrovascula|cerebrovascolar", "S"),
    ("Ictus", r"\baphasia|\bafasia", "S"),
    ("Ictus", r"hemipare|emipare|hemipleg|emipleg", "S"),
    # Parkinson
    ("Parkinson", r"parkinson", "S"),
    ("Parkinson", r"movement disorder|disturbi del movimento", "S"),
    ("Parkinson", r"deep brain stimulation|stimolazione cerebrale profonda", "S"),
    # Invecchiamento
    ("Invecchiamento", r"\bdementi|\bdemenz", "S"),
    ("Invecchiamento", r"alzheimer", "S"),
    ("Invecchiamento", r"cognitive (decline|impairment)|declino cognitivo|decadimento cognitivo", "S"),
    ("Invecchiamento", r"healthy (ageing|aging)|active (and|&) assisted living|invecchiamento (attivo|in salute|sano)", "N"),
    ("Invecchiamento", r"\bfrailty|fragilita", "S"),
    ("Invecchiamento", r"geriatr|gerontolog", "S"),
    ("Invecchiamento", r"falls? prevention|prevenzione (delle )?cadute", "S"),
    ("Invecchiamento", r"older (adults|people|persons|population)|elderly|\banzian|terza eta", "N"),
    ("Invecchiamento", r"\b(ageing|aging)\b|invecchiamento|longevit", "N"),
    # Tecnologie per la riabilitazione
    ("Tecnologie riabilitazione", r"assistive (technolog|device)|tecnologie assistive|\bausili", "S"),
    ("Tecnologie riabilitazione", r"exoskeleton|esoschelet", "S"),
    ("Tecnologie riabilitazione", r"brain[- ]computer interface|brain[- ]machine interface", "S"),
    ("Tecnologie riabilitazione", r"tele-?rehabilit|teleriabilit", "S"),
    ("Tecnologie riabilitazione", r"rehabilitation (robot|technolog|engineering)|robotic rehabilit|riabilitazione robotica", "S"),
    ("Tecnologie riabilitazione", r"digital therapeut|terapie digitali", "S"),
    ("Tecnologie riabilitazione", r"functional electrical stimulation|elettrostimolazione funzionale", "S"),
    ("Tecnologie riabilitazione", r"neuroprosthe|neuroprotes", "S"),
    ("Tecnologie riabilitazione", r"\brobot", "N"),
    ("Tecnologie riabilitazione", r"wearable|indossabil", "N"),
    ("Tecnologie riabilitazione", r"virtual reality|realta virtuale|exergam|serious game", "N"),
    ("Tecnologie riabilitazione", r"neuromodulat|neurostimulat|\btms\b|\btdcs\b|transcranial", "N"),
    ("Tecnologie riabilitazione", r"\bprosthe|\bprotesi", "N"),
    ("Tecnologie riabilitazione", r"\bgait\b|mobility aid|deambulaz", "N"),
    # Altre malattie (neurologiche, croniche, disabilità) e riabilitazione
    ("Altre malattie e riabilitazione", r"rehabilitat|riabilitaz|\brehab\b", "R"),
    ("Altre malattie e riabilitazione", r"physiotherap|fisioterap|physical therap", "S"),
    ("Altre malattie e riabilitazione", r"occupational therap|terapia occupazionale|ergoterap", "S"),
    ("Altre malattie e riabilitazione", r"motor (recovery|function|learning)|recupero (motorio|funzionale)", "S"),
    ("Altre malattie e riabilitazione", r"speech (and language )?therap|logoped", "S"),
    ("Altre malattie e riabilitazione", r"multiple sclerosis|sclerosi multipla", "S"),
    ("Altre malattie e riabilitazione", r"amyotrophic lateral sclerosis|sclerosi laterale amiotrofica|motor neuron disease", "S"),
    ("Altre malattie e riabilitazione", r"neurodegenerat", "S"),
    ("Altre malattie e riabilitazione", r"huntington|ataxia|atassia|neuromuscular|neuromuscolar|muscular dystroph|distrofia muscolare", "S"),
    ("Altre malattie e riabilitazione", r"spinal cord injur|lesion[ei] (del )?midoll|mielolesion", "S"),
    ("Altre malattie e riabilitazione", r"(acquired|traumatic) brain injur|cerebrolesion|trauma cranico", "S"),
    ("Altre malattie e riabilitazione", r"cerebral palsy|paralisi cerebrale", "S"),
    ("Altre malattie e riabilitazione", r"neurological (disorder|disease|condition)|malattie neurologiche|neurologic(al)? rehabil", "S"),
    ("Altre malattie e riabilitazione", r"chronic pain|dolore cronico", "S"),
    ("Altre malattie e riabilitazione", r"\bdisabilit|persons with disabilities|people with disabilities|disabled (people|persons)", "D"),
    ("Altre malattie e riabilitazione", r"non-?communicable disease|malattie non trasmissibili|chronic (disease|condition)|malattie croniche|cronicita|multimorbid", "C"),
    ("Altre malattie e riabilitazione", r"cardiovascular|heart failure|cardiac|cardiolog|cardiovascolar|scompenso cardiaco", "C"),
    ("Altre malattie e riabilitazione", r"\bcopd\b|bpco|pulmonary|respiratory|polmonar|respiratori", "C"),
    ("Altre malattie e riabilitazione", r"diabet|obesit", "C"),
    # termini in spagnolo, portoghese, francese e tedesco (fonti estere senza pagina inglese)
    ("Ictus", r"accidente cerebrovascular|accident vasculaire|\bavc\b|schlaganfall", "S"),
    ("Invecchiamento", r"\bdemenc|\bdemence|envejecimiento saludable|vieillissement", "S"),
    ("Altre malattie e riabilitazione", r"rehabilitaci|reabilitac|readaptation|reeducation", "R"),
    ("Altre malattie e riabilitazione", r"fisioterapia|kinesitherap|physiotherapie", "S"),
    ("Altre malattie e riabilitazione", r"esclerosis multiple|esclerose multipla|sclerose en plaques|multiple sklerose", "S"),
    ("Altre malattie e riabilitazione", r"lesion medular|lesao medular|lesion de la moelle|querschnitt", "S"),
    ("Altre malattie e riabilitazione", r"discapacidad|deficiencia fisica|handicap|behinderung", "D"),
    # Ricerca clinica in qualsiasi area medica (le diciture NIH «Clinical Trial Not Allowed/Optional/Required» non contano)
    ("Ricerca clinica", r"randomi[sz]ed (controlled )?trials?|pragmatic (clinical )?trials?|efficacy trials?|effectiveness trials?|"
                        r"phase (i{1,3}|[123])[ab]? (clinical )?trials?|clinical trials? (network|planning|implementation|readiness|site)|"
                        r"comparative effectiveness|patient[- ]cent(er|re)d outcomes|health services research|"
                        r"implementation (science|research)|outcomes research|real[- ]world (evidence|data)|"
                        r"clinical (research|investigation|epidemiolog)|ricerca clinica|sperimentazion[ei] clinic|studi clinici|"
                        r"ensayos? clinicos?|investigacion clinica|ensaios? clinicos?|pesquisa clinica|essais? cliniques?|"
                        r"recherche clinique|klinische (studie|forschung)", "S"),
    ("Ricerca clinica", r"\bclinical trials?\b(?!\s*\(?\s*(not allowed|optional|required))|\bpatients?\b|\bpazienti\b", "N"),
    # Ingegneria clinica e biomedica, tecnologie per la salute
    ("Ingegneria clinica", r"biomedical engineering|clinical engineering|bioengineering|rehabilitation engineering|"
                           r"medical (devices?|technolog|imaging)|health(care)? technolog|digital health|\bmhealth\b|"
                           r"\be-?health\b|telemedicine|telehealth|point[- ]of[- ]care|diagnostic (devices?|technolog)|"
                           r"ingegneria (clinica|biomedica)|dispositivi medici|sanita digitale|tecnologie (sanitarie|biomediche)|"
                           r"ingenieria biomedica|engenharia biomedica|genie biomedical|medizintechnik", "S"),
    ("Ingegneria clinica", r"artificial intelligence|machine learning|deep learning|\bsensors?\b|intelligenza artificiale", "N"),
    # spagnolo e portoghese: ricerca clinica, salute, ingegneria
    ("Ricerca clinica", r"estudios? clinicos?|investigacion en salud|pesquisa em saude|estudos? clinicos?|"
                        r"investigacion biomedica|pesquisa biomedica|salud publica|saude publica", "S"),
    ("Ingegneria clinica", r"dispositivos medicos|tecnologias? (en|em|para la|para a) salud|tecnologias? em saude|"
                           r"salud digital|saude digital|telemedicina|telessaude", "S"),
    # giapponese, coreano, cinese (fonti senza pagina inglese)
    ("Altre malattie e riabilitazione", r"リハビリテーション|理学療法|作業療法|재활|물리치료|작업치료|康复|物理治疗|作业治疗", "S"),
    ("Ictus", r"脳卒中|뇌졸중|脑卒中|卒中", "S"),
    ("Ricerca clinica", r"臨床研究|臨床試験|治験|임상연구|임상시험|临床研究|临床试验", "S"),
    ("Ingegneria clinica", r"医療機器|의료기기|医疗器械|生体医工学|의공학|生物医学工程", "S"),
]

# contesto clinico: servono almeno due espressioni diverse (una sola citazione
# di "salute" o "pazienti" in un bando sociale o ambientale non basta)
CONTESTO_CLINICO = re.compile(
    r"\b(clinical|clinic[aoi]?|patients?|pazient[ei]|trials?|sperimentazion[ei] clinic\w*|therap\w*|terapi\w*|"
    r"rehabilit\w*|riabilit\w*|hospitals?|ospedal\w*|neurolog\w*|diagnos\w*|treatments?|"
    r"physiother\w*|fisioterap\w*|medical|medic[oi]|disease|disorders?|malattia|malattie|"
    r"pacientes?|patienten|clinico|clinique|klinisch\w*|ensayos?|essais?|ensaios?|enfermedad\w*|doenca\w*|maladie\w*|krankheit\w*)\b", re.I)
# per le malattie croniche serve un contesto riabilitativo vero e proprio
CONTESTO_CRONICITA = re.compile(
    r"(rehabilit|riabilit|physical therap|fisioterap|exercise (therapy|training|intervention|program)|"
    r"esercizio terapeutico|functional (capacity|recovery|decline|status)|recupero funzionale|"
    r"self[- ]management|autogestione|telemonitor|tele-?rehab|teleriabil)", re.I)
VICINO_CLINICO = re.compile(
    r"(patient|pazient|stroke|ictus|physical|fisic|motor|clinic|health|salute|sanit|disabilit|neurolog|"
    r"cognit|therap|terap|medic|function|funzional|injur|lesion|disease|malatti|cardiac|cardi|pulmonary|"
    r"older|\banzian|elderly|sclerosis|sclerosi|parkinson|dementi|pacient|klinik|salud|saude|sante|gesundheit)", re.I)
# frasi standard (diritti civili, non discriminazione) che citano disabilità o
# "Rehabilitation Act" senza che il bando riguardi la sanità
BOILERPLATE = re.compile(
    r"(on the basis of (race|colou?r|national origin|sex|age|disability|religion)|section 50[48]\b|"
    r"rehabilitation act|americans with disabilities act|civil rights|age discrimination act|"
    r"non-?discriminat|equal (employment )?opportunit|reasonable accommodation|limited english proficiency|"
    r"disadvantaged business|accessible to (individuals|persons|people) with disabilities|"
    r"accessibility (requirements|standards)|non discriminazione|pari opportunita)", re.I)

GRUPPI_ORDINE = ["Ictus", "Parkinson", "Invecchiamento", "Tecnologie riabilitazione",
                 "Altre malattie e riabilitazione", "Ricerca clinica", "Ingegneria clinica"]
TEMA_LIBERO = "Ricerca sanitaria a tema libero"
# bando sanitario ampio: ricerca medica o clinica senza un tema imposto
RX_SALUTE = re.compile(r"(health|medical|clinical|biomedical|life sciences?|neuroscien|sanitari|medic[aio]|clinic[aoi]|"
                       r"salud|saude|sante|gesundheit)", re.I)
RX_TEMA_LIBERO = re.compile(
    r"(investigator[- ]initiated|investigator[- ]driven|researcher[- ]initiated|bottom[- ]up|responsive mode|"
    r"open (topic|call|theme|competition)|any (area|field|topic|discipline)|all (areas|fields|disciplines|topics)|"
    r"(regardless|irrespective) of (the )?(field|topic|discipline)|ideas? grants?|project grants?|"
    r"postdoctoral fellowships?|research fellowships?|tema libero|qualsiasi (area|ambito)|"
    r"tema abierto|cualquier (area|disciplina)|qualquer area|tous les domaines)", re.I)
# apertura a ricercatori o enti di altri Paesi
RX_ESTERO = re.compile(
    r"(international (applicants|researchers|scientists|investigators|students)|foreign (researchers|nationals|"
    r"applicants|scientists|institutions|organi[sz]ations)|researchers? from (abroad|other countries|any country|"
    r"all countries|overseas)|from abroad|overseas researchers|(regardless|irrespective) of nationality|"
    r"any nationality|all nationalities|non-?(japanese|korean|chinese|singaporean|australian|brazilian) (researchers|nationals)|"
    r"open to (researchers|applicants|scientists) (from|of) (all|any)|any country|worldwide|"
    r"investigadores extranjeros|pesquisadores estrangeiros|chercheurs etrangers)", re.I)
SOGLIA = 3


def norm(t: str | None) -> str:
    if not t:
        return ""
    t = unicodedata.normalize("NFKD", t)
    return "".join(c for c in t if not unicodedata.combining(c))


# parole chiave con la stessa normalizzazione del testo (accenti, hangul, kana)
_TERMINI_C = [(g, re.compile(norm(p), re.I), t) for g, p, t in TERMINI]


def togli_boilerplate(testo: str) -> str:
    frasi = re.split(r"(?<=[.;!?])\s+|\n+", testo)
    return " ".join(f for f in frasi if not BOILERPLATE.search(f))


def _riab_clinica(testo: str) -> bool:
    for m in re.finditer(r"rehabilitat|riabilitaz|\brehab\b", testo, re.I):
        finestra = testo[max(0, m.start() - 150):m.end() + 150]
        if VICINO_CLINICO.search(finestra.replace(m.group(0), " ")):
            return True
    return False


def classifica(titolo: str, testo: str = "") -> tuple[list[str], list[str]]:
    """Restituisce (macrogruppi, parole chiave) se il bando supera la soglia, altrimenti ([], [])."""
    tit = norm(togli_boilerplate(titolo or ""))
    corpo = norm(togli_boilerplate(testo or ""))
    tutto = f"{tit} {corpo}"
    clinico = len({m.group(0).lower()[:6] for m in CONTESTO_CLINICO.finditer(tutto)}) >= 2
    cronicita = bool(CONTESTO_CRONICITA.search(tutto))
    riab_ok = _riab_clinica(tutto)
    punteggio, gruppi, parole = 0, {}, []
    for gruppo, rx, tipo in _TERMINI_C:
        nel_titolo = bool(rx.search(tit))
        occorrenze = len(rx.findall(corpo))
        if not (nel_titolo or occorrenze):
            continue
        if (tipo in ("N", "D") and not clinico) or (tipo == "C" and not cronicita) or (tipo == "R" and not riab_ok):
            continue
        peso = 1 if tipo == "N" else 2
        peso += 1 if nel_titolo else 0
        peso += 1 if occorrenze >= 3 else 0
        punteggio += peso
        gruppi[gruppo] = gruppi.get(gruppo, 0) + peso
        m = rx.search(tit) or rx.search(corpo)
        parola = m.group(0).strip().lower()
        if parola not in parole:
            parole.append(parola)
    if punteggio < SOGLIA:
        return [], []
    # il gruppo generico solo se non ci sono gruppi più specifici con peso comparabile
    altri = [g for g in gruppi if g not in ("Altre malattie e riabilitazione", "Ricerca clinica", "Ingegneria clinica")]
    if altri and gruppi.get("Altre malattie e riabilitazione", 0) < max(gruppi[g] for g in altri):
        gruppi.pop("Altre malattie e riabilitazione", None)
    return [g for g in GRUPPI_ORDINE if g in gruppi], parole[:12]


# ---------------------------------------------------------------------------
# Esclusioni per regola (prima dell'LLM, sul titolo originale)
#   - oncologia e tumori;
#   - infrastrutture (centri di coordinamento, archivi dati, reti, NIH U24/P30...);
#   - ricerca preclinica (modelli animali, in vitro).
# Il resto (servizi sociali, condizioni non legate alla riabilitazione) lo
# decide l'LLM leggendo la call.
# ---------------------------------------------------------------------------

ESCLUSIONI_TITOLO = [
    ("Infrastruttura", re.compile(
        r"(\binfrastructure\b|infrastruttur|coordinating cent(er|re)|coordination cent(er|re)|"
        r"data (management|coordinating|coordination|resource|sharing) (cent(er|re)|core)|\bdmcc\b|\bdcc\b|"
        r"data archive|archivio|repositor(y|ies)|biobank|biorepositor|resource cent(er|re)|"
        r"\b(u24|p30|s10|g20|c06|u2c|u42)\b)", re.I)),
    ("Preclinica", re.compile(
        r"\b(mouse|mice|murine|rodents?|rats?|animal models?|preclinical|pre-clinical|non-human primates?|"
        r"zebrafish|drosophila|in vitro|organoids?|cell lines?|modelli animali|preclinic[ao])\b", re.I)),
]


def esclusione_titolo(titolo: str) -> str | None:
    t = html.unescape(titolo or "")
    for motivo, rx in ESCLUSIONI_TITOLO:
        if rx.search(t):
            return motivo
    return None


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
    elif s.count(".") > 1 or re.fullmatch(r"\d{1,3}\.\d{3}", s):
        s = s.replace(".", "")          # 10.000 = diecimila (separatore delle migliaia)
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
                italia=None, italia_fonte=None, tipo_ricerca=None,
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
# Programmi e cluster UE senza ricerca clinica: scartati prima del filtro tematico.
# CL2 cultura e società, CL3 sicurezza, CL5 clima ed energia, CL6 alimentazione e
# ambiente, Missions (compresa quella sul cancro), infrastrutture, Euratom. Modificabile.
EU_PREFISSI_ESCLUSI = re.compile(
    r"^(CERV|JUST|LIFE|AGRIP|EMFAF|RFCS|EDF|CEF|UCPM|CREA|EUAF|SOCPL|ERASMUS|ESF|SMP|I3|EIE|"
    r"HORIZON-CL[2356]|HORIZON-MISS|HORIZON-INFRA|HORIZON-EURATOM|EURATOM|HORIZON-WIDERA|HORIZON-EIE)\b", re.I)


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
    titolo = html.unescape(str(_meta(m, "title") or v.get("title") or ident or ""))
    if not ident or EU_PREFISSI_ESCLUSI.search(str(ident)):
        return None
    descr = html_a_testo(_meta(m, "descriptionByte") or v.get("summary") or "")
    parole = " ".join(m.get("keywords") or []) + " " + " ".join(m.get("tags") or [])
    testo = f"{titolo}. {parole}. {descr}"
    gruppi, chiavi = classifica(titolo, f"{parole}. {descr}")
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
        italia="Sì", lingua="Inglese", url=url, stato=stato, _testo=testo[:8000])


def copertura_eu(ident: str, tipi: str) -> str | None:
    t, i = tipi.lower(), ident.upper()
    if "cofund" in t or "COFUND" in i:
        return "Co-finanziamento richiesto (azione Cofund)"
    if i.startswith("EU4H"):
        return "Co-finanziamento richiesto (di norma 60% UE, fino all'80%)"
    if "EIC" in i and "ACCELERATOR" in i:
        return "Co-finanziamento richiesto (componente grant 70% UE)"
    if "EIC" in i and ("PATHFINDER" in i or "TRANSITION" in i):
        return "Interamente finanziato"
    if "research and innovation" in t or "-RIA" in i or "coordination and support" in t or "-CSA" in i:
        return "Interamente finanziato"
    if "innovation action" in t or "-IA" in i:
        return "Co-finanziamento richiesto per le imprese (70% UE); interamente finanziato per gli enti non profit"
    if i.startswith(("ERC", "HORIZON-ERC", "HORIZON-MSCA", "MSCA")):
        return "Interamente finanziato"
    if i.startswith("DIGITAL") and "simple grant" in t:
        return "Co-finanziamento richiesto (di norma 50% UE)"
    return None


def normalizza_copertura(c: str | None) -> str:
    """Converte anche le diciture delle versioni precedenti."""
    if not c:
        return "Non indicato"
    cl = c.lower()
    if cl.startswith("100%") or "nessun cost sharing" in cl:
        return "Interamente finanziato"
    if cl.startswith("possibile co-finanziamento"):
        return "Co-finanziamento da verificare"
    if cl.startswith("co-finanziamento:"):
        if "60%" in cl:
            return "Co-finanziamento richiesto (di norma 60% UE, fino all'80%)"
        if "50%" in cl:
            return "Co-finanziamento richiesto (di norma 50% UE)"
        return "Co-finanziamento richiesto"
    if cl.startswith("70% per le imprese"):
        return "Co-finanziamento richiesto per le imprese (70% UE); interamente finanziato per gli enti non profit"
    if cl == "co-finanziamento richiesto (cost sharing)":
        return "Co-finanziamento richiesto"
    return c


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
# Agenzie federali senza bandi sanitari: escluse prima di scaricare i dettagli
# (agricoltura, trasporti, interni, ambiente, giustizia, edilizia, energia, commercio,
# sicurezza interna, esteri, tesoro, piccole imprese). Modificabile.
GG_AGENZIE_ESCLUSE = {"USDA", "DOT", "DOI", "EPA", "DOJ", "HUD", "DOE", "DOC", "DHS", "DOS",
                      "TREAS", "SBA", "NRC", "FCC", "USDOJ", "DOD-USACE"}
GG_PAROLE = ["multiple sclerosis", "amyotrophic lateral sclerosis", "spinal cord injury",
             "cardiac rehabilitation", "pulmonary rehabilitation", "chronic disease",
             "neurological disorders", "digital therapeutics", "rehabilitation", "stroke", "Parkinson", "aging", "older adults", "dementia",
             "Alzheimer", "assistive technology", "disability", "neurorehabilitation",
             "physical therapy", "exoskeleton", "mobility", "falls", "neurodegenerative",
             "brain injury", "prosthetic", "telerehabilitation", "geriatric", "cognitive decline",
             "frailty", "robotics", "motor recovery", "aphasia", "wearable",
             # ricerca clinica e ingegneria clinica (focus esteso)
             "clinical trial", "clinical research", "randomized", "comparative effectiveness",
             "implementation science", "health services research", "patient-centered outcomes",
             "biomedical engineering", "bioengineering", "medical device", "digital health", "telehealth"]


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
                agenzia = str(h.get("agencyCode") or "").upper()
                if any(agenzia == a or agenzia.startswith(a + "-") for a in GG_AGENZIE_ESCLUSE):
                    continue
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
            # per i bandi NIH decide il testo completo della call, non la scheda di Grants.gov
            valore, frase = italia_nih(http, str(hit.get("number") or ""))
            if valore:
                rec["italia"], rec["italia_fonte"] = valore, f"NIH Guide {hit.get('number')}: {frase}"
                rec["paesi"] = {"Sì": "Ammessi anche enti non statunitensi (call NIH completa)",
                                "No": "Enti esteri non ammessi (call NIH completa)"}[valore]
            risultati.append(rec)
        time.sleep(0.2)
    return risultati, len(ids)


_RX_COMPONENTI = re.compile(r"(non-?domestic|foreign)[^.;]{0,40}?components?[^.;]*", re.I)
_RX_ESTERI_NO = re.compile(
    r"(non-?domestic|foreign)\s*(\(non-?u\.?s\.?\)\s*)?(entities|institutions|organi[sz]ations|applicants)"
    r"[^.;]{0,60}?\b(is|are)\s+not\s+(eligible|allowed|permitted)|"
    r"\bnot\s+eligible[^.;]{0,30}?(non-?domestic|foreign)\s*(\(non-?u\.?s\.?\)\s*)?(entities|institutions|organi[sz]ations)|"
    r"\bonly\s+(domestic|u\.?s\.?(-based)?)\s+(institutions|organi[sz]ations|entities|applicants)", re.I)
_RX_ESTERI_SI = re.compile(
    r"non-?domestic\s*\(non-?u\.?s\.?\)\s*entities|"
    r"(foreign|non-?domestic|international)\s+(institutions|organi[sz]ations|entities|applicants)"
    r"[^.;]{0,40}?\b(are|is)\s+(also\s+)?(eligible|allowed|permitted)|"
    r"(foreign|international)\s+(institutions|organi[sz]ations|applicants)\s+(may|can)\s+apply", re.I)


def _valori_testo(x, escludi: tuple[str, ...] = ()) -> list[tuple[str, str]]:
    """(chiave, testo) di tutti i valori di tipo stringa, anche annidati."""
    out = []
    if isinstance(x, dict):
        for k, v in x.items():
            if k in escludi:
                continue
            if isinstance(v, str):
                out.append((k, v))
            else:
                out += _valori_testo(v, escludi)
    elif isinstance(x, list):
        for v in x:
            out += _valori_testo(v, escludi)
    return out


def italia_grants_gov(syn: dict, tipi_ente: list[str]) -> str:
    """'Sì', 'No' o 'Da verificare' per un ente italiano come richiedente.
    Cerca nei valori del record (i nomi dei campi di Grants.gov variano) le frasi
    standard sugli enti esteri, ignorando le 'foreign components' di enti USA."""
    if not syn:
        return "Da verificare"
    valori = _valori_testo(syn)
    t = html_a_testo(" ".join(v for _, v in valori))
    t = _RX_COMPONENTI.sub(" ", t)
    if _RX_ESTERI_NO.search(t):
        return "No"
    # Gli enti esteri citati nella scheda di Grants.gov NON bastano per dire «Sì»: la scheda è un riassunto
    # e spesso li elenca anche quando il testo completo della call li esclude. Conferma solo dalla call
    # completa (per il NIH: italia_nih) o da una frase citata dall'LLM e ritrovata nel testo.
    if _RX_ESTERI_SI.search(t) or any(re.search(r"foreign|non-?domestic", x, re.I) for x in tipi_ente):
        return "Da verificare"
    if any(re.search(r"unrestricted", x, re.I) for x in tipi_ente):
        return "Da verificare"
    # testo sull'ammissibilità (escluse le descrizioni dei tipi di ente)
    elig = html_a_testo(" ".join(v for k, v in _valori_testo(syn, ("applicantTypes",)) if re.search(r"eligib", k, re.I)))
    if len(elig) < 40 or re.search(r"\bsee (section|the nofo|nofo|the (full )?(funding )?(opportunity|announcement))", elig, re.I):
        return "Da verificare"
    return "No"                         # elenco di enti ammessi senza enti esteri: riservato agli USA


NIH_GUIDE = "https://grants.nih.gov/grants/guide/{cartella}/{numero}.html"
_RX_NUMERO_NIH = re.compile(r"^(RFA|PAR|PAS|PA|NOT|OTA)-([A-Z]{2}-)?\d{2}-\d{3}$", re.I)
_RX_NIH_ESTERI = re.compile(
    r"non-?domestic\s*\(non-?u\.?s\.?\)\s*entities\s*\(foreign\s+(?:organi[sz]ations|institutions)\)\s*"
    r"(?:are|is)\s+(not\s+)?(?:eligible|allowed|permitted)[^.]*\.", re.I)


def italia_nih(http: "Http", numero_call: str) -> tuple[str | None, str | None]:
    """Legge la sezione «Foreign Organizations» della call completa sul NIH Guide.
    Restituisce ('Sì' | 'No', frase ufficiale) oppure (None, None) se la pagina o la frase non si trovano."""
    numero_call = (numero_call or "").strip().upper()
    if not _RX_NUMERO_NIH.match(numero_call):
        return None, None
    pref = numero_call.split("-")[0]
    cartella = {"RFA": "rfa-files", "NOT": "notice-files", "OTA": "notice-files"}.get(pref, "pa-files")
    try:
        r = http.get(NIH_GUIDE.format(cartella=cartella, numero=numero_call), cortese=True, timeout=45)
        if r.status_code != 200:
            return None, None
    except Exception as e:
        log.debug("NIH Guide %s: %s", numero_call, e)
        return None, None
    m = _RX_NIH_ESTERI.search(html_a_testo(r.text))
    if not m:
        return None, None
    return ("No" if m.group(1) else "Sì"), m.group(0).strip()


def converti_grants_gov(hit: dict, det: dict) -> dict | None:
    syn = det.get("synopsis") or det.get("forecast") or {}
    titolo = html.unescape(str(hit.get("title") or det.get("opportunityTitle") or ""))
    descr = html_a_testo(_primo(syn, "synopsisDesc", "forecastDesc", "description") or "")
    eleggibilita = html_a_testo(str(_primo(syn, "applicantEligibilityDesc", "applicantEligibilityDescription",
                                           "eligibilityDesc", "additionalEligibilityInfo") or ""))
    testo = f"{titolo}. {descr}" + (f" Eligibility: {eleggibilita}" if eleggibilita else "")
    gruppi, chiavi = classifica(titolo, descr)
    if not gruppi:
        return None
    stato = "In apertura" if (hit.get("oppStatus") or "").lower() == "forecasted" else "Aperto"
    scad = data_da_testo(hit.get("closeDate") or "", formato_usa=True) or \
        data_da_testo(str(_primo(syn, "responseDate", "estApplicationResponseDate") or ""), formato_usa=True)
    apertura = data_da_testo(hit.get("openDate") or "", formato_usa=True)
    if scad and scad < OGGI:
        return None

    tipi_ente = [t.get("description", "") for t in (_primo(syn, "applicantTypes") or []) if isinstance(t, dict)]
    italia = italia_grants_gov(syn, tipi_ente)
    cost = _primo(syn, "costSharing", "costSharingOrMatchingRequirement")
    if isinstance(cost, str):
        cost = cost.strip().lower() in ("true", "yes", "y")
    copertura = None
    if cost is True:
        copertura = "Co-finanziamento richiesto"
    elif cost is False:
        copertura = "Interamente finanziato"
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
        paesi={"Sì": "Ammessi anche enti non statunitensi", "No": "Solo enti statunitensi"}.get(
            italia, "Enti esteri: da verificare nel bando"),
        italia=italia, lingua="Inglese", url=GG_URL.format(id=hit.get("id")), stato=stato, _testo=testo[:9000])


# ---------------------------------------------------------------------------
# Fonte 3 – Siti web (ministeri, regioni, fondazioni, charity) da fonti_web.csv
# ---------------------------------------------------------------------------

RX_LINK_BANDO = re.compile(
    r"(bando|bandi|avvis|call|grant|funding|finanziament|opportunit|fellowship|award|"
    r"research[-_ ]?(programme|program|fund)|ricerca|apply|contribut|premi|programm|scheme|"
    r"convocatori|chamad|edita|appel|ausschreibung|forderung|foerder|financia|financement|subvenc|beca|bolsa|"
    r"公募|募集|研究費|공모|모집|과제|申报|指南|项目)", re.I)
RX_PAGINA_BANDO = re.compile(
    r"(bando|avviso|call for (proposals|applications)|grant|funding (opportunity|call|round)|"
    r"fellowship|award|finanziament|application deadline|how to apply|come partecipare|scadenza|"
    r"convocatoria|chamada|edital|appel a projets|appel a candidatures|ausschreibung|bekanntmachung|financiacion|financiamento|"
    r"公募|募集要項|공모|공고|申报指南|项目指南|征集)", re.I)
# pagine che non sono bandi: notizie, elenchi di vincitori, portfolio, moduli...
RX_URL_NON_BANDO = re.compile(
    r"/(news|notizie|notizia|press|comunicati|blog|stories|story|events?|eventi|webinars?|podcasts?|faqs?|"
    r"privacy|cookies?|cookie-policy|careers?|jobs|lavora-con-noi|about|about-us|chi-siamo|team|staff|"
    r"donate|dona|shop|governance|our-research|research-we-fund|what-we-fund|funded-research|"
    r"progetti-finanziati)(/|$)|"
    r"(recipients?|awardees?|grantees?|vincitori|portfolio|annual-report|template|nomination|"
    r"press-release|comunicato-stampa)", re.I)
RX_TITOLO_NON_BANDO = re.compile(
    r"\b(recipients?|awardees?|grantees?|vincitori|portfolio|templates?|nomination|award conditions|"
    r"terms and conditions|committee|annual report|press release|comunicato stampa|newsletter|celebrat\w+|"
    r"announces?|receives?|we'?re investing|we are investing|meet\b|research we fund|what research)", re.I)
# senza una scadenza futura la pagina deve almeno dire che la call è aperta
RX_APERTO = re.compile(
    r"(applications? (are |is )?(now )?open|now open|open for (applications|submissions|proposals)|"
    r"apply (now|online|here|by)|call (is )?open|currently accepting|accepting (applications|proposals|submissions)|"
    r"candidature (aperte|entro)|domande (entro|possono essere presentate)|"
    r"presentazione delle (domande|proposte|candidature)|bando aperto|deadline|closing date|scadenza|"
    r"plazo|prazo|date limite|einreichungsfrist|abierta|aberta|ouvert|締切|応募期間|마감|접수기간|截止|申报时间|"
    r"termine (per la presentazione|ultimo))", re.I)
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
        if prof > 0 and RX_URL_NON_BANDO.search(urlparse(url).path):
            continue
        if not http.consentito(url):
            log.debug("robots.txt non consente %s", url)
            continue
        try:
            r = http.get(url, cortese=True, timeout=60)
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
                titolo = unquote(Path(urlparse(url).path).stem).replace("\ufeff", "")
                titolo = re.sub(r"^[\d\s_-]+", "", re.sub(r"[-_]+", " ", titolo)).strip()
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
    if RX_TITOLO_NON_BANDO.search(titolo or "") or RX_URL_NON_BANDO.search(urlparse(url).path):
        return None
    anni = [int(a) for a in re.findall(r"\b(20\d\d)\b", titolo or "")]
    if anni and max(anni) < OGGI.year:
        return None                      # bando di un anno passato
    if esclusione_titolo(titolo):
        return None
    if not (RX_PAGINA_BANDO.search(titolo) or RX_PAGINA_BANDO.search(url) or
            RX_PAGINA_BANDO.search(testo[:3000])):
        return None
    gruppi, chiavi = classifica(titolo, testo[:20000])
    tema_libero = False
    if not gruppi:
        corpo = norm(f"{titolo} {testo[:20000]}")
        if not (RX_SALUTE.search(corpo) and RX_TEMA_LIBERO.search(corpo)):
            return None
        gruppi, chiavi, tema_libero = [TEMA_LIBERO], [RX_TEMA_LIBERO.search(corpo).group(0).lower()], True
    scad, solo_passate = scadenza_da_testo(testo[:30000])
    if solo_passate:
        return None                      # bando chiuso
    if not scad and not RX_APERTO.search(testo[:30000]):
        return None                      # nessun segno di call aperta
    lingua = "Italiano" if re.search(r"\b(della|degli|delle|bando|ricerca|scadenza)\b", testo[:3000], re.I) else "Inglese"
    budget = None
    m = re.search(r"(?:€|eur(?:o|os)?|\$|usd|£|gbp)\s?([\d.,]{4,})|([\d.,]{4,})\s?(?:€|euro|eur\b)", testo[:20000], re.I)
    if m:
        budget = numero(m.group(1) or m.group(2))
        if budget and budget < 1000:
            budget = None                # marche da bollo, quote di iscrizione, numeri spuri
    valuta = None
    if budget:
        frammento = m.group(0).lower()
        valuta = "USD" if ("$" in frammento or "usd" in frammento) else "GBP" if ("£" in frammento or "gbp" in frammento) else "EUR"
    cof = None
    if re.search(r"co-?financ|co-?finanziament|cofinanziament|matching fund|cost[- ]shar|quota a carico", testo[:30000], re.I):
        cof = "Co-finanziamento da verificare"
    return nuovo_record(
        id="WEB-" + hashlib.sha1(url.encode()).hexdigest()[:12], fonte=fonte.get("nome"),
        programma=titolo[:200], ente=fonte.get("nome"), titolo=titolo[:300],
        macrogruppi=gruppi, parole_chiave=chiavi, budget_totale=budget, valuta=valuta,
        scadenza=iso(scad), copertura=cof, lingua=lingua,
        paesi=fonte.get("copertura") or None, url=url,
        italia={"si": "Sì", "sì": "Sì", "no": "No"}.get((fonte.get("italia") or "").strip().lower(), "Da verificare"),
        stato="Aperto" if scad else "Scadenza da verificare",
        _testo=f"{titolo}. {testo}"[:8000], _origine="web" + ("-pdf" if pdf else ""), tema_libero=tema_libero)


# ---------------------------------------------------------------------------
# LLM: Gemini (API gratuita di Google) oppure, senza chiave, Ollama in locale
# ---------------------------------------------------------------------------

PROMPT_VERSIONE = "5"
ISTRUZIONI_LLM = """Sei un esperto italiano di finanziamenti per la ricerca clinica, l'ingegneria clinica e biomedica e la riabilitazione.
Ricevi un elenco JSON di call (bandi), ciascuna con un "id". Rispondi SOLO con JSON nella forma
{"bandi": [ ... ]}: un oggetto per ogni call ricevuta, con lo stesso "id", scritto in italiano
corretto. Ogni oggetto ha queste chiavi:
{
 "id": "copia esatta dell'id ricevuto",
 "e_un_bando": true se il testo descrive un'opportunità di finanziamento a cui candidarsi;
               false se è una notizia, un elenco di progetti finanziati o di vincitori, un modulo,
               una pagina informativa generica,
 "aperto": true se la call è aperta o in apertura, false se è chiusa, null se non si capisce,
 "italia_ammessa": "si" se il testo dice esplicitamente che un ente o un ricercatore con sede in Italia
               (o in qualsiasi paese, o in un gruppo di paesi che comprende l'Italia, come l'UE) può
               ricevere il finanziamento come capofila o partner beneficiario; "no" se il bando è
               riservato a enti di altri paesi o esclude gli enti esteri; null se il testo non lo dice
               chiaramente. Un semplice elenco di tipi di ente o le "foreign components" di enti USA
               NON bastano per "si",
 "italia_evidenza": la frase del testo, copiata parola per parola nella lingua originale, che giustifica
               "si" o "no"; null se italia_ammessa è null,
 "tipo_ricerca": "clinica" | "tecnologia" | "formazione" | "preclinica" | "infrastruttura" | "servizi" | "altro",
 "rilevanza": "Alta" | "Media" | "Bassa",
 "motivo": "una frase: perché ha questa rilevanza",
 "titolo_it": "traduzione italiana del titolo",
 "sommario_it": "80-130 parole: obiettivi, chi può partecipare, cosa si finanzia",
 "scadenza": "AAAA-MM-GG" oppure null,
 "budget_totale": numero oppure null,
 "budget_progetto": numero oppure null,
 "valuta": "EUR" | "USD" | "GBP" | altro codice | null,
 "progetti_finanziabili": numero intero oppure null,
 "copertura": "interamente" | "richiesto" | "non_indicato",
 "cofinanziamento_percentuale": percentuale a carico del beneficiario oppure null,
 "partenariato": "requisiti di consorzio in breve" oppure null,
 "chi_puo": "tipi di enti o persone ammessi" oppure null,
 "paesi": "paesi ammessi" oppure null,
 "durata_mesi": numero oppure null,
 "tipo_bando": "Ricerca" | "Innovazione" | "Dottorato / formazione / mobilità" | "Infrastruttura" | "Altro"
}

TIPO DI RICERCA
- clinica: studi su pazienti o persone (trial clinici, studi osservazionali, ricerca su interventi,
  percorsi di cura o servizi sanitari, valutazione degli esiti).
- tecnologia: sviluppo e validazione clinica di dispositivi, robotica, terapie digitali, ausili.
- formazione: borse, fellowship, career award, programmi di formazione alla ricerca.
- preclinica: ricerca di base, modelli animali o cellulari, scoperta di farmaci o bersagli prima
  della sperimentazione sull'uomo.
- infrastruttura: centri di coordinamento, reti, banche dati, archivi, piattaforme, attrezzature.
- servizi: erogazione di servizi sociali o assistenziali, programmi di salute pubblica, campagne
  di sensibilizzazione, inclusione lavorativa, attività per comunità, scuole o territorio.

RILEVANZA (ambito: riabilitazione, ricerca clinica, ingegneria clinica)
- Alta: dedicata alla riabilitazione, al recupero funzionale o alla gestione a lungo termine della
  disabilità in ictus, Parkinson, sclerosi multipla, SLA, altre malattie neurologiche o
  neurodegenerative, demenze, invecchiamento (fragilità, cadute), lesioni midollari o cerebrali,
  dolore cronico; oppure alla riabilitazione cardiologica, respiratoria o di altre malattie croniche
  non oncologiche; oppure alle tecnologie per la riabilitazione.
- Media: ricerca clinica in qualsiasi area medica, oncologia compresa (trial, studi su pazienti,
  epidemiologia clinica, metodologia, servizi sanitari, implementazione); ingegneria clinica o biomedica
  e tecnologie per la salute con validazione su pazienti (dispositivi, imaging, sanità digitale, IA
  clinica); bandi sulle condizioni elencate sopra non specifici della riabilitazione; bandi o fellowship
  sanitari a tema libero, anche riservati agli enti di un solo Paese.
- Bassa: ricerca di base o preclinica senza pazienti; temi non sanitari; bandi che citano questi temi
  solo di passaggio.

TRADUZIONE
titolo_it: italiano naturale e professionale, massimo 20 parole, come lo scriverebbe un ricercatore
italiano. Usa la terminologia medica italiana corretta (spinal cord injury = lesione del midollo
spinale; amyotrophic lateral sclerosis = sclerosi laterale amiotrofica (SLA); multiple sclerosis =
sclerosi multipla; award = finanziamento; investigator-initiated = promosso dal ricercatore;
counterfeiting = contraffazione; healthy ageing = invecchiamento in salute). Non tradurre parola
per parola. Lascia invariati sigle, codici (R01, U01, UG3/UH3, RIA, CSA) e nomi propri.

COPERTURA
"interamente" se il finanziamento copre tutti i costi ammissibili o il testo dice che non serve
co-finanziamento; "richiesto" se il beneficiario deve coprire una quota.

Usa solo informazioni presenti nel testo: se un dato manca scrivi null. Non inventare importi o date."""

TIPI_RICERCA = {"clinica": "Ricerca clinica", "tecnologia": "Tecnologia / innovazione",
                "formazione": "Formazione / fellowship", "preclinica": "Preclinica",
                "infrastruttura": "Infrastruttura", "servizi": "Servizi / programmi", "altro": "Altro"}


class QuotaEsaurita(Exception):
    """Nessun modello Gemini ha più quota disponibile oggi."""


class ErroreFatale(Exception):
    """Errore che non si risolve riprovando (per esempio chiave non valida)."""


def _payload(recs: list[dict], caratteri: int) -> str:
    return json.dumps([{"id": r["id"], "ente": r.get("ente"), "programma": r.get("programma"),
                        "titolo": r.get("titolo"), "testo": r["_testo"][:caratteri]} for r in recs],
                      ensure_ascii=False)


def estrai_lotto(testo: str, recs: list[dict]) -> dict[str, dict]:
    testo = re.sub(r"^\s*```(?:json)?|```\s*$", "", testo.strip())
    dati = json.loads(testo)
    if isinstance(dati, dict):
        elenco = dati.get("bandi")
        if elenco is None:
            elenco = [dati]
    else:
        elenco = dati
    elenco = [x for x in (elenco or []) if isinstance(x, dict)]
    ids = {r["id"] for r in recs}
    risultati = {str(x.get("id")): x for x in elenco if str(x.get("id")) in ids}
    if not risultati and len(elenco) == len(recs):          # id non ricopiati: si usa l'ordine
        risultati = {r["id"]: x for r, x in zip(recs, elenco)}
    return risultati


class Gemini:
    URL = "https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent"

    def __init__(self, chiave: str, modelli: list[str], rpm: float = 8):
        self.chiave, self.modelli, self.i = chiave, modelli, 0
        self.pausa, self._ultimo = 60 / rpm, 0.0
        self.nome = "gemini"
        self.s = requests.Session()

    @property
    def modello(self) -> str | None:
        return self.modelli[self.i] if self.i < len(self.modelli) else None

    def _passa_al_prossimo(self, perche: str) -> None:
        log.warning("Gemini %s: %s", self.modello, perche)
        self.i += 1
        if self.modello:
            log.info("Gemini: si prosegue con %s", self.modello)

    def analizza_lotto(self, recs: list[dict]) -> dict[str, dict]:
        corpo = {
            "systemInstruction": {"parts": [{"text": ISTRUZIONI_LLM}]},
            "contents": [{"role": "user", "parts": [{"text": _payload(recs, 6000)}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1,
                                 "maxOutputTokens": 32768},
        }
        tentativi = 0
        while self.modello:
            attesa = self.pausa - (time.time() - self._ultimo)
            if attesa > 0:
                time.sleep(attesa)
            self._ultimo = time.time()
            try:
                r = self.s.post(self.URL.format(m=self.modello), json=corpo, timeout=300,
                                headers={"x-goog-api-key": self.chiave})
            except requests.RequestException as e:
                tentativi += 1
                if tentativi >= 4:
                    raise
                log.warning("Gemini irraggiungibile (%s), nuovo tentativo", e)
                time.sleep(15 * tentativi)
                continue
            if r.status_code == 200:
                dati = r.json()
                cand = (dati.get("candidates") or [{}])[0]
                parti = (cand.get("content") or {}).get("parts") or []
                testo = "".join(p.get("text", "") for p in parti if not p.get("thought"))
                if not testo:
                    raise RuntimeError(f"risposta vuota ({cand.get('finishReason')})")
                return estrai_lotto(testo, recs)
            dettaglio = r.text[:600]
            if r.status_code == 429:
                if re.search(r"PerDay|per day|daily", r.text, re.I):
                    self._passa_al_prossimo("quota giornaliera esaurita")
                    tentativi = 0
                    continue
                tentativi += 1
                if tentativi >= 6:
                    self._passa_al_prossimo("troppe richieste respinte")
                    tentativi = 0
                    continue
                m = re.search(r'"retryDelay":\s*"(\d+)', r.text)
                time.sleep(min(int(m.group(1)) + 2 if m else 20 * tentativi, 120))
                continue
            if r.status_code in (401, 403) or (r.status_code == 400 and re.search(r"API key", r.text, re.I)):
                raise ErroreFatale(f"chiave Gemini non valida o non autorizzata (HTTP {r.status_code}): {dettaglio}")
            if r.status_code == 404 or (r.status_code == 400 and re.search(r"model|not supported", r.text, re.I)):
                self._passa_al_prossimo(f"modello non disponibile (HTTP {r.status_code})")
                tentativi = 0
                continue
            if r.status_code >= 500:
                tentativi += 1
                if tentativi >= 4:
                    raise RuntimeError(f"Gemini HTTP {r.status_code}")
                time.sleep(20 * tentativi)
                continue
            raise RuntimeError(f"Gemini HTTP {r.status_code}: {dettaglio}")
        raise QuotaEsaurita("quota gratuita esaurita su tutti i modelli configurati")


class Ollama:
    def __init__(self, url: str, modello: str):
        self.url, self.modello = url.rstrip("/"), modello
        self.nome = f"ollama:{modello}"
        self.s = requests.Session()

    def disponibile(self) -> bool:
        try:
            r = self.s.get(self.url + "/api/tags", timeout=10)
            nomi = [m.get("name", "") for m in r.json().get("models", [])]
            return any(n.startswith(self.modello.split(":")[0]) for n in nomi)
        except Exception:
            return False

    def analizza_lotto(self, recs: list[dict]) -> dict[str, dict]:
        risultati = {}
        for rec in recs:
            r = self.s.post(self.url + "/api/chat", timeout=400, json={
                "model": self.modello, "stream": False, "format": "json",
                "options": {"temperature": 0.1, "num_ctx": 6144, "num_predict": 900},
                "messages": [{"role": "system", "content": ISTRUZIONI_LLM},
                             {"role": "user", "content": _payload([rec], 4500)}]})
            r.raise_for_status()
            risultati.update(estrai_lotto(r.json()["message"]["content"], [rec]))
        return risultati


def impronta(testo: str) -> str:
    return hashlib.sha1(testo.encode("utf-8", "ignore")).hexdigest()[:16]


def applica_llm(rec: dict, out: dict) -> None:
    ril = str(out.get("rilevanza") or "").capitalize()
    rec["rilevanza"] = ril if ril in ("Alta", "Media", "Bassa") else rec.get("rilevanza")
    rec["motivo"] = (out.get("motivo") or "")[:400] or rec.get("motivo")
    rec["titolo_it"] = html.unescape(str(out.get("titolo_it") or ""))[:250] or rec.get("titolo_it")
    tr = norm(str(out.get("tipo_ricerca") or "")).lower().strip()
    if tr in TIPI_RICERCA:
        rec["tipo_ricerca"] = TIPI_RICERCA[tr]
    it = norm(str(out.get("italia_ammessa") or "")).lower().strip()
    rec["_italia_llm"] = "Sì" if it in ("si", "yes", "true") else "No" if it in ("no", "false") else None
    if rec["_italia_llm"] == "Sì":
        # «Sì» solo se la frase citata dall'LLM compare davvero nel testo del bando
        ev = str(out.get("italia_evidenza") or "").strip()
        semplice = lambda x: re.sub(r"[^a-z0-9àèéìòù]+", " ", x.lower()).strip()
        if len(ev) < 25 or semplice(ev) not in semplice(rec.get("_testo") or ""):
            rec["_italia_llm"] = None
        else:
            rec["_italia_llm_frase"] = ev[:400]
    rec["_e_bando"] = out.get("e_un_bando") if isinstance(out.get("e_un_bando"), bool) else None
    rec["_aperto"] = out.get("aperto") if isinstance(out.get("aperto"), bool) else None
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
    # copertura: l'LLM decide solo se le regole non hanno già dato una risposta certa
    attuale = normalizza_copertura(rec.get("copertura"))
    if attuale in ("Non indicato", "Co-finanziamento da verificare"):
        cop = norm(str(out.get("copertura") or "")).lower()
        perc = numero(out.get("cofinanziamento_percentuale"))
        if cop.startswith(("interamente", "non_richiesto", "100")):
            rec["copertura"] = "Interamente finanziato"
        elif cop.startswith("richiesto") or "co-finanz" in cop or "cofinanz" in cop:
            m = re.search(r"(\d+(?:[.,]\d+)?)\s*%", cop)
            perc = perc or (numero(m.group(1)) if m else None)
            rec["copertura"] = "Co-finanziamento richiesto" + (f" ({perc:g}% a carico del beneficiario)" if perc and perc < 100 else "")
    for campo, chiave in (("partenariato", "partenariato"),
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


# ---------------------------------------------------------------------------
# Esclusioni: prima dell'LLM (regole) e dopo (giudizio dell'LLM)
# ---------------------------------------------------------------------------

def esclusione_preliminare(rec: dict) -> str | None:
    # i bandi non aperti all'Italia restano: il report copre tutti i Paesi (gruppo «Altri Paesi»)
    return esclusione_titolo(rec.get("titolo") or "")


def esclusione_finale(rec: dict) -> str | None:
    """Motivo di esclusione dopo l'analisi LLM, oppure None se il bando resta.
    Aggiorna anche il campo 'italia' quando la regola non era conclusiva."""
    llm_it = rec.get("_italia_llm")
    web = str(rec.get("_origine", "")).startswith("web")
    if llm_it and (rec.get("italia") in (None, "Da verificare") or (web and llm_it == "No")):
        rec["italia"] = llm_it
        if llm_it == "Sì" and rec.get("_italia_llm_frase"):
            rec["italia_fonte"] = f"Testo del bando: {rec['_italia_llm_frase']}"
    if web and rec.get("_e_bando") is False:
        return "Pagina che non è un bando"
    if rec.get("_aperto") is False and not rec.get("scadenza"):
        return "Call chiusa"
    if rec.get("tipo_ricerca") in ("Preclinica", "Infrastruttura", "Servizi / programmi"):
        return rec["tipo_ricerca"]
    if rec.get("rilevanza") == "Bassa":
        return "Non pertinente alla riabilitazione"
    if rec.get("tema_libero"):
        if rec.get("rilevanza") == "Alta":
            rec["rilevanza"] = "Media"       # tema libero: mai «Alta»
    return None


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
    rec["copertura"] = normalizza_copertura(rec.get("copertura"))


def calcola_stato(rec: dict) -> None:
    scad, ap = da_iso(rec.get("scadenza")), da_iso(rec.get("apertura"))
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
    ("ID", "id", 22), ("Italia ammessa", "italia", 11), ("Pertinenza riabilitazione", "rilevanza", 13),
    ("Tipo di ricerca", "tipo_ricerca", 16), ("Titolo IT", "titolo_it", 50),
    ("Scadenza", "scadenza", 12), ("Giorni alla scadenza", None, 11), ("Stato", "stato", 18),
    ("Ente finanziatore", "ente", 24), ("Fonte", "fonte", 18), ("Programma", "programma", 30),
    ("Titolo originale", "titolo", 45), ("Macrogruppo", "macrogruppi", 22), ("Parole chiave", "parole_chiave", 28),
    ("Motivo LLM", "motivo", 40),
    ("Budget totale", "budget_totale", 15), ("Budget per progetto", "budget_progetto", 15),
    ("Valuta", "valuta", 8), ("Progetti finanziabili", "progetti_finanziabili", 12),
    ("Indicatore competitività", "competitivita", 34), ("Copertura finanziaria", "copertura", 30),
    ("Apertura", "apertura", 12), ("Durata progetto", "durata", 12), ("Tipo di bando", "tipo_bando", 18),
    ("Chi può candidarsi", "chi_puo", 30), ("Struttura partenariato", "partenariato", 34),
    ("Paesi ammessi", "paesi", 26), ("Lingua bando", "lingua", 10), ("URL", "url", 40),
    ("Sommario", "sommario", 70), ("Aggiornato il", "aggiornato", 12),
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
            c.alignment = a_capo if nome in ("Sommario", "Motivo LLM", "Titolo originale", "Titolo IT",
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
    intest = ["Data esecuzione", "Bandi esaminati", "Bandi nuovi aggiunti", "Bandi rimossi",
              "Esclusi dal filtro", "Analizzati con LLM", "Errori", "Durata (minuti)"]
    for j, (n, w) in enumerate(zip(intest, [20, 14, 14, 14, 50, 14, 80, 12]), 1):
        c = wl.cell(row=1, column=j, value=n)
        c.font, c.fill = font_b, testata
        wl.column_dimensions[get_column_letter(j)].width = w
    for i, e in enumerate(reversed(stato["esecuzioni"]), 2):
        for j, k in enumerate(["data", "esaminati", "nuovi", "rimossi", "esclusi", "llm_analizzati",
                               "errori", "durata_min"], 1):
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


ETICHETTE_ESCLUSI = {
    "Italia non ammessa": "non aperti all'Italia", "Oncologia": "oncologici", "Infrastruttura": "di infrastruttura",
    "Preclinica": "preclinici", "Servizi / programmi": "servizi o programmi sociali",
    "Pagina che non è un bando": "pagine che non sono bandi", "Call chiusa": "chiusi",
    "Non pertinente alla riabilitazione": "non pertinenti alla riabilitazione",
}


def _testo_esclusi(esclusi: dict[str, int]) -> str:
    return ", ".join(f"{n} {ETICHETTE_ESCLUSI.get(m, m.lower())}"
                     for m, n in sorted(esclusi.items(), key=lambda x: -x[1]))


def oggetto_con_repo(descrizione: str) -> str:
    """Oggetto della mail: "[nome repository] – descrizione".

    Su GitHub Actions il nome arriva da GITHUB_REPOSITORY (owner/nome), cosi'
    resta giusto anche se la repository viene rinominata; in locale si usa
    quello attuale.
    """
    repo = os.environ.get("GITHUB_REPOSITORY", "").rsplit("/", 1)[-1] or "GrantFinder"
    return f"[{repo}] – {descrizione}"


def componi_email(bandi: list[dict], nuovi: set[str], allegato: Path, mittente: str, destinatario: str,
                  esclusi: dict[str, int] | None = None) -> EmailMessage:
    in_scadenza = sum(1 for b in bandi if b["stato"].startswith("In scadenza"))
    descrizione = (f"{len(bandi)} bandi per la riabilitazione ({len(nuovi)} "
                   f"{'nuovo' if len(nuovi) == 1 else 'nuovi'}, {in_scadenza} in scadenza)")
    oggetto = f"NeuroGrant – {descrizione}"  # titolo dentro la mail
    blocchi = [("🇮🇹 Aperti a enti italiani", "Sì"), ("❔ Ammissibilità dell'Italia da verificare", "Da verificare"),
               ("🌍 Altri Paesi (enti italiani non ammessi)", "No")]
    livelli = [("Pertinenza alta", "Alta"), ("Pertinenza media", "Media"), ("Non ancora analizzati", "Non analizzato")]
    legenda = "⏰ scade entro 30 giorni · 🔜 in apertura · 🆕 nuovo"

    def riga(b):
        icone = ("⏰ " if b["stato"].startswith("In scadenza") else "") + \
                ("🔜 " if b["stato"] == "In apertura" else "") + ("🆕 " if b["id"] in nuovi else "")
        tit = b.get("titolo_it") or b.get("titolo") or ""
        dettagli = [b.get("ente") or b.get("fonte") or "", f"scade {_fmt_data(b.get('scadenza'))}",
                    _fmt_soldi(b.get("budget_progetto") or b.get("budget_totale"), b.get("valuta")),
                    (b.get("copertura") or "Non indicato").split(" (")[0].split(";")[0].strip()]
        if b.get("tipo_ricerca"):
            dettagli.append(b["tipo_ricerca"])
        dettagli.append("competitività " + (b.get("competitivita") or "").split("–")[0].strip())
        return icone, tit, dettagli, b.get("url") or ""

    testo = [oggetto, legenda, ""]
    corpo = [f"<h2 style='font-family:Arial'>{html.escape(oggetto)}</h2>",
             f"<p style='font-family:Arial;color:#555;font-size:13px'>{html.escape(legenda)}</p>"]
    for titolo_blocco, valore in blocchi:
        del_blocco = [b for b in bandi if (b.get("italia") or "Da verificare") == valore]
        if not del_blocco:
            continue
        testo += [f"{titolo_blocco} ({len(del_blocco)})", "=" * 40]
        corpo.append(f"<h3 style='font-family:Arial;margin-top:28px'>{html.escape(titolo_blocco)} ({len(del_blocco)})</h3>")
        for titolo_liv, ril in livelli:
            elenco = [b for b in del_blocco if (b.get("rilevanza") or "Non analizzato") == ril]
            if not elenco:
                continue
            testo += [f"{titolo_liv} ({len(elenco)})", "-" * 30]
            corpo.append(f"<h4 style='font-family:Arial;margin:14px 0 6px;color:#1F4E5A'>{titolo_liv} ({len(elenco)})</h4>"
                         "<ul style='font-family:Arial;font-size:14px;padding-left:18px;margin-top:0'>")
            for b in elenco:
                icone, tit, dettagli, url = riga(b)
                testo.append(f"• {icone}{tit} – " + " – ".join(dettagli) + f" – {url}")
                corpo.append(f"<li style='margin-bottom:6px'>{icone}<a href='{html.escape(url)}'>{html.escape(tit)}</a> – "
                             + " – ".join(f"<b>{html.escape(d)}</b>" if d.startswith("scade") else html.escape(d)
                                          for d in dettagli) + "</li>")
            corpo.append("</ul>")
            testo.append("")
    conteggio = {g: sum(1 for b in bandi if g in b.get("macrogruppi", [])) for g in GRUPPI_ORDINE + [TEMA_LIBERO]}
    riepilogo = " | ".join(f"{g}: {n}" for g, n in conteggio.items())
    nota_esclusi = f"Esclusi automaticamente: {_testo_esclusi(esclusi)}." if esclusi else ""
    testo += ["Riepilogo per macrogruppo: " + riepilogo, nota_esclusi, f"In allegato: {allegato.name}"]
    corpo.append(f"<p style='font-family:Arial;color:#555'>Riepilogo per macrogruppo: {html.escape(riepilogo)}<br>"
                 + (f"{html.escape(nota_esclusi)}<br>" if nota_esclusi else "")
                 + f"In allegato il database completo: {html.escape(allegato.name)}. "
                 "Ammissibilità, competitività e copertura sono stime automatiche: verificare sempre il testo "
                 "ufficiale della call.</p>")

    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = oggetto_con_repo(descrizione), mittente, destinatario
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

# ---------------------------------------------------------------- ricerca guidata (Gemini + Google Search)
# Per le regioni dove i siti dei ministeri sono pochi, lenti o bloccano i bot: Gemini cerca su Google le call
# aperte e restituisce gli indirizzi; ogni pagina poi si legge e si filtra come le altre fonti web
# (parole chiave, call aperta, scadenza) e passa dall'analisi e dalla verifica sul testo ufficiale.
RICERCHE_GUIDATE = [
    ("America Latina", "Latin America", "Mexico, Colombia, Chile, Argentina, Peru, Uruguay, Ecuador, Costa Rica, Panama",
     "Spanish (convocatoria, investigación clínica, rehabilitación, fisioterapia, ingeniería biomédica)"),
    ("America Latina", "Brazil", "Brazil (federal agencies, state FAPs, Ministry of Health, foundations)",
     "Portuguese (chamada pública, edital, pesquisa clínica, reabilitação, fisioterapia, engenharia biomédica)"),
    ("Asia", "Japan", "Japan (AMED, JSPS, JST, foundations)", "Japanese (公募, 臨床研究, リハビリテーション, 医工学)"),
    ("Asia", "South Korea", "South Korea (NRF, KHIDI, ministries, foundations)", "Korean (공모, 임상연구, 재활, 의공학)"),
    ("Asia", "China, Taiwan and Hong Kong", "China, Taiwan, Hong Kong (NSFC, NSTC, HMRF, foundations)",
     "Chinese (申请指南, 临床研究, 康复, 生物医学工程)"),
    ("Asia", "India", "India (ICMR, ANRF, DBT, DST, India Alliance, foundations)", "English"),
    ("Asia", "Southeast Asia", "Singapore, Malaysia, Thailand, Vietnam, Philippines, Indonesia", "English and local languages"),
    ("Asia", "Middle East", "Saudi Arabia, United Arab Emirates, Qatar, Israel, Turkey, Iran, Jordan", "English and local languages"),
    ("Oceania", "Australia and New Zealand", "Australia and New Zealand (NHMRC, MRFF, state funds, charities, HRC)", "English"),
    ("Internazionale", "Global South and international", "low- and middle-income countries, international funders "
     "(WHO/TDR, PAHO, TWAS, ICGEB, IDRC, Wellcome, Gates, bilateral programmes with Italy)", "English, Spanish, Portuguese, French"),
]
PROMPT_RICERCA = """Search the web for research FUNDING CALLS that are OPEN today ({oggi}) or open soon, with a
submission deadline after {oggi}, from funders in: {zona}. Search also in {lingua}.
Topics: clinical research or clinical trials in any medical area, rehabilitation, physiotherapy, occupational or
speech therapy, neurorehabilitation, disability, assistive technology, clinical or biomedical engineering,
medical devices, digital health. Grants for researchers or research projects only: no scholarships for
students, no prizes for published work, no tenders for services, no calls that are already closed.
Give the OFFICIAL page of each call on the funder's own website (not news sites or aggregators).
Answer ONLY with a JSON array, at most 15 items: [{{"title": "...", "funder": "...", "country": "...",
"deadline": "YYYY-MM-DD or empty", "url": "https://..."}}]"""


def _risolvi(http: Http, url: str) -> str:
    """Gli indirizzi delle fonti di Google passano da un redirect: si legge la destinazione senza seguirla."""
    if "grounding-api-redirect" not in url:
        return url
    try:
        r = http.s.get(url, allow_redirects=False, timeout=20)
        return r.headers.get("location") or url
    except requests.RequestException:
        return url


def cerca_call(http: Http, chiave: str, modello: str, zona: str, lingua: str) -> list[tuple[str, str]]:
    """Indirizzi delle call trovate, con il nome del finanziatore quando Gemini lo indica."""
    corpo = {"contents": [{"role": "user", "parts": [{"text": PROMPT_RICERCA.format(oggi=OGGI.isoformat(), zona=zona, lingua=lingua)}]}],
             "tools": [{"google_search": {}}], "generationConfig": {"temperature": 0.2}}
    for tentativo in range(3):
        r = http.s.post(Gemini.URL.format(m=modello), json=corpo, timeout=180, headers={"x-goog-api-key": chiave})
        if r.status_code == 429 and tentativo < 2:
            time.sleep(40)
            continue
        r.raise_for_status()
        break
    cand = (r.json().get("candidates") or [{}])[0]
    testo = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts") or [] if not p.get("thought"))
    urls = []
    m = re.search(r"\[.*\]", testo, re.S)
    if m:
        try:
            urls += [(x.get("url", ""), x.get("funder", "")) for x in json.loads(m.group(0)) if isinstance(x, dict)]
        except ValueError:
            pass
    for ch in (cand.get("groundingMetadata") or {}).get("groundingChunks") or []:
        urls.append(((ch.get("web") or {}).get("uri", ""), ""))
    out, visti = [], set()
    for u, ente in urls:
        u = _risolvi(http, (u or "").strip())
        if u.startswith("http") and u not in visti and not re.search(
                r"fundsforngos|grantforward|instrumentl|linkedin|facebook|twitter|x\.com|youtube|wikipedia", u, re.I):
            visti.add(u)
            out.append((u, str(ente or "")[:150]))
    return out[:25]


def leggi_pagina_call(http: Http, fonte: dict, url: str) -> list[dict]:
    """Una pagina trovata dalla ricerca: si valuta direttamente; se è un elenco, si seguono i suoi link ai bandi."""
    from bs4 import BeautifulSoup
    if not http.consentito(url):
        return []
    r = http.get(url, cortese=True, timeout=60)
    if r.status_code >= 400:
        return []
    tipo = r.headers.get("content-type", "").lower()
    if "pdf" in tipo or url.lower().endswith(".pdf"):
        if len(r.content) > 8_000_000:
            return []
        titolo = unquote(Path(urlparse(url).path).stem).replace("_", " ").replace("-", " ").strip()
        rec = valuta_pagina(fonte, url, titolo, _testo_pdf(r.content), pdf=True)
        return [rec] if rec else []
    if "html" not in tipo:
        return []
    soup = BeautifulSoup(r.text, "lxml")
    titolo = (soup.find("h1").get_text(" ", strip=True) if soup.find("h1") else "") or \
             (soup.title.get_text(" ", strip=True) if soup.title else url)
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "form"]):
        tag.decompose()
    rec = valuta_pagina(fonte, url, titolo, re.sub(r"\s+", " ", soup.get_text(" ")).strip())
    if rec:
        return [rec]
    recs, _, _ = esplora_fonte(http, dict(fonte, url=url), 6)
    return recs


def ricerca_guidata(http: Http, chiave: str, modello: str) -> list[tuple[dict, list[dict], int, str | None]]:
    risultati = []
    for continente, nome, zona, lingua in RICERCHE_GUIDATE:
        fonte = {"nome": f"Ricerca web – {nome}", "copertura": zona.split(" (")[0], "tipo": "Ricerca guidata",
                 "italia": "verificare", "continente": continente}
        recs, errore, n = [], None, 0
        try:
            for url, ente in cerca_call(http, chiave, modello, zona, lingua):
                n += 1
                try:
                    for rec in leggi_pagina_call(http, fonte, url):
                        if ente:
                            rec["ente"] = ente
                        recs.append(rec)
                except Exception as e:
                    log.debug("Ricerca %s: %s non letto (%s)", nome, url, e)
            time.sleep(8)
        except Exception as e:
            errore = str(e)[:200]
        risultati.append((fonte, recs, n, errore))
        log.info("Ricerca web %s: %d pagine trovate, %d candidati%s", nome, n, len(recs), f" (errore: {errore})" if errore else "")
    return risultati


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
    ap = argparse.ArgumentParser(description="NeuroGrant – bandi di ricerca clinica sulla riabilitazione")
    ap.add_argument("--minuti-llm", type=float, default=180, help="tempo massimo per l'LLM (0 = disattivo)")
    ap.add_argument("--ollama-url", default=os.environ.get("OLLAMA_URL", "http://localhost:11434"))
    ap.add_argument("--modello", default=os.environ.get("NEUROGRANT_MODEL", "qwen2.5:7b"),
                    help="modello Ollama, usato solo senza GEMINI_API_KEY")
    ap.add_argument("--modelli-gemini", default=os.environ.get("GEMINI_MODELS", "").strip() or
                    "gemini-flash-latest,gemini-flash-lite-latest",
                    help="modelli Gemini in ordine di preferenza: si passa al successivo quando la quota finisce")
    ap.add_argument("--lotto", type=int, default=6, help="bandi per richiesta a Gemini")
    ap.add_argument("--fonti", default="eu,gg,web,ricerca", help="sottoinsieme di eu,gg,web,ricerca")
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
    fonti_ok: set[str] = set()
    fonti = set(args.fonti.split(","))

    if "eu" in fonti:
        try:
            recs, n = scarica_eu(http)
            raccolti += recs
            esaminati_tot += n
            registra_fonte(stato, "EU Funding & Tenders Portal", "API", "UE e paesi associati", "Pubblico",
                           "https://ec.europa.eu/info/funding-tenders/opportunities/portal", recs, n)
            fonti_ok.add("EU Funding & Tenders Portal")
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
            fonti_ok.add("Grants.gov")
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
            else:
                fonti_ok.add(nome)
            registra_fonte(stato, nome, "Lettura pagine web", fonte.get("copertura"), fonte.get("tipo"),
                           fonte["url"], recs, n, errore)
            log.info("Web %s: %d pagine, %d candidati%s", nome, n, len(recs), f" (errore: {errore})" if errore else "")

    chiave_g = os.environ.get("GEMINI_API_KEY", "").strip()
    if "ricerca" in fonti and chiave_g:
        modello_r = [m.strip() for m in args.modelli_gemini.split(",") if m.strip()][0]
        for fonte, recs, n, errore in ricerca_guidata(http, chiave_g, modello_r):
            raccolti += recs
            esaminati_tot += n
            if errore:
                errori.append(f"{fonte['nome']}: {errore}")
            else:
                fonti_ok.add(fonte["nome"])
            registra_fonte(stato, fonte["nome"], "Ricerca guidata (Gemini + Google Search)", fonte["copertura"],
                           "Vari", "https://www.google.com", recs, n, errore)

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

    # fonti non raggiunte in questa esecuzione: si tengono i loro bandi visti negli ultimi 14 giorni
    for rid, prec in stato["bandi"].items():
        if rid in unici or prec.get("fonte") in fonti_ok:
            continue
        ultima = da_iso(prec.get("_ultima_vista"))
        scad = da_iso(prec.get("scadenza"))
        if ultima and (OGGI - ultima).days <= 14 and (not scad or scad >= OGGI):
            r = nuovo_record(**{k: v for k, v in prec.items() if not k.startswith("_")})
            r["_origine"] = prec.get("_origine", "api")
            r["_testo"] = ""
            r["_ultima_vista"] = prec.get("_ultima_vista")
            unici[rid] = r

    # esclusioni per regola (prima dell'LLM: non consumano quota)
    esclusi: dict[str, int] = {}
    candidati: dict[str, dict] = {}
    for rid, r in unici.items():
        motivo = esclusione_preliminare(r)
        if motivo:
            esclusi[motivo] = esclusi.get(motivo, 0) + 1
        else:
            candidati[rid] = r
    log.info("Esclusi per regola: %s", _testo_esclusi(esclusi) or "nessuno")

    # LLM: Gemini se c'è la chiave, altrimenti Ollama in locale
    llm, lotto = None, 1
    if args.minuti_llm > 0:
        chiave = os.environ.get("GEMINI_API_KEY", "").strip()
        if chiave:
            llm = Gemini(chiave, [m.strip() for m in args.modelli_gemini.split(",") if m.strip()])
            lotto = max(1, args.lotto)
        else:
            llm = Ollama(args.ollama_url, args.modello)
            if not llm.disponibile():
                log.warning("Né GEMINI_API_KEY né Ollama disponibili: si prosegue senza LLM")
                llm = None
    scadenza_llm = time.time() + args.minuti_llm * 60
    versione = f"{PROMPT_VERSIONE}:{llm.nome}" if llm else None
    da_analizzare = []
    for rid, r in candidati.items():
        cache = stato["llm"].get(rid)
        if cache:
            applica_llm(r, cache["risultato"])        # intanto vale il risultato precedente
        aggiornata = cache and cache.get("versione", "").split(":")[0] == PROMPT_VERSIONE and \
            (versione is None or cache.get("versione") == versione) and \
            (not r["_testo"] or cache.get("hash") == impronta(r["_testo"]))
        if r["_testo"] and not aggiornata:
            da_analizzare.append((1 if cache else 0, r))
    # prima i bandi mai analizzati, poi quelli analizzati con una versione precedente;
    # a parità, prima quelli aperti all'Italia e con la scadenza più vicina
    da_analizzare = [r for _, r in sorted(da_analizzare, key=lambda x: (
        x[0], x[1].get("italia") != "Sì", x[1].get("scadenza") is None, x[1].get("scadenza") or ""))]
    analizzati = 0
    if llm and da_analizzare:
        log.info("LLM (%s): %d bandi da analizzare", llm.nome, len(da_analizzare))
        for k in range(0, len(da_analizzare), lotto):
            if time.time() > scadenza_llm:
                log.info("Tempo LLM esaurito")
                break
            gruppo = da_analizzare[k:k + lotto]
            try:
                risultati = llm.analizza_lotto(gruppo)
            except QuotaEsaurita as e:
                log.warning("LLM: %s", e)
                errori.append(f"LLM: {e}")
                break
            except ErroreFatale as e:
                log.error("LLM: %s", e)
                errori.append(f"LLM: {e}")
                break
            except Exception as e:
                log.warning("LLM non riuscito per %s: %s", ", ".join(r["id"] for r in gruppo), e)
                continue
            for r in gruppo:
                out = risultati.get(r["id"])
                if not out:
                    continue
                stato["llm"][r["id"]] = {"hash": impronta(r["_testo"]), "versione": versione,
                                         "modello": getattr(llm, "modello", None), "risultato": out}
                applica_llm(r, out)
                analizzati += 1
            log.info("LLM: %d/%d analizzati", analizzati, len(da_analizzare))
        if analizzati < len(da_analizzare):
            log.info("%d bandi rimandati alla prossima esecuzione", len(da_analizzare) - analizzati)

    tassi = leggi_tassi(BASE / "tassi_successo.csv")
    attivi = []
    for r in candidati.values():
        r["rilevanza"] = r.get("rilevanza") or "Non analizzato"
        r["titolo_it"] = r.get("titolo_it") or r.get("titolo")
        scad = da_iso(r.get("scadenza"))
        if scad and scad < OGGI:
            continue
        motivo = esclusione_finale(r)
        if motivo:
            esclusi[motivo] = esclusi.get(motivo, 0) + 1
            continue
        r["italia"] = r.get("italia") or "Da verificare"
        calcola_indicatori(r, tassi)
        calcola_stato(r)
        attivi.append(r)
    # ordine: Italia ammessa -> pertinenza alla riabilitazione -> scadenza (senza data in fondo)
    ordine_it = {"Sì": 0, "Da verificare": 1, "No": 2}
    ordine_ril = {"Alta": 0, "Media": 1, "Non analizzato": 2}
    attivi.sort(key=lambda r: (ordine_it.get(r["italia"], 1), ordine_ril.get(r.get("rilevanza"), 2),
                               r.get("scadenza") or "9999"))
    log.info("Esclusi in totale: %s", _testo_esclusi(esclusi) or "nessuno")

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
    # la memoria LLM tiene anche i bandi esclusi ancora online, per non rianalizzarli ogni settimana
    stato["llm"] = {k: v for k, v in stato["llm"].items() if k in unici}
    durata = round((time.time() - t0) / 60, 1)
    stato["esecuzioni"].append({"data": datetime.now().strftime("%Y-%m-%d %H:%M"), "esaminati": esaminati_tot,
                                "nuovi": len(nuovi), "rimossi": rimossi,
                                "esclusi": _testo_esclusi(esclusi) or "Nessuno",
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
        invia_email(componi_email(attivi, nuovi, allegato, utente, destinatario, esclusi), utente, password)
        log.info("Email inviata a %s", destinatario)
    finally:
        allegato.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
