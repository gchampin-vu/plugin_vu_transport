#!/usr/bin/env python3
"""
Amorce du serveur MCP indices-eu, lancee par le plugin.

Le plugin ne peut pas supposer que `mcp`, `httpx` et `openpyxl` sont installes
sur le poste du collegue : un plugin s'installe en une commande, il n'ouvre pas
un terminal pour faire un `pip install`. Cette amorce s'en charge, une fois, au
premier demarrage.

    python bootstrap.py            # prepare l'environnement puis lance le serveur
    python bootstrap.py doctor     # idem, puis diagnostic

REGLE ABSOLUE : rien sur la sortie standard. Le serveur MCP parle JSON-RPC sur
stdout, et une seule ligne de `pip` egaree casse le protocole en silence. Toute
sortie de `venv` et de `pip` part donc sur stderr, ou Claude Code la lit comme
un journal.
"""

from __future__ import annotations

import os
import pathlib
import runpy
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
SERVER = HERE / "server.py"
REQUIREMENTS = HERE / "requirements.txt"

# Ce dont le serveur a besoin pour demarrer. openpyxl n'est pas optionnel : le
# bulletin petrolier n'est publie qu'en classeur Excel, il n'y a pas d'API.
NEEDED = ("mcp", "httpx", "openpyxl")


def log(message: str) -> None:
    """Journal sur stderr. Jamais stdout."""
    print(f"[indices-mcp] {message}", file=sys.stderr, flush=True)


def has_dependencies(python: pathlib.Path | None = None) -> bool:
    """Les dependances sont-elles importables ?"""
    probe = "import " + ", ".join(NEEDED)
    if python is None:
        try:
            exec(compile(probe, "<probe>", "exec"), {})
        except ImportError:
            return False
        return True
    try:
        done = subprocess.run(
            [str(python), "-c", probe],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def venv_python(venv: pathlib.Path) -> pathlib.Path:
    """L'interpreteur d'un environnement virtuel, des deux cotes.

    Windows le pose sous Scripts/, tout le reste sous bin/. Le test est ici, et
    ici seulement - pas recopie a cinq endroits.
    """
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def default_root() -> pathlib.Path:
    """Racine locale de l'outil, hors de tout dossier synchronise.

    PAS %LOCALAPPDATA%. Un Python empaquete (Microsoft Store, Python Manager)
    donne a ses processus enfants une vue VIRTUALISEE de %LOCALAPPDATA% : le
    plugin ecrirait d'un cote et un terminal lirait de l'autre, sans erreur. Le
    profil utilisateur, lui, n'est pas virtualise. C'est la lecon mesuree en
    empaquetant le connecteur Yooz le 2026-08-27.
    """
    return pathlib.Path.home() / ".indices-mcp"


def candidate_venvs() -> list[pathlib.Path]:
    """Emplacements de venv, dans l'ordre de preference."""
    out: list[pathlib.Path] = []
    override = (os.environ.get("INDICES_MCP_VENV") or "").strip()
    if override:
        out.append(pathlib.Path(override))
    out.append(default_root() / "venv")
    plugin_data = (os.environ.get("CLAUDE_PLUGIN_DATA") or "").strip()
    if plugin_data:
        out.append(pathlib.Path(plugin_data) / "venv")
    return out


def create_venv(venv: pathlib.Path) -> pathlib.Path:
    log(f"premier demarrage : creation de l'environnement dans {venv}")
    venv.parent.mkdir(parents=True, exist_ok=True)
    done = subprocess.run(
        [sys.executable, "-m", "venv", str(venv)],
        stdout=sys.stderr,
        stderr=sys.stderr,
    )
    if done.returncode != 0:
        raise RuntimeError(
            f"creation de l'environnement virtuel impossible dans {venv}. "
            f"Python utilise : {sys.executable}"
        )
    return venv_python(venv)


def install_dependencies(python: pathlib.Path) -> None:
    log("installation des dependances (mcp, httpx, openpyxl) - une seule fois")
    for args in (
        [str(python), "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
        [str(python), "-m", "pip", "install", "--quiet", "-r", str(REQUIREMENTS)],
    ):
        done = subprocess.run(args, stdout=sys.stderr, stderr=sys.stderr)
        if done.returncode != 0:
            raise RuntimeError(
                "installation des dependances impossible. Si le poste est "
                "derriere un proxy, configure pip avant de relancer "
                "(variables HTTP_PROXY / HTTPS_PROXY)."
            )


def run_server() -> None:
    """Lance le serveur dans l'interpreteur courant."""
    sys.argv = [str(SERVER), *sys.argv[1:]]
    runpy.run_path(str(SERVER), run_name="__main__")


def launch(python: pathlib.Path) -> int:
    """Passe la main a l'interpreteur de l'environnement.

    `subprocess` et pas `os.execv` : sous Windows, execv ne met pas les
    arguments entre guillemets, et le chemin du plugin passe par une
    bibliotheque SharePoint dont le nom contient des espaces
    ("Transport BtoC - Documents"). execv coupait le chemin au premier espace.
    subprocess.run cite correctement, et le serveur herite de stdin/stdout/
    stderr - ce dont le protocole MCP a besoin.
    """
    done = subprocess.run([str(python), str(SERVER), *sys.argv[1:]])
    return done.returncode


def main() -> int:
    if not SERVER.is_file():
        log(f"ERREUR : server.py introuvable ({SERVER}). Installation du plugin incomplete.")
        return 2

    # Cas courant apres le premier demarrage : l'interpreteur qui nous lance a
    # deja tout. On enchaine sans sous-processus, donc sans risque de boucle.
    if has_dependencies():
        run_server()
        return 0

    for venv in candidate_venvs():
        python = venv_python(venv)
        if python.is_file() and has_dependencies(python):
            log(f"environnement reutilise : {venv}")
            return launch(python)

    target = candidate_venvs()[-1]
    try:
        python = venv_python(target)
        if not python.is_file():
            python = create_venv(target)
        install_dependencies(python)
    except RuntimeError as exc:
        log(f"ERREUR : {exc}")
        return 1

    if not has_dependencies(python):
        log(
            "ERREUR : les dependances restent introuvables apres installation. "
            f"Verifie a la main : {python} -m pip install -r {REQUIREMENTS}"
        )
        return 1

    log("environnement pret")
    return launch(python)


if __name__ == "__main__":
    sys.exit(main())
