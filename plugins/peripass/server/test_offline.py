#!/usr/bin/env python3
"""
Controles du serveur MCP peripass, SANS RESEAU et SANS CLE.

    python test_offline.py

Ce que ca verifie : la liste blanche des chemins, les garde-fous qui refusent
avant d'emettre, la pagination, l'aplatissement, la projection de colonnes, le
multi-site, et la purete du flux stdio. Ce que ca ne verifie PAS : que l'API
Peripass rend ce qu'on croit. Ca, c'est la recette contre le vrai tenant, et
elle demande une cle.

Aucun appel reseau n'est emis : le seul chemin qui appellerait est remplace par
un faux qui leve si on l'atteint.
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# Un environnement propre : aucune cle, aucun fichier de configuration lu.
for name in list(os.environ):
    if name.startswith("PERIPASS_"):
        del os.environ[name]
os.environ["PERIPASS_ENV_FILE"] = str(HERE / "_inexistant_.env")
# Et aucun fichier d'equipe : sur un poste qui synchronise la bibliotheque,
# 08_ENGINE/04_mcp/00_config/peripass.shared.env serait trouve et lu, et les
# controles tourneraient avec les vraies cles - donc ne prouveraient rien.
os.environ["PERIPASS_SHARED_ENV"] = str(HERE / "_inexistant_.shared.env")
os.environ.pop("VU_ENGINE_DIR", None)
os.environ.pop("CLAUDE_PLUGIN_ROOT", None)
os.environ.pop("CLAUDE_PLUGIN_DATA", None)

import server  # noqa: E402

PASS = 0
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASS
    if condition:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL.append(label)
        print(f"  ECHEC {label}" + (f" | {detail}" if detail else ""))


def section(title: str) -> None:
    print()
    print(title)


def boom(*args, **kwargs):
    raise AssertionError("appel reseau emis alors qu'il devait etre refuse avant")


# --------------------------------------------------------------------------
section("1. Le contrat et la liste blanche")

spec = server._spec()
check("le contrat se charge", isinstance(spec, dict) and "paths" in spec)
check("18 chemins GET", len(spec["paths"]) == 18, str(len(spec["paths"])))
check(
    "les enums du contrat sont la",
    "VisitorStatus" in spec.get("enums", {})
    and "TimestampFilterField" in spec.get("enums", {}),
)
check(
    "SlotStart est une valeur de timestampFilterField",
    "SlotStart" in server._enum("TimestampFilterField"),
)

section("2. Chemins acceptes, chemins refuses")

ok, template = server._check_get_path("/visitors")
check("/visitors accepte", ok == "/visitors" and template == "/visitors")
ok, template = server._check_get_path("/visitors/")
check("slash final tolere", ok == "/visitors", ok)
ok, template = server._check_get_path("/assets/42")
check(
    "un id concret retrouve son gabarit",
    template == "/assets/{id}",
    template,
)
ok, template = server._check_get_path("/visitors/id:10")
check(
    "une reference retrouve /visitors/{reference}, pas validateeticket",
    template == "/visitors/{reference}",
    template,
)

for bad, why in (
    ("/visitors/12/status/checkedin", "chemin d'ecriture"),
    ("/n-importe-quoi", "chemin inconnu"),
    ("/visitors?pageSize=10", "parametres colles dans le chemin"),
    ("/visitors/validateeticket", "cle d'API en parametre d'URL"),
    ("/visitors/1/attachments/abc/download", "binaire"),
):
    try:
        server._check_get_path(bad)
        check(f"refus de {bad} ({why})", False, "accepte alors qu'il devait etre refuse")
    except server.PeripassError:
        check(f"refus de {bad} ({why})", True)

section("3. Pagination : ce que le contrat dit")

check("/visitors est pagine", server._supports_paging("/visitors"))
check("/assets est pagine", server._supports_paging("/assets"))
check(
    "/assets/{id} ne l'est pas",
    not server._supports_paging("/assets/{id}"),
)
check(
    "/visitors/{reference} ne l'est pas",
    not server._supports_paging("/visitors/{reference}"),
)
check("pageSize plafonne a 100", server.PAGE_SIZE_MAX == 100)

section("4. Les sites")

os.environ["PERIPASS_SITES"] = "AUV,AMB"
server._ENV_DONE = True  # pas de fichier a relire
sites = server._all_sites()
check("deux sites declares", len(sites) == 2, str([s.code for s in sites]))
check(
    "AUV porte la racine du script Power Query",
    sites[0].base_url == "https://vente-unique-logistics-auv.peripass.app/api/v2",
    sites[0].base_url,
)
check("aucun site pret sans cle", all(not s.ready for s in sites))
try:
    server._sites("")
    check("sans cle, _sites refuse avec un message", False)
except server.ConfigError as exc:
    check(
        "sans cle, _sites refuse avec un message",
        "Aucun site Peripass configure" in str(exc) and "PERIPASS_AUV_API_KEY" in str(exc),
    )

os.environ["PERIPASS_AUV_API_KEY"] = "faux-pour-le-test"
check("AUV devient pret", server._describe_site("AUV").ready)
check("AMB reste en attente", not server._describe_site("AMB").ready)
check("le lot par defaut ne garde que AUV", [s.code for s in server._sites("")] == ["AUV"])
try:
    server._sites("AMB")
    check("un site explicitement demande mais non configure est refuse", False)
except server.ConfigError as exc:
    check(
        "un site explicitement demande mais non configure est refuse",
        "AMB non configure" in str(exc),
        str(exc),
    )
try:
    server._sites("ZZZ")
    check("un site inconnu est refuse", False)
except server.ConfigError:
    check("un site inconnu est refuse", True)

os.environ["PERIPASS_AMB_API_KEY"] = "faux-pour-le-test-2"
check("les deux sites prets", [s.code for s in server._sites("")] == ["AUV", "AMB"])
check("selection explicite", [s.code for s in server._sites("amb")] == ["AMB"])

section("5. Le tenant, forme moderne")

os.environ["PERIPASS_AUV_BASE_URL"] = server.SHARED_BASE_URL
os.environ["PERIPASS_AUV_TENANT"] = "un-tenant"
site = server._describe_site("AUV")
check("la racine partagee est prise", site.base_url == server.SHARED_BASE_URL)
check("le tenant est retenu", site.tenant == "un-tenant")
del os.environ["PERIPASS_AUV_BASE_URL"]
del os.environ["PERIPASS_AUV_TENANT"]

section("6. Les garde-fous des filtres, avant tout appel")

real_request = server._request
server._request = boom
try:
    out = server.peripass_list_visitors(timestamp_start="2026-08-01T00:00:00Z")
    check(
        "une periode sans timestamp_field est refusee",
        out.startswith("ERREUR") and "timestamp_field" in out,
        out[:120],
    )
    out = server.peripass_list_visitors(status="CheckdIn")
    check(
        "un statut mal orthographie est refuse, avec la liste des valeurs",
        out.startswith("ERREUR") and "CheckedIn" in out,
        out[:160],
    )
    check(
        "la casse d'un enum est corrigee, pas refusee",
        server._check_enum("slotstart", "TimestampFilterField", "x") == "SlotStart",
    )
    check(
        "un enum vide reste vide",
        server._check_enum("", "TimestampFilterField", "x") == "",
    )
    out = server.peripass_list_assets(search_text="ABC")
    check(
        "search_text sans search_object est refuse",
        out.startswith("ERREUR") and "search_object" in out,
        out[:120],
    )
    out = server.peripass_get_visitor("")
    check("une reference vide est refusee", out.startswith("ERREUR"))
    out = server.peripass_get_visitor("id.10")
    check("un point dans la reference est refuse", out.startswith("ERREUR"), out[:120])
    out = server.peripass_visitor_history(visitor_id=1, site="")
    check(
        "un historique sans site est refuse",
        out.startswith("ERREUR") and "site est obligatoire" in out,
        out[:120],
    )
    out = server.peripass_referential("inconnu")
    check("un referentiel inconnu est refuse", out.startswith("ERREUR"))
    out = server.peripass_fields(collection="camions")
    check("une collection inconnue est refusee", out.startswith("ERREUR"))
    out = server.peripass_export_csv(path="/visitors", query_json="{pas du json}")
    check("un query_json invalide est refuse", out.startswith("ERREUR"))
    out = server.peripass_get(path="/visitors/12/status/blocked")
    check(
        "l'echappatoire refuse un chemin d'ecriture",
        out.startswith("ERREUR") and "lecture seule" in out,
        out[:160],
    )
finally:
    server._request = real_request

section("7. Pagination et multi-site, sur des donnees simulees")

pages: dict[tuple[str, int], list[dict]] = {
    ("AUV", 0): [{"id": i, "displayName": f"AUV-{i}"} for i in range(100)],
    ("AUV", 1): [{"id": 100 + i, "displayName": f"AUV-{100 + i}"} for i in range(20)],
    ("AUV", 2): [],
    ("AMB", 0): [{"id": i, "displayName": f"AMB-{i}"} for i in range(5)],
    ("AMB", 1): [],
}


def fake_request(site, path, query=None):
    page = int((query or {}).get("pageNumber", 0))
    return pages.get((site.code, page), [])


server._request = fake_request
try:
    rows, truncated, notes = server._collect(
        server._sites(""), "/visitors", None, max_rows=1000
    )
    check("120 + 5 lignes ramenees", len(rows) == 125, str(len(rows)))
    check("aucune troncature annoncee", truncated == [])
    check("la colonne site est posee", rows[0]["site"] == "AUV" and rows[-1]["site"] == "AMB")
    check("le compte par site est rapporte", notes == ["[AUV] 120 ligne(s)", "[AMB] 5 ligne(s)"], str(notes))

    rows, truncated, notes = server._collect(
        server._sites(""), "/visitors", None, max_rows=50
    )
    check("max_rows s'applique PAR SITE", len(rows) == 55, str(len(rows)))
    check("la troncature nomme le site", truncated == ["AUV"], str(truncated))

    rendered = server._render_table(rows, "entete", truncated, notes, "site,id")
    check(
        "le rendu crie la troncature",
        "ATTENTION" in rendered and "n'est PAS un total" in rendered,
    )
    check("le rendu est du CSV point-virgule", "site;id" in rendered, rendered[:200])

    # Un site qui tombe ne doit pas passer pour un perimetre complet.
    def half_broken(site, path, query=None):
        if site.code == "AMB":
            raise server.PeripassError("panne simulee")
        return fake_request(site, path, query)

    server._request = half_broken
    rows, truncated, notes = server._collect(
        server._sites(""), "/visitors", None, max_rows=1000
    )
    rendered = server._render_table(rows, "entete", truncated, notes, "site,id")
    check(
        "un site en panne rend le resultat explicitement PARTIEL",
        "PARTIEL" in rendered and "ECHEC" in rendered,
    )
finally:
    server._request = real_request

section("8. Aplatissement et projection")

row = {
    "id": 7,
    "displayName": "Camion",
    "currentHost": {"name": "Accueil", "email": "a@b.c"},
    "fields": {"Transporteur": "VIR", "Numero RDV": "R-1", "Vide": ""},
    "profiles": [{"id": 1}],
    "rien": None,
}
flat = server._flatten(row)
check("un objet imbrique devient une colonne pointee", flat["currentHost.name"] == "Accueil")
check("un champ personnalise aussi", flat["fields.Transporteur"] == "VIR")
check("une liste sort en JSON compact", flat["profiles"] == '[{"id": 1}]', str(flat["profiles"]))

sel = server._select([flat], "id,fields.*")
check("le joker de prefixe prend les champs personnalises", set(sel[0]) == {"id", "fields.Transporteur", "fields.Numero RDV", "fields.Vide"}, str(set(sel[0])))
sel = server._select([flat], "id,id,displayName")
check("les doublons de projection sont ecrases", list(sel[0]) == ["id", "displayName"])
sel = server._select([flat], "colonne_absente")
check("une colonne absente rend vide, sans lever", sel[0]["colonne_absente"] == "")
check("fields='*' rend tout", server._select([flat], "*")[0] == flat)

check(
    "VISITOR_BRIEF utilise le joker",
    "fields.*" in server.VISITOR_BRIEF and server.VISITOR_BRIEF.startswith("site,"),
)

section("9. Normalisation des reponses")

check("une liste nue est acceptee", len(server._rows([{"a": 1}, {"a": 2}])) == 2)
check("une enveloppe items est acceptee", len(server._rows({"items": [{"a": 1}]})) == 1)
check("une enveloppe content est acceptee", len(server._rows({"content": [{"a": 1}]})) == 1)
check("un objet seul devient une ligne", len(server._rows({"a": 1})) == 1)
check("le vide rend zero ligne", server._rows(None) == [])

section("10. Secrets : rien ne sort en clair")

check("une cle est masquee", server._mask("abcdefghij") == "ab*****hij")
check("une cle vide se dit", server._mask("") == "(non renseigne)")
check("l'empreinte fait 12 caracteres", len(server._fingerprint("x")) == 12)
check(
    "l'empreinte ne contient pas la cle",
    "secret" not in server._fingerprint("secret"),
)
check(
    "un dossier synchronise est detecte",
    server._is_synced(pathlib.Path(r"C:\Users\x\CAFOM\Transport BtoC - Documents\a")),
)
check(
    "le profil utilisateur ne l'est pas",
    not server._is_synced(pathlib.Path.home() / ".peripass-mcp" / "peripass.env"),
)
try:
    os.environ["PERIPASS_ENV_FILE"] = r"C:\Users\x\OneDrive\peripass.env"
    server._write_key_store({"PERIPASS_AUV_API_KEY": "x"})
    check("refus d'ecrire une cle dans un dossier synchronise", False)
except server.ConfigError as exc:
    check(
        "refus d'ecrire une cle dans un dossier synchronise",
        "synchronise" in str(exc),
    )
finally:
    os.environ["PERIPASS_ENV_FILE"] = str(HERE / "_inexistant_.env")

parsed = server._parse_env('A="va#leur"\n# commentaire\nB=simple # note\nexport C=3\n')
check("le # dans une valeur entre guillemets est preserve", parsed["A"] == "va#leur")
check("un commentaire en fin de ligne est retire", parsed["B"] == "simple")
check("export est tolere", parsed["C"] == "3")
parsed = server._parse_env("\ufeffPERIPASS_AUV_API_KEY=k")
check(
    "un BOM ne doit pas manger la premiere variable",
    "PERIPASS_AUV_API_KEY" in parsed or "\ufeffPERIPASS_AUV_API_KEY" in parsed,
)

section("11. La configuration d'equipe")

import tempfile  # noqa: E402


def _with_shared(text: str):
    """Ecrit un faux fichier d'equipe et vide le cache. Rend son chemin."""
    path = pathlib.Path(tempfile.gettempdir()) / "peripass.shared.env.test"
    path.write_text(text, encoding="utf-8")
    os.environ["PERIPASS_SHARED_ENV"] = str(path)
    server._SHARED_CACHE = None
    server._SHARED_LOADED_FROM = ""
    server._SHARED_REJECTED = []
    server._SHARED_LOCAL_SEEN = []
    return path


