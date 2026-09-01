#!/usr/bin/env python3
"""
Controles hors-ligne du connecteur jira. AUCUN appel reseau.

    python test_offline.py

Ce que ces controles protegent, et pourquoi ils existent :

  - **la traduction francais -> JQL**. C'est le coeur du connecteur, et une
    traduction fausse ne leve pas d'erreur : elle rend des lignes plausibles.
  - **les REFUS**. Un filtre que le serveur ne comprend pas doit etre refuse et
    non ignore : un filtre ignore rend TOUTE la base avec l'air d'avoir compris.
    Chaque refus est donc teste comme une fonctionnalite.
  - **la lecture seule**. La liste blanche des chemins GET est ce qui empeche
    l'outil generique d'ecrire. Un trou dedans ne se verrait nulle part ailleurs.
  - **le refus des secrets dans le fichier d'equipe**. Un jeton Jira est
    nominatif : s'il passait, il serait lisible par vingt-six personnes.
  - **la signature des outils MCP**. Le bug passe une fois chez shiptify : sans
    functools.wraps, FastMCP publie chaque outil avec (args, kwargs) et les
    outils deviennent inappelables, sans erreur au demarrage.

Les controles tournent avec JIRA_SHARED_ENV et JIRA_ENV_FILE pointes sur des
fichiers inexistants : sinon la suite tournerait avec la vraie configuration du
poste qui la lance - c'est-a-dire la seule ou elle est lancee - et un garde-fou
qui passe toujours finit par etre ignore.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent

# AVANT l'import du serveur : neutralise toute configuration reelle.
os.environ["JIRA_SHARED_ENV"] = str(HERE / "_inexistant_partage.env")
os.environ["JIRA_ENV_FILE"] = str(HERE / "_inexistant_poste.env")
for name in list(os.environ):
    if name.startswith("JIRA_") and name not in {
        "JIRA_SHARED_ENV",
        "JIRA_ENV_FILE",
    }:
        del os.environ[name]

sys.path.insert(0, str(HERE))
import server  # noqa: E402
import vu_cache  # noqa: E402

ECHECS: list[str] = []
PASSES = 0


def verifie(libelle: str, condition: bool, detail: str = "") -> None:
    global PASSES
    if condition:
        PASSES += 1
        print(f"  ok   {libelle}")
    else:
        ECHECS.append(f"{libelle}{' - ' + detail if detail else ''}")
        print(f"  ECHEC {libelle}{' - ' + detail if detail else ''}")


def leve(libelle: str, fn, exception=Exception) -> None:
    """Verifie qu'un appel REFUSE. Un refus est une fonctionnalite, ici."""
    try:
        resultat = fn()
    except exception:
        verifie(libelle, True)
        return
    verifie(libelle, False, f"aucun refus, a rendu : {resultat!r}")


print("Controles hors-ligne du connecteur jira")
print()

# --------------------------------------------------------------------------
print("1. Fichiers embarques")
ctx = server._contexte()
verifie("contexte_jira.json lisible", bool(ctx), server._CONTEXTE_ERROR)
verifie(
    "contexte : les deux tenants sont decrits",
    set(server._contexte_tenants()) == {"PROJET", "WF"},
    str(list(server._contexte_tenants())),
)
verifie(
    "contexte : SUPPLY et VUD sont dans les projets",
    {"SUPPLY", "VUD"} <= set(ctx.get("projets") or {}),
)
verifie("contexte : des recettes JQL sont livrees", len(ctx.get("jql") or []) >= 8)
spec = server._spec()
verifie("rest_get_paths.json lisible", bool(spec.get("paths")))
verifie(
    "liste blanche : /search/jql et /myself y sont",
    {"/search/jql", "/myself"} <= set(spec["paths"]),
)

# --------------------------------------------------------------------------
print()
print("2. vu_cache.py est identique dans tous les plugins")
empreintes: dict[str, str] = {}
# HERE vaut <marketplace>/plugins/jira/server : la racine des plugins est donc
# parents[1], et non parents[2] qui designe le marketplace lui-meme. Avec le
# mauvais niveau, le glob ne trouvait AUCUNE copie et le controle passait a
# vide - une divergence de vu_cache.py entre plugins serait restee invisible,
# ce qui est exactement ce que ce controle existe pour empecher.
racine_plugins = HERE.parents[1]
for copie in sorted(racine_plugins.glob("*/server/vu_cache.py")):
    empreintes[copie.parents[1].name] = hashlib.sha256(
        copie.read_bytes()
    ).hexdigest()[:16]
