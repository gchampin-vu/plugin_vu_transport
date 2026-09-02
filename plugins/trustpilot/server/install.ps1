# Installe le serveur MCP trustpilot sur un poste Windows.
#
# CE N'EST PAS LE CHEMIN NOMINAL. Le point d'entree suppose du connecteur est
# bootstrap.py, qui tourne sur Mac comme sur Windows et cree l'environnement
# tout seul au premier demarrage. Ce script n'est qu'un confort pour installer
# le connecteur A LA MAIN, hors plugin - il n'existe pas d'equivalent macOS, et
# il n'en faut pas.
#
# Deux choses vont HORS du vault, pour la meme raison : cette bibliotheque est
# synchronisee SharePoint avec toute l'equipe L&T.
#   - l'environnement virtuel -> %USERPROFILE%\.trustpilot-mcp\venv
#   - le fichier .env         -> %USERPROFILE%\.trustpilot-mcp\.env
# Le code, lui, reste ici.
#
# Le profil utilisateur et PAS %LOCALAPPDATA% : c'est la convention de
# construction de l'equipe (08_ENGINE/04_mcp/README.md). Un Python empaquete
# (Microsoft Store, Python Manager) donne a ses processus enfants une vue
# VIRTUALISEE de %LOCALAPPDATA% - le script ecrit d'un cote, le serveur lit de
# l'autre, sans aucune erreur.
#
#   .\install.ps1                                     # installe, cree le .env s'il manque
#   .\install.ps1 -Upgrade                            # met a jour les dependances
#   .\install.ps1 -ApiKey '<cle>' -ApiSecret '<secret>'
#   .\install.ps1 -Doctor                             # installe puis diagnostique

param(
    [switch]$Upgrade,
    [switch]$Doctor,
    [string]$ApiKey,
    [string]$ApiSecret
)

$ErrorActionPreference = 'Stop'

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Join-Path $env:USERPROFILE '.trustpilot-mcp'
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
    $key    = if ($ApiKey)    { $ApiKey }    else { 'a-remplacer' }
    $secret = if ($ApiSecret) { $ApiSecret } else { '' }
    $lines = @(
        '# Configuration du serveur MCP trustpilot.',
        '# Fichier local, hors du vault synchronise. Ne le recopie pas dans SharePoint.',
        ('# Cree le ' + (Get-Date -Format 'yyyy-MM-dd') + ' par install.ps1'),
        '',
        ('TRUSTPILOT_API_KEY="' + $key + '"')
    )
    if ($secret) {
        $lines += ('TRUSTPILOT_API_SECRET="' + $secret + '"')
    } else {
        $lines += '# Sans le secret, le connecteur reste sur le chemin PUBLIC :'
        $lines += '# pas de filtre de date cote serveur, et pas de referenceId'
        $lines += '# donc pas de lien avis / commande / transporteur.'
        $lines += '# TRUSTPILOT_API_SECRET='
    }
    $lines += ''
    $lines += '# Optionnel - voir .env.example a cote de server.py'
    $lines += '# TRUSTPILOT_EXPORT_DIR='
    $lines += '# TRUSTPILOT_MAX_PAGES=60'
    $lines | Out-File -FilePath $envF -Encoding utf8
    Write-Host "Fichier de configuration cree : $envF"
    if (-not $ApiKey) {
        Write-Host "  -> ouvre-le et remplace 'a-remplacer' par ta cle d'API Trustpilot."
    }
} else {
    Write-Host "Fichier de configuration deja present : $envF (inchange)"
    if ($ApiKey) {
        Write-Host '  -> -ApiKey ignore : le fichier existe deja. Modifie-le a la main,'
        Write-Host '     ou appelle trustpilot_save_key depuis une session Claude Code.'
    }
}

# Droits restreints a l'utilisateur courant : le fichier porte un secret.
if (Test-Path $envF) {
    icacls $envF /inheritance:r /grant:r ("$env:USERNAME" + ':(R,W)') | Out-Null
    Write-Host "Droits NTFS restreints a $env:USERNAME"
}

Write-Host ''
Write-Host 'Installation terminee.'
Write-Host "  serveur : $srv"
Write-Host "  python  : $py"

if ($Doctor) {
    Write-Host ''
    & $py $srv doctor
}
