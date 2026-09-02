---
updated: 2026-09-02
updated_by: Guillaume_Champin
---

# gisco — le referentiel geographique europeen, en lecture seule

GISCO est le service geographique d'**Eurostat**. Il publie librement, sans
compte ni cle, le decoupage administratif et statistique europeen : pays,
regions NUTS, communes, villes, codes postaux. Ce connecteur le rend
interrogeable en francais depuis une session Claude, et **hors ligne** une fois
les couches rapatriees.

Il repond a la famille de questions qui revient des qu'on touche a un zonage de
livraison : *ce code postal, c'est quelle commune, quelle region, urbain ou
rural ? ces coordonnees, elles tombent ou ? combien de communes dans ce
departement ? quelle est l'aire d'attraction reelle de Lyon ?*

## Ce qu'il y a dedans

| Couche | Contenu | Lignes | Poids |
|---|---|---|---|
| `nuts` | regions NUTS, niveaux 0 a 3 | 1 798 | 1,5 Mo |
| `countries` | pays du monde, statut UE / AELE | 263 | 1 Mo |
| `lau` | communes (Local Administrative Units) | 97 987 | 75 Mo |
| `urau` | villes et zones urbaines fonctionnelles | 1 332 | 26 Mo |
| `pcode` | codes postaux europeens, 34 pays | 830 032 | 200 Mo |
| `uk_lad` | districts britanniques (ONS) | 361 | leger |
| `uk_itl3` | equivalents NUTS3 britanniques (ONS) | 182 | leger |
| `uk_onspd` | codes postaux britanniques actifs (ONS) | 1 809 679 | long |

Chiffres releves le 2026-09-01, aux millesimes par defaut. `gisco_couches()` dit
ce qui est reellement rapatrie sur un poste donne.

**Chaque code postal porte sa commune, son NUTS3, sa zone urbaine fonctionnelle
et son degre d'urbanisation.** C'est de loin la couche la plus utile pour un
zonage de transport : elle distingue une livraison urbaine d'une livraison
rurale sans avoir a le deviner du code postal.

## Mise en service

Aucune cle, aucun `.env`. Il faut seulement **rapatrier les couches**, une fois.

```
/gisco-cache
```

La commande annonce le poids et la duree avant de telecharger quoi que ce soit.
`nuts` et `countries` se rapatrient toutes seules a la premiere question : elles
sont sous le seuil de rapatriement automatique.

En ligne de commande, pour verifier l'installation :

```bash
python server/bootstrap.py doctor
```

Et les controles hors ligne, qui ne demandent ni reseau ni cache :

```bash
python server/test_offline.py
```

## Les outils

20 outils, tous en lecture. Les noms parlent d'eux-memes ; la skill `gisco`
sait lequel choisir.

| Famille | Outils |
|---|---|
| Diagnostic et catalogue | `gisco_doctor`, `gisco_catalogue`, `gisco_couches`, `gisco_tables` |
| Rapatriement et mise a jour | `gisco_sync`, `gisco_maj` |
| Referentiels | `gisco_pays`, `gisco_nuts`, `gisco_lau`, `gisco_villes`, `gisco_codes_postaux` |
| Resolution | `gisco_resoudre`, `gisco_localiser`, `gisco_geocoder`, `gisco_distance` |
| Agregation et geometrie | `gisco_repartition`, `gisco_geometrie`, `gisco_sql` |
| Export | `gisco_export_csv`, `gisco_export_sql` |

Deux commandes : `/gisco-cache` pour la mise en service, `/gisco-export` pour
sortir un perimetre en CSV.

## Les millesimes ne sont pas ecrits dans le code

GISCO ouvre de nouveaux millesimes et renomme ses fichiers sans prevenir. Une
liste d'annees en dur vieillit, et le jour ou elle vieillit le connecteur refuse
un millesime qui existe pourtant — en ayant l'air d'accuser Eurostat.

