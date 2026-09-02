#!/usr/bin/env python3
"""
Controles HORS LIGNE du connecteur indices-eu.

    python test_offline.py

Aucun appel reseau : tout tourne sur des donnees fabriquees ici. C'est
volontaire - une suite qui a besoin d'Internet ne se lance pas quand il faut,
c'est-a-dire quand quelque chose est deja casse.

Ce que ces controles protegent, dans l'ordre ou ca a mordu :

1. L'APPARIEMENT PAYS <-> PRIX dans le classeur du bulletin. Le script Power
   Query d'origine lisait le code pays dans la VALEUR de la colonne CTR et n'en
   gardait que deux caracteres : `EU_` et `EUR_` donnaient tous deux "EU", donc
   la moyenne de l'Union ecrasait celle de la zone euro. Ici le code se lit dans
   le NOM de la colonne, et le controle le verifie.
2. LES TROUS D'EUROSTAT. Le script d'origine remplacait les index manquants par
   0 : ca donnait un salaire minimum de 0 EUR au Danemark, qui n'a pas de
   salaire minimum legal. Ce qui manque doit rester manquant.
3. LA GRECE. Eurostat la code EL, le bulletin petrolier GR. Un mauvais code ne
   leve aucune erreur, il rend zero ligne - ce qui se lit comme "pas de donnee".
4. L'EXCLUSIVITE d'un chemin de configuration explicite. Tant que
   INDICES_SHARED_ENV n'etait pas exclusif chez les autres connecteurs, cette
   suite tournait en fait avec la vraie configuration d'equipe.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent

# Isole le serveur AVANT de l'importer : pas de fichier d'equipe, pas de fichier
# de poste, un cache et un dossier d'export jetables.
_TMP = pathlib.Path(tempfile.mkdtemp(prefix="indices-mcp-test-"))
os.environ["INDICES_SHARED_ENV"] = str(_TMP / "aucun-fichier-d-equipe.env")
os.environ["INDICES_ENV_FILE"] = str(_TMP / "aucun-fichier-de-poste.env")
os.environ["INDICES_CACHE_DIR"] = str(_TMP / "cache")
os.environ["INDICES_EXPORT_DIR"] = str(_TMP / "exports")
os.environ.pop("INDICES_PAYS_DEFAUT", None)
os.environ.pop("INDICES_GASOIL_FILE", None)

sys.path.insert(0, str(HERE))
_spec = importlib.util.spec_from_file_location("indices_server", HERE / "server.py")
srv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(srv)

_ECHECS: list[str] = []
_PASSES = 0


def verifie(titre: str, condition: bool, detail: str = "") -> None:
    global _PASSES
    if condition:
        _PASSES += 1
        print(f"  OK    {titre}")
    else:
        _ECHECS.append(titre)
        print(f"  ECHEC {titre}" + (f" | {detail}" if detail else ""))


def bloc(titre: str) -> None:
    print(f"\n{titre}")


# --------------------------------------------------------------------------
bloc("1. Les bornes de periode")

verifie("une annee donne l'annee entiere", srv._bornes("2024") == (dt.date(2024, 1, 1), dt.date(2024, 12, 31)))
verifie("un mois donne le mois entier", srv._bornes("2024-06") == (dt.date(2024, 6, 1), dt.date(2024, 6, 30)))
verifie("fevrier bissextile", srv._bornes("2024-02")[1] == dt.date(2024, 2, 29))
verifie("decembre ne deborde pas sur l'annee suivante", srv._bornes("2024-12")[1] == dt.date(2024, 12, 31))
verifie("un semestre S1", srv._bornes("2024-S1") == (dt.date(2024, 1, 1), dt.date(2024, 6, 30)))
verifie("un semestre S2", srv._bornes("2024-S2") == (dt.date(2024, 7, 1), dt.date(2024, 12, 31)))
verifie("un trimestre Q3", srv._bornes("2024-Q3") == (dt.date(2024, 7, 1), dt.date(2024, 9, 30)))
verifie("un jour precis", srv._bornes("2024-06-15") == (dt.date(2024, 6, 15), dt.date(2024, 6, 15)))

try:
    srv._bornes("hier")
    verifie("une periode incomprise est REFUSEE, pas devinee", False)
except srv.ConfigError:
    verifie("une periode incomprise est REFUSEE, pas devinee", True)

# --------------------------------------------------------------------------
bloc("2. Le lexique, et le piege de la Grece")

verifie("un nom francais se resout", srv._resoudre_pays("Allemagne", "eurostat") == ("DE", "DE"))
verifie("la casse et les accents ne comptent pas", srv._resoudre_pays("ESPAGNE", "eurostat") == ("ES", "ES"))
verifie(
    "Grece -> EL cote Eurostat",
    srv._resoudre_pays("Grece", "eurostat") == ("EL", "EL"),
)
verifie(
    "Grece -> GR cote bulletin petrolier",
    srv._resoudre_pays("Grece", "gasoil") == ("EL", "GR"),
    "c'est LE piege du connecteur : un mauvais code rend zero ligne sans erreur",
)
verifie("'GR' en entree est accepte et ramene a EL", srv._resoudre_pays("GR", "eurostat")[0] == "EL")
verifie(
    "zone euro -> EUR cote bulletin petrolier",
    srv._resoudre_pays("zone euro", "gasoil") == ("EA", "EUR"),
)
try:
    srv._resoudre_pays("Zorglub", "eurostat")
    verifie("un pays inconnu est REFUSE", False)
except srv.ConfigError:
    verifie("un pays inconnu est REFUSE", True)

verifie("'gazole' est un alias de diesel", (srv._lexique()["produits"]).get("gazole") == "diesel")
verifie("'essence' est un alias de euro95", (srv._lexique()["produits"]).get("essence") == "euro95")

# --------------------------------------------------------------------------
bloc("3. Le depliage JSON-stat d'Eurostat")

# Deux pays, trois periodes, et DEUX TROUS : FR-2024-02 et DK partout sauf une.
# C'est exactement la forme que rend Eurostat pour le salaire minimum.
DOC = {
    "id": ["freq", "geo", "time"],
    "size": [1, 2, 3],
    "updated": "2026-01-01T00:00:00+0100",
    "dimension": {
        "freq": {"category": {"index": {"S": 0}, "label": {"S": "Semestriel"}}},
        "geo": {"category": {"index": {"FR": 0, "DK": 1}, "label": {"FR": "France", "DK": "Danemark"}}},
        "time": {"category": {"index": {"2024-S1": 0, "2024-S2": 1, "2025-S1": 2}}},
    },
    # index plat = freq*6 + geo*3 + time
    "value": {"0": 1766.9, "2": 1801.8, "4": 1300.0},
    "status": {"2": "p"},
}
points = srv._jsonstat_points(DOC)
par_cle = {(p["geo"], p["time"]): p for p in points}
verifie("le nombre de points est celui des valeurs presentes", len(points) == 3, f"{len(points)} points")
verifie("decodage par pas : index 0 = FR / 2024-S1", par_cle.get(("FR", "2024-S1"), {}).get("valeur") == 1766.9)
verifie("decodage par pas : index 2 = FR / 2025-S1", par_cle.get(("FR", "2025-S1"), {}).get("valeur") == 1801.8)
verifie("decodage par pas : index 4 = DK / 2024-S2", par_cle.get(("DK", "2024-S2"), {}).get("valeur") == 1300.0)
verifie(
    "un trou reste un TROU, il ne devient pas zero",
    ("FR", "2024-S2") not in par_cle and ("DK", "2024-S1") not in par_cle,
    "c'est le defaut du script Power Query d'origine : 0 EUR de salaire minimum",
)
verifie("le statut de publication est remonte", par_cle[("FR", "2025-S1")]["statut"] == "p")
verifie("le libelle de dimension est repris", par_cle[("FR", "2024-S1")]["geo_label"] == "France")

# --------------------------------------------------------------------------
bloc("4. La lecture du classeur du bulletin petrolier")


def _classeur_factice() -> bytes:
    """Un classeur a la forme du bulletin : trois lignes d'en-tete, des blocs
    par pays, une colonne de taux de change qui decale les blocs, et une note
    de bas de page sans date."""
    import openpyxl

    book = openpyxl.Workbook()
    for taxes, (nom, marqueur) in srv.WOB_SHEETS.items():
        sheet = book.create_sheet(nom)
        entetes = ["Consumer prices", "CTR"]
        for code in ("EU", "EUR", "FR", "GR"):
            if code == "GR":
                # La Grece n'est pas dans la zone euro dans ce faux classeur :
                # elle porte donc une colonne de taux de change, comme la
                # Bulgarie ou la Pologne dans le vrai. C'est ce decalage qui
                # cassait l'appariement par position.
                entetes.append(f"{code}_exchange_rate")
            entetes.extend(f"{code}{marqueur}{p}" for p in ("euro95", "diesel"))
            entetes.append("CTR")
        sheet.append(entetes)
        sheet.append([None] * len(entetes))
        sheet.append(["Date"] + [None] * (len(entetes) - 1))
        base = {"EU": 2000.0, "EUR": 2100.0, "FR": 2200.0, "GR": 1900.0}
        for semaine, jour in enumerate(
            (dt.datetime(2026, 8, 24), dt.datetime(2026, 8, 17)), start=0
        ):
            ligne: list = [jour, "EU_"]
            for code in ("EU", "EUR", "FR", "GR"):
                if code == "GR":
                    ligne.append(1.0)
                ligne.extend([base[code] - 100, base[code] - semaine * 10])
                ligne.append(f"{code}_")
            sheet.append(ligne)
        sheet.append(["(1) Note de bas de page, sans date", None])
    del book["Sheet"]
    buf = io.BytesIO()
    book.save(buf)
    return buf.getvalue()


parsed = srv._parse_wob(_classeur_factice())
ttc = parsed["ttc"]
verifie("les deux feuilles de prix sont lues", set(parsed) == {"ttc", "ht"})
verifie("la note de bas de page n'est pas comptee comme une semaine", ttc["dates"] == ["2026-08-24", "2026-08-17"])
verifie(
    "EU et EUR restent DEUX series distinctes",
    ttc["series"]["EU"]["diesel"][0] != ttc["series"]["EUR"]["diesel"][0],
    "le script Power Query d'origine les confondait en tronquant CTR a 2 caracteres",
)
verifie("le prix suit la bonne colonne malgre le taux de change", ttc["series"]["GR"]["diesel"] == [1900.0, 1890.0])
verifie("les deux produits sont distingues", ttc["series"]["FR"]["euro95"][0] == 2100.0)
verifie("la feuille hors taxes porte les memes pays", set(parsed["ht"]["series"]) == set(ttc["series"]))

# --------------------------------------------------------------------------
bloc("5. La serie gazole, sur un cache pose a la main")

srv._cache_write(
    "gasoil",
    {
        "source": "cache de test",
        "provenance": "classeur factice pose par test_offline.py",
        "data": parsed,
    },
)
lignes, meta = srv._serie_gasoil("Grece", "", "", "diesel", "ttc")
verifie("la Grece rend des lignes malgre l'ecart de code", len(lignes) == 2, f"{len(lignes)} lignes")
verifie("le code rendu est celui d'Eurostat", lignes and lignes[0]["code_pays"] == "EL")
verifie("le pays est nomme en francais", lignes and lignes[0]["pays"] == "Grece")
verifie("le tri est du plus recent au plus ancien", [l["date"] for l in lignes] == ["2026-08-24", "2026-08-17"])
verifie("l'unite accompagne la valeur", lignes and lignes[0]["unite"] == "EUR/1000 l")

bornees, _ = srv._serie_gasoil("Grece", "2026-08-20", "", "diesel", "ttc")
verifie("la borne basse filtre", [l["date"] for l in bornees] == ["2026-08-24"])
bornees, _ = srv._serie_gasoil("Grece", "", "2026-08-20", "diesel", "ttc")
verifie("la borne haute filtre", [l["date"] for l in bornees] == ["2026-08-17"])

_, meta_absent = srv._serie_gasoil("Pologne", "", "", "diesel", "ttc")
verifie(
    "un pays absent du classeur est SIGNALE, pas passe sous silence",
    any("PL" in str(a) for a in meta_absent["absents"]),
    str(meta_absent["absents"]),
)

alias, _ = srv._serie_gasoil("FR", "", "", "gazole", "ttc")
verifie("'gazole' en entree est compris comme diesel", alias and alias[0]["produit"] == "diesel")
try:
    srv._serie_gasoil("FR", "", "", "kerosene", "ttc")
    verifie("un produit inconnu est REFUSE", False)
except srv.ConfigError:
    verifie("un produit inconnu est REFUSE", True)

# --------------------------------------------------------------------------
bloc("5 bis. Le pays que la source a cesse de publier")

# Le cas du Royaume-Uni : il figure encore dans le classeur, mais ses releves
# s'arretent fin 2020. Sans avertissement, une question sur le gazole outre-Manche
# rend un prix de 2020 en tete de tableau, la ou tout le reste est de la semaine
# derniere - une valeur, pas une case vide, donc rien ne se voit.
gele = {
    "ttc": {
        "dates": ["2026-08-24", "2026-08-17", "2020-12-21"],
        "series": {
            "FR": {"diesel": [2231.0, 2199.0, 1300.0]},
            "UK": {"diesel": [None, None, 1300.85]},
            "GR": {"diesel": [1900.0, 1890.0, 1100.0]},
        },
    },
    "ht": {"dates": [], "series": {}},
}
srv._cache_write("gasoil", {"source": "test", "provenance": "fige", "data": gele})

# En mode `europe`, l'avertissement de gel doit toujours partir : c'est le mode
# qui interroge REELLEMENT le bulletin, et c'est la qu'un prix de 2020 sortirait.
_, meta_uk = srv._serie_gasoil("UK", "", "", "diesel", "ttc", source="europe")
verifie(
    "source=europe : un pays fige est signale, avec sa derniere date",
    any("2020-12-21" in n and "UK" in n for n in meta_uk.get("notes") or []),
    str(meta_uk.get("notes")),
)
_, meta_fr = srv._serie_gasoil("FR", "", "", "diesel", "ttc")
verifie("un pays a jour ne declenche aucun avertissement", not (meta_fr.get("notes") or []))

# Meme sur une question ancienne, l'avertissement doit tomber : la couverture se
# mesure sur toute la serie, pas sur la fenetre demandee.
_, meta_vieux = srv._serie_gasoil(
    "UK", "2020-01", "2020-12-31", "diesel", "ttc", source="europe"
)
verifie(
    "l'avertissement tient meme si la question porte sur 2020",
    any("UK" in n for n in meta_vieux.get("notes") or []),
)

_, meta_ch = srv._serie_gasoil("Suisse", "", "", "diesel", "ttc")
verifie(
    "un pays hors bulletin petrolier est nomme comme tel",
    any("hors bulletin petrolier" in str(a) for a in meta_ch["absents"]),
    str(meta_ch["absents"]),
)

points_geles = [
    {"geo": "FR", "time": "2026-08", "valeur": 2.7, "statut": ""},
    {"geo": "CH", "time": "2026-07", "valeur": 0.2, "statut": ""},
    {"geo": "UK", "time": "2020-11", "valeur": 0.3, "statut": ""},
]
couv, dernier = srv._couverture_points(points_geles, {"FR", "CH", "UK"})
verifie("la derniere periode du flux est trouvee", dernier == "2026-08")
notes = srv._note_arret(couv, dernier, 70, "une inflation")
verifie("le Royaume-Uni fige est signale sur l'inflation", any("UK" in n for n in notes))
verifie(
    "un mois de retard normal (CH, NO, IS hors UE) ne declenche rien",
    not any("CH" in n for n in notes),
    "sinon l'avertissement crie a chaque publication et on cesse de le lire",
)

# Remet le classeur factice complet pour la suite des controles.
srv._cache_write("gasoil", {"source": "test", "provenance": "factice", "data": parsed})

# --------------------------------------------------------------------------
bloc("6. L'export CSV")

sortie = srv.indices_export_csv("gazole", pays="FR,Grece", filename="controle.csv")
cible = srv._export_dir() / "controle.csv"
verifie("le fichier est ecrit", cible.is_file(), sortie)
brut = cible.read_bytes()
verifie("il porte le BOM UTF-8, pour Excel FR", brut.startswith(b"\xef\xbb\xbf"))
verifie("il est en point-virgule", b";" in brut.splitlines()[0])
verifie(
    "les fins de ligne sont ecrites par le module csv, pas par la plateforme",
    brut.count(b"\r\n") == len(brut.splitlines()) - 0 or b"\r\n" in brut,
)
verifie("l'export dit le chemin ET le compte", "controle.csv" in sortie and "ligne(s)" in sortie)
verifie("l'export ne va PAS dans la bibliotheque d'equipe", not srv._is_synced(cible))

vide = srv.indices_export_csv("gazole", pays="Pologne", depuis="1999", jusqu_a="1999")
verifie("un perimetre vide n'ecrit aucun fichier et le dit", "Rien a exporter" in vide, vide[:120])

# --------------------------------------------------------------------------
bloc("7. La configuration")

verifie(
    "un chemin de fichier d'equipe explicite est EXCLUSIF",
    srv._shared_env_candidates() == [pathlib.Path(os.environ["INDICES_SHARED_ENV"])],
    "sinon la suite tournerait avec la vraie configuration d'equipe",
)
verifie("aucun reglage d'equipe n'est charge dans ce test", srv._load_shared_env() == {})
verifie("la racine locale est dans le profil, pas dans %LOCALAPPDATA%", ".indices-mcp" in str(srv._local_root()))
verifie("le mode TLS par defaut est strict", srv._tls_mode() == "strict")
os.environ["INDICES_GASOIL_TLS"] = "n-importe-quoi"
srv._ENV_DONE = True
verifie("un mode TLS inconnu retombe sur strict, il n'ouvre rien", srv._tls_mode() == "strict")
os.environ.pop("INDICES_GASOIL_TLS")

verifie(
    "un certificat *.europa.eu est reconnu comme appartenant a la Commission",
    srv._covers_europa(["europa.eu", "*.europa.eu"]),
)
verifie(
    "un certificat d'un autre domaine ne l'est pas",
    not srv._covers_europa(["exemple.fr", "*.exemple.fr"]),
    "c'est ce controle qui empeche le mode chaine-seule d'accepter n'importe qui",
)

parse = srv._parse_env('A=1\n# commentaire\nB="valeur avec espace"\nC=http://x/y?a=1&b=2\n')
verifie("le parseur .env garde une URL entiere", parse.get("C") == "http://x/y?a=1&b=2")
verifie("le parseur .env retire les guillemets", parse.get("B") == "valeur avec espace")

# --------------------------------------------------------------------------
bloc("8. Le pays sans salaire minimum legal")

verifie(
    "les huit pays sans salaire minimum legal sont connus du serveur",
    "DK" in srv.SANS_SALAIRE_MINIMUM_LEGAL and "IT" in srv.SANS_SALAIRE_MINIMUM_LEGAL,
)
rendu = srv.indices_pays()
verifie("indices_pays le signale pays par pays", "pas de salaire minimum legal" in rendu)
verifie("indices_pays donne le code du bulletin petrolier", "GR" in rendu and "EL;Grece;GR" in rendu)

# --------------------------------------------------------------------------
bloc("8 bis. Les sources nationales : Royaume-Uni et Norvege")

# Le releve DESNZ, fabrique ici. Sept colonnes, dont l'accise et la TVA : c'est
# ce qui permet de reconstituer le hors taxes, que gov.uk ne publie pas.
CSV_UK = (
    "﻿Date,ULSP Pump price in pence/litre,ULSD Pump price in pence/litre,"
    "ULSP Duty rate in pence/litre,ULSD Duty rate in pence/litre,"
    "ULSP VAT percentage rate,ULSD VAT percentage rate\n"
    # TVA a 17,5 % : le calcul du HT ne doit PAS supposer 20 % partout.
    "09/06/2003,74.59,76.77,45.82,45.82,17.5,17.5\n"
    "31/08/2026,161.61,183.49,52.95,52.95,20,20\n"
)
uk = srv._parse_desnz([CSV_UK])
verifie("DESNZ : les deux blocs de taxes sont produits", set(uk) == {"ttc", "ht"})
verifie(
    "DESNZ : la date jj/mm/aaaa n'est pas lue a l'envers",
    uk["ttc"]["dates"] == ["2003-06-09", "2026-08-31"],
    str(uk["ttc"]["dates"]),
)
verifie(
    "DESNZ : le prix a la pompe est repris tel quel",
    uk["ttc"]["series"]["UK"]["diesel"][-1] == 183.49,
)
# 183.49 / 1.20 = 152.908 ; - 52.95 d'accise = 99.958 -> 99.96
verifie(
    "DESNZ : le hors taxes est reconstitue depuis l'accise ET la TVA",
    uk["ht"]["series"]["UK"]["diesel"][-1] == 99.96,
    str(uk["ht"]["series"]["UK"]["diesel"][-1]),
)
# 74.59 / 1.175 = 63.481 ; - 45.82 = 17.66. Avec 20 % suppose, on aurait 16.34 :
# l'ecart de 1,3 penny par litre est exactement ce que ce controle protege.
verifie(
    "DESNZ : le taux de TVA d'EPOQUE est utilise, pas le taux courant",
    uk["ht"]["series"]["UK"]["euro95"][0] == 17.66,
    str(uk["ht"]["series"]["UK"]["euro95"][0]),
)
try:
    srv._parse_desnz(["Semaine;prix\n01/01/2020;100\n"])
    verifie("DESNZ : un CSV a la structure changee est REFUSE", False)
except srv.SourceError:
    verifie("DESNZ : un CSV a la structure changee est REFUSE", True)

# SSB : JSON-stat mensuel, en couronnes, sans hors taxes.
SSB = {
    "dimension": {
        "PetroleumProd": {"category": {"index": {"031": 0, "035": 1}}},
        "Tid": {"category": {"index": {"2026M06": 0, "2026M07": 1}}},
    },
    "value": [19.10, 19.59, 20.02, 20.25],
}
no = srv._parse_ssb(SSB)
verifie(
    "SSB : les periodes mensuelles deviennent des premiers de mois",
    no["ttc"]["dates"] == ["2026-06-01", "2026-07-01"],
    str(no["ttc"]["dates"]),
)
verifie(
    "SSB : le code 035 est bien le gazole, pas l'essence",
    no["ttc"]["series"]["NO"]["diesel"] == [20.02, 20.25],
    str(no["ttc"]["series"]["NO"]),
)
verifie(
    "SSB : le bloc hors taxes reste VIDE, il n'est pas recopie du TTC",
    no["ht"]["dates"] == [] and no["ht"]["series"] == {},
)

# La serie complete, sur des caches poses a la main : c'est l'aiguillage par
# pays qui est teste, pas le reseau.
srv._cache_write("gasoil_uk", {"source": "t", "provenance": "test UK", "data": uk})
srv._cache_write("gasoil_no", {"source": "t", "provenance": "test NO", "data": no})

lignes_auto, meta_auto = srv._serie_gasoil("UK,NO,FR", "", "", "diesel", "ttc")
par_pays = {r["code_pays"]: r for r in lignes_auto}
verifie(
    "auto : le Royaume-Uni vient de DESNZ, pas du bulletin fige",
    par_pays.get("UK", {}).get("source") == "desnz",
    str(par_pays.get("UK")),
)
verifie(
    "auto : la Norvege vient de SSB",
    par_pays.get("NO", {}).get("source") == "ssb",
)
verifie(
    "auto : un Etat membre reste sur le bulletin europeen",
    par_pays.get("FR", {}).get("source") == "europe",
)
verifie(
    "auto : PLUS d'avertissement de gel sur le Royaume-Uni",
    not any("2020-12-21" in n and "fige au" in n and "ATTENTION" in n
            for n in meta_auto.get("notes") or []),
    str(meta_auto.get("notes")),
)
verifie(
    "chaque ligne porte son unite, et elles diffferent",
    {r["unite"] for r in lignes_auto} == {"EUR/1000 l", "GBp/l", "NOK/l"},
    str({r["unite"] for r in lignes_auto}),
)
verifie(
    "chaque ligne porte sa frequence",
    par_pays["NO"]["frequence"] == "mensuelle"
    and par_pays["UK"]["frequence"] == "hebdomadaire",
)
verifie(
    "le melange d'unites est ANNONCE, pas laisse a la vigilance du lecteur",
    any("PLUSIEURS SOURCES" in n for n in meta_auto.get("notes") or []),
)

# Le hors taxes norvegien n'existe pas : il doit etre REFUSE, pas comble.
_, meta_ht = srv._serie_gasoil("NO", "", "", "diesel", "ht")
verifie(
    "la Norvege refuse le hors taxes au lieu de rendre le TTC",
    any("ne publie pas le hors taxes" in str(a) for a in meta_ht.get("absents") or []),
    str(meta_ht.get("absents")),
)
# Un produit que la source nationale ne porte pas doit etre nomme.
_, meta_lpg = srv._serie_gasoil("UK", "", "", "LPG", "ttc")
verifie(
    "un produit absent d'une source nationale est nomme, pas rendu vide",
    any("n'est pas publie par" in str(a) for a in meta_lpg.get("absents") or []),
    str(meta_lpg.get("absents")),
)
# Et la Suisse n'a toujours aucun prix : c'est un fait, pas un oubli.
_, meta_ch2 = srv._serie_gasoil("CH", "", "", "diesel", "ttc", source="national")
verifie(
    "la Suisse : aucune source nationale de prix, et c'est dit",
    any("aucune source nationale" in str(a) for a in meta_ch2.get("absents") or []),
    str(meta_ch2.get("absents")),
)
try:
    srv._serie_gasoil("FR", "", "", "diesel", "ttc", source="peu importe")
    verifie("une source inconnue est REFUSEE", False)
except srv.ConfigError:
    verifie("une source inconnue est REFUSEE", True)

# --------------------------------------------------------------------------
bloc("8 ter. Le salaire minimum britannique, en taux horaire")

CORPS_NMW = (
    "<table><tr><th></th><th>21 and over</th><th>18 to 20</th></tr>"
    "<tr><td>April 2026</td><td>£12.71</td><td>£10.85</td></tr></table>"
    "<table><tr><th></th><th>23 and over</th><th>21 to 22</th></tr>"
    "<tr><td>April 2023 to March 2024</td><td>£10.42</td><td>£10.18</td></tr></table>"
)
nmw = srv._parse_nmw(CORPS_NMW)
verifie("NMW : les deux periodes sont lues", len(nmw) == 2, str(nmw))
verifie(
    "NMW : la prise d'effet est le PREMIER mois cite, pas le dernier",
    nmw[0]["debut"] == dt.date(2026, 4, 1),
    str(nmw[0]),
)
verifie(
    "NMW : le taux retenu est celui de la tranche adulte haute",
    nmw[0]["valeur"] == 12.71,
)
verifie(
    "NMW : la tranche d'age est rendue, parce qu'elle CHANGE dans le temps",
    nmw[0]["tranche"] == "21 and over" and nmw[1]["tranche"] == "23 and over",
    str([x["tranche"] for x in nmw]),
)
try:
    srv._parse_nmw("<p>plus de tableau ici</p>")
    verifie("NMW : une page sans tableau rend une liste vide", True)
except Exception as exc:  # noqa: BLE001 - on veut savoir si ca leve
    verifie("NMW : une page sans tableau rend une liste vide", False, str(exc))
verifie("NMW : une page sans tableau ne fabrique rien", srv._parse_nmw("<p>x</p>") == [])

srv._cache_write(
    "salaire_minimum_uk",
    {
        "source": "test",
        "millesime": "2026-04-01",
        "provenance": "tableau HTML de test",
        "lignes": [
            {
                "periode_source": "April 2026",
                "debut": "2026-04-01",
                "tranche": "21 and over",
                "valeur": 12.71,
            }
        ],
    },
)
srv._cache_write(
    "salaire_minimum_EUR",
    {
        "source": "test",
        "flow": srv.MW_FLOW,
        "millesime": "2026-07-31T11:00:00+0200",
        "points": [
            {"geo": "FR", "time": "2026-S2", "valeur": 1867, "statut": ""},
            {"geo": "UK", "time": "2020-S2", "valeur": 1509, "statut": ""},
        ],
    },
)
lignes_sm, meta_sm = srv._serie_salaire("FR,UK", "", "", "EUR")
par_pays = {r["code_pays"]: r for r in lignes_sm}
verifie(
    "le Royaume-Uni vient de gov.uk, pas du fige Eurostat",
    par_pays.get("UK", {}).get("source") == "gov.uk",
    str(par_pays.get("UK")),
)
verifie(
    "il est en GBP par HEURE, et le connecteur ne le convertit pas en mensuel",
    par_pays["UK"]["unite"] == srv.NMW_UNITE and par_pays["UK"]["valeur"] == 12.71,
)
verifie(
    "les Etats membres restent en montant mensuel Eurostat",
    par_pays["FR"]["unite"].endswith("par mois") and par_pays["FR"]["source"] == "eurostat",
)
verifie(
    "la substitution reussie ETEINT l'avertissement de gel",
    not any("plus publie depuis 2020-S2" in n for n in meta_sm.get("notes") or []),
    str(meta_sm.get("notes")),
)
verifie(
    "le melange horaire / mensuel est ANNONCE",
    any("PAR HEURE" in n for n in meta_sm.get("notes") or []),
)
verifie(
    "les deux millesimes restent SEPARES : ils n'ont pas le meme format",
    meta_sm.get("millesime") == "2026-07-31T11:00:00+0200"
    and meta_sm.get("millesime_uk") == "2026-04-01",
    f"{meta_sm.get('millesime')} / {meta_sm.get('millesime_uk')}",
)

# --------------------------------------------------------------------------
bloc("8 quater. L'indice carburant IPCH, la reponse pour la Suisse")

srv._cache_write(
    "inflation_RCH_A_CP0722",
    {
        "source": "test",
        "flow": srv.HICP_FLOW,
        "millesime": "2026-09-01T23:00:00+0200",
        "points": [
            {"geo": "CH", "time": "2026-07", "valeur": 9.0, "statut": ""},
            {"geo": "FR", "time": "2026-07", "valeur": 19.9, "statut": ""},
        ],
    },
)
lignes_carb, meta_carb = srv._serie_inflation("CH,FR", "", "", "annuel", poste="carburants")
verifie(
    "le poste carburants rend la Suisse, que le bulletin ne couvre pas",
    any(r["code_pays"] == "CH" for r in lignes_carb),
    str([r["code_pays"] for r in lignes_carb]),
)
verifie(
    "le poste est porte par chaque ligne",
    all(r.get("poste") == "carburants" for r in lignes_carb),
)
verifie(
    "il est annonce comme un INDICE et pas comme un prix",
    any("INDICE, PAS UN PRIX" in n for n in meta_carb.get("notes") or []),
    str(meta_carb.get("notes")),
)
verifie(
    "le libelle nomme le poste, pour ne pas le confondre avec l'inflation totale",
    "carburants" in meta_carb.get("libelle", ""),
    meta_carb.get("libelle", ""),
)
verifie(
    "le poste total et le poste carburants ont des cles de cache DISTINCTES",
    srv._cache_path("inflation_RCH_A_TOTAL")
    != srv._cache_path("inflation_RCH_A_CP0722"),
)
try:
    srv._serie_inflation("FR", "", "", "annuel", poste="alimentation")
    verifie("un poste inconnu est REFUSE", False)
except srv.ConfigError:
    verifie("un poste inconnu est REFUSE", True)
for entree in ("carburant", "gazole", "CP0722"):
    _, m = srv._serie_inflation("CH", "", "", "annuel", poste=entree)
    verifie(f"'{entree}' en entree est comprise comme le poste carburants",
            m.get("poste") == "carburants")

# --------------------------------------------------------------------------
bloc("8 quinquies. L'aiguillage ne decale pas ses arguments")

# Regression : quand `source` et `poste` ont ete ajoutes, les appels positionnels
# de _serie faisaient atterrir `force` sur eux. L'export forcait alors un
# rapatriement reseau a chaque appel, et une source explicite etait ignoree -
# sans aucune erreur. Le controle porte sur ce qui remonte, pas sur le texte.
lignes_d, _ = srv._serie("gazole", "UK", "", "", "diesel", "ttc", source="europe")
verifie(
    "_serie transmet bien source=europe (et non force)",
    all(r["source"] == "europe" for r in lignes_d) or not lignes_d,
    str({r["source"] for r in lignes_d}),
)
lignes_d, meta_d = srv._serie("inflation", "CH", "", "", mesure="annuel", poste="carburants")
verifie(
    "_serie transmet bien poste=carburants",
    meta_d.get("poste") == "carburants",
    str(meta_d.get("poste")),
)

# --------------------------------------------------------------------------
bloc("9. Ce que le serveur declare au client")

# Un client MCP ne lit pas ce README. Ce qu'il voit d'un outil, c'est sa
# signature et ses annotations - donc c'est la qu'il faut dire la lecture seule,
# pas seulement dans la documentation. Le controle porte sur les outils REELLEMENT
# publies (list_tools), pas sur le texte du fichier : un decorateur mal pose se
# verrait ici et pas dans un grep.
outils = asyncio.run(srv.mcp.list_tools())
par_nom = {o.name: o for o in outils}

verifie("les neuf outils sont publies", len(outils) == 9, f"{len(outils)} publie(s)")
consignes = srv.mcp.instructions or ""
verifie("les instructions de serveur sont posees", len(consignes) > 500, f"{len(consignes)} car.")
verifie(
    "elles portent le garde-fou : le connecteur n'applique aucune clause",
    "N'APPLIQUE AUCUNE CLAUSE" in consignes.upper(),
)
verifie(
    "elles nomment le piege du Royaume-Uni fige",
    "FIGE" in consignes.upper() and "Royaume-Uni" in consignes,
)

for outil in outils:
    annot = outil.annotations
    verifie(f"{outil.name} : annotations presentes", annot is not None)
    if annot is None:
        continue
    verifie(f"{outil.name} : readOnlyHint", annot.readOnlyHint is True)
    verifie(f"{outil.name} : destructiveHint a faux", annot.destructiveHint is False)
    verifie(f"{outil.name} : un titre lisible", bool(annot.title))

# indices_pays lit le lexique embarque : ensemble ferme, reponse reproductible.
verifie(
    "indices_pays est annonce hors monde ouvert",
    par_nom["indices_pays"].annotations.openWorldHint is False,
)
# Les deux outils qui posent un fichier ou rappellent la source ne sont pas
# idempotents : deux appels ne rendent pas le meme etat local.
for nom in ("indices_export_csv", "indices_refresh"):
    verifie(
        f"{nom} n'est pas annonce idempotent",
        par_nom[nom].annotations.idempotentHint is False,
    )

# --------------------------------------------------------------------------
print()
print("=" * 70)
if _ECHECS:
    print(f"{len(_ECHECS)} controle(s) en ECHEC sur {_PASSES + len(_ECHECS)} :")
    for titre in _ECHECS:
        print(f"  - {titre}")
    sys.exit(1)
print(f"Les {_PASSES} controles hors ligne passent.")
print(f"(dossier jetable : {_TMP})")
sys.exit(0)
