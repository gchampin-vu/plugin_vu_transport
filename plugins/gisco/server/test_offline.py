#!/usr/bin/env python3
"""
Controles hors ligne du connecteur gisco.

    python test_offline.py

Aucun appel reseau, aucun cache requis : ces controles portent sur ce qui casse
en silence - la lecture de la geometrie, la normalisation des noms, le nettoyage
des guillemets du jeu pcode, les garde-fous SQL et le catalogue embarque.

Ce qu'ils NE couvrent pas, et qui se verifie en ligne avec `doctor` puis une
synchronisation : la disponibilite des fichiers chez Eurostat et chez l'ONS.
"""

from __future__ import annotations

import json
import math
import os
import pathlib
import re
import sqlite3
import struct
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import geo  # noqa: E402

ECHECS: list[str] = []


def verifie(nom: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  OK   {nom}")
    else:
        print(f"  ECHEC {nom}" + (f" - {detail}" if detail else ""))
        ECHECS.append(nom)


# --------------------------------------------------------------------------
# Geometrie
# --------------------------------------------------------------------------

CARRE = {
    "type": "Polygon",
    "coordinates": [[[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0], [0.0, 0.0]]],
}
CARRE_TROUE = {
    "type": "Polygon",
    "coordinates": [
        [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0], [0.0, 0.0]],
        [[4.0, 4.0], [6.0, 4.0], [6.0, 6.0], [4.0, 6.0], [4.0, 4.0]],
    ],
}


def test_geometrie() -> None:
    print("Geometrie")
    verifie("point dedans", geo.contains(CARRE, 5.0, 5.0))
    verifie("point dehors", not geo.contains(CARRE, 15.0, 5.0))
    # Un trou n'est pas un detail : sans lui, une enclave se rattache a la
    # commune qui l'entoure, et personne ne le voit.
    verifie("point dans un trou = dehors", not geo.contains(CARRE_TROUE, 5.0, 5.0))
    verifie("point hors du trou = dedans", geo.contains(CARRE_TROUE, 1.0, 1.0))

    multi = {"type": "MultiPolygon", "coordinates": [CARRE["coordinates"],
             [[[20.0, 20.0], [30.0, 20.0], [30.0, 30.0], [20.0, 30.0], [20.0, 20.0]]]]}
    verifie("multipolygone, deuxieme partie", geo.contains(multi, 25.0, 25.0))

    verifie("cadre englobant", geo.bbox(CARRE) == (0.0, 0.0, 10.0, 10.0))
    cx, cy = geo.centroid(CARRE)
    verifie("centroide", abs(cx - 5.0) < 1e-9 and abs(cy - 5.0) < 1e-9, f"{cx},{cy}")

    cas = {
        "carre": CARRE,
        "carre troue": CARRE_TROUE,
        "multipolygone": multi,
        "point": {"type": "Point", "coordinates": [1.5, 2.5]},
    }
    for etiquette, g in cas.items():
        retour = geo.wkb_to_geojson(geo.geojson_to_wkb(g))
        verifie(f"aller-retour WKB, {etiquette}", retour == g, json.dumps(retour)[:80])

    # Paris - Lyon, valeur de reference a vol d'oiseau : 392 km environ.
    km = geo.haversine_km(48.8566, 2.3522, 45.7640, 4.8357)
    verifie("distance Paris-Lyon ~392 km", abs(km - 392) < 5, f"{km:.1f} km")
    verifie("distance nulle", geo.haversine_km(48.0, 2.0, 48.0, 2.0) == 0.0)


