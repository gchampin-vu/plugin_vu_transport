---
updated: 2026-09-02
updated_by: Guillaume_Champin
type: process
---

# Plugin `taux-tva` — connecteur MCP en lecture seule

Serveur MCP qui donne à Claude Code un accès **en lecture** aux taux de TVA de
**45 juridictions européennes** : taux normal, taux réduits, super-réduit, taux
parking, exemptions, devise — et, pour les États membres, les **catégories de
biens et services**, les **codes de nomenclature douanière**, les
**commentaires officiels** et la **date d'effet** de chaque taux.

## Ce qui a changé en v2.0, et pourquoi

La v1.0 appelait `api.vatcomply.com`, un agrégateur gratuit qui couvrait les 27
États membres. Elle butait sur une limite qui n'était pas contournable : **la
Suisse, la Norvège et le Royaume-Uni — trois pays où le groupe vend — n'y
étaient pas**, et ne pouvaient pas y être.

La v2.0 remplace cet intermédiaire par **deux sources**, choisies pour ce que
chacune sait faire :

| | **TEDB** | **vatnode** |
| --- | --- | --- |
| Qui | Commission européenne, alimentée par les États membres | dépôt GitHub, licence MIT |
| Autorité | **officielle** | **tenue à la main** |
| Couvre | les 27 + **XI** (Irlande du Nord) | 17 juridictions hors Union, **et les devises de tout le monde** |
| Protocole | SOAP | JSON sur CDN |
| Catégories de biens | **87, avec libellé** | aucune |
| Codes de nomenclature (CN) | **oui** | non |
| Commentaires officiels | **oui** | non |
| Date d'effet du taux | **oui** | non |
| Devise | **non** | oui |

Le gain n'est donc pas seulement la couverture. En passant par la source
officielle plutôt que par un agrégateur, le connecteur **gagne** les catégories
(87 contre 27), les codes CN, les commentaires des administrations et les dates
d'effet. Aller chercher CH, GB et NO n'a rien coûté au reste — c'est l'inverse
qui s'est produit.

## La provenance est une colonne, pas une note de bas de page

C'est le point de conception de cette version. Les deux moitiés du périmètre
n'ont **pas la même autorité** :

- pour les 27 et XI, TEDB est une source officielle ;
- pour les 17 autres, vatnode annonce lui-même des taux *« maintained manually
  from national sources »* — la saisie d'un mainteneur, pas un flux officiel.

Un tableau qui mélange les deux sans le dire laisse citer un taux suisse avec
l'assurance d'un taux français. Chaque ligne porte donc sa `provenance`
(`TEDB` ou `vatnode (main)`), et tout rendu contenant une ligne tenue à la main
affiche une réserve nommant **l'administration où vérifier** :

```
PROVENANCE - CH, GB ne viennent PAS de la base officielle de la Commission :
ce sont des taux tenus a la main dans un jeu de donnees public. Bons pour
comparer, a verifier avant de facturer, aupres de : CH (Administration
federale des contributions (AFC)), GB (HMRC).
```

**Ce n'est pas une référence fiscale.** Dès qu'un taux va servir à facturer, à
déclarer ou à paramétrer un ERP, la référence est l'administration fiscale du
pays. Chaque rendu porte cette phrase en pied : c'est volontaire, et ça ne se
retire pas. C'est la règle n° 3 du `CLAUDE.md` de la bibliothèque — *on
n'invente jamais un chiffre* — appliquée à des sources externes.

## Les quatre pièges traités dans le code

Ce sont ceux qui produisent un chiffre faux **sans lever d'erreur**.

**1. La Grèce.** TEDB la code `EL`, vatnode la code `GR`. Un rapprochement fait
sans traduction produit deux pays là où il y en a un, ou en perd un en silence.
Le connecteur traduit vers `EL`, la convention de l'Union, et le rappelle dans
chaque rendu.

**2. « UE » ne veut pas dire « tout ».** Depuis que le périmètre dépasse
l'Union, confondre les deux ferait apparaître la Suisse dans une réponse sur les
États membres. `UE` désigne les **27** ; `Europe` ou `tous`, les **45**.

**3. XI n'est pas GB.** L'Irlande du Nord reste dans le champ TVA de l'Union
pour les biens. TEDB la sert comme une juridiction à part entière, et une
livraison à Belfast ne se traite pas comme une livraison en Grande-Bretagne. Les
deux sont deux lignes distinctes. `Belfast` est un alias reconnu.

**4. La devise n'est pas une constante.** La Bulgarie est passée à l'euro le
1ᵉʳ janvier 2026. Graver les devises dans le référentiel aurait fabriqué un fait
faux au premier changement suivant : elles viennent de vatnode, daté et vérifié
chaque jour.

