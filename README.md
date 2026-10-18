# NeuroGrant

Bot che ogni lunedì cerca i bandi aperti in ambito sanitario su ictus, Parkinson, sclerosi multipla e altre malattie neurologiche, invecchiamento e demenze, disabilità, tecnologie per la riabilitazione e malattie croniche non trasmissibili quando riguardano riabilitazione o gestione a lungo termine. Invia per email un Excel con tutti i bandi.

Macrogruppi: Ictus · Parkinson · Invecchiamento · Tecnologie riabilitazione · Altre malattie e riabilitazione (SM, SLA, altre neurologiche, disabilità, croniche).

## Fonti

| Fonte | Copertura | Accesso |
|---|---|---|
| EU Funding & Tenders Portal | Tutti i programmi UE (Horizon Europe, EU4Health, Digital Europe, ERC, MSCA, …) | API pubblica |
| Grants.gov | Tutte le agenzie federali USA, NIH compreso | API pubblica |
| `fonti_web.csv` | Ministeri e regioni italiane, fondazioni bancarie, charity, fondazioni ed enti esteri | Lettura delle pagine web |

Per aggiungere una fonte basta una riga in `fonti_web.csv` (nome; indirizzo; copertura; tipo; note). Il bot parte dall’indirizzo, segue i link che sembrano bandi (fino a due livelli, massimo 35 pagine per sito) e rispetta il file robots.txt di ogni sito.

## Come seleziona i bandi

1. **Filtro a punteggio** (italiano e inglese) su titolo e testo della call. Un bando entra con un termine specifico nel titolo, oppure con almeno due termini nel testo. Regole principali:
   - i termini generici («robot», «wearable», «ageing») contano solo con un contesto sanitario;
   - le malattie croniche (cardiovascolari, respiratorie, tumori, diabete) contano solo insieme a riabilitazione, cronicità o qualità della vita;
   - «riabilitazione» conta solo in senso clinico, non per edifici, habitat o detenuti;
   - le frasi standard sui diritti civili e la non discriminazione vengono ignorate;
   - su Grants.gov sono escluse le agenzie senza bandi sanitari (agricoltura, trasporti, giustizia, energia…: elenco `GG_AGENZIE_ESCLUSE` nel codice).
2. **LLM gratuito** (Qwen 2.5 7B tramite Ollama, eseguito sul server di GitHub): legge la call e assegna la pertinenza (Alta, Media o Bassa) con il motivo. Scrive inoltre titolo e sommario in italiano e completa i dati mancanti (partenariato, copertura, chi può candidarsi). Non scarta mai un bando: segnala soltanto.
3. **Dati strutturati prima dell’LLM**: scadenze, budget, numero di progetti e copertura dei bandi UE e USA vengono dalle API ufficiali; l’LLM riempie solo i campi vuoti.

L’analisi LLM ha un tempo massimo (240 minuti la settimana, 60 per gli avvii manuali; circa 2-3 minuti per bando). Ogni bando viene analizzato una sola volta: quelli rimasti in coda vengono analizzati alla settimana successiva e intanto compaiono come «Non analizzato».

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
4. Configura l’email (sezione sotto).
5. Avvia il primo report da Actions → «NeuroGrant settimanale» → «Run workflow».

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
- `dati/stato.json`: memoria del bot (date di prima apparizione, analisi LLM già fatte).
- Foglio **FONTI** nell’Excel: per ogni fonte, ultimo aggiornamento riuscito ed eventuali errori. Se un sito non dà risultati per settimane, probabilmente carica i contenuti con JavaScript o ha cambiato struttura: si può sostituire con l’indirizzo diretto della sua pagina bandi.

## Limiti

- I siti web vengono letti in modo generico: alcune scadenze o importi possono mancare o essere imprecisi (stato «Scadenza da verificare»). Verificare sempre la call ufficiale.
- Restano escluse le banche dati a pagamento o che vietano la lettura automatica (per esempio GrantWatch).
- Copertura finanziaria e partenariato dei bandi UE seguono le regole standard del tipo di azione: le condizioni del singolo topic possono differire.
