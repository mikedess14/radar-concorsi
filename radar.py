#!/usr/bin/env python3
"""
Radar concorsi - soluzione B, fase 1.

Ogni giorno:
  1. visita le fonti elencate in fonti.txt e raccoglie i link ai bandi;
  2. per i link nuovi legge la pagina (e il PDF del bando, se c'è);
  3. tiene solo quelli che contengono le parole di parole_chiave.txt;
  4. chiede a Gemini (gratuito) se il bando è davvero pertinente ed estrae la scheda;
  5. avvisa su Telegram e aggiorna CONCORSI.md e data/concorsi.json.

Non serve modificare questo file: le impostazioni sono in fonti.txt, profilo.txt e parole_chiave.txt.
"""

import datetime as dt
import hashlib
import html
import json
import logging
import os
import re
import ssl
import sys
import time
import unicodedata
from collections import deque
from io import BytesIO
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter

logging.getLogger("pypdf").setLevel(logging.ERROR)  # nasconde gli avvisi tecnici sui PDF malformati

try:
    from pypdf import PdfReader
except Exception:  # pypdf non indispensabile: senza, i PDF vengono saltati
    PdfReader = None

# ---------------------------------------------------------------- impostazioni

OGGI = dt.date.today().isoformat()
BOT = "RadarConcorsiPersonale"
UA = f"Mozilla/5.0 (compatible; {BOT}/1.1; uso personale, una visita al giorno)"
STATO = "data/stato.json"
EXPORT = "data/concorsi.json"
PAGINA = "CONCORSI.md"

MAX_DETTAGLI_PER_FONTE = int(os.getenv("MAX_DETTAGLI_PER_FONTE", "40"))  # pagine lette per fonte e per giorno
MAX_AI = int(os.getenv("MAX_AI", "40"))  # bandi analizzati dall'AI per giorno
PAUSA_AI = 7  # secondi tra due chiamate a Gemini (resta sotto i limiti gratuiti)
PAUSA_WEB = 1.5  # secondi tra due pagine dello stesso sito
MAX_TESTO = 15000  # caratteri di testo inviati all'AI per ogni bando

GEMINI_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODELLI = [m for m in [os.getenv("GEMINI_MODEL", "").strip(),
                              "gemini-flash-lite-latest", "gemini-flash-latest", "gemini-2.5-flash-lite"] if m]
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()

INPA_API = "https://portale.inpa.gov.it/concorsi-smart/api/concorso-public-area/search-better"
INPA_DETTAGLIO = "https://www.inpa.gov.it/bandi-e-avvisi/dettaglio-bando-avviso/?concorso_id={}"
INPA_ARGOMENTI = [
    "profili informatici o ICT presso enti con sede in Sardegna",
    "funzionario informatico o funzionario sistemi informativi in Sardegna",
    "istruttore informatico in comuni, unioni di comuni o province della Sardegna",
    "profili informatici in concorsi nazionali con sedi in tutta Italia, comprese le sedi in Sardegna",
    "elenchi di idonei o selezioni uniche nazionali per enti locali che comprendono profili informatici",
]
INPA_RICERCHE = ["informatico", "informatica", "informatici", "ICT", "sistemi informativi",
                 "transizione digitale", "analista", "programmatore", "cybersecurity", "elenco idonei"]

PAROLE_SARDEGNA = ["sardegna", "sardo", "sarda", "sardi", "cagliari", "sassari", "nuoro", "oristano",
                   "sud sardegna", "carbonia", "iglesias", "olbia", "tempio", "lanusei", "tortoli",
                   "ogliastra", "gallura", "medio campidano", "sanluri", "villacidro", "alghero",
                   "quartu", "nazionale", "intero territorio", "tutto il territorio", "idonei"]

SEMBRA_BANDO = re.compile(r"concors|selezion|avvis|bando|bandi|mobilit|idone|reclutament|assunzion|"
                          r"recruit|lavora con noi|offerte di lavoro|caricaDettaglio", re.I)
SOLO_AGGIORNAMENTO = re.compile(r"graduatori|esit[oi]|ammess|diario|calendario|convocazion|rettific|"
                                r"commissione|punteggi|verbal|tracce|sede (della |delle )?prov|"
                                r"tabella riepilogativa|espletat|stabilizzazion|bandi di gara|gare e contratti", re.I)
SOCIAL = re.compile(r"facebook\.com|twitter\.com|x\.com/intent|linkedin\.com|whatsapp|telegram\.me|"
                    r"instagram\.com|youtube\.com|mailto:", re.I)
DATATA = re.compile(r"\b\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}\b|n[°o.]\s*\d+", re.I)
SEZIONE = re.compile(r"bandi di concorso|concorsi e selezioni|concorsi|selezion[ei] del personale|"
                     r"selezioni|lavora con noi|reclutamento|avvisi di selezione|offerte di lavoro|"
                     r"opportunit[aà] di lavoro", re.I)
