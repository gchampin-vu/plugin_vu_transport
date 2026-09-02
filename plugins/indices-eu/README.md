---
updated: 2026-09-02
updated_by: Guillaume_Champin
---

# `indices-eu` — carburant, inflation et salaire minimum en Europe

Trois indices publics, en **lecture seule**, dans la session. Ce sont les trois
chiffres qu'un transporteur invoque quand il demande une révision tarifaire :
le carburant, l'inflation, les salaires. Ils sont publics, datés et opposables —
encore faut-il les lire à la bonne source et à la bonne mesure.

| Indice | Ce que c'est | Fréquence | Source |
|---|---|---|---|
| **gazole** — Union | prix à la pompe, TTC et hors taxes, 29 pays + moyennes UE et zone euro, depuis 2005, six produits | hebdomadaire | Weekly Oil Bulletin, Commission européenne (DG ENER) |
| **gazole** — Royaume-Uni | prix à la pompe en pence/litre, depuis 2003, gazole et essence 95 ; hors taxes **reconstitué** depuis l'accise et la TVA publiées | hebdomadaire | DESNZ, *Weekly road fuel prices* (`gov.uk`) |
| **gazole** — Norvège | prix à la pompe en NOK/litre, depuis 1986, gazole et essence 95 ; pas de hors taxes | mensuelle | Statistisk sentralbyrå, table `09654` |
| **inflation** | IPCH harmonisé : taux annuel, taux mensuel, moyenne glissante 12 mois, indice — tous postes **ou poste carburants** | mensuelle | Eurostat `prc_hicp_minr` |
| **salaire_minimum** | salaire minimum légal national, en EUR, en monnaie nationale ou en SPA | semestrielle | Eurostat `earn_mw_cur` |
| **salaire_minimum** — Royaume-Uni | *National Living Wage*, en **livres par heure** | annuelle, effet en avril | `gov.uk` |

**Aucune authentification, aucun secret.** Ce sont des données ouvertes. Le
connecteur démarre sans le moindre réglage.

**Trois sources pour le gazole, et ce n'est pas un détail.** Elles n'ont ni la
même unité ni la même fréquence. Chaque ligne porte donc sa `source`, son
`unite` et sa `frequence`, et le connecteur **ne convertit rien** : convertir
supposerait un taux de change à la date, c'est-à-dire fabriquer un chiffre.
Ce qui se compare d'un pays à l'autre, c'est la **variation en pourcentage** —
elle est sans unité. C'est `indices_variation`.

## La couverture sur les vingt pays de livraison

C'est la question à laquelle ce connecteur doit répondre. Le périmètre est celui
de `01_CONTEXTE/EQUIPE_LT.md` — les trois zones opérationnelles. Relevé
**le 2026-09-02 sur les sources elles-mêmes**, pas d'après leur documentation :

| Pays | prix du carburant | indice carburant | inflation | salaire minimum |
|---|---|---|---|---|
| FR BE LU NL DE ES PT IE SK | bulletin UE, 2026-08-31 | oui | 2026-08 | 2026-S2 |
| PL CZ HU | bulletin UE, 2026-08-31 | oui | 2026-07 | 2026-S2 |
| AT IT FI | bulletin UE, 2026-08-31 | oui | 2026-08 | pas de SMIC légal |
| SE DK | bulletin UE, 2026-08-31 | oui | 2026-07 | pas de SMIC légal |
| **UK** | **DESNZ, 2026-08-31** | **non** — sorti de l'IPCH fin 2020 | non | **gov.uk, 2026-04, en GBP/heure** |
| **NO** | **SSB, 2026-07** (mensuel) | oui | 2026-07 | pas de SMIC légal |
| **CH** | **aucun** — voir ci-dessous | **oui, 2026-07** | 2026-07 | pas de SMIC légal |

**Dix-neuf pays sur vingt ont un prix du carburant**, et le vingtième a un
indice. Le tableau exact, colonne par colonne, sort de `indices_pays()`.

### Le Royaume-Uni n'était pas absent, il était figé

C'était le cas dangereux, et le seul qui ne se voyait pas : il n'a jamais
disparu des fichiers européens, il s'y est **arrêté** fin 2020 — Brexit. Une
question sur le gazole outre-Manche rendait donc un prix de décembre 2020 en
tête de tableau, à côté de chiffres de la semaine dernière. Une **valeur**, pas
une case vide, donc rien ne sautait aux yeux.