## Le seul « non couvert » qui reste

Les **territoires à régime particulier** : Canaries (IGIC), Ceuta-Melilla
(IPSI), Madère, Açores, Corse, DOM, Åland, Büsingen-Helgoland,
Livigno-Campione, Mont Athos.

Aucune des deux sources ne les distingue — elles rendent **un** taux par
juridiction. C'est le cas qui produit le plus d'erreurs de facturation,
justement parce qu'un taux national existe et se cite par réflexe. Un
territoire demandé ressort en tête, nommé, avec la conséquence explicite :

```
NON COUVERT - CANARIES : territoire a regime particulier, rattache a ES.
Hors champ de la TVA de l'Union. Les Canaries appliquent l'IGIC, un impot
local distinct. Le taux espagnol ne s'y applique PAS.
```

## Ce que ça sait faire

| Outil | Ce qu'il rend |
| --- | --- |
| `tva_taux` | **l'outil principal** : les taux des juridictions demandées, en CSV point-virgule, avec la colonne `provenance`. Accepte codes, noms français, `UE`, `Europe`, `VU` |
| `tva_detail` | tout ce que les sources disent d'**une** juridiction : catégories, **codes CN**, commentaires officiels, exemptions, date d'effet, territoires rattachés |
| `tva_categories` | les catégories que TEDB sait qualifier, et combien de pays en déclarent une. **À lire avant** de passer `categorie` à `tva_taux` |
| `tva_pays` | ce qui est couvert, **avec quelle autorité**, et ce qu'aucune source ne couvre. Répond hors ligne |
| `tva_changements` | ce qui a bougé entre le relevé courant et le précédent |
| `tva_rafraichir` | force un appel aux sources sans attendre l'expiration du cache |
| `tva_export_csv` | écrit un CSV, **sur demande explicite**. Une ligne par juridiction, ou une ligne par juridiction × catégorie avec ses codes CN |
| `tva_doctor` | interpréteur, chemins, configuration, cache, et **un appel de vérification par source, séparément** — parce qu'une panne de vatnode et une panne de TEDB ne se réparent pas pareil |

Et une commande : **`/tva-export`**.

Les outils sont annotés `readOnlyHint`. Ce qui *garantit* la lecture seule reste
le code : deux chemins appelés, l'un en POST SOAP sans autre paramètre que des
codes pays et une date, l'autre en GET sur un fichier statique.

## Les codes de nomenclature douanière — ce que TEDB apporte de neuf

C'est la trouvaille de cette version. TEDB rattache chaque taux réduit non
seulement à une catégorie (`FOODSTUFFS`, `TRANSPORT_PASSENGERS`) mais aux
**codes de la nomenclature combinée** concernés, avec leur libellé douanier :

```
FOODSTUFFS : 5.5 | 10   [CN 0401 0402 0403 0404 0405 0406 +12]
```

Ces codes sont ce qui relie une catégorie fiscale à un **produit réel du
catalogue**. Ils partent dans l'export sous forme `categories`, colonne
`codes_cn`.

## Aucune dépendance nouvelle

Le SOAP est parlé avec `httpx` et `xml.etree`, tous deux déjà là — **ni `zeep`,
ni `lxml`, ni générateur de client**. `requirements.txt` est inchangé :
`mcp` et `httpx`.

L'enveloppe SOAP fait douze lignes et la réponse se dépouille avec la
bibliothèque standard. Ajouter `zeep` aurait apporté une dépendance lourde, un
parseur de WSDL au démarrage et une compilation de `lxml` sur les postes — pour
construire un document XML dont on connaît la forme exacte.

## Installation

```
/plugin install taux-tva@vu-transport
```

**Il n'y a rien à mettre en service.** Aucune clé d'API : les deux sources sont
publiques et anonymes. Le premier démarrage crée un environnement Python sous
`~/.tva-mcp/venv` et y installe `mcp` et `httpx` — une fois, automatiquement,
par `server/bootstrap.py`.

Pour vérifier :

```bash
python server/bootstrap.py doctor
```

Il n'y a **pas d'`install.ps1`** ici, à la différence des autres connecteurs de
l'équipe : le `bootstrap.py` est le chemin nominal, et un chemin réservé à
Windows rendrait le plugin non installable sur Mac
(voir [`../../../04_mcp/README.md`](../../../04_mcp/README.md)).

## Configuration

Tout est optionnel — le connecteur démarre sans rien.