TRASPARENTE = re.compile(r"amministrazione trasparente|societ[aà] trasparente|ente trasparente", re.I)
SEZIONE_TRASP = re.compile(r"bandi di concorso|selezione del personale|reclutamento del personale", re.I)

STATI_LEGGIBILI = {"indeterminato": "tempo indeterminato", "determinato": "tempo determinato",
                   "mobilita": "mobilità", "altro": "altro contratto"}
AREE = {"F": "Funzionari (ex D)", "I": "Istruttori (ex C)", "O": "Operatori", "D": "Dirigenti",
        "P": "Contratto privato"}
PROVE = {"titoli": "Titoli", "pre": "Preselezione", "pre_ev": "Preselezione eventuale",
         "scritta": "Scritta", "pratica": "Pratica", "orale": "Orale"}

class SSLCompatibile(HTTPAdapter):
    """Per i siti con certificati o protocolli datati."""
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
        ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


sessione = requests.Session()
sessione.headers.update({"User-Agent": UA, "Accept-Language": "it-IT,it;q=0.9"})
sessione_compatibile = requests.Session()
sessione_compatibile.headers.update(sessione.headers)
sessione_compatibile.mount("https://", SSLCompatibile())
_robots = {}
_ultima_visita = {}


# ---------------------------------------------------------------- utilità

def log(*parti):
    print(*parti, flush=True)


def norm(testo):
    testo = unicodedata.normalize("NFD", str(testo or "").lower())
    testo = "".join(c for c in testo if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", testo).strip()


def leggi_righe(nome_file):
    if not os.path.exists(nome_file):
        return []
    with open(nome_file, encoding="utf-8") as f:
        return [r.strip() for r in f if r.strip() and not r.strip().startswith("#")]


def carica_stato():
    try:
        with open(STATO, encoding="utf-8") as f:
            stato = json.load(f)
    except Exception:
        stato = {}
    for chiave, vuoto in [("visti", {}), ("coda", []), ("concorsi", {}), ("sezioni", {}), ("errori", {})]:
        stato.setdefault(chiave, vuoto)
    return stato


def salva_stato(stato):
    # i link visti più di un anno fa vengono dimenticati, così il file non cresce all'infinito
    limite = (dt.date.today() - dt.timedelta(days=365)).isoformat()
    stato["visti"] = {u: v for u, v in stato["visti"].items() if v.get("d", OGGI) >= limite}
    os.makedirs("data", exist_ok=True)
    with open(STATO, "w", encoding="utf-8") as f:
        json.dump(stato, f, ensure_ascii=False, indent=1, sort_keys=True)


def stesso_sito(url, base):
    h1 = urlparse(url).netloc.lower().removeprefix("www.")
    h2 = urlparse(base).netloc.lower().removeprefix("www.")
    return h1 == h2 or h1.endswith("." + h2) or h2.endswith("." + h1)


def pulisci_url(url):
    return url.split("#")[0].strip()


def data_it(iso):
    try:
        d = dt.date.fromisoformat(iso)
    except Exception:
        return "non indicata"
    mesi = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto",
            "settembre", "ottobre", "novembre", "dicembre"]
    return f"{d.day} {mesi[d.month - 1]} {d.year}"


# ---------------------------------------------------------------- accesso ai siti

def consentito(url):
    """Rispetta il file robots.txt di ogni sito."""
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    if base not in _robots:
        rp = robotparser.RobotFileParser()
        try:
            r = sessione.get(base + "/robots.txt", timeout=15)
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        except Exception:
            rp.parse([])
        _robots[base] = rp
    return _robots[base].can_fetch(BOT, url)


def scarica(url):
    """Scarica una pagina rispettando robots.txt e una pausa tra visite allo stesso sito."""
    if not consentito(url):
        raise PermissionError(f"il sito non consente l'accesso automatico a {url}")
    host = urlparse(url).netloc
    attesa = PAUSA_WEB - (time.time() - _ultima_visita.get(host, 0))
    if attesa > 0:
        time.sleep(attesa)
    _ultima_visita[host] = time.time()
    try:
        r = sessione.get(url, timeout=30, allow_redirects=True)
    except requests.exceptions.SSLError:
        r = sessione_compatibile.get(url, timeout=30, allow_redirects=True)
    if r.status_code in (401, 403):
        raise PermissionError(f"il sito blocca le visite automatiche dai server di GitHub (errore {r.status_code})")
    r.raise_for_status()
    return r


def testo_da_risposta(r):
    tipo = r.headers.get("content-type", "").lower()
    if "pdf" in tipo or r.url.lower().endswith(".pdf"):
        return testo_pdf(r.content), None
    soup = BeautifulSoup(r.content, "html.parser")  # legge la codifica dichiarata nella pagina
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "form"]):
        tag.decompose()
    for tag in soup.select('[role=navigation], [class*=breadcrumb], [id*=breadcrumb], [class*=social], '
                           '[class*=share], [id*=cookie], [class*=cookie]'):
        tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)), soup


