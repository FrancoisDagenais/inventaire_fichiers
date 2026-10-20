"""Supprime de PublicBackup les fichiers déjà présents, identiques, dans Public.

Critères (tous requis) : même chemin relatif, même taille, même SHA-256, même date de modification.

Usage:
  nettoyer_backup.py [--db=<db>] [--tolerance=<s>] [--workers=<n>] [--apply]
  nettoyer_backup.py (-h | --help)

Options:
  --db=<db>         Fichier SQLite [default: inventaire_fichiers.db].
  --tolerance=<s>   Écart de date toléré en secondes [default: 2].
  --workers=<n>     Nombre de vérifications réseau en parallèle [default: 16].
  --apply           Supprimer réellement (sinon simulation seulement).
  -h --help         Afficher cette aide.
"""

import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from docopt import docopt

ORI = "/mnt/Public"
BAK = "/mnt/MyBookNoir/PublicBackup"
BATCH = 500


def verifier(candidate):
    """Revérifie l'état réel des deux fichiers (exécuté dans un fil)."""
    bak_path, ori_path, size, bak_mtime, ori_mtime = candidate
    try:
        b = os.stat(bak_path)
        if b.st_size != size or b.st_mtime_ns != bak_mtime:
            return "stale", candidate, None
        o = os.stat(ori_path)
        if o.st_size != size or o.st_mtime_ns != ori_mtime:
            return "stale", candidate, None
        return "ok", candidate, None
    except OSError as exc:
        return "error", candidate, exc


def main() -> int:
    args = docopt(__doc__)
    db_path = Path(args["--db"]).expanduser().resolve()
    tolerance_ns = int(float(args["--tolerance"]) * 1_000_000_000)
    workers = int(args["--workers"])
    apply = args["--apply"]

    if not db_path.exists():
        print(f"Base introuvable : {db_path}", file=sys.stderr)
        return 1

    # Lecture seule : ce script ne modifie jamais la base.
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    bak_prefix = BAK.rstrip("/") + "/"
    ori_prefix = ORI.rstrip("/") + "/"
    high = bak_prefix[:-1] + chr(ord("/") + 1)  # borne haute de l'intervalle de chemins

    # Phase 1 : comparaison dans la base seulement (aucun accès aux disques).
    print("Phase 1 : comparaison dans la base...", file=sys.stderr)
    cursor = conn.execute(
        """
        SELECT b.path, b.size, b.mtime_ns, b.sha256, o.size, o.mtime_ns, o.sha256
        FROM files AS b
        LEFT JOIN files AS o ON o.path = ? || substr(b.path, ?)
        WHERE b.path >= ? AND b.path < ?
        """,
        (ori_prefix, len(bak_prefix) + 1, bak_prefix, high),
    )
    examined = no_match = different = 0
    candidates = []
    for b_path, b_size, b_mtime, b_sha, o_size, o_mtime, o_sha in cursor:
        examined += 1
        if o_size is None:
            no_match += 1
        elif b_size != o_size or b_sha != o_sha or abs(b_mtime - o_mtime) > tolerance_ns:
            different += 1
        else:
            ori_path = ori_prefix + b_path[len(bak_prefix) :]
            candidates.append((b_path, ori_path, b_size, b_mtime, o_mtime))
    conn.close()

    total = len(candidates)
    print(
        f"Phase 1 terminée : {examined:,} fichiers dans PublicBackup, {total:,} candidats identiques.",
        file=sys.stderr,
    )

    # Phase 2 : vérification réseau en parallèle, puis suppression.
    deleted = freed = stale = errors = done = 0
    start = last_report = time.monotonic()
    interrupted = False

    def report():
        elapsed = time.monotonic() - start
        rate = done / elapsed if elapsed else 0
        remaining = (total - done) / rate / 60 if rate else 0
        print(
            f"  {done:,}/{total:,} vérifiés | {deleted:,} "
            f"{'supprimés' if apply else 'à supprimer'} | "
            f"{rate:.1f} fichiers/s | reste ~{remaining:.0f} min",
            file=sys.stderr,
            flush=True,
        )

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for i in range(0, total, BATCH):
                batch = candidates[i : i + BATCH]
                for status, cand, exc in pool.map(verifier, batch):
                    done += 1
                    bak_path, ori_path, size, _, _ = cand
                    if status == "error":
                        print(f"ERREUR : {exc}", file=sys.stderr)
                        errors += 1
                    elif status == "stale":
                        print(f"IGNORÉ (modifié depuis l'inventaire) : {bak_path}")
                        stale += 1
                    else:
                        if apply:
                            try:
                                os.remove(bak_path)
                            except OSError as exc2:
                                print(
                                    f"ERREUR suppression : {bak_path} : {exc2}",
                                    file=sys.stderr,
                                )
                                errors += 1
                                continue
                            print(f"SUPPRIMÉ : {bak_path}")
                        else:
                            print(f"À SUPPRIMER : {bak_path}")
                        deleted += 1
                        freed += size
                    if time.monotonic() - last_report >= 30:
                        report()
                        last_report = time.monotonic()
    except KeyboardInterrupt:
        interrupted = True
        print("\nInterrompu par l'utilisateur.", file=sys.stderr)

    verbe = "Supprimés" if apply else "À supprimer (simulation)"
    print("\nBilan")
    print(f"Fichiers examinés dans PublicBackup : {examined:,}")
    print(f"Sans équivalent dans Public         : {no_match:,}")
    print(f"Présents mais différents            : {different:,}")
    print(f"Candidats identiques (base)         : {total:,}")
    print(f"Candidats vérifiés                  : {done:,}")
    print(f"Ignorés (modifiés depuis le scan)   : {stale:,}")
    print(f"{verbe:<36}: {deleted:,}")
    print(f"Espace libéré                       : {freed / 1024**3:.2f} Go")
    print(f"Erreurs                             : {errors:,}")
    if interrupted:
        print("Exécution incomplète (interrompue).")
    if not apply:
        print("\nSimulation seulement. Relancer avec --apply pour supprimer.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
