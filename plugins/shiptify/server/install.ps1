# Installe le serveur MCP shiptify.
#
# Deux choses vont HORS du vault, pour la meme raison : cette bibliotheque est
# synchronisee SharePoint avec toute l'equipe L&T.
#   - l'environnement virtuel  -> %LOCALAPPDATA%\shiptify-mcp\venv
#   - le fichier .env (la cle) -> %LOCALAPPDATA%\shiptify-mcp\.env
# Le code, lui, reste ici.
#
#   .\install.ps1                    # installe, cree le .env s'il manque
#   .\install.ps1 -Upgrade           # met a jour les dependances
#   .\install.ps1 -ApiKey '<cle>'    # installe et renseigne la cle

param(
    [switch]$Upgrade,
    [string]$ApiKey
)

$ErrorActionPreference = 'Stop'

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Join-Path $env:LOCALAPPDATA 'shiptify-mcp'
$venv = Join-Path $root 'venv'
$py   = Join-Path $venv 'Scripts\python.exe'
$envF = Join-Path $root '.env'
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

# --- Le fichier .env, hors du vault -------------------------------------

if (-not (Test-Path $envF)) {
    $key = if ($ApiKey) { $ApiKey } else { 'a-remplacer' }
    $lines = @(
        '# Configuration du serveur MCP shiptify.',
        '# Fichier local, hors du vault synchronise. Ne le recopie pas dans SharePoint.',
        ('# Cree le ' + (Get-Date -Format 'yyyy-MM-dd') + ' par install.ps1'),
        '',
        ('SHIPTIFY_API_KEY="' + $key + '"'),
        '',
        '# Optionnel - voir .env.example a cote de server.py',
        '# SHIPTIFY_BASE_URL=https://api.shiptify.com',
        '# SHIPTIFY_AUTH_PREFIX=Api-Key',
        '# SHIPTIFY_ACCOUNT_ID=',
        '# SHIPTIFY_MAX_PAGES=60',
        '# SHIPTIFY_TIMEOUT_S=60',
        '# SHIPTIFY_EXPORT_DIR='
    )
    Set-Content -Path $envF -Value $lines -Encoding utf8
    Write-Host ''
    Write-Host "Fichier de configuration cree : $envF" -ForegroundColor Green
    if (-not $ApiKey) {
        Write-Host 'Renseigne SHIPTIFY_API_KEY dedans avant de continuer.' -ForegroundColor Yellow
    }
} elseif ($ApiKey) {
    # Idempotent par construction : on retire toutes les lignes de cle
    # existantes, puis on en ecrit exactement une. La version precedente
    # comparait l'avant et l'apres pour decider d'ajouter une ligne - quand le
    # remplacement ne changeait rien (cle deja a jour), elle en ajoutait une
    # deuxieme. Le fichier finissait avec deux SHIPTIFY_API_KEY.
    $kept = @(Get-Content $envF | Where-Object { $_ -notmatch '^\s*SHIPTIFY_API_KEY\s*=' })
    $patched = $kept + ('SHIPTIFY_API_KEY="' + $ApiKey + '"')
    Set-Content -Path $envF -Value $patched -Encoding utf8
    Write-Host "Cle mise a jour dans $envF" -ForegroundColor Green
} else {
    Write-Host "Configuration deja presente : $envF"
}

# Restreint le fichier au seul utilisateur courant.
try {
    icacls $envF /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null
    Write-Host "Droits restreints a $env:USERNAME sur le fichier .env"
} catch {
    Write-Host "Droits NTFS non modifies (non bloquant) : $_" -ForegroundColor Yellow
}

Write-Host ''
Write-Host 'Installe.' -ForegroundColor Green
Write-Host ''
Write-Host 'Etape 1 - verification :'
Write-Host "  & '$py' '$srv' doctor"
Write-Host ''
Write-Host 'Etape 2 - enregistrement du serveur dans Claude Code :'
Write-Host "  claude mcp add shiptify --scope user -- `"$py`" `"$srv`""
Write-Host ''
Write-Host "La cle ne passe PAS par la ligne d'enregistrement : le serveur lit son .env."