def testo_pdf(contenuto):
    if PdfReader is None:
        return ""
    try:
        lettore = PdfReader(BytesIO(contenuto))
        pagine = []
        for pagina in lettore.pages[:25]:
            pagine.append(pagina.extract_text() or "")
            if sum(len(p) for p in pagine) > MAX_TESTO:
                break
        return re.sub(r"\s+", " ", " ".join(pagine))
    except Exception as e:
        log("   PDF non leggibile:", e)
        return ""


def link_utile(url, base, testo, intorno):
    if SOCIAL.search(url):
        return False
    if not (stesso_sito(url, base) or url.lower().split("?")[0].endswith(".pdf")):
        return False
    if SOLO_AGGIORNAMENTO.search(testo):
        return False
    return bool(SEMBRA_BANDO.search(f"{testo} {intorno} {url}"))


def link_della_pagina(soup, base):
    """Restituisce (indirizzo, testo del link, testo intorno) per ogni link della pagina."""
    risultati, visti = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = pulisci_url(urljoin(base, href))
        if not url.startswith("http") or url in visti:
            continue
        visti.add(url)
        testo = re.sub(r"\s+", " ", a.get_text(" ", strip=True) or a.get("title", ""))
        contenitore = a.find_parent(["li", "tr", "article", "p", "div"])
        intorno = re.sub(r"\s+", " ", contenitore.get_text(" ", strip=True))[:400] if contenitore else ""
        risultati.append((url, testo, intorno))
    return risultati


# ---------------------------------------------------------------- filtri

def carica_parole():
    parole = [norm(p) for p in leggi_righe("parole_chiave.txt")]
    return [p for p in parole if p]


def contiene_parole(testo, parole):
    t = " " + norm(testo) + " "
    return any((" " + p) in t for p in parole)


def riguarda_sardegna(testo):
    t = " " + norm(testo) + " "
    return any((" " + p) in t for p in PAROLE_SARDEGNA)


# ---------------------------------------------------------------- fonti

def leggi_fonti():
    fonti = []
    for riga in leggi_righe("fonti.txt"):
        parti = [p.strip() for p in riga.split("|")]
        if len(parti) < 2 or not parti[1]:
            continue
        fonti.append({"nome": parti[0], "url": parti[1],
                      "nazionale": len(parti) > 2 and parti[2].lower().startswith("naz")})
    return fonti


def sezioni_da_controllare(fonte, stato):
    """Se l'indirizzo è una home page trova la sezione dei bandi (e la ricorda per 7 giorni)."""
    url = fonte["url"]
    percorso = urlparse(url).path.strip("/")
    if percorso:
        return [url]
    memoria = stato["sezioni"].get(url)
    if memoria and memoria.get("d", "") >= (dt.date.today() - dt.timedelta(days=7)).isoformat() and memoria["u"]:
        return memoria["u"]
    r = scarica(url)
    _, soup = testo_da_risposta(r)
    trovate = []
    if soup is not None:
        link = link_della_pagina(soup, r.url)
        trovate = [u for u, t, _ in link if stesso_sito(u, url) and SEZIONE.search(t)]
        if not trovate:
            trasp = [u for u, t, _ in link if stesso_sito(u, url) and TRASPARENTE.search(t)][:1]
            for tu in trasp:
                _, soup2 = testo_da_risposta(scarica(tu))
                if soup2 is not None:
                    trovate += [u for u, t, _ in link_della_pagina(soup2, tu)
                                if stesso_sito(u, url) and SEZIONE_TRASP.search(t)]
    trovate = list(dict.fromkeys(trovate))[:4]
    stato["sezioni"][url] = {"d": OGGI, "u": trovate}
    if trovate:
        log("   sezioni trovate:", ", ".join(trovate))
    else:
        raise RuntimeError("non trovo la sezione dei bandi: inserisci in fonti.txt l'indirizzo esatto della pagina")
    return trovate


def raccogli_da_pagine(fonte, stato):
    """Link ai bandi presenti nelle sezioni della fonte e negli elenchi scoperti in passato."""
    candidati, visti = [], set()
    pagine = sezioni_da_controllare(fonte, stato) + stato.setdefault("elenchi", {}).get(fonte["nome"], [])
    for sezione in dict.fromkeys(pagine):
        try:
            r = scarica(sezione)
        except Exception as e:
            log("   pagina elenco non letta:", sezione, "-", e)
            continue
        _, soup = testo_da_risposta(r)
        if soup is None:
            continue
        for url, testo, intorno in link_della_pagina(soup, r.url):
            if url in visti or url == sezione or url.rstrip("/") == fonte["url"].rstrip("/"):
                continue
            if not link_utile(url, r.url, testo, intorno):
                continue
            visti.add(url)
            candidati.append({"url": url, "titolo": testo[:200], "contesto": intorno, "livello": 1})
    return candidati


