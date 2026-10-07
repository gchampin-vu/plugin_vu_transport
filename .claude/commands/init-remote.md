---
description: Prepare une session distante (mobile, claude.ai/code) pour travailler sur le cerveau collectif 0_IA BRAIN VU et le vault individuel myrddin, lus via le connecteur Google Drive.
argument-hint: "[sujet du jour, facultatif]"
updated: 2026-10-07
updated_by: Guillaume_Champin
---

Tu ouvres une session **distante** : un conteneur cloud, pas le poste de
Guillaume. Le Drive partage n'y est pas monte, `~/.claude/cerveau` n'existe pas,
les commandes du collectif et les 13 connecteurs de `.mcp.json` ne se chargent
pas tout seuls. Cette commande recompose le contexte que le poste charge
d'office, par le connecteur Google Drive.

Sujet du jour, s'il est donne : `$ARGUMENTS`.

## Regle de conduite

Tout se lit **a distance**, par identifiant de fichier. Tu ne recopies aucun
fichier du Drive dans ce depot. Tu ne lis jamais un fichier `*.shared.env` ni
un `.env` : un secret ne transite pas par la conversation ni par un appel
d'outil. Si un identifiant ci-dessous rend une erreur, retrouve le fichier par
`search_files` sur son titre et son dossier parent, puis signale a Guillaume
l'identifiant a corriger dans cette commande.

## Etape 1 - Verifier les outils

Charge par ToolSearch `mcp__Google_Drive__search_files`,
`mcp__Google_Drive__read_file_content` et `mcp__Google_Drive__get_file_metadata`.
S'ils n'existent pas, arrete-toi : le connecteur Google Drive n'est pas actif
sur le compte claude.ai, rien ne peut se faire sans lui.

## Etape 2 - Lire le socle, dans cet ordre

Lis ces fichiers avec `read_file_content`. Ce sont eux qui font foi, pas ce
resume : applique ce qu'ils disent pour toute la session.

| Ordre | Fichier | Identifiant |
|---|---|---|
| 1 | Collectif `CLAUDE.md` (regles, ou est quoi) | `1kXzWzcWXQpvwI7lfgLGgO00DVJtJoKG3` |
| 2 | Vault `AGENTS.md` (qui est Myrddin, regles du vault) | `1W7T8JhJXiONQb6LTFD3f0e8QWp3YAkdH` |
| 3 | Vault `_System/Contexte_Global.md` | `1GQNGEojy2bpua1-erHpjfpwX3WMeA5o8` |
| 4 | Vault `_System/MEMORY.md` | `1BxX6L06TKGodUWX1ItpvP1Tt1qleIGvG` |

A la demande seulement, pas au demarrage :

| Fichier | Identifiant |
|---|---|
| Collectif `_System/Conventions_Collectif.md` | `1K6qmaA8gJxlZRHgqtsoeCeSVDzE48qhZ` |
| Vault `_System/Conventions_Frontmatter.md` | `1uimYmbCC067alfH5t0aKagZbopZiGkeK` |
| Vault `_System/Signature_Gmail.md` (tout mail) | `1M6NKEXhC8653hkem0JSW-v-kJPla9C5s` |

## Etape 3 - La carte des dossiers

Pour lister un dossier : `search_files` avec `parentId = '<id>'` et
`excludeContentSnippets: true`, en suivant `nextPageToken` jusqu'au bout.

**Collectif `0_IA BRAIN VU`** (Drive partage `0AOLI7stsQZKyUk9PVA`)

| Dossier | Identifiant |
|---|---|
| `01_CONTEXTE` | `1TxEb8GB4hBzbjr2MSYzFomI4Ll18iEcz` |
| `02_TRANSPORTEURS` | `1SPBClJQrfrdLFeQhkXF7UjNa5lypA7Zk` |
| `03_SYSTEMES` | `1oYl6Y970tsHmAkg_JhVBeOExJKaBSWjV` |
| `04_FACTURATION` | `1InwLWQhBkVmLh0o8R-FXJ01zXbORQ2P-` |
| `05_PROCESS_TRANSVERSAUX` | `1V2pNSZ5pdmLbLeWABK3SbWIEg9TsFn9s` |
| `06_PROJETS` | `1uGo9VfIJyiidMUmEMF-0EKV6QUrUTXk_` |
| `08_ENGINE` | `1UM_ddYDi8hsTjQL0XwOYXNWR-TB6V--p` |
| `09_ENTREPOTS` | `1z-A-sYiz6OKvGaGCzFkEJ59X9mz9AA8u` |
| `10_SERVICE_CLIENT` | `1PdGxmf9fpkqdxMii99AizjzXYQsRMtN2` |
| `11_DATA_ET_MODELES` | `1MvAWOux2JrFi0fxjmJeDPuWDF93I1yHD` |
| `12_RESOURCES` | `1C0ofiVF2lJDIz99sw5dYPZiSy-PVetuG` |
| `_System` | `1UZI09v2zK-1c3iIGaw8L8WDzjqC2YiHz` |
| `.claude/commands` | `1lS4sez2oF_IqAb8Spw24NF5A4m0kwudU` |

