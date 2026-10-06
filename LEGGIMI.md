# Radar concorsi

Ogni mattina questo progetto controlla le fonti elencate in `fonti.txt`, cerca i bandi
per i profili descritti in `profilo.txt` e pubblica i risultati nella web app
(`index.html`, pubblicata con GitHub Pages). Se hai configurato Telegram, ti avvisa anche lì.

L'elenco è disponibile anche in forma semplice nel file [CONCORSI.md](CONCORSI.md).

## Cosa puoi modificare

- `fonti.txt`: i siti controllati. Per aggiungere un comune o un ente, aggiungi una riga
  `Nome | indirizzo`. Se una fonte dà problemi, metti l'indirizzo esatto della pagina dei bandi.
- `profilo.txt`: la descrizione dei profili che ti interessano, in italiano normale.
- `parole_chiave.txt`: le parole del primo filtro.

Per modificare un file: aprilo su GitHub, premi la matita in alto a destra, cambia il testo
e premi "Commit changes".

## Avviare un controllo a mano

Dalla web app premi "Avvia un controllo ora", oppure: scheda **Actions** > **Radar concorsi** > **Run workflow**.

## Attivare Telegram in un secondo momento

1. Su Telegram crea un bot con @BotFather e mandagli un messaggio.
2. Aggiungi il segreto `TELEGRAM_BOT_TOKEN` e avvia il workflow: nel registro trovi il tuo chat id.
3. Aggiungi il segreto `TELEGRAM_CHAT_ID`.

## Se qualcosa non va

Apri l'ultima esecuzione nella scheda **Actions**, poi il passaggio "Controlla le fonti":
lì c'è il registro con tutto quello che il programma ha fatto. Copialo e mandalo a Claude.