def primo_valore(diz, chiavi):
    for k, v in diz.items():
        if any(c in k.lower() for c in chiavi) and isinstance(v, (str, int, float)) and str(v).strip():
            return str(v).strip()
    return ""


def raccogli_da_inpa(stato):
    """Usa il servizio di ricerca pubblico del portale inPA (lo stesso usato dal sito)."""
    if not consentito(INPA_API):
        log("   il servizio di ricerca interno di inPA non è consentito: cerco le pagine dei bandi con Google")
        return cerca_inpa_con_google()
    trovati = {}
    campione_mostrato = False
    for testo in INPA_RICERCHE:
        for pagina in range(5):
            try:
                r = sessione.post(f"{INPA_API}?page={pagina}&size=50",
                                  json={"text": testo, "status": ["OPEN"]}, timeout=30)
            except Exception as e:
                log(f"   inPA, ricerca '{testo}': errore di rete {e}")
                break
            if r.status_code != 200:
                log(f"   inPA, ricerca '{testo}': risposta {r.status_code} {r.text[:200]}")
                break
            dati = r.json()
            elenco = dati.get("content", []) if isinstance(dati, dict) else dati
            if not elenco:
                break
            if not campione_mostrato:
                log("   inPA, campi disponibili:", ", ".join(sorted(elenco[0].keys()))[:400])
                campione_mostrato = True
            for voce in elenco:
                ident = voce.get("id") or voce.get("concorsoId") or voce.get("codice")
                if ident:
                    trovati[str(ident)] = voce
            if isinstance(dati, dict) and (dati.get("last") or pagina + 1 >= dati.get("totalPages", 1)):
                break
            time.sleep(1)
        time.sleep(1)
    log(f"   inPA: {len(trovati)} bandi aperti corrispondono alle ricerche")
    candidati = []
    for ident, voce in trovati.items():
        titolo = primo_valore(voce, ["titolo", "title", "denominazione"])
        descr = primo_valore(voce, ["descrizione", "description"])
        candidati.append({"url": INPA_DETTAGLIO.format(ident), "titolo": titolo[:200],
                          "contesto": (descr + " " + json.dumps(voce, ensure_ascii=False))[:1500]})
    return candidati


def cerca_inpa_con_google():
    """Chiede a Gemini, con la ricerca Google, le pagine di dettaglio inPA dei bandi pertinenti."""
    if not GEMINI_KEY:
        log("   manca GEMINI_API_KEY: ricerca inPA saltata")
        return []
    trovati = {}
    for argomento in INPA_ARGOMENTI:
        domanda = (f"Oggi è il {OGGI}. Cerca sul portale inPA (www.inpa.gov.it) i bandi di concorso, gli avvisi "
                   f"di selezione e gli elenchi di idonei ancora aperti per: {argomento}. Per ognuno scrivi "
                   "l'indirizzo completo della pagina di dettaglio inPA, quella che contiene "
                   "'dettaglio-bando-avviso/?concorso_id='. Rispondi solo con gli indirizzi, uno per riga.")
        corpo = {"contents": [{"parts": [{"text": domanda}]}], "tools": [{"google_search": {}}]}
        risposta = None
        for modello in list(GEMINI_MODELLI):
            try:
                r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{modello}:generateContent",
                                  json=corpo, headers={"x-goog-api-key": GEMINI_KEY}, timeout=(15, 90))
            except Exception as e:
                log(f"   ricerca Google non riuscita ({modello}): {e}")
                continue
            if r.status_code == 200:
                risposta = r.json()
                break
            log(f"   ricerca Google con {modello}: risposta {r.status_code} {r.text[:200]}")
        if not risposta:
            continue
        cand = (risposta.get("candidates") or [{}])[0]
        testo = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []))
        indirizzi = set(re.findall(r"https?://[^\s)\]\"'<>]+", testo))
        for ch in cand.get("groundingMetadata", {}).get("groundingChunks", []):
            if ch.get("web", {}).get("uri"):
                indirizzi.add(ch["web"]["uri"])
        for u in indirizzi:
            if "grounding-api-redirect" in u or "vertexaisearch" in u:
                try:  # i risultati di Google arrivano come link di reindirizzamento: ricavo l'indirizzo vero
                    u = requests.head(u, allow_redirects=True, timeout=20, headers={"User-Agent": UA}).url
                except Exception:
                    continue
            m = re.search(r"concorso_id=([0-9a-fA-F]{16,})", u)
            if "inpa.gov.it" in u and m:
                trovati[m.group(1).lower()] = INPA_DETTAGLIO.format(m.group(1).lower())
        time.sleep(PAUSA_AI)
    log(f"   inPA tramite Google: {len(trovati)} pagine di bandi trovate")
    return [{"url": u, "titolo": "", "contesto": "", "livello": 2} for u in trovati.values()]