def _no_shared() -> None:
    os.environ["PERIPASS_SHARED_ENV"] = str(HERE / "_inexistant_.shared.env")
    server._SHARED_CACHE = None
    server._SHARED_LOADED_FROM = ""
    server._SHARED_REJECTED = []
    server._SHARED_LOCAL_SEEN = []


check("le fichier d'equipe s'appelle peripass.shared.env",
      server.SHARED_FILE_NAME == "peripass.shared.env")
check("il est cherche dans 08_ENGINE/04_mcp/00_config",
      server.ENGINE_DIR_NAME == "08_ENGINE"
      and server.SHARED_SUBPATH == ("04_mcp", "00_config"))

# La liste blanche : ce qui passe, ce qui ne passe pas.
check("un reglage global passe", server._shared_allows("PERIPASS_SITES"))
check("un reglage de site passe", server._shared_allows("PERIPASS_AUV_BASE_URL"))
check("une cle de site passe (cle de tenant, pas de personne)",
      server._shared_allows("PERIPASS_AMB_API_KEY"))
check("un site inconnu d'avance passe aussi",
      server._shared_allows("PERIPASS_MOULINS_2_TENANT"))
check("un chemin local ne passe pas",
      not server._shared_allows("PERIPASS_EXPORT_DIR"))