def test_entete_geopackage() -> None:
    print("En-tete GeoPackage")
    wkb = geo.geojson_to_wkb({"type": "Point", "coordinates": [3.0, 4.0]})
    # En-tete GP : magie, version, drapeaux (petit-boutiste, enveloppe 2D), srs.
    entete = b"GP" + bytes([0, 0b00000011]) + struct.pack("<i", 4326)
    # GeoPackage ordonne minx, MAXX, miny, maxy. Des valeurs toutes distinctes,
    # sinon l'inversion qu'on cherche a detecter passe inapercue.
    entete += struct.pack("<4d", 1.0, 2.0, 3.0, 4.0)  # minx, maxx, miny, maxy
    srs, env, corps = geo.strip_gpkg_header(entete + wkb)
    verifie("srs lu", srs == 4326, str(srs))
    verifie("enveloppe reordonnee en minx,miny,maxx,maxy", env == (1.0, 3.0, 2.0, 4.0), str(env))
    verifie("corps = WKB nu", corps == wkb)
    verifie("WKB nu accepte tel quel", geo.strip_gpkg_header(wkb)[2] == wkb)
    g = geo.wkb_to_geojson(entete + wkb)
    verifie("decodage a travers l'en-tete", g == {"type": "Point", "coordinates": [3.0, 4.0]})


# --------------------------------------------------------------------------
# Serveur : ce qui ne demande ni reseau ni cache
# --------------------------------------------------------------------------

