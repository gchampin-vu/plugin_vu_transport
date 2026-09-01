#!/usr/bin/env python3
"""
Controles du serveur MCP shiptify, SANS RESEAU et SANS CLE.

    python test_offline.py

Ce que ca verifie : la liste blanche des chemins GET, la pagination et ses trois
raisons d'arret, le budget d'affichage, l'aplatissement, la projection de
colonnes, le lexique et la resolution des noms maison, l'agregation, le cache
local SQLite, et la purete du flux stdio.

Ce que ca ne verifie PAS : que l'API Shiptify rend ce qu'on croit. Ca, c'est la
recette contre le vrai compte, et elle demande une cle.

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

SANDBOX = pathlib.Path(os.environ.get("TEMP") or "/tmp") / "shiptify-mcp-test"
shutil.rmtree(SANDBOX, ignore_errors=True)
SANDBOX.mkdir(parents=True, exist_ok=True)

# Un environnement propre : aucune cle, aucun fichier de configuration lu, et
# surtout AUCUN fichier d'equipe. Sur un poste qui synchronise la bibliotheque,
# 08_ENGINE/04_mcp/00_config/shiptify.shared.env serait trouve et lu au niveau 2
# de _env, et les controles tourneraient avec la vraie cle de service - donc ne
# prouveraient rien. Un chemin explicite est exclusif depuis le 2026-08-28,
# c'est ce qui rend cette isolation fiable.
for name in list(os.environ):
    if name.startswith("SHIPTIFY_"):
        del os.environ[name]
os.environ["SHIPTIFY_ENV_FILE"] = str(SANDBOX / "_inexistant_.env")
os.environ["SHIPTIFY_SHARED_ENV"] = str(SANDBOX / "_inexistant_.shared.env")
os.environ["SHIPTIFY_HOME"] = str(SANDBOX)
os.environ.pop("VU_ENGINE_DIR", None)
os.environ.pop("CLAUDE_PLUGIN_ROOT", None)
os.environ.pop("CLAUDE_PLUGIN_DATA", None)

import server  # noqa: E402

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
# vu_cache.py est DUPLIQUE a l'identique dans les trois plugins : un plugin
# Claude Code est autonome et ne peut pas importer son voisin. L'empreinte
# ci-dessous est la contrepartie de cette duplication : si quelqu'un corrige le
# module dans un seul plugin, ce controle le dit, au lieu de laisser les trois
# copies diverger en silence.
#
# En cas d'echec : recopier le fichier dans les trois plugins, puis mettre a
# jour l'empreinte dans les trois test_offline.py.

EMPREINTE_VU_CACHE = "d346b3cfc1ac0e21ab44640a6004827f564dbd5b3b9cfaf865ce36f24204fdb2"
_brut = (HERE / "vu_cache.py").read_bytes().replace(b"\r\n", b"\n")
check(
    "vu_cache.py est la version partagee attendue",
    hashlib.sha256(_brut).hexdigest() == EMPREINTE_VU_CACHE,
    hashlib.sha256(_brut).hexdigest(),
)

# --------------------------------------------------------------------------
section("2. Liste blanche des chemins GET")
# --------------------------------------------------------------------------

check("le contrat est charge", len(server._spec()["paths"]) > 50)
check("/shipments/ est accepte", server._check_get_path("/shipments/") == "/shipments/")
check("le slash manquant est tolere", server._check_get_path("shipments/") == "/shipments/")
for mauvais, pourquoi in (
    ("/inconnu", "chemin absent du contrat"),
    ("/shipments/?limit=10", "parametres dans le chemin"),
):
    try:
        server._check_get_path(mauvais)
        check(f"refus de {mauvais} ({pourquoi})", False, "accepte a tort")
    except server.ShiptifyError:
        check(f"refus de {mauvais} ({pourquoi})", True)

check("/shipments/ est pagine", server._supports_paging("/shipments/"))
check(
    "/carriers/active ne l'est pas",
    not server._supports_paging("/carriers/active"),
)
check("/visits accepte 200 par page", server._page_limit_for("/visits") == 200)
check("les autres plafonnent a 100", server._page_limit_for("/shipments/") == 100)

# --------------------------------------------------------------------------
section("3. Pagination : les trois raisons d'arret")
# --------------------------------------------------------------------------

APPELS: list[dict] = []


def faux_request(path, query=None):
    APPELS.append(dict(query or {}))
    offset = int((query or {}).get("offset") or 0)
    limite = int((query or {}).get("limit") or 100)
    restant = max(0, FAUX_TOTAL - offset)
    n = min(limite, restant)
    return [
        {
            "id": offset + i,
            "code": f"SH{offset + i}",
            "cost": "0.000",
            "price": 100 + i,
            "created_at": "2026-07-15T08:00:00Z",
            "date": "2026-08-02T08:00:00Z",
            "carrier": {"name": "Sennder I FR Paris"},
            "address_dest": {"name": "VIR Log I Cestas", "country": "FR", "city": "Cestas"},
        }
        for i in range(n)
    ]


server._request = faux_request

FAUX_TOTAL = 250
APPELS.clear()
rows, stop = server._paginate("/shipments/", {}, max_rows=1000)
check("lecture complete : 250 lignes", len(rows) == 250, str(len(rows)))
check("lecture complete : aucune raison d'arret", stop == "", repr(stop))

FAUX_TOTAL = 1000
APPELS.clear()
rows, stop = server._paginate("/shipments/", {}, max_rows=250)
check("plafond max_rows respecte", len(rows) == 250, str(len(rows)))
check("et la raison est max_rows", stop == "max_rows", repr(stop))

# Le controle qui a motive la correction du 2026-08-28 : un perimetre qui fait
# EXACTEMENT max_rows lignes et qui est complet ne doit PAS etre dit tronque.
# Un rendu tronque n'est pas citable, donc un chiffre juste devenait inutile.
FAUX_TOTAL = 200
APPELS.clear()
rows, stop = server._paginate("/shipments/", {}, max_rows=200)
check("un perimetre pile a max_rows n'est PAS dit tronque", stop == "", repr(stop))
check("et il rend bien ses 200 lignes", len(rows) == 200, str(len(rows)))

FAUX_TOTAL = 100000
APPELS.clear()
rows, stop = server._paginate(
    "/shipments/",
    {},
    max_rows=100000,
    render_fields=server.SHIPMENT_BRIEF,
    render_budget=5000,
)
check("le budget d'affichage arrete la lecture", stop == "budget", repr(stop))
check(
    "et il l'arrete VITE : une page suffisait",
    len(APPELS) <= 2,
    f"{len(APPELS)} appel(s)",
)

# --------------------------------------------------------------------------
section("4. Aplatissement et projection")
# --------------------------------------------------------------------------

plat = server._flatten(
    {"id": 1, "carrier": {"name": "X"}, "tags": [], "contents": [{"a": 1}]}
)
check("les objets imbriques deviennent des colonnes pointees", plat["carrier.name"] == "X")
check("une liste vide devient une chaine vide", plat["tags"] == "")
check("une liste pleine devient du JSON", plat["contents"].startswith("["))

projete = server._select([plat], "id,carrier.name")
check("la projection garde l'ordre demande", list(projete[0]) == ["id", "carrier.name"])
check("une colonne absente devient vide", server._select([plat], "absente")[0]["absente"] == "")

csv_texte = server._to_csv_text([{"a": 1, "b": 2}])
check("le CSV est en point-virgule", csv_texte.splitlines()[0] == "a;b")

# --------------------------------------------------------------------------
section("5. Le rendu dit la verite sur ce qu'il montre")
# --------------------------------------------------------------------------

rendu = server._render_table([{"id": 1}], "entete", "max_rows", "id")
check(
    "une lecture tronquee crie, et interdit de citer le compte",
    "ATTENTION" in rendu and "n'est PAS un total" in rendu,
)
check("et elle oriente vers l'agregation", "shiptify_summary" in rendu)
rendu = server._render_table([{"id": 1}], "entete", "", "id")
check(
    "une lecture complete le dit, et autorise a citer",
    "complete" in rendu and "citable" in rendu,
)
rendu = server._render_table([{"id": 1}], "entete", "budget", "id")
check(
    "l'arret budget explique qu'il n'a rien coute d'inutile",
    "n'auraient pas ete affichees" in rendu,
)

# --------------------------------------------------------------------------
section("6. Le lexique et la resolution des noms maison")
# --------------------------------------------------------------------------

check("le lexique est livre et lisible", bool(server._lexique()), server._LEXIQUE_ERROR)
check("il porte un numero de version", bool(server._lexique().get("version")))

# Le referentiel vivant est remplace : ce test ne parle pas a l'API.
server._CARRIERS = [
    {"id": 3509, "name": "XPO I FR Villepinte", "code": "x1"},
    {"id": 1695, "name": "XPO I ES Barcelona", "code": "x2"},
    {"id": 2777, "name": "Sennder I FR Paris", "code": "s1"},
]

res = server._resolve("VIR")
check("« VIR » est connu du lexique", res["connu_du_lexique"])
check(
    "et il pointe address_dest.name, PAS carrier.name",
    res["champ"] == "address_dest.name",
    res["champ"],
)
check(
    "et il cherche les trois libelles VIR / JP HOME / JPH",
    {"VIR", "JP HOME", "JPH"} <= set(res["motifs"]),
    str(res["motifs"]),
)

res = server._resolve("XPO")
check("« XPO » resout plusieurs entites", len(res["carriers"]) == 2, str(res["carriers"]))
check(
    "et il previent qu'un seul identifiant sous-compte",
    any("sous-compte" in n for n in res["notes"]),
)

# Un transporteur du portefeuille absent du referentiel Shiptify doit etre
# EXPLIQUE, jamais rendu vide : une liste vide se lit comme « il n'y a pas de
# flux », et c'est faux - c'est la mauvaise source. Deux chemins menent la, et
# les deux doivent tenir.
#
# Chemin 1 : le terme est dans le lexique, qui dit lui-meme ou aller.
res = server._resolve("TAMDIS")
check(
    "un transporteur connu du lexique dit ou est la reponse",
    res["connu_du_lexique"] and any("Yooz" in n for n in res["notes"]),
    str(res["notes"]),
)
check(
    "et il porte son rattachement de groupe",
    "Perrenot" in res["canonique"],
    res["canonique"],
)

# Chemin 2 : le terme n'est PAS dans le lexique, mais il est dans l'annuaire du
# portefeuille. C'est le repli, et c'est lui qui evite le tableau vide.
res = server._resolve("Cogepart")
check(
    "un transporteur du portefeuille absent du lexique est quand meme explique",
    not res["connu_du_lexique"]
    and any("mauvaise source" in n for n in res["notes"]),
    str(res["notes"]),
)

# Chemin 3 : un terme inconnu partout ne doit pas inventer une explication.
res = server._resolve("Zzzz Transport")
check(
    "un terme inconnu partout renvoie vers le referentiel reel",
    any("shiptify_list_carriers" in n for n in res["notes"]),
    str(res["notes"]),
)

check(
    "le filtre cote client trouve JP HOME quand on cherche VIR",
    server._row_matches(
        {"address_dest.name": "JP Home center HUB"}, "address_dest.name", ["VIR", "JP HOME"]
    ),
)
check(
    "et il n'attrape pas un libelle sans rapport",
    not server._row_matches(
        {"address_dest.name": "Sennder I FR Paris"}, "address_dest.name", ["VIR", "JP HOME"]
    ),
)

# --------------------------------------------------------------------------
section("7. Agregation")
# --------------------------------------------------------------------------

echantillon = [
    {"carrier.name": "A", "price": "10", "created_at": "2026-07-05T00:00:00Z"},
    {"carrier.name": "A", "price": "20", "created_at": "2026-07-06T00:00:00Z"},
    {"carrier.name": "B", "price": "", "created_at": "2026-08-01T00:00:00Z"},
]
groupes, total, label = server._aggregate(echantillon, "carrier.name", "price", 10)
check("le regroupement compte les lignes", total["nb"] == 3, str(total))
check("il somme la mesure", total["somme"] == 30.0, str(total))
check(
    "et il ISOLE les lignes sans valeur",
    total["nb_sans_valeur"] == 1,
    str(total),
)
check("la moyenne ne divise que par les lignes mesurees", groupes[0]["moyenne"] == 15.0,
      str(groupes[0]))

groupes, total, label = server._aggregate(echantillon, "mois", "nb", 10, "created_at")
check("le regroupement mensuel nomme sa colonne de date", "created_at" in label, label)
check(
    "et il est trie dans l'ordre du TEMPS, pas du volume",
    [g[label] for g in groupes] == ["2026-07", "2026-08"],
    str([g[label] for g in groupes]),
)

# --------------------------------------------------------------------------
section("8. Le cache local")
# --------------------------------------------------------------------------

import vu_cache  # noqa: E402

conn = vu_cache.open_rw(server._cache_path())
try:
    ecrites = vu_cache.upsert(
        conn,
        "shipments",
        [server._flatten(r) for r in faux_request("/shipments/", {"limit": 3})],
        server._cache_key,
        server.NUMERIC_COLUMNS,
        ("created_at",),
    )
    conn.commit()
    check("la synchronisation ecrit dans le cache", ecrites == 3, str(ecrites))
    # Rejouer la meme page ne doit pas creer de doublon : c'est ce qui permet de
    # dire a l'utilisateur qu'en cas de doute il relance une synchro complete.
    vu_cache.upsert(
        conn,
        "shipments",
        [server._flatten(r) for r in faux_request("/shipments/", {"limit": 3})],
        server._cache_key,
        server.NUMERIC_COLUMNS,
    )
    conn.commit()
    total_cache = conn.execute("SELECT COUNT(*) FROM shipments").fetchone()[0]
    check("et la rejouer ne cree PAS de doublon", total_cache == 3, str(total_cache))
    check(
        "les montants sont types en numerique, pas en texte",
        isinstance(conn.execute("SELECT price FROM shipments LIMIT 1").fetchone()[0], float),
    )
finally:
    conn.close()

sortie = server.shiptify_sql("SELECT * FROM shipments", max_rows=2)
check("le SQL du cache repond", "ligne(s) rendues" in sortie, sortie[:120])
check(
    "et il annonce le VRAI total, pas le nombre de lignes affichees",
    "3 ligne(s) au total" in sortie,
    sortie[:300],
)
for interdit in ("DELETE FROM shipments", "DROP TABLE shipments", "UPDATE shipments SET id=1"):
    reponse = server.shiptify_sql(interdit)
    check(f"SQL en ecriture refuse : {interdit.split()[0]}", "ERREUR" in reponse, reponse[:80])

# --------------------------------------------------------------------------
section("9. Surface MCP")
# --------------------------------------------------------------------------

tools = [n for n in dir(server) if n.startswith("shiptify_")]
check("28 outils exposes", len(tools) == 28, str(len(tools)))
for attendu in (
    "shiptify_summary",
    "shiptify_lexique",
    "shiptify_resolve",
    "shiptify_sync",
    "shiptify_sql",
    "shiptify_tables",
    "shiptify_columns",
    "shiptify_export_sql",
):
    check(f"outil {attendu} expose", attendu in tools, ", ".join(sorted(tools)))

import inspect  # noqa: E402

mauvaise_signature = [
    nom
    for nom in tools
    if list(inspect.signature(getattr(server, nom)).parameters) in (["args", "kwargs"],)
]
check(
    "aucun outil ne publie une signature (args, kwargs)",
    not mauvaise_signature,
    ", ".join(mauvaise_signature),
)

# --------------------------------------------------------------------------
section("10. Purete du flux stdio")
# --------------------------------------------------------------------------
# Le serveur MCP parle JSON-RPC sur stdout : une seule ligne egaree casse le
# protocole en silence.

capture = io.StringIO()
vrai_stdout, sys.stdout = sys.stdout, capture
try:
    server.shiptify_lexique("alias")
    server.shiptify_resolve("XPO")
    server._render_table([{"id": 1}], "entete", "", "id")
finally:
    sys.stdout = vrai_stdout
check("aucun outil n'ecrit sur stdout", capture.getvalue() == "", repr(capture.getvalue()[:80]))

# La propriete a tenir n'est pas un libelle, c'est une absence : une cle posee
# dans la configuration ne doit jamais ressortir en clair d'un diagnostic, meme
# partiellement. On en pose donc une fausse et on verifie qu'on ne la retrouve
# pas - ni entiere, ni sur ses douze premiers caracteres.
FAUSSE_CLE = "cle-de-test-ne-doit-jamais-ressortir-0123456789"
os.environ["SHIPTIFY_API_KEY"] = FAUSSE_CLE
try:
    rapports = [server.shiptify_setup_status(), server.shiptify_doctor()]
finally:
    del os.environ["SHIPTIFY_API_KEY"]
for i, rapport in enumerate(rapports):
    quoi = ("setup_status", "doctor")[i]
    check(
        f"{quoi} ne rend pas la cle en clair",
        FAUSSE_CLE not in rapport,
    )
    check(
        f"{quoi} n'en rend pas non plus un fragment utilisable",
        FAUSSE_CLE[:12] not in rapport,
    )
    check(
        f"{quoi} dit quand meme d'ou vient la cle",
        "Origine" in rapport or "origine" in rapport,
        rapport[:120],
    )

# --------------------------------------------------------------------------
print()
print("=" * 60)
print(f"{PASS} controle(s) passes, {len(FAIL)} echec(s)")
for nom in FAIL:
    print(f"  ECHEC : {nom}")
print()
print(
    "Rappel : ces controles ne touchent pas l'API Shiptify. Le connecteur n'est "
    "pas verifie tant qu'une recette n'a pas ete passee contre le vrai compte, "
    "avec une cle valide."
)
shutil.rmtree(SANDBOX, ignore_errors=True)
sys.exit(1 if FAIL else 0)