| Réglage | Ce qu'il fait | Où il vit |
| --- | --- | --- |
| `TVA_PAYS_VU` | les juridictions interrogées quand la question n'en nomme aucune, et ce que désigne `VU` | **fichier d'équipe** — c'est une décision, pas un réglage de poste |
| `TVA_BASE_URL` | endpoint SOAP de TEDB. **Garde le slash final** : c'est l'adresse à laquelle on POSTe | équipe ou poste |
| `TVA_VATNODE_URL` | jeu de données hors Union et devises. **Le renseigner supprime le repli** CDN → dépôt brut | équipe ou poste |
| `TVA_TTL_HOURS` | fraîcheur du cache, en heures (défaut 24) | équipe ou poste |
| `TVA_TIMEOUT_S` | délai d'attente d'un appel (défaut 30) | équipe ou poste |
| `TVA_EXPORT_DIR` | où atterrissent les CSV | **poste seulement** |
| `TVA_CACHE_FILE` | où est écrit le cache | **poste seulement** |

Priorité : configuration du plugin > `~/.tva-mcp/tva.env` > fichier d'équipe
`08_ENGINE/04_mcp/00_config/tva.shared.env` > défaut du serveur.

Modèles : `server/tva.env.example` et `server/tva.shared.env.example`.

### `TVA_PAYS_VU` n'est pas renseigné, et c'est délibéré

La liste des pays où l'équipe travaille **n'est pas écrite dans le code**. Être
présent sur un marché et y être immatriculé à la TVA sont deux choses
différentes : déduire ce périmètre de la liste des boutiques du groupe aurait
été un fait faux gravé dans du code.

Tant qu'il n'est pas posé, une question sans pays rend les 45 juridictions — et
la réserve de provenance qui va avec.

## Le cache, et la dégradation asymétrique

Le connecteur garde le dernier relevé dans `~/.tva-mcp/vat_rates.json`, et le
relevé précédent à côté. La réponse dit toujours son origine et son âge, et un
cache de plus de 30 jours est annoncé comme **périmé**, en majuscules.

Les deux sources ne se dégradent pas de la même façon, et c'est voulu :

- **TEDB ne se dégrade pas.** S'il ne répond pas, le connecteur ne rend rien.
  Perdre les 27 pour ne servir que les juridictions tenues à la main serait
  servir la moins bonne moitié en silence.
- **vatnode peut manquer.** On perd alors les devises et le hors-Union, et le
  rendu le dit — *« ce n'est PAS "elles n'ont pas de TVA" »*.

Sans cache **et** sans réseau, le serveur ne rend rien et le dit. Il n'affiche
pas un taux qu'il n'a pas lu.

## Contrôles hors ligne

```bash
python server/test_offline.py
```

**181 contrôles, aucun appel réseau.** Le dépouillement des deux sources est
éprouvé sur des échantillons de forme réelle — un dépouillement faux ne lève
aucune erreur, il rend un taux plausible et faux. Ce qui est vérifié : la
traduction `GR` → `EL`, le regroupement d'une catégorie portant deux taux, la
distinction exemption / taux zéro, l'amputation du décalage horaire dans les
dates d'effet, la sémantique `UE` ≠ `tous`, la réserve de provenance posée
**une seule fois** malgré la récursion du périmètre d'équipe, la dégradation
asymétrique des deux sources, le fait qu'un réglage affiché par le diagnostic
soit réellement branché — et les conventions de portabilité Mac / Windows.

## État

**v2.0.0, le 2026-09-02.** 181 contrôles hors ligne au vert, et les deux
sources confrontées à la production le même jour : TEDB rend 28 juridictions
avec catégories, vatnode 17 juridictions et 45 devises, `tva_doctor` au vert sur
les deux appels séparément.

Ce qui reste ouvert :

- **`TVA_PAYS_VU`** — à établir avec qui connaît le périmètre
  d'immatriculation, puis à déposer dans le fichier d'équipe.
- **Les taux CH, NO et GB restent tenus à la main.** C'est le point faible
  assumé de cette version : les trois pays qui ont motivé le chantier sont
  ceux dont la provenance est la plus faible. Les brancher sur leurs sources
  officielles (AFC, Skatteetaten, HMRC) est un chantier par pays, pas une
  ligne de configuration. Le connecteur le dit sur chaque ligne en attendant.
- **La requête TEDB par code CN ou CPA** est possible — le service accepte
  `cnCodes` et `cpaCodes` en filtre — et n'est pas exposée. Ce serait la vraie
  réponse à « quel taux sur ce produit du catalogue ». À arbitrer.
- **La validation de numéro de TVA** (VIES) reste volontairement hors
  périmètre : elle enverrait un identifiant de tiers à un service externe.
  C'est une décision à prendre, pas un oubli.
