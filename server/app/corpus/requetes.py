"""Ce qu'une requête devient avant d'atteindre l'index : bornes de fréquence et formes de nombre.

Les deux fonctions ci-dessous ne choisissent **aucun** bloc : elles élargissent une *requête*, et
c'est l'index qui classe ensuite. Elles vivaient dans `steps/retrouver.py`, seule étape qui les
employait ; l'amendement AD-1 du 03/09/2026 donne au modèle un outil `chercher` servi par une
**autre** étape (*naviguer*), et une étape n'importe jamais une autre étape (table des couches).
Elles vivent donc ici, une fois, à la couche qui connaît déjà le document et son vocabulaire.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from server.app.corpus.index import Index, words
from server.app.corpus.text import forme_de_nombre, normalize

# La liste fermée des mots-outils du français employée par les deux usages : le score de
# chevauchement de *retrouver* et les variantes de nombre ci-dessous. Aucun vocabulaire métier.
MOTS_OUTILS_LIMITES = frozenset({
    "a", "au", "aux", "avec", "ce", "ces", "d", "dans", "de", "des", "du", "elle", "en",
    "est", "et", "il", "ils", "l", "la", "le", "les", "leur", "leurs", "lui", "ne", "ni", "on", "ou",
    "par", "pas", "pour", "que", "qui", "sa", "se", "ses", "son", "sur", "un", "une",
})


def part_du_mot_borne(index: Index | None, doc_id: str | None, *,
                      part_max: float) -> Callable[[str], float] | None:
    """La fréquence documentaire du document servi, **quand elle mesure quelque chose**.

    Correctif du tour 5 (C8). `Dictionnaire` ne connaît aucun index — c'est bien ainsi : il décrit
    un vocabulaire, pas un corpus chargé. La borne de fréquence lui est donc **injectée** par
    l'appelant qui, lui, sait quel document est interrogé. Une recherche sans document (`doc_id`
    nul) ne borne rien : aucune fréquence documentaire n'a de sens sur plusieurs documents à la
    fois, exactement comme `utilisable_pour(None)` ne reconnaît aucun dictionnaire.

    **Et un seuil en part n'est une mesure que si le document a de quoi la porter.** La plus fine
    part qu'un document de `N` blocs sache exprimer vaut `1/N` : tant que `part_max × N` n'atteint
    pas un bloc entier, « dépasser le seuil » et « figurer une seule fois dans le document » sont la
    même chose, et la borne ne dirait plus « cette forme nomme le sujet » mais « cette forme
    existe ». Elle s'abstient donc, et c'est le bon sens de l'abstention : une variante de
    dictionnaire est une équivalence **écrite** (AD-5), pas une forme dérivée par le code comme les
    variantes de nombre ; la refuser sans mesure désarmerait en silence le rappel qu'AD-5 apporte.
    Sur le contrat servi — 1 400 blocs, seuil 1 % — la borne parle à partir de 14 blocs.
    """
    if index is None or doc_id is None or doc_id not in index.corpus.documents:
        return None
    blocs = len(index.corpus.documents[doc_id].blocks)
    if part_max * blocs < 1:
        return None
    return lambda mot: index.part_des_blocs(mot, doc_id=doc_id)


def variantes_de_nombre(formes: Iterable[str], *, index: Index, doc_id: str,
                        part_max: float) -> list[str]:
    """Les formes de nombre d'un libellé qui **désignent une clause**, jamais celles qui nomment le sujet.

    Correctif du tour 3 (R2). Un libellé de facette est une phrase et l'index est littéral : sur les
    six libellés des trois runs A16, **aucun** n'atteignait `full_matches > 0`, avec ou sans sa forme
    plurielle de phrase. La variante utile est donc au niveau du **mot** — mais seulement pour les
    mots que le document porte rarement : une variante d'un mot fréquent est pleinement couverte par
    des dizaines de blocs, ce qui rendrait la garde de R1 inerte et renverrait le rang 0 au bruit.

    Deux filtres, tous deux déjà dans le dépôt : les mots-outils du français
    (`MOTS_OUTILS_LIMITES`) et la part des blocs que le document consacre au mot
    (`Index.part_des_blocs`, la fréquence documentaire qui pondère déjà les couvertures partielles).
    Aucun vocabulaire métier, aucune lecture du texte des clauses.

    La forme **de phrase** est conservée en tête : elle ne prouve rien à elle seule (aucun bloc ne
    couvre une phrase entière) mais elle remonte le bon bloc au score partiel, et c'est elle qui
    porte l'ordre quand plusieurs mots rares sont en jeu.

    Les formes sont celles du **groupe entier** — le canonique et les variantes que le dictionnaire
    lui a déjà données —, jamais du seul libellé reçu : deux libellés que le dictionnaire ramène au
    même groupe doivent rester **une** requête, et cesseraient de l'être si chacun dérivait ses
    variantes de son propre texte.

    Mesuré le 03/09/2026 sur le prototype de navigation : sans elle, `chercher(['fumée'])` ne
    ramène pas « Les fumées et les suies » et le modèle conclut que le contrat est muet. Une
    recherche qui manque le seul bloc décisif n'est pas une proposition, c'est un contresens.
    """
    variantes: list[str] = []
    for forme_source in dict.fromkeys(formes):
        mots = words(normalize(forme_source))
        if not mots:
            continue
        phrase = " ".join(forme_de_nombre(mot) or mot for mot in mots)
        if phrase != " ".join(mots) and phrase not in variantes:
            variantes.append(phrase)
        for mot in mots:
            if mot in MOTS_OUTILS_LIMITES:
                continue
            autre = forme_de_nombre(mot)
            if autre is None or autre in variantes:
                continue
            try:
                if index.part_des_blocs(autre, doc_id=doc_id) <= part_max:
                    variantes.append(autre)
            except KeyError:  # pragma: no cover — document servi, garanti par l'appelant
                continue
    return variantes