Le connecteur **decouvre** donc ce qui est publie, en deux appels :
`datasets.json` pour les editions d'un jeu, puis l'index de l'edition pour la
liste des GeoPackage. Le nom du fichier a rapatrier est ensuite **confronte a
cette liste** au lieu d'etre seulement fabrique depuis un gabarit : c'est ce qui
permet de suivre un renommage sans toucher au code, et de dire ce qui existe
reellement quand la demande ne correspond a rien. Le releve est garde une
semaine dans le cache (`GISCO_CATALOGUE_TTL_JOURS`).

Quand Eurostat ne repond pas, le repli est explicite et progressif : le dernier
releve local, **meme perime**, en disant son age ; a defaut seulement, la liste
`annees_connues` de `catalogue.json`, en disant qu'elle est embarquee. On ne
renonce jamais en silence.

`gisco_couches()` compare chaque couche du cache au dernier millesime publie et
signale celles qui sont en retard. `gisco_maj()` les remet a niveau — **en
simulation par defaut**, parce qu'une mise a jour de `pcode` represente 200 Mo
et que personne ne veut la declencher en posant une question.

Le versant britannique echappe a cette decouverte, et ce n'est pas un oubli : le
millesime de l'ONS vit dans le **nom du service** (`LAD_MAY_2025_UK_BGC_V2`) et
dans le nom des champs — `LAD25CD` deviendra `LAD26CD`. Il se met a jour a la
main, dans `catalogue.json`.

## Les pieges, releves et verifies

Ils sont dans `server/catalogue.json`, embarques avec le connecteur, et la skill
les rappelle. Les quatre qui produisent une reponse fausse **sans lever
d'erreur** :

**`PT` ne veut pas dire Portugal.** Dans un nom de fichier GISCO, les deux
lettres qui suivent le nom du jeu designent le **type de geometrie** :
`RG` = region (polygone), `BN` = limite, `LB` = point d'etiquette, `PT` =
point. `PCODE_PT_2025_4326` est donc l'ensemble des codes postaux **europeens**
en geometrie ponctuelle — 830 032 codes sur 34 pays — et surtout pas les codes
postaux du Portugal. La confusion fait croire que GISCO ne publie qu'un pays et
pousse a chercher ailleurs ce qui est deja la.

**La population n'existe pas pour la France ni l'Espagne au millesime LAU
2024.** Les 34 946 communes francaises et les 8 132 communes espagnoles y sont a
zero. Au millesime 2023, la France n'a que 6 communes sans population et
l'Espagne une seule. Un zero n'est pas une population, c'est une absence de
mesure. `gisco_lau` previent tout seul quand tout le perimetre demande est a
zero, et rappelle la commande a lancer.

**`EL` et `UK`, pas `GR` et `GB`.** GISCO suit la nomenclature Eurostat. Un
rapprochement avec un referentiel maison en GR ou GB rate ces deux pays en
silence. Le connecteur accepte les deux graphies en entree ; le fichier d'en
face, lui, ne les accepte pas.

**Un code postal n'a qu'une commune de rattachement.** `60110` sort avec Meru,
alors qu'il couvre aussi Amblainville. C'est la commune de rattachement, pas
« la » commune du code postal.

Deux autres, moins piegeurs mais a connaitre : les colonnes `CODE` et
`NUTS3_<annee>` du jeu `pcode` arrivent entourees de **guillemets litteraux**
(`"NL366"` et non `NL366`) — le connecteur les retire au chargement, mais une
jointure faite ailleurs sur la valeur brute rend zero ligne sans erreur ; et
`gisco_distance` rend une **orthodromie**, pas un kilometrage routier.

## Pourquoi le GeoPackage, et pas le GeoJSON

