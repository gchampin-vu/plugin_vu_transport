---
name: gisco
description: Interroger le referentiel geographique europeen d'Eurostat (GISCO) - pays, regions NUTS, communes LAU, villes et zones urbaines fonctionnelles, codes postaux avec leur commune, leur NUTS3 et leur degre d'urbanisation. Active des qu'une question porte sur un code postal et ce qu'il recouvre, sur une commune, sur un decoupage administratif ou statistique europeen, sur le rattachement d'un point ou d'une adresse a sa commune ou a sa region, sur une zone urbaine ou rurale, sur une distance a vol d'oiseau, ou des qu'il faut construire ou verifier un zonage de livraison. Declenche aussi sur "gisco", "eurostat", "NUTS", "NUTS3", "LAU", "code postal", "codes postaux", "commune", "zonage", "decoupage administratif", "urbain ou rural", "densite de population", "dans quelle commune", "dans quelle region", "coordonnees GPS", "latitude longitude", "ITL3", "ONSPD", "referentiel geographique".
---

# Interroger GISCO

GISCO est le service geographique d'**Eurostat**. Il publie, librement, le
decoupage administratif et statistique europeen : pays, regions NUTS, communes,
villes, codes postaux. C'est le referentiel qui permet de repondre a « ce code
postal, c'est quelle commune, quelle region, zone dense ou rurale ? » sans
acheter de base d'adresses.

Les outils sont exposes par le serveur MCP `gisco` du meme plugin. Ils sont
**tous en lecture** : GISCO ne publie que des fichiers statiques, il n'y a rien
a ecrire.

**Aucune cle n'est necessaire.** Si quelqu'un cherche a en configurer une pour ce
connecteur, c'est une erreur.

## La regle qui prime sur tout : dire le millesime

Un decoupage administratif **change**. Les communes fusionnent, les codes
postaux naissent, les regions sont redecoupees. Une reponse geographique sans
millesime est invérifiable, et elle sera fausse dans deux ans sans que personne
ne s'en apercoive.

Chaque outil rend une ligne `Source :` avec le millesime et la date de
rapatriement. **Recopie-la dans ta reponse.** Ce n'est pas de la decoration :
c'est ce qui rend la reponse verifiable.

### Un millesime en retard se dit, il ne se rattrape pas dans ton dos

`gisco_couches()` compare ce qui est en cache au dernier millesime publie par
Eurostat et signale les couches en retard. Quand c'est le cas, **dis-le dans ta
reponse** au lieu de mettre a jour de ton propre chef : `gisco_maj()`
telecharge, et une mise a jour de `pcode` represente 200 Mo. L'outil **simule
par defaut** ; ne passe `simuler=False` que si on te l'a demande.

Avant de mettre `lau` a jour, relis le piege 1 : le millesime le plus recent
n'est pas le mieux renseigne.

## Avant de conclure qu'une donnee manque

`gisco_couches()` dit ce qui est rapatrie sur ce poste. Neuf fois sur dix, une
donnee « absente » est simplement une couche pas encore synchronisee - et le
message d'erreur donne la commande exacte a lancer.

Les couches legeres (`nuts`, `countries`) se rapatrient toutes seules. Les
autres se demandent, parce qu'elles ecrivent des dizaines ou des centaines de
mega-octets sur le disque. La commande `/gisco-cache` conduit l'installation.

## Les quatre pieges qui produisent une reponse fausse sans erreur

**1. La population n'existe pas pour la France ni l'Espagne au millesime LAU
2024.** Les 34 946 communes francaises et les 8 132 communes espagnoles y sont a
**zero** - ce qui est une absence de mesure, pas un chiffre. Le millesime 2023
est complet. Donc : pour une question de population sur ces deux pays, recharge
`gisco_sync(couche="lau", annee="2023")` et dis-le. Pour une question de
perimetre ou de geometrie, 2024 reste le bon millesime. L'outil `gisco_lau`
previent tout seul quand tout le perimetre est a zero - **ne passe pas outre
cet avertissement**.

**2. EL et UK, pas GR et GB.** GISCO suit la nomenclature Eurostat : la Grece
est `EL`, le Royaume-Uni `UK`. Un rapprochement avec un fichier maison en GR ou
GB rate ces deux pays **en silence**. Le connecteur accepte les deux graphies en
entree ; le fichier de l'autre cote, lui, ne les accepte pas - signale-le quand
tu prepares un rapprochement.

**3. Un code postal n'a qu'une seule commune de rattachement.** GISCO rend une
ligne par code postal, attachee a une commune. Un code postal qui couvre
plusieurs communes n'en porte qu'une : `60110` sort avec **Meru**, alors qu'il
couvre aussi Amblainville. Ne presente jamais cette commune comme « la » commune
du code postal - c'est la commune de rattachement, et c'est different.

