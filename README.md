# NeuroGrant

Bot che ogni lunedì cerca i bandi di **ricerca clinica sulla riabilitazione** di tutti i Paesi, mettendo in evidenza quelli aperti a enti italiani: ictus, Parkinson, sclerosi multipla e altre malattie neurologiche, invecchiamento e demenze, disabilità, tecnologie per la riabilitazione e malattie croniche non oncologiche quando riguardano la riabilitazione. Invia per email un Excel con tutti i bandi.

Macrogruppi: Ictus · Parkinson · Invecchiamento · Tecnologie riabilitazione · Altre malattie e riabilitazione (SM, SLA, altre neurologiche, disabilità, croniche).

## Fonti

| Fonte | Copertura | Accesso |
|---|---|---|
| EU Funding & Tenders Portal | Tutti i programmi UE (Horizon Europe, EU4Health, Digital Europe, ERC, MSCA, …) | API pubblica |
| Grants.gov | Tutte le agenzie federali USA, NIH compreso | API pubblica |
| `fonti_web.csv` | Ministeri e regioni italiane, fondazioni bancarie, charity, fondazioni ed enti esteri | Lettura delle pagine web |

Per aggiungere una fonte basta una riga in `fonti_web.csv` (nome; indirizzo; copertura; tipo; italia; continente; note). La colonna `continente` (Europa, Nord America, America Latina, Asia, Africa, Oceania, Internazionale) serve a ordinare la pagina pubblica dei bandi. Le fonti coprono Italia, Europa, Nord e Sud America, Asia, Africa, Oceania e le principali fondazioni internazionali; il filtro riconosce anche termini in spagnolo, portoghese, francese e tedesco. La colonna `italia` dice se la fonte finanzia enti italiani:

- `si`: i bandi risultano aperti all’Italia;
- `verificare`: l’LLM legge ogni call e decide;
- `no`: la fonte finanzia solo enti del proprio Paese (per esempio le charity britanniche, canadesi o australiane): viene letta comunque e i suoi bandi finiscono nel gruppo «Altri Paesi».

Il bot parte dall’indirizzo, segue i link che sembrano bandi (fino a due livelli, massimo 35 pagine per sito) e rispetta il file robots.txt di ogni sito. Salta notizie, elenchi di vincitori, portfolio, moduli e pagine con un anno passato nel titolo; senza una scadenza futura, una pagina entra solo se dice che la call è aperta.

## Come seleziona i bandi

1. **Filtro a punteggio** (italiano e inglese) su titolo e testo della call. Un bando entra con un termine specifico nel titolo, oppure con almeno due termini nel testo. Regole principali:
   - i termini generici («robot», «wearable», «anziani», «invecchiamento attivo») e «disabilità» contano solo in un contesto clinico (pazienti, terapia, riabilitazione, trial…): così restano fuori i bandi sociali, urbanistici o sull’inclusione lavorativa;
   - le malattie croniche (cardiovascolari, respiratorie, diabete) contano solo insieme alla riabilitazione; l’oncologia non è più tra i temi;
   - «riabilitazione» conta solo in senso clinico, non per edifici, habitat o detenuti;
   - le frasi standard sui diritti civili e la non discriminazione vengono ignorate;
   - su Grants.gov sono escluse le agenzie senza bandi sanitari (elenco `GG_AGENZIE_ESCLUSE`); sul portale UE i programmi e i cluster senza ricerca clinica (sicurezza, clima, cultura, Missions, infrastrutture…: elenco `EU_PREFISSI_ESCLUSI`).
2. **Italia ammessa solo con una prova esplicita.** Su Grants.gov l’elenco dei tipi di ente («Non-domestic (non-U.S.) Entities») non basta: è un riassunto e spesso contraddice la call. Per i bandi NIH il bot legge la call completa sul NIH Guide e usa la frase ufficiale della sezione «Foreign Organizations» (es. «…are not eligible to apply»). Senza una prova esplicita il bando resta «Da verificare». La frase usata è salvata nel campo `italia_fonte` di `dati/stato.json`.
3. **Esclusioni per regola**, prima dell’LLM:
   - titoli oncologici, di infrastruttura (centri di coordinamento, archivi dati, reti, NIH U24/P30…) o preclinici (modelli animali, in vitro).