def test_serveur() -> None:
    print("Serveur")
    try:
        import server as S
    except ImportError as exc:
        verifie(
            "import du serveur",
            False,
            f"{exc} - relance avec l'interpreteur de l'environnement du "
            "connecteur, ou installe mcp et httpx",
        )
        return

    verifie("accents ignores", S._norm("Dibër") == "diber", S._norm("Dibër"))
    verifie("casse ignoree", S._norm("MÜNSTER") == "munster", S._norm("MÜNSTER"))

    # Le piege du jeu pcode : NUTS3 arrive entoure de guillemets litteraux.
    verifie("guillemets retires", S._unquote('"NL366"') == "NL366")
    verifie("valeur normale intacte", S._unquote("NL366") == "NL366")
    verifie("non-chaine intacte", S._unquote(3) == 3)

    # Les deux codes qui ne suivent pas l'ISO.
    verifie("GR devient EL", S._clean_country("gr") == "EL")
    verifie("GB devient UK", S._clean_country("GB") == "UK")
    verifie("les deux graphies du Royaume-Uni", set(S._country_variants("GB")) == {"UK", "GB"})
    verifie("pays vide = pas de filtre", S._country_variants("") == ())

    # Le cache est en lecture seule, et ca ne doit pas dependre de la vigilance.
    for mauvais in (
        "DELETE FROM lau",
        "SELECT 1; DROP TABLE lau",
        "PRAGMA table_info(lau)",
        "ATTACH DATABASE 'x' AS y",
        "",
    ):
        try:
            S._guard_sql(mauvais)
            verifie(f"SQL refuse : {mauvais[:28]!r}", False, "accepte a tort")
        except S.GiscoError:
            verifie(f"SQL refuse : {mauvais[:28]!r}", True)
    try:
        propre = S._guard_sql("  SELECT * FROM nuts;  ")
        verifie("SELECT accepte et nettoye", propre == "SELECT * FROM nuts", propre)
    except S.GiscoError as exc:
        verifie("SELECT accepte", False, str(exc))

    # Le catalogue est embarque : il doit se lire sans reseau.
    cat = S._catalogue()
    verifie("catalogue lisible", isinstance(cat.get("couches"), dict))
    for nom in ("nuts", "lau", "pcode", "urau", "countries"):
        spec = cat["couches"].get(nom) or {}
        verifie(f"couche {nom} decrite", bool(spec.get("gabarit") and spec.get("colonnes")))
        verifie(
            f"couche {nom} rangee dans une table connue",
            spec.get("table") in S.TABLES,
            str(spec.get("table")),
        )
    verifie("alias francais", S._layer_spec("communes")["nom"] == "lau")
    verifie("alias codes postaux", S._layer_spec("codes postaux")["nom"] == "pcode")
    try:
        S._layer_spec("chose")
        verifie("couche inconnue refusee", False, "acceptee a tort")
    except S.GiscoError:
        verifie("couche inconnue refusee", True)

    # Le catalogue ne fige plus les millesimes, mais il garde un filet pour le
    # jour ou Eurostat ne repond pas : sans lui, une panne reseau ne rend plus
    # aucun millesime et la couche devient introuvable.
    for nom in ("nuts", "lau", "pcode", "urau", "countries"):
        spec = cat["couches"].get(nom) or {}
        verifie(
            f"couche {nom} garde un filet annees_connues",
            bool(spec.get("annees_connues")),
        )

    # Le gabarit et le motif doivent se repondre. Si le motif ne reconnait pas
    # le nom que le gabarit produit, la confrontation a la liste publiee rejette
    # un fichier qui existe pourtant - et le message accuse Eurostat a tort.
    for nom, spec in (cat.get("couches") or {}).items():
        for cle_gabarit, cle_motif, niveau in (
            ("gabarit", "motif_fichier", ""),
            ("gabarit_niveau", "motif_fichier_niveau", "3"),
        ):
            gabarit, motif = spec.get(cle_gabarit), spec.get(cle_motif)
            if not gabarit or not motif:
                continue
            resolution = (spec.get("resolution_preferee") or ["20M"])[0]
            exemple = (
                gabarit.replace("{annee}", "2024")
                .replace("{resolution}", resolution)
                .replace("{niveau}", niveau)
            )
            m = re.match(motif, exemple)
            verifie(f"{nom} : {cle_motif} reconnait son gabarit", bool(m), exemple)
            if m:
                verifie(
                    f"{nom} : {cle_motif} rend l'annee",
                    m.groupdict().get("annee") == "2024",
                    str(m.groupdict()),
                )

    # Les colonnes sont retrouvees dans les colonnes REELLES du fichier, en
    # trois passes. C'est la troisieme - le motif - qui compte : le millesime
    # d'une colonne ne suit pas celui du jeu, et le calculer revient a parier.
    cols, manquantes = S._resolve_columns(
        S._layer_spec("lau"),
        "2023",
        ["GISCO_ID", "CNTR_CODE", "LAU_NAME", "LAU_ID", "POP_2023",
         "POP_DENS_2023", "AREA_KM2", "geom"],
    )
    verifie("POP_{annee} resolu", cols.get("pop") == "POP_2023", str(cols.get("pop")))
    verifie("aucune colonne lau manquante", not manquantes, ", ".join(manquantes))

    # Le fichier pcode 2025 porte NUTS3_2024 et GISCO_2021 : deux colonnes dont
    # l'annee ne se deduit PAS de celle du jeu.
    cols, manquantes = S._resolve_columns(
        S._layer_spec("pcode"),
        "2025",
        ["POSTCODE", "CNTR_ID", "LAU_NAME", "GISCO_2021", "NUTS3_2024",
         "DGURBA", "FUA_ID", "CITY_ID", "PC_CNTR"],
    )
    verifie("NUTS3 retrouve par motif", cols.get("nuts3") == "NUTS3_2024", str(cols.get("nuts3")))
    verifie(
        "GISCO_<annee> retrouve par motif",
        cols.get("lau_id") == "GISCO_2021",
        str(cols.get("lau_id")),
    )
    verifie("aucune colonne pcode manquante", not manquantes, ", ".join(manquantes))

    # Deux millesimes de la meme colonne dans un fichier : le plus recent gagne.
    cols, _ = S._resolve_columns(
        S._layer_spec("lau"), "2024", ["GISCO_ID", "POP_2020", "POP_2024"]
    )
    verifie(
        "colonne au millesime le plus recent retenue",
        cols.get("pop") == "POP_2024",
        str(cols.get("pop")),
    )

    # Une colonne absente doit etre DITE, pas chargee vide en silence : c'est
    # ce qui est arrive a LAU_ID, disparu du millesime 2024.
    cols, manquantes = S._resolve_columns(
        S._layer_spec("lau"), "2024", ["GISCO_ID", "CNTR_CODE", "LAU_NAME", "AREA_KM2"]
    )
    verifie(
        "colonne absente signalee",
        any(m.startswith("lau_id") for m in manquantes),
        ", ".join(manquantes) or "(rien signale)",
    )

    verifie(
        "URL construite depuis le nom publie",
        S._gisco_url("nuts", "NUTS_RG_20M_2024_4326_LEVL_3.gpkg").endswith(
            "/distribution/v2/nuts/gpkg/NUTS_RG_20M_2024_4326_LEVL_3.gpkg"
        ),
        S._gisco_url("nuts", "NUTS_RG_20M_2024_4326_LEVL_3.gpkg"),
    )

    # Le choix du fichier est confronte a ce qu'Eurostat publie reellement. On
    # simule ce releve pour rester hors ligne : ce qui est teste ici, c'est la
    # decision, pas la disponibilite du service.
    releve_reel = S._releve
    S._releve = lambda jeu, rafraichir=False: {
        "editions": {"2024": "nuts-2024-files.json", "2021": "nuts-2021-files.json"},
        "fichiers": {
            "2024": [
                "NUTS_RG_20M_2024_4326.gpkg",
                "NUTS_RG_60M_2024_4326.gpkg",
                "NUTS_RG_20M_2024_4326_LEVL_3.gpkg",
            ]
        },
        "releve_le": "2026-01-01T00:00:00",
        "age_jours": 0.0,
        "frais": True,
        "origine": "simule",
    }
    try:
        spec = S._layer_spec("nuts")
        fichier, annee, resolution, _ = S._choisir_fichier(spec, "", "", "")
        verifie("dernier millesime publie choisi", annee == "2024", annee)
        verifie("resolution preferee publiee retenue", resolution == "20M", resolution)
        verifie(
            "fichier choisi parmi les publies",
            fichier == "NUTS_RG_20M_2024_4326.gpkg",
            fichier,
        )
        fichier, _, _, _ = S._choisir_fichier(spec, "", "", "3")
        verifie(
            "fichier de niveau choisi",
            fichier == "NUTS_RG_20M_2024_4326_LEVL_3.gpkg",
            fichier,
        )
        for libelle, args in (
            ("millesime non publie refuse", ("1999", "", "")),
            ("resolution non publiee refusee", ("2024", "01M", "")),
            ("millesime illisible refuse", ("20 24", "", "")),
        ):
            try:
                S._choisir_fichier(spec, *args)
                verifie(libelle, False, "accepte a tort")
            except S.GiscoError:
                verifie(libelle, True)
    finally:
        S._releve = releve_reel

    # Le versant britannique n'a pas de decouverte : ses couches sont decrites a
    # la main, et une description incomplete se voit ici, pas en production.
    for nom, spec in (cat.get("couches_uk") or {}).items():
        if nom.startswith("_") or nom == "racine":
            continue
        verifie(
            f"couche UK {nom} decrite",
            bool(spec.get("chemin") and spec.get("champs")),
        )
        verifie(
            f"couche UK {nom} rangee dans une table connue",
            spec.get("table") in S.TABLES or spec.get("table") is None,
            str(spec.get("table")),
        )
        verifie(f"couche UK {nom} lisible par _uk_spec", S._uk_spec(nom)["nom"] == nom)

    # La racine locale ne doit jamais tomber dans un dossier synchronise.
    racine = str(S._local_root()).lower()
    verifie(
        "racine locale hors dossier synchronise",
        not any(m in racine for m in S.SYNCED_MARKERS),
        racine,
    )

    # La reprise de schema, sur un cache d'une version precedente. C'est le
    # defaut releve le 2026-09-02 : la colonne 'millesime' n'etait ajoutee que
    # sur le chemin d'ECRITURE, donc gisco_couches - la premiere chose qu'on
    # lance - echouait sur « no such column » jusqu'a la synchronisation
    # suivante. Et une fois ajoutee vide, elle faisait passer les cinq couches
    # pour EN RETARD, ce qui proposait 270 Mo de retelechargement inutile.
    with tempfile.TemporaryDirectory() as tmp:
        faux_cache = pathlib.Path(tmp) / "vieux.sqlite"
        conn = sqlite3.connect(faux_cache)
        conn.executescript(
            """
            CREATE TABLE couches (
                couche TEXT PRIMARY KEY, cible TEXT, edition TEXT, url TEXT,
                telecharge_le TEXT, lignes INTEGER, octets INTEGER, remarque TEXT
            );
            INSERT INTO couches (couche, cible, edition, lignes) VALUES
                ('nuts',    'nuts', '2024_20M',                1798),
                ('pcode',   'pcode', '2025',                   830032),
                ('uk_lad',  'lau',  'LAD_MAY_2025_UK_BGC_V2',  361);
            """
        )
        conn.commit()
        conn.close()

        ancien = os.environ.get("GISCO_CACHE_DB")
        os.environ["GISCO_CACHE_DB"] = str(faux_cache)
        try:
            verifie("cache d'une version precedente reconnu", S._reprise_en_attente(faux_cache))
            # Une LECTURE doit suffire a rattraper le schema.
            conn = S._open_db(readonly=True)
            try:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(couches)")}
                verifie("colonne millesime ajoutee par une lecture", "millesime" in cols)
                etat = dict(conn.execute("SELECT couche, millesime FROM couches"))
            finally:
                conn.close()
            verifie(
                "millesime GISCO repris depuis l'edition",
                (etat.get("nuts"), etat.get("pcode")) == ("2024", "2025"),
                str(etat),
            )
            verifie(
                "millesime britannique laisse vide",
                not etat.get("uk_lad"),
                str(etat.get("uk_lad")),
            )
            verifie("plus rien en attente apres reprise", not S._reprise_en_attente(faux_cache))
        finally:
            if ancien is None:
                os.environ.pop("GISCO_CACHE_DB", None)
            else:
                os.environ["GISCO_CACHE_DB"] = ancien

    # Le rendu tronque doit le DIRE : une liste coupee en silence se lit comme
    # une liste complete.
    rendu = S._render([(f"ligne {i}", "x" * 40) for i in range(500)], ["a", "b"], max_chars=400)
    verifie("rendu tronque signale", "TRONQUE" in rendu)


