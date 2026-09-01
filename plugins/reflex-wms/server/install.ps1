# Installe le serveur MCP reflex-wms.
#
# Ce que ce script installe, et ce qu'il n'installe pas :
#   - un environnement virtuel Python avec `mcp` et `pyodbc`  -> oui,
#     dans %LOCALAPPDATA%\reflex-mcp\venv, hors du vault synchronise ;
#   - un pilote ODBC                                          -> NON.
#     Le connecteur utilise "SQL Server", livre avec Windows. C'est un choix :
#     rien a installer en dehors du plugin.
#
# Le code, lui, reste dans le vault.
#
#   .\install.ps1                 # installe, verifie le pilote ODBC
#   .\install.ps1 -Upgrade        # met a jour les dependances
#   .\install.ps1 -SqlLogin       # cree en plus un reflex.env pour un compte SQL
#
# Dans le cas normal il n'y a AUCUN secret a poser : le connecteur
# s'authentifie avec la session Windows.

param(
    [switch]$Upgrade,
    [switch]$SqlLogin
)

$ErrorActionPreference = 'Stop'

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Join-Path $env:LOCALAPPDATA 'reflex-mcp'
$venv = Join-Path $root 'venv'
$py   = Join-Path $venv 'Scripts\python.exe'
$envF = Join-Path $root 'reflex.env'
$srv  = Join-Path $here 'server.py'

if (-not (Test-Path $root)) { New-Item -ItemType Directory -Path $root | Out-Null }

if (-not (Test-Path $py)) {
    Write-Host "Creation de l'environnement virtuel dans $venv"
    python -m venv $venv
}

Write-Host 'Installation des dependances (mcp, pyodbc)'
& $py -m pip install --quiet --upgrade pip
if ($Upgrade) {
    & $py -m pip install --upgrade -r (Join-Path $here 'requirements.txt')
} else {
    & $py -m pip install -r (Join-Path $here 'requirements.txt')
}

# --- Le pilote ODBC : on verifie, on n'installe pas ----------------------

Write-Host ''
Write-Host 'Pilotes ODBC SQL Server presents sur ce poste :'
$drivers = @(Get-OdbcDriver | Select-Object -ExpandProperty Name | Where-Object { $_ -match 'SQL Server' } | Sort-Object -Unique)
if ($drivers.Count -eq 0) {
    Write-Host "  AUCUN. C'est inattendu : le pilote 'SQL Server' est livre avec Windows." -ForegroundColor Red
} else {
    foreach ($d in $drivers) { Write-Host "  $d" }
    Write-Host "Le connecteur prendra le plus recent, et retombera sur 'SQL Server' - aucune installation requise." -ForegroundColor Green
}

# --- Joignabilite du serveur --------------------------------------------

Write-Host ''
Write-Host 'Test de joignabilite du serveur Reflex (172.17.151.114:1433)'
$reachable = Test-NetConnection -ComputerName '172.17.151.114' -Port 1433 -WarningAction SilentlyContinue
if ($reachable.TcpTestSucceeded) {
    Write-Host '  Joignable.' -ForegroundColor Green
} else {
    Write-Host "  Injoignable depuis ce poste. Reflex est sur le reseau interne : il faut etre au bureau ou sur le VPN." -ForegroundColor Yellow
    Write-Host "  Ce n'est pas bloquant pour l'installation - les outils de schema du connecteur repondent hors ligne."
}

# --- Le fichier reflex.env, hors du vault, seulement si compte SQL -------

if ($SqlLogin) {
    if (-not (Test-Path $envF)) {
        $lines = @(
            '# Configuration du serveur MCP reflex-wms.',
            '# Fichier local, hors du vault synchronise. Ne le recopie pas dans SharePoint.',
            ('# Cree le ' + (Get-Date -Format 'yyyy-MM-dd') + ' par install.ps1'),
            '',
            'REFLEX_AUTH=sql',
            'REFLEX_USER=a-remplacer',
            'REFLEX_PASSWORD="a-remplacer"',
            '',
            '# Optionnel - voir reflex.env.example a cote de server.py'
        )
        Set-Content -Path $envF -Value $lines -Encoding utf8
        Write-Host ''
        Write-Host "Fichier de configuration cree : $envF" -ForegroundColor Green
        Write-Host 'Renseigne REFLEX_USER et REFLEX_PASSWORD dedans avant de continuer.' -ForegroundColor Yellow
    } else {
        Write-Host ''
        Write-Host "Configuration deja presente : $envF"
    }
    try {
        icacls $envF /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null
        Write-Host "Droits restreints a $env:USERNAME sur le fichier reflex.env"
    } catch {
        Write-Host "Droits NTFS non modifies (non bloquant) : $_" -ForegroundColor Yellow
    }
} else {
    Write-Host ''
    Write-Host "Mode d'authentification : session Windows. Aucun secret a poser." -ForegroundColor Green
    Write-Host "Pour un compte SQL dedie fourni par l'IT : relance avec -SqlLogin."
}

Write-Host ''
Write-Host 'Installe.' -ForegroundColor Green
Write-Host ''
Write-Host 'Etape 1 - verification :'
Write-Host "  & '$py' '$srv' doctor"
Write-Host ''
Write-Host 'Etape 2 - enregistrement du serveur dans Claude Code :'
Write-Host "  claude mcp add reflex --scope user -- `"$py`" `"$srv`""
Write-Host ''
Write-Host "En mode plugin, rien de tout ca n'est necessaire : /plugin installe et configure."
