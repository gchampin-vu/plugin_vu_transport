---
name: shiptify
description: Interroger la base Shiptify - envois, demandes de transport, acheminement middle mile vers les agences, commandes, lieux, transporteurs, factures et lignes de facture, evenements. Active des qu'une question porte sur Shiptify, sur un envoi ou une demande de transport, sur un acheminement vers une agence, sur un numero de tracking Shiptify, ou des qu'il faut extraire des envois en CSV. Declenche aussi sur "shiptify", "middle mile", "demande de transport", "expedition vers l'agence", "export des envois".
---

# Interroger Shiptify

Shiptify porte les **demandes de transport middle mile vers les agences**. C'est
la source pour une question d'acheminement, et un complement du controle de
facturation cote lignes de facture.

Les outils sont exposes par le serveur MCP `shiptify` du meme plugin. Ils sont
**tous en lecture**. Creer un transport, annuler un envoi ou confirmer un
enlevement ne se font pas ici : ces operations engagent un transporteur, elles
restent un geste humain dans l'interface Shiptify.

## L'export est sur demande, jamais par defaut

**La reponse par defaut va dans la session, pas dans un fichier.** N'appelle
`shiptify_export_csv` que si l'utilisateur a demande un fichier, un export, un
CSV ou un classeur : cet outil ecrit sur le disque, ce n'est pas une etape de
routine. Sans cette demande, reponds avec les outils de liste, une projection
`fields` serree, et cite le chiffre depuis ce que la liste a rendu.

Un perimetre trop gros pour la session ne justifie pas un export de ta propre
initiative : resserre les dates, filtre sur une agence ou un transporteur, ou
dis a l'utilisateur que le perimetre demande un export et laisse-le decider.

## Si la cle manque

Un outil qui repond `SHIPTIFY_API_KEY manquant` veut dire que le connecteur
n'est pas encore configure sur ce poste. Appelle `shiptify_setup_status` : il
donne la marche a suivre. La commande `/shiptify-setup` fait le tour complet.

**Ne demande jamais a l'utilisateur de coller sa cle dans la conversation**, et
n'appelle aucun outil en lui passant une cle en parametre - aucun ne l'accepte.
La saisie se fait dans l'interface de Claude Code (`/plugin` > shiptify >
configuration), ou la cle reste hors du contexte du modele.

## Compter n'est pas lister : commence par l'agregation

**Des qu'une question commence par « combien », « quel est le plus »,
« repartition », « par mois », « par pays » : c'est `shiptify_summary`, pas
`shiptify_list_shipments`.**

Ce n'est pas une preference de style, c'est un ordre de grandeur. Mesure du
2026-08-28 : cent envois en colonnes completes pesent 40 000 tokens, et la
projection courte 4 100. Compter les 1 051 envois de juillet en les listant
coute donc environ 120 000 tokens et un comptage a la main ; le meme compte
agrege rend 550 caracteres, en 1,4 seconde, et il est juste.

`shiptify_summary` pagine le perimetre, agrege cote serveur, et **dit toujours
combien de lignes il a parcourues et si le parcours est alle jusqu'au bout**.
Un agregat sur un parcours incomplet n'est pas un total : c'est ecrit en
majuscules dans sa reponse, recopie-le.

Lister reste la bonne reponse quand la question porte sur **des lignes** : « quels
sont les envois bloques », « montre-moi les cinq derniers vers Madrid ».

## Traduis les noms maison avant de filtrer

**`shiptify_resolve` avant de conclure qu'un transporteur n'a pas de flux.**
C'est lui qui sait que « VIR » se cherche sur `address_dest.name` avec trois
libelles (VIR, JP HOME, JPH), que « XPO » porte quatre identifiants Shiptify, et
qu'un transporteur du portefeuille absent d'ici releve de Yooz, pas d'une
absence de flux.

Les outils de liste et d'agregation acceptent directement `carrier="VIR"` : la
resolution est faite cote serveur et l'entete dit ce qu'elle a produit. Le filtre
est applique **cote client**, l'API Shiptify n'ayant aucun filtre transporteur
sur `/shipments/` - donc sur un resultat tronque il ne donne pas un compte.

`shiptify_lexique` donne le vocabulaire complet : alias, pieges de nommage,
prestations, zones par pays, et les taux de remplissage des colonnes.

## Deux colonnes a ne pas confondre, et une a ne jamais sommer

Mesure du 2026-08-28 sur les 4 050 envois de mai a aout 2026 :

- **`cost` est renseigne sur 100 % des lignes et vaut zero sur 100 % d'entre
  elles.** Le sommer rend zero. **Le montant est dans `price`** (rempli a 96,4 %).
- `total_weight`, `total_volume`, `total_linear_meters` : remplis a 63,6 %. Une
  somme ne porte donc que sur deux tiers du perimetre - le dire, ou ne pas la
  citer.
- `created_at` est la date de CREATION, `date` la date de DEPART prevue, souvent
  posterieure et parfois dans le futur. Regrouper par mois sur `date` apres avoir
  filtre sur `created_date_from` fait apparaitre des mois hors perimetre.
  `shiptify_summary` choisit la colonne qui correspond au filtre pose et **nomme
  celle qu'il a utilisee** dans l'intitule du regroupement.

## L'historique large passe par le cache

Le direct plafonne : `SHIPTIFY_MAX_PAGES` arrete la pagination a 6 000 lignes.
Pour douze mois, ou pour reposer la meme question sans reconsommer l'API :

1. **`shiptify_sync`** une fois - il rapatrie une collection dans un SQLite
   local, sans ce plafond. Il ecrit sur le disque : sur demande.
