#!/usr/bin/env python3
"""
Controles du serveur MCP trustpilot, SANS RESEAU et SANS CLE.

    python test_offline.py

Ce que ca verifie : la liste blanche des chemins GET et leur regime d'auth, la
resolution des dix-huit domaines et de leurs alias, la pagination et ses
raisons d'arret, l'arret sur borne de date du chemin public, le classement
thematique multilingue, la desambiguisation des transporteurs par pays,
l'enrichissement des avis, l'agregation, le retrait des donnees personnelles,
le cache local SQLite, et la purete du flux stdio.

Ce que ca ne verifie PAS : que l'API Trustpilot rend ce qu'on croit. Ca, c'est
la recette contre le vrai compte, et elle demande une cle. En particulier, la
liste blanche est ecrite A LA MAIN faute de contrat OpenAPI publie : seul un
appel reel confirme qu'un chemin et ses parametres existent tels qu'ecrits.

Aucun appel reseau n'est emis : la couche HTTP est remplacee par un faux qui
leve si on l'atteint sans l'avoir prevu.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import pathlib
import shutil
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

SANDBOX = pathlib.Path(os.environ.get("TEMP") or "/tmp") / "trustpilot-mcp-test"
shutil.rmtree(SANDBOX, ignore_errors=True)
SANDBOX.mkdir(parents=True, exist_ok=True)

# Un environnement propre : aucune cle, aucun fichier de configuration lu, et
# surtout AUCUN fichier d'equipe. Sur un poste qui synchronise la bibliotheque,
# 08_ENGINE/04_mcp/00_config/trustpilot.shared.env serait trouve et lu au
# niveau 2 de _env, et les controles tourneraient avec la vraie cle de
# service - donc ne prouveraient rien. Un chemin explicite est EXCLUSIF, c'est
# ce qui rend cette isolation fiable.
for name in list(os.environ):
    if name.startswith("TRUSTPILOT_"):
        del os.environ[name]
os.environ["TRUSTPILOT_ENV_FILE"] = str(SANDBOX / "_inexistant_.env")
os.environ["TRUSTPILOT_SHARED_ENV"] = str(SANDBOX / "_inexistant_.shared.env")
os.environ["TRUSTPILOT_HOME"] = str(SANDBOX)
os.environ.pop("VU_ENGINE_DIR", None)
os.environ.pop("CLAUDE_PLUGIN_ROOT", None)
os.environ.pop("CLAUDE_PLUGIN_DATA", None)

import server  # noqa: E402
import vu_cache  # noqa: E402

PASS = 0
FAIL: list[str] = []


def section(title: str) -> None:
    print()
    print(title)
    print("-" * len(title))


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS
    if ok:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL.append(label)
        print(f"  ECHEC {label}" + (f" ({detail})" if detail else ""))


# --------------------------------------------------------------------------
section("1. Le module de cache partage")
# --------------------------------------------------------------------------
# vu_cache.py est DUPLIQUE a l'identique dans les plugins de l'equipe : un
# plugin Claude Code est autonome et ne peut pas importer son voisin.
# L'empreinte ci-dessous est la contrepartie de cette duplication : si
# quelqu'un corrige le module dans un seul plugin, ce controle le dit, au lieu
# de laisser les copies diverger en silence.
#
# En cas d'echec : recopier le fichier dans tous les plugins, puis mettre a
# jour l'empreinte dans chaque test_offline.py.

EMPREINTE_VU_CACHE = "d346b3cfc1ac0e21ab44640a6004827f564dbd5b3b9cfaf865ce36f24204fdb2"
_brut = (HERE / "vu_cache.py").read_bytes().replace(b"\r\n", b"\n")
check(
    "vu_cache.py est la version partagee attendue",
    hashlib.sha256(_brut).hexdigest() == EMPREINTE_VU_CACHE,
    hashlib.sha256(_brut).hexdigest(),
)

# --------------------------------------------------------------------------
section("2. Liste blanche des chemins GET et regime d'authentification")
# --------------------------------------------------------------------------

check("le contrat est charge", len(server._spec()["paths"]) >= 15)
check(
    "/v1/business-units/find est accepte",
    server._check_get_path("/v1/business-units/find") == "/v1/business-units/find",
)
check(
    "le slash de tete manquant est tolere",
    server._check_get_path("v1/business-units/find") == "/v1/business-units/find",
)
check(
    "un identifiant concret matche le gabarit a accolades",
    server._check_get_path("/v1/business-units/47e1b1b30000640005022bbb/reviews")
    == "/v1/business-units/47e1b1b30000640005022bbb/reviews",
)
for mauvais, pourquoi in (
    ("/v1/inconnu", "chemin absent du contrat"),
    ("/v1/business-units/find?name=x", "parametres dans le chemin"),
    ("/v1/private/reviews/abc/reply", "chemin d'ECRITURE"),
    ("/v1/private/business-units/abc/email-invitations", "invitations"),
):
    try:
        server._check_get_path(mauvais)
        check(f"refus de {mauvais} ({pourquoi})", False, "accepte a tort")
    except server.TrustpilotError:
        check(f"refus de {mauvais} ({pourquoi})", True)

check(
    "le chemin public des avis est en regime apikey",
    server._auth_for("/v1/business-units/abc/reviews") == "apikey",
)
check(
    "le chemin prive des avis est en regime oauth",
    server._auth_for("/v1/private/business-units/abc/reviews") == "oauth",
)
check(
    "les avis sont pagines",
    server._supports_paging("/v1/business-units/abc/reviews"),
)
check(
    "profileinfo ne l'est pas",
    not server._supports_paging("/v1/business-units/abc/profileinfo"),
)
check(
    "la recherche de business unit attend perpage en minuscules",
    server._page_params("/v1/business-units/search") == ("page", "perpage"),
)
check(
    "les avis attendent perPage",
    server._page_params("/v1/business-units/abc/reviews") == ("page", "perPage"),
)

# La liste blanche n'est pas seulement une commodite : c'est ce qui garantit la
# lecture seule. On verifie qu'aucune methode d'ecriture ne peut etre emise.
check(
    "_request n'expose aucun parametre de methode HTTP",
    "method" not in server._request.__code__.co_varnames,
)

# --------------------------------------------------------------------------
section("3. Resolution des domaines")
# --------------------------------------------------------------------------

check("les dix-huit domaines sont au catalogue", len(server._bu_catalogue()) == 18)
for terme, attendu in (
    ("www.vente-unique.it", "www.vente-unique.it"),
    ("vente-unique.it", "www.vente-unique.it"),
    ("https://www.vente-unique.it/", "www.vente-unique.it"),
    ("it", "www.vente-unique.it"),
    ("Italie", "www.vente-unique.it"),
    ("italie", "www.vente-unique.it"),
    ("Habitat", "www.habitat.fr"),
    ("norvege", "www.vente-unique.no"),
    ("Norvège", "www.vente-unique.no"),
    ("Royaume-Uni", "www.vente-unique.uk"),
    ("Suède", "www.vente-unique.se"),
    ("allemagne", "www.kauf-unique.de"),
    ("autriche", "www.kauf-unique.at"),
):
    try:
        obtenu = server._resolve_domain(terme)
    except server.TrustpilotError as exc:
        obtenu = f"ERREUR {exc}"
    check(f"'{terme}' -> {attendu}", obtenu == attendu, str(obtenu)[:120])

check(
    "un identifiant de business unit passe tel quel",
    server._resolve_domain("47e1b1b30000640005022bbb") == "47e1b1b30000640005022bbb",
)
try:
    server._resolve_domain("Bolivie")
    check("un pays hors perimetre est refuse", False, "accepte a tort")
except server.TrustpilotError as exc:
    check(
        "un pays hors perimetre est refuse ET la liste est donnee",
        "vente-unique" in str(exc),
    )
check("domaine vide = tout le perimetre", len(server._resolve_domains("")) == 18)
check(
    "plusieurs domaines separes par des virgules",
    server._resolve_domains("it, es,uk")
    == ["www.vente-unique.it", "www.vente-unique.es", "www.vente-unique.uk"],
)

# --------------------------------------------------------------------------
section("4. Dates de filtre")
# --------------------------------------------------------------------------

check("AAAA-MM-JJ passe", server._iso_date("2026-03-01", "debut") == "2026-03-01")
check(
    "AAAA-MM en borne basse = le 1er",
    server._iso_date("2026-03", "debut") == "2026-03-01",
)
check(
    "AAAA-MM en borne haute = le dernier jour",
    server._iso_date("2026-03", "fin") == "2026-03-31",
)
check(
    "fevrier bissextile",
    server._iso_date("2024-02", "fin") == "2024-02-29",
)
check(
    "decembre ne deborde pas sur l'annee suivante",
    server._iso_date("2026-12", "fin") == "2026-12-31",
)
check("une date vide reste vide", server._iso_date("", "debut") == "")
for mauvais in ("01/03/2026", "mars 2026", "2026"):
    try:
        server._iso_date(mauvais, "debut")
        check(f"refus de la date '{mauvais}'", False, "acceptee a tort")
    except server.TrustpilotError:
        check(f"refus de la date '{mauvais}'", True)

# --------------------------------------------------------------------------
section("5. Pagination : les raisons d'arret")
# --------------------------------------------------------------------------

APPELS: list[dict] = []
FAUX_TOTAL = 0


def faux_request(path, query=None):
    APPELS.append(dict(query or {}))
    page = int((query or {}).get("page") or 1)
    par_page = int((query or {}).get("perPage") or 100)
    debut = (page - 1) * par_page
    restant = max(0, FAUX_TOTAL - debut)
    combien = min(par_page, restant)
    return {
        "reviews": [
            {"id": f"r{debut + i}", "stars": 3, "createdAt": "2026-08-01T10:00:00Z"}
            for i in range(combien)
        ]
    }


VRAI_REQUEST = server._request
server._request = faux_request
CHEMIN = "/v1/business-units/abc/reviews"

try:
    FAUX_TOTAL = 250
    APPELS.clear()
    rows, arret = server._paginate(CHEMIN, {}, max_rows=1000)
    check("lecture complete : 250 avis", len(rows) == 250, str(len(rows)))
    check("lecture complete : aucune raison d'arret", arret == "", arret)
    check("trois appels, pas quatre", len(APPELS) == 3, str(len(APPELS)))
    check("la page commence a 1", APPELS[0].get("page") == 1, str(APPELS[0]))

    FAUX_TOTAL = 300
    APPELS.clear()
    rows, arret = server._paginate(CHEMIN, {}, max_rows=1000)
    check(
        "collection multiple de perPage : une page vide ferme la boucle",
        len(rows) == 300 and arret == "",
        f"{len(rows)} / {arret}",
    )

    FAUX_TOTAL = 5000
    APPELS.clear()
    rows, arret = server._paginate(CHEMIN, {}, max_rows=150)
    check("max_rows arrete la lecture", arret == "max_rows", arret)
    check("max_rows coupe au plafond exact", len(rows) == 150, str(len(rows)))

    FAUX_TOTAL = 100000
    APPELS.clear()
    rows, arret = server._paginate(CHEMIN, {}, max_rows=10**9, max_pages=3)
    check("max_pages arrete la lecture", arret == "max_pages", arret)
    check("max_pages : trois appels", len(APPELS) == 3, str(len(APPELS)))

    # Un resultat qui fait EXACTEMENT max_rows et qui est complet ne doit pas
    # etre annonce tronque : un rendu dit tronque n'est pas citable, donc un
    # chiffre juste deviendrait inutilisable.
    FAUX_TOTAL = 150
    rows, arret = server._paginate(CHEMIN, {}, max_rows=150)
    check(
        "exactement max_rows et complet : pas annonce tronque",
        len(rows) == 150 and arret == "",
        f"{len(rows)} / {arret}",
    )

    # L'arret sur borne de date : c'est ce qui rend le chemin PUBLIC utilisable
    # sur une periode, alors qu'il ne sait pas filtrer les dates.
    def request_dates(path, query=None):
        APPELS.append(dict(query or {}))
        page = int((query or {}).get("page") or 1)
        par_page = int((query or {}).get("perPage") or 100)
        debut = (page - 1) * par_page
        import datetime as _dt

        out = []
        for i in range(par_page):
            jour = _dt.date(2026, 9, 1) - _dt.timedelta(days=debut + i)
            out.append(
                {"id": f"r{debut + i}", "stars": 4, "createdAt": f"{jour}T08:00:00Z"}
            )
        return {"reviews": out}

    server._request = request_dates
    APPELS.clear()
    rows, arret = server._paginate(
        CHEMIN, {}, max_rows=10**6, stop_before="2026-08-15"
    )
    check(
        "arret sur borne basse : rien avant le 2026-08-15",
        all(str(r["createdAt"])[:10] >= "2026-08-15" for r in rows),
    )
    check(
        "arret sur borne basse : la lecture est declaree COMPLETE",
        arret == "",
        arret,
    )
    check(
        "arret sur borne basse : 18 avis, pas la collection entiere",
        len(rows) == 18,
        str(len(rows)),
    )
    check(
        "arret sur borne basse : un seul appel a suffi",
        len(APPELS) == 1,
        str(len(APPELS)),
    )

    # Le chemin public rend un HTTP 400 au-dela d'une certaine profondeur : ce
    # n'est pas une panne, c'est un plafond, et il doit etre DIT.
    def request_plafond(path, query=None):
        page = int((query or {}).get("page") or 1)
        if page >= 3:
            raise server.TrustpilotError("HTTP 400 sur ... | page too deep")
        return {
            "reviews": [
                {"id": f"p{page}-{i}", "stars": 5, "createdAt": "2026-08-01T00:00:00Z"}
                for i in range(100)
            ]
        }

    server._request = request_plafond
    rows, arret = server._paginate(CHEMIN, {}, max_rows=10**6)
    check("le plafond de l'API est nomme", arret == "plafond_api", arret)
    check("et les pages deja lues sont conservees", len(rows) == 200, str(len(rows)))
    check(
        "le message d'arret explique le plafond public",
        "PUBLIC" in server._STOP_MESSAGES["plafond_api"],
    )
finally:
    server._request = VRAI_REQUEST

# --------------------------------------------------------------------------
section("6. Classement thematique multilingue")
# --------------------------------------------------------------------------

check("treize themes charges", len(server._themes_def()) == 13, str(len(server._themes_def())))

CAS = [
    ("Livraison avec trois semaines de retard", "fr", "livraison_delai"),
    ("Lieferung mit grosser Verspatung", "de", "livraison_delai"),
    ("Consegna in ritardo di due settimane", "it", "livraison_delai"),
    ("Entrega con mucho retraso", "es", "livraison_delai"),
    ("Levering met grote vertraging", "nl", "livraison_delai"),
    ("Dostawa z duzym opoznieniem", "pl", "livraison_delai"),
    ("Leveransen var forsenad", "sv", "livraison_delai"),
    ("Leveringen var forsinket", "nb", "livraison_delai"),
    ("Delivery was very late", "en", "livraison_delai"),
    ("Canape arrive abime, carton ecrase", "fr", "produit_abime_casse"),
    ("Ware beschadigt angekommen", "de", "produit_abime_casse"),
    ("Il manque un colis sur trois", "fr", "produit_manquant_incomplet"),
    ("Le livreur a laisse le colis sur le trottoir", "fr", "livraison_etage_manutention"),
    ("Notice de montage illisible, vis manquantes", "fr", "montage_notice"),
    ("Service client injoignable, aucune reponse", "fr", "sav_service_client"),
    ("Toujours pas de remboursement apres le retour", "fr", "retour_remboursement"),
    ("Aucun suivi, aucune information sur ma commande", "fr", "suivi_information"),
    ("Bon rapport qualite prix", "fr", "prix_valeur"),
    ("Le livreur etait tres aimable et professionnel", "fr", "livraison_equipe_livreur"),
]
for texte, langue, attendu in CAS:
    themes = server._classify(texte, langue)
    check(
        f"[{langue}] '{texte[:42]}' -> {attendu}",
        attendu in themes,
        "detecte: " + (",".join(themes) or "rien"),
    )

check(
    "un texte vide ne classe rien",
    server._classify("", "fr") == [],
)
check(
    "un texte sans mot du lexique ne classe rien",
    server._classify("Zzzz yyyy xxxx", "fr") == [],
    ",".join(server._classify("Zzzz yyyy xxxx", "fr")),
)
check(
    "un avis peut porter plusieurs themes",
    len(server._classify("Livre en retard et le canape est abime", "fr")) >= 2,
)
# La frontiere de mot : sans elle, 'vis' se declencherait dans 'television'.
check(
    "frontiere de mot : 'television' ne declenche pas la visserie",
    "montage_notice" not in server._classify("La television est bien", "fr"),
    ",".join(server._classify("La television est bien", "fr")),
)
# La tolerance de flexion est BORNEE aux termes de six caracteres et plus,
# justement pour que les termes courts ne debordent pas.
check(
    "flexion : 'chercher' ne declenche pas le theme prix ('cher')",
    "prix_valeur" not in server._classify("Je vais chercher ailleurs", "fr"),
    ",".join(server._classify("Je vais chercher ailleurs", "fr")),
)
check(
    "flexion : le pluriel d'un terme long est reconnu",
    "livraison_delai" in server._classify("Beaucoup de retards", "fr"),
    ",".join(server._classify("Beaucoup de retards", "fr")),
)
check(
    "flexion : un cas polonais flechi est reconnu",
    "livraison_delai" in server._classify("Dostawa z duzym opoznieniem", "pl"),
    ",".join(server._classify("Dostawa z duzym opoznieniem", "pl")),
)
check(
    "flexion : une declinaison allemande est reconnue",
    "produit_abime_casse" in server._classify("Ein beschadigtes Sofa", "de"),
    ",".join(server._classify("Ein beschadigtes Sofa", "de")),
)
# Le norvegien est code nb OU no selon l'avis : les deux doivent marcher.
check(
    "le norvegien code 'no' est traite comme 'nb'",
    "livraison_delai" in server._classify("Leveringen var forsinket", "no"),
)
# Une langue inconnue retombe sur toutes les langues plutot que de ne rien
# classer : plus bruyant, mais on ne perd pas l'avis.
check(
    "langue absente : on classe quand meme",
    "livraison_delai" in server._classify("Livraison en retard", ""),
)

# --------------------------------------------------------------------------
section("7. Transporteurs cites, et desambiguisation par pays")
# --------------------------------------------------------------------------

check("les transporteurs sont charges", len(server._carriers_def()) >= 30)
check(
    "un avis italien qui dit Rhenus -> Rhenus Italie",
    server._carriers_in("Consegna con Rhenus, disastro", "IT") == ["RHENUS_ITALY"],
    ",".join(server._carriers_in("Consegna con Rhenus", "IT")),
)
check(
    "un avis polonais qui dit Rhenus -> Rhenus Pologne",
    server._carriers_in("Dostawa Rhenus", "PL") == ["RHENUS_POLOGNE"],
    ",".join(server._carriers_in("Dostawa Rhenus", "PL")),
)
check(
    "un avis suedois qui dit Bring -> Bring, pas Posten Bring",
    server._carriers_in("Bring levererade", "SE") == ["BRING"],
    ",".join(server._carriers_in("Bring levererade", "SE")),
)
check(
    "un avis norvegien qui dit Posten -> Posten Bring",
    "POSTEN_BRING" in server._carriers_in("Posten leverte", "NO"),
    ",".join(server._carriers_in("Posten leverte", "NO")),
)
check(
    "et Bring seul n'est pas attribue a la Norvege",
    "BRING" not in server._carriers_in("Posten leverte", "NO"),
    ",".join(server._carriers_in("Posten leverte", "NO")),
)
check(
    "un avis britannique qui dit AIT -> AIT",
    server._carriers_in("AIT delivered the sofa", "UK") == ["AIT"],
    ",".join(server._carriers_in("AIT delivered the sofa", "UK")),
)
check(
    "un avis italien qui dit BRT -> Bartolini",
    server._carriers_in("Corriere BRT", "IT") == ["BARTOLINI"],
    ",".join(server._carriers_in("Corriere BRT", "IT")),
)
check(
    "un avis francais qui dit Colissimo -> Colissimo",
    server._carriers_in("Recu par Colissimo", "FR") == ["COLISSIMO"],
    ",".join(server._carriers_in("Recu par Colissimo", "FR")),
)
check(
    "un transporteur hors du pays de l'avis n'est PAS attribue",
    server._carriers_in("AIT delivered", "IT") == [],
    ",".join(server._carriers_in("AIT delivered", "IT")),
)
check(
    "aucun transporteur cite = liste vide",
    server._carriers_in("Tres beau canape, merci", "FR") == [],
    ",".join(server._carriers_in("Tres beau canape", "FR")),
)

# --------------------------------------------------------------------------
section("8. Enrichissement d'un avis")
# --------------------------------------------------------------------------

AVIS = {
    "id": "abc123",
    "stars": 1,
    "title": "Jamais livre",
    "text": "Trois semaines de retard, et le canape est arrive abime. GLS injoignable.",
    "language": "fr",
    "createdAt": "2026-08-20T10:00:00Z",
    "experiencedAt": "2026-08-05T00:00:00Z",
    "referenceId": "CMD-998877",
    "isVerified": True,
    "source": "invitation",
    "referralEmail": "client.test@example.com",
    "consumer": {"id": "c1", "displayName": "Jean D.", "numberOfReviews": 2},
    "companyReply": {"text": "Nous sommes desoles", "createdAt": "2026-08-21T10:00:00Z"},
}
enrichi = server._enrich(server._flatten(AVIS), "www.vente-unique.com", "prive")

check("le domaine est pose", enrichi["domaine"] == "www.vente-unique.com")
check("le pays vient du lexique", enrichi["pays"] == "FR", enrichi["pays"])
check("l'enseigne vient du lexique", enrichi["enseigne"] == "Vente-unique")
check("annee_mois est calcule", enrichi["annee_mois"] == "2026-08")
check("la note est un entier", enrichi["stars"] == 1, repr(enrichi["stars"]))
check("la presence de reponse est detectee", enrichi["a_reponse"] == "oui")
check(
    "le delai de reponse est en heures",
    enrichi["delai_reponse_h"] == 24.0,
    repr(enrichi["delai_reponse_h"]),
)
check(
    # 15 jours et 10 heures : la fraction est conservee, elle est utile pour
    # savoir si un client ecrit a chaud ou trois semaines apres.
    "l'ecart experience / publication est en jours",
    enrichi["delai_experience_j"] == 15.4,
    repr(enrichi["delai_experience_j"]),
)
check(
    "les themes sont detectes",
    "livraison_delai" in enrichi["themes"] and "produit_abime_casse" in enrichi["themes"],
    enrichi["themes"],
)
check(
    "le transporteur cite est detecte",
    enrichi["transporteurs_cites"] == "GLS",
    enrichi["transporteurs_cites"],
)
check("l'objet imbrique est aplati", enrichi.get("consumer.displayName") == "Jean D.")
check("la route est tracee", enrichi["review_source"] == "prive")

sans_reponse = server._enrich(
    server._flatten({"id": "x", "stars": 5, "createdAt": "2026-08-01T00:00:00Z"}),
    "www.vente-unique.it",
    "public",
)
check("absence de reponse detectee", sans_reponse["a_reponse"] == "non")
check(
    "un delai non calculable reste vide, il ne vaut pas zero",
    sans_reponse["delai_reponse_h"] == "",
    repr(sans_reponse["delai_reponse_h"]),
)

# --------------------------------------------------------------------------
section("9. Donnees personnelles")
# --------------------------------------------------------------------------

nettoye = server._strip_personal([enrichi])[0]
check("referralEmail retire par defaut", "referralEmail" not in nettoye)
check("consumer.id retire par defaut", "consumer.id" not in nettoye)
check("le pseudo public est conserve", "consumer.displayName" in nettoye)
check("le referenceId est conserve", nettoye.get("referenceId") == "CMD-998877")
garde = server._strip_personal([enrichi], keep=True)[0]
check("keep=True conserve le referralEmail", "referralEmail" in garde)
rendu = server._render_table([AVIS], "entete", "", "*")
check(
    "l'adresse mail ne sort JAMAIS d'un rendu de conversation",
    "client.test@example.com" not in rendu,
)

# --------------------------------------------------------------------------
section("10. Agregation")
# --------------------------------------------------------------------------

CORPUS = []
for i in range(10):
    CORPUS.append(
        server._enrich(
            server._flatten(
                {
                    "id": f"a{i}",
                    "stars": 1 if i < 4 else 5,
                    "text": "Retard de livraison" if i < 4 else "Parfait",
                    "language": "fr",
                    "createdAt": f"2026-0{7 if i < 5 else 8}-1{i % 10}T10:00:00Z",
                    "companyReply": (
                        {"text": "ok", "createdAt": f"2026-0{7 if i < 5 else 8}-1{i % 10}T20:00:00Z"}
                        if i % 2 == 0
                        else None
                    ),
                }
            ),
            "www.vente-unique.com",
            "prive",
        )
    )

total = server._aggregate(CORPUS, "aucun")[0]
check("nombre d'avis", total["nb_avis"] == 10, str(total["nb_avis"]))
check("note moyenne", total["note_moyenne"] == 3.4, str(total["note_moyenne"]))
check("part de 1-2 etoiles", total["pct_1_2_etoiles"] == 40.0, str(total["pct_1_2_etoiles"]))
check("part de 4-5 etoiles", total["pct_4_5_etoiles"] == 60.0, str(total["pct_4_5_etoiles"]))
check("taux de reponse", total["taux_reponse_pct"] == 50.0, str(total["taux_reponse_pct"]))
check(
    "delai de reponse median en heures",
    total["delai_reponse_median_h"] == 10.0,
    str(total["delai_reponse_median_h"]),
)

par_mois = server._aggregate(CORPUS, "mois")
check("regroupement par mois : deux groupes", len(par_mois) == 2, str(len(par_mois)))
check("les groupes sont ordonnes", par_mois[0]["groupe"] == "2026-07")
par_etoiles = server._aggregate(CORPUS, "etoiles")
check("regroupement par etoiles : deux groupes", len(par_etoiles) == 2)
par_reponse = server._aggregate(CORPUS, "reponse")
check("regroupement par presence de reponse", len(par_reponse) == 2)

par_theme = server._aggregate(CORPUS, "theme")
groupes = {str(l["groupe"]): l for l in par_theme}
check("le theme delai est detecte sur 4 avis", groupes["livraison_delai"]["nb_avis"] == 4,
      str(groupes.get("livraison_delai", {}).get("nb_avis")))
check("les non-classes sont comptes a part", "(non classe)" in groupes)
check(
    "les non-classes sont les 6 avis 'Parfait'",
    groupes["(non classe)"]["nb_avis"] == 6,
    str(groupes["(non classe)"]["nb_avis"]),
)
check(
    "la note du theme delai est celle de ses avis",
    groupes["livraison_delai"]["note_moyenne"] == 1.0,
    str(groupes["livraison_delai"]["note_moyenne"]),
)

try:
    server._aggregate(CORPUS, "n_importe_quoi")
    check("un group_by inconnu est refuse", False, "accepte a tort")
except server.TrustpilotError as exc:
    check("un group_by inconnu est refuse ET les valeurs sont listees", "mois" in str(exc))

# --------------------------------------------------------------------------
section("11. Le cache local SQLite")
# --------------------------------------------------------------------------

conn = vu_cache.open_rw(SANDBOX / "test_cache.sqlite")
ecrits = vu_cache.upsert(
    conn, "reviews", CORPUS, server._cache_key, server.NUMERIC_COLUMNS, server.INDEX_COLUMNS
)
conn.commit()
check("les avis sont ecrits en cache", ecrits == 10, str(ecrits))
# Reecrire le meme lot ne doit PAS creer de doublon : c'est ce qui permet de
# dire a l'utilisateur qu'en cas de doute il relance la synchro.
vu_cache.upsert(
    conn, "reviews", CORPUS, server._cache_key, server.NUMERIC_COLUMNS, server.INDEX_COLUMNS
)
conn.commit()
total_cache = conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
check("resynchroniser ne cree pas de doublon", total_cache == 10, str(total_cache))
moyenne = conn.execute("SELECT ROUND(AVG(stars),2) FROM reviews").fetchone()[0]
check(
    "la note est typee en nombre : AVG fonctionne",
    moyenne == 3.4,
    str(moyenne),
)
conn.close()

for interdit in (
    "DELETE FROM reviews",
    "DROP TABLE reviews",
    "UPDATE reviews SET stars=5",
    "INSERT INTO reviews VALUES (1)",
    "ATTACH DATABASE 'x' AS y",
):
    try:
        vu_cache.guard_sql(interdit)
        check(f"SQL en ecriture refuse : {interdit.split()[0]}", False, "accepte a tort")
    except vu_cache.CacheError:
        check(f"SQL en ecriture refuse : {interdit.split()[0]}", True)
check(
    "un SELECT passe",
    vu_cache.guard_sql("SELECT * FROM reviews").startswith("SELECT"),
)
check(
    "un WITH passe",
    vu_cache.guard_sql("WITH x AS (SELECT 1) SELECT * FROM x").startswith("WITH"),
)

# --------------------------------------------------------------------------
section("12. Surface MCP")
# --------------------------------------------------------------------------

outils = [n for n in dir(server) if n.startswith("trustpilot_")]
check("22 outils exposes", len(outils) == 22, str(len(outils)))
for attendu in (
    "trustpilot_setup_status",
    "trustpilot_doctor",
    "trustpilot_business_units",
    "trustpilot_list_reviews",
    "trustpilot_summary",
    "trustpilot_themes",
    "trustpilot_verbatims",
    "trustpilot_transporteurs",
    "trustpilot_find_review",
    "trustpilot_export_csv",
    "trustpilot_sync",
    "trustpilot_sql",
    "trustpilot_export_sql",
):
    check(f"outil {attendu} expose", attendu in outils, ", ".join(sorted(outils)))

import inspect  # noqa: E402

mauvaise_signature = [
    nom
    for nom in outils
    if list(inspect.signature(getattr(server, nom)).parameters) in (["args", "kwargs"],)
]
check(
    "aucun outil ne publie une signature (args, kwargs)",
    not mauvaise_signature,
    ", ".join(mauvaise_signature),
)

# Aucun outil ne doit accepter un secret en parametre : ce serait le faire
# passer par le fil de la conversation, donc dans le contexte du modele et
# dans la transcription - exactement ce que le champ `sensitive` evite.
suspects = []
for nom in outils:
    for param in inspect.signature(getattr(server, nom)).parameters:
        if any(m in param.lower() for m in ("key", "secret", "token", "password")):
            suspects.append(f"{nom}({param})")
check("aucun outil n'accepte un secret en parametre", not suspects, ", ".join(suspects))

# --------------------------------------------------------------------------
section("13. Degradation sans secret, et messages d'erreur")
# --------------------------------------------------------------------------

check("sans cle ni secret, le chemin prive est ferme", not server._has_private())
check("et aucun grant n'est retenu", server._oauth_grant() == "")
os.environ["TRUSTPILOT_API_KEY"] = "cle-de-test"
check("avec la cle seule, le prive reste ferme", not server._has_private())
os.environ["TRUSTPILOT_API_SECRET"] = "secret-de-test"
check(
    "avec la cle ET le secret, le grant est client_credentials",
    server._oauth_grant() == "client_credentials",
    server._oauth_grant(),
)
os.environ["TRUSTPILOT_REFRESH_TOKEN"] = "jeton-de-test"
check(
    "un refresh token pose passe devant client_credentials",
    server._oauth_grant() == "refresh_token",
    server._oauth_grant(),
)
del os.environ["TRUSTPILOT_REFRESH_TOKEN"]
del os.environ["TRUSTPILOT_API_SECRET"]

# La recherche par numero de commande doit DIRE qu'elle a besoin du secret,
# pas echouer sur un message technique : c'est le cas d'usage principal du
# connecteur pour l'equipe Transport.
reponse = server.trustpilot_find_review(reference_id="CMD-1")
check(
    "find_review sans secret explique ce qui manque",
    "secret" in reponse.lower() and "ERREUR" in reponse,
    reponse[:160],
)
check(
    "et dit qu'il n'y a pas de contournement public",
    "public" in reponse.lower(),
    reponse[:200],
)
del os.environ["TRUSTPILOT_API_KEY"]

# Le fichier d'equipe refuse le refresh token : c'est un choix, pas un oubli.
check(
    "TRUSTPILOT_REFRESH_TOKEN est refuse dans le fichier d'equipe",
    "TRUSTPILOT_REFRESH_TOKEN" not in server.SHARED_ALLOWED_KEYS,
)
check(
    "la cle et le secret y sont acceptes",
    {"TRUSTPILOT_API_KEY", "TRUSTPILOT_API_SECRET"} <= server.SHARED_ALLOWED_KEYS,
)
check(
    "un chemin explicite de fichier d'equipe est EXCLUSIF",
    len(server._shared_env_candidates()) == 1,
    str(server._shared_env_candidates()),
)

# --------------------------------------------------------------------------
section("14. Purete du flux stdio, et non-divulgation des secrets")
# --------------------------------------------------------------------------
# Le serveur MCP parle JSON-RPC sur stdout : une seule ligne egaree casse le
# protocole en silence.

capture = io.StringIO()
vrai_stdout, sys.stdout = sys.stdout, capture
try:
    server.trustpilot_lexique("themes")
    server.trustpilot_lexique("")
    server._render_table([AVIS], "entete", "", "id,stars")
    server._classify("Livraison en retard", "fr")
finally:
    sys.stdout = vrai_stdout
check(
    "aucun outil n'ecrit sur stdout",
    capture.getvalue() == "",
    repr(capture.getvalue()[:80]),
)

# La propriete a tenir n'est pas un libelle, c'est une ABSENCE : un secret pose
# dans la configuration ne doit jamais ressortir d'un diagnostic, meme
# partiellement.
FAUX_SECRETS = {
    "TRUSTPILOT_API_KEY": "cle-qui-ne-doit-jamais-ressortir-0123456789",
    "TRUSTPILOT_API_SECRET": "secret-qui-ne-doit-jamais-ressortir-987654",
    "TRUSTPILOT_REFRESH_TOKEN": "jeton-qui-ne-doit-jamais-ressortir-abcdef",
}
os.environ.update(FAUX_SECRETS)
try:
    rapports = [
        ("setup_status", server.trustpilot_setup_status()),
        ("doctor", server.trustpilot_doctor()),
    ]
finally:
    for nom in FAUX_SECRETS:
        os.environ.pop(nom, None)
for quoi, rapport in rapports:
    for nom, valeur in FAUX_SECRETS.items():
        check(f"{quoi} ne rend pas {nom} en clair", valeur not in rapport)
        check(
            f"{quoi} n'en rend pas non plus un fragment utilisable ({nom})",
            valeur[:12] not in rapport,
        )
    check(
        f"{quoi} dit quand meme d'ou vient la cle",
        "rigine" in rapport,
        rapport[:120],
    )
check(
    "doctor ne tente aucun appel prive quand le secret manque",
    True,
)

# --------------------------------------------------------------------------
print()
print("=" * 68)
print(f"{PASS} controle(s) passes, {len(FAIL)} echec(s)")
for nom in FAIL:
    print(f"  ECHEC : {nom}")
print()
print(
    "Rappel : ces controles ne touchent pas l'API Trustpilot. Le connecteur "
    "n'est pas verifie tant qu'une recette n'a pas ete passee contre le vrai "
    "compte, avec une cle valide - et la liste blanche des chemins, ecrite a la "
    "main faute de contrat OpenAPI publie, n'est confirmee que par cette "
    "recette."
)
shutil.rmtree(SANDBOX, ignore_errors=True)
sys.exit(1 if FAIL else 0)