check("une cle sans prefixe de site ne passe pas",
      not server._shared_allows("PERIPASS_API_KEY"))
check("une faute de frappe ne passe pas",
      not server._shared_allows("PERIPASS_AUV_APIKEY"))
check("une cle d'un autre connecteur ne passe pas",
      not server._shared_allows("SHIPTIFY_API_KEY"))

# Les sections precedentes ont pose des cles dans l'environnement du processus,
# et le poste passe DEVANT l'equipe : tant qu'elles sont la, la couche d'equipe
# n'est jamais atteinte et les controles ci-dessous ne prouveraient rien. On les
# retire le temps de la verifier, on les remet ensuite.
_poste = {
    name: os.environ.pop(name)
    for name in ("PERIPASS_AUV_API_KEY", "PERIPASS_AMB_API_KEY")
    if name in os.environ
}

_with_shared(
    "\ufeffPERIPASS_SITES=AUV,AMB\n"
    "PERIPASS_AUV_TENANT=vu-auv\n"
    'PERIPASS_AUV_API_KEY="cle-d-equipe"\n'
    "PERIPASS_EXPORT_DIR=C:/chez-moi\n"
    "PERIPASS_AUV_APIKEY=faute-de-frappe\n"
)
shared = server._load_shared_env()
check("le fichier d'equipe est lu", bool(server._SHARED_LOADED_FROM))
check("un BOM ne mange pas la premiere variable du fichier d'equipe",
      shared.get("PERIPASS_SITES") == "AUV,AMB", str(sorted(shared)))