def leggi_pagina(url):
    """Testo completo di una pagina (più l'eventuale PDF del bando) e i link ai bandi che contiene."""
    r = scarica(url)
    testo, soup = testo_da_risposta(r)
    link = []
    if soup is not None:
        tutti = link_della_pagina(soup, r.url)
        link = [(u, t, c) for u, t, c in tutti if u != r.url and link_utile(u, r.url, t, c)]
        if len(testo) < MAX_TESTO and len(link) < 8:
            pdf = [u for u, t, _ in tutti
                   if (".pdf" in u.lower() or "download" in u.lower() or "media" in u.lower())
                   and re.search(r"bando|avviso|selezion|concors", f"{t} {u}", re.I) and consentito(u)][:2]
            for u in pdf:
                try:
                    aggiunta, _ = testo_da_risposta(scarica(u))
                    testo += " \n[BANDO PDF] " + aggiunta
                except Exception as e:
                    log("   PDF non scaricato:", u, "-", e)
                if len(testo) > MAX_TESTO:
                    break
    return testo[:MAX_TESTO], link


# ---------------------------------------------------------------- intelligenza artificiale

def prompt_ai(fonte, url, testo, profilo):
    return f"""Oggi è il {OGGI}. Ti fornisco il testo di una pagina pubblicata da "{fonte}".
Indirizzo: {url}

Decidi se è un bando di concorso, un avviso di selezione o un elenco di idonei che:
1) riguarda questo profilo: {profilo}
2) è ancora aperto: scadenza delle domande uguale o successiva a oggi, oppure non indicata;
3) ha posti in Sardegna, oppure è nazionale con possibilità di sede in Sardegna, oppure è un elenco di idonei o una selezione unica da cui possono assumere enti sardi.

Rispondi solo con un oggetto JSON, senza altro testo:
{{"pertinente": true, "motivo": "massimo 15 parole", "titolo": "titolo breve del profilo", "ente": "ente",
"ambito": "R|NS|NN|EL", "tipo": "C|E|S", "area": "F|I|O|D|P", "sede": "sede di lavoro", "comuni": [], "province": [],
"scadenza": "YYYY-MM-DD", "ora": "HH:MM", "posti": "numero posti", "contratto": "indeterminato|determinato|mobilita|altro",
"titolo_studio": "breve", "prove": ["titoli","pre","pre_ev","scritta","pratica","orale"], "come_candidarsi": "breve",
"link_bando": "url", "link_candidatura": "url", "retribuzione_annua": 0,
"comparto": "FC|FL|SAN|IR|RAS|PRIV|ALTRO"}}

Legenda: ambito R ente sardo, NS ente nazionale con sede in Sardegna, NN nazionale con scelta della sede,
EL elenco di idonei o selezione unica nazionale. tipo C concorso di ente pubblico, E elenco di idonei, S selezione
di società pubblica. area F funzionari o ex categoria D, I istruttori o ex C, O operatori, D dirigenti,
P contratto privato. comparto è il contratto applicato: FC funzioni centrali (ministeri, agenzie, INPS, INAIL),
FL funzioni locali (comuni, unioni di comuni, province, camere di commercio), SAN sanità, IR università e ricerca,
RAS contratto regionale della Sardegna (Regione, agenzie ed enti regionali), PRIV contratto privato, ALTRO.
prove: solo i codici indicati, nell'ordine; pre_ev è la preselezione che si svolge solo oltre
una soglia di domande. retribuzione_annua: solo se scritta nel bando, altrimenti 0.
Non inventare nulla: se un dato manca usa "" oppure []. Se la pagina non è un bando, metti "pertinente": false.

TESTO:
\"\"\"{testo}\"\"\""""


class QuotaEsaurita(Exception):
    pass