**Vault `myrddin`** (`18yLmFqO_0Wmi3wBeCVYdu_d1d-uHOpyP`)

| Dossier | Identifiant |
|---|---|
| `00_IMPORT` | `1_OCsCqnAHRFIadyx4QuxfPdFF9GmLFr_` |
| `01_COCKPIT` | `1pUoKHNlmK3kskO3BncCV9WelNwdUEzte` |
| `02_PROJETS` | `1Xy-A3TDm2OppKolgEYiic9DgaPfK3yOr` |
| `03_OPERATIONS` | `1aKxDRrpKVblhH_vFPSmPtxgP7Phq8137` |
| `04_FACTURATION` | `1MQqWxo46DYPh6DSu8lsNOMMxNOwBLN9Q` |
| `05_EQUIPE` | `1_ldzvq__WtoZkm46e9Oxv99thcO0naUS` |
| `06_COMMUNICATION` | `1LtKLNrFosCMZAqky8uM1843LdBx2-wPG` |
| `_System` | `1i1nk-ydNunBVgQjejy-rGMx3E1v1LB1a` |
| `.claude` (commandes propres) | `1_z6WJoHSfMsZVThRGNjAzM9_K6-PWEE6` |
| `_SENSIBLE` | `1ZPAUAZcXJmlphMibi3xp1znhbzMLdaxX` |

`_SENSIBLE` : regles de `AGENTS.md` sans exception. Tu n'y vas que si la
question l'exige, tu le dis quand une reponse s'en sert, rien n'en ressort.

## Etape 4 - Les commandes du vault et du collectif

Elles ne sont pas chargees en natif. Quand Guillaume tape une commande qui
n'existe pas ici (`/triage`, `/briefing`, `/ask`, `/cost`...) :

1. cherche `<nom>.md` dans `.claude/commands` du vault, puis du collectif ;
2. lis-la et deroule-la a la main ;
3. si elle depend du disque local (NAS, `~/.claude/cerveau`, cache SQLite) ou
   d'un connecteur absent, dis lequel avant de commencer, et propose ce qui
   reste faisable.

Ne les recense pas au demarrage : c'est a la demande.

## Etape 5 - Les connecteurs metier

Les serveurs MCP de l'equipe sont dans ce depot, sous
`plugins/<connecteur>/server/`. Ils ne tournent pas comme serveurs ici, mais
leurs outils s'appellent comme des fonctions Python.

Pour chaque connecteur, verifie sans jamais afficher de valeur :

```bash
[ -n "$SHIPTIFY_API_KEY" ] && echo "cle presente" || echo "cle absente"
curl -sS -o /dev/null -w "%{http_code}\n" --max-time 10 https://api.shiptify.com/
```

- cle absente : le secret se pose dans les reglages de l'environnement cloud
  (Network secrets ou variable d'environnement), nouvelle session ensuite. Tu ne
  demandes jamais la cle dans le chat.
- code `000` ou refus du proxy : le domaine n'est pas autorise dans l'acces
  reseau de l'environnement.
- `401` sans cle, ou cle presente : le reseau passe.

Appel d'un outil, une fois les dependances installees
(`pip install -q -r plugins/shiptify/server/requirements.txt`) :

```bash
cd plugins/shiptify/server && python -c "import server; print(server.shiptify_doctor())"
```

Commence toujours par `shiptify_doctor` ; ne conclus jamais qu'un connecteur
marche sur un code autre que 200. Les exports vont dans le scratchpad de la
session, jamais dans le Drive partage.

## Etape 6 - Rendre compte, en cinq lignes au plus

1. socle lu (les quatre fichiers, avec leur date `updated`) ;
2. ce que dit `MEMORY.md` des prochaines etapes, en deux ou trois points ;
3. connecteurs metier disponibles dans cette session, et ce qui manque ;
4. les limites de la session distante : pas d'ecriture dans le collectif sans
   feu vert, pas de commande native, pas de NAS ;
5. si `$ARGUMENTS` est renseigne : par ou tu attaques ce sujet.

Puis attends la demande. Pas de recapitulatif des regles : elles sont lues,
elles s'appliquent.
