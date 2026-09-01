#!/usr/bin/env python3
"""
Tests hors ligne du connecteur reflex-wms. Aucune connexion a la base.

    python test_offline.py

Ce qui est verifie ici, c'est ce qui doit tenir MEME sans reseau, et surtout
ce qui protege la production : l'analyseur de lecture seule, l'exigence de
WITH (NOLOCK), le refus des jointures sans condition, et l'integrite du
catalogue MLD embarque.

Un garde-fou qui n'est pas teste n'est pas un garde-fou : c'est une intention.
"""

from __future__ import annotations

import os
import pathlib
import sys

# Les tests ne doivent dependre ni de la config du poste, ni de celle de
# l'equipe : on neutralise avant d'importer le serveur.
os.environ["REFLEX_SHARED_ENV"] = str(pathlib.Path(__file__).with_name("__absent__"))
os.environ["REFLEX_ENV_FILE"] = str(pathlib.Path(__file__).with_name("__absent__"))

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


def accepte(label: str, sql: str) -> None:
    try:
        server.assert_read_only(sql)
        check(label, True)
    except server.ReflexError as exc:
        check(label, False, f"refusee a tort : {exc}".splitlines()[0])


def refuse(label: str, sql: str, attendu: str = "") -> None:
    try:
        server.assert_read_only(sql)
        check(label, False, "acceptee a tort")
    except server.ReflexError as exc:
        check(label, not attendu or attendu.lower() in str(exc).lower(),
              f"refusee mais pas pour la bonne raison : {str(exc).splitlines()[0]}")


# --------------------------------------------------------------------------
# 1. L'analyseur de lecture seule
# --------------------------------------------------------------------------

accepte(
    "SELECT simple",
    "SELECT TOP 10 pe.PENPRE FROM reflex.HLPRENP pe WITH (NOLOCK) WHERE pe.PECDPO = 'AMB'",
)
accepte(
    "CTE",
    "WITH b AS (SELECT PENPRE FROM reflex.HLPRENP WITH (NOLOCK) WHERE PECDPO = 'AMB') "
    "SELECT COUNT(*) FROM b",
)
accepte(
    "point-virgule final tolere",
    "SELECT 1 FROM reflex.HLPRENP WITH (NOLOCK);",
)
accepte(
    "table hors schema reflex : pas de NOLOCK exige",
    "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES",
)
accepte(
    "mot-cle dans un litteral",
    "SELECT PENPRE FROM reflex.HLPRENP WITH (NOLOCK) WHERE PECDPO = 'DROP'",
)
accepte(
    "mot-cle dans un commentaire",
    "-- on ne fait surtout pas de DELETE ici\n"
    "SELECT PENPRE FROM reflex.HLPRENP WITH (NOLOCK)",
)
accepte(
    "sous-requete avec NOLOCK",
    "SELECT a.PENPRE FROM reflex.HLPRENP a WITH (NOLOCK) "
    "INNER JOIN (SELECT P1NPRE FROM reflex.HLPRPLP WITH (NOLOCK)) b ON b.P1NPRE = a.PENPRE",
)

refuse("UPDATE", "UPDATE reflex.HLPRENP SET PECDPO = 'X'", "SELECT ou WITH")
refuse("DELETE", "DELETE FROM reflex.HLPRENP", "SELECT ou WITH")
refuse("INSERT", "INSERT INTO reflex.HLPRENP VALUES (1)", "SELECT ou WITH")
refuse("DROP", "DROP TABLE reflex.HLPRENP", "SELECT ou WITH")
refuse("EXEC", "EXEC sp_who2", "SELECT ou WITH")
refuse(
    "deux instructions",
    "SELECT 1 FROM reflex.HLPRENP WITH (NOLOCK); DROP TABLE reflex.HLPRENP",
    "une seule instruction",
)
refuse(
    "SELECT INTO",
    "SELECT PENPRE INTO #t FROM reflex.HLPRENP WITH (NOLOCK)",
    "INTO",
)
refuse(
    "procedure systeme derriere un SELECT",
    "SELECT * FROM sp_helpdb",
    "procedure systeme",
)
refuse(
    "commentaire de bloc qui cache une ecriture",
    "/* SELECT */ DELETE FROM reflex.HLPRENP",
    "SELECT ou WITH",
)
refuse("requete vide", "   ", "vide")

# --------------------------------------------------------------------------
# 2. WITH (NOLOCK) exige
# --------------------------------------------------------------------------

