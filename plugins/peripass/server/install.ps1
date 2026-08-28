# Installe le serveur MCP peripass.
#
# Deux choses vont HORS du vault, pour la meme raison : cette bibliotheque est
# synchronisee SharePoint avec toute l'equipe L&T.
#   - l'environnement virtuel        -> %USERPROFILE%\.peripass-mcp\venv
#   - le fichier peripass.env (cles) -> %USERPROFILE%\.peripass-mcp\peripass.env
# Le code, lui, reste ici.
#
# Pourquoi le profil utilisateur et pas %LOCALAPPDATA% : un Python empaquete
# (Microsoft Store, Python Manager) donne a ses processus enfants une vue
# VIRTUALISEE de %LOCALAPPDATA%. Le plugin ecrirait d'un cote, un terminal
# lirait de l'autre, sans erreur. Mesure le 2026-08-27 en empaquetant Yooz.
#
#   .\install.ps1                                  # installe, cree le fichier
#   .\install.ps1 -Upgrade                         # met a jour les dependances
#   .\install.ps1 -AuvApiKey '<cle>' -AmbApiKey '<cle>'
#
# Les cles se passent en argument, jamais dans un message : ce script tourne
# sur le poste, la valeur ne quitte pas la machine.
#
# Ce que ce fichier n'a PAS a porter : les sites, leurs racines d'API, leurs
# tenants, les plafonds. Ils sont poses une fois pour toute l'equipe dans
# 08_ENGINE/04_mcp/00_config/peripass.shared.env, que le serveur va lire tout
# seul. Les lignes correspondantes ci-dessous restent commentees : elles ne
# servent qu'a surcharger le reglage d'equipe sur ce poste (tenant de recette).
# Si les cles sont, elles aussi, portees par le fichier d'equipe, ce script n'a
# plus qu'a installer les dependances - lance-le sans -AuvApiKey ni -AmbApiKey.

param(
    [switch]$Upgrade,
    [string]$AuvApiKey,
    [string]$AmbApiKey
)

$ErrorActionPreference = 'Stop'

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Join-Path $env:USERPROFILE '.peripass-mcp'
$venv = Join-Path $root 'venv'
$py   = Join-Path $venv 'Scripts\python.exe'
$envF = Join-Path $root 'peripass.env'
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

# --- Le fichier de configuration, hors du vault --------------------------
#
# Encoding utf8 explicite, et le fichier est ecrit sans BOM par le bloc de
# reecriture ci-dessous : le serveur lit en utf-8-sig, mais un autre outil qui
# lirait le fichier ne le ferait pas forcement.

if (-not (Test-Path $envF)) {
    $auv = if ($AuvApiKey) { $AuvApiKey } else { 'a-remplacer' }
    $amb = if ($AmbApiKey) { $AmbApiKey } else { 'a-remplacer' }
    $lines = @(
        '# Configuration du serveur MCP peripass.',
        '# Fichier local, hors du vault synchronise. Ne le recopie pas dans SharePoint.',
        ('# Cree le ' + (Get-Date -Format 'yyyy-MM-dd') + ' par install.ps1'),
        '',
        '# PERIPASS_SITES=AUV,AMB',
        '',
        ('PERIPASS_AUV_API_KEY="' + $auv + '"'),
        ('PERIPASS_AMB_API_KEY="' + $amb + '"'),
        '',
        '# Optionnel - normalement fixe par l equipe dans',
        '#   08_ENGINE/04_mcp/00_config/peripass.shared.env',
        '# A ne remplir ici que pour surcharger le reglage d equipe sur ce poste.',
        '# Voir peripass.env.example a cote de server.py',
        '# PERIPASS_AUV_BASE_URL=',
        '# PERIPASS_AMB_BASE_URL=',
        '# PERIPASS_AUV_TENANT=',
        '# PERIPASS_AMB_TENANT=',
        '# PERIPASS_PAGE_SIZE=100',
        '# PERIPASS_MAX_PAGES=60',
        '# PERIPASS_TIMEOUT_S=60',
        '# PERIPASS_EXPORT_DIR='
    )
    Set-Content -Path $envF -Value $lines -Encoding utf8
    Write-Host ''
    Write-Host "Fichier de configuration cree : $envF" -ForegroundColor Green
    if (-not $AuvApiKey -or -not $AmbApiKey) {
        Write-Host 'Renseigne les cles manquantes dedans avant de continuer.' -ForegroundColor Yellow
    }
} else {
    # Idempotent par construction : on retire toutes les lignes portant la cle
    # visee, puis on en ecrit exactement une. La version naive comparait
    # l'avant et l'apres pour decider d'ajouter une ligne - quand le
    # remplacement ne changeait rien, elle en ajoutait une deuxieme, et le
    # fichier finissait avec deux fois la meme variable.
    $changed = $false
    $content = @(Get-Content $envF)
    if ($AuvApiKey) {
        $content = @($content | Where-Object { $_ -notmatch '^\s*PERIPASS_AUV_API_KEY\s*=' })
        $content = $content + ('PERIPASS_AUV_API_KEY="' + $AuvApiKey + '"')
        $changed = $true
    }
    if ($AmbApiKey) {
        $content = @($content | Where-Object { $_ -notmatch '^\s*PERIPASS_AMB_API_KEY\s*=' })
        $content = $content + ('PERIPASS_AMB_API_KEY="' + $AmbApiKey + '"')
        $changed = $true
    }
    if ($changed) {
        Set-Content -Path $envF -Value $content -Encoding utf8
        Write-Host "Cles mises a jour dans $envF" -ForegroundColor Green
    } else {
        Write-Host "Configuration deja presente : $envF"
    }
}

# Restreint le fichier au seul utilisateur courant.
try {
    icacls $envF /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null
    Write-Host "Droits restreints a $env:USERNAME sur le fichier de configuration"
} catch {
    Write-Host "Droits NTFS non modifies (non bloquant) : $_" -ForegroundColor Yellow
}

Write-Host ''
Write-Host 'Installe.' -ForegroundColor Green
Write-Host ''
Write-Host 'Etape 1 - controles hors reseau :'
Write-Host "  & '$py' '$(Join-Path $here 'test_offline.py')'"
Write-Host ''
Write-Host 'Etape 2 - verification de la connexion :'
Write-Host "  & '$py' '$srv' doctor"
Write-Host ''
Write-Host 'Etape 3 - enregistrement du serveur dans Claude Code :'
Write-Host "  claude mcp add peripass --scope user -- `"$py`" `"$srv`""
Write-Host ''
Write-Host "Les cles ne passent PAS par la ligne d'enregistrement : le serveur lit son fichier."