check("un reglage d'equipe est repris", shared.get("PERIPASS_AUV_TENANT") == "vu-auv")
check("une cle d'equipe est reprise", shared.get("PERIPASS_AUV_API_KEY") == "cle-d-equipe")
check("un chemin local est ecarte et signale",
      "PERIPASS_EXPORT_DIR" not in shared
      and "PERIPASS_EXPORT_DIR" in server._SHARED_LOCAL_SEEN)
check("une faute de frappe est ecartee et signalee",
      "PERIPASS_AUV_APIKEY" not in shared
      and "PERIPASS_AUV_APIKEY" in server._SHARED_REJECTED)

# Une valeur d'equipe ne doit PAS atterrir dans l'environnement du processus :
# elle serait heritee par tout sous-processus.
check("une cle d'equipe n'entre pas dans os.environ",
      not os.environ.get("PERIPASS_AUV_API_KEY"))

# L'ordre de priorite : le poste passe devant l'equipe.
check("a defaut de poste, la valeur d'equipe est retenue",
      server._env("PERIPASS_AUV_TENANT") == "vu-auv")
os.environ["PERIPASS_AUV_TENANT"] = "vu-recette"
check("le poste passe devant l'equipe",
      server._env("PERIPASS_AUV_TENANT") == "vu-recette")