refuse(
    "FROM sans NOLOCK",
    "SELECT PENPRE FROM reflex.HLPRENP",
    "NOLOCK",
)
refuse(
    "FROM avec alias mais sans NOLOCK",
    "SELECT pe.PENPRE FROM reflex.HLPRENP pe",
    "NOLOCK",
)
refuse(
    "JOIN sans NOLOCK alors que le FROM en a un",
    "SELECT a.PENPRE FROM reflex.HLPRENP a WITH (NOLOCK) "
    "INNER JOIN reflex.HLPRPLP b ON b.P1NPRE = a.PENPRE",
    "NOLOCK",
)
# Le message doit nommer la table fautive : sur une requete de trente lignes,
# "il manque un NOLOCK quelque part" ne sert a rien.
try:
    server.assert_read_only(
        "SELECT a.PENPRE FROM reflex.HLPRENP a WITH (NOLOCK) "
        "INNER JOIN reflex.HLPRPLP b ON b.P1NPRE = a.PENPRE"
    )
    check("le message nomme la table fautive", False, "acceptee a tort")
except server.ReflexError as exc:
    check("le message nomme la table fautive", "HLPRPLP" in str(exc), str(exc).splitlines()[0])

# Desactivable, parce qu'un poste peut avoir une bonne raison ponctuelle.
os.environ["REFLEX_REQUIRE_NOLOCK"] = "0"
server._SHARED_CACHE = None
accepte("NOLOCK non exige quand REFLEX_REQUIRE_NOLOCK=0", "SELECT PENPRE FROM reflex.HLPRENP")
os.environ["REFLEX_REQUIRE_NOLOCK"] = "1"

# --------------------------------------------------------------------------
# 3. Jointures sans condition
# --------------------------------------------------------------------------

refuse(
    "JOIN sans ON",
    "SELECT a.PENPRE FROM reflex.HLPRENP a WITH (NOLOCK) "
    "INNER JOIN reflex.HLPRPLP b WITH (NOLOCK)",
    "sans ON",
)
refuse(
    "jointure implicite par virgule",
    "SELECT a.PENPRE FROM reflex.HLPRENP a WITH (NOLOCK), reflex.HLPRPLP b WITH (NOLOCK)",
    "virgule",
)

# --------------------------------------------------------------------------
# 4. Le choix de la base
# --------------------------------------------------------------------------

check("base 'prod'", server._database("prod") == server.DEFAULT_DATABASE)
check("base 'epu'", server._database("epu") == server.DEFAULT_DATABASE_EPU)
try:
    server._database("recette")
    check("base inconnue refusee", False, "acceptee a tort")
except server.ReflexError:
    check("base inconnue refusee", True)

# La cascade : epuration D'ABORD, courante ensuite. L'ordre est la regle
# metier, pas un detail d'implementation - un test le fige.
check("cascade par defaut = epu puis prod", server._base_cascade("") == ["epu", "prod"])
check("cascade 'auto' = epu puis prod", server._base_cascade("auto") == ["epu", "prod"])
check("cascade 'epu' = epu seule", server._base_cascade("epu") == ["epu"])
check("cascade 'prod' = prod seule", server._base_cascade("prod") == ["prod"])

# La base retenue doit toujours etre dite : un chiffre dont on ignore de quelle
# base il sort n'est pas citable.
note = server._cascade_note("RFXCAFPRDEPU", ["RFXCAFPRDEPU", "RFXCAFPRDDAT"], [[1]])
check("la note nomme la base retenue", "RFXCAFPRDEPU" in note and "cascade" in note)

# Le piege du referentiel : quand c'est l'EPURATION qui a repondu, la note doit
# avertir. Mesure du 2026-09-01 sur HLDEPPP - l'epuration rend MOR (site ferme)
# et pas AUV. Sans cet avertissement, la liste est fausse en silence.
check("l'epuration qui repond declenche l'avertissement referentiel",
      "REFERENTIEL" in note, note.splitlines()[-1][:70])
note_prod = server._cascade_note("RFXCAFPRDDAT", ["RFXCAFPRDEPU", "RFXCAFPRDDAT"], [[1]])
check("la base courante ne declenche pas l'avertissement",
      "REFERENTIEL" not in note_prod)
vide = server._cascade_note("RFXCAFPRDDAT", ["RFXCAFPRDEPU", "RFXCAFPRDDAT"], [])
check("le vide sur les deux bases est explicite", "AUCUNE" in vide)
seule = server._cascade_note("RFXCAFPRDDAT", ["RFXCAFPRDDAT"], [[1]])
check("base forcee : pas de mention de cascade", "cascade" not in seule)

# --------------------------------------------------------------------------
# 5. Les garde-fous sont bien bornes
# --------------------------------------------------------------------------