verifie(
    "au moins deux copies de vu_cache.py sont comparees",
    len(empreintes) >= 2,
    json.dumps(empreintes),
)
verifie(
    "toutes les copies de vu_cache.py ont la meme empreinte",
    len(set(empreintes.values())) <= 1,
    json.dumps(empreintes),
)
print(f"       copies vues : {', '.join(sorted(empreintes)) or '(aucune)'}")

# --------------------------------------------------------------------------
print()
print("3. Les tenants et leurs alias")
verifie("« vuproject » vaut PROJET", server._tenant_code("vuproject") == "PROJET")
verifie("« webfacto » vaut WF", server._tenant_code("webfacto") == "WF")
verifie("« wf » vaut WF", server._tenant_code("wf") == "WF")
verifie("« les devs » vaut WF", server._tenant_code("dev") == "WF")
verifie("« PROJET » reste PROJET", server._tenant_code("PROJET") == "PROJET")
verifie("un inconnu reste tel quel", server._tenant_code("autre") == "AUTRE")
tenants = server._all_tenants()
verifie("deux tenants declares par defaut", len(tenants) == 2, str(len(tenants)))
verifie(
    "sans identifiant, aucun tenant n'est pret",
    all(not t.ready for t in tenants),
    str([(t.code, t.problem) for t in tenants]),
)
verifie(
    "le probleme nomme la variable a renseigner",
    all("JIRA_" in t.problem for t in tenants),
    str([t.problem for t in tenants]),
)
leve("un tenant inconnu explicite est refuse", lambda: server._tenants("ZZZ"))
leve("sans configuration, _tenants leve", lambda: server._tenants())

# La racine d'API : le piege n° 1 du mode OAuth.
t_basic = server.Tenant("X", "https://exemple.atlassian.net", auth="basic")
verifie(
    "mode basic : on parle au site",
    t_basic.api_base == "https://exemple.atlassian.net/rest/api/3",
    t_basic.api_base,
)
t_oauth = server.Tenant(
    "X", "https://exemple.atlassian.net", auth="oauth", cloud_id="abc-123"
)
verifie(
    "mode oauth : on parle a la passerelle avec le cloudId",
    t_oauth.api_base == "https://api.atlassian.com/ex/jira/abc-123/rest/api/3",
    t_oauth.api_base,
)
verifie(
    "l'URL cliquable passe toujours par le site",
    t_oauth.browse("SUPPLY-1") == "https://exemple.atlassian.net/browse/SUPPLY-1",
)

# --------------------------------------------------------------------------
print()
print("4. La traduction des statuts")
clause, note = server._statut_jql("en cours")
verifie("« en cours » -> In Progress", clause == 'statusCategory = "In Progress"', clause)
verifie("la traduction est annoncee", "traduit" in note.lower(), note)
verifie(
    "« ouvert » -> tout sauf Done",
    server._statut_jql("ouverts")[0] == "statusCategory != Done",
)
verifie("« termine » -> Done", server._statut_jql("termine")[0] == "statusCategory = Done")
verifie(
    "les accents et la casse ne comptent pas",
    server._statut_jql("Terminé")[0] == "statusCategory = Done",
)
verifie("« tout » ne pose aucun filtre", server._statut_jql("tout")[0] == "")
clause, note = server._statut_jql("En recette")
verifie(
    "un libelle inconnu passe en status = et le dit",
    clause == 'status = "En recette"' and "libelle reel" in note,
    f"{clause} / {note}",
)

# --------------------------------------------------------------------------
print()
print("5. La traduction des periodes - et les refus")
verifie(
    "« cette semaine » -> startOfWeek()",
    server._periode_jql("cette semaine", "created")[0] == "startOfWeek()",
)
verifie(
    "« 30 jours » -> -30d", server._periode_jql("30 jours", "created")[0] == "-30d"
)
verifie(
    "une date ISO est mise entre guillemets",
    server._periode_jql("2026-08-01", "created")[0] == '"2026-08-01"',
)
verifie(
    "une date francaise est convertie",
    server._periode_jql("01/08/2026", "created")[0] == '"2026-08-01"',
)
verifie(
    "une duree JQL passe telle quelle",
    server._periode_jql("-4w", "created")[0] == "-4w",
)
leve(
    "une periode incomprehensible est REFUSEE, pas ignoree",
    lambda: server._periode_jql("vers la fin du printemps", "created"),
    server.JiraError,
)

