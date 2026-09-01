---
description: Exporter des tickets JIRA en CSV, en choisissant l'instance. La commande generique - /jira-projet-export et /jira-wf-export font la meme chose sur une instance fixee.
---

Tu produis un **export CSV** de tickets JIRA. C'est la commande generique du
connecteur : elle demande d'abord **de quelle instance** il s'agit.

La demande est : **$ARGUMENTS**

## Etape 1 - l'instance, avant tout le reste

Il y a **deux instances sans aucun lien** entre elles, et un export lance sur la
mauvaise rend un fichier vide sans erreur :

| Instance | Ce qu'elle porte | Commande dediee |
|---|---|---|
| **PROJET** (`vuproject`) | le metier : chantiers `SUPPLY`, incidents `VUD` | `/jira-projet-export` |
| **WF** (`webfacto`) | les developpements de la WebFacto | `/jira-wf-export` |

Si `$ARGUMENTS` nomme l'instance - « les tickets webfacto de la semaine », « les
epics SUPPLY » - prends-la et continue. **Si c'est ambigu, demande**, en une
question fermee : une cle de projet inconnue de l'instance visee est une reponse
fausse silencieuse.

Si la demande porte sur les deux, fais **deux exports** et nomme les fichiers en
consequence : ne melange pas deux instances dans un meme CSV, les cles y
seraient indiscernables.

## Etape 2 - exporte

Enchaine sur la commande dediee, ou fais directement :

1. `jira_contexte` si le sujet a un vocabulaire maison (cote `PROJET`) ;
   `jira_projects` si l'instance est `WF`, dont le referentiel n'est pas releve.
2. `jira_summary` pour l'ordre de grandeur, avant de paginer.
3. `jira_export_csv(tenant=..., ...)` avec les filtres en francais, ou `jql`.

## Etape 3 - rends compte en quatre lignes

Le chemin du fichier, le nombre de lignes et de colonnes, le JQL utilise, et -
en premier s'il y a lieu - **si l'export a ete tronque**.

L'export atterrit dans le dossier local (`~/.jira-mcp/exports` par defaut),
**jamais dans la bibliotheque d'equipe** : un CSV depose dans un dossier
synchronise part chez les vingt-six personnes de l'equipe.