4. **LLM** (Gemini, API gratuita di Google): legge la call e stabilisce
   - la **pertinenza alla riabilitazione**: Alta se è dedicata alla riabilitazione o al recupero funzionale; Media se riguarda le stesse malattie ma non la riabilitazione (farmaci, diagnosi, biomarcatori); Bassa per oncologia, condizioni non legate alla riabilitazione o temi non sanitari;
   - il **tipo di ricerca**: clinica, tecnologia, formazione, preclinica, infrastruttura, servizi;
   - se l’**Italia è ammessa**, quando le regole non bastano: vale solo se Gemini cita una frase del bando che lo dice esplicitamente e la frase si ritrova nel testo;
   - titolo e sommario in italiano, e i dati mancanti (partenariato, copertura, chi può candidarsi).

   Vengono **esclusi** i bandi con pertinenza Bassa, quelli preclinici, di infrastruttura o di servizi sociali, le pagine che non sono bandi e le call chiuse. I bandi non aperti all’Italia restano, nel gruppo «Altri Paesi». L’email riporta in fondo quanti bandi sono stati esclusi e perché.
5. **Dati strutturati prima dell’LLM**: scadenze, budget, numero di progetti e copertura dei bandi UE e USA vengono dalle API ufficiali; l’LLM riempie solo i campi vuoti.

Ogni bando viene analizzato una sola volta (il bot ricorda l’esito in `dati/stato.json`, anche per i bandi esclusi). Gemini riceve sei bandi per richiesta, così un’esecuzione normale resta ampiamente nella quota gratuita. Se la quota finisce, i bandi rimasti compaiono come «Non ancora analizzati» e vengono analizzati la settimana successiva.

## Ordine nel report

Email ed Excel seguono lo stesso ordine:

1. **Italia ammessa** («Sì»), poi i bandi in cui l’ammissibilità dell’Italia è **da verificare**, infine gli **altri Paesi** (enti italiani non ammessi);
2. dentro ogni gruppo, **pertinenza alla riabilitazione**: Alta, Media, non ancora analizzati;
3. dentro ogni livello, **scadenza** dalla più vicina; i bandi senza data vanno in fondo.

Nell’email le icone segnalano i bandi che scadono entro 30 giorni (⏰), quelli in apertura (🔜) e i nuovi (🆕).

## Copertura finanziaria

- **Interamente finanziato**: certo (regole UE del tipo di azione, Grants.gov, oppure confermato dall’LLM leggendo il testo).
- **Co-finanziamento richiesto**: certo, con la percentuale quando è indicata.
- **Co-finanziamento da verificare**: il testo cita il co-finanziamento ma non è chiaro se sia obbligatorio.
- **Non indicato**: il testo non ne parla.

## Indicatore di competitività

Combina i progetti finanziabili (dichiarati nel bando, oppure budget totale diviso budget per progetto) con il tasso di successo storico del programma, se lo inserisci in `tassi_successo.csv`:

```
programma_contiene;tasso_percentuale;fonte_dato
horizon europe;15;Dato pubblicato dalla Commissione, anno X
```

Il file parte vuoto di proposito: inserisci solo tassi presi da fonti ufficiali. Senza tasso storico l’indicatore si basa solo sul numero di progetti ed è segnalato come tale. In ogni caso è una stima, perché la probabilità reale dipende dal numero di domande, noto solo a bando chiuso.

## Installazione su GitHub

1. Crea un nuovo repository **Public** chiamato `NeuroGrant`, senza README.
2. Carica con il trascinamento: `neurogrant.py`, `fonti_web.csv`, `tassi_successo.csv`, `requirements.txt`, `README.md`.
3. Clicca «Add file», poi «Create new file», e scrivi come nome `.github/workflows/neurogrant.yml`. Incolla il contenuto del file `neurogrant.yml` e fai il commit.
4. Configura Gemini e l’email (sezioni sotto).
5. Avvia il primo report da Actions → «NeuroGrant settimanale» → «Run workflow».

**Aggiornamento da una versione precedente:** sostituisci `neurogrant.py`, `fonti_web.csv` (ha la nuova colonna `italia`), `README.md` e il contenuto di `.github/workflows/neurogrant.yml`, poi aggiungi il segreto `GEMINI_API_KEY`. Lascia com’è la cartella `dati`. Alla prima esecuzione tutti i bandi vengono rianalizzati con Gemini una volta sola; per questo è normale che il conteggio «rimossi» sia alto.