def chiedi_gemini(prompt):
    if not GEMINI_KEY:
        raise RuntimeError("manca GEMINI_API_KEY nei segreti del repository")
    ultimo_errore = None
    quota_finita = False
    for modello in list(GEMINI_MODELLI):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{modello}:generateContent"
        corpo = {"contents": [{"parts": [{"text": prompt}]}],
                 "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1}}
        for tentativo in range(3):
            try:
                r = requests.post(url, json=corpo, headers={"x-goog-api-key": GEMINI_KEY}, timeout=(15, 75))
            except Exception as e:
                ultimo_errore = f"rete: {e}"
                log(f"   Gemini non risponde ({modello}, tentativo {tentativo + 1}): {e}")
                time.sleep(15)
                continue
            if r.status_code == 200:
                try:
                    testo = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                    return json.loads(re.sub(r"^```(json)?|```$", "", testo.strip()))
                except Exception as e:
                    ultimo_errore = f"risposta non leggibile: {e}"
                    break
            if r.status_code == 404:
                log(f"   modello {modello} non disponibile, provo il successivo")
                GEMINI_MODELLI.remove(modello)
                break
            if r.status_code == 429:
                compatto = r.text.lower().replace(" ", "").replace("_", "")
                if "perday" in compatto:
                    log(f"   quota giornaliera di {modello} esaurita, provo il modello successivo")
                    GEMINI_MODELLI.remove(modello)
                    quota_finita = True
                    break
                attesa = 60 * (tentativo + 1)
                log(f"   Gemini chiede di rallentare, attendo {attesa} secondi")
                time.sleep(attesa)
                continue
            if r.status_code in (500, 502, 503, 504):
                time.sleep(20)
                continue
            ultimo_errore = f"errore {r.status_code}: {r.text[:300]}"
            break
        if ultimo_errore and not quota_finita:
            break
    if not GEMINI_MODELLI and quota_finita:
        raise QuotaEsaurita("quota giornaliera gratuita di Gemini esaurita per tutti i modelli")
    raise RuntimeError(ultimo_errore or "nessun modello Gemini disponibile")


# ---------------------------------------------------------------- archivio e avvisi

def chiave_concorso(scheda, url):
    base = scheda.get("link_bando") or url
    return hashlib.sha1(pulisci_url(base).lower().encode()).hexdigest()[:12]


def simile(a, b):
    ta, tb = set(norm(a).split()), set(norm(b).split())
    return len(ta & tb) / max(1, len(ta | tb))


def trova_doppione(stato, scheda, url):
    link = {pulisci_url(scheda.get("link_bando") or ""), pulisci_url(url)} - {""}
    for k, c in stato["concorsi"].items():
        if link & set(c.get("link", [])):
            return k
        if (simile(c.get("ente", ""), scheda.get("ente", "")) >= 0.6
                and simile(c.get("titolo", ""), scheda.get("titolo", "")) >= 0.6
                and (not c.get("scadenza") or not scheda.get("scadenza") or c["scadenza"] == scheda["scadenza"])):
            return k
    return None


def registra(stato, scheda, url, fonte):
    """Salva un concorso pertinente. Restituisce True se è nuovo."""
    k = trova_doppione(stato, scheda, url)
    link = [l for l in {pulisci_url(scheda.get("link_bando") or ""), pulisci_url(url)} if l]
    if k:
        c = stato["concorsi"][k]
        c["link"] = sorted(set(c.get("link", []) + link))
        c["fonti"] = sorted(set(c.get("fonti", []) + [fonte]))
        for campo, valore in scheda.items():
            if valore and not c.get(campo):
                c[campo] = valore
        return False
    k = chiave_concorso(scheda, url)
    scheda.update({"link": link, "fonti": [fonte], "trovato": OGGI})
    stato["concorsi"][k] = scheda
    return True


def invia_telegram(testo):
    if not (TG_TOKEN and TG_CHAT):
        return
    for pezzo in [testo[i:i + 3900] for i in range(0, len(testo), 3900)]:
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", timeout=30,
                          json={"chat_id": TG_CHAT, "text": pezzo, "parse_mode": "HTML",
                                "disable_web_page_preview": True})
        except Exception as e:
            log("Telegram non raggiungibile:", e)


def messaggio_concorso(c):
    e = html.escape
    righe = [f"<b>{e(c.get('titolo') or 'Nuovo bando')}</b>", e(c.get("ente", ""))]
    if c.get("sede"):
        righe.append("Sede: " + e(c["sede"]))
    if c.get("scadenza"):
        giorni = (dt.date.fromisoformat(c["scadenza"]) - dt.date.today()).days
        righe.append(f"Scadenza: {data_it(c['scadenza'])}" + (f", ore {e(c['ora'])}" if c.get("ora") else "")
                     + f" (tra {giorni} giorni)")
    dettagli = [STATI_LEGGIBILI.get(c.get("contratto", ""), ""), AREE.get(c.get("area", ""), "")]
    if c.get("tipo") == "E":
        dettagli.append("elenco di idonei")
    dettagli = [d for d in dettagli if d]
    if dettagli:
        righe.append(", ".join(dettagli).capitalize())
    if c.get("prove"):
        righe.append("Prove: " + " + ".join(PROVE.get(p, p) for p in c["prove"]))
    link = c.get("link_bando") or (c.get("link") or [""])[0]
    if link:
        righe.append(f'<a href="{e(link)}">Apri il bando</a>')
    return "\n".join(r for r in righe if r)


def pulisci_scheda(s):
    if not isinstance(s, dict):
        return None
    s = {k: v for k, v in s.items() if v not in (None, "")}
    if s.get("scadenza") and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(s["scadenza"])):
        s.pop("scadenza")
    s["prove"] = [p for p in s.get("prove", []) if p in PROVE] if isinstance(s.get("prove"), list) else []
    for campo in ("comuni", "province"):
        s[campo] = [str(x) for x in s.get(campo, [])] if isinstance(s.get(campo), list) else []
    return s


