#!/usr/bin/env python3
"""
MCP shiptify : interrogation en LECTURE SEULE de la base Shiptify.

Trois usages :
    python server.py            # mode serveur MCP (stdio) - lance par Claude Code
    python server.py doctor     # diagnostic de configuration et de connexion
    python server.py --help

Lecture seule par construction. L'API publique Shiptify expose 277 operations,
dont 91 en GET : ce serveur n'expose que les GET. Aucun POST, PUT, PATCH ni
DELETE n'est atteignable, y compris par l'outil generique shiptify_get, qui
valide le chemin demande contre la liste blanche des chemins GET du contrat
OpenAPI (openapi_get_paths.json, a cote de ce fichier).

Consequence a connaitre : creer un transport, annuler un envoi, confirmer un
enlevement ou poster un message dans un chat Shiptify ne se font pas ici. C'est
volontaire, et cela rejoint la regle du vault : jamais d'envoi automatique.

Contrat de reference : https://api-docs.shiptify.com/ (OpenAPI 3.0.2, spec
publiee a shiptify-public-api.openapi.json). Releve le 2026-08-27.

Configuration : un fichier .env, hors du vault. Voir README.md.
"""

from __future__ import annotations

import csv
import datetime as dt
import functools
import io
import json
import os
import pathlib
import re
import sys
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

DEFAULT_BASE_URL = "https://api.shiptify.com"
DEFAULT_AUTH_PREFIX = "Api-Key"

# Limite imposee par l'API sur la plupart des collections.
PAGE_LIMIT = 100
# /visits accepte 200.
PAGE_LIMIT_VISITS = 200

DEFAULT_TIMEOUT_S = 60.0
# Garde-fou de pagination : au-dela, on exporte en CSV plutot que de boucler.
DEFAULT_MAX_PAGES = 60

# Plafond de caracteres rendus dans la conversation par un outil de liste.
RENDER_MAX_CHARS = 24_000

# Dossiers synchronises : un secret n'y vit pas.
SYNCED_MARKERS = ("/onedrive", "cafom", "sharepoint", "dropbox", "google drive")

SPEC_FILE = pathlib.Path(__file__).resolve().parent / "openapi_get_paths.json"

# Projection par defaut pour les envois : un enregistrement Shiptify aplati
# porte plus de 100 colonnes, les rendre toutes sature la conversation pour
# rien. fields="*" rend tout.
SHIPMENT_BRIEF = (
    "id,code,status,internal_ref,other_reference,created_at,date,"
    "carrier.name,shipment_mode.name,"
    "address_from.city,address_from.country,"
    "address_dest.city,address_dest.zipcode,address_dest.country,"
    "total_weight,total_volume,total_linear_meters,cost,price,"
    "real_departure_time,real_arrival_time,tracking_code"
)


class ConfigError(RuntimeError):
    pass


class ShiptifyError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Liste blanche des chemins GET
# --------------------------------------------------------------------------

