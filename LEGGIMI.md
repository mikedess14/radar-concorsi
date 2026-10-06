# Radar concorsi

Ogni mattina questo progetto controlla le fonti elencate in `fonti.txt`, cerca i bandi
per i profili descritti in `profilo.txt` e ti avvisa su Telegram quando ne trova di nuovi.

L'elenco aggiornato dei concorsi aperti è nel file [CONCORSI.md](CONCORSI.md).

## Cosa puoi modificare

- `fonti.txt`: i siti controllati. Per aggiungere un comune o un ente, aggiungi una riga
  `Nome | indirizzo`. Se una fonte dà problemi, metti l'indirizzo esatto della pagina dei bandi.
- `profilo.txt`: la descrizione dei profili che ti interessano, in italiano normale.
- `parole_chiave.txt`: le parole del primo filtro.

Per modificare un file: aprilo su GitHub, premi la matita in alto a destra, cambia il testo
e premi "Commit changes".

## Avviare un controllo a mano

Scheda **Actions** > **Radar concorsi** > **Run workflow**.

## Se qualcosa non va

Apri l'ultima esecuzione nella scheda **Actions**, poi il passaggio "Controlla le fonti":
lì c'è il registro con tutto quello che il programma ha fatto. Copialo e mandalo a Claude.
