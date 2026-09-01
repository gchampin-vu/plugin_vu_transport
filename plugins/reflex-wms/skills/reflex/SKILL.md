---
name: reflex
description: >
  Interroger le WMS Reflex de Vente-unique en lecture seule - preparations et ordres de preparation, receptions previsionnelles et reelles, avis d'expedition, expeditions et chargements, stock, mouvements, emplacements, articles, quais.
  Active des qu'une question porte sur Reflex ou sur l'entrepot : une preparation, un ODP, une commande qui ne part pas, un conteneur en retard, un chargement, un touliv, un quai, un stock bloque, un mouvement de GEI, un article en entrepot, un volume ou un poids expedie depuis AMB ou AUV.
  Declenche aussi sur "reflex", "WMS", "HLPRENP" ou tout nom de table Reflex, "MLD", "quelle table", "requete SQL sur l'entrepot", "prepa en cours", "reception en retard", "conteneur non decharge", "chargement valide", "export des preparations".
---

# Reflex WMS — interroger l'entrepôt

Le connecteur `reflex` répond à une question en français par une requête T-SQL
juste. Ce qui rend ça possible : **le MLD Hardis des 1 985 tables est embarqué
dans le connecteur**. Les outils de schéma répondent hors ligne, en quelques
millisecondes, sans rien demander à la production.

## La séquence, dans cet ordre

1. **`reflex_guide()`** — une fois par session, avant la première requête. Il
   porte les deux pièges qui produisent des résultats faux **sans lever
   d'erreur** : les dates éclatées en cinq colonnes, et les tops qui valent
   `'1'`/`'0'` chez Vente-unique alors que la doc de l'éditeur annonce
   `'O'`/`'N'`.
2. **Trouver les tables** — `reflex_tables('réception')`,
   `reflex_prefix('PE')`, `reflex_find_column('conteneur')`.
3. **Lire les colonnes exactes** — `reflex_columns('HLPRENP', 'sol')`.
   **Un nom de colonne ne s'invente jamais.** C'est la discipline qui sépare
   une requête qui tourne d'une requête qui tourne juste.
4. **Partir d'une recette si elle existe** — `reflex_recipes()`. Elles portent
   les définitions métier sur lesquelles l'équipe s'est mise d'accord.
5. **Exécuter** — `reflex_query(sql)`.

Pour se repérer sans écrire de SQL : `reflex_peek('HLPRENP', depot='AMB')`
montre à quoi ressemblent vraiment les valeurs.

## Les règles d'écriture, non négociables

- Préfixe `reflex.` sur chaque table. Jamais `dbo.`.
- Nom physique court : `HLPRENP`, jamais `HL_PREPA_ENTETE` (qui n'existe pas
  en table).
- **`WITH (NOLOCK)` sur chaque table lue.** Le connecteur **refuse** une
  requête qui l'oublie — pas par formalisme : une requête écrite ici finit
  recopiée dans Power BI ou SSMS, où elle posera des verrous sur la base de
  production de l'entrepôt.
- Filtrer **d'abord sur le dépôt** : `AMB` (Amblainville) ou `AUV`
  (Montbeugny/Moulins).
- Une seule instruction par appel. Pas de `DECLARE` : les paramètres se posent
  en littéraux dans le `WHERE`.

## Ne pas bloquer la production

Le connecteur borne ce qui sort : gouverneur de coût (SQL Server **refuse de
démarrer** un plan trop cher), délai d'exécution, une requête à la fois,
plafond de lignes. Mais un garde-fou qui se déclenche est du temps perdu :

- Borner la période sur les colonnes de date **brutes** (`PEACRE >= 26`),
  jamais sur `RFX_DHB_DATE2DATETIME(...)` — un filtre posé sur le résultat de
  la fonction interdit l'usage des index.
- Joindre sur la **clé complète** : activité, dépôt, millésime, numéro. Joindre
  sur le seul numéro marche sur un jeu d'essai et explose en production.
- **Compter avant de lister.** Un `COUNT(*)` dit en une seconde si le périmètre
  tient.

## Les deux bases — épuration d'abord

`RFXCAFPRDDAT` est la base courante, `RFXCAFPRDEPU` la base d'**épuration** :
même schéma, mêmes tables, l'historique que Reflex a sorti de la courante.

**Par défaut, une lecture interroge l'épuration puis la courante** et s'arrête
à la première qui rend des lignes. C'est voulu : les questions portent presque
toujours sur quelque chose qui a déjà eu lieu, Reflex épure en continu, et
personne ne sait à quel moment un dossier bascule.

**Dis toujours quelle base a répondu** — le connecteur te le donne. Un chiffre
sans cette mention n'est pas citable : les deux bases ne couvrent pas la même
période. Force une seule base avec `base='prod'` ou `base='epu'`.

Trois réflexes :

- **un référentiel se lit sur `base='prod'`, toujours.** Dépôts, articles,
  états, transporteurs : l'épuration en porte une photo ancienne. Mesuré sur
  `HLDEPPP` — l'épuration rend `001`, `AMB` et `MOR` (Moreuil, site fermé) et
  **pas `AUV`**. Elle répond, donc la cascade s'y arrête, et la liste est
  fausse sans erreur. La cascade sert à retrouver **l'historique d'un
  dossier**, pas à lire une table de codes ;
- sur une extraction lourde dont tu sais qu'elle porte sur du récent, pose
  `base='prod'` — sinon la requête tourne deux fois ;
- **un `COUNT(*)` sans `GROUP BY` ne bascule jamais** : il rend toujours une
  ligne, même à zéro, donc la cascade s'arrête sur le compte de l'épuration.
  Pour compter sur les deux, pose la question sur chaque base.

## Restituer

- **Le résultat d'abord**, la méthode ensuite. Puis les filtres appliqués et
  ce qui reste incertain.
- **Un chiffre se cite avec son périmètre** : dépôt, période, filtres. « 412
  préparations non soldées sur AMB au 28/08 » — jamais « 412 préparations ».
- **Un résultat tronqué n'est pas un total.** Le connecteur le signale ; ne le
  cite jamais comme un volume, demande un `COUNT(*)`.
- **N'exporte pas de ta propre initiative.** `reflex_export_csv` ne sert que si
  l'utilisateur a demandé un export, un fichier, ou de quoi ouvrir dans Excel.
  Sinon, la réponse va dans la conversation.

## Lecture seule, et ce que ça veut dire

Ce connecteur ne peut pas écrire dans Reflex, et ce n'est pas un réglage :
il n'y a aucun outil d'écriture, l'analyseur refuse tout ce qui n'est pas un
`SELECT`, et la session tourne en `READ UNCOMMITTED`. Débloquer un stock,
solder une préparation, corriger un emplacement passe par Reflex, par
l'entrepôt, ou par la Webfacto.

## Un écart connu, non arbitré

`reflex_guide('ecarts')` documente trois définitions de la compétence d'équipe
`reflex-mld` que le MLD Hardis contredit — dont `PESLEF`, qui est la date de
**livraison effectuée** et non de « lancement effectif ». **Si un chiffre issu
d'ici ne recoupe pas un chiffre Power BI existant, regarde là en premier.**
L'arbitrage appartient à Jimmy Mieuzet, porteur de la compétence.