def _relance_dans_venv() -> int | None:
    """Rejoue ce fichier avec l'interpreteur de l'environnement du connecteur.

    Le serveur vit dans un venv monte par bootstrap.py, pas dans le Python du
    poste : lance avec `python test_offline.py`, la moitie des controles
    echouait sur un import manquant, ce qui se lit comme une regression du
    connecteur alors que c'est un interpreteur qui n'a rien a voir. On rejoue
    donc le fichier la ou les dependances sont, et on le DIT.

    Rend None quand il n'y a rien a relancer : c'est alors a l'appelant de
    continuer dans l'interpreteur courant.
    """
    try:
        import httpx  # noqa: F401
        import mcp  # noqa: F401
        return None
    except ImportError:
        pass
    try:
        import bootstrap
    except ImportError:
        return None
    for venv in bootstrap.candidate_venvs():
        python = bootstrap.venv_python(venv)
        if python.is_file() and bootstrap.has_dependencies(python):
            print(f"Dependances absentes ici : relance dans {python}")
            print()
            done = subprocess.run([str(python), str(pathlib.Path(__file__).resolve())])
            return done.returncode
    print(
        "Dependances (mcp, httpx) absentes, et aucun environnement du "
        "connecteur trouve.\nLance d'abord `python bootstrap.py doctor` : il "
        "le monte. Les controles de geometrie, eux, passent sans."
    )
    print()
    return None


def main() -> int:
    code = _relance_dans_venv()
    if code is not None:
        return code
    print("Controles hors ligne du connecteur gisco")
    print()
    test_geometrie()
    print()
    test_entete_geopackage()
    print()
    test_serveur()
    print()
    if ECHECS:
        print(f"{len(ECHECS)} echec(s) : " + ", ".join(ECHECS))
        return 1
    print("Tout est vert.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