def scrivi_pagina(stato):
    aperti = [c for c in stato["concorsi"].values() if not c.get("scadenza") or c["scadenza"] >= OGGI]
    aperti.sort(key=lambda c: c.get("scadenza") or "9999")
    limite = (dt.date.today() - dt.timedelta(days=60)).isoformat()
    per_web = [dict(c, id=k) for k, c in stato["concorsi"].items()
               if not c.get("scadenza") or c["scadenza"] >= limite]
    with open(EXPORT, "w", encoding="utf-8") as f:
        json.dump({"aggiornato": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
                   "fonti": [x["nome"] for x in leggi_fonti()], "concorsi": per_web},
                  f, ensure_ascii=False, indent=1)
    righe = [f"# Concorsi aperti\n\nAggiornato il {data_it(OGGI)}. Concorsi aperti: {len(aperti)}.\n",
             "| Scadenza | Concorso | Ente e sede | Contratto | Prove | Bando |",
             "|---|---|---|---|---|---|"]
    for c in aperti:
        cella = lambda t: str(t or "").replace("|", "/").replace("\n", " ")
        link = c.get("link_bando") or (c.get("link") or [""])[0]
        contratto = ", ".join(x for x in [STATI_LEGGIBILI.get(c.get("contratto", ""), ""),
                                          AREE.get(c.get("area", ""), "")] if x)
        righe.append("| {} | {} | {} | {} | {} | {} |".format(
            data_it(c["scadenza"]) if c.get("scadenza") else "non indicata",
            cella(c.get("titolo")) + (" (elenco di idonei)" if c.get("tipo") == "E" else ""),
            cella(c.get("ente")) + (", " + cella(c["sede"]) if c.get("sede") else ""),
            cella(contratto),
            cella(" + ".join(PROVE.get(p, p) for p in c.get("prove", []))),
            f"[apri]({link})" if link else ""))
    with open(PAGINA, "w", encoding="utf-8") as f:
        f.write("\n".join(righe) + "\n")


# ---------------------------------------------------------------- programma principale

def mostra_chat_id():
    """Prima configurazione: trova l'identificativo della chat Telegram."""
    r = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates", timeout=30).json()
    chat = {str(u["message"]["chat"]["id"]) for u in r.get("result", []) if "message" in u}
    if chat:
        log("=" * 60)
        log("Il tuo TELEGRAM_CHAT_ID è:", ", ".join(chat))
        log("Copialo nei segreti del repository e rilancia il programma.")
        log("=" * 60)
    else:
        log("Nessun messaggio trovato: scrivi un messaggio qualsiasi al tuo bot su Telegram e rilancia.")


