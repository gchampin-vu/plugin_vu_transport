---
updated: 2026-09-02
updated_by: Guillaume_Champin
type: process
---

# Plugin `taux-de-change` — connecteur MCP en lecture seule

Serveur MCP qui donne à Claude Code les **taux de change de chancellerie DGFiP** : le taux
en vigueur à une date, sur 189 devises, la conversion dans les deux sens, l'historique par
devise, et l'export CSV du périmètre exact d'une question.

C'est le portage du script Power Query qui ramenait le jeu de données
`dgfip-taux-de-change` dans Power BI. **Aucune clé d'API, aucune mise en service** : la
source est publique et anonyme. Si le plugin est installé, il répond.

| | |
|---|---|
| Source | `dgfip-taux-de-change` sur [data.economie.gouv.fr](https://data.economie.gouv.fr), API Opendatasoft Explore v2.1 |
| Licence | Licence Ouverte v2.0 (Etalab) |
| Publication | mensuelle |
| Relevé du 2026-09-01 | 24 103 lignes, 189 devises, 262 couples devise/pays, 659 dates de publication |
| Authentification | aucune |

## Ce que ce connecteur apporte que le script Power Query n'avait pas

Le script ramenait la table brute, paginée par `offset` de 10 en 10. Quatre différences,
et les trois premières corrigent des résultats **faux** :

**1. Il résout le taux en vigueur à une date.** Un taux n'est republié que quand il change.
Au 2026-09-01, 44 devises seulement portaient une ligne datée de ce jour, alors que 189
avaient un taux applicable : la livre sterling n'avait pas bougé depuis février, le zloty
depuis avril, la couronne danoise depuis 2020. Un `where date = '2026-09-01'` rend donc
44 devises sur 189, et l'absence de la livre se lit comme « il n'y a pas de taux pour la
livre ». Les outils retiennent, par devise, la dernière publication antérieure ou égale à
la date, et rendent **cette date d'effet** à côté du taux.

**2. Il dédoublonne les pays.** Le jeu porte une ligne par couple (devise, pays) : USD
apparaît plus de dix fois à chaque publication, avec le même taux. D'où 24 103 lignes pour
189 devises. Compter les lignes ne compte pas des taux, et moyenner une colonne moyenne
des doublons.

**3. Il peut lire le jeu en entier.** L'API refuse `offset + limit` au-delà de 10 000 : la
pagination par offset du script Power Query **ne peut pas** atteindre les 24 103 lignes.
La lecture complète passe par `/exports`, qui n'a pas ce plafond.

**4. Il rappelle le sens du taux dans chaque réponse.** `taux` est le nombre d'**euros**
que vaut **une** unité de la devise. Une inversion ne lève aucune erreur : elle rend un
montant plausible, du bon ordre de grandeur, et faux.

## Ce que ça sait faire

| Outil | Ce qu'il rend |
| --- | --- |
| `taux_guide` | les cinq pièges du jeu de données. **Répond hors ligne**, en quelques millisecondes. À lire avant d'écrire un filtre |
| `taux_devises` | le référentiel des 189 devises : code ISO, libellé, taux courant, date d'effet, pays. Filtrable sur un mot |
| `taux_du_jour` | les taux en vigueur aujourd'hui, avec la date d'effet de chacun |
| `taux_a_la_date` | **l'outil central** : les taux en vigueur à une date donnée |
| `taux_historique` | la suite des taux publiés pour une devise, du plus récent au plus ancien, avec min / max sur le périmètre |
| `taux_convertir` | un montant converti, avec le taux utilisé, sa date d'effet et le calcul posé |
| `taux_records` | requête ODSQL brute pour agréger ou filtrer autrement. Les plafonds de l'API sont refusés **avant** l'appel, avec la raison |
| `taux_export_csv` | écrit le périmètre en CSV point-virgule, UTF-8 avec BOM. Deux modes : `en_vigueur` et `brut`. Sans plafond de pagination |
| `taux_doctor` | interpréteur, chemins, **origine de chaque réglage** (poste / équipe / défaut), connexion, état de l'instantané |

Une compétence, `taux-de-change`, sait s'en servir. Une commande, `/taux-export`, conduit
le choix du mode d'export.

## Installation

Le catalogue `vu-transport` s'ajoute une fois, puis :

```
/plugin install taux-de-change@vu-transport
```

**Rien à configurer.** Au premier démarrage, l'amorce crée un environnement virtuel dans
`~/.taux-de-change-mcp/venv` et y installe `mcp` et `httpx` — une fois, en une minute.
Ensuite, `taux_doctor` doit afficher `Connexion API : OK`.

## Les pièges du jeu de données

Ils sont dans `taux_guide`, qui répond hors ligne. Ils sont ici aussi, parce qu'ils
expliquent pourquoi ce connecteur a la forme qu'il a.

| # | Le piège | Ce qu'il produit si on l'ignore |
|---|---|---|
| 1 | un taux n'est republié que quand il change | un filtre d'égalité sur la date rend 44 devises sur 189, sans erreur |
| 2 | une ligne par couple (devise, pays) | compter les lignes surestime de 30 %, moyenner une colonne moyenne des doublons |
| 3 | `taux` = euros par unité de devise | une conversion inversée, plausible et fausse |
| 4 | `monnaievigueur` dit « en vigueur **aujourd'hui** » | sur une date passée, le filtre par défaut écarte des devises qui avaient cours alors |
| 5 | « historique complet depuis 1990 » | 83 lignes seulement sont antérieures à 1999, sur neuf devises obscures. Le dollar n'entre qu'au 2005-06-01 |

Sur le piège 5, la mesure : le nombre de devises ayant un taux applicable passe de **12**
au 2000-06-30 à **162** au 2005-06-30, puis 189 aujourd'hui. L'historique réellement
exploitable commence à l'été 2005 — la constante `PREMIERE_DATE_UTILE` du serveur, et les
outils le signalent quand la date demandée est antérieure.

## Ce que ce jeu de données n'est pas

**Ce sont des taux de chancellerie : une référence administrative mensuelle.** Pas un
cours de marché, pas un taux de banque, pas le taux de conversion d'un règlement.

Sur une question de facturation transporteur, ce connecteur donne la référence publique.
**Il ne dit pas quel taux s'applique au contrat** : le taux contractuel, sa date de
référence et sa règle d'arrondi se lisent dans `02_TRANSPORTEURS/<NOM>/01_contrat/` et
`02_tarif/`. Les deux se disent ensemble, ou le chiffre part faux dans un mail.

Le jeu ne porte pas non plus les monnaies pré-euro : ni franc français, ni mark.

## Lecture seule, et pourquoi ce n'est pas une convention

L'API Explore n'expose que des GET, et ce serveur n'émet que des GET. Il n'y a **aucune
opération d'écriture à exposer** sur un jeu de données public : la règle de lecture seule
de [`../../README.md`](../../README.md) est ici une propriété de la source, pas une
retenue de notre part.

L'export CSV écrit un fichier **local** — ce n'est pas une écriture vers un système tiers.

## Configuration

**Il n'y en a pas besoin.** Trois choses seulement sont réglables, et aucune n'est
obligatoire :

| Réglage | Défaut | Quand y toucher |
|---|---|---|
| `TAUX_BASE_URL` | `https://data.economie.gouv.fr` | un miroir interne, si le portail devenait injoignable du réseau CAFOM |
| `TAUX_DATASET` | `dgfip-taux-de-change` | si la DGFiP republie le jeu sous un autre identifiant |
| `TAUX_EXPORT_DIR` | `~/.taux-de-change-mcp/exports` | pour poser les CSV ailleurs. **Jamais dans la bibliothèque SharePoint** |

Ordre de priorité : configuration du plugin (`/plugin`) > `~/.taux-de-change-mcp/.env` >
`08_ENGINE/04_mcp/00_config/taux.shared.env` > défaut du serveur. Le poste passe donc
devant l'équipe. `taux_doctor` affiche, pour chaque réglage, sa valeur **et son origine**.

**Aucun fichier n'a été déposé dans `00_config/`, et c'est volontaire.** Un fichier
d'équipe n'a de raison d'être que s'il porte une décision commune : une clé de service, une
racine interne, un périmètre métier. Ce connecteur n'en a aucune. Déposer un fichier ne
portant que des commentaires ferait croire qu'il y a un réglage à tenir — c'est la raison
qui a fait archiver `INDEX_DOCUMENTS.md`. Le modèle est versionné à côté du serveur, dans
`server/taux.shared.env.example`, pour le jour où une décision apparaîtrait.

## Le repli hors ligne

Après chaque lecture réussie, le jeu entier est déposé dans
`~/.taux-de-change-mcp/instantane.json`. Si l'API ne répond pas — portail en panne, poste
hors réseau, proxy — les outils répondent depuis cet instantané **en le disant** : l'entête
porte `REPLI LOCAL` et la date de récupération.

Un taux de chancellerie du mois dernier reste exploitable *si on dit qu'il date*. C'est
tout l'objet de cette date. Et s'il n'y a ni réseau ni instantané, le connecteur répond
qu'il n'a pas de réponse — **il ne rend jamais un taux de mémoire**.

## Compatibilité Mac / Windows

Les conventions de [`../../../04_mcp/README.md`](../../../04_mcp/README.md) sont appliquées
à la lettre : `pathlib` partout, aucun chemin absolu en dur, racine locale sous le profil
utilisateur (`~/.taux-de-change-mcp`) et **pas** sous `%LOCALAPPDATA%`, `sys.executable`
seul interpréteur nommé, `subprocess.run` avec une liste d'arguments, `encoding="utf-8"` et
`newline=""` explicites, rien sur `stdout`.

Le code source est en **ASCII pur** : la casse et les accents d'un nom de fichier comptent
sous macOS et pas sous Windows.

## Contrôles

```
python server/test_offline.py
```

**65 contrôles, aucun appel réseau.** L'instantané est remplacé par un jeu d'essai qui
reproduit chaque piège : une devise dormante depuis des mois, une devise dupliquée sur deux
pays, deux taux qui divergent à la 10e décimale sur la même date (le franc CFA), une devise
sans cours, une devise dont la première publication est postérieure à la date demandée.
`_get` est remplacé par une fonction qui lève : si un contrôle tentait un appel réseau, il
échouerait au lieu de passer.

Le fichier d'équipe est neutralisé par `TAUX_SHARED_ENV` pointant un chemin inexistant. La
variable est **exclusive**, donc la suite tourne bien comme sur un poste sans configuration
d'équipe — et pas, en silence, avec celle du mainteneur.

État au 2026-09-02 : **65 contrôles hors ligne au vert**, et confronté à la vraie source le
même jour — 24 103 lignes lues, 189 devises, `doctor` au vert, les deux modes d'export
vérifiés (BOM unique, fins de ligne LF, séparateur point-virgule).

## Ce qui reste à faire

- **Rien de bloquant.** Le connecteur est utilisable en l'état.
- **Croiser avec le taux contractuel.** La question réelle de l'équipe n'est pas « quel
  est le taux » mais « quel taux s'applique à cette facture ». Y répondre demande le taux
  de référence du contrat, qui vit dans `02_TRANSPORTEURS/<NOM>/01_contrat/` et n'est pas
  structuré. À reprendre quand un cas concret se présentera, plutôt qu'à deviner
  maintenant.
