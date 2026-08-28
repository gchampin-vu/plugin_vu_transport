---
name: factures-yooz
description: Interroger la base de factures fournisseurs Yooz - facture recue, montant facture, facture bloquee, cause de blocage, echeance, avoir, fiche fournisseur, unite d'organisation, export comptable. Active des qu'une question porte sur Yooz, sur une facture fournisseur ou son montant, sur ce qui est bloque ou en attente de validation, sur ce qu'un transporteur a facture, ou des qu'il faut le cote facture d'un controle de facturation. Declenche aussi sur "yooz", "facture fournisseur", "facture bloquee", "montant facture", "avoir", "echeance de paiement", "ce que VIR nous a facture".
---

# Interroger Yooz

Yooz porte les **factures fournisseurs** des societes du groupe : ce qui a ete
recu, saisi, bloque, valide, comptabilise. C'est la source du **montant
facture** - le troisieme des trois indicateurs du controle de facturation, en
face du FL calcule en interne et du recalcul sur grille.

Les outils sont exposes par le serveur MCP `yooz` du meme plugin. Ils sont
**tous en lecture**. Valider, bloquer, comptabiliser ou importer une facture ne
se font pas ici : ces gestes engagent la comptabilite, ils restent humains dans
l'interface Yooz.

## Trois chemins, et le reflexe est de prendre le premier qui repond

**Une question sur des factures -> le direct, `yooz_live_*`. C'est le defaut.**

La grille de recherche du portail Yooz accepte des filtres serveur : tiers,
periode, echeance, montant, blocage. Une question se repond donc **en un appel,
sur l'etat courant de Yooz, sans synchro prealable**.

- `yooz_live_summary` : le montant et le volume agreges. C'est la reponse a
  "combien GLS nous a-t-il facture depuis janvier". `group_by` vaut `mois`
  (defaut), `aucun` pour un total unique, ou un code de colonne
  (`thirdPartyName`, `orgUnitName`, `documentTypeName`, `portalStatus`,
  `blockingCause`, `source_app`).
- `yooz_live_invoices` : les lignes elles-memes, pour lire le detail.
- `yooz_live_columns` : les 97 colonnes de la grille, si celle qu'il faut n'est
  pas dans le jeu par defaut.

Le filtre tiers passe par le **code du referentiel**, pas par le nom : un
`yooz_referential` en famille `fournisseur` donne le code (`GLS` -> `FGLS`),
puis `third_code="FGLS"`. Le parametre `third_name` existe en secours, mais il
filtre **cote client**, apres coup : ne l'utilise jamais sans periode.

**Une question sur un referentiel ou sur la structure -> chemin direct, un appel.**

- `yooz_referential` : la fiche d'un fournisseur par son code, le comptage d'un
  referentiel, ou une page de 100 lignes. Familles : `fournisseur`, `compte`,
  `cause_blocage`, `devise`, `mode_paiement`, `journal`, `categorie_facture`...
  Premier appel sans `referential` pour obtenir les codes disponibles.
- `yooz_org_units` : relier un `orgUnitCode` de facture (7000, 7005...) a
  l'entite qui la porte.
- `yooz_reports` / `yooz_report_peek` : les data reports disponibles et un coup
  d'oeil a leur structure, sans toucher au cache.

**Un historique large, du SQL libre, un fichier -> le cache.**

Le cache reste le bon chemin pour croiser plusieurs annees, ecrire une requete
que les filtres de la grille ne savent pas exprimer, ou produire un CSV.

1. `yooz_status` pour savoir de quand date le cache. Si la synchro est vieille
   ou absente, `yooz_sync` d'abord.
2. `yooz_invoices`, `yooz_summary` ou `yooz_sql` (SQL libre, lecture seule sur
   la table `factures`). `yooz_columns` donne les noms exacts des colonnes -
   ils ne sont pas devinables, et **ce ne sont pas ceux de la grille**.
3. `yooz_export_csv` des que le resultat doit etre joint a un mail.

## Les pieges du chemin direct

**Un filtre que la grille ne sait pas appliquer est IGNORE EN SILENCE.** Elle
repond HTTP 200 en rendant *tout*, sans le moindre signal. Un `like` sur le nom
du tiers rend la grille entiere - et un total faux. Le connecteur refuse ces
operateurs (`like`, `contains`, `startsWith`...) plutot que de laisser sortir un
chiffre errone. Ceux qui sont reellement appliques : `eq`, `neq`, `in`, `gte`,
`lte`, `gt`, `lt`, `contextual`.

