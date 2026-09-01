---
description: Exporter en CSV le perimetre exact d'une question posee en francais sur l'instance JIRA metier (vuproject). Le fichier atterrit en local, jamais dans la bibliotheque d'equipe.
---

Tu produis un **export CSV** de tickets de l'instance metier
`vuproject.atlassian.net`. Le tenant est **`PROJET`**, toujours.

Le perimetre demande est : **$ARGUMENTS**

Si c'est vide, demande ce qu'il faut exporter. N'exporte jamais « tout JIRA » :
un export sans perimetre est un fichier que personne ne relira.

## La regle qui gouverne cette commande

**L'export prend le MEME perimetre que la question, pas les lignes affichees.**
Tu traduis la demande en filtres, tu les passes a `jira_export_csv`, et il
pagine la collection entiere - pas les cent premieres lignes qu'une reponse en
conversation aurait montrees. C'est toute la difference entre un export et une
copie d'ecran.

## Ce que tu fais

1. **Si le sujet a un vocabulaire maison, passe par `jira_contexte`** avant de
   traduire. Exporter les incidents « Habitat » suppose de savoir que le prefixe
   est `HF` dans le titre ; exporter « le chantier tracking » suppose de savoir
   que c'est `TIL`.

2. **Appelle `jira_export_csv` avec `tenant="PROJET"`** et les filtres en
   francais - les memes que `jira_recherche` : `projet`, `titre`, `texte`,
   `statut`, `type_ticket`, `epic`, `cree_depuis`, `cree_jusqua`, `maj_depuis`,
   `etiquette`. Pour un perimetre que la traduction ne sait pas exprimer, passe
   `jql`.

3. **Regle `max_rows` sur le volume attendu.** Le defaut est 20 000 lignes. Sur
   `VUD`, qui porte des milliers de tickets par mois, verifie l'ordre de
   grandeur avec `jira_summary` **avant** d'exporter : un export tronque coute
   un aller-retour, et surtout il ment s'il n'est pas relu.

4. **Ajoute les colonnes utiles, pas toutes.** `champs` accepte des champs Jira
   supplementaires (`customfield_...`, trouves avec `jira_fields`). La
   description n'est pas exportee par defaut : elle est en ADF et elle rend le
   fichier illisible dans Excel.

## Ce que tu dis en rendant la main

Quatre lignes, pas plus :

1. **le chemin complet du fichier**, tel que l'outil le rend ;
2. **le nombre de lignes et de colonnes** ;
3. **le JQL utilise** - c'est le perimetre, en une ligne verifiable ;
4. **si l'export a ete tronque**, en premier et en clair. L'outil le dit ; ne
   l'enterre pas en fin de reponse. Un export tronque sans le dire, c'est un
   chiffre faux qui part dans un mail.

## Deux choses a ne pas faire

- **Ne mets jamais l'export dans la bibliotheque d'equipe.** Il atterrit dans le
  dossier local (`~/.jira-mcp/exports` par defaut). Un CSV depose dans un
  dossier synchronise part chez les vingt-six personnes de l'equipe, et la base
  de connaissance ne porte pas de donnees.
- **N'exporte pas de ta propre initiative.** Cette commande est une demande
  explicite ; en dehors d'elle, reponds dans la conversation. Un perimetre trop
  gros pour l'affichage n'est pas une raison d'ecrire un fichier - c'est une
  raison d'agreger avec `jira_summary`.
