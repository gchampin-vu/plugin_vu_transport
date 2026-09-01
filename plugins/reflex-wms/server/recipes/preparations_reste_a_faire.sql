-- Lignes de preparation avec du reste a faire.
--
-- ATTENTION - ECART AVEC LA COMPETENCE D'EQUIPE `reflex-mld`, releve le
-- 2026-08-28 contre le MLD Hardis. La competence filtre les lignes sur
-- P1TSLO et P1TSOR en les lisant comme "top soldee" et "top sortie de stock".
-- Le MLD dit autre chose :
--
--     P1TSLO = Top substitution sur ligne odp
--     P1TSOR = Top service obligatoire sur reference reservation
--     P1TVLP = Top ligne preparation validee        <- celui-ci existe bien
--
-- Le filtre d'origine ecarte donc des lignes substituees, pas des lignes
-- soldees : la population change sans qu'aucune erreur ne soit levee. Cette
-- recette est ecrite sur P1TVLP et sur la comparaison de quantites, qui se
-- suffit a elle-meme. A arbitrer avec Jimmy Mieuzet avant d'en faire un
-- indicateur.
--
-- La comparaison de quantites est la condition la plus sure : P1QAPR est la
-- quantite a preparer, P1QPRE la quantite VALIDEE. P1QPRE < P1QAPR, c'est du
-- reste a faire, quelle que soit la lecture des tops.
--
-- A adapter : le depot.

SELECT
    pe.PECDPO                                AS depot,
    pe.PENANN                                AS millesime,
    pe.PENPRE                                AS num_preparation,
    pe.PERODP                                AS reference_donneur_ordre,
    pe.PECETL                                AS code_etat_livraison,
    et.T0LEPP                                AS libelle_etat,
    pl.P1NLPR                                AS num_ligne,
    pl.P1CART                                AS code_article,
    pl.P1QODP                                AS qte_odp,
    pl.P1QAPR                                AS qte_a_preparer,
    pl.P1QPRE                                AS qte_validee,
    (pl.P1QAPR - pl.P1QPRE)                  AS qte_restante,
    pl.P1TVLP                                AS top_ligne_validee
FROM   reflex.HLPRENP pe  WITH (NOLOCK)
INNER  JOIN reflex.HLPRPLP pl  WITH (NOLOCK)
       ON  pl.P1CACT = pe.PECACT
       AND pl.P1CDPO = pe.PECDPO
       AND pl.P1NANP = pe.PENANN
       AND pl.P1NPRE = pe.PENPRE
LEFT   JOIN reflex.HLETPPP et  WITH (NOLOCK)
       ON  et.T0CEPP = pe.PECETL
WHERE  pe.PECDPO = 'AMB'
  AND  ISNULL(pe.PETSOL, '0') NOT IN ('O', '1', 'Y')   -- preparation non soldee
  AND  ISNULL(pe.PETSOP, '0') NOT IN ('O', '1', 'Y')   -- sortie de stock non effectuee
  AND  ISNULL(pl.P1TVLP, '0') NOT IN ('O', '1', 'Y')   -- ligne non validee
  AND  pl.P1QPRE < pl.P1QAPR                           -- reste a faire
  AND  pe.PEACRE >= 26
ORDER BY pe.PENPRE, pl.P1NLPR