Il vient maintenant de **DESNZ**, et par défaut : `indices_gasoil(pays='UK')`
rend le relevé de la semaine dernière. `source='europe'` force le bulletin
européen — utile pour comparer vingt-sept pays dans une seule unité — et
l'avertissement de gel repart alors en toutes lettres :

> ATTENTION : Royaume-Uni (UK) n'est plus publié depuis 2020-12-21, alors que la
> source va jusqu'au 2026-08-31.

L'avertissement tient même quand la question porte sur 2019 : la couverture se
mesure sur toute la série, pas sur la fenêtre demandée.

**Le hors taxes britannique est calculé, pas lu.** DESNZ publie le prix à la
pompe, le droit d'accise et le taux de TVA, donc :

```
hors taxes = pompe / (1 + tva/100) − accise
```

Les deux varient dans l'histoire — la TVA est passée de 17,5 % à 20 %, l'accise a
bougé plusieurs fois — donc le calcul se fait **ligne par ligne**. Sur juin 2003,
supposer 20 % au lieu de 17,5 % décale le hors taxes de 1,3 penny par litre. Un
contrôle hors ligne tient ce point précis.

### La Suisse n'a pas de prix, et ce n'est pas faute d'avoir cherché

Trois pistes fermées, vérifiées le 2026-09-02 : la division « prix » est
**absente** de l'API PX-Web de l'OFS, `energiedashboard.admin.ch` est une
application web dont la route `/api/` rend du HTML, et `opendata.swiss` ne porte
qu'une **republication cantonale** de l'IPC — pas une source opposable dans un
échange contractuel.

Ce qui la rend lisible quand même : **Eurostat publie l'IPCH suisse, sous-position
carburants comprise** (`CP0722`), sur le flux déjà branché.

```
indices_inflation(pays='CH', poste='carburants')
```

**C'est un indice, pas un prix**, et le connecteur le dit dans l'en-tête de
chaque réponse : un panier de consommation — essence, gazole et lubrifiants
pondérés ensemble — en base mensuelle. Il répond à « de combien a bougé le
carburant en Suisse », ce qui suffit à une clause d'indexation. Il ne répond pas
à « combien coûte le gazole en Suisse », et il **ne se compare pas** à un prix au
litre.

### Le salaire minimum britannique est horaire, et il reste horaire

Eurostat le porte encore, figé à 2020-S2. `gov.uk` le remplace, avec deux écarts
que le connecteur **ne comble pas** :

- le taux est **horaire** (12,71 £ depuis avril 2026) là où Eurostat publie un
  montant **mensuel**. Convertir supposerait une durée de travail hebdomadaire —
  donc un chiffre inventé, qui partirait ensuite dans une comparaison de coût ;
- il prend effet en **avril**, là où Eurostat découpe en semestres.

La colonne `categorie` porte la **tranche d'âge**, et ce n'est pas décoratif :
elle a changé trois fois — « 25 and over » avant 2021, puis « 23 and over »,
puis « 21 and over ». Sans elle, on suivrait une seule courbe en croyant qu'elle
porte sur la même population.

Enfin, `gov.uk` publie ces taux dans un **tableau HTML**, pas dans une série :
il n'existe pas d'API pour eux. C'est la limite acceptée de cette source, et le
serveur échoue franchement si la structure de la page change.

### Les autres pays

Hors périmètre de livraison, pour mémoire : l'Islande est absente du bulletin
pétrolier ; la Turquie, la Serbie, l'Albanie, la Macédoine du Nord et le
Monténégro aussi mais publient leur IPCH ; les États-Unis sont arrêtés à 2024-12
sur l'inflation. `indices_pays()` couvre les 47 pays du lexique.

**Les pays rendus quand la question n'en nomme aucun sont les huit principaux**
(FR, BE, NL, DE, ES, IT, PT, PL) — un choix de lisibilité, pas de couverture :
une réponse de vingt-sept lignes par semaine ne se lit pas. GB, CH et NO sont
donc disponibles mais **pas dans ce défaut** : il faut les nommer, ou changer
`INDICES_PAYS_DEFAUT`.

## Ce qu'il ne fait pas

