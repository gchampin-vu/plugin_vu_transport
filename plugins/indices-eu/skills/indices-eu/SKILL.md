---
name: indices-eu
description: Interroger les trois indices publics qui font bouger un tarif de transport, sur les vingt pays de livraison de Vente-unique - le prix du carburant (Weekly Oil Bulletin pour l'Union, DESNZ / gov.uk pour le Royaume-Uni, Statistisk sentralbyra pour la Norvege), l'inflation harmonisee IPCH (Eurostat, mensuelle, tous postes ou poste carburants) et le salaire minimum legal national (Eurostat, semestriel ; gov.uk pour le Royaume-Uni, en livres par heure). Active des qu'une question porte sur le prix du carburant ou du gazole dans un pays, sur son evolution, sur une surcharge carburant ou une indexation gazole, sur l'inflation d'un pays europeen, sur le cout de la main-d'oeuvre ou le salaire minimum d'un pays, ou des qu'un transporteur demande une revision tarifaire au motif du carburant, de l'inflation ou des salaires. Active aussi des qu'une question porte sur le carburant, l'inflation ou le salaire minimum au ROYAUME-UNI, en SUISSE ou en NORVEGE - trois pays que les sources europeennes ne couvrent pas ou plus, et pour lesquels ce connecteur porte des sources nationales (DESNZ / gov.uk, Statistisk sentralbyra) ou un indice de substitution. Declenche aussi sur "gazole", "gasoil", "diesel", "prix a la pompe", "surcharge carburant", "indice carburant", "bulletin petrolier", "IPCH", "HICP", "inflation", "indexation", "revision tarifaire", "salaire minimum", "SMIC europeen", "National Living Wage", "cout du travail", "DESNZ", "SSB", "prix du carburant au Royaume-Uni", "gazole en Suisse", "carburant en Norvege", "export des indices".
---

# Les indices europeens : gazole, inflation, salaire minimum

Trois series publiques, en lecture seule, servies par le serveur MCP `indices`
du meme plugin. Aucune authentification : ce sont des donnees ouvertes de la
Commission europeenne et d'Eurostat.

| Indice | Ce que c'est | Frequence | Source |
|---|---|---|---|
| **gazole** - Union | prix a la pompe, TTC et hors taxes, 29 pays + moyennes UE et zone euro, depuis 2005 | hebdomadaire | Weekly Oil Bulletin, DG ENER |
| **gazole** - Royaume-Uni | prix a la pompe en pence/litre, depuis 2003, hors taxes reconstitue | hebdomadaire | DESNZ, gov.uk |
| **gazole** - Norvege | prix a la pompe en NOK/litre, depuis 1986, pas de hors taxes | mensuelle | Statistisk sentralbyra, table 09654 |
| **gazole** - Suisse | AUCUN prix disponible - l'indice IPCH du poste carburants le remplace | mensuelle | Eurostat, CP0722 |
| **inflation** | IPCH harmonise : taux annuel, taux mensuel, moyenne glissante 12 mois, indice | mensuelle | Eurostat `prc_hicp_minr` |
| **salaire_minimum** | salaire minimum legal national, en EUR, en monnaie nationale ou en SPA | semestrielle | Eurostat `earn_mw_cur` |

C'est la source a citer quand un transporteur demande une revision tarifaire au
motif du carburant, de l'inflation ou des salaires : les trois chiffres sont
publics, dates, et opposables.

## La chose a ne jamais faire : appliquer la clause a la place du contrat

**Ce connecteur rend un ecart constate, jamais une surcharge.** La formule de
revision - indice de reference, part carburant de l'assiette, periodicite,
seuil de declenchement, arrondi - est dans le contrat du transporteur, sous
`02_TRANSPORTEURS/<NOM>/02_tarif/`, et **deux transporteurs n'ont pas la meme**.

Donc : tu rends « le gazole a monte de 30,3 % en France entre juin 2024 et aout
2026 ». Tu ne rends pas « la surcharge passe donc a X % » sans avoir lu la
clause. Si personne ne l'a lue, dis-le : c'est une phrase, pas un blocage.

## Choisir la bonne mesure — c'est la que ca se joue

**Le gazole : TTC ou hors taxes ?**

- **Hors taxes** pour comparer deux pays : dans le TTC, la fiscalite ecrase tout
  le reste, et un ecart France/Espagne y mesure surtout un ecart d'accise.
- **TTC** pour ce que paie reellement un transporteur qui ne recupere pas la
  TVA, et pour une clause qui vise le prix a la pompe.
- Le prix est en **euros par 1000 litres** : 2231 veut dire 2,231 EUR le litre.
  Ne recopie jamais 2231 comme un prix au litre.
- Le fioul lourd (`fuel_oil_1`, `fuel_oil_2`) est en **EUR/tonne**, pas au
  litre. Melanger les deux dans un tableau donne un facteur mille.

**L'inflation : un taux ou un indice ?**

- **`annuel`** (defaut) : l'inflation au sens courant, ce qu'on cite.
- **`moyenne_12m`** : la moyenne glissante sur douze mois. C'est ce que visent
  la plupart des clauses d'indexation, parce qu'elle lisse la saisonnalite.
- **`indice`** (base 2025 = 100) : le seul qui se compare **entre deux dates**.
  Additionner des taux mensuels ne donne pas une evolution de prix.

**Le salaire minimum : S1 = au 1er janvier, S2 = au 1er juillet.** La valeur est
un montant mensuel brut.

## La couverture n'est pas la meme d'une serie a l'autre

| Pays | gazole | inflation | salaire minimum |
|---|---|---|---|
| **Royaume-Uni** | fige au 21/12/2020 | fige a 2020-11 | fige a 2020-S2 |
| **Suisse, Norvege, Islande** | **absents** du bulletin | a jour, un mois de retard | pas de salaire minimum legal |
| Turquie, Serbie, Albanie, Macedoine du Nord, Montenegro | absents du bulletin | a jour | publies |
| Etats-Unis | absent | fige a 2024-12 | publie |

**Le Royaume-Uni est le cas piegeux** : il n'a pas disparu des fichiers, il s'y
est **fige** fin 2020. Sans avertissement, une question sur le gazole
outre-Manche rendrait un prix de decembre 2020 a cote de chiffres de la semaine
derniere. Le serveur le detecte et l'ecrit dans l'en-tete - **recopie cet
avertissement**, ne rends jamais une valeur britannique comme si elle etait
courante.

Pour la Suisse et la Norvege, le bulletin petrolier n'a **aucune** serie : ce
n'est pas un trou de la semaine, c'est un pays hors perimetre. Si la question
porte sur le carburant en Suisse, dis-le et propose l'inflation, qui elle est a
jour.

## Les pieges qui rendent un chiffre faux sans lever d'erreur

**1. La Grece.** Eurostat la code `EL`, le bulletin petrolier la code `GR`. Le
serveur traduit dans les deux sens et accepte les deux en entree - mais si tu
vois zero ligne sur la Grece, c'est le premier reflexe a verifier avec
`indices_pays()`.

**2. Huit pays n'ont pas de salaire minimum legal national** : DK, IT, AT, FI,
SE, NO, IS, CH. Le plancher y est fixe par convention collective, branche par
branche. Ils sortent **sans ligne**, et l'en-tete de la reponse le dit. **Une
case vide n'est pas un zero** - c'est exactement l'erreur du script Power Query
d'origine, qui donnait un salaire minimum de 0 EUR au Danemark.

**3. Les flux Eurostat geles.** `prc_hicp_manr` et `prc_hicp_midx` repondent
encore un HTTP 200 mais sont **figes depuis le 2026-02-06** (derniere periode
publiee : 2025-12). Le connecteur n'interroge que `prc_hicp_minr`, qui est a
jour et porte toutes les unites. Si tu vois passer une inflation qui s'arrete
fin 2025, c'est ce piege - pas un trou de publication.

**4. Le statut de publication.** Une valeur peut porter `e` (estimee) ou `p`
(provisoire). La colonne `statut` le dit. Une valeur provisoire ne part pas dans
un courrier a un transporteur sans le mot « provisoire » a cote.

**5. Le certificat du bulletin petrolier.** Le 2026-09-01, la Commission servait
sur `energy.ec.europa.eu` un certificat emis pour `europa.eu` et `*.europa.eu`
seulement, qui ne couvrait pas ce sous-domaine. **Corrige le 2026-09-02** : le
telechargement strict passe, et c'est le mode par defaut. Il n'y a rien a armer.

Ce qui reste a savoir : si une reponse porte « NOM D'HOTE NON VERIFIE », ce poste
tourne encore en mode `chaine-seule`. Deux choses a faire, dans cet ordre —
**recopier cet avertissement** si le chiffre part dans un echange contractuel, et
signaler que le reglage peut maintenant etre retire.

## Les trois pays qui ne passent pas par les sources europeennes

C'est ce qu'il faut savoir avant de repondre sur GB, CH ou NO - trois pays de
livraison de Vente-unique, avec des transporteurs reels : AIT au Royaume-Uni,
SwissPost Home et Planzer en Suisse, Posten Bring en Norvege.

| Pays | Ce qui se passe | Ce que tu appelles |
|---|---|---|
| **Royaume-Uni** | le bulletin europeen l'a encore mais FIGE en 2020. Le connecteur passe **par defaut** par DESNZ (gov.uk), qui est a jour. Unite : **pence par litre** | `indices_gasoil(pays='UK')` - rien de special a faire |
| **Norvege** | hors bulletin europeen. Source nationale SSB, **mensuelle**, en **couronnes par litre**, pas de hors taxes | `indices_gasoil(pays='NO')` |
| **Suisse** | **aucun prix du carburant n'existe** en API publique. Sa seule mesure est un **indice** | `indices_inflation(pays='CH', poste='carburants')` |

**Ne convertis jamais ces valeurs entre elles.** Un prix en pence par litre et un
prix en euros par 1000 litres ne se comparent pas sans un taux de change a la
date, et ce connecteur n'en pose aucun - c'est voulu. Si la question est
« qui a le gazole le plus cher », la reponse honnete passe par la **variation en
pourcentage**, qui est sans unite : `indices_variation`.

**Et l'indice suisse n'est pas un prix.** Le poste `carburants` de l'IPCH est un
panier de consommation - essence, gazole et lubrifiants ponderes ensemble. Il dit
de combien ca a bouge, pas combien ca coute. Le connecteur le rappelle dans
l'en-tete : ne retire pas cette phrase de ta reponse.

Le salaire minimum britannique suit la meme logique : il vient de gov.uk, en
**livres par HEURE**, quand Eurostat publie du **mensuel**. Le connecteur ne
convertit pas, et toi non plus - il faudrait une duree de travail hebdomadaire,
donc un chiffre invente. La colonne `categorie` porte la tranche d'age, qui a
change trois fois depuis 2020 : cite-la.

`indices_pays()` donne le tableau complet - quelle source repond pour quel pays,
qui a un prix, qui n'a qu'un indice.

## Comment tu conduis

**Une question de niveau** — « le gazole en France aujourd'hui », « l'inflation
en Espagne », « le salaire minimum en Pologne » → `indices_gasoil`,
`indices_inflation`, `indices_salaire_minimum`. Borne la periode : sans
`depuis`, le gazole remonte a 2003 et le rendu est tronque.

**Une question sur GB, CH ou NO** → voir la section ci-dessus. En pratique, tu
appelles la meme chose : c'est le connecteur qui choisit la source et qui te dit
laquelle a repondu, dans la colonne `source` de chaque ligne.

**Une question d'evolution** — « de combien a monte le gazole depuis la
signature », « quelle inflation entre 2024 et aujourd'hui » →
**`indices_variation`**, qui compare la **moyenne des releves** de deux
periodes. Une semaine prise seule est du bruit : le gazole bouge de plusieurs
pour cent d'une semaine a l'autre.

**Un fichier** — une etude de revision, un controle de surcharge, un envoi a un
transporteur → **`indices_export_csv`**, et **seulement si l'utilisateur a
demande un fichier**. Il rend la serie complete, sans plafond d'affichage.

**Un doute sur la fraicheur** — `indices_sources()` dit le millesime de chaque
serie et l'age de chaque cache, source par source. `indices_refresh()` force le
rapatriement le jour ou une source vient de paraitre.

**Rien ne repond** — `indices_doctor()`. Il sonde les six sources une par une,
chacune sur le pays qu'elle sert : un gazole « OK » sur la France ne dirait rien
d'un DESNZ hors service.

## Ce que tu rends

1. **Le chiffre, son unite et sa date.** « 2231 EUR/1000 l au 24 aout 2026 »,
   pas « 2231 ». L'unite n'est plus la meme d'un pays a l'autre depuis que le
   gazole a trois sources : lis-la sur la LIGNE, pas dans l'en-tete.
2. **La source, quand elle n'est pas celle qu'on attend.** « 183,49 pence par
   litre au 31 aout, releve DESNZ » - parce que le lecteur, lui, croit que tout
   vient du bulletin europeen.
3. **Le millesime de la source**, repris de l'en-tete de l'outil. Un prix de
   gazole sans sa semaine ne veut rien dire.
4. **Ce qui manque**, s'il manque quelque chose : un pays sans donnee, une
   valeur provisoire, un rendu tronque, un melange d'unites. L'en-tete des
   outils le dit deja - ne le retire pas de ta reponse.
5. **Le lien vers le contrat** quand la question est tarifaire : le chiffre est
   ici, la clause est dans `02_TRANSPORTEURS/<NOM>/02_tarif/`.

## Ce que tu ne fais pas

- **Tu n'ecris jamais dans la bibliotheque d'equipe.** Les exports vont dans le
  dossier local du connecteur. Un chiffre recopie dans la base y sera faux dans
  trois semaines : la base dit **ou** sont les chiffres, pas leur valeur.
- **Tu n'inventes ni un chiffre ni une date.** Si une source ne repond pas, tu
  le dis - tu ne comble pas avec ce que tu crois savoir du prix du gazole.
- **Tu ne compares pas deux indices de frequences differentes sans le dire.** Le
  gazole est hebdomadaire, l'inflation mensuelle, le salaire minimum
  semestriel : sur un meme graphique, ils n'ont pas le meme pas de temps.