## Configurare Gemini (una volta sola)

Gemini scrive titoli e sommari in un italiano molto migliore del modello locale usato prima ed è molto più veloce.

1. Apri aistudio.google.com con il tuo account Google e accetta le condizioni.
2. Clicca «Get API key», poi «Create API key», e copia la chiave.
3. Su GitHub, nel repository: «Settings», poi «Secrets and variables», poi «Actions», poi «New repository secret». Nome `GEMINI_API_KEY`, valore: la chiave.

Note:

- Il bot usa `gemini-flash-latest` e, quando la sua quota giornaliera gratuita finisce, passa a `gemini-flash-lite-latest`. Google cambia spesso modelli e quote gratuite. Per usare altri modelli crea in «Variables» (stessa pagina dei segreti) una variabile `GEMINI_MODELS` con i nomi separati da virgole, in ordine di preferenza.
- A Gemini vengono inviati solo i testi pubblici dei bandi. Con il livello gratuito Google può usare i contenuti ricevuti per migliorare i propri prodotti.
- Senza il segreto `GEMINI_API_KEY` il bot torna a usare il modello locale Ollama (più lento e meno preciso).

## Configurare l’email (una volta sola)

Il bot invia la mail dalla tua casella Gmail alla tua casella Gmail. Serve una **password per le app**, valida solo per il bot e revocabile in qualsiasi momento.

**Su Google:**

1. Apri myaccount.google.com, poi «Sicurezza». La **Verifica in due passaggi** deve essere attiva: se non lo è, attivala.
2. Apri myaccount.google.com/apppasswords.
3. Come nome scrivi `NeuroGrant` e clicca «Crea».
4. Copia il codice di 16 lettere che compare. Viene mostrato una sola volta.

**Su GitHub, nel repository NeuroGrant:**

1. Vai in «Settings», poi «Secrets and variables», poi «Actions».
2. Clicca «New repository secret» e crea due segreti:
   - nome `GMAIL_USER`, valore: il tuo indirizzo Gmail;
   - nome `GMAIL_APP_PASSWORD`, valore: il codice di 16 lettere.
3. Facoltativo: un segreto `MAIL_TO` se vuoi ricevere il report su un indirizzo diverso. Senza, arriva allo stesso indirizzo Gmail.

I segreti non sono visibili a nessuno, nemmeno nel repository pubblico.

## Cosa trovi nel repository dopo ogni esecuzione

- `report/NeuroGrant_ultimo.xlsx`: l’ultimo Excel, scaricabile anche senza email.
- `dati/stato.json`: memoria del bot (date di prima apparizione, analisi LLM già fatte, anche per i bandi esclusi).
- Foglio **FONTI** nell’Excel: per ogni fonte, ultimo aggiornamento riuscito ed eventuali errori. Se un sito non dà risultati per settimane, probabilmente carica i contenuti con JavaScript o ha cambiato struttura: si può sostituire con l’indirizzo diretto della sua pagina bandi.

## Limiti

- I siti web vengono letti in modo generico: alcune scadenze o importi possono mancare o essere imprecisi (stato «Scadenza da verificare»). Verificare sempre la call ufficiale.
- Restano escluse le banche dati a pagamento o che vietano la lettura automatica (per esempio GrantWatch).
- L’ammissibilità dell’Italia e il tipo di ricerca sono stime automatiche: un bando segnato «Sì» va comunque verificato nella call, e un bando escluso per errore non compare nel report. Le esclusioni sono conteggiate nel foglio LOG e in fondo all’email.
- Copertura finanziaria e partenariato dei bandi UE seguono le regole standard del tipo di azione: le condizioni del singolo topic possono differire.

### Bandi sanitari a tema libero
Oltre ai bandi di riabilitazione (filtro a parole chiave), entrano i bandi e le fellowship di ricerca clinica o biomedica a **tema libero** solo se aperti a ricercatori o enti di altri Paesi (Italia ammessa con frase verificata). Hanno il macrogruppo «Ricerca sanitaria a tema libero» e pertinenza al massimo «Media».
