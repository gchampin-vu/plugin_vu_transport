#!/usr/bin/env python3
"""
Tests hors ligne du connecteur tva. Aucun appel a TEDB ni a vatnode.

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
        "cn_codes": {"foodstuffs": ["0401", "0402"]},
        "exemptions": ["POSTAGE"],
        "effective_on": "2026-07-01",
        "source": "TEDB",
        "provenance": "officielle",
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
        "cn_codes": {},
        "exemptions": [],
        "effective_on": "2025-01-01",
        "source": "TEDB",
        "provenance": "officielle",
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
        "cn_codes": {},
        "exemptions": [],
        "effective_on": "2026-07-01",
        "source": "TEDB",
        "provenance": "officielle",
    },
    {
        # La juridiction tenue a la main : c'est elle qui doit declencher la
        # reserve de provenance dans chaque rendu qui la contient.
        "country_code": "CH",
        "country_name": "Suisse",
        "standard_rate": 8.1,
        "reduced_rates": [2.6, 3.8],
        "super_reduced_rate": None,
        "parking_rate": None,
        "currency": "CHF",
        "member_state": False,
        "rate_comments": {},
        "rate_categories": {},
        "cn_codes": {},
        "exemptions": [],
        "effective_on": "",
        "source": "vatnode",
        "provenance": "tenue a la main",
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

    def boum() -> tuple[list[dict], dict, list[str]]:
        raise server.TvaError("reseau coupe (test hors ligne)")

    server._fetch_live = boum  # type: ignore[assignment]


coupe_le_reseau()

# --------------------------------------------------------------------------
# 1. Le referentiel local
# --------------------------------------------------------------------------

ref = server._ref()
MEMBRES = set(ref["etats_membres"])
check("referentiel : 45 juridictions", len(ref["codes"]) == 45, str(len(ref["codes"])))
check("referentiel : 27 Etats membres", len(MEMBRES) == 27, str(len(MEMBRES)))
check("referentiel : la Grece est EL", "EL" in ref["codes"] and "GR" not in ref["codes"])
check("referentiel : XI existe et n'est PAS un Etat membre",
      "XI" in ref["codes"] and "XI" not in MEMBRES)
check("referentiel : CH, NO, GB sont desormais COUVERTS",
      all(c in ref["codes"] for c in ("CH", "NO", "GB")))
check("referentiel : CH, NO, GB ne sont pas Etats membres",
      not any(c in MEMBRES for c in ("CH", "NO", "GB")))
for _c, _a in (("CH", "contributions"), ("NO", "Skatteetaten"), ("GB", "HMRC")):
    check(f"referentiel : {_c} porte son administration",
          _a.lower() in str(ref["administrations"][_c]["administration"]).lower())
check("referentiel : toute juridiction hors UE a une administration",
      all(c in ref["administrations"]
          for c in ref["codes"] if c not in MEMBRES))
check("referentiel : aucun alias ne pointe vers un code inconnu",
      all(v in ref["codes"] for v in ref["alias"].values()
          if not v.startswith(("@", "*"))))
check("referentiel : les territoires restent non couverts",
      len(ref["territoires"]) >= 10)
check("referentiel : aucun taux dans le referentiel local",
      "rate" not in json.dumps(ref["codes"]).lower())
# Le referentiel ne doit porter AUCUN chiffre de taux : le jour ou il en porte
# un, il devient une source concurrente qui vieillit sans que personne ne le
# voie.
import re as _re
check("referentiel : aucun pourcentage grave dans le referentiel",
      not _re.search(r"\d{1,2}[.,]\d\s*%", json.dumps(ref, ensure_ascii=False)))

# --------------------------------------------------------------------------
# 2. La normalisation et les alias
# --------------------------------------------------------------------------

check("norm : accents", server._norm("Grèce") == "GRECE")
check("norm : casse et espaces", server._norm(" pays-bas ") == "PAYS_BAS")
check("norm : None", server._norm(None) == "")

pose_cache(FAUX, "2026-09-01T08:00:00+00:00")
blob = server._payload()
connus = server._index(blob)
check("index : 4 juridictions", len(connus) == 4, str(sorted(connus)))

codes, alertes = server._resolve("FR", connus)
check("resolve : FR", codes == ["FR"] and not alertes)

codes, alertes = server._resolve("GR", connus)
check("resolve : GR traduit en EL", codes == ["EL"], str(codes))

codes, alertes = server._resolve("Grèce", connus)
check("resolve : nom accentue", codes == ["EL"], str(codes))

codes, alertes = server._resolve("France, Suède", connus)
check("resolve : deux noms francais", codes == ["FR", "SE"], str(codes))

# « UE » ne veut PAS dire « tout ce que le connecteur sait ». C'est le
# garde-fou qui empeche la Suisse d'apparaitre dans une reponse sur les Etats
# membres.
codes, alertes = server._resolve("UE", connus)
check("resolve : UE = les Etats membres SEULEMENT, pas la Suisse",
      codes == ["EL", "FR", "SE"], str(codes))
check("resolve : UE ne declenche aucune reserve de provenance",
      not any("PROVENANCE" in a for a in alertes), str(alertes))

codes, alertes = server._resolve("Europe", connus)
check("resolve : Europe = toutes les juridictions, Suisse comprise",
      len(codes) == 4 and "CH" in codes, str(codes))

codes, alertes = server._resolve("", connus)
check("resolve : vide = tout le perimetre couvert", len(codes) == 4, str(codes))
check("resolve : et vide porte QUAND MEME la reserve de provenance",
      any("PROVENANCE" in a for a in alertes), str(alertes))

# --------------------------------------------------------------------------
# 3. Le garde-fou central : ce qui n'est pas couvert est NOMME
# --------------------------------------------------------------------------

# La Suisse est desormais COUVERTE - mais pas avec la meme autorite. Elle doit
# ressortir avec son taux ET une reserve nommant l'administration a consulter.
codes, alertes = server._resolve("CH", connus)
check("resolve : CH rend bien un code maintenant", codes == ["CH"], str(codes))
check("resolve : CH declenche une reserve de provenance",
      any("PROVENANCE" in a for a in alertes), str(alertes))
check("resolve : la reserve CH dit que ce n'est pas officiel",
      any("officielle de la Commission" in a for a in alertes), str(alertes))
check("resolve : la reserve CH nomme l'administration a consulter",
      any("contributions" in a for a in alertes), str(alertes))
check("resolve : la reserve CH s'accorde au singulier",
      any("ne vient PAS" in a for a in alertes), str(alertes))

# NO n'est pas dans le faux releve : reconnu du referentiel, absent des
# donnees. Ce n'est PAS « la Norvege n'a pas de TVA ».
codes, alertes = server._resolve("FR,CH,NO", connus)
check("resolve : les deux couverts passent, l'absent alerte",
      codes == ["FR", "CH"], str(codes))
check("resolve : l'absent du releve est nomme comme tel",
      any("ABSENT DU RELEVE" in a and "Norvege" in a for a in alertes), str(alertes))
check("resolve : et il dit ou verifier", any("Skatteetaten" in a for a in alertes),
      str(alertes))
check("resolve : aucune alerte n'est dupliquee", len(alertes) == len(set(alertes)),
      str(alertes))

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
check("taux : la Suisse est rendue", "\nCH;" in sortie)
check("taux : avec sa devise", "CHF" in sortie)
check("taux : et sa provenance en colonne", "vatnode (main)" in sortie)
check("taux : la reserve de provenance est en tete", "PROVENANCE" in sortie)
check("taux : la ligne France reste marquee TEDB",
      any(l.startswith("FR;") and l.endswith("TEDB") for l in sortie.splitlines()),
      [l for l in sortie.splitlines() if l.startswith("FR;")])

# Un territoire, lui, reste NON COUVERT - et c'est le dernier vrai « non
# couvert » du connecteur.
sortie = server.tva_taux("ES,CANARIES")
check("taux : un territoire ressort NON COUVERT", "NON COUVERT" in sortie)
check("taux : et le territoire ne fabrique pas de ligne", "\nCANARIES;" not in sortie)

sortie = server.tva_taux("FR,EL", categorie="transport_passengers")
check("taux : colonne de categorie ajoutee", "taux_transport_passengers" in sortie)
check("taux : la categorie absente d'un pays est signalee",
      "pas de taux reduit declare" in sortie, "note manquante")

sortie = server.tva_taux("CANARIES")
check("taux : aucune juridiction couverte -> aucune ligne", "(aucune ligne)" in sortie)
check("taux : et la mention de source reste presente",
      "administration fiscale" in sortie)

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

sortie = server.tva_detail("CANARIES")
check("detail : un territoire non couvert ne fabrique pas de fiche",
      "NON COUVERT" in sortie and "Aucun pays couvert" in sortie)

# La fiche suisse existe, mais elle doit dire ce qu'elle N'A PAS.
sortie = server.tva_detail("CH")
check("detail : la fiche Suisse existe", "Suisse" in sortie and "8.1" in sortie)
check("detail : elle annonce sa provenance", "vatnode (main)" in sortie)
check("detail : elle previent qu'il n'y a ni categorie ni nomenclature",
      "ne vient PAS de la base officielle" in sortie)
check("detail : elle nomme l'administration federale", "contributions" in sortie)

sortie = server.tva_detail("SE")
check("detail : un pays sans categorie le dit", "aucune declaree" in sortie)

sortie = server.tva_detail("FR")
check("detail : les codes de nomenclature douaniere sont montres",
      "[CN 0401 0402]" in sortie, [l for l in sortie.splitlines() if "CN" in l])
check("detail : les exemptions sont distinguees du taux zero",
      "POSTAGE" in sortie and "taux zero" in sortie)
check("detail : la date d'effet du taux normal est donnee",
      "en vigueur depuis le 2026-07-01" in sortie)

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
check("pays : distingue la source officielle de celle tenue a la main",
      "SOURCE OFFICIELLE" in sortie and "SOURCE TENUE A LA MAIN" in sortie)
check("pays : le Royaume-Uni est cite du cote tenu a la main",
      sortie.index("SOURCE TENUE A LA MAIN") < sortie.index("Royaume-Uni"))
check("pays : XI est signalee comme distincte de GB",
      "distincte de GB" in sortie)
check("pays : les territoires sont dits couverts par AUCUNE source",
      "AUCUNE DES DEUX SOURCES" in sortie)

codes, alertes = server._resolve("VU", connus)
check("resolve : VU sans perimetre retombe sur tout le perimetre couvert",
      len(codes) == 4, str(codes))
check("resolve : et il dit que TVA_PAYS_VU est une decision, pas un defaut",
      any("PERIMETRE" in a and "decision" in a for a in alertes), str(alertes))

os.environ["TVA_PAYS_VU"] = "FR, Suède, CH"
server._SHARED_CACHE = None
codes, alertes = server._resolve("VU", connus)
check("resolve : VU respecte le perimetre d'equipe", codes == ["FR", "SE", "CH"], str(codes))
check("resolve : la Suisse du perimetre garde sa reserve de provenance",
      any("PROVENANCE" in a and "CH" in a for a in alertes), str(alertes))
check("resolve : la reserve n'est posee QU'UNE FOIS malgre la recursion",
      sum(1 for a in alertes if "PROVENANCE" in a) == 1, str(alertes))
codes, _ = server._resolve("", connus)
check("resolve : vide suit le perimetre d'equipe", codes == ["FR", "SE", "CH"], str(codes))
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

sortie = server.tva_export_csv("FR,CANARIES", filename="controle_alerte")
check("export : le territoire non couvert est signale dans le rendu",
      "NON COUVERT" in sortie)
sortie = server.tva_export_csv("FR,CH", filename="controle_provenance")
check("export : la provenance part dans le fichier", "provenance" in sortie)
check("export : et la reserve est rappelee", "PROVENANCE" in sortie)

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
# 14. Les deux sources : le depouillement, sur echantillon, hors ligne
# --------------------------------------------------------------------------
#
# C'est la couche qui a change au passage a TEDB. Un depouillement faux ne leve
# aucune erreur : il rend un taux plausible et faux. On l'eprouve donc sur un
# echantillon reduit mais de forme reelle, sans jamais sortir de la machine.

import sources  # noqa: E402

check("sources : 28 juridictions demandees a TEDB",
      len(sources.TEDB_JURIDICTIONS) == 28, str(len(sources.TEDB_JURIDICTIONS)))
check("sources : TEDB est interroge sur EL, jamais sur GR",
      "EL" in sources.TEDB_JURIDICTIONS and "GR" not in sources.TEDB_JURIDICTIONS)
check("sources : XI fait partie de la demande TEDB",
      "XI" in sources.TEDB_JURIDICTIONS)

# --- l'enveloppe SOAP ---
env = sources.enveloppe_tedb(["FR", "EL"], "2026-09-02")
check("sources : l'enveloppe porte les deux codes",
      "<t:isoCode>FR</t:isoCode>" in env and "<t:isoCode>EL</t:isoCode>" in env)
check("sources : l'enveloppe porte la date de situation",
      "<t:situationOn>2026-09-02</t:situationOn>" in env)
check("sources : l'enveloppe declare le namespace des types",
      "IVatRetrievalService:types" in env)

# --- une reponse TEDB de forme reelle ---
T = "urn:ec.europa.eu:taxud:tedb:services:v1:IVatRetrievalService:types"
ECHANTILLON = (
    '<env:Envelope xmlns:env="http://schemas.xmlsoap.org/soap/envelope/">'
    "<env:Body><ns0:retrieveVatRatesRespMsg"
    ' xmlns="' + T + '"'
    ' xmlns:ns0="urn:ec.europa.eu:taxud:tedb:services:v1:IVatRetrievalService">'
    "<additionalInformation/>"
    # le taux normal
    "<vatRateResults><memberState>FR</memberState><type>STANDARD</type>"
    "<rate><type>DEFAULT</type><value>20.0</value></rate>"
    "<situationOn>2026-07-01+02:00</situationOn></vatRateResults>"
    # un reduit avec categorie, codes CN et commentaire
    "<vatRateResults><memberState>FR</memberState><type>REDUCED</type>"
    "<rate><type>REDUCED_RATE</type><value>5.5</value></rate>"
    "<situationOn>2026-07-01+02:00</situationOn>"
    "<cnCodes><code><value>0401</value><description>Lait</description></code>"
    "<code><value>0402</value><description>Lait en poudre</description></code>"
    "</cnCodes>"
    "<category><identifier>FOODSTUFFS</identifier>"
    "<description>Produits alimentaires</description></category>"
    "<comment>Ne couvre pas les boissons alcooliques</comment>"
    "</vatRateResults>"
    # un second reduit sur la MEME categorie : la liste doit garder les deux
    "<vatRateResults><memberState>FR</memberState><type>REDUCED</type>"
    "<rate><type>REDUCED_RATE</type><value>10.0</value></rate>"
    "<situationOn>2026-07-01+02:00</situationOn>"
    "<category><identifier>FOODSTUFFS</identifier>"
    "<description>Produits alimentaires</description></category>"
    "</vatRateResults>"
    # super-reduit et parking
    "<vatRateResults><memberState>FR</memberState><type>REDUCED</type>"
    "<rate><type>SUPER_REDUCED_RATE</type><value>2.1</value></rate>"
    "<situationOn>2026-07-01+02:00</situationOn></vatRateResults>"
    "<vatRateResults><memberState>EL</memberState><type>REDUCED</type>"
    "<rate><type>PARKING_RATE</type><value>13.0</value></rate>"
    "<situationOn>2025-01-01+01:00</situationOn></vatRateResults>"
    "<vatRateResults><memberState>EL</memberState><type>STANDARD</type>"
    "<rate><type>DEFAULT</type><value>24.0</value></rate>"
    "<situationOn>2025-01-01+01:00</situationOn></vatRateResults>"
    # une exemption : « exempte » n'est PAS « taux zero »
    "<vatRateResults><memberState>FR</memberState><type>REDUCED</type>"
    "<rate><type>EXEMPTED</type></rate>"
    "<situationOn>2026-07-01+02:00</situationOn>"
    "<category><identifier>POSTAGE</identifier>"
    "<description>Services postaux</description></category>"
    "</vatRateResults>"
    "</ns0:retrieveVatRatesRespMsg></env:Body></env:Envelope>"
).encode("utf-8")

lignes = {r["country_code"]: r for r in sources.depouille_tedb(ECHANTILLON)}
check("tedb : deux juridictions depouillees", sorted(lignes) == ["EL", "FR"], str(sorted(lignes)))
fr = lignes["FR"]
check("tedb : le taux normal se lit dans rate/type=DEFAULT", fr["standard_rate"] == 20.0,
      str(fr["standard_rate"]))
check("tedb : les reduits sont regroupes et tries", fr["reduced_rates"] == [5.5, 10.0],
      str(fr["reduced_rates"]))
check("tedb : le super-reduit est distingue du reduit", fr["super_reduced_rate"] == 2.1)
check("tedb : la categorie porte SES DEUX taux, pas le dernier lu",
      fr["rate_categories"]["FOODSTUFFS"] == [5.5, 10.0],
      str(fr["rate_categories"]))
check("tedb : les codes de nomenclature sont rattaches a la categorie",
      fr["cn_codes"]["FOODSTUFFS"] == ["0401", "0402"], str(fr["cn_codes"]))
check("tedb : le commentaire officiel est conserve",
      "alcooliques" in str(fr["rate_comments"]), str(fr["rate_comments"]))
check("tedb : une exemption va dans exemptions, pas dans les taux",
      fr["exemptions"] == ["POSTAGE"] and 0.0 not in fr["reduced_rates"],
      str(fr["exemptions"]))
check("tedb : la date d'effet est amputee de son decalage horaire",
      fr["effective_on"] == "2026-07-01", fr["effective_on"])
check("tedb : le taux parking est lu", lignes["EL"]["parking_rate"] == 13.0)
check("tedb : TEDB ne rend aucune devise", fr["currency"] == "")
check("tedb : la provenance est marquee officielle",
      fr["source"] == "TEDB" and fr["provenance"] == "officielle")

# --- ce qui doit ECHOUER bruyamment ---
FAUTE = (
    '<env:Envelope xmlns:env="http://schemas.xmlsoap.org/soap/envelope/"><env:Body>'
    '<ns0:retrieveVatRatesFaultMsg xmlns="' + T + '"'
    ' xmlns:ns0="urn:ec.europa.eu:taxud:tedb:services:v1:IVatRetrievalService">'
    "<error><code>TEDB_003</code><description>Invalid member state</description></error>"
    "</ns0:retrieveVatRatesFaultMsg></env:Body></env:Envelope>"
).encode("utf-8")
try:
    sources.depouille_tedb(FAUTE)
    check("tedb : une faute SOAP leve une erreur", False, "aucune exception")
except sources.SourceError as exc:
    check("tedb : une faute SOAP leve une erreur", True)
    check("tedb : et le message porte le code de la faute", "TEDB_003" in str(exc), str(exc))

try:
    sources.depouille_tedb(b"ceci n'est pas du XML")
    check("tedb : une reponse illisible leve une erreur", False, "aucune exception")
except sources.SourceError:
    check("tedb : une reponse illisible leve une erreur", True)

# --- vatnode, et le piege de la Grece ---
VATNODE = {
    "version": "2026-09-01",
    "source": "European Commission TEDB",
    "rates": {
        "FR": {"country": "France", "currency": "EUR", "eu_member": True,
               "standard": 20.0, "reduced": [5.5, 10.0], "super_reduced": 2.1,
               "parking": None},
        "GR": {"country": "Greece", "currency": "EUR", "eu_member": True,
               "standard": 24.0, "reduced": [6.0], "super_reduced": 4.0,
               "parking": 13.0},
        "CZ": {"country": "Czechia", "currency": "CZK", "eu_member": True,
               "standard": 21.0, "reduced": [12.0], "super_reduced": None,
               "parking": None},
        "CH": {"country": "Switzerland", "currency": "CHF", "eu_member": False,
               "standard": 8.1, "reduced": [3.8, 2.6], "super_reduced": None,
               "parking": None, "vat_name": "Mehrwertsteuer", "vat_abbr": "MWST"},
        "XI": {"country": "Northern Ireland", "currency": "GBP",
               "eu_member": False, "standard": 20.0, "reduced": [5.0],
               "super_reduced": None, "parking": None},
    },
}
hors, devises, version = sources.depouille_vatnode(VATNODE)
check("vatnode : la version du jeu est lue", version == "2026-09-01", version)
check("vatnode : GR est traduit en EL dans les devises",
      "EL" in devises and "GR" not in devises, str(sorted(devises)))
check("vatnode : la devise d'un Etat membre est conservee",
      devises["CZ"] == "CZK" and devises["FR"] == "EUR")
check("vatnode : les Etats membres ne produisent AUCUNE ligne",
      not any(r["country_code"] in ("FR", "EL", "CZ") for r in hors),
      str([r["country_code"] for r in hors]))
check("vatnode : XI non plus - c'est TEDB qui la sert",
      not any(r["country_code"] == "XI" for r in hors),
      str([r["country_code"] for r in hors]))
check("vatnode : la Suisse produit une ligne", [r["country_code"] for r in hors] == ["CH"],
      str([r["country_code"] for r in hors]))
ch = hors[0]
check("vatnode : ses reduits sont tries", ch["reduced_rates"] == [2.6, 3.8],
      str(ch["reduced_rates"]))
check("vatnode : la ligne est marquee tenue a la main",
      ch["source"] == "vatnode" and ch["provenance"] == "tenue a la main")
check("vatnode : elle ne porte ni categorie ni code CN",
      ch["rate_categories"] == {} and ch["cn_codes"] == {})

try:
    sources.depouille_vatnode({"pas_de_rates": True})
    check("vatnode : une forme inattendue leve une erreur", False, "aucune exception")
except sources.SourceError as exc:
    check("vatnode : une forme inattendue leve une erreur", True)
    check("vatnode : et le message dit ce qui manque", "rates" in str(exc), str(exc))

check("config : TVA_BASE_URL garde son slash final (endpoint SOAP, pas racine)",
      server._base_url().endswith("/"), server._base_url())
os.environ["TVA_BASE_URL"] = "https://exemple.test/ws"
check("config : TVA_BASE_URL est bien pris en compte",
      server._base_url() == "https://exemple.test/ws/", server._base_url())
del os.environ["TVA_BASE_URL"]
check("config : TVA_VATNODE_URL vide laisse le repli au module sources",
      server._vatnode_url() == "", server._vatnode_url())
check("config : TVA_VATNODE_URL est distribuable par le fichier d'equipe",
      "TVA_VATNODE_URL" in server.SHARED_ALLOWED)
# Quand l'URL est forcee, on ne doit PLUS essayer le miroir : sinon un
# bouchon de test se ferait doubler par un appel reseau reel.
_appels: list[str] = []
_vrai_http = sources._http


def _http_espion(method, url, timeout, **kw):
    _appels.append(url)
    raise sources.SourceError("bouchon")


sources._http = _http_espion
try:
    sources.fetch_vatnode(1.0, url_base="https://exemple.test/jeu.json")
except sources.SourceError:
    pass
check("config : URL vatnode forcee = un seul chemin essaye, pas de miroir",
      _appels == ["https://exemple.test/jeu.json"], str(_appels))
_appels.clear()
try:
    sources.fetch_vatnode(1.0)
except sources.SourceError:
    pass
check("config : sans URL forcee, le CDN puis le depot brut sont essayes",
      _appels == [sources.VATNODE_URL, sources.VATNODE_MIROIR], str(_appels))
sources._http = _vrai_http

check("sources : _jour ampute le decalage horaire",
      sources._jour("2025-01-01+01:00") == "2025-01-01")
check("sources : _jour sur une valeur vide ne fabrique pas de date",
      sources._jour(None) == "" and sources._jour("") == "")

# --- la fusion, et la degradation asymetrique ---
#
# TEDB est la source qui ne se degrade pas : sans elle, on ne rend rien. Perdre
# les 27 pour ne servir que les juridictions tenues a la main serait servir la
# moins bonne moitie en silence. vatnode, lui, peut manquer : on perd les
# devises et le hors-Union, et le rendu le dit.
_vrai_tedb, _vrai_vatnode = sources.fetch_tedb, sources.fetch_vatnode


def _tedb_ok(timeout, situation="", endpoint=""):
    return sources.depouille_tedb(ECHANTILLON)


def _vatnode_ko(timeout, url_base=""):
    raise sources.SourceError("panne simulee")


sources.fetch_tedb, sources.fetch_vatnode = _tedb_ok, _vatnode_ko
degrade = sources.fetch_tout(1.0)
check("fusion : sans vatnode, les lignes TEDB sont quand meme rendues",
      len(degrade["rows"]) == 2, str(len(degrade["rows"])))
check("fusion : et l'incident est remonte, pas avale",
      any("vatnode" in i for i in degrade["incidents"]), str(degrade["incidents"]))
check("fusion : l'incident dit que ce n'est PAS une absence de TVA",
      any("pas" in i.lower() and "TVA" in i for i in degrade["incidents"]),
      str(degrade["incidents"]))


def _tedb_ko(timeout, situation="", endpoint=""):
    raise sources.SourceError("TEDB muet (simule)")


sources.fetch_tedb = _tedb_ko
try:
    sources.fetch_tout(1.0)
    check("fusion : sans TEDB, on ne rend RIEN", False, "aucune exception")
except sources.SourceError:
    check("fusion : sans TEDB, on ne rend RIEN", True)

# La devise vient de vatnode, y compris pour les Etats membres : la Bulgarie est
# passee a l'euro en 2026, donc graver les devises aurait fabrique un fait faux.
def _vatnode_ok(timeout, url_base=""):
    return sources.depouille_vatnode(VATNODE)


sources.fetch_tedb, sources.fetch_vatnode = _tedb_ok, _vatnode_ok
fusion = {r["country_code"]: r for r in sources.fetch_tout(1.0)["rows"]}
check("fusion : la devise de vatnode est posee sur les lignes TEDB",
      fusion["FR"]["currency"] == "EUR" and fusion["EL"]["currency"] == "EUR",
      str({k: v["currency"] for k, v in fusion.items()}))
check("fusion : la ligne EL garde ses donnees TEDB, pas celles de vatnode",
      fusion["EL"]["parking_rate"] == 13.0 and fusion["EL"]["source"] == "TEDB")
check("fusion : les Etats membres passent avant le hors-Union",
      [r["country_code"] for r in sources.fetch_tout(1.0)["rows"]][:2] == ["EL", "FR"],
      str([r["country_code"] for r in sources.fetch_tout(1.0)["rows"]]))
sources.fetch_tedb, sources.fetch_vatnode = _vrai_tedb, _vrai_vatnode

# --------------------------------------------------------------------------
# Verdict
# --------------------------------------------------------------------------

print(f"\n{PASSED} controle(s) passe(s), {len(FAILED)} echec(s).")
if FAILED:
    print("\nEchecs :")
    for line in FAILED:
        print(f"  - {line}")
sys.exit(1 if FAILED else 0)
