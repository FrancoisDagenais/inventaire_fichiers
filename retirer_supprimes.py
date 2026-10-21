"""Retire de la table files les fichiers listés « SUPPRIMÉ : ... » dans un log.

Usage:
  retirer_supprimes.py [--db=<db>] [--apply] <log>
  retirer_supprimes.py (-h | --help)

Options:
  --db=<db>   Fichier SQLite [default: inventaire_fichiers.db].
  --apply     Modifier réellement la base (sinon simulation, rien n'est écrit).
  -h --help   Afficher cette aide.
"""

import os
import sqlite3
import sys
from pathlib import Path

from docopt import docopt

PREFIXE = "SUPPRIMÉ : "
BAK = "/mnt/MyBookNoir/PublicBackup/"
BATCH = 5000


def lire_chemins(log_path: Path):
    """Produit les chemins des lignes « SUPPRIMÉ : ... » du log."""
    with log_path.open(encoding="utf-8", errors="surrogateescape") as stream:
        for ligne in stream:
            ligne = ligne.rstrip("\n")  # ne pas toucher aux espaces : ils peuvent faire partie du nom
            if ligne.startswith(PREFIXE):
                yield ligne[len(PREFIXE) :]


def main() -> int:
    args = docopt(__doc__)
    db_path = Path(args["--db"]).expanduser().resolve()
    log_path = Path(args["<log>"]).expanduser().resolve()
    apply = args["--apply"]

    for p in (db_path, log_path):
        if not p.exists():
            print(f"Introuvable : {p}", file=sys.stderr)
            return 1

    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=30000")

    lues = hors_bak = encore_presents = illisibles = retirees = 0
    batch = []

    def vider_lot():
        nonlocal retirees
        if batch:
            cur = conn.executemany("DELETE FROM files WHERE path = ?", batch)
            retirees += cur.rowcount
            batch.clear()

    conn.execute("BEGIN")
    try:
        for chemin in lire_chemins(log_path):
            lues += 1
            if not chemin.startswith(BAK):
                hors_bak += 1
                continue
            try:
                chemin.encode("utf-8")
            except UnicodeEncodeError:
                illisibles += 1
                continue
            if os.path.lexists(chemin):
                # Le fichier est encore là : ne pas le retirer de la base.
                print(f"ENCORE PRÉSENT : {chemin}", file=sys.stderr)
                encore_presents += 1
                continue
            batch.append((chemin,))
            if len(batch) >= BATCH:
                vider_lot()
                if apply:
                    conn.execute("COMMIT")
                    conn.execute("BEGIN")
                print(f"  {lues:,} lignes lues, {retirees:,} retirées", file=sys.stderr, flush=True)
        vider_lot()
        if apply:
            conn.execute("COMMIT")
        else:
            conn.execute("ROLLBACK")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    print("\nBilan")
    print(f"Lignes « SUPPRIMÉ » lues              : {lues:,}")
    print(f"Hors de PublicBackup (ignorées)        : {hors_bak:,}")
    print(f"Caractères illisibles (ignorées)       : {illisibles:,}")
    print(f"Encore présentes sur disque (ignorées) : {encore_presents:,}")
    print(f"{'Retirées de la base' if apply else 'À retirer de la base (simulation)':<39}: {retirees:,}")
    if not apply:
        print("\nSimulation seulement. Relancer avec --apply pour modifier la base.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
