#!/usr/bin/env python3
"""
Tests hors ligne du connecteur tva. Aucun appel a api.vatcomply.com.

    python test_offline.py

Ce qui est verifie ici, c'est ce qui doit tenir MEME sans reseau, et surtout ce
qui protege contre un chiffre faux : la resolution des pays, le refus de rendre
un taux pour un pays non couvert, la presence de la date de releve dans chaque
rendu, et le comportement quand la source ne repond pas.

Un garde-fou qui n'est pas teste n'est pas un garde-fou : c'est une intention.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile

# Les tests ne doivent dependre ni de la config du poste, ni de celle de
# l'equipe : on neutralise AVANT d'importer le serveur. Le chemin explicite est
# exclusif cote serveur, c'est ce qui rend cette neutralisation effective.
_ABSENT = str(pathlib.Path(__file__).with_name("__absent__"))
os.environ["TVA_SHARED_ENV"] = _ABSENT
os.environ["TVA_ENV_FILE"] = _ABSENT
os.environ.pop("TVA_PAYS_VU", None)

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="tva-mcp-test-"))
os.environ["TVA_CACHE_FILE"] = str(_TMP / "vat_rates.json")
os.environ["TVA_EXPORT_DIR"] = str(_TMP / "exports")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import server  # noqa: E402

PASSED = 0
FAILED: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(f"{label}{' - ' + detail if detail else ''}")


# --------------------------------------------------------------------------
# Un faux releve, qui ressemble a la vraie source
# --------------------------------------------------------------------------

FAUX = [
    {
        "country_code": "FR",
        "country_name": "France",
        "standard_rate": 20.0,
        "reduced_rates": [5.5, 10.0],
        "super_reduced_rate": 2.1,
        "parking_rate": None,
        "currency": "EUR",
        "member_state": True,
        "rate_comments": {"5.5": ["Livres", "Alimentation"]},
        "rate_categories": {"foodstuffs": [5.5], "transport_passengers": [10.0]},
    },
    {
        "country_code": "EL",
        "country_name": "Greece",
        "standard_rate": 24.0,
        "reduced_rates": [6.0, 13.0],
        "super_reduced_rate": None,
        "parking_rate": None,
        "currency": "EUR",
        "member_state": True,
        "rate_comments": {},
        "rate_categories": {"foodstuffs": [13.0]},
    },
    {
        "country_code": "SE",
        "country_name": "Sweden",
        "standard_rate": 25.0,
        "reduced_rates": [6.0, 12.0],
        "super_reduced_rate": None,
        "parking_rate": None,
        "currency": "SEK",
        "member_state": True,
        "rate_comments": {},
        "rate_categories": {},
    },
]


def pose_cache(rows: list[dict], releve: str) -> None:
    """Ecrit un releve dans le cache, et vide la memoire du process."""
    path = pathlib.Path(os.environ["TVA_CACHE_FILE"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"releve": releve, "source": "test", "rows": rows}),
        encoding="utf-8",
    )
    server._MEMO = None


def coupe_le_reseau() -> None:
    """Remplace l'appel reel par une panne. Aucun test ne sort de la machine."""

    def boum() -> list[dict]:
        raise server.TvaError("reseau coupe (test hors ligne)")

    server._fetch_live = boum  # type: ignore[assignment]


coupe_le_reseau()

# --------------------------------------------------------------------------
# 1. Le referentiel local
# --------------------------------------------------------------------------

ref = server._ref()
check("referentiel : 27 codes", len(ref["codes"]) == 27, str(len(ref["codes"])))
check("referentiel : la Grece est EL", "EL" in ref["codes"] and "GR" not in ref["codes"])
check("referentiel : CH declare non couvert", "CH" in ref["hors_perimetre"])
check("referentiel : NO declare non couvert", "NO" in ref["hors_perimetre"])
check("referentiel : GB declare non couvert", "GB" in ref["hors_perimetre"])
check("referentiel : aucun taux dans le referentiel local",
      "rate" not in json.dumps(ref["codes"]).lower())

# --------------------------------------------------------------------------
# 2. La normalisation et les alias
# --------------------------------------------------------------------------

check("norm : accents", server._norm("Grèce") == "GRECE")
check("norm : casse et espaces", server._norm(" pays-bas ") == "PAYS_BAS")
check("norm : None", server._norm(None) == "")