del os.environ["PERIPASS_AUV_TENANT"]
check("une valeur absente des deux couches tombe sur le defaut",
      server._env("PERIPASS_ZZZ_TENANT", "defaut") == "defaut")

# Le rapport : il nomme les cles, il n'affiche jamais leur valeur.
report = "\n".join(server._shared_report())
check("le rapport d'equipe ne montre aucune cle en clair", "cle-d-equipe" not in report)
check("le rapport d'equipe nomme la cle sans la montrer",
      "PERIPASS_AUV_API_KEY" in report and "dont secrets" in report)
check("le rapport d'equipe signale les cles ignorees", "IGNOREES" in report)

# Une cle d'equipe rend le site pret, et setup_status doit le dire.
site = server._describe_site("AUV")
check("une cle d'equipe suffit a rendre un site pret", site.ready, site.problem)
check("l'origine de la cle nomme la config d'equipe",
      "equipe" in server._key_source("AUV"))
status = server.peripass_setup_status()
check("setup_status ne montre pas la cle d'equipe", "cle-d-equipe" not in status)
check("setup_status dit qu'il n'y a rien a saisir pour le site couvert",
      "RIEN a saisir pour AUV" in status)
check("setup_status demande quand meme la cle du site non couvert",
      "AMB" in status and "saisir les cles manquantes" in status)