**Il ne calcule aucune surcharge carburant et n'applique aucune clause.** Il rend
la donnée et son millésime. La formule de révision — indice de référence, part
carburant de l'assiette, périodicité, seuil, arrondi — est dans le contrat du
transporteur, sous `02_TRANSPORTEURS/<NOM>/02_tarif/`, et **deux transporteurs
n'ont pas la même**. Un connecteur qui trancherait à la place du contrat
produirait un chiffre juste appliqué à la mauvaise règle.

## Les huit outils

| Outil | Quand |
|---|---|
| `indices_sources` | ce que portent les séries, leur millésime, l'âge de chaque cache |
| `indices_pays` | le référentiel : codes, noms français acceptés, **quelle source répond pour quel pays** |
| `indices_gasoil` | prix des carburants, par pays — paramètre `source` : `auto`, `europe`, `national` |
| `indices_inflation` | IPCH par pays et par mois, cinq mesures — paramètre `poste` : `total`, `carburants` |
| `indices_salaire_minimum` | salaire minimum légal, par pays et par semestre |
| `indices_variation` | **l'écart entre deux périodes** — la forme utile pour une révision tarifaire, et la seule qui se compare entre pays d'unités différentes |
| `indices_export_csv` | la série complète en CSV, **sur demande explicite** |
| `indices_refresh` | force le rapatriement, sans attendre l'expiration du cache |

Plus `indices_doctor` en diagnostic, la skill `indices-eu` et les commandes
`/indices-export` et `/indices-setup`.

**Les neuf outils portent `readOnlyHint`**, conformément à la spécification MCP :
un client ne lit pas cette page, ce qu'il voit d'un outil est sa signature et ses
annotations. Ce ne sont que des indications — la spécification demande aux
clients de ne pas leur faire confiance — et ce qui garantit la lecture seule
reste le code : des `GET` sur trois URL publiques, et rien d'autre. Le serveur
porte aussi ses `instructions`, qui rappellent au client les trois garde-fous
avant qu'il ne cite un chiffre : aucune clause appliquée, une couverture
inégale, un millésime obligatoire.

## Les cinq pièges, et ce que le connecteur en fait

Chacun rend un chiffre faux **sans lever la moindre erreur**. C'est pour ça
qu'ils sont traités dans le serveur et pas laissés à la vigilance de qui pose la
question.

**1. `EU` et `EUR` sont deux séries différentes.** Dans le classeur du bulletin,
la colonne `CTR` vaut `EU_` pour l'Union et `EUR_` pour la zone euro. Le script
Power Query d'origine en gardait les deux premiers caractères : les deux
devenaient « EU », et la moyenne de l'Union écrasait celle de la zone euro. Ici
le code pays est lu dans le **nom** de la colonne (`FR_price_with_tax_diesel`),
jamais dans sa valeur.

**2. La Grèce est `EL` chez Eurostat et `GR` dans le bulletin pétrolier.** Un
mauvais code ne lève rien : il rend zéro ligne, ce qui se lit comme « pas de
donnée ». Les deux codes sont acceptés en entrée, la traduction est dans
`lexique.json`, et `indices_pays()` la donne noir sur blanc.

**3. Un trou d'Eurostat n'est pas un zéro.** L'API rend un index **creux** : les
combinaisons sans donnée n'y figurent pas. Le script d'origine complétait les
index manquants par 0 — ce qui donnait un salaire minimum de **0 € au
Danemark**, qui n'a pas de salaire minimum légal. Le décodage se fait ici par
pas d'indice, et ce qui manque reste manquant. Les huit pays concernés (DK, IT,
AT, FI, SE, NO, IS, CH) sont nommés dans l'en-tête de la réponse, avec la raison :
le plancher y est conventionnel, pas légal.

**4. Deux flux Eurostat sont gelés depuis le 2026-02-06.** `prc_hicp_manr` (taux
annuel) et `prc_hicp_midx` (indice) répondent encore un HTTP 200, mais leur
dernière période est **2025-12** : ils portent l'ancienne nomenclature COICOP.
Le flux vivant est `prc_hicp_minr`, classé en `coicop18`, et il porte **toutes**
les unités, taux annuel compris. Ce connecteur n'interroge que celui-là.
Interroger `manr` aujourd'hui, c'est publier une inflation vieille de huit mois
sans qu'aucune erreur ne le signale.

