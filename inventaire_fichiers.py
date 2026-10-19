"""Inventaire incrémental de fichiers avec SQLite et détection de doublons.

Usage:
  inventaire_fichiers.py [--db=<db>] scan <roots>...
  inventaire_fichiers.py [--db=<db>] duplicates [--csv=<csv>]
  inventaire_fichiers.py (-h | --help)

Options:
  --db=<db>    Fichier SQLite [default: inventaire_fichiers.db].
  --csv=<csv>  Exporter les doublons vers ce fichier CSV.
  -h --help    Afficher cette aide.
"""

import csv
import hashlib
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from docopt import docopt

# pour éviter l'arrêt de l'ordinateur: $ systemd-inhibit --what=idle:sleep --why="Inventaire de fichiers en cours" python inventaire_fichiers.py scan /home/francois /mnt/Public /mnt/MyBookNoir


def connect_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY,
            root TEXT NOT NULL DEFAULT '',
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            checked_at TEXT NOT NULL,
            last_seen_scan TEXT NOT NULL DEFAULT ''
        )
    """)
    # Migration douce si la base a été créée avec une version précédente du script.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
    if "root" not in columns:
        conn.execute("ALTER TABLE files ADD COLUMN root TEXT NOT NULL DEFAULT ''")
    if "last_seen_scan" not in columns:
        conn.execute("ALTER TABLE files ADD COLUMN last_seen_scan TEXT NOT NULL DEFAULT ''")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_files_sha256 ON files(sha256)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_files_size ON files(size)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_files_root_seen ON files(root, last_seen_scan)")
    conn.commit()
    return conn


def format_bytes(value: int) -> str:
    """Formate un nombre d'octets en unités lisibles (base 1024)."""
    amount = float(value)
    for unit in ("B", "K", "M", "G", "T", "P"):
        if amount < 1024 or unit == "P":
            if unit == "B":
                return f"{int(amount)}{unit}"
            return f"{amount:.1f}{unit}"
        amount /= 1024
    return f"{amount:.1f}P"


