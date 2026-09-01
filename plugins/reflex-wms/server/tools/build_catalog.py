#!/usr/bin/env python3
"""
Fabrique le catalogue MLD embarque dans le connecteur reflex-wms.

Entree  : le MLD HTML livre par Hardis - un fichier .htm par table, sous
          <racine>/tables/. C'est la documentation officielle de l'editeur,
          pas une extraction faite sur la production.
Sortie  : server/catalog/reflex_mld.jsonl.gz - une ligne JSON par table.

    python build_catalog.py "<racine du MLD Hardis>"
    python build_catalog.py "<racine>" --out <fichier.jsonl.gz>

Pourquoi un artefact genere et pas les 1 986 .htm : 40 Mo de HTML, illisibles
par le serveur a chaud, contre 550 Ko compresses qui se chargent en une
fraction de seconde. Le catalogue est versionne a cote du serveur - il fait
partie du connecteur, au meme titre que la liste blanche des chemins GET du
connecteur Shiptify.

A relancer le jour ou Hardis livre un MLD d'une version superieure a 9.14 :
le fichier porte sa version et sa date, et reflex_setup_status les affiche.

PIEGE D'ENCODAGE, constate le 2026-08-28. Les fichiers annoncent tous
`charset=iso-8859-1` dans leur <meta>, et l'annonce est FAUSSE pour une partie
d'entre eux, qui sont en UTF-8. Decoder tout le corpus en latin-1 donne des
libelles corrompus ("Mouvement physique a  executer" au lieu de "a executer"),
et decoder tout en UTF-8 fait echouer les autres. On essaie donc UTF-8 en
strict, et on retombe sur cp1252 - jamais l'inverse : un fichier latin-1 n'est
presque jamais du UTF-8 valide, alors que l'inverse passe silencieusement.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import gzip
import html
import json
import pathlib
import re
import sys

TR = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.S | re.I)
TD = re.compile(r"<td\b[^>]*>(.*?)</td>", re.S | re.I)
TAG = re.compile(r"<[^>]+>")
VERSION = re.compile(r"Version:\s*</b>\s*([0-9.]+)", re.I)
UPDATED = re.compile(r"Upd:\s*</b>\s*([0-9/]+)", re.I)

# Type Adelia -> lettre. La correspondance vers le type SQL Server est dans
# Legend.htm du MLD, et rappelee dans le README du plugin.
#   A Alphanumeric  -> varchar / nvarchar
#   P Packed numeric-> numeric(p,s)
#   N Numerique etendu -> numeric
#   B Binary        -> smallint / numeric
#   I Image         -> image
#   T TimeStamp     -> datetime
TYPE_RULES = (
    ("alphanum", "A"),
    ("packed", "P"),
    ("num", "N"),
    ("binary", "B"),
    ("image", "I"),
    ("timestamp", "T"),
    ("date", "T"),
)


def decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def cells(row: str) -> list[str]:
    """Les cellules porteuses de texte d'une ligne, les separateurs en moins.

    Le gabarit Hardis intercale entre chaque colonne une cellule qui ne
    contient qu'un GIF transparent d'un pixel. Les ecarter ici donne des
    lignes de 8 cellules exactement - l'ordinal, le nom physique, la propriete
    logique, le libelle, le type, la longueur, le rang dans la cle, le top
    virtuel - au lieu de 17 cellules dont une sur deux est vide.
    """
    out: list[str] = []
    for cell in TD.findall(row):
        if "<img" in cell.lower():
            continue
        out.append(html.unescape(TAG.sub("", cell)).replace("\xa0", " ").strip())
    return out


def type_letter(label: str, unknown: collections.Counter) -> str:
    low = label.lower()
    for needle, letter in TYPE_RULES:
        if needle in low:
            return letter
    if label:
        unknown[label] += 1
    return "?"


def parse_table(path: pathlib.Path, unknown: collections.Counter) -> dict | None:
    text = decode(path.read_bytes())
    info: dict[str, str] = {}
    columns: list[list[str]] = []
    for row in TR.findall(text):
        got = cells(row)
        if len(got) == 2 and got[0] in ("Table", "Logical entity", "Description"):
            info.setdefault(got[0], got[1])
        elif len(got) == 8 and got[0].isdigit():
            columns.append(
                [
                    got[1],                                   # nom physique
                    got[2],                                   # propriete logique
                    got[3],                                   # libelle
                    type_letter(got[4], unknown),             # type
                    got[5],                                   # longueur
                    got[6] if got[6] not in ("-", "") else "",  # rang dans la cle
                    "X" if got[7] else "",                    # colonne virtuelle
                ]
            )
    if not columns:
        return None
    table = info.get("Table") or path.stem
    # Le prefixe de colonnes : deux lettres partagees par la quasi-totalite des
    # colonnes d'une table (PE pour HLPRENP, P1 pour HLPRPLP). C'est la cle
    # d'entree la plus utilisee en pratique - on lit PECDPO dans une requete
    # heritee et on cherche de quelle table elle sort.
    common = collections.Counter(c[0][:2] for c in columns).most_common(1)
    return {
        "t": table,
        "e": info.get("Logical entity", ""),
        "d": info.get("Description", ""),
        "p": common[0][0] if common else "",
        "c": columns,
    }


def main(argv: list[str]) -> int:
    here = pathlib.Path(__file__).resolve().parent
    default_out = here.parent / "catalog" / "reflex_mld.jsonl.gz"

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="racine du MLD Hardis (le dossier qui contient tables/)")
    parser.add_argument("--out", default=str(default_out))
    args = parser.parse_args(argv)

    root = pathlib.Path(args.source).expanduser()
    tables_dir = root / "tables" if (root / "tables").is_dir() else root
    files = sorted(tables_dir.glob("*.htm"))
    if not files:
        print(f"Aucun fichier .htm sous {tables_dir}", file=sys.stderr)
        return 2

    # Version et date de generation du MLD : elles sont dans le bandeau de
    # chaque page. On les releve sur la premiere, elles sont identiques partout.
    head = decode(files[0].read_bytes())
    version = (VERSION.search(head).group(1) if VERSION.search(head) else "?").rstrip(".")
    updated = UPDATED.search(head).group(1) if UPDATED.search(head) else "?"

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    unknown: collections.Counter = collections.Counter()
    ntables = ncolumns = skipped = 0

    with gzip.open(out, "wt", encoding="utf-8", compresslevel=9) as fh:
        fh.write(
            json.dumps(
                {
                    "_meta": {
                        "source": "MLD Hardis Reflex",
                        "version": version,
                        "mld_updated": updated,
                        "built": dt.date.today().isoformat(),
                        "tables": len(files),
                    }
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        for path in files:
            record = parse_table(path, unknown)
            if record is None:
                skipped += 1
                continue
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            ntables += 1
            ncolumns += len(record["c"])

    print(f"MLD Reflex v{version} (maj editeur {updated})")
    print(f"{ntables} tables, {ncolumns} colonnes -> {out} ({out.stat().st_size // 1024} Ko)")
    if skipped:
        print(f"{skipped} fichier(s) sans colonne exploitable, ignore(s)")
    if unknown:
        print(f"types non reconnus : {dict(unknown)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