pose_cache(FAUX, "2026-09-01T08:00:00+00:00")
blob = server._payload()
connus = server._index(blob)
check("index : 3 pays", len(connus) == 3, str(sorted(connus)))

codes, alertes = server._resolve("FR", connus)
check("resolve : FR", codes == ["FR"] and not alertes)

codes, alertes = server._resolve("GR", connus)
check("resolve : GR traduit en EL", codes == ["EL"], str(codes))

codes, alertes = server._resolve("Grèce", connus)
check("resolve : nom accentue", codes == ["EL"], str(codes))

codes, alertes = server._resolve("France, Suède", connus)
check("resolve : deux noms francais", codes == ["FR", "SE"], str(codes))

codes, alertes = server._resolve("UE", connus)
check("resolve : UE = tous les pays connus", len(codes) == 3, str(codes))

codes, alertes = server._resolve("", connus)
check("resolve : vide = tous, sans perimetre d'equipe", len(codes) == 3, str(codes))

# --------------------------------------------------------------------------
# 3. Le garde-fou central : ce qui n'est pas couvert est NOMME
# --------------------------------------------------------------------------

codes, alertes = server._resolve("CH", connus)
check("resolve : CH ne rend aucun code", codes == [])
check("resolve : CH rend une alerte", len(alertes) == 1)
check("resolve : l'alerte CH nomme la Suisse", "Suisse" in alertes[0], alertes[0] if alertes else "")
check("resolve : l'alerte CH dit ou chercher",
      "chercher" in alertes[0].lower(), alertes[0] if alertes else "")

codes, alertes = server._resolve("FR,CH,NO", connus)
check("resolve : le pays couvert passe, les autres alertent",
      codes == ["FR"] and len(alertes) == 2, f"{codes} / {len(alertes)}")

codes, alertes = server._resolve("CANARIES", connus)
check("resolve : territoire reconnu comme regime particulier",
      codes == [] and len(alertes) == 1 and "IGIC" in alertes[0],
      alertes[0] if alertes else "")

codes, alertes = server._resolve("Groland", connus)
check("resolve : code inconnu alerte", codes == [] and len(alertes) == 1)
check("resolve : l'alerte inconnu rappelle le piege EL",
      "EL" in alertes[0], alertes[0] if alertes else "")

# --------------------------------------------------------------------------
# 4. Le rendu porte TOUJOURS la date du releve
# --------------------------------------------------------------------------

sortie = server.tva_taux("FR")
check("taux : la date de releve est en tete", "2026-09-01" in sortie.splitlines()[1], sortie.splitlines()[1])
check("taux : la ligne France est exacte",
      "FR;France;20;5.5 | 10;2.1;;EUR" in sortie,
      [l for l in sortie.splitlines() if l.startswith("FR;")])
check("taux : la mention de source est presente", "administration fiscale" in sortie)
check("taux : CSV point-virgule", "code;pays;taux_normal" in sortie)

sortie = server.tva_taux("FR,CH")
check("taux : le non-couvert remonte dans le rendu", "NON COUVERT" in sortie)
check("taux : et il ne rend pas de ligne CH", "\nCH;" not in sortie)

sortie = server.tva_taux("FR,EL", categorie="transport_passengers")
check("taux : colonne de categorie ajoutee", "taux_transport_passengers" in sortie)
check("taux : la categorie absente d'un pays est signalee",
      "pas de taux reduit declare" in sortie, "note manquante")

sortie = server.tva_taux("CH")
check("taux : aucun pays couvert -> aucune ligne", "(aucune ligne)" in sortie)

# --------------------------------------------------------------------------
# 5. Un cache perime ne se rend jamais en silence
# --------------------------------------------------------------------------

pose_cache(FAUX, "2025-01-01T08:00:00+00:00")
sortie = server.tva_taux("FR")
check("perime : l'avertissement de source injoignable est affiche",
      "ATTENTION" in sortie, sortie.splitlines()[1])
check("perime : le rendu dit que ce ne sont pas forcement les taux du jour",
      "PAS forcement les taux du jour" in sortie)

pose_cache(FAUX, "2026-09-01T08:00:00+00:00")

# --------------------------------------------------------------------------
# 6. Pas de cache + source muette = pas de reponse inventee
# --------------------------------------------------------------------------