GISCO publie chaque couche en six formats. Le GeoJSON est le plus simple a lire
et le plus lourd de tous : 150 Mo pour les communes, 490 Mo pour les codes
postaux. Le GeoPackage porte les memes donnees en deux fois moins de place, et
c'est **une base SQLite** — donc le `sqlite3` de la bibliotheque standard
l'ouvre, sans GDAL, sans geopandas, sans roue compilee a installer sur le poste
d'un collegue. Les seules dependances du connecteur sont `mcp` et `httpx`.

Le prix a payer est que la geometrie y est en WKB : `server/geo.py` la lit,
trois cents lignes ecrites une fois, sans dependance. C'est aussi ce qui permet
le test d'appartenance d'un point a un contour — `gisco_localiser` — entierement
hors ligne, avec un pre-filtrage par cadre englobant qui elimine 99,9 % des
candidats avant de decoder quoi que ce soit.

## Le versant britannique

Le Royaume-Uni est sorti du perimetre NUTS et LAU apres le Brexit. L'equivalent
vient de l'**ONS**, par son ArcGIS public, et se charge dans les **memes tables**
avec `source='ONS'` — de sorte qu'une question ne change pas de forme selon le
pays. Chaque reponse affiche la colonne `source`, parce qu'un district
britannique n'est pas une commune francaise et qu'un decompte qui les additionne
sans le dire n'est pas comparable.

Les noms de couches ONS portent leur mois de publication
(`LAD_MAY_2025_UK_BGC_V2`) : ils changent a chaque millesime, et c'est dans
`server/catalogue.json` qu'on les met a jour, pas dans le code.

## De Power Query a ce connecteur

Ce connecteur reprend et generalise quatre requetes Power Query montees pour
Power BI (LAU, NUTS3, URAU, PCODE + ONSPD). La correspondance :

| La requete M faisait | Ici |
|---|---|
| `Web.Contents` sur le GeoJSON, expansion des `properties` | `gisco_sync`, une fois, en GeoPackage |
| L'union GISCO + ONS a la main, colonne par colonne | integree : memes tables, colonne `source` |
| `Table.ReplaceValue` pour retirer les guillemets de `NUTS3_2024` | fait au chargement |
| La pagination ArcGIS par `List.Generate` | `_arcgis_pages`, qui avance de l'effectif reellement rendu |
| `Table.Distinct` sur `LAD25CD` avant la jointure | fait : sans lui, la jointure multiplie les lignes |
| `GeometryJson` en colonne texte | `gisco_export_csv(avec_geometrie=True)` |

Ce qui change vraiment : les requetes M **retelechargent** a chaque
rafraichissement, ici on rapatrie une fois ; et elles ne savent pas repondre a
« ce point tombe dans quelle commune », ce qui demande un test geometrique.

Pour Power BI, le chemin reste le meme qu'avant — mais on peut desormais lui
donner un CSV deja filtre au perimetre voulu plutot que la couche entiere.

## Configuration

Tout est optionnel. Par defaut, la racine locale est `~/.gisco-mcp` : le profil
utilisateur, pas `%LOCALAPPDATA%` — un Python empaquete en donne a ses processus
enfants une vue virtualisee, et le plugin ecrirait d'un cote pendant qu'un
terminal lirait de l'autre, sans erreur.

| Variable | Effet | Defaut |
|---|---|---|
| `GISCO_HOME` | racine locale : cache et telechargements | `~/.gisco-mcp` |
| `GISCO_EXPORT_DIR` | ou atterrissent les CSV | `<racine>/exports` |
| `GISCO_CACHE_DB` | emplacement du cache SQLite | `<racine>/gisco.sqlite` |
| `GISCO_MAX_DOWNLOAD_MB` | plafond au-dela duquel une synchro doit etre confirmee | 400 |
| `GISCO_AUTOSYNC_MAX_MB` | poids sous lequel une couche se rapatrie toute seule | 6 |
| `GISCO_TIMEOUT_S` | delai des appels | 120 |
| `GISCO_CATALOGUE_TTL_JOURS` | duree de validite du releve des millesimes publies | 7 |

