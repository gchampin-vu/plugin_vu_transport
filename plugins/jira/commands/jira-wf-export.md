---
description: Exporter en CSV le perimetre exact d'une question posee en francais sur l'instance JIRA de la WebFacto (webfacto). Le fichier atterrit en local, jamais dans la bibliotheque d'equipe.
---

Tu produis un **export CSV** de tickets de l'instance de la WebFacto,
`webfacto.atlassian.net`. Le tenant est **`WF`**, toujours.

Le perimetre demande est : **$ARGUMENTS**

Si c'est vide, demande ce qu'il faut exporter.

## Avant d'exporter, decouvre - ici plus qu'ailleurs

Le referentiel de cette instance **n'est pas releve** dans le contexte embarque :
aucune cle de projet, aucun libelle de statut, aucun type de ticket n'y est
connu. Un export lance sur une cle de projet supposee rend un fichier vide sans
erreur, ce qui est la pire des sorties.

Donc, dans cet ordre :

1. **`jira_projects(tenant="WF")`** - les cles reelles.
2. **`jira_summary(tenant="WF", ...)`** - l'ordre de grandeur, pour regler
   `max_rows` avant de paginer des milliers de tickets.
3. **`jira_export_csv(tenant="WF", ...)`** - avec les filtres en francais
   (`projet`, `statut`, `cree_depuis`, `maj_depuis`, `texte`, `titre`, `epic`) ou
   un `jql` si la traduction ne suffit pas.

**Rappel du faux ami :** les epics `WF - TICKET SPOT <MOIS>` sont dans le projet
`SUPPLY` de l'instance `PROJET`. Si c'est ca qu'on veut exporter, c'est
`/jira-projet-export`, pas cette commande.

## Ce que tu dis en rendant la main

1. **le chemin complet du fichier** ;
2. **le nombre de lignes et de colonnes** ;
3. **le JQL utilise** ;
4. **si l'export a ete tronque** - en premier et en clair.

## Deux choses a ne pas faire

- **Jamais dans la bibliotheque d'equipe.** L'export va dans le dossier local
  (`~/.jira-mcp/exports` par defaut) : un CSV depose dans un dossier synchronise
  part chez toute l'equipe.
- **Pas d'export de ta propre initiative.** En dehors de cette commande, reponds
  dans la conversation.
