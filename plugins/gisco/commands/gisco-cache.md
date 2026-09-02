---
description: Met en service le connecteur GISCO sur ce poste, ou explique pourquoi il ne repond pas. Rapatrie les couches geographiques une fois pour toutes, et dit ce que ca pese.
---

Tu mets en service le connecteur **GISCO** sur ce poste, ou tu diagnostiques
pourquoi il ne repond pas. `$ARGUMENTS` precise eventuellement les couches
voulues.

## Il n'y a pas de cle a saisir

GISCO est un service public ouvert d'Eurostat, et l'ArcGIS de l'ONS aussi. Ce
connecteur n'a **aucun secret** a configurer : ni cle d'API, ni compte, ni
fichier `.env`. Si quelqu'un te demande une cle pour ce connecteur, c'est une
erreur - dis-le.

Ce qu'il faut en revanche, c'est **rapatrier les couches**, une fois. Les
fichiers d'Eurostat sont gros ; on ne les retelecharge pas a chaque question.

## Conduis dans cet ordre

**1. `gisco_doctor()`.** Il dit quel Python execute le serveur, ou vivent le
cache et les exports, quelles couches sont deja la, et si GISCO repond. Lis-le
avant de conclure quoi que ce soit.

**2. Annonce le cout avant de rapatrier.** Ce n'est pas une formalite : le cache
complet occupe environ 250 Mo, et les fichiers telecharges 300 Mo de plus.
Donne le tableau, puis demande ce qu'on veut.

| Couche | Ce qu'elle apporte | Poids | Duree |
|---|---|---|---|
| `nuts` | regions, niveaux 0 a 3 | 1,5 Mo | quelques secondes |
| `countries` | pays, statut UE / AELE | 1 Mo | quelques secondes |
| `urau` | villes et zones urbaines fonctionnelles | 26 Mo | moins d'une minute |
| `lau` | 98 000 communes | 75 Mo | une a deux minutes |
| `pcode` | 830 000 codes postaux europeens | 200 Mo | deux a cinq minutes |
| `uk_lad` + `uk_itl3` | communes et NUTS3 britanniques (ONS) | leger | moins d'une minute |
| `uk_onspd` | 1,8 million de codes postaux britanniques | long | **une dizaine de minutes**, environ 900 appels |

`nuts` et `countries` se rapatrient **toutes seules** a la premiere question qui
en a besoin : elles sont sous le seuil de rapatriement automatique. Les autres
se demandent.

**3. Le socle raisonnable, pour Vente-unique.** Si on te dit « installe ce qu'il
faut » sans plus de precision, propose : `nuts`, `countries`, `pcode`, `lau`.
C'est ce qui repond aux questions de zonage sur les pays ou l'on livre. Ajoute
`uk_lad` et `uk_itl3` si le Royaume-Uni est dans le perimetre - ils sont legers.
**Ne lance `uk_onspd` que si on te le demande explicitement**, et redis la duree
avant.

**4. Verifie, et dis ce qui est charge.** `gisco_couches()` apres coup : couche,
millesime, nombre de lignes, date. C'est cette date qu'il faudra citer dans les
reponses.

**5. Sur un poste deja installe, dis le retard, ne le rattrape pas seul.**
`gisco_couches()` compare chaque couche GISCO au dernier millesime publie par
Eurostat — les millesimes ne sont pas ecrits en dur, ils sont decouverts. S'il y
a du retard, chiffre-le avec `gisco_maj(simuler=True)`, qui dit ce qui serait
retelecharge et ce que ca pese, **sans rien telecharger**. `simuler=False` ne se
lance que si on te le demande. Les couches britanniques n'y sont pas : leur
millesime vit dans le nom du service ArcGIS, et il se change a la main dans
`catalogue.json`.

## Le piege a annoncer des l'installation

**Le millesime LAU 2024 ne porte aucune population pour la France ni pour
l'Espagne** : les 34 946 communes francaises et les 8 132 communes espagnoles y
sont a zero. Le millesime 2023, lui, est complet.

Donc : `lau` en 2024 pour le **decoupage** (c'est le plus recent), et
`gisco_sync(couche="lau", annee="2023")` si la question porte sur la
**population**. Les deux ne peuvent pas etre en cache en meme temps - la
synchronisation remplace. Dis-le au moment ou tu installes, pas le jour ou
quelqu'un sort un tableau de population a zero.

## Quand ca ne repond pas

- **« couche absente du cache »** : ce n'est pas une panne, c'est une
  synchronisation qui manque. `gisco_sync` avec la couche nommee dans le message.
- **Telechargement qui echoue** : GISCO est en `https` sur `europa.eu`. Derriere
  un proxy d'entreprise, il faut `HTTP_PROXY` / `HTTPS_PROXY` dans
  l'environnement. Le message d'erreur le rappelle.
- **« Telechargement incomplet »** : le fichier partiel est supprime tout seul,
  relance simplement.
- **Le dossier d'export est refuse** : il pointe un espace synchronise
  (OneDrive, SharePoint). C'est voulu. Change `GISCO_EXPORT_DIR` pour un dossier
  local.

## Ce que tu ne fais pas

- **Tu ne mets pas le cache dans la bibliotheque d'equipe.** 550 Mo qui
  remontent dans le drive partage, ce n'est pas un cache, c'est un incident.
- **Tu ne lances pas `uk_onspd` « pour voir ».**
