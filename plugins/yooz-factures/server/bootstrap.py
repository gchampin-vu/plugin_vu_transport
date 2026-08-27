#!/usr/bin/env python3
"""
Amorce du serveur MCP yooz-factures, lancee par le plugin.

Le plugin ne peut pas supposer que `mcp` et `httpx` sont installes sur le poste
du collegue : un plugin s'installe en une commande, il n'ouvre pas un terminal
pour faire un `pip install`. Cette amorce s'en charge, une fois, au premier
demarrage.

    python bootstrap.py            # prepare l'environnement puis lance le serveur
    python bootstrap.py doctor     # idem, puis diagnostic
    python bootstrap.py sync all full

REGLE ABSOLUE : rien sur la sortie standard. Le serveur MCP parle JSON-RPC sur
stdout, et une seule ligne de `pip` egaree casse le protocole en silence. Toute
sortie de `venv` et de `pip` est donc redirigee sur stderr, ou Claude Code la
lit comme un journal.

Meme structure que l'amorce du connecteur shiptify : les deux plugins de l'equipe
se depannent de la meme facon.
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

# Ce dont le serveur a besoin pour demarrer.
NEEDED = ("mcp", "httpx")


def log(message: str) -> None:
    """Journal sur stderr. Jamais stdout."""
    print(f"[yooz-mcp] {message}", file=sys.stderr, flush=True)


def has_dependencies(python: pathlib.Path | None = None) -> bool:
    """Les dependances sont-elles importables ?"""
    if python is None:
        try:
            import httpx  # noqa: F401
            import mcp  # noqa: F401
        except ImportError:
            return False
        return True
    try:
        done = subprocess.run(
            [str(python), "-c", "import mcp, httpx"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def venv_python(venv: pathlib.Path) -> pathlib.Path:
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def candidate_venvs() -> list[pathlib.Path]:
    """Emplacements de venv, dans l'ordre de preference.

    L'ancienne racine %LOCALAPPDATA%\\yooz-mcp est encore essayee : sur le poste
    ou le connecteur a d'abord ete installe a la main, on reutilise les 40 Mo de
    dependances deja la plutot que de les reinstaller a cote. Le serveur, lui,
    ecrit desormais son cache sous ~/.yooz-mcp (voir la note sur les
    interpreteurs empaquetes dans le README).
    """
    out: list[pathlib.Path] = []
    override = (os.environ.get("YOOZ_MCP_VENV") or "").strip()
    if override and not override.startswith("${"):
        out.append(pathlib.Path(override))
    out.append(pathlib.Path.home() / ".yooz-mcp" / "venv")
    legacy = os.environ.get("LOCALAPPDATA")
    if legacy:
        out.append(pathlib.Path(legacy) / "yooz-mcp" / "venv")
    plugin_data = (os.environ.get("CLAUDE_PLUGIN_DATA") or "").strip()
    if plugin_data and not plugin_data.startswith("${"):
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
    log("installation des dependances (mcp, httpx) - une seule fois")
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
    if sys.version_info < (3, 10):
        log(
            f"ERREUR : Python 3.10 minimum, trouve {sys.version.split()[0]} "
            f"({sys.executable})."
        )
        return 2

    # Cas courant apres le premier demarrage : l'interpreteur qui nous lance a
    # deja tout. On enchaine sans sous-processus, et sans re-exec - donc sans
    # risque de boucle.
    if has_dependencies():
        run_server()
        return 0

    # Un venv utilisable existe deja ?
    for venv in candidate_venvs():
        python = venv_python(venv)
        if python.is_file() and has_dependencies(python):
            log(f"environnement reutilise : {venv}")
            return launch(python)

    # Sinon : creer, installer, passer la main.
    target = candidate_venvs()[0]
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