**4. Une distance GISCO est a vol d'oiseau.** `gisco_distance` rend une
orthodromie. Par la route, compte 20 a 30 % de plus, davantage en relief ou de
part et d'autre d'un bras de mer. C'est un ordre de grandeur pour degrossir un
zonage, **jamais** une base de facturation.

## Choisir le bon outil

| La question | L'outil |
|---|---|
| Ce code postal, c'est quelle commune, quelle region, urbain ou rural ? | `gisco_resoudre` — il accepte une liste entiere et **dit lesquels sont introuvables** |
| Tous les codes postaux d'un departement, d'une region, d'une densite | `gisco_codes_postaux` |
| Ces coordonnees GPS, c'est ou ? | `gisco_localiser` — vrai test geometrique, hors ligne |
| Cette adresse, c'est quelles coordonnees ? | `gisco_geocoder`, **puis** `gisco_localiser` sur le resultat |
| Une region, un departement, son code | `gisco_nuts` |
| Une commune, sa population, sa superficie | `gisco_lau` |
| L'aire d'attraction d'une ville | `gisco_villes` |
| Combien de, repartition, le plus, par | `gisco_repartition` — **pas** un export |
| Un croisement que les filtres ne disent pas | `gisco_tables()` puis `gisco_sql` |
| Un fichier a livrer | `gisco_export_csv` ou `/gisco-export` |
| Le cache est-il a jour ? | `gisco_couches()` puis `gisco_maj(simuler=True)` — qui chiffre sans rien telecharger |

### Geocoder n'est pas le referentiel

`gisco_geocoder` interroge l'API de recherche de GISCO, qui s'appuie sur
**OpenStreetMap** : donnee collaborative, tres bonne en ville, inegale en zone
rurale, et pas officielle. Elle sert a trouver **des coordonnees**. Le
rattachement administratif ferme, lui, se fait ensuite avec `gisco_localiser`,
qui teste l'appartenance geometrique aux contours officiels. Ne presente jamais
le `city` ou le `county` rendu par le geocodeur comme la commune officielle.

### « Une ville », ca veut dire trois choses

`gisco_villes` distingue :

- **C** — la ville au sens administratif ;
- **K** — son noyau dense ;
- **F** — la zone urbaine fonctionnelle, l'aire d'attraction, bassin de
  main-d'oeuvre compris.

Pour Lyon, C fait 297 km2 et F en fait 4 127 : un facteur quatorze. Quand
quelqu'un dit « on livre sur Lyon », **demande laquelle** avant de chiffrer.

## Le Royaume-Uni est un cas a part, et il se dit

Le Royaume-Uni est sorti du perimetre NUTS et LAU apres le Brexit : GISCO ne le
publie plus. L'equivalent vient de l'**ONS** et se charge dans les memes tables,
avec `source='ONS'` :

- `uk_lad` — les districts, equivalents des communes. **Sans population** :
  l'ONS ne la publie pas dans cette couche. C'est un manque, pas un zero.
- `uk_itl3` — l'equivalent des NUTS3. Les codes commencent par **TL** la ou les
  anciens codes britanniques commencaient par UK : `TLC31` etait `UKC31`.
- `uk_onspd` — 1,8 million de codes postaux. **Une dizaine de minutes de
  rapatriement** : ne le lance jamais sans le demander, et redis la duree avant.

Quand une reponse melange les deux origines, **dis-le**. Un decompte de communes
par pays qui additionne GISCO et ONS sans le signaler n'est pas comparable :
un district britannique n'est pas une commune francaise.

## L'export est sur demande, jamais par defaut

La reponse par defaut va dans la session, pas dans un fichier. N'appelle
`gisco_export_csv` que si on a demande un fichier, un export, un CSV, ou
« quelque chose a ouvrir dans Excel ». Un perimetre trop gros pour la
conversation ne justifie pas un export tout seul : resserre, ou agrege avec
`gisco_repartition`.

Un export va **toujours** dans le dossier local du connecteur, jamais dans la
bibliotheque d'equipe : un CSV depose dans un dossier synchronise part chez tout
le monde, et la base de connaissance ne porte pas de donnees.

N'ajoute `avec_geometrie=True` que si on te l'a demande : les contours font
passer un fichier de quelques centaines de kilo-octets a plusieurs centaines de
mega-octets. C'est utile pour une carte Power BI, inutile dans un tableur.

## Ce que GISCO ne dit pas

- **Aucune adresse.** GISCO s'arrete au code postal et a la commune.
- **Aucun kilometrage routier**, aucun temps de trajet, aucune tournee.
- **Aucune donnee Vente-unique.** Les volumes, les couts et les tournees sont
  dans Power BI, Snowflake, Reflex, Shiptify. GISCO fournit la **grille
  geographique** sur laquelle les poser, pas les chiffres.

Quand une question demande de croiser un zonage GISCO avec des volumes maison,
sors le zonage en CSV et dis explicitement d'ou doivent venir les volumes. On
n'invente jamais un chiffre.