os.environ["REFLEX_QUERY_TIMEOUT_S"] = "99999"
check(
    "le delai est plafonne en dur",
    server._query_timeout() == server.MAX_QUERY_TIMEOUT_S,
    f"vaut {server._query_timeout()}",
)
del os.environ["REFLEX_QUERY_TIMEOUT_S"]
check("delai par defaut", server._query_timeout() == server.DEFAULT_QUERY_TIMEOUT_S)
check("gouverneur de cout actif par defaut", server._cost_limit() == server.DEFAULT_COST_LIMIT)
check("NOLOCK exige par defaut", server._require_nolock() is True)

# L'identifiant de service de l'equipe vit dans le fichier partage depuis le
# 2026-09-01 : l'instance refuse l'authentification Windows, il n'y a qu'un
# compte, et toute l'equipe l'a deja.
check("REFLEX_USER lisible depuis la config d'equipe",
      "REFLEX_USER" in server.SHARED_ALLOWED_KEYS)
check("REFLEX_PASSWORD lisible depuis la config d'equipe",
      "REFLEX_PASSWORD" in server.SHARED_ALLOWED_KEYS)

# En contrepartie, la VALEUR du mot de passe ne doit jamais s'afficher. Le nom
# de la cle, si : c'est ce qui permet de savoir d'ou vient la valeur active.
check("le mot de passe est declare secret",
      "REFLEX_PASSWORD" in server.SHARED_SECRET_KEYS)
check("le compte n'est pas un secret", "REFLEX_USER" not in server.SHARED_SECRET_KEYS)

# Un chemin local n'a toujours rien a faire dans un fichier partage par 26
# postes : ce qui vaut sur l'un n'existe pas sur les vingt-cinq autres.
check("REFLEX_EXPORT_DIR reste interdit dans la config d'equipe",
      "REFLEX_EXPORT_DIR" in server.SHARED_FORBIDDEN_KEYS)
check("liste blanche et liste noire disjointes",
      not (server.SHARED_ALLOWED_KEYS & server.SHARED_FORBIDDEN_KEYS))

# Le test qui compte vraiment : un mot de passe pose dans l'environnement ne
# doit ressortir NI dans le diagnostic, NI dans la chaine de connexion rendue
# a l'ecran. On en pose un factice et on verifie qu'on ne le retrouve pas.
_SENTINELLE = "MotDePasseFactice-NeDoitJamaisSortir"
os.environ["REFLEX_AUTH"] = "sql"
os.environ["REFLEX_USER"] = "compte_de_test"
os.environ["REFLEX_PASSWORD"] = _SENTINELLE
server._SHARED_CACHE = None
statut = server.reflex_setup_status.fn() if hasattr(server.reflex_setup_status, "fn") else server.reflex_setup_status()
check("le mot de passe ne fuit pas dans le statut", _SENTINELLE not in statut)
check("le compte s'affiche, lui", "compte_de_test" in statut)
try:
    chaine = server._conn_str(mask=True)
    check("le mot de passe ne fuit pas dans la chaine masquee", _SENTINELLE not in chaine)
    check("la chaine masquee montre bien un PWD", "PWD=" in chaine)
    check("le mot de passe EST present sans masque", _SENTINELLE in server._conn_str(mask=False))
except server.ConfigError as exc:
    check("chaine de connexion construite", False, str(exc).splitlines()[0])
for _cle in ("REFLEX_AUTH", "REFLEX_USER", "REFLEX_PASSWORD"):
    del os.environ[_cle]
server._SHARED_CACHE = None

# --------------------------------------------------------------------------
# 6. Le catalogue MLD embarque
# --------------------------------------------------------------------------

catalogue = server._catalog()
check("le catalogue se charge", len(catalogue) > 1900, f"{len(catalogue)} tables")
check("HLPRENP present", "HLPRENP" in catalogue)
check(
    "HLPRENP a ses 219 colonnes",
    len(catalogue["HLPRENP"]["c"]) == 219,
    str(len(catalogue["HLPRENP"]["c"])),
)
check("prefixe de HLPRENP", catalogue["HLPRENP"]["p"] == "PE")
check(
    "aucun caractere de remplacement dans les libelles",
    not any("�" in c[2] for r in catalogue.values() for c in r["c"]),
)
check(
    "les accents sont preserves",
    catalogue["HLPRENP"]["d"].startswith("Préparation"),
    catalogue["HLPRENP"]["d"],
)