path = pathlib.Path(os.environ["TVA_CACHE_FILE"])
path.unlink(missing_ok=True)
pathlib.Path(str(path).replace(".json", ".precedent.json")).unlink(missing_ok=True)
server._MEMO = None
sortie = server.tva_taux("FR")
check("sans cache et sans reseau : erreur explicite", sortie.startswith("ERREUR"), sortie[:120])
check("sans cache et sans reseau : aucun tableau rendu",
      "taux_normal" not in sortie, sortie[:200])
check("sans cache : le message dit qu'il n'affiche pas ce qu'il n'a pas lu",
      "n'a pas lu" in sortie)

pose_cache(FAUX, "2026-09-01T08:00:00+00:00")

# --------------------------------------------------------------------------
# 7. Le detail d'un pays
# --------------------------------------------------------------------------

sortie = server.tva_detail("FR")
check("detail : les categories sont listees", "transport_passengers : 10" in sortie, "")
check("detail : les commentaires officiels sont rendus", "Livres" in sortie)
check("detail : les territoires du pays sont signales", "DOM" in sortie and "CORSE" in sortie)
check("detail : et l'avertissement territorial est explicite",
      "ne repond pas" in sortie)

sortie = server.tva_detail("CH")
check("detail : un pays non couvert ne fabrique pas de fiche",
      "NON COUVERT" in sortie and "Aucun pays couvert" in sortie)

sortie = server.tva_detail("SE")
check("detail : un pays sans categorie le dit", "n'en declare aucune" in sortie)

# --------------------------------------------------------------------------
# 8. Les categories
# --------------------------------------------------------------------------

sortie = server.tva_categories()
check("categories : foodstuffs vu sur 2 pays", "foodstuffs (2)" in sortie, "")
check("categories : l'absence n'est pas une exoneration",
      "ne veut pas dire" in sortie)

# --------------------------------------------------------------------------
# 9. Le perimetre d'equipe
# --------------------------------------------------------------------------

sortie = server.tva_pays()
check("pays : dit que le perimetre d'equipe n'est pas renseigne",
      "non renseigne" in sortie)
check("pays : signale EL comme piege", "PAS le code ISO GR" in sortie)
check("pays : liste les non couverts", "Royaume-Uni" in sortie)

codes, alertes = server._resolve("VU", connus)
check("resolve : VU sans perimetre alerte et retombe sur tous",
      len(codes) == 3 and len(alertes) == 1, f"{codes} / {alertes}")

os.environ["TVA_PAYS_VU"] = "FR, Suède, CH"
server._SHARED_CACHE = None
codes, alertes = server._resolve("VU", connus)
check("resolve : VU respecte le perimetre d'equipe", codes == ["FR", "SE"], str(codes))
check("resolve : un pays non couvert dans le perimetre alerte quand meme",
      any("Suisse" in a for a in alertes), str(alertes))
codes, _ = server._resolve("", connus)
check("resolve : vide suit le perimetre d'equipe", codes == ["FR", "SE"], str(codes))
del os.environ["TVA_PAYS_VU"]
server._SHARED_CACHE = None

# --------------------------------------------------------------------------
# 10. Les changements entre deux releves
# --------------------------------------------------------------------------

sortie = server.tva_changements()
check("changements : un seul releve ne vaut pas « aucun changement »",
      "Aucune comparaison possible" in sortie or "aucune comparaison" in sortie.lower())

precedent = pathlib.Path(str(path).replace(".json", ".precedent.json"))
precedent.write_text(
    json.dumps(
        {
            "releve": "2026-08-01T08:00:00+00:00",
            "rows": [
                {**FAUX[0], "standard_rate": 19.6},
                {**FAUX[1], "currency": "GRD"},
            ],
        }
    ),
    encoding="utf-8",
)
sortie = server.tva_changements()
check("changements : la hausse du taux normal est vue", "19.6" in sortie and "20" in sortie)
check("changements : le changement de devise est vu", "GRD" in sortie)
check("changements : le pays apparu est vu", "SE" in sortie and "absent" in sortie)
check("changements : rappelle qu'un ecart n'est pas une loi",
      "correction de la source" in sortie)

# --------------------------------------------------------------------------
# 11. L'export CSV
# --------------------------------------------------------------------------

