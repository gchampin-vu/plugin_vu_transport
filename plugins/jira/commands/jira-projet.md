---
description: Poser une question en langage naturel a l'instance JIRA metier (vuproject) - chantiers SUPPLY, incidents VUD. Repond avec le JQL utilise, sans jamais inventer un avancement.
---

Tu reponds a une question posee en francais sur l'instance JIRA **metier**,
`vuproject.atlassian.net`, avec les outils du serveur MCP `jira`. Le tenant est
**`PROJET`**, toujours, et jamais `WF` : cette commande ne sort pas de cette
instance.

La question est : **$ARGUMENTS**

Si elle est vide, ne lance rien : demande ce qu'il faut chercher, et donne trois
exemples de ce qui marche - « les chantiers transport ouverts », « ce qui a
bouge cette semaine sur SUPPLY », « les incidents VUD du mois par prestation ».

## Ce que tu fais, dans cet ordre

**1. Situe le sujet avant de requeter.** Si la question porte sur un chantier,
un sigle maison, un code d'agence ou un prefixe de ticket que tu ne connais pas
de facon certaine, appelle d'abord `jira_contexte` avec le mot en argument. Le
contexte embarque sait ce qui ne se devine pas : que `TIL` veut dire *Tracking
Information Log*, que `SUPPLY-3527` est le cadrage d'Atlas, que `VUD7113` est
Marseille, que le titre d'un ticket VUD est une reference d'expedition et pas
une phrase. Une requete posee sans ce detour cherche le bon mot dans le mauvais
projet.

**2. Choisis le bon grain, et c'est la decision qui compte le plus.**

| La question demande | L'outil |
|---|---|
| un compte, une repartition, une evolution | **`jira_summary`** - jamais une liste |
| des tickets a lire, a citer, a suivre | `jira_recherche` |
| un ticket precis, son cadrage, son historique | `jira_issue` |
| ce que porte un epic | `jira_children` |
| depuis quand c'est bloque | `jira_changelog` |
| un JQL que la traduction ne sait pas exprimer | `jira_search` |

Compter en listant coute la fenetre de conversation pour rien, et l'API de
recherche de Jira Cloud **ne rend aucun total** : `jira_summary` parcourt les
pages pour toi et dit s'il a ete plafonne.

**3. Prefere les filtres en francais au JQL ecrit a la main.**
`jira_recherche(tenant="PROJET", projet="SUPPLY", statut="ouvert", maj_depuis="cette semaine")`
est plus sur qu'un JQL improvise : le serveur traduit, affiche ce qu'il a
compris, et **refuse** un filtre qu'il ne comprend pas. Un filtre ignore ne rend
pas moins de lignes, il rend toute la base avec l'air d'avoir compris.

Passe a `jira_search` quand il faut un `OR`, une fonction JQL, un champ
personnalise - apres `jira_fields` pour son nom reel.

**4. Deux reflexes qui evitent une reponse fausse.**

- **Un filtre par personne exige un accountId**, pas un nom : `jira_user_lookup`
  le donne. Pour soi-meme, `assigne="moi"` suffit.
- **Ne devine jamais un libelle de statut.** Filtre sur `statut="ouvert"` /
  `"en cours"` / `"termine"`, qui passent par la categorie de statut - la meme
  partout. Si la question porte vraiment sur un libelle precis, lis-les avec
  `jira_statuses`.

## Comment tu restitues

**Le resultat d'abord.** La reponse a la question, en une phrase ou un tableau
court. La methode ensuite, si elle change la lecture.

**Le JQL utilise, toujours, en fin de reponse.** C'est ce qui rend le chiffre
verifiable et rejouable. Une reponse sans son JQL n'est pas citable.

**Une cle de ticket se cite avec son tenant et son URL.** `SUPPLY-3527` existe
sur `PROJET` ; il n'a aucun rapport avec un ticket de meme numero sur `WF`.

**Si la lecture a ete plafonnee, dis-le avant le chiffre.** Le rendu des outils
le signale explicitement. Un compte issu d'une lecture tronquee est un plancher,
pas un volume - et c'est exactement le chiffre qui finit dans un mail.

**Tu ne recopies aucun avancement dans la bibliotheque d'equipe.** JIRA fait foi
sur les statuts et les dates ; la bibliotheque porte le pourquoi et les
decisions. Si la reponse merite d'etre conservee, propose-le - et attends le feu
vert avant d'ecrire dans le collectif.

## Si ca ne repond pas

Un `ERREUR : ... non configure` veut dire que les identifiants manquent :
enchaine sur `/jira-setup`, ne cherche pas plus loin. Un `HTTP 400` est presque
toujours le JQL, et le message de Jira est precis : lis-le, corrige, ne
recommence pas a l'identique.