# --------------------------------------------------------------------------
print()
print("6. Les personnes - le piege de l'accountId")
verifie(
    "« moi » -> currentUser()",
    server._assigne_jql("moi")[0] == "assignee = currentUser()",
)
verifie(
    "« personne » -> IS EMPTY",
    server._assigne_jql("personne")[0] == "assignee IS EMPTY",
)
verifie(
    "un accountId est accepte",
    "712020:" in server._assigne_jql("712020:1a2b3c4d-5e6f-7890-abcd-ef1234567890")[0],
)
leve(
    "un nom d'affichage est REFUSE, avec l'orientation vers jira_user_lookup",
    lambda: server._assigne_jql("Guillaume Champin"),
    server.JiraError,
)

# --------------------------------------------------------------------------
print()
print("7. L'echappement JQL")
verifie(
    "un guillemet est echappe",
    server._quote_jql('cle "speciale"') == 'cle \\"speciale\\"',
    server._quote_jql('cle "speciale"'),
)
verifie(
    "un antislash est echappe",
    server._quote_jql("a\\b") == "a\\\\b",
)
leve(
    "un saut de ligne dans un filtre est refuse",
    lambda: server._quote_jql("ligne1\nligne2"),
    server.JiraError,
)

# --------------------------------------------------------------------------
print()
print("8. La construction du JQL")
leve(
    "une recherche SANS AUCUN filtre est refusee",
    lambda: server._build_jql(),
    server.JiraError,
)
jql, notes = server._build_jql(
    projet="SUPPLY", statut="ouvert", type_ticket="Epic", ordre="recent"
)
verifie(
    "projet + statut + type + ordre",
    jql == 'project = SUPPLY AND issuetype = "Epic" AND statusCategory != Done '
    "ORDER BY updated DESC",
    jql,
)
jql, _ = server._build_jql(projet="supply,vud", texte="TIL")
verifie("deux projets -> IN", "project IN (SUPPLY, VUD)" in jql, jql)
verifie("le texte cherche partout avec ~", 'text ~ "TIL"' in jql, jql)
jql, _ = server._build_jql(projet="VUD", titre="HF2607050041")
verifie(
    "le titre seul, pour une reference d'expedition",
    'summary ~ "HF2607050041"' in jql,
    jql,
)
jql, _ = server._build_jql(projet="SUPPLY", cree_depuis="ce mois", maj_depuis="-7d")
verifie(
    "deux periodes sur deux champs",
    "created >= startOfMonth()" in jql and "updated >= -7d" in jql,
    jql,
)
jql, notes = server._build_jql(projet="SUPPLY", epic="supply-3527")
verifie("un epic devient parent =", "parent = SUPPLY-3527" in jql, jql)
verifie(
    "et l'ancien champ Epic Link est signale",
    any("Epic Link" in n for n in notes),
    str(notes),
)
jql, notes = server._build_jql(projet="ZZZ", statut="ouvert")
verifie(
    "un projet hors contexte passe, avec un avertissement",
    "project = ZZZ" in jql and any("pas dans le contexte" in n for n in notes),
    f"{jql} / {notes}",
)
verifie(
    "le nom d'un projet est reconnu",
    "project = SUPPLY" in server._build_jql(projet="Supply_Chain")[0],
)
leve(
    "un ordre incomprehensible est refuse",
    lambda: server._build_jql(projet="SUPPLY", ordre="par ordre d'importance"),
    server.JiraError,
)

