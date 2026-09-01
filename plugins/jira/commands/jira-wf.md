---
description: Poser une question en langage naturel a l'instance JIRA de la WebFacto (webfacto) - les developpements. Repond avec le JQL utilise, et commence par decouvrir le referentiel, qui n'est pas encore releve.
---

Tu reponds a une question posee en francais sur l'instance JIRA de la
**WebFacto**, `webfacto.atlassian.net`, avec les outils du serveur MCP `jira`.
Le tenant est **`WF`**, toujours, et jamais `PROJET` : cette commande ne sort pas
de cette instance.

La question est : **$ARGUMENTS**

Si elle est vide, ne lance rien : demande ce qu'il faut chercher, et propose de
commencer par `jira_projects` - voir juste en dessous pourquoi c'est
particulierement vrai ici.

## Ce qui est different de `/jira-projet`, et qu'il faut savoir avant

**Le referentiel de cette instance n'est PAS releve.** Le contexte embarque du
connecteur decrit les 12 projets de `vuproject` ; pour `webfacto`, il ne porte
que le nom du site. La raison est simple et elle est ecrite dans le contexte :
personne n'avait d'acces API a cette instance au moment de la construction du
connecteur.

Consequences pratiques, dans l'ordre :

1. **Ne suppose aucune cle de projet.** `SUPPLY` n'existe probablement pas ici,
   et un `project = SUPPLY` rendrait une erreur ou zero ligne selon la
   configuration. Commence par **`jira_projects(tenant="WF")`**.
2. **Ne suppose aucun libelle de statut ni aucun type de ticket.** Une equipe de
   developpement a souvent des workflows a elle - `jira_statuses` et
   `jira_referentiel` les donnent.
3. **Les epics `WF - TICKET SPOT <MOIS>` ne sont PAS ici.** Ce sont des epics du
   projet `SUPPLY`, sur l'instance `PROJET` : le prefixe `WF` designe le
   destinataire de la demande, pas l'instance qui la porte. C'est le faux ami le
   plus previsible entre les deux commandes - si la question parle de « ticket
   spot », elle releve de `/jira-projet`.
4. **Le compte et le jeton sont propres a cette instance.** Un jeton d'API est
   lie a un compte, et le compte doit avoir ete invite sur `webfacto` : un 401
   ici alors que `/jira-projet` fonctionne n'est pas une faute de saisie, c'est
   un probleme d'acces. `jira_myself(tenant="WF")` dit avec quel compte on parle.

**Quand tu decouvres quelque chose de structurant** - les cles de projet, la
convention de titre, le decoupage des workflows - dis-le, et propose de le
verser dans `01_CONTEXTE/JIRA_ET_CHANTIERS.md` de la bibliotheque d'equipe. Ce
fichier a une case a cocher qui attend exactement ca. N'ecris pas dans le
collectif sans feu vert.

## Ce que tu fais, dans cet ordre

1. **Decouvre si besoin** : `jira_projects(tenant="WF")`, puis `jira_statuses` ou
   `jira_fields` si la question porte sur un statut ou un champ maison.
2. **Choisis le grain** : `jira_summary` pour compter et repartir,
   `jira_recherche` pour lister, `jira_issue` pour lire un ticket,
   `jira_children` pour ce que porte un epic, `jira_search` pour un JQL que la
   traduction ne sait pas exprimer.
3. **Prefere les filtres en francais** (`statut="en cours"`,
   `maj_depuis="cette semaine"`, `assigne="moi"`) : le serveur traduit, montre
   ce qu'il a compris, et refuse ce qu'il ne comprend pas plutot que de
   l'ignorer.
4. **Un filtre par personne exige un accountId** : `jira_user_lookup(tenant="WF", ...)`.
   L'annuaire de cette instance est distinct de l'autre - une personne connue
   sur `PROJET` peut n'exister pas ici.

## Comment tu restitues

**Le resultat d'abord**, la methode ensuite. **Le JQL utilise en fin de
reponse**, toujours : c'est ce qui rend le chiffre verifiable.

**Chaque cle de ticket se cite avec son instance et son URL.** Entre deux
instances, une cle seule est ambigue.

**Si la lecture a ete plafonnee, dis-le avant le chiffre** : le rendu des outils
le signale, un compte tronque est un plancher, pas un volume.