# Les colonnes sur lesquelles reposent les recettes existent bien. Si l'une
# disparait d'un futur MLD, c'est ici qu'on le voit, pas en production.
ATTENDUES = {
    "HLPRENP": ("PECDPO", "PENPRE", "PENANN", "PETSOL", "PETSOP", "PECETL", "PEACRE", "PECCHA"),
    "HLPRPLP": ("P1CDPO", "P1NPRE", "P1NANP", "P1TVLP", "P1QAPR", "P1QPRE", "P1CART"),
    "HLCHARP": ("CGCDPO", "CGCCHA", "CGTCHV", "CGSSCA", "CGNEMP"),
    "HLEXPEP": ("EXCDPO", "EXNEXP", "EXCCHA", "EXSSCA"),
    "HLRCPEP": ("RPCDPO", "RPNRPR", "RPRRPR", "RPTRPS"),
    "HLRECPP": ("RECDPO", "RETRVA", "RETGEI", "RERREC"),
    "HLAEXEP": ("VECDPO", "VENAEX", "VERLIX"),
    "HLEMPLP": ("EMCDPO", "EMNEMP", "EMLIEM"),
    "HLETPPP": ("T0CEPP", "T0LEPP"),
}
for table, colonnes in ATTENDUES.items():
    presentes = {c[0] for c in catalogue[table]["c"]}
    manquantes = [c for c in colonnes if c not in presentes]
    check(f"colonnes de {table}", not manquantes, "absentes : " + ", ".join(manquantes))

# La recherche doit pardonner l'accentuation : les libelles sont accentues,
# les questions posees ne le sont pas toujours.
check("recherche insensible aux accents", server._norm("Dépôt physique") == "depot physique")

# --------------------------------------------------------------------------
# 7. Les recettes
# --------------------------------------------------------------------------

recettes = sorted(server.RECIPES_DIR.glob("*.sql"))
check("des recettes sont livrees", len(recettes) >= 5, f"{len(recettes)} trouvee(s)")
for path in recettes:
    sql = path.read_text(encoding="utf-8")
    try:
        server.assert_read_only(sql)
        check(f"recette {path.stem}", True)
    except server.ReflexError as exc:
        check(f"recette {path.stem}", False, str(exc).splitlines()[0])

# --------------------------------------------------------------------------
# 8. Le memo
# --------------------------------------------------------------------------

for sujet in ("", "domaines", "ecarts", "preparations", "receptions", "expeditions", "stock", "sql"):
    texte = server.reflex_guide.fn(sujet) if hasattr(server.reflex_guide, "fn") else server.reflex_guide(sujet)
    check(f"memo '{sujet or 'general'}'", len(texte) > 200 and "ERREUR" not in texte[:40])

# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# 9. Les conventions MCP
# --------------------------------------------------------------------------
#
# Ce connecteur affirme etre en lecture seule. La specification MCP a un
# endroit pour le dire formellement au client : les annotations de
# comportement. Un outil qui les oublierait annoncerait, par defaut, qu'il
# peut ecrire.

import asyncio  # noqa: E402

outils = asyncio.run(server.mcp.list_tools())
check("onze outils publies", len(outils) == 11, str(len(outils)))
check("le serveur porte des instructions", bool(server.mcp.instructions))

for outil in outils:
    a = outil.annotations
    check(f"{outil.name} : annotations presentes", a is not None)
    if a is None:
        continue
    check(f"{outil.name} : readOnlyHint", a.readOnlyHint is True)
    check(f"{outil.name} : destructiveHint faux", a.destructiveHint is False)
    check(f"{outil.name} : titre lisible", bool(a.title) and a.title != outil.name)
    check(f"{outil.name} : description", bool(outil.description))

# openWorldHint doit distinguer les deux familles : le catalogue embarque est
# un ensemble ferme et reproductible, la base ne l'est pas.
par_nom = {o.name: o for o in outils}
for nom in ("reflex_tables", "reflex_columns", "reflex_find_column", "reflex_prefix", "reflex_guide"):
    check(f"{nom} : monde ferme (catalogue embarque)",
          par_nom[nom].annotations.openWorldHint is False)
for nom in ("reflex_query", "reflex_peek", "reflex_export_csv"):
    check(f"{nom} : monde ouvert (la base bouge)",
          par_nom[nom].annotations.openWorldHint is True)

# Le parametre `base` doit etre expose partout ou la cascade s'applique.
for nom in ("reflex_query", "reflex_peek", "reflex_export_csv"):
    check(f"{nom} : parametre base expose",
          "base" in par_nom[nom].inputSchema.get("properties", {}))

# --------------------------------------------------------------------------

print(f"\n{PASSED} verification(s) passee(s), {len(FAILED)} echec(s).")
for line in FAILED:
    print(f"  ECHEC  {line}")
raise SystemExit(1 if FAILED else 0)