def sha256_file(path: Path, on_bytes_read=None) -> str:
    """Calcule le SHA-256 et signale les octets lus à chaque bloc."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            if on_bytes_read is not None:
                on_bytes_read(len(chunk))
    return digest.hexdigest()


def is_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def scan(conn: sqlite3.Connection, roots: list[Path]) -> None:
    """Actualise les racines une par une, sans charger un million de chemins en RAM."""
    total_examined = total_hashed = total_reused = total_bytes_hashed = total_removed = total_errors = 0
    db_file = Path(conn.execute("PRAGMA database_list").fetchone()[2]).resolve()
    excluded_db_paths = {str(db_file), str(db_file) + "-wal", str(db_file) + "-shm"}

    for root in roots:
        root = root.resolve()
        if not root.exists() or not root.is_dir():
            print(f"ERREUR : racine absente ou inaccessible : {root}. Rien supprimé de cette racine.", file=sys.stderr)
            total_errors += 1
            continue

        # Protection contre un point de montage déconnecté : ne pas scanner son dossier vide.
        # On ne l'applique qu'aux chemins sous /mnt; les racines personnelles restent permises.
        if str(root).startswith("/mnt/") and not os.path.ismount(root):
            print(
                f"ERREUR : {root} n'est pas détecté comme point de montage. Racine ignorée pour éviter une suppression accidentelle.",
                file=sys.stderr,
            )
            total_errors += 1
            continue

        scan_id = datetime.now(timezone.utc).isoformat() + f"-{os.getpid()}"
        examined = hashed = reused = errors = bytes_hashed = 0
        last_progress = time.monotonic()
        now = datetime.now(timezone.utc).isoformat()
        print(f"\nAnalyse : {root}", flush=True)
        conn.execute("BEGIN")

        def report_progress(current_time: float) -> None:
            nonlocal last_progress
            elapsed = int(current_time - last_progress)
            print(
                f"  {examined:,} fichiers | {hashed:,} hashés | "
                f"{reused:,} réutilisés | SHA-256 : {format_bytes(bytes_hashed)} | "
                f"{errors:,} erreurs (~{elapsed} s)",
                flush=True,
            )
            last_progress = current_time

        def count_hashed_bytes(amount: int) -> None:
            nonlocal bytes_hashed, last_progress
            bytes_hashed += amount
            current_time = time.monotonic()
            if current_time - last_progress >= 30:
                # Valider les écritures précédentes même si le fichier courant est très gros.
                conn.commit()
                conn.execute("BEGIN")
                report_progress(current_time)

        try:
            for folder, dirnames, filenames in os.walk(root, followlinks=False):
                dir_path = Path(folder)
                # Ne pas suivre les liens symboliques vers des dossiers.
                dirnames[:] = [name for name in dirnames if not (dir_path / name).is_symlink()]
                for filename in filenames:
                    path = dir_path / filename
                    if path.is_symlink():
                        continue
                    try:
                        stat = path.stat()
                        if not path.is_file():
                            continue
                        path_key = str(path.resolve())
                        examined += 1
                        previous = conn.execute(
                            "SELECT size, mtime_ns, sha256 FROM files WHERE path = ?",
                            (path_key,),
                        ).fetchone()
                        if previous and previous[0] == stat.st_size and previous[1] == stat.st_mtime_ns:
                            checksum = previous[2]
                            reused += 1
                        else:
                            checksum = sha256_file(path, count_hashed_bytes)
                            stat_after = path.stat()
                            if stat.st_size != stat_after.st_size or stat.st_mtime_ns != stat_after.st_mtime_ns:
                                print(f"MODIFIÉ PENDANT LA LECTURE : {path}", file=sys.stderr)
                                errors += 1
                                continue
                            hashed += 1

                        conn.execute(
                            """
                            INSERT INTO files(path, root, size, mtime_ns, sha256, checked_at, last_seen_scan)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(path) DO UPDATE SET
                                root=excluded.root, size=excluded.size, mtime_ns=excluded.mtime_ns,
                                sha256=excluded.sha256, checked_at=excluded.checked_at,
                                last_seen_scan=excluded.last_seen_scan
                        """,
                            (
                                path_key,
                                str(root),
                                stat.st_size,
                                stat.st_mtime_ns,
                                checksum,
                                now,
                                scan_id,
                            ),
                        )

                        # Commit périodiquement pour limiter la taille de la transaction.
                        # Afficher la progression selon le temps écoulé, pas le nombre de fichiers.
                        current_time = time.monotonic()
                        if examined % 1000 == 0:
                            conn.commit()
                            conn.execute("BEGIN")
                        elif current_time - last_progress >= 30:
                            conn.commit()
                            conn.execute("BEGIN")
                            report_progress(current_time)
                    except OSError as exc:
                        print(f"ERREUR : {path} : {exc}", file=sys.stderr)
                        errors += 1

            # Ne purger les entrées absentes que si le parcours a été sans erreur.
            # En cas de permissions refusées ou de lecture instable, on conserve l'ancien inventaire.
            removed = 0
            if errors == 0:
                removed = conn.execute(
                    "DELETE FROM files WHERE root = ? AND last_seen_scan <> ?",
                    (str(root), scan_id),
                ).rowcount
            else:
                print(
                    f"Attention : {errors} erreur(s); aucune entrée ancienne supprimée pour {root}.",
                    file=sys.stderr,
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        total_examined += examined
        total_hashed += hashed
        total_reused += reused
        total_bytes_hashed += bytes_hashed
        total_removed += removed
        total_errors += errors
        print(f"  Fichiers examinés : {examined:,}")
        print(f"  Checksums calculés : {hashed:,}")
        print(f"  Checksums réutilisés : {reused:,}")
        print(f"  Données lues pour SHA-256 : {format_bytes(bytes_hashed)}")
        print(f"  Entrées supprimées : {removed:,}")
        print(f"  Erreurs : {errors:,}")

    print("\nBilan global")
    print(f"Fichiers examinés : {total_examined:,}")
    print(f"Checksums calculés : {total_hashed:,}")
    print(f"Checksums réutilisés : {total_reused:,}")
    print(f"Données lues pour SHA-256 : {format_bytes(total_bytes_hashed)}")
    print(f"Entrées supprimées : {total_removed:,}")
    print(f"Erreurs / racines ignorées : {total_errors:,}")


def duplicates(conn: sqlite3.Connection, csv_path: Path | None) -> None:
    query = """
        SELECT f.sha256, f.size, f.path, groups.file_count
        FROM files AS f
        JOIN (
            SELECT sha256, COUNT(*) AS file_count
            FROM files
            GROUP BY sha256
            HAVING COUNT(*) > 1
        ) AS groups ON groups.sha256 = f.sha256
        ORDER BY f.sha256, f.path
    """
    cursor = conn.execute(query)

    if csv_path:
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        row_count = 0
        with csv_path.open("w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.writer(stream, delimiter=";")
            writer.writerow(
                [
                    "sha256",
                    "nombre_fichiers",
                    "taille_octets",
                    "dossier",
                    "fichier",
                    "chemin_complet",
                ]
            )
            for checksum, size, full_path, count in cursor:
                path = Path(full_path)
                writer.writerow([checksum, count, size, str(path.parent), path.name, full_path])
                row_count += 1
        print(f"Rapport CSV écrit : {csv_path}")
        print(f"Fichiers dans les groupes de doublons : {row_count}")
        return

    current_checksum = None
    row_count = 0
    for checksum, size, full_path, count in cursor:
        row_count += 1
        if checksum != current_checksum:
            if current_checksum is not None:
                print()
            print(f"SHA-256 : {checksum}  |  {count} fichiers  |  {size} octets")
            current_checksum = checksum
        print(f"  {full_path}")
    if row_count == 0:
        print("Aucun doublon trouvé dans l'inventaire.")


def main() -> int:
    args = docopt(__doc__)
    db_path = Path(args["--db"]).expanduser().resolve()

    try:
        with connect_db(db_path) as conn:
            if args["scan"]:
                roots = [Path(root).expanduser().resolve() for root in args["<roots>"]]
                scan(conn, roots)
            elif args["duplicates"]:
                csv_value = args["--csv"]
                duplicates(conn, Path(csv_value).expanduser() if csv_value else None)
    except sqlite3.Error as exc:
        print(f"Erreur SQLite : {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