**Un avoir sort de la grille en POSITIF.** Le sens est dans le type de
document, pas dans le signe - au contraire du data report, qui le rend negatif.
Le connecteur ajoute deux colonnes, `amountSigned` et `totalAmountSigned` :
**ce sont elles, et elles seules, qu'on additionne**. `yooz_live_summary` le
fait deja, et isole la part des avoirs dans `nb_avoirs` / `ttc_avoirs`.

**La grille porte tous les documents, pas seulement les factures.** On y trouve
aussi des `Autre document` et des `Devis - Proforma`, que le data report
n'inclut pas. Pour un chiffre "factures", passe `doc_kind="facture"`.

**`blocked="non"` n'est pas un filtre serveur.** Le `eq false` que poste
l'interface ne veut pas dire "non bloque" : sur 145 documents GLS tous non
bloques, il n'en rend que 8. Le connecteur applique donc `blocked="non"` cote
client. `blocked="oui"`, lui, est bien un filtre serveur.

**Un resultat tronque n'est pas un compte.** Si le plafond `max_rows` est
atteint, le connecteur le dit en tete de reponse. Les filtres cote client
(`doc_kind`, `org_unit`, `third_name`, `blocked="non"`) jouent *apres* ce
plafond : sur un resultat tronque, ils ne comptent rien. Resserre la periode.

**Le numero de piece ne se cherche qu'en entier.** La recherche partielle de
numero n'existe pas sur ce chemin.

## Les pieges du cache

**Le cache n'est pas la production.** Un chiffre sorti d'ici a l'age de la
derniere synchro. `yooz_status` la donne par societe. Sur GLS, deux factures de
juillet avaient ete redatees en aout dans Yooz depuis la synchro de la veille :
le cache repondait juste, sur un etat perime. Quand la fraicheur compte, prends
le chemin direct.

**`mode="full"` est le defaut, et c'est voulu.** `lastExecutionDatetime` ne
filtre que si le data report porte lui-meme un filtre sur cette date : un
`mode="delta"` peut donc rendre le meme volume qu'un `full` sans que rien ne le
signale. Le cache etant en `INSERT OR REPLACE`, un `full` est toujours juste.

**Les lignes de facture ne sont pas dans le cache par defaut.** La colonne
`YZ_INVOICE_LINE` est ecartee. Pour du ligne a ligne, il faut un data report
dedie rapatrie dans son propre `dataset` - `yooz_reports` dira s'il en existe un.

## Deux pieges qui valent partout

**Le telechargement d'un export comptable peut avoir un effet.** L'API marque le
fichier comme telecharge par defaut, ce qui peut le faire sauter a
l'integration comptable. `yooz_export_download` envoie
`ignoreMarkAsDownloaded=true` d'office : ne passe `mark_as_downloaded=True` que
si tu veux deliberement consommer le fichier.

**Le nom Yooz d'un tiers n'est pas le nom maison du transporteur.** Croise avec
l'annuaire du contexte d'equipe (`01_CONTEXTE/PORTEFEUILLE_ET_ZONES.md`) avant
de conclure sur le portefeuille : « Rhenus » designe quatre entites,
« Posten Bring » et « Bring » sont deux prestataires distincts.

## Citer un chiffre Yooz

Avec son perimetre, sa periode et sa source, sinon il est inutilisable en
negociation ou en revue. Sur le chemin direct, la source est l'heure de
lecture ; sur le cache, c'est la date de synchro.

> 72 documents GLS, DistriService / VUL, dates de facture du 2026-01-01 au
> 2026-08-28, 2 529 242,97 EUR TTC dont 12 avoirs pour -4 960,95 - Yooz lu en
> direct le 2026-08-28 a 14h43.

Et la regle qui ne bouge pas : **on n'invente jamais un chiffre**. S'il n'est pas
rendu par un appel, il n'existe pas.

## Quand ca coince

`yooz_status` en premier. Il dit d'ou vient la configuration, si le jeton
s'obtient, si l'API repond, et ou en est le cache. Les erreurs qui reviennent :

- `403 EMPTY_OR_BAD_APPLICATION_ID` : l'`applicationId` est faux. C'est un
  en-tete HTTP, un par societe, et **ce n'est pas le `client_id`**.
- `invalid_grant` sur le jeton : le refresh token est revoque ou expire. Il faut
  en regenerer un dans Yooz et le remettre dans la configuration du plugin.
- `Column X is not defined in component configuration` sur un `yooz_live_*` :
  la colonne, ou la propriete de filtre, n'existe pas sur la grille.
  `yooz_live_columns` donne les codes valides.
- `NOT_RIGHT_ROLE_ON_COMPONENT` : la grille visee n'est pas ouverte a ce compte.
  Celle des documents est `-18`, et c'est le defaut.
