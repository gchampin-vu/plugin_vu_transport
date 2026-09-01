# Installe le serveur MCP jira (installation directe, hors plugin).
#
# Le chemin NOMINAL d'installation est le plugin : /plugin > vu-transport > jira,
# et bootstrap.py prepare tout au premier demarrage - sur Mac comme sur Windows.
# Ce script n'est qu'un confort pour un poste Windows qui veut le serveur en
# direct, ou pour poser les identifiants une fois pour toutes.
#
# Deux choses vont HORS du vault, pour la meme raison : cette bibliotheque est
# synchronisee SharePoint avec toute l'equipe L&T.
#   - l'environnement virtuel  -> %USERPROFILE%\.jira-mcp\venv
#   - le fichier jira.env      -> %USERPROFILE%\.jira-mcp\jira.env
# Le code, lui, reste ici.
#
# Pourquoi le profil utilisateur et pas %LOCALAPPDATA% : un Python empaquete
# (Microsoft Store, Python Manager) donne a ses processus enfants une vue
# VIRTUALISEE de %LOCALAPPDATA%. Le plugin ecrirait d'un cote, un terminal
# lirait de l'autre, sans erreur. Mesure le 2026-08-27 en empaquetant Yooz.
#
#   .\install.ps1                                       # installe, cree le fichier
#   .\install.ps1 -Upgrade                              # met a jour les dependances
#   .\install.ps1 -ProjetEmail 'p.nom@vente-unique.com' -ProjetToken '<jeton>'
#   .\install.ps1 -WfEmail 'p.nom@vente-unique.com' -WfToken '<jeton>'
#
# Les identifiants se passent en argument, jamais dans un message : ce script
# tourne sur le poste, la valeur ne quitte pas la machine.
#
# LE COURRIEL EST OBLIGATOIRE avec le jeton. Jira Cloud attend le couple : un
# jeton seul rend un 401 qui ressemble a un mauvais jeton.
#
# Le jeton se cree a la main sur
#   https://id.atlassian.com/manage-profile/security/api-tokens
# Atlassian n'expose aucune API pour en creer un - ce n'est pas une limite du
# connecteur, c'est un choix d'Atlassian, le jeton portant l'identite.
#
# Ce que ce fichier n'a PAS a porter : les racines de site et les plafonds. Ils
# sont poses une fois pour toute l'equipe dans
# 08_ENGINE/04_mcp/00_config/jira.shared.env, que le serveur lit tout seul.

param(
    [switch]$Upgrade,
    [string]$ProjetEmail,
    [string]$ProjetToken,
    [string]$WfEmail,
    [string]$WfToken
)

$ErrorActionPreference = 'Stop'

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Join-Path $env:USERPROFILE '.jira-mcp'
$venv = Join-Path $root 'venv'
$py   = Join-Path $venv 'Scripts\python.exe'
$envF = Join-Path $root 'jira.env'
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

if (-not (Test-Path $envF)) {
    $pe = if ($ProjetEmail) { $ProjetEmail } else { 'a-remplacer@vente-unique.com' }
    $pt = if ($ProjetToken) { $ProjetToken } else { 'a-remplacer' }
    $we = if ($WfEmail)     { $WfEmail }     else { 'a-remplacer@vente-unique.com' }
    $wt = if ($WfToken)     { $WfToken }     else { 'a-remplacer' }
    $lines = @(
        '# Configuration du serveur MCP jira.',
        '# Fichier LOCAL, hors du vault synchronise : un jeton d API Jira est',
        '# nominatif et autorise l ecriture. Ne le recopie pas dans SharePoint.',
        ('# Cree le ' + (Get-Date -Format 'yyyy-MM-dd') + ' par install.ps1'),
        '',
        '# JIRA_TENANTS=PROJET,WF',
        '',
        '# --- PROJET : vuproject.atlassian.net (le metier) ---',
        ('JIRA_PROJET_EMAIL="' + $pe + '"'),
        ('JIRA_PROJET_TOKEN="' + $pt + '"'),
        '',
        '# --- WF : webfacto.atlassian.net (les developpements) ---',
        ('JIRA_WF_EMAIL="' + $we + '"'),
        ('JIRA_WF_TOKEN="' + $wt + '"'),
        '',
        '# Optionnel - normalement fixe par l equipe dans',
        '#   08_ENGINE/04_mcp/00_config/jira.shared.env',
        '# A ne remplir ici que pour surcharger le reglage d equipe sur ce poste.',
        '# Voir jira.env.example a cote de server.py, qui documente aussi les',
        '# modes bearer et oauth.',
        '# JIRA_PROJET_BASE_URL=',
        '# JIRA_WF_BASE_URL=',
        '# JIRA_PAGE_SIZE=100',
        '# JIRA_MAX_PAGES=50',
        '# JIRA_TIMEOUT_S=60',
        '# JIRA_EXPORT_DIR='
    )
    Set-Content -Path $envF -Value $lines -Encoding utf8
    Write-Host ''
    Write-Host "Fichier de configuration cree : $envF" -ForegroundColor Green
    if (-not $ProjetToken -or -not $WfToken) {
        Write-Host 'Renseigne les identifiants manquants dedans avant de continuer.' -ForegroundColor Yellow
        Write-Host 'Jeton a creer sur https://id.atlassian.com/manage-profile/security/api-tokens' -ForegroundColor Yellow
    }
} else {
    # Idempotent par construction : on retire toutes les lignes portant la
    # variable visee, puis on en ecrit exactement une. La version naive
    # comparait l avant et l apres pour decider d ajouter une ligne - quand le
    # remplacement ne changeait rien, elle en ajoutait une deuxieme, et le
    # fichier finissait avec deux fois la meme variable.
    $changed = $false
    $content = @(Get-Content $envF)
    $pairs = @(
        @{ Name = 'JIRA_PROJET_EMAIL'; Value = $ProjetEmail },
        @{ Name = 'JIRA_PROJET_TOKEN'; Value = $ProjetToken },
        @{ Name = 'JIRA_WF_EMAIL';     Value = $WfEmail },
        @{ Name = 'JIRA_WF_TOKEN';     Value = $WfToken }
    )
    foreach ($pair in $pairs) {
        if ($pair.Value) {
            $motif = '^\s*' + $pair.Name + '\s*='
            $content = @($content | Where-Object { $_ -notmatch $motif })
            $content = $content + ($pair.Name + '="' + $pair.Value + '"')
            $changed = $true
        }
    }
    if ($changed) {
        Set-Content -Path $envF -Value $content -Encoding utf8
        Write-Host "Identifiants mis a jour dans $envF" -ForegroundColor Green
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
Write-Host "  claude mcp add jira --scope user -- `"$py`" `"$srv`""
Write-Host ''
Write-Host "Les identifiants ne passent PAS par la ligne d'enregistrement : le serveur lit son fichier."