# --------------------------------------------------------------------------
print()
print("9. Lecture seule : la liste blanche des chemins")
verifie("/myself est accepte", server._check_get_path("/myself")[0] == "/myself")
verifie(
    "un chemin avec parametre est reconnu",
    server._check_get_path("/issue/SUPPLY-1/comment")[1]
    == "/issue/{issueIdOrKey}/comment",
    str(server._check_get_path("/issue/SUPPLY-1/comment")),
)
verifie(
    "le plus specifique gagne",
    server._check_get_path("/issue/SUPPLY-1")[1] == "/issue/{issueIdOrKey}",
)
verifie(
    "la racine /rest/api/3 est acceptee et retiree",
    server._check_get_path("/rest/api/3/myself")[0] == "/myself",
)
leve(
    "un chemin hors liste blanche est refuse",
    lambda: server._check_get_path("/issue/SUPPLY-1/attachments"),
    server.JiraError,
)
leve(
    "un chemin explicitement non expose est refuse",
    lambda: server._check_get_path("/users/search"),
    server.JiraError,
)
leve(
    "une URL complete est refusee",
    lambda: server._check_get_path("https://vuproject.atlassian.net/rest/api/3/myself"),
    server.JiraError,
)
leve(
    "des filtres dans le chemin sont refuses",
    lambda: server._check_get_path("/search/jql?jql=project=SUPPLY"),
    server.JiraError,
)
verifie(
    "aucun chemin d'ecriture n'est dans la liste blanche",
    not any(
        mot in path.lower()
        for path in spec["paths"]
        for mot in ("/create", "/delete", "/assignee", "/transition/")
    ),
)

# --------------------------------------------------------------------------
print()
print("10. ADF : rendre lisible une description")
doc = {
    "type": "doc",
    "version": 1,
    "content": [
        {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "Contexte"}]},
        {"type": "paragraph", "content": [{"type": "text", "text": "Le plan France 2026."}]},
        {
            "type": "bulletList",
            "content": [
                {
                    "type": "listItem",
                    "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": "GLS d'abord"}]}
                    ],
                },
                {
                    "type": "listItem",
                    "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": "puis VIR"}]}
                    ],
                },
            ],
        },
        {
            "type": "table",
            "content": [
                {
                    "type": "tableRow",
                    "content": [
                        {"type": "tableCell", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Tier A"}]}]},
                        {"type": "tableCell", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "GLS"}]}]},
                    ],
                }
            ],
        },
    ],
}
texte = server._texte_champ(doc)
verifie("le titre est rendu", "## Contexte" in texte, texte)
verifie("le paragraphe est rendu", "Le plan France 2026." in texte)
verifie("les puces sont rendues", "- GLS d'abord" in texte and "- puis VIR" in texte, texte)
verifie("le tableau est rendu en ligne", "Tier A | GLS" in texte, texte)
verifie("un texte simple passe tel quel", server._texte_champ("bonjour") == "bonjour")
verifie("un champ vide rend une chaine vide", server._texte_champ(None) == "")

# --------------------------------------------------------------------------
print()
print("11. Un ticket ramene a une ligne")
issue = {
    "key": "SUPPLY-3527",
    "fields": {
        "summary": "Atlas - cadrage du besoin",
        "status": {"name": "En cours", "statusCategory": {"name": "In Progress"}},
        "issuetype": {"name": "Epic"},
        "priority": {"name": "Medium"},
        "assignee": {"displayName": "Prenom Nom"},
        "reporter": {"displayName": "Autre Personne"},
        "created": "2026-06-02T09:15:00.000+0200",
        "updated": "2026-08-28T17:42:11.000+0200",
        "resolutiondate": None,
        "parent": {"key": "SUPPLY-3418"},
        "labels": ["atlas", "cadrage"],
        "customfield_10111": {"value": "Transport"},
    },
}
row = server._issue_row(server.Tenant("PROJET", "https://vuproject.atlassian.net"), issue)
verifie("la cle est reprise", row["cle"] == "SUPPLY-3527")
verifie("le projet est deduit de la cle", row["projet"] == "SUPPLY")
verifie("le tenant est en colonne", row["tenant"] == "PROJET")
verifie("le statut et sa categorie sont separes", row["statut"] == "En cours" and row["categorie"] == "In Progress")
verifie("la date est raccourcie", row["cree"] == "2026-06-02 09:15", row["cree"])
verifie("l'age est calcule", isinstance(row["age_jours"], int), str(row["age_jours"]))
verifie("les etiquettes sont jointes", row["etiquettes"] == "atlas,cadrage")
verifie("le parent est repris", row["parent"] == "SUPPLY-3418")
verifie(
    "l'URL est cliquable",
    row["url"] == "https://vuproject.atlassian.net/browse/SUPPLY-3527",
)
verifie(
    "la description n'est PAS dans la ligne (elle pese trop)",
    "description" not in row,
)
row2 = server._issue_row(
    server.Tenant("PROJET", "https://vuproject.atlassian.net"),
    issue,
    extra="customfield_10111",
)
verifie(
    "un champ personnalise demande est ajoute, aplati",
    row2.get("customfield_10111") == "Transport",
    str(row2.get("customfield_10111")),
)
vide = server._issue_row(server.Tenant("WF", "https://webfacto.atlassian.net"), {})
verifie("un ticket vide ne casse pas", vide["cle"] == "" and vide["statut"] == "")

