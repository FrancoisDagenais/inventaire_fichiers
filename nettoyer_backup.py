"""Supprime de PublicBackup les fichiers déjà présents, identiques, dans Public.

Critères (tous requis) : même chemin relatif, même taille, même SHA-256, même date de modification.

Usage:
  nettoyer_backup.py [--db=<db>] [--tolerance=<s>] [--apply]
  nettoyer_backup.py (-h | --help)

Options:
  --db=<db>         Fichier SQLite [default: inventaire_fichiers.db].
  --tolerance=<s>   Écart de date toléré en secondes [default: 2].
  --apply           Supprimer réellement (sinon simulation seulement).
  -h --help         Afficher cette aide.
"""

import os
import sqlite3
import sys
from pathlib import Path

from docopt import docopt

ORI = "/mnt/Public"
BAK = "/mnt/MyBookNoir/PublicBackup"


def main() -> int:
    args = docopt(__doc__)
    db_path = Path(args["--db"]).expanduser().resolve()
    tolerance_ns = int(float(args["--tolerance"]) * 1_000_000_000)
    apply = args["--apply"]

    if not db_path.exists():
        print(f"Base introuvable : {db_path}", file=sys.stderr)
        return 1

    # Lecture seule : ce script ne modifie jamais la base.
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    bak_prefix = BAK.rstrip("/") + "/"
    ori_prefix = ORI.rstrip("/") + "/"

    examined = identical = deleted = freed = 0
    no_match = different = stale = errors = 0

    cursor = conn.execute(
        "SELECT path, size, mtime_ns, sha256 FROM files WHERE path LIKE ? ESCAPE '\\'",
        (
            bak_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            + "%",
        ),
    )

    for bak_path, bak_size, bak_mtime, bak_sha in cursor:
        examined += 1
        relative = bak_path[len(bak_prefix) :]
        ori_path = ori_prefix + relative

        ori = conn.execute(
            "SELECT size, mtime_ns, sha256 FROM files WHERE path = ?", (ori_path,)
        ).fetchone()
        if ori is None:
            no_match += 1
            continue

        ori_size, ori_mtime, ori_sha = ori
        if (
            bak_size != ori_size
            or bak_sha != ori_sha
            or abs(bak_mtime - ori_mtime) > tolerance_ns
        ):
            different += 1
            continue

        identical += 1

        # Revérifier l'état réel des deux fichiers : la base peut dater un peu.
        try:
            bak_stat = os.stat(bak_path)
            ori_stat = os.stat(ori_path)
        except OSError as exc:
            print(f"ERREUR : {exc}", file=sys.stderr)
            errors += 1
            continue
        if (
            bak_stat.st_size != bak_size
            or bak_stat.st_mtime_ns != bak_mtime
            or ori_stat.st_size != ori_size
            or ori_stat.st_mtime_ns != ori_mtime
        ):
            print(f"IGNORÉ (modifié depuis l'inventaire) : {relative}")
            stale += 1
            continue

        if apply:
            try:
                os.remove(bak_path)
            except OSError as exc:
                print(f"ERREUR suppression : {bak_path} : {exc}", file=sys.stderr)
                errors += 1
                continue
            print(f"SUPPRIMÉ : {bak_path}")
        else:
            print(f"À SUPPRIMER : {bak_path}")
        deleted += 1
        freed += bak_size

    conn.close()

    verbe = "Supprimés" if apply else "À supprimer (simulation)"
    print("\nBilan")
    print(f"Fichiers examinés dans PublicBackup : {examined:,}")
    print(f"Sans équivalent dans Public         : {no_match:,}")
    print(f"Présents mais différents            : {different:,}")
    print(f"Identiques                          : {identical:,}")
    print(f"Ignorés (modifiés depuis le scan)   : {stale:,}")
    print(f"{verbe:<36}: {deleted:,}")
    print(f"Espace libéré                       : {freed / 1024**3:.2f} Go")
    print(f"Erreurs                             : {errors:,}")
    if not apply:
        print("\nSimulation seulement. Relancer avec --apply pour supprimer.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