**5. Le certificat du bulletin pétrolier a été cassé côté Commission — et l'est
plus.** Le 2026-09-01, `energy.ec.europa.eu` présentait un certificat Amazon émis
pour `europa.eu` et `*.europa.eu` seulement. Un joker ne couvre **qu'un** label —
il valait donc pour `ec.europa.eu`, pas pour `energy.ec.europa.eu`. Ce n'était ni
le poste ni le proxy de l'entreprise : une erreur de rotation à la Commission.
**Vérifié le 2026-09-02 : corrigé.** Le classeur se télécharge en mode strict,
certificat vérifié, sans aucun réglage. Le piège reste écrit ici parce que la
rotation qui a cassé une fois recassera — voir la section suivante.

## Le certificat : le repli, et l'issue qu'on ne prend pas

**Il n'y a rien à faire aujourd'hui.** Le mode par défaut est `strict`, il
fonctionne, et c'est le bon mode. N'arme rien d'avance : un poste qui tourne en
`chaine-seule` pour un incident clos garde une vérification affaiblie pour rien,
et chaque réponse y porte un avertissement devenu faux.

Ce qui suit sert **le jour où la rotation recasse**. Le connecteur essaie
toujours `strict` d'abord et ne bascule jamais de lui-même : il refuse le
téléchargement et explique. L'inflation et le salaire minimum ne sont de toute
façon pas concernés — ils passent par `ec.europa.eu`.

| Issue | Ce que ça implique |
|---|---|
| **Poser le classeur à la main** — le télécharger dans un navigateur, pointer `INDICES_GASOIL_FILE` dessus | Rien n'est affaibli. En contrepartie le fichier ne se met plus à jour tout seul, et chaque réponse rappelle qu'elle lit un fichier local |
| **`INDICES_GASOIL_TLS=chaine-seule`** | La chaîne de certification reste vérifiée, **et** le certificat servi doit couvrir `europa.eu` — sinon le téléchargement est refusé. Seul le nom d'hôte n'est plus vérifié. Chaque réponse bâtie sur ce classeur porte l'avertissement, en toutes lettres |

**Ce qu'on ne fait pas : désactiver la vérification TLS en bloc.** Sans le
contrôle d'appartenance à `europa.eu`, n'importe quel certificat valide pour
n'importe quel domaine ferait l'affaire — et un prix de gazole falsifié entrerait
directement dans un calcul de surcharge. C'est la raison pour laquelle le mode
dégradé vérifie **plus** de choses que le mode strict n'en vérifiait, pas moins.

## Installation

Le catalogue `vu-transport` est déjà connu de ceux qui ont un autre connecteur
de l'équipe. Sinon, ajoute-le une fois, puis :

```
/plugin install indices-eu@vu-transport
```

Les dépendances (`mcp`, `httpx`, `openpyxl`) s'installent seules au premier
démarrage, dans `~/.indices-mcp/venv`. `openpyxl` n'est pas optionnel : le
bulletin pétrolier n'est publié qu'en classeur Excel, il n'y a pas d'API.

Puis `/indices-setup`, qui lance le diagnostic et dit ce que portent les trois
sources. Il n'y a plus de point bloquant à traiter à l'installation depuis que le
certificat est réparé.

## Configuration

Rien n'est obligatoire. L'ordre de priorité est celui de tous les connecteurs de
l'équipe : **configuration du plugin > `indices.env` du poste > fichier d'équipe
> défaut du serveur**. Le poste passe devant l'équipe.