# --------------------------------------------------------------------------
print()
print("12. Le fichier d'equipe porte les identifiants du compte de SERVICE")
# Decision du 2026-09-01 : les identifiants utilises sont ceux d'un compte de
# service d'equipe, pas des jetons nominatifs. Le fichier partage les porte
# donc, comme chez shiptify et yooz. Restent refuses les deux secrets du mode
# oauth - pour une raison mecanique : le refresh token tourne et n'a qu'un
# detenteur possible.
with tempfile.TemporaryDirectory() as tmp:
    partage = pathlib.Path(tmp) / "jira.shared.env"
    partage.write_text(
        "\n".join(
            [
                "JIRA_TENANTS=PROJET,WF",
                "JIRA_PROJET_BASE_URL=https://vuproject.atlassian.net",
                "JIRA_MAX_PAGES=30",
                "# les identifiants du compte de service : ADMIS",
                "JIRA_PROJET_EMAIL=service.jira@vente-unique.com",
                "JIRA_PROJET_TOKEN=jeton-de-service-projet",
                "JIRA_WF_EMAIL=service.jira@vente-unique.com",
                "JIRA_WF_TOKEN=jeton-de-service-wf",
                "# les deux secrets oauth : REFUSES, ils ne se partagent pas",
                "JIRA_WF_CLIENT_SECRET=secret-oauth",
                "JIRA_WF_REFRESH_TOKEN=refresh-oauth",
                "# celle-ci est locale par nature",
                "JIRA_EXPORT_DIR=C:/temp",
                "# celle-la est ambigue sans prefixe de tenant",
                "JIRA_TOKEN=sans-prefixe",
                "# et celle-la est une faute de frappe",
                "JIRA_PROJET_BASEURL=https://faute.atlassian.net",
            ]
        ),
        encoding="utf-8",
    )
    os.environ["JIRA_SHARED_ENV"] = str(partage)
    server._SHARED_CACHE = None
    valeurs = server._load_shared_env()
    verifie("les reglages non secrets sont repris", valeurs.get("JIRA_MAX_PAGES") == "30")
    verifie(
        "la racine de site est reprise",
        valeurs.get("JIRA_PROJET_BASE_URL") == "https://vuproject.atlassian.net",
    )
    verifie(
        "le courriel de service est repris",
        valeurs.get("JIRA_PROJET_EMAIL") == "service.jira@vente-unique.com",
    )
    verifie(
        "le jeton de service est repris",
        valeurs.get("JIRA_PROJET_TOKEN") == "jeton-de-service-projet",
    )
    verifie(
        "les deux instances sont couvertes",
        valeurs.get("JIRA_WF_TOKEN") == "jeton-de-service-wf",
    )
    verifie("le client_secret oauth est REFUSE", "JIRA_WF_CLIENT_SECRET" not in valeurs)
    verifie("le refresh token oauth est REFUSE", "JIRA_WF_REFRESH_TOKEN" not in valeurs)
    verifie(
        "les deux secrets oauth sont signales",
        len(set(server._SHARED_SECRETS_SEEN)) == 2,
        str(server._SHARED_SECRETS_SEEN),
    )
    verifie(
        "un chemin local pose dans le partage est signale",
        "JIRA_EXPORT_DIR" in server._SHARED_LOCAL_SEEN,
    )
    verifie(
        "un jeton sans prefixe de tenant est signale comme ambigu",
        "JIRA_TOKEN" in server._SHARED_LOCAL_SEEN,
        str(server._SHARED_LOCAL_SEEN),
    )
    verifie(
        "une faute de frappe est signalee",
        "JIRA_PROJET_BASEURL" in server._SHARED_REJECTED,
        str(server._SHARED_REJECTED),
    )

    # Le connecteur doit MARCHER sur la seule configuration d'equipe : c'est
    # tout l'objet de la decision. Un poste sans rien saisi doit etre pret.
    tenant = server._describe_tenant("PROJET")
    verifie(
        "un tenant est PRET avec les seuls identifiants d'equipe",
        tenant.ready,
        tenant.problem,
    )
    verifie(
        "et son jeton est bien celui de l'equipe",
        tenant.token == "jeton-de-service-projet",
    )
    verifie(
        "l'origine du jeton est nommee",
        "config d'equipe" in server._key_source("PROJET"),
        server._key_source("PROJET"),
    )

    # La valeur d'un secret ne s'affiche JAMAIS, meme quand elle est partagee.
    rapport = "\n".join(server._shared_report())
    verifie("le nom du secret est dit", "JIRA_PROJET_TOKEN" in rapport)
    verifie(
        "sa VALEUR n'apparait nulle part dans le rapport",
        "jeton-de-service-projet" not in rapport,
    )
    verifie(
        "le rapport explique pourquoi les secrets oauth sont refuses",
        "refresh token" in rapport.lower(),
        rapport,
    )

    # Le poste passe DEVANT l'equipe : c'est la sortie pour un acces nominatif.
    os.environ["JIRA_PROJET_TOKEN"] = "jeton-du-poste"
    tenant = server._describe_tenant("PROJET")
    verifie(
        "un jeton pose sur le poste gagne sur celui de l'equipe",
        tenant.token == "jeton-du-poste",
        tenant.token,
    )
    del os.environ["JIRA_PROJET_TOKEN"]
    # Un chemin explicite est EXCLUSIF : il ne doit pas retomber sur le vrai.
    os.environ["JIRA_SHARED_ENV"] = str(pathlib.Path(tmp) / "absent.env")
    server._SHARED_CACHE = None
    verifie(
        "un chemin explicite absent ne retombe PAS sur la config reelle",
        server._load_shared_env() == {},
        str(server._load_shared_env()),
    )
    verifie(
        "et un seul emplacement est essaye",
        len(server._shared_env_candidates()) == 1,
    )

