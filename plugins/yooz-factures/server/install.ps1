# Installe le serveur MCP yooz-factures SANS passer par le plugin.
#
# A n'utiliser que pour le mode direct : diagnostic, synchro planifiee, session
# hors Claude Code. Pour l'usage normal, installe le plugin - il prepare son
# environnement tout seul (voir server/bootstrap.py).
#
# Trois choses vont HORS du vault, pour la meme raison : cette bibliotheque est
# synchronisee SharePoint avec toute l'equipe L&T.
#   - l'environnement virtuel -> ~\.yooz-mcp\venv
#   - le fichier de secrets   -> ~\.yooz-mcp\yooz.env
#   - le cache SQLite         -> ~\.yooz-mcp\yooz_cache.sqlite
# Le code, lui, reste ici.
#
# Pas sous %LOCALAPPDATA% : un interpreteur Python empaquete (Microsoft Store)
# virtualise cette zone pour ses processus enfants, et le serveur lance par le
# plugin ne verrait pas le meme cache que celui lance a la main.
#
#   .\install.ps1            # installe, cree le fichier de secrets s'il manque
#   .\install.ps1 -Upgrade   # met a jour les dependances

param([switch]$Upgrade)

$ErrorActionPreference = 'Stop'

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Join-Path $HOME '.yooz-mcp'
$venv = Join-Path $root 'venv'
$py   = Join-Path $venv 'Scripts\python.exe'
$envF = Join-Path $root 'yooz.env'
$srv  = Join-Path $here 'server.py'

if (-not (Test-Path $root)) { New-Item -ItemType Directory -Path $root | Out-Null }

if (-not (Test-Path $py)) {
    Write-Host "Creation de l'environnement virtuel dans $venv"
    python -m venv $venv
}

Write-Host 'Installation des dependances'
& $py -m pip install --quiet --upgrade pip
if ($Upgrade) {
    & $py -m pip install --upgrade -r (Join-Path $here 'requirements.txt')
} else {
    & $py -m pip install -r (Join-Path $here 'requirements.txt')
}

if (-not (Test-Path $envF)) {
    Copy-Item (Join-Path $here '.env.example') $envF
    Write-Host ''
    Write-Host "Fichier de secrets cree : $envF" -ForegroundColor Yellow
    Write-Host 'Il est VIDE : remplis les quatre variables de chaque societe avant la suite.'
} else {
    Write-Host ''
    Write-Host "Fichier de secrets deja present : $envF"
}

Write-Host ''
Write-Host 'Installe.' -ForegroundColor Green
Write-Host ''
Write-Host 'Etape 1 - renseigner les identifiants :'
Write-Host "  notepad `"$envF`""
Write-Host ''
Write-Host 'Etape 2 - verifier la connexion :'
Write-Host "  & `"$py`" `"$srv`" doctor"
Write-Host ''
Write-Host 'Etape 3 - premier rapatriement :'
Write-Host "  & `"$py`" `"$srv`" sync all full"
Write-Host ''
Write-Host 'Le plugin, lui, n a besoin d aucune de ces etapes : il lit sa'
Write-Host 'configuration depuis les champs saisis a son installation.'