# Fichier d'equipe absent : le connecteur tourne quand meme, et il le dit.
_no_shared()
check("un fichier d'equipe absent ne fait pas tomber le serveur",
      server._load_shared_env() == {})
check("l'absence de fichier d'equipe est signalee",
      "aucune" in "\n".join(server._shared_report()))

os.environ.update(_poste)

section("12. Surface MCP")

tools = [n for n in dir(server) if n.startswith("peripass_")]
check("20 outils exposes", len(tools) == 20, str(len(tools)))
import inspect  # noqa: E402

bad_signature = [
    name
    for name in tools
    if list(inspect.signature(getattr(server, name)).parameters) in (["args", "kwargs"],)
]
check(
    "aucun outil n'a perdu sa signature (functools.wraps)",
    not bad_signature,
    str(bad_signature),
)
secret_params = [
    name
    for name in tools
    for param in inspect.signature(getattr(server, name)).parameters
    if any(word in param.lower() for word in ("key", "token", "secret", "password"))
]
check(
    "aucun outil n'accepte un secret en parametre",
    not secret_params,
    str(secret_params),
)

section("13. Purete du flux stdout")

buf = io.StringIO()
real_stdout = sys.stdout
sys.stdout = buf
try:
    server.peripass_setup_status()
    server.peripass_sites()
    server.peripass_list_paths()
finally:
    sys.stdout = real_stdout
check(
    "aucun outil n'ecrit sur stdout (le protocole MCP y parle)",
    buf.getvalue() == "",
    repr(buf.getvalue()[:200]),
)

status = server.peripass_setup_status()
check("setup_status ne rend aucune cle en clair", "faux-pour-le-test" not in status)
doctor_like = server.peripass_sites()
check("peripass_sites ne rend aucune cle en clair", "faux-pour-le-test" not in doctor_like)

# --------------------------------------------------------------------------
print()
print("=" * 60)
print(f"{PASS} controle(s) passes, {len(FAIL)} echec(s)")
for name in FAIL:
    print(f"  ECHEC : {name}")
print()
print(
    "Rappel : ces controles ne touchent pas l'API Peripass. Le connecteur "
    "n'est pas verifie tant qu'une recette n'a pas ete passee contre un vrai "
    "tenant, avec une cle valide."
)
sys.exit(1 if FAIL else 0)