# --------------------------------------------------------------------------
print()
print("13. Lecture d'un .env : le BOM et les valeurs bizarres")
valeurs = server._parse_env(
    "\ufeffJIRA_PROJET_TOKEN=\"ATATT3xFfGF0#avec=des+signes\"\n"
    "# commentaire\n"
    "export JIRA_PROJET_EMAIL=a@b.com\n"
    "MAUVAISE LIGNE\n"
)
verifie(
    "une valeur avec # et = est preservee",
    valeurs.get("\ufeffJIRA_PROJET_TOKEN") == "ATATT3xFfGF0#avec=des+signes"
    or valeurs.get("JIRA_PROJET_TOKEN") == "ATATT3xFfGF0#avec=des+signes",
    str(valeurs),
)
verifie("le prefixe export est retire", valeurs.get("JIRA_PROJET_EMAIL") == "a@b.com")
verifie("une ligne sans = est ignoree", "MAUVAISE LIGNE" not in valeurs)
with tempfile.TemporaryDirectory() as tmp:
    fichier = pathlib.Path(tmp) / "jira.env"
    fichier.write_text("JIRA_TEST_BOM=ok\n", encoding="utf-8-sig")
    lu = server._parse_env(fichier.read_text(encoding="utf-8-sig"))
    verifie(
        "un fichier avec BOM est lu sans octets parasites",
        lu.get("JIRA_TEST_BOM") == "ok",
        str(lu),
    )

# --------------------------------------------------------------------------
print()
print("14. Le magasin local refuse les dossiers synchronises")
verifie(
    "un chemin OneDrive/SharePoint est detecte comme synchronise",
    server._is_synced(pathlib.Path(r"C:\Users\x\CAFOM\Transport BtoC - Documents\a.env")),
)
verifie(
    "le profil utilisateur ne l'est pas",
    not server._is_synced(pathlib.Path.home() / ".jira-mcp" / "jira.env"),
)
verifie(
    "la racine locale est dans le profil, pas dans %LOCALAPPDATA%",
    "localappdata" not in str(server._local_root()).lower(),
    str(server._local_root()),
)

