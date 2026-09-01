-- Ou est alimente un numero de conteneur : la passe prealable, a faire UNE fois.
--
-- Le numero de conteneur n'a pas d'emplacement unique et connu d'avance dans
-- Reflex. Figer une jointure dessus sans avoir constate ou il se trouve, c'est
-- se preparer un resultat vide ou faux. Cette requete regarde les trois
-- porteurs candidats d'un coup, sur une fenetre courte, et dit lequel est
-- reellement alimente chez Vente-unique.
--
-- Elle est volontairement bornee : UNION ALL de trois lectures filtrees, avec
-- un TOP sur chacune. On cherche a savoir OU, pas a tout lister.
--
-- A adapter : le numero de conteneur, le depot.

SELECT TOP 20
    'HLRCPEP.RPRRPR'        AS porteur,
    rp.RPCDPO               AS depot,
    rp.RPNRPR               AS numero,
    rp.RPRRPR               AS valeur
FROM   reflex.HLRCPEP rp  WITH (NOLOCK)
WHERE  rp.RPCDPO = 'AMB'
  AND  rp.RPRRPR LIKE '%2604240076%'

UNION ALL

SELECT TOP 20
    'HLRECPP.RERREC',
    re.RECDPO,
    re.RENRPR,
    re.RERREC
FROM   reflex.HLRECPP re  WITH (NOLOCK)
WHERE  re.RECDPO = 'AMB'
  AND  re.RERREC LIKE '%2604240076%'

UNION ALL

SELECT TOP 20
    'HLAEXEP.VERLIX',
    ve.VECDPO,
    ve.VENAEX,
    ve.VERLIX
FROM   reflex.HLAEXEP ve  WITH (NOLOCK)
WHERE  ve.VECDPO = 'AMB'
  AND  ve.VERLIX LIKE '%2604240076%'