def _load_spec() -> dict[str, Any]:
    try:
        return json.loads(SPEC_FILE.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(
            f"Liste des chemins GET introuvable ({SPEC_FILE}) : {exc}. "
            "Ce fichier fait partie du serveur, il est versionne a cote de "
            "server.py. Regenere-le depuis "
            "https://api-docs.shiptify.com/shiptify-public-api.openapi.json."
        ) from exc


_SPEC: dict[str, Any] | None = None


def _spec() -> dict[str, Any]:
    global _SPEC
    if _SPEC is None:
        _SPEC = _load_spec()
    return _SPEC


def _get_patterns() -> list[tuple[str, re.Pattern[str]]]:
    out: list[tuple[str, re.Pattern[str]]] = []
    for path in _spec()["paths"]:
        # {id} -> [^/]+ ; le reste du chemin est litteral.
        regex = "^" + re.sub(r"\\\{[^/}]+\\\}", "[^/]+", re.escape(path)) + "$"
        out.append((path, re.compile(regex)))
    return out


# --------------------------------------------------------------------------
# Configuration : le fichier .env
# --------------------------------------------------------------------------

_ENV_LOADED_FROM: str = ""
_ENV_LOAD_ERROR: str = ""
_ENV_DONE: bool = False
# Cles qui viennent du fichier, pour que doctor sache dire d'ou sort la cle.
_ENV_FILE_KEYS: set[str] = set()


def _is_synced(path: pathlib.Path) -> bool:
    flat = str(path).replace("\\", "/").lower()
    return any(marker in flat for marker in SYNCED_MARKERS)


def _packaged_python() -> str:
    """Detecte un interpreteur Windows empaquete. Rend la raison, ou "".

    Pourquoi c'est ici et pas dans une note de bas de page : un Python livre
    par le Microsoft Store ou par le Python Manager tourne dans un conteneur
    d'application, et **ses processus enfants heritent d'une vue virtualisee de
    `%LOCALAPPDATA%`**. Mesure sur ce poste : le meme interpreteur de venv voit
    `['.env', 'venv']` lance directement, et `['venv']` seulement lance par le
    Python du Store - `LOCALAPPDATA` valant pourtant la meme chaine.

    Consequence : un `.env` pose sous `%LOCALAPPDATA%` par PowerShell peut etre
    **invisible** pour ce serveur, sans aucune erreur. On le detecte pour le
    dire, au lieu de rapporter "absent" et d'envoyer chercher un fichier qui
    est bien la.
    """
    flat = str(pathlib.Path(sys.base_prefix)).replace("\\", "/").lower()
    for marker in ("/windowsapps/", "/packages/pythonsoftwarefoundation", "/python/pythoncore-"):
        if marker in flat:
            return f"interpreteur empaquete detecte ({sys.base_prefix})"
    return ""


def _candidate_env_files() -> list[pathlib.Path]:
    """Emplacements de .env essayes, dans l'ordre."""
    out: list[pathlib.Path] = []
    explicit = (os.environ.get("SHIPTIFY_ENV_FILE") or "").strip()
    if explicit:
        out.append(pathlib.Path(explicit))
    # Mode plugin d'abord : ce dossier vit sous ~/.claude/plugins/data/, hors
    # de la zone que les interpreteurs empaquetes virtualisent. C'est donc le
    # seul emplacement de fichier fiable quand le plugin lance `python`.
    plugin_data = (os.environ.get("CLAUDE_PLUGIN_DATA") or "").strip()
    if plugin_data:
        out.append(pathlib.Path(plugin_data) / ".env")
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    out.append(pathlib.Path(base) / "shiptify-mcp" / ".env")
    out.append(pathlib.Path.home() / ".shiptify" / ".env")
    # Voisin du script : refuse si le vault est synchronise, mais on le regarde
    # quand meme pour pouvoir le dire clairement plutot que rester muet.
    out.append(pathlib.Path(__file__).resolve().parent / ".env")
    return out


def _parse_env(text: str) -> dict[str, str]:
    """Parseur .env minimal : KEY=VALUE, # en commentaire, guillemets optionnels.

    Une cle d'API peut contenir n'importe quoi (%, *, /, #). La valeur n'est
    donc jamais decoupee sur un # sans espace devant, et des guillemets la
    preservent telle quelle.
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        else:
            val = val.split(" #", 1)[0].rstrip()
        if key:
            out[key] = val
    return out


def _load_env_file() -> None:
    """Charge le premier .env exploitable. Une variable du process **non vide** gagne.

    Le "non vide" n'est pas un detail. En mode plugin, la configuration arrive
    par l'environnement : `SHIPTIFY_API_KEY=${user_config.api_key}`. Si le
    collegue n'a pas encore renseigne le champ, la substitution pose une
    variable **vide**, et un `setdefault` la considererait comme renseignee -
    le .env du poste serait alors ignore en silence. On ne remplit donc que ce
    qui est vide ou absent.
    """
    global _ENV_LOADED_FROM, _ENV_LOAD_ERROR, _ENV_DONE
    if _ENV_DONE:
        return
    _ENV_DONE = True
    for path in _candidate_env_files():
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        if _is_synced(path):
            _ENV_LOAD_ERROR = (
                f"Un fichier .env a ete trouve dans un dossier synchronise "
                f"({path}) et n'a PAS ete lu : il porterait une cle d'API sur "
                "le drive partage de l'equipe. Deplace-le vers "
                "%LOCALAPPDATA%\\shiptify-mcp\\.env (c'est ce que fait "
                "install.ps1), ou pointe un emplacement local avec "
                "SHIPTIFY_ENV_FILE."
            )
            continue
        try:
            values = _parse_env(path.read_text(encoding="utf-8-sig"))
        except OSError as exc:
            _ENV_LOAD_ERROR = f"Lecture impossible de {path} : {exc}"
            continue
        for key, val in values.items():
            if not os.environ.get(key):
                os.environ[key] = val
                _ENV_FILE_KEYS.add(key)
        _ENV_LOADED_FROM = str(path)
        return


def _env(name: str, default: str = "") -> str:
    _load_env_file()
    return (os.environ.get(name) or default).strip()


def _api_key() -> str:
    key = _env("SHIPTIFY_API_KEY")
    if not key:
        tried = "\n".join(f"  - {p}" for p in _candidate_env_files())
        packaged = _packaged_python()
        raise ConfigError(
            "SHIPTIFY_API_KEY manquant."
            + (f"\n\n{_ENV_LOAD_ERROR}" if _ENV_LOAD_ERROR else "")
            + (
                f"\n\nATTENTION : {packaged}. Un .env pose sous %LOCALAPPDATA% "
                "peut etre invisible pour ce processus (virtualisation). "
                "Renseigne la cle dans la configuration du plugin, ou pose le "
                ".env dans %CLAUDE_PLUGIN_DATA%, ou passe SHIPTIFY_ENV_FILE."
                if packaged
                else ""
            )
            + "\n\nEmplacements de .env essayes, dans l'ordre :\n"
            + tried
            + "\n\nEn mode plugin, le plus simple est de renseigner la cle dans "
            "la configuration du plugin (/plugin, champ « Cle d'API Shiptify »)."
            "\n\nSinon, cree le fichier avec install.ps1, a cote de server.py :\n"
            f'  powershell -ExecutionPolicy Bypass -File "{pathlib.Path(__file__).resolve().parent / "install.ps1"}"\n'
            "puis renseigne SHIPTIFY_API_KEY dedans. Modele : .env.example."
        )
    return key


def _base_url() -> str:
    return _env("SHIPTIFY_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _auth_prefix() -> str:
    return _env("SHIPTIFY_AUTH_PREFIX", DEFAULT_AUTH_PREFIX)


def _auth_header() -> str:
    return f"{_auth_prefix()} {_api_key()}".strip()


def _account_id() -> str:
    return _env("SHIPTIFY_ACCOUNT_ID")


def _timeout() -> float:
    try:
        return float(_env("SHIPTIFY_TIMEOUT_S") or DEFAULT_TIMEOUT_S)
    except ValueError:
        return DEFAULT_TIMEOUT_S


def _max_pages() -> int:
    try:
        return max(1, int(_env("SHIPTIFY_MAX_PAGES") or DEFAULT_MAX_PAGES))
    except ValueError:
        return DEFAULT_MAX_PAGES


def _export_dir() -> pathlib.Path:
    """Ou atterrissent les CSV. Quatre cas, dans cet ordre.

    Le serveur tourne dans deux contextes : installe dans le vault de
    Guillaume, ou installe comme plugin chez un collegue qui n'a pas de vault.
    Un chemin relatif au code serait juste dans le premier cas et absurde dans
    le second.
    """
    raw = _env("SHIPTIFY_EXPORT_DIR")
    if raw:
        return pathlib.Path(raw)
    # Mode plugin : dossier de donnees du plugin, qui survit aux mises a jour.
    plugin_data = (os.environ.get("CLAUDE_PLUGIN_DATA") or "").strip()
    if plugin_data:
        return pathlib.Path(plugin_data) / "exports"
    # Installe dans le vault : Assets/shiptify, avec les autres binaires.
    assets = pathlib.Path(__file__).resolve().parents[2] / "Assets"
    if assets.is_dir():
        return assets / "shiptify"
    return pathlib.Path.cwd() / "shiptify-exports"


def _key_source() -> str:
    """D'ou sort la cle : la premiere question quand ca ne marche pas."""
    _load_env_file()
    if not os.environ.get("SHIPTIFY_API_KEY"):
        return "(aucune)"
    if "SHIPTIFY_API_KEY" in _ENV_FILE_KEYS:
        return f"fichier {_ENV_LOADED_FROM}"
    if os.environ.get("CLAUDE_PLUGIN_ROOT"):
        return "configuration du plugin (userConfig)"
    return "environnement du processus"


def _mask(secret: str) -> str:
    if not secret:
        return "(non renseigne)"
    if len(secret) <= 6:
        return "*" * len(secret)
    return f"{secret[:2]}{'*' * (len(secret) - 5)}{secret[-3:]}"


# --------------------------------------------------------------------------
# Couche HTTP
# --------------------------------------------------------------------------

def _headers() -> dict[str, str]:
    head = {
        "Accept": "application/json",
        "Authorization": _auth_header(),
        "User-Agent": "myrddin-mcp-shiptify/1.0",
    }
    account = _account_id()
    if account:
        head["X-Account-ID"] = account
    return head


def _clean_query(query: dict[str, Any] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, val in (query or {}).items():
        if val is None or val == "":
            continue
        if isinstance(val, bool):
            out[key] = "true" if val else "false"
        else:
            out[key] = str(val)
    return out


def _check_get_path(path: str) -> str:
    """Normalise et valide un chemin contre la liste blanche des GET."""
    path = path.strip()
    if "?" in path:
        raise ShiptifyError(
            "Passe les parametres de requete dans query_json, pas dans le chemin."
        )
    if not path.startswith("/"):
        path = "/" + path
    patterns = _get_patterns()
    for _, pattern in patterns:
        if pattern.match(path):
            return path
    # Tolere l'oubli du slash final (/shipments vs /shipments/) et l'inverse.
    alt = path[:-1] if path.endswith("/") else path + "/"
    for _, pattern in patterns:
        if pattern.match(alt):
            return alt
    raise ShiptifyError(
        f"Chemin inconnu ou non accessible en lecture : {path}\n"
        f"Ce serveur est en lecture seule : seuls les {len(patterns)} chemins "
        "GET du contrat OpenAPI Shiptify sont atteignables. "
        "Appelle shiptify_list_paths pour voir la liste."
    )


def _supports_paging(path: str) -> bool:
    """L'endpoint accepte-t-il limit/offset ?

    Tous ne les acceptent pas, et ceux qui ne les acceptent pas les REFUSENT :
    /carriers/active rend un HTTP 400 "limit is not allowed" si on les envoie.
    La reponse est dans le contrat, on la lit plutot que de la deviner.
    """
    params = _spec()["paths"].get(path, {}).get("parameters", [])
    names = {p["name"] for p in params if p.get("in") == "query"}
    return {"limit", "offset"} <= names


def _page_limit_for(path: str) -> int:
    return PAGE_LIMIT_VISITS if path.rstrip("/") == "/visits" else PAGE_LIMIT


def _request(path: str, query: dict[str, Any] | None = None) -> Any:
    url = _base_url() + path
    try:
        resp = httpx.get(
            url,
            headers=_headers(),
            params=_clean_query(query),
            timeout=_timeout(),
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        raise ShiptifyError(f"Appel {path} impossible : {exc}") from exc

    if resp.status_code == 401:
        raise ShiptifyError(
            "HTTP 401 : cle d'API refusee. Verifie SHIPTIFY_API_KEY et "
            f"SHIPTIFY_AUTH_PREFIX (actuellement '{_auth_prefix()}'). "
            "Une cle revoquee rend le meme code."
        )
    if resp.status_code == 403:
        raise ShiptifyError(
            "HTTP 403 : cle valide mais compte non autorise sur cette "
            "ressource. Si l'API attend un compte explicite, renseigne "
            "SHIPTIFY_ACCOUNT_ID (shiptify_accounts liste les comptes "
            "autorises)."
        )
    if resp.status_code == 404:
        raise ShiptifyError(f"HTTP 404 sur {path} : ressource inexistante.")
    if resp.status_code == 429:
        raise ShiptifyError(
            "HTTP 429 : quota d'appels atteint. Reduis max_rows, ou resserre "
            "le perimetre de dates."
        )
    if resp.status_code >= 400:
        body = (resp.text or "")[:600]
        raise ShiptifyError(f"HTTP {resp.status_code} sur {path} | {body}")

    if not resp.content:
        return []
    try:
        return resp.json()
    except ValueError as exc:
        raise ShiptifyError(
            f"Reponse non JSON sur {path} : {(resp.text or '')[:300]}"
        ) from exc


def _rows(payload: Any) -> list[dict[str, Any]]:
    """L'API rend soit une liste, soit un objet {results|data|items: [...]}."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("results", "data", "items", "rows"):
            val = payload.get(key)
            if isinstance(val, list):
                return [r for r in val if isinstance(r, dict)]
        return [payload]
    return []


def _paginate(
    path: str,
    query: dict[str, Any] | None = None,
    max_rows: int = 1000,
    page_limit: int = PAGE_LIMIT,
) -> tuple[list[dict[str, Any]], bool]:
    """Boucle offset/limit jusqu'a page vide. Rend (lignes, tronque).

    L'API ne renvoie aucun total : la seule fin de collection fiable est une
    page vide. C'est la logique du script Power Query d'origine, et elle est
    conservee volontairement. S'arreter sur une page partielle ferait gagner un
    appel, mais sous-compterait en silence si l'API filtre apres avoir applique
    la limite - exactement le genre de chiffre faux qu'on ne veut pas citer.
    """
    out: list[dict[str, Any]] = []
    offset = 0
    pages = 0
    cap = _max_pages()
    while True:
        page_query = dict(query or {})
        page_query["limit"] = page_limit
        page_query["offset"] = offset
        batch = _rows(_request(path, page_query))
        out.extend(batch)
        pages += 1
        if not batch:
            return out, False
        if len(out) >= max_rows:
            return out[:max_rows], True
        if pages >= cap:
            return out, True
        offset += page_limit


# --------------------------------------------------------------------------
# Aplatissement et rendu
# --------------------------------------------------------------------------

def _flatten(row: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Aplatit les objets imbriques en notation pointee.

    Reproduit ce que faisait Table.ExpandRecordColumn dans le script Power
    Query : address_dest.city, carrier.name, shipment_mode.id. Les listes sont
    rendues en JSON compact : elles n'ont pas de forme de colonne stable.
    """
    out: dict[str, Any] = {}
    for key, val in row.items():
        name = f"{prefix}{key}"
        if isinstance(val, dict):
            out.update(_flatten(val, prefix=f"{name}."))
        elif isinstance(val, list):
            out[name] = json.dumps(val, ensure_ascii=False, default=str) if val else ""
        else:
            out[name] = val
    return out


def _columns(rows: list[dict[str, Any]]) -> list[str]:
    """Union des colonnes, dans l'ordre de premiere apparition."""
    seen: dict[str, None] = {}
    for row in rows:
        for key in row:
            seen.setdefault(key, None)
    return list(seen)


def _select(rows: list[dict[str, Any]], fields: str) -> list[dict[str, Any]]:
    wanted = [f.strip() for f in fields.split(",") if f.strip()]
    if not wanted or wanted == ["*"]:
        return rows
    return [{f: r.get(f, "") for f in wanted} for r in rows]


def _to_csv_text(rows: list[dict[str, Any]], delimiter: str = ";") -> str:
    if not rows:
        return ""
    cols = _columns(rows)
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=cols,
        delimiter=delimiter,
        extrasaction="ignore",
        lineterminator="\n",
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({c: row.get(c, "") for c in cols})
    return buf.getvalue()


def _render_table(
    rows: list[dict[str, Any]],
    header: str,
    truncated: bool,
    fields: str = "",
    max_chars: int = RENDER_MAX_CHARS,
) -> str:
    """Rend une collection en CSV point-virgule, borne en taille."""
    flat = _select([_flatten(r) for r in rows], fields)
    lines = [header, f"{len(rows)} ligne(s) rendues."]
    if truncated:
        lines.append(
            "ATTENTION : resultat tronque (max_rows ou garde-fou de pagination "
            "atteint). Le compte ci-dessus n'est PAS un total : ne le cite pas "
            "comme un volume. Pour un perimetre complet, shiptify_export_csv."
        )
    if not flat:
        lines.append("(aucune ligne)")
        return "\n".join(lines)

    body = _to_csv_text(flat)
    if len(body) > max_chars:
        kept: list[str] = []
        size = 0
        for line in body.splitlines():
            if size + len(line) > max_chars:
                break
            kept.append(line)
            size += len(line) + 1
        body = "\n".join(kept)
        lines.append(
            f"Rendu limite a {max(0, len(kept) - 1)} ligne(s) pour ne pas "
            "saturer la conversation. Restreins avec fields=..., ou exporte "
            "en CSV."
        )
    lines.append("")
    lines.append(body)
    return "\n".join(lines)


def _render_json(payload: Any, header: str, max_chars: int = RENDER_MAX_CHARS) -> str:
    body = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if len(body) > max_chars:
        body = body[:max_chars] + "\n... (tronque)"
    return f"{header}\n\n{body}"


def _guard(fn):
    """Rend les erreurs de configuration et d'API comme message lisible.

    functools.wraps n'est pas cosmetique ici : il pose __wrapped__, que
    inspect.signature suit. Sans lui, FastMCP lit la signature du wrapper et
    publie chaque outil avec deux parametres (args, kwargs) au lieu des vrais.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ConfigError, ShiptifyError) as exc:
            return f"ERREUR : {exc}"

    return wrapper


# --------------------------------------------------------------------------
# Outils MCP
# --------------------------------------------------------------------------

mcp = FastMCP("shiptify")


@mcp.tool()
def shiptify_doctor() -> str:
    """Diagnostic : configuration lue, cle presente, et vrai appel a l'API.

    Le premier outil a appeler quand quelque chose coince. Ne rend jamais la
    cle en clair.
    """
    _load_env_file()
    lines = ["Configuration du serveur MCP shiptify", ""]
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    lines.append(
        f"Mode                     : {'plugin (' + plugin_root + ')' if plugin_root else 'installation directe'}"
    )
    lines.append(f"Origine de la cle        : {_key_source()}")
    lines.append(f"Fichier .env lu          : {_ENV_LOADED_FROM or '(aucun)'}")
    if _ENV_LOAD_ERROR:
        lines.append(f"Avertissement            : {_ENV_LOAD_ERROR}")
    lines.append(f"Emplacements essayes     :")
    for path in _candidate_env_files():
        mark = "OK" if str(path) == _ENV_LOADED_FROM else "  "
        try:
            exists = "present" if path.is_file() else "absent"
        except OSError as exc:
            exists = f"illisible : {exc}"
        lines.append(f"  [{mark}] {path} ({exists})")
    packaged = _packaged_python()
    if packaged and not _env("SHIPTIFY_API_KEY"):
        lines.append("")
        lines.append(f"  ATTENTION : {packaged}.")
        lines.append(
            "  Un .env pose sous %LOCALAPPDATA% peut etre INVISIBLE pour ce "
            "processus (virtualisation du conteneur d'application) : il est "
            "alors rapporte 'absent' alors qu'il existe. Renseigne la cle dans "
            "la configuration du plugin, ou pose le .env dans "
            "%CLAUDE_PLUGIN_DATA%, ou passe SHIPTIFY_ENV_FILE."
        )
    lines.append("")
    lines.append(f"SHIPTIFY_BASE_URL        : {_base_url()}")
    lines.append(f"SHIPTIFY_AUTH_PREFIX     : {_auth_prefix()}")
    lines.append(f"SHIPTIFY_API_KEY         : {_mask(_env('SHIPTIFY_API_KEY'))}")
    lines.append(f"SHIPTIFY_ACCOUNT_ID      : {_account_id() or '(non renseigne)'}")
    lines.append(f"SHIPTIFY_MAX_PAGES       : {_max_pages()}")
    lines.append(f"SHIPTIFY_TIMEOUT_S       : {_timeout()}")
    lines.append(f"Dossier d'export         : {_export_dir()}")
    try:
        spec = _spec()
        lines.append(
            f"Chemins GET connus       : {len(spec['paths'])} "
            f"(contrat {spec.get('api_version')}, releve {spec.get('released')})"
        )
    except ConfigError as exc:
        lines.append(f"Chemins GET connus       : ERREUR - {exc}")

    lines.append("")
    lines.append("Appel de verification")
    if not _env("SHIPTIFY_API_KEY"):
        lines.append("  Aucune cle : appel non tente.")
        return "\n".join(lines)
    for path, label in (("/", "racine"), ("/accounts/", "comptes autorises")):
        try:
            payload = _request(path)
            preview = json.dumps(payload, ensure_ascii=False, default=str)[:300]
            lines.append(f"  GET {path} ({label}) : OK | {preview}")
        except (ConfigError, ShiptifyError) as exc:
            lines.append(f"  GET {path} ({label}) : ECHEC | {exc}")
    return "\n".join(lines)


@mcp.tool()
@_guard
def shiptify_list_paths(contains: str = "") -> str:
    """Liste les chemins GET atteignables et leurs parametres.

    A appeler avant shiptify_get, pour ne pas deviner un chemin ou un nom de
    filtre. `contains` filtre sur le chemin ou le resume.
    """
    spec = _spec()
    needle = contains.strip().lower()
    lines = [
        f"Chemins GET de l'API Shiptify ({len(spec['paths'])} au total, "
        f"contrat {spec.get('api_version')}, releve {spec.get('released')})",
        f"Source : {spec.get('source')}",
        "",
    ]
    shown = 0
    for path in sorted(spec["paths"]):
        info = spec["paths"][path]
        summary = info.get("summary", "")
        if needle and needle not in path.lower() and needle not in summary.lower():
            continue
        shown += 1
        lines.append(f"{path}  -  {summary}")
        for param in info.get("parameters", []):
            if param.get("in") == "header":
                continue
            bits = [f"{param['in']} {param['name']}", str(param.get("type") or "?")]
            if param.get("required"):
                bits.append("requis")
            if param.get("enum"):
                bits.append("valeurs " + ", ".join(str(v) for v in param["enum"]))
            desc = param.get("description") or ""
            line = "    " + " | ".join(bits)
            if desc:
                line += f" | {desc[:120]}"
            lines.append(line)
    if not shown:
        lines.append(f"(aucun chemin ne correspond a '{contains}')")
    return "\n".join(lines)


@mcp.tool()
@_guard
def shiptify_accounts(account_type: str = "") -> str:
    """Comptes Shiptify autorises pour cette cle d'API.

    account_type : 'shipper', 'carrier', ou vide pour les deux. A appeler en
    premier si un appel rend un HTTP 403 : l'identifiant a mettre dans
    SHIPTIFY_ACCOUNT_ID vient de la.
    """
    payload = _request("/accounts/", {"type": account_type})
    return _render_json(payload, "GET /accounts/")


@mcp.tool()
@_guard
def shiptify_list_shipments(
    created_date_from: str = "",
    created_date_to: str = "",
    departure_date_min: str = "",
    departure_date_max: str = "",
    arrival_date_min: str = "",
    arrival_date_max: str = "",
    shipment_request_id: int = 0,
    shipment_request_internal_ref: str = "",
    shipper_id: int = 0,
    from_address_id: int = 0,
    dest_address_id: int = 0,
    from_address_internal_ref: str = "",
    dest_address_internal_ref: str = "",
    max_rows: int = 300,
    fields: str = "",
) -> str:
    """Les envois (shipments), filtres et pagines. L'outil principal.

    Dates au format YYYY-MM-DD (ou YYYY-MM-DDTHH:mm:ss). fields est une liste
    de colonnes separees par des virgules, en notation pointee
    (carrier.name, address_dest.zipcode) ; '*' rend les 100+ colonnes.
    Par defaut, une projection courte des colonnes utiles.

    Pour un volume qui depasse quelques centaines de lignes, prefere
    shiptify_export_csv : la conversation n'est pas un entrepot.
    """
    query = {
        "created_date_from": created_date_from,
        "created_date_to": created_date_to,
        "departure_date_min": departure_date_min,
        "departure_date_max": departure_date_max,
        "arrival_date_min": arrival_date_min,
        "arrival_date_max": arrival_date_max,
        "sh_request_id": shipment_request_id or "",
        "sr_internal_ref": shipment_request_internal_ref,
        "shipper_id": shipper_id or "",
        "from_address_id": from_address_id or "",
        "dest_address_id": dest_address_id or "",
        "from_address_internal_ref": from_address_internal_ref,
        "dest_address_internal_ref": dest_address_internal_ref,
    }
    rows, truncated = _paginate("/shipments/", query, max_rows=max_rows)
    header = "GET /shipments/ | filtres : " + json.dumps(
        _clean_query(query), ensure_ascii=False
    )
    return _render_table(rows, header, truncated, fields or SHIPMENT_BRIEF)


@mcp.tool()
@_guard
def shiptify_get_shipment(shipment_id: int, include: str = "") -> str:
    """Un envoi et, au choix, ses sous-ressources.

    include : liste separee par des virgules parmi tracking-points, contents,
    attachments, metadata, sscc. Vide = l'envoi seul.
    """
    out: dict[str, Any] = {
        "shipment": _request(f"/shipments/{int(shipment_id)}")
    }
    allowed = {
        "tracking-points": f"/shipments/{int(shipment_id)}/tracking-points",
        "attachments": f"/shipments/{int(shipment_id)}/attachments",
        "metadata": f"/shipments/{int(shipment_id)}/metadata",
        "sscc": f"/shipments/{int(shipment_id)}/sscc",
        # contents n'existe en GET que sur le chemin galaxy.
        "contents": f"/galaxy/shipments/{int(shipment_id)}/contents",
    }
    for name in [x.strip() for x in include.split(",") if x.strip()]:
        if name not in allowed:
            out[name] = f"inconnu. Valeurs possibles : {', '.join(sorted(allowed))}"
            continue
        try:
            out[name] = _request(allowed[name])
        except ShiptifyError as exc:
            out[name] = f"ERREUR : {exc}"
    return _render_json(out, f"Envoi {shipment_id}")


@mcp.tool()
@_guard
def shiptify_list_shipment_requests(
    internal_ref: str = "", max_rows: int = 300, fields: str = ""
) -> str:
    """Les demandes de transport (shipment requests), paginees.

    Dans le vocabulaire maison, c'est la demande envoyee vers l'agence ; les
    envois qui en decoulent se lisent avec shiptify_get_shipment_request
    (include=shipments).
    """
    rows, truncated = _paginate(
        "/shipment-requests/", {"internal_ref": internal_ref}, max_rows=max_rows
    )
    return _render_table(rows, "GET /shipment-requests/", truncated, fields)


@mcp.tool()
@_guard
def shiptify_get_shipment_request(
    shipment_request_id: int = 0, internal_ref: str = "", include: str = ""
) -> str:
    """Une demande de transport, par identifiant ou par reference interne.

    include : shipments, pre-shipments, contents, attachments, metadata,
    invoicing, invoice-line, tracking-points.
    tracking-points et invoice-line ne sont accessibles que par reference
    interne cote API : renseigne internal_ref pour les obtenir.
    """
    if not shipment_request_id and not internal_ref:
        raise ShiptifyError(
            "Donne shipment_request_id ou internal_ref."
        )
    out: dict[str, Any] = {}
    sid = int(shipment_request_id) if shipment_request_id else 0
    ref = internal_ref.strip()

    if sid:
        out["shipment_request"] = _request(f"/shipment-requests/{sid}")

    by_id = {
        "shipments": f"/shipment-requests/{sid}/shipments",
        "pre-shipments": f"/shipment-requests/{sid}/pre-shipments",
        "contents": f"/shipment-requests/{sid}/contents",
        "attachments": f"/shipment-requests/{sid}/attachments",
        "metadata": f"/shipment-requests/{sid}/metadata",
        "invoicing": f"/shipment-requests/{sid}/invoicing/details",
        "invoice-line": f"/shipment-requests/{sid}/invoice-line",
    }
    by_ref = {
        "shipments": f"/shipment-requests/ref/{ref}/shipments",
        "pre-shipments": f"/shipment-requests/ref/{ref}/pre-shipments",
        "attachments": f"/shipment-requests/ref/{ref}/attachments",
        "invoice-line": f"/shipment-requests/ref/{ref}/invoice-line",
        "tracking-points": f"/shipment-requests/ref/{ref}/shipments/tracking-points",
    }
    for name in [x.strip() for x in include.split(",") if x.strip()]:
        path = by_id.get(name) if sid else None
        if path is None and ref:
            path = by_ref.get(name)
        if path is None:
            known = sorted(set(by_id) | set(by_ref))
            out[name] = (
                f"non disponible avec ce mode d'acces. Valeurs possibles : "
                f"{', '.join(known)}"
            )
            continue
        try:
            out[name] = _request(path)
        except ShiptifyError as exc:
            out[name] = f"ERREUR : {exc}"
    if not out:
        out["shipment_request"] = "aucune ressource demandee pour cette reference"
    label = f"Demande de transport {sid or ref}"
    return _render_json(out, label)


@mcp.tool()
@_guard
def shiptify_list_invoices(
    status: str = "",
    accounting_month: str = "",
    carrier_id: str = "",
    max_rows: int = 300,
    fields: str = "",
) -> str:
    """Les factures transporteur. accounting_month au format YYYY-MM.

    C'est l'entree du controle de facturation : la facture cote Shiptify, a
    rapprocher de l'estimation du back-office. Le detail ligne a ligne se lit
    avec shiptify_list_invoice_lines.
    """
    query = {
        "status": status,
        "accounting_month": accounting_month,
        "carrier_id": carrier_id,
    }
    rows, truncated = _paginate("/invoices", query, max_rows=max_rows)
    header = "GET /invoices | filtres : " + json.dumps(
        _clean_query(query), ensure_ascii=False
    )
    return _render_table(rows, header, truncated, fields)


@mcp.tool()
@_guard
def shiptify_get_invoice(invoice_id: int) -> str:
    """Une facture par identifiant, avec ses rattachements."""
    return _render_json(
        _request(f"/invoices/{int(invoice_id)}"), f"Facture {invoice_id}"
    )


@mcp.tool()
@_guard
def shiptify_list_invoice_lines(
    status: str = "",
    account_id: int = 0,
    carrier_id: int = 0,
    from_pick_up_date: str = "",
    to_pick_up_date: str = "",
    from_delivery_date: str = "",
    to_delivery_date: str = "",
    from_accounting_date: str = "",
    to_accounting_date: str = "",
    max_rows: int = 500,
    fields: str = "",
) -> str:
    """Les lignes de facturation, filtrees par transporteur, dates ou statut.

    status : new, started, accepted, blocked, closed, not_priced,
    ready_to_invoice.
    Dates d'enlevement et de livraison au format YYYY-MM-DD ; dates comptables
    au format YYYY-MM.

    La granularite utile pour un ecart de facturation : c'est ici que se lit le
    prix ligne a ligne.
    """
    query = {
        "status": status,
        "account_id": account_id or "",
        "carrier_id": carrier_id or "",
        "from_pick_up_date": from_pick_up_date,
        "to_pick_up_date": to_pick_up_date,
        "from_delivery_date": from_delivery_date,
        "to_delivery_date": to_delivery_date,
        "from_accounting_date": from_accounting_date,
        "to_accounting_date": to_accounting_date,
    }
    rows, truncated = _paginate("/galaxy/invoice-lines", query, max_rows=max_rows)
    header = "GET /galaxy/invoice-lines | filtres : " + json.dumps(
        _clean_query(query), ensure_ascii=False
    )
    return _render_table(rows, header, truncated, fields)


@mcp.tool()
@_guard
def shiptify_list_orders(
    calculated_departure_date_from: str = "",
    calculated_departure_date_to: str = "",
    shipment_id: int = 0,
    shipment_request_id: int = 0,
    max_rows: int = 300,
    fields: str = "",
) -> str:
    """Les commandes (orders), filtrees par date de depart estimee ou par envoi."""
    query = {
        "calculated_departure_date_from": calculated_departure_date_from,
        "calculated_departure_date_to": calculated_departure_date_to,
        "shipment_id": shipment_id or "",
        "shipment_request_id": shipment_request_id or "",
    }
    rows, truncated = _paginate("/orders", query, max_rows=max_rows)
    header = "GET /orders | filtres : " + json.dumps(
        _clean_query(query), ensure_ascii=False
    )
    return _render_table(rows, header, truncated, fields)


@mcp.tool()
@_guard
def shiptify_list_locations(
    q: str = "", internal_ref: str = "", max_rows: int = 300, fields: str = ""
) -> str:
    """Les lieux (agences, entrepots, points de livraison).

    q cherche dans nom, adresse, ville, pays, reference interne. Utile pour
    retrouver le code d'une agence avant de filtrer les envois dessus.
    """
    rows, truncated = _paginate(
        "/locations", {"q": q, "internal_ref": internal_ref}, max_rows=max_rows
    )
    return _render_table(rows, "GET /locations", truncated, fields)


@mcp.tool()
@_guard
def shiptify_list_carriers(internal_ref: str = "") -> str:
    """Les transporteurs actifs du compte.

    A croiser avec l'annuaire du contexte d'equipe
    (01_CONTEXTE/PORTEFEUILLE_ET_ZONES.md) : un nom Shiptify n'est pas toujours
    le nom maison du transporteur.
    """
    payload = _request("/carriers/active", {"internal_ref": internal_ref})
    rows = _rows(payload)
    return _render_table(rows, "GET /carriers/active", False)


@mcp.tool()
@_guard
def shiptify_list_events(
    event: str,
    date_from: str = "",
    date_to: str = "",
    max_rows: int = 300,
    fields: str = "",
) -> str:
    """Le journal d'evenements. `event` est obligatoire cote API.

    Valeurs : create_shipment, cancel_shipment, cancel_shipment_request,
    reactivate_shipment, update_shipment_contents, update_tracking,
    update_shipment_dates, added_shipment_to_group,
    removed_shipment_from_group, update_shipment_request_price,
    accept_shipment_request_price, refuse_shipment_request_price.
    """
    query = {"event": event, "date_from": date_from, "date_to": date_to}
    rows, truncated = _paginate("/events", query, max_rows=max_rows)
    header = "GET /events | filtres : " + json.dumps(
        _clean_query(query), ensure_ascii=False
    )
    return _render_table(rows, header, truncated, fields)


_DICTIONARIES = {
    "shipment-modes": "/dictionary/shipment-modes",
    "metadata-prototypes": "/dictionary/metadata-prototypes",
    "metadata-prototypes-active": "/metadata-prototypes/active",
    "causes": "/dictionary/causes",
    "incidents": "/dictionary/incidents",
    "claims": "/dictionary/claims",
    "attachment-types": "/dictionary/attachment-types",
    "dangerous-goods": "/dictionary/dangerous-goods",
    "shipment-price-details": "/dictionary/shipment-price-details",
    "additional-services": "/dictionary/additional-services",
    "additional-services-active": "/additional-services/active",
    "specificities": "/specificities",
    "accounting-entities": "/accounting-entities",
    "price-details": "/price-details",
    "content-types": "/content-types",
    "content-types-active": "/content-types/active",
    "tags": "/tags",
    "freight-units": "/freight-units",
}


@mcp.tool()
@_guard
def shiptify_dictionary(name: str = "") -> str:
    """Les referentiels : modes, causes, incidents, litiges, services, tags...

    Appeler sans argument liste les referentiels disponibles. Ce sont les
    valeurs que l'API accepte et rend : les lire evite de deviner un libelle.
    """
    key = name.strip().lower()
    if not key:
        return "Referentiels disponibles :\n" + "\n".join(
            f"  {k:32} {v}" for k, v in sorted(_DICTIONARIES.items())
        )
    if key not in _DICTIONARIES:
        raise ShiptifyError(
            f"Referentiel inconnu : {name}. Valeurs : "
            + ", ".join(sorted(_DICTIONARIES))
        )
    path = _DICTIONARIES[key]
    rows = _rows(_request(path))
    return _render_table(rows, f"GET {path}", False)


@mcp.tool()
@_guard
def shiptify_export_csv(
    path: str,
    query_json: str = "{}",
    filename: str = "",
    max_rows: int = 100000,
) -> str:
    """Pagine une collection entiere et l'ecrit en CSV, sans charger la conversation.

    C'est l'equivalent du script Power Query : meme pagination, meme
    aplatissement des objets imbriques en colonnes pointees
    (address_dest.city, carrier.name). CSV point-virgule, UTF-8 avec BOM,
    ouvrable directement dans Excel FR.

    path : un chemin GET de collection, par exemple /shipments/ ou
    /galaxy/invoice-lines. query_json : les filtres, en JSON.
    Le fichier va dans <vault>/Assets/shiptify/ sauf SHIPTIFY_EXPORT_DIR.
    """
    checked = _check_get_path(path)
    try:
        query = json.loads(query_json or "{}")
    except ValueError as exc:
        raise ShiptifyError(f"query_json n'est pas du JSON valide : {exc}") from exc
    if not isinstance(query, dict):
        raise ShiptifyError("query_json doit etre un objet JSON.")

    if _supports_paging(checked):
        rows, truncated = _paginate(
            checked, query, max_rows=max_rows, page_limit=_page_limit_for(checked)
        )
    else:
        # Referentiels et collections non paginees : un seul appel, et surtout
        # pas de limit/offset, que ces endpoints refusent.
        rows = _rows(_request(checked, query))
        truncated = len(rows) > max_rows
        rows = rows[:max_rows]
    flat = [_flatten(r) for r in rows]

    target_dir = _export_dir()
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ShiptifyError(f"Dossier d'export inutilisable ({target_dir}) : {exc}") from exc

    if filename.strip():
        name = filename.strip()
        if not name.lower().endswith(".csv"):
            name += ".csv"
    else:
        slug = re.sub(r"[^a-z0-9]+", "_", checked.lower()).strip("_") or "export"
        name = f"shiptify_{slug}_{dt.date.today().isoformat()}.csv"
    target = target_dir / name

    try:
        target.write_text(_to_csv_text(flat), encoding="utf-8-sig")
    except OSError as exc:
        raise ShiptifyError(f"Ecriture impossible dans {target} : {exc}") from exc

    cols = _columns(flat)
    out = [
        f"Export termine : {target}",
        f"{len(flat)} ligne(s), {len(cols)} colonne(s).",
        f"Requete : GET {checked} | filtres : "
        + json.dumps(_clean_query(query), ensure_ascii=False),
    ]
    if truncated:
        out.append(
            "ATTENTION : export tronque (max_rows ou SHIPTIFY_MAX_PAGES "
            "atteint). Le fichier n'est PAS le perimetre complet : resserre "
            "les dates, ou releve SHIPTIFY_MAX_PAGES."
        )
    if cols:
        out.append("")
        out.append("Colonnes : " + ", ".join(cols[:60]))
        if len(cols) > 60:
            out.append(f"... et {len(cols) - 60} autres.")
    return "\n".join(out)


@mcp.tool()
@_guard
def shiptify_get(
    path: str, query_json: str = "{}", max_rows: int = 200, fields: str = ""
) -> str:
    """Appelle n'importe quel chemin GET du contrat Shiptify. Echappatoire.

    Pour les 70 chemins GET qui n'ont pas d'outil dedie : visites, creneaux,
    points de suivi, unites de fret, pieces jointes, chemins carrier/galaxy.
    Appelle shiptify_list_paths d'abord pour le chemin exact et ses filtres.

    Un chemin absent de la liste blanche est refuse, et seule la methode GET
    est emise : aucune ecriture n'est possible par cet outil.
    """
    checked = _check_get_path(path)
    try:
        query = json.loads(query_json or "{}")
    except ValueError as exc:
        raise ShiptifyError(f"query_json n'est pas du JSON valide : {exc}") from exc
    if not isinstance(query, dict):
        raise ShiptifyError("query_json doit etre un objet JSON.")

    header = f"GET {checked} | filtres : " + json.dumps(
        _clean_query(query), ensure_ascii=False
    )
    if _supports_paging(checked) and "limit" not in query and "offset" not in query:
        rows, truncated = _paginate(
            checked, query, max_rows=max_rows, page_limit=_page_limit_for(checked)
        )
        return _render_table(rows, header, truncated, fields)
    payload = _request(checked, query)
    rows = _rows(payload)
    if isinstance(payload, list) or (rows and len(rows) > 1):
        return _render_table(rows[:max_rows], header, len(rows) > max_rows, fields)
    return _render_json(payload, header)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

HELP = """\
MCP shiptify - interrogation en lecture seule de la base Shiptify.

  python server.py            mode serveur MCP (stdio), lance par Claude Code
  python server.py doctor     diagnostic de configuration et de connexion
  python server.py --help     cette aide

Configuration : un fichier .env hors du vault. Emplacement par defaut
%LOCALAPPDATA%\\shiptify-mcp\\.env, cree par install.ps1. Modele : .env.example.
Voir README.md.
"""


def main() -> int:
    arg = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    if arg in {"-h", "--help", "help"}:
        print(HELP)
        return 0
    if arg == "doctor":
        report = shiptify_doctor()
        print(report)
        return 0 if " : OK |" in report else 1
    if arg:
        print(f"Argument inconnu : {arg}\n")
        print(HELP)
        return 2
    mcp.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
