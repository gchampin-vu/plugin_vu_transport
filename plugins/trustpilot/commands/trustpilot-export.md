---
description: Exporte un perimetre Trustpilot en CSV, ouvrable dans Excel. Decris le perimetre en francais, la commande le traduit en filtres, pagine tous les domaines concernes et dit ce qui manque.
---

Tu produis un **export CSV Trustpilot** a partir du perimetre decrit dans
`$ARGUMENTS`.

## Ce que cette commande garantit, et que la conversation ne garantit pas

Un rendu en session est **borne** : il s'arrete au budget d'affichage et ne
montre qu'une partie des avis. L'export, lui, **pagine chaque domaine du
perimetre jusqu'a sa derniere page**. C'est la difference entre regarder et
livrer.

C'est aussi la seule sortie acceptable pour preparer une revue transporteur, un
point qualite de service ou une piece a joindre a un mail : sans elle, la
personne recopie a la main ce que le connecteur a affiche, et c'est exactement
la que les chiffres se deforment.

## Comment tu conduis

**1. Traduis le perimetre, ne le devine pas.**

Reformule d'abord en une phrase ce que tu as compris, puis resous ce qui doit
l'etre :

- un pays, une enseigne, « les avis allemands » -> le connecteur les resout
  lui-meme (`domaine='allemagne'`), mais **verifie ce qu'il a retenu** dans
  l'entete qu'il rend. « Allemagne » est `kauf-unique.de`, pas
  `kauf-unique.at` : deux domaines, meme langue.
- « Habitat » -> `habitat.fr` pour la France, `habitat-design.com` pour
  l'international, qui sert **six locales** sous un seul domaine. Demande
  lequel si c'est ambigu.
- un nom de theme ou de transporteur -> `trustpilot_lexique`, jamais un mot
  invente. Le filtre `theme` et le filtre `transporteur` sont **lexicaux** :
  ils ne voient que ce qui est ecrit avec les mots du lexique, donc ils
  sous-comptent, et l'export doit le dire.

**Si le perimetre n'a aucune borne de date, demande-la** avant d'exporter. Sans
date, le connecteur applique quatre-vingt-dix jours par defaut - ce qui n'est
presque jamais ce qu'on voulait pour un fichier. L'historique demarre au
`TRUSTPILOT_START_DATE` de la configuration d'equipe (2023-01-01).

**2. Choisis le bon chemin d'export.**

| Situation | Outil |
|---|---|
| Une periode courte, un ou deux domaines | **`trustpilot_export_csv`** — il appelle l'API en direct |
| Douze mois, ou les dix-huit domaines, ou un croisement | **`trustpilot_sync`** une fois, puis **`trustpilot_export_sql`** — le cache n'a pas le plafond de pagination |

Le plafond du direct est `TRUSTPILOT_MAX_PAGES`, soit 6 000 avis **par
domaine**. Sur `vente-unique.com`, ca se depasse en quelques mois. Si l'export
revient tronque, ne le livre pas tel quel : passe par le cache, ou resserre.

**3. Ne fais pas sortir une adresse client sans qu'on te l'ait demandee.**

Le parametre `inclure_donnees_personnelles` est **faux par defaut**, et le
`referralEmail` — la boite mail du client invite — est retire du fichier. Ne le
passe a vrai que si la personne en a explicitement besoin, et dis-lui alors, en
une phrase, que le fichier ne doit aller ni dans la bibliotheque d'equipe, ni en
piece jointe a un transporteur.

**4. Rends compte, et rends compte de ce qui manque.**

Une fois le fichier ecrit, donne dans cet ordre :

1. **si l'export est incomplet, dis-le en premier, pas en dernier.** Un export
   tronque dont on ne dit rien est un chiffre faux qui part dans un mail. Le
   connecteur liste les domaines qui n'ont pas ete lus jusqu'au bout : recopie
   cette liste.
2. le chemin exact du fichier ;
3. le nombre de lignes et de colonnes ;
4. la periode et les filtres appliques, recopies depuis l'entete rendu.

Precise aussi les colonnes dont le taux de remplissage change la lecture :

- **`referenceId` n'existe que sur les avis issus d'une invitation**, et
  seulement par le chemin prive. C'est la colonne qui relie l'avis a la
  commande : si elle est vide, c'est cette chaine qui est coupee, pas la
  donnee qui manque.
- `themes` et `transporteurs_cites` sont calcules par **detection de mots**.
  Une case vide veut dire « aucun mot du lexique », pas « rien a signaler ».
- `experiencedAt` est la date vecue, `createdAt` la date de publication.
  L'export est filtre sur `createdAt`.

## Ce que tu ne fais pas

- **Tu n'exportes pas pour te rassurer sur un chiffre.** Si la question est
  « combien » ou « quelle note », la reponse est `trustpilot_summary`. Si elle
  est « de quoi se plaignent-ils », c'est `trustpilot_themes` puis
  `trustpilot_verbatims`. Un fichier ne repond a aucune des deux.
- **Tu n'ecris jamais dans la bibliotheque d'equipe.** L'export va dans le
  dossier local du connecteur. Un CSV de verbatims clients depose dans un
  dossier synchronise part chez les vingt-six, et la base de connaissance ne
  porte pas de donnees.
- **Tu ne traduis pas et tu ne reformules pas les verbatims** dans le fichier :
  c'est une extraction, pas une analyse. Le texte sort dans la langue du client.
