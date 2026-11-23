# Cubetto — dashboard live di riconoscimento rotazione

Legge il flusso di tocco da un broker MQTT (indirizzo e credenziali presi da
variabili d'ambiente, vedi `.env.example` — nessun dato sensibile è
presente nel codice), topic configurabile (default `cubetto/touch`),
payload = intero 0-63 = bitmask a 6 bit, bit *i* = faccia *i*), lo passa
all'algoritmo (`cube_algorithm.py`) e mostra in tempo reale, in una pagina
web, lo stato del cubo e ogni rotazione riconosciuta.

**Nota:** il codice non è stato testato dal vivo contro il broker perché
l'ambiente in cui è stato scritto non ha accesso di rete a quell'indirizzo
né a PyPI. Va eseguito e verificato in locale.

## Avvio

```bash
python -m venv .venv
source .venv/bin/activate        # su Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # poi compila .env con i tuoi valori reali
export $(grep -v '^#' .env | xargs)
python app.py
```

Poi apri http://127.0.0.1:5000 nel browser: vedrai le 6 facce illuminarsi
quando toccate, il bitmask grezzo, lo stato (asse pinzato + faccia indice)
e l'ultima mossa riconosciuta (spin/ribaltamento), con un log scorrevole di
tutti gli eventi.

## Se la connessione al broker non parte

- Controlla il log in console (`python app.py` stampa lo stato della
  connessione MQTT).
- Il path websocket di default è `/mqtt` (standard paho-mqtt). Se il
  broker usa un path diverso, imposta `CUBETTO_BROKER_WS_PATH` in `.env`.
- Se il payload non è un intero semplice ("9"), controlla il log: i
  payload non interpretabili vengono segnalati (evento `raw_error`) invece
  di far crashare l'app — usa quell'informazione per adattare
  `_parse_bitmask` in `mqtt_client.py`.

## Se le facce non corrispondono a quelle toccate fisicamente

Il bit meno significativo (bit 0) è assunto corrispondere alla faccia 0,
bit 1 → faccia 1, ecc. (`bitmask_to_faces` in `cube_algorithm.py`). Se il
firmware del cubo usa un ordine diverso, aggiorna quella funzione.

## Struttura

- `cube_algorithm.py` — il modello geometrico e il classificatore delle
  rotazioni (`CubeTracker`), verificato con test automatici (round-trip su
  500 mosse casuali, 0 errori).
- `mqtt_client.py` — connessione al broker, parsing del payload, aggiorna
  il tracker.
- `broadcaster.py` — inoltra gli eventi dal thread MQTT alle connessioni
  browser (SSE).
- `app.py` — server Flask, route `/` (dashboard) e `/stream` (SSE).
- `templates/index.html` — dashboard live.