**Le connecteur refuse d'ecrire un export dans un chemin qui ressemble a
OneDrive, SharePoint ou Dropbox.** Ce n'est pas une precaution de style : un CSV
depose dans un dossier synchronise part chez toute l'equipe, et la base de
connaissance ne porte pas de donnees.

Compte environ **250 Mo de cache** et **300 Mo de fichiers telecharges** si l'on
rapatrie tout, codes postaux compris. Les fichiers telecharges peuvent etre
supprimes apres chargement ; les garder evite un retelechargement au prochain
millesime identique.

## Ce que ce connecteur ne fait pas

- **Aucune adresse.** GISCO s'arrete au code postal et a la commune.
- **Aucun kilometrage routier**, aucun temps de trajet, aucune optimisation de
  tournee. `gisco_distance` est une orthodromie.
- **Aucune donnee Vente-unique.** Volumes, couts et tournees vivent dans Power
  BI, Snowflake, Reflex et Shiptify. GISCO fournit la grille geographique sur
  laquelle les poser — jamais les chiffres.
- **Aucune ecriture.** Ni vers GISCO, ni vers l'ONS, ni dans la bibliotheque
  d'equipe.

## Etat au 2026-09-02

Verifie de bout en bout sur un poste Windows : les cinq couches GISCO et les
deux couches ONS legeres se rapatrient et repondent ; le test d'appartenance
retrouve Amblainville dans l'Oise et Leeds dans TLE42 ; les exports CSV et SQL
sortent le perimetre complet.

Les controles hors ligne sont **verts, 98 au total, et realignes** sur la
decouverte des millesimes : ils avaient ete ecrits contre l'ancienne resolution
par gabarit fige, et trois d'entre eux appelaient des fonctions qui n'existent
plus. Ils couvrent desormais le choix du fichier (millesime, resolution, refus
d'un millesime non publie) sur un releve simule, la coherence de chaque gabarit
avec son motif, la resolution des colonnes dont le millesime ne suit pas celui
du jeu, et la reprise de schema d'un cache monte par une version precedente.
`python server/test_offline.py` se relance tout seul dans l'environnement du
connecteur quand l'interpreteur du poste n'a pas `mcp` et `httpx`.

Le handshake MCP est verifie : 20 outils, tous annotes `readOnlyHint`, aucune
ligne parasite sur `stdout`, et les instructions du serveur — les quatre pieges,
la regle du millesime, l'interdit d'export non demande — sont desormais servies
au client au lieu de ne vivre que dans la skill.

**Un defaut corrige le 2026-09-02.** La reprise de schema ne vivait que sur le
chemin d'ecriture : sur un cache monte par une version precedente,
`gisco_couches()` echouait sur `no such column: millesime` jusqu'a la
synchronisation suivante — c'est-a-dire la premiere commande qu'on lance pour
savoir ou l'on en est, et la seule issue apparente etait de supprimer 250 Mo de
cache. Une lecture rattrape maintenant le schema, et remet le millesime depuis
l'edition : sans cela, la colonne ajoutee vide faisait passer les cinq couches
pour EN RETARD et `gisco_maj` proposait 270 Mo de retelechargement inutile.

**Une reserve honnete :** la couche `uk_onspd` a ete validee sur un echantillon
de 4 000 codes postaux, pas sur les 1,8 million. Le chemin de code est le meme —
pagination, correspondance LAD vers ITL3 dedupliquee, positions inconnues mises
a vide — mais la duree reelle du rapatriement complet reste une estimation.

**Portee macOS : non verifiee.** Le code suit les conventions de
[`../../../04_mcp/README.md`](../../../04_mcp/README.md) — `pathlib`,
`sys.executable`, racine sous le profil, encodages explicites, rien sur
`stdout` — mais il n'a pas encore tourne sur un Mac.