# --------------------------------------------------------------------------
print()
print("15. Les outils MCP sont bien publies, avec leurs vrais parametres")


async def _outils():
    return await server.mcp.list_tools()


outils = asyncio.run(_outils())
noms = {o.name for o in outils}
verifie("au moins 25 outils publies", len(outils) >= 25, str(len(outils)))
attendus = {
    "jira_setup_status",
    "jira_doctor",
    "jira_tenants",
    "jira_contexte",
    "jira_projects",
    "jira_fields",
    "jira_statuses",
    "jira_recherche",
    "jira_search",
    "jira_summary",
    "jira_issue",
    "jira_comments",
    "jira_changelog",
    "jira_children",
    "jira_export_csv",
    "jira_sync",
    "jira_sql",
    "jira_get",
}
verifie("les outils attendus sont tous la", attendus <= noms, str(sorted(attendus - noms)))
mauvais = []
for outil in outils:
    proprietes = set((outil.inputSchema or {}).get("properties") or {})
    if {"args", "kwargs"} & proprietes:
        mauvais.append(outil.name)
verifie(
    "aucun outil publie avec (args, kwargs) - le bug functools.wraps",
    not mauvais,
    str(mauvais),
)
recherche = next(o for o in outils if o.name == "jira_recherche")
proprietes = set((recherche.inputSchema or {}).get("properties") or {})
verifie(
    "jira_recherche expose ses filtres en francais",
    {"projet", "statut", "cree_depuis", "assigne", "epic"} <= proprietes,
    str(sorted(proprietes)),
)
sans_docstring = [o.name for o in outils if not (o.description or "").strip()]
verifie("tous les outils ont une description", not sans_docstring, str(sans_docstring))

# --------------------------------------------------------------------------
print()
print("16. Les erreurs sont rendues, pas levees (le decorateur _guard)")
sortie = server.jira_recherche(tenant="PROJET", projet="SUPPLY")
verifie(
    "un tenant non configure rend un message, pas une exception",
    isinstance(sortie, str) and sortie.startswith("ERREUR"),
    sortie[:120],
)
verifie(
    "le message oriente vers la mise en service",
    "jira_setup_status" in sortie,
    sortie[:200],
)
sortie = server.jira_issue(tenant="PROJET", cle="pas-une-cle")
verifie("une cle mal formee est refusee proprement", sortie.startswith("ERREUR"), sortie[:120])
sortie = server.jira_contexte("Atlas")
verifie(
    "le contexte trouve Atlas par recherche libre",
    "SUPPLY-3527" in sortie,
    sortie[:200],
)
sortie = server.jira_contexte("titres")
verifie("le decodeur VUD est accessible", "HF2607050041" in sortie, sortie[:200])
sortie = server.jira_summary(tenant="PROJET", grouper_par="nawak", projet="SUPPLY")
verifie(
    "un regroupement inconnu est refuse avec la liste des valeurs",
    sortie.startswith("ERREUR") and "statut" in sortie,
    sortie[:160],
)

# --------------------------------------------------------------------------
print()
print("17. Le cache local : garde-fou SQL")
leve("une ecriture SQL est refusee", lambda: vu_cache.guard_sql("DELETE FROM issues"), vu_cache.CacheError)
leve("un DROP est refuse", lambda: vu_cache.guard_sql("DROP TABLE issues"), vu_cache.CacheError)
leve("deux instructions sont refusees", lambda: vu_cache.guard_sql("SELECT 1; SELECT 2"), vu_cache.CacheError)
leve("un PRAGMA est refuse", lambda: vu_cache.guard_sql("PRAGMA table_info(issues)"), vu_cache.CacheError)
verifie(
    "un SELECT passe",
    vu_cache.guard_sql("SELECT * FROM issues") == "SELECT * FROM issues",
)
verifie(
    "un WITH passe",
    vu_cache.guard_sql("WITH x AS (SELECT 1) SELECT * FROM x").startswith("WITH"),
)

# --------------------------------------------------------------------------
print()
print("-" * 70)
print(f"{PASSES} controle(s) passe(s), {len(ECHECS)} echec(s)")
if ECHECS:
    print()
    for echec in ECHECS:
        print(f"  ECHEC : {echec}")
sys.exit(1 if ECHECS else 0)