sortie = server.tva_export_csv("FR,EL", filename="controle_pays")
fichier = pathlib.Path(os.environ["TVA_EXPORT_DIR"]) / "controle_pays.csv"
check("export : le fichier est ecrit", fichier.is_file(), sortie[:200])
check("export : le chemin est rendu", str(fichier) in sortie)
brut = fichier.read_bytes()
check("export : BOM UTF-8 present", brut.startswith(b"\xef\xbb\xbf"))
check("export : pas de CRLF ajoute par la plateforme", b"\r\n" not in brut)
texte = brut.decode("utf-8-sig")
check("export : separateur point-virgule", texte.splitlines()[0].count(";") >= 6)
check("export : colonne releve presente", "releve" in texte.splitlines()[0])
check("export : la date de releve est sur chaque ligne",
      all("2026-09-01" in l for l in texte.splitlines()[1:]))

sortie = server.tva_export_csv("FR", forme="categories", filename="controle_cat")
fichier = pathlib.Path(os.environ["TVA_EXPORT_DIR"]) / "controle_cat.csv"
texte = fichier.read_text(encoding="utf-8-sig")
check("export categories : une ligne par categorie",
      len(texte.splitlines()) == 3, str(len(texte.splitlines())))
check("export categories : la categorie est en colonne", "categorie" in texte.splitlines()[0])

sortie = server.tva_export_csv("FR,CH", filename="controle_alerte")
check("export : le non-couvert est signale comme ABSENT du fichier",
      "ABSENT du fichier" in sortie)

sortie = server.tva_export_csv("FR", forme="n_importe_quoi")
check("export : forme inconnue refusee", sortie.startswith("ERREUR"), sortie[:120])
check("export : et les formes permises sont nommees", "categories" in sortie)

# --------------------------------------------------------------------------
# 12. Portabilite Mac / Windows
# --------------------------------------------------------------------------

source = pathlib.Path(server.__file__).read_text(encoding="utf-8")
boot = (pathlib.Path(server.__file__).with_name("bootstrap.py")).read_text(encoding="utf-8")
check("portabilite : aucun chemin absolu Windows en dur",
      "C:\\" not in source and "C:\\" not in boot)
check("portabilite : %LOCALAPPDATA% jamais lu comme chemin",
      'environ.get("LOCALAPPDATA")' not in source
      and 'environ.get("LOCALAPPDATA")' not in boot)
check("portabilite : racine locale sous le profil utilisateur",
      'pathlib.Path.home() / ".tva-mcp"' in source)
check("portabilite : la meme racine dans le bootstrap",
      'pathlib.Path.home() / ".tva-mcp"' in boot)
check("portabilite : os.name teste dans une seule fonction",
      boot.count('os.name == "nt"') == 1)
check("portabilite : sys.executable, jamais « python » en dur",
      "sys.executable" in boot and '"python3"' not in boot)
check("portabilite : subprocess appele avec une liste",
      "os.system" not in boot and "shell=True" not in boot)
check("portabilite : encodage explicite a l'ecriture du CSV",
      'encoding="utf-8-sig", newline=""' in source)
check("portabilite : rien sur stdout dans le bootstrap",
      boot.count("file=sys.stderr") >= 1 and "print(f\"[tva-mcp]" in boot)

# --------------------------------------------------------------------------
# 13. Lecture seule
# --------------------------------------------------------------------------

check("lecture seule : un seul chemin d'API appele", source.count("RATES_PATH") <= 6)
check("lecture seule : aucun POST / PUT / DELETE",
      "httpx.post" not in source and "httpx.put" not in source
      and "httpx.delete" not in source)
check("lecture seule : readOnlyHint pose sur les annotations",
      "readOnlyHint=True" in source)

outils = [n for n in dir(server) if n.startswith("tva_")]
check("outils : les huit sont exposes", len(outils) == 8, str(sorted(outils)))
for nom in outils:
    fn = getattr(server, nom)
    check(
        f"outils : {nom} garde sa signature (functools.wraps)",
        nom == "tva_doctor" or hasattr(fn, "__wrapped__"),
        "le decorateur _guard masquerait les parametres",
    )
    check(f"outils : {nom} est documente", bool((fn.__doc__ or "").strip()))

# --------------------------------------------------------------------------
# Verdict
# --------------------------------------------------------------------------

print(f"\n{PASSED} controle(s) passe(s), {len(FAILED)} echec(s).")
if FAILED:
    print("\nEchecs :")
    for line in FAILED:
        print(f"  - {line}")
sys.exit(1 if FAILED else 0)