| Réglage | Où il a sa place | À quoi il sert |
|---|---|---|
| `INDICES_GASOIL_URL` | **fichier d'équipe** | l'URL porte un identifiant de document que la Commission fait tourner. Le jour du 404, ça se corrige une fois pour les vingt-six postes |
| `INDICES_DESNZ_API`, `INDICES_SSB_URL`, `INDICES_NMW_API` | **fichier d'équipe** | les trois sources nationales. Rien à régler : à la différence du bulletin européen, `gov.uk` expose une **API de contenu** qui résout l'URL courante du CSV, donc la rotation hebdomadaire de l'asset ne casse rien |
| `INDICES_GASOIL_TLS` | équipe ou poste | `strict` (défaut, et ce qu'il faut garder) ou `chaine-seule`, le repli si le certificat recasse |
| `INDICES_GASOIL_FILE` | **poste uniquement** | un classeur téléchargé à la main |
| `INDICES_PAYS_DEFAUT` | équipe ou poste | les pays rendus quand la question n'en nomme aucun |
| `INDICES_DEPUIS` | équipe ou poste | depuis quand les séries Eurostat sont rapatriées (défaut 2010) |
| `INDICES_CACHE_TTL_H` | équipe ou poste | durée de vie du cache, en heures (défaut 24) |
| `INDICES_EXPORT_DIR`, `INDICES_CACHE_DIR` | **poste uniquement** | des chemins — un chemin valable ici n'existe pas sur les vingt-cinq autres postes |

Le fichier d'équipe est
`08_ENGINE/04_mcp/00_config/indices.shared.env`. **Il n'existe pas encore** : le
connecteur tourne sur ses valeurs par défaut et le dit dans `indices_doctor`. Le
créer est une écriture dans le collectif, avec ce que ça implique — elle prend
effet sur tous les postes au prochain démarrage de session, et elle se trace.

Modèle du fichier de poste : `server/indices.env.example`.

## Le cache

Des fichiers JSON sous `~/.indices-mcp/cache/`, pas une base : il n'y a aucune
jointure à faire ici. Un fichier par source et par mesure — le bulletin européen,
DESNZ, SSB, chaque couple mesure/poste d'Eurostat, le salaire minimum par devise,
et `gov.uk`. Le classeur européen pèse 4,4 Mo et se republie une fois par
semaine ; les séries Eurostat une fois par mois. Le cache expire au bout de 24 h,
et `indices_refresh()` force le rapatriement le jour où une source paraît.
`indices_doctor` liste l'âge de chacun.

**Les exports vont dans `~/.indices-mcp/exports/`, jamais dans la bibliothèque
d'équipe** : un CSV déposé dans un dossier synchronisé part chez tout le monde,
et la base de connaissance ne porte pas de données — elle dit **où** elles sont.

## Contrôles

```
python server/test_offline.py
```

153 contrôles, **sans réseau** : bornes de période, lexique et le piège grec,
dépliage JSON-stat sur un index creux, lecture d'un classeur fabriqué pour
l'occasion (avec sa colonne de taux de change qui décale les blocs et sa note de
bas de page sans date), écriture CSV, exclusivité du chemin de configuration, et
ce que le serveur déclare au client — les neuf outils publiés, leurs annotations,
les instructions de serveur.

Quatre blocs tiennent les sources nationales : le CSV DESNZ lu à la bonne date
(`jj/mm/aaaa`, pas l'inverse) et son hors taxes calculé **au taux de TVA
d'époque** ; le JSON-stat de SSB, dont le bloc hors taxes doit rester **vide** et
non recopié du TTC ; le tableau HTML de `gov.uk` et sa tranche d'âge qui change ;
et l'aiguillage lui-même — quand `source` et `poste` ont été ajoutés, les appels
positionnels faisaient atterrir `force` sur eux, l'export forçait alors un
rapatriement réseau à chaque appel **sans lever d'erreur**. Le contrôle porte sur
ce qui remonte, pas sur le texte du fichier.

Une suite qui a besoin d'Internet ne se lance pas quand il faut, c'est-à-dire
quand quelque chose est déjà cassé.

Le contrôle en ligne, lui, est `python server/bootstrap.py doctor`.

## Portabilité

Le connecteur suit les conventions de
[`08_ENGINE/04_mcp/README.md`](../../../04_mcp/README.md) : `pathlib` partout,
aucun chemin absolu en dur, racine locale sous le profil utilisateur (**pas**
`%LOCALAPPDATA%`, qu'un Python empaqueté virtualise), `sys.executable` comme
seul interpréteur nommé, `subprocess.run` avec une liste d'arguments — le chemin
du plugin traverse `Transport BtoC - Documents`, qui contient des espaces et un
tiret —, encodage explicite partout, et **rien sur `stdout`** : le journal part
sur `stderr`.

## Le jalon Webfacto

Le prototypage et l'usage individuel de ce connecteur sont libres. **Dès qu'il
s'agit d'industrialiser** — brancher ces indices sur un tableau de bord, une
révision tarifaire automatisée, un flux vers le SI ou une publication hors
SharePoint — le cas d'usage passe par un cadrage Webfacto préalable : besoin,
faisabilité, sécurité, priorisation.