def main():
    if TG_TOKEN and not TG_CHAT:
        mostra_chat_id()
        return
    stato = carica_stato()
    primo_avvio = not stato["visti"]
    parole = carica_parole()
    profilo = " ".join(leggi_righe("profilo.txt"))
    problemi = []

    # 1-3. raccolta dei link nuovi, lettura e primo filtro
    for fonte in leggi_fonti():
        nome = fonte["nome"]
        log(f"\n== {nome}")
        try:
            candidati = raccogli_da_inpa(stato) if fonte["url"].lower() == "inpa" else raccogli_da_pagine(fonte, stato)
            nuovi = [c for c in candidati if c["url"] not in stato["visti"]]
            log(f"   {len(candidati)} link a bandi, {len(nuovi)} nuovi")
            da_leggere = deque(nuovi)
            gia_in_coda = {v["url"] for v in stato["coda"]}
            letti = 0
            while da_leggere:
                if letti >= MAX_DETTAGLI_PER_FONTE:
                    log(f"   raggiunto il limite di {MAX_DETTAGLI_PER_FONTE} pagine: le altre domani")
                    break
                c = da_leggere.popleft()
                if c["url"] in stato["visti"]:
                    continue
                try:
                    testo, link = leggi_pagina(c["url"])
                except Exception as e:
                    log("   pagina non letta:", c["url"], "-", e)
                    stato["visti"][c["url"]] = {"d": OGGI, "t": c["titolo"][:120], "f": nome, "x": "errore"}
                    letti += 1
                    continue
                letti += 1
                stato["visti"][c["url"]] = {"d": OGGI, "t": c["titolo"][:120], "f": nome}
                tutto = f"{c['titolo']} {c['contesto']} {testo}"

                # Pagina che elenca altri bandi (es. "Selezioni aperte", un numero della Gazzetta): ne seguo i link
                if len(link) >= 8 and c.get("livello", 1) < 2:
                    grande = len(link) > 30
                    aggiunti = 0
                    for u, t, ctx in link:
                        if u in stato["visti"] or u == c["url"]:
                            continue
                        if grande and not contiene_parole(f"{t} {ctx}", parole):
                            continue  # elenchi lunghi: seguo solo i titoli che contengono le parole chiave
                        da_leggere.append({"url": u, "titolo": t[:200], "contesto": ctx, "livello": 2})
                        aggiunti += 1
                    log(f"   elenco di bandi: {(c['titolo'] or c['url'])[:70]} ({aggiunti} link da leggere)")
                    if not DATATA.search(c["titolo"]):
                        elenchi = stato.setdefault("elenchi", {}).setdefault(nome, [])
                        if c["url"] not in elenchi and len(elenchi) < 6:
                            elenchi.append(c["url"])  # lo ricontrollo ogni giorno, come una sezione
                    if len(link) >= 15:
                        continue

                if not contiene_parole(tutto, parole):
                    continue
                if fonte["nazionale"] and not riguarda_sardegna(tutto):
                    continue
                if c["url"] not in gia_in_coda:
                    stato["coda"].append({"url": c["url"], "fonte": nome, "testo": tutto[:MAX_TESTO]})
                    gia_in_coda.add(c["url"])
                    log("   + da analizzare:", (c["titolo"] or c["url"])[:90])
            stato["errori"].pop(nome, None)
        except Exception as e:
            log("   ERRORE:", e)
            stato["errori"][nome] = stato["errori"].get(nome, 0) + 1
            problemi.append(f"{nome}: {str(e)[:120]}")
        salva_stato(stato)
    scrivi_pagina(stato)  # la pagina esiste anche se l'analisi successiva venisse interrotta

    # 4. analisi con l'intelligenza artificiale
    nuovi_concorsi = []
    analizzati = 0
    coda = stato["coda"]
    log(f"\n== Analisi AI: {len(coda)} bandi in coda")
    while coda and analizzati < MAX_AI:
        voce = coda[0]
        log(f"   analizzo {analizzati + 1}/{min(len(coda) + analizzati, MAX_AI)}: {voce['url'][:100]}")
        try:
            scheda = pulisci_scheda(chiedi_gemini(prompt_ai(voce["fonte"], voce["url"], voce["testo"], profilo)))
        except QuotaEsaurita as e:
            log("   " + str(e) + ": riprendo domani")
            problemi.append("Gemini: quota gratuita del giorno esaurita, i bandi restanti saranno analizzati domani")
            break
        except Exception as e:
            log("   analisi non riuscita:", voce["url"], "-", e)
            voce["tentativi"] = voce.get("tentativi", 0) + 1
            coda.pop(0)
            if voce["tentativi"] < 3:
                coda.append(voce)
            analizzati += 1
            time.sleep(PAUSA_AI)
            continue
        coda.pop(0)
        analizzati += 1
        if scheda and scheda.get("pertinente") and (not scheda.get("scadenza") or scheda["scadenza"] >= OGGI):
            scheda.pop("pertinente", None)
            if registra(stato, scheda, voce["url"], voce["fonte"]):
                nuovi_concorsi.append(scheda)
                log("   NUOVO:", scheda.get("titolo"), "-", scheda.get("ente"))
        elif scheda:
            log("   scartato:", scheda.get("motivo", "non pertinente"), "-", voce["url"][:90])
        salva_stato(stato)
        time.sleep(PAUSA_AI)

    # 5. avvisi e pagina riepilogativa
    scrivi_pagina(stato)
    salva_stato(stato)
    if primo_avvio:
        invia_telegram("Radar concorsi attivo. Da domani ti scrivo solo quando trovo bandi nuovi o se qualcosa non va.")
    if len(nuovi_concorsi) > 5:
        blocco = f"<b>{len(nuovi_concorsi)} nuovi concorsi trovati</b>"
        for c in nuovi_concorsi:
            scheda = messaggio_concorso(c)
            if len(blocco) + len(scheda) > 3800:
                invia_telegram(blocco)
                blocco = ""
            blocco += "\n\n" + scheda
        if blocco.strip():
            invia_telegram(blocco)
    else:
        for c in nuovi_concorsi:
            invia_telegram("Nuovo concorso\n\n" + messaggio_concorso(c))
    persistenti = [f"{n} (da {k} giorni)" for n, k in stato["errori"].items() if k >= 3]
    if persistenti:
        invia_telegram("Alcune fonti non rispondono da giorni: " + ", ".join(persistenti)
                       + ". Controlla l'indirizzo in fonti.txt.")
    log(f"\nFatto. Nuovi concorsi: {len(nuovi_concorsi)}. In coda per domani: {len(stato['coda'])}.")
    if problemi:
        log("Problemi:\n - " + "\n - ".join(problemi))


if __name__ == "__main__":
    try:
        main()
    except Exception as errore:
        log("ERRORE GENERALE:", errore)
        invia_telegram(f"Radar concorsi: il controllo di oggi non è riuscito ({html.escape(str(errore)[:200])}).")
        sys.exit(1)