2. **`shiptify_sql`** ensuite, autant de fois qu'on veut, instantanement. Les
   colonnes portent des points, il faut les guillemeter :
   `SELECT "carrier.name", COUNT(*) FROM shipments GROUP BY 1`.
3. **`shiptify_columns`** avant d'ecrire le SQL, pour les noms et les taux de
   remplissage. **`shiptify_tables`** dit ce que le cache contient et **de quand
   il date**.

**Le cache n'est pas l'etat courant.** Ce qui a bouge dans Shiptify depuis la
synchro n'y est pas. Pour le jour meme, ce sont les outils de liste.

## L'ordre qui marche

1. **`shiptify_resolve`** ou **`shiptify_lexique`** si la question porte un nom
   maison.
2. **`shiptify_summary`** si la question est un compte, une somme ou une
   repartition. C'est le cas le plus frequent, et il s'arrete la.
3. **`shiptify_list_paths`** ou **`shiptify_dictionary`** pour les noms exacts
   des filtres et les valeurs permises, avant d'aller sur un chemin sans outil
   dedie. Sauter cette etape, c'est deviner un nom de parametre et se prendre un
   `HTTP 400`.
4. **`shiptify_list_shipments`** avec un perimetre de dates serre, quand la
   question porte vraiment sur des lignes.
5. **`shiptify_export_csv`** ou **`shiptify_export_sql`** uniquement si
   l'utilisateur a demande un fichier - voir la section ci-dessus. Jamais pour te
   rassurer sur un chiffre.

Si quelque chose coince, **`shiptify_doctor`** avant tout : il dit d'ou vient la
cle, si l'API repond, et ce que le serveur a lu comme configuration.

## Les quatre pieges

**Le nombre de lignes rendu n'est pas un volume.** L'API Shiptify ne renvoie
aucun total : ni `total`, ni `X-Total-Count`. Les outils de liste disent
desormais explicitement laquelle des deux situations tu as :

- « **Lecture complete** sur ce perimetre [...] Ce compte est citable » - la
  collection a ete parcourue jusqu'a la derniere page ;
- « **ATTENTION : resultat tronque** » avec la raison (`max_rows`, `max_pages`,
  `budget`). Le compte n'est PAS un total.

**Un chiffre ne se cite que depuis une lecture complete.** Si la lecture est
incomplete, la reponse n'est pas de resserrer au hasard : c'est
`shiptify_summary`, qui parcourt tout et ne rend que l'agregat.

**Un envoi porte 114 colonnes.** Utilise `fields` pour projeter ce qui est
utile, en notation pointee :

```
fields="id,status,carrier.name,address_dest.city,address_dest.zipcode,price"
```

`fields="*"` rend tout, et sature la conversation pour rien.

**Un nom Shiptify n'est pas le nom maison du transporteur.** `Prevote I Meru`
est un transporteur actif cote Shiptify et un prospect messagerie non retenu
cote equipe. Croise avec l'annuaire du contexte d'equipe
(`01_CONTEXTE/PORTEFEUILLE_ET_ZONES.md`) avant de conclure sur le portefeuille.
Meme prudence sur les entites multiples : « Rhenus » designe quatre entites,
« Posten Bring » et « Bring » sont deux prestataires distincts.

**Les dates sont au format `YYYY-MM-DD`**, les mois comptables au format
`YYYY-MM`. `/events` exige un type d'evenement : il n'y a pas de journal global.

## Le resultat d'abord, la methode ensuite

**L'ordre de restitution n'est pas libre.** Une reponse Shiptify se rend dans cet ordre, toujours :

1. **Le resultat.** Le chiffre, le tableau, le graphique. En premier, sans preambule. Pas de
   « je vais regarder », pas d'annonce de ce que tu t'apprêtes a faire, pas de recit des etapes
   que tu as suivies pour y arriver.
2. **La methode, apres.** Une fois le resultat pose : les filtres exactes que tu as appliques,
   le perimetre de dates, le champ sur lequel tu as compte, le nombre de lignes obtenues.
3. **Les risques que tu as identifies.** Ce qui peut rendre le chiffre faux : un champ vide, un
   referentiel incomplet, un libelle saisi en deux langues, une troncature, un statut annule
   inclus ou exclu, un mois en cours. Nomme-les, ne les sous-entends pas.

Ce qui est interdit : commenter ta demarche **avant** d'avoir produit le resultat. Si une piste
s'est revelee fausse en route, ca se dit dans la partie methode, pas en ouverture.

## Citer un chiffre Shiptify

La regle de l'equipe s'applique sans changement : **un chiffre se cite avec son
perimetre et ses hypotheses.** Pas « 104 envois vers l'Espagne », mais « 104
envois AMB vers ES, crees du 26 au 27/08/2026, filtre `created_date_from`,
rendu non tronque ». Chaque outil rappelle en tete la requete exacte qu'il a
jouee : recopie-la.

Et la regle qui ne bouge pas : **on n'invente jamais un chiffre.** Si l'API rend
une liste vide, la reponse est « aucune ligne sur ce perimetre », pas une
estimation.

## La facturation, a verifier avant de s'en servir

`shiptify_list_invoices` et `shiptify_list_invoice_lines` fonctionnent
techniquement, mais la surface facturation s'est revelee **pauvre** a la recette
du 2026-08-27 : une seule facture rendue, datee de 2024, et aucune ligne sur
juin-aout 2026. Soit la facturation Shiptify n'est pas alimentee pour le compte,
soit la cle n'a pas la portee.

**Verifie avec Shiptify avant de fonder un controle de facture dessus**, et dis
au lecteur ce que tu as reellement obtenu plutot que de conclure a l'absence de
facturation.
