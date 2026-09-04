"""AD-6 — Verdict « au regard des conditions générales seules », décidé par table.

Ce module est **du code pur** : il ne connaît ni le modèle, ni le corpus, ni les étapes. AD-6 confie
au modèle l'extraction de *valeurs typées* et au code la décision — le découpage d'exécution est donc
littéral :

(a) `applicable_de_claim()` dérive `ClaimStatus.applicable ∈ {oui, non, humain}` d'une claim retenue,
    à partir du typage des blocs qu'elle cite (`Block.kind`, seule source de typage) et des quatre
    champs typés rendus par l'unique appel `reason` de *vérifier* ;
(b) `decider()` applique la table exclusive d'AD-6 aux claims **affichées** et compose le `Verdict`
    — sa valeur, sa raison, le paquet manquant, les questions à poser, les points à escalader.

Rien de ce que le modèle rend n'est une décision : il dit si le fait exigé par la clause est présent,
si une option ou des conditions particulières conditionnent la clause, et quel fait lui manque. Le
seul texte du modèle qui traverse jusqu'à l'utilisateur est le libellé d'un `fait_manquant`, borné et
dédupliqué par l'appelant, et il n'entre jamais dans `Answer.texte`.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

from pydantic import Field

from .document import DomainModel

VerdictValue = Literal["couvert", "non_couvert", "sous_conditions", "ne_tranche_pas"]
Applicable = Literal["oui", "non", "humain"]
ApplicableReason = Literal["hors_portee", "faits_contraires"]

# AD-6 : les `Block.kind` qui portent une décision. Une claim n'en couvre qu'**un seul** (les clauses
# hétérogènes sont éclatées en claims distinctes) ; un bloc `para`, `definition` ou `table` cité à
# côté reste le contexte de la clause, et n'a pas d'applicabilité.
KINDS_DECISIONNELS: frozenset[str] = frozenset({"garantie", "exclusion", "condition", "franchise"})
# Les deux seuls kinds qui peuvent porter un verdict autre que `ne_tranche_pas` : la table d'AD-6 ne
# tranche que sur une garantie ou une exclusion ; une condition ou une franchise ouvre le verdict,
# elle ne le fonde jamais.
KINDS_FONDATEURS: frozenset[str] = frozenset({"garantie", "exclusion"})

# Portée affichée, invariable : le verdict ne vaut **jamais** décision d'indemnisation (AD-6).
PORTEE = "au regard des conditions générales seules"


class MissingPackage(DomainModel):
    conditions_particulieres: bool = True
    options_souscrites: bool = True
    avenants: bool = True
    date_effet: bool = True
    faits: list[str] = Field(default_factory=list)


class Verdict(DomainModel):
    value: VerdictValue
    reason: str
    missing: MissingPackage = Field(default_factory=MissingPackage)
    ask_client: list[str] = Field(default_factory=list)
    escalate: list[str] = Field(default_factory=list)


class ChampsApplicabilite(DomainModel):
    """Les quatre valeurs typées d'AD-4, telles que le code les reçoit — jamais un verdict.

    - `fait_requis_present` : le fait que la clause exige est-il **établi** par les faits déclarés ?
    - `option_requise` / `cp_requise` : la clause ne joue-t-elle que si une option / les conditions
      particulières le prévoient ?
    - `fait_manquant` : le fait que la clause exige et que les faits déclarés ne disent **pas**.

    Deux situations que `fait_requis_present=false` ne distingue pas seul, et que la table doit
    séparer (D1 de la spec 1.8) : le fait est **connu et contraire** (la clause ne s'applique pas —
    c'est ce qui écarte l'exclusion p. 46, qui vise le bâtiment quand le sinistre porte sur le
    contenu) ou le fait est **inconnu** (personne ne peut trancher). C'est `fait_manquant` qui les
    sépare : renseigné, le fait est inconnu ; vide, il est connu et contraire.

    **`qualites_exigees` / `qualites_non_etablies` (revue Codex 1.8, B3).** `fait_requis_present` est
    un booléen : rien dans la trace ne dit *ce que* la clause exigeait, et un run réel a rendu la
    qualité « subite » pour établie sans que rien ne puisse le contredire. AD-6 prescrit le remède
    littéralement — « le modèle n'effectue aucun calcul : il extrait des valeurs typées, le code
    compare ». Le modèle **énumère** donc les qualités que la clause subordonne à l'événement, au bien
    ou à l'assuré (`qualites_exigees`) puis celles que les faits déclarés établissent *dans ces
    termes* ; le **code** fait la différence, et toute qualité exigée non établie rend `humain` et
    devient une question bornée. Un booléen ne se vérifie pas ; une liste qui doit se recouvrir elle-
    même, si.

    **La corroboration par les faits (revue Codex 1.8, B3, tour 2).** Deux listes que le modèle remplit
    seul restent une auto-déclaration : recopier `qualites_exigees` dans `qualites_etablies` annulait
    le contrôle. *Vérifier* n'accepte donc une qualité pour établie que si le modèle produit avec elle
    un **fragment des faits déclarés**, relu mot pour mot dans les faits soumis — la mécanique d'AD-3
    appliquée aux faits. Et un modèle qui **n'énumère pas** (listes absentes) rend son jeu de champs
    inexploitable : la claim vaut `humain`, jamais `oui` par défaut.
    """

    fait_requis_present: bool
    option_requise: bool = False
    cp_requise: bool = False
    fait_manquant: str | None = None
    # Ce que la clause exige, tel que le modèle l'a nommé — affiché nulle part, il sert la trace.
    qualites_exigees: list[str] = Field(default_factory=list)
    # `qualites_exigees` moins celles que les faits déclarés établissent : la différence est faite par
    # l'appelant (*vérifier*), qui seul borne et normalise les libellés du modèle.
    qualites_non_etablies: list[str] = Field(default_factory=list)


class ConditionDeSection(DomainModel):
    """La condition d'applicabilité que le contrat écrit **en tête** de la section d'une garantie.

    Story 5.7 (L1e). Le contrat AXA ouvre « 3.1.4 Dégâts des eaux » par `p37:11` — « Les présentes
    conditions spéciales sont applicables si les conditions particulières mentionnent que la garantie
    “dégâts des eaux” est souscrite. » Tant que ce bloc n'était lu que s'il était **cité**, le verdict
    de la même question basculait entre `sous_conditions` et `couvert` selon que le modèle avait ou
    non retenu cette clause : la garantie était servie comme acquise alors que le contrat dit
    lui-même qu'elle ne l'est qu'à une condition que personne n'a vérifiée.

    Elle est donc lue sur l'**arbre** (`Document.condition_de_section_applicable`), jamais sur les
    claims, et portée par chaque `ClauseCitee` de la section. `texte` est le bloc relu dans le corpus
    (AD-3) : c'est lui que la raison du verdict cite, pour que l'utilisateur voie *pourquoi* le
    verdict est ouvert. `renvoie_cp` est un **témoin** lexical, pas la règle : il ne décide pas
    qu'une section est conditionnée (la structure le fait), il décide seulement des mots de la
    question posée au client — « vos conditions particulières mentionnent-elles … » n'a de sens que
    si la condition y renvoie.
    """

    block_id: str
    titre: str  # titre du nœud de la section conditionnée, tel qu'il est écrit (« 3.1.4 Dégâts des eaux »)
    texte: str  # `Block.text`, relu dans le corpus comme le `kind` et la portée
    renvoie_cp: bool = False


class ClauseCitee(DomainModel):
    """Un bloc de kind décisionnel cité par une claim, réduit à ce dont la table a besoin.

    Construit par *vérifier* depuis le bloc **relu dans le corpus** : le modèle ne produit ni `kind`
    (AD-6 : « `Block.kind` (ingestion) est la seule source de typage ; *rédiger* ne produit pas de
    `kind` »), ni portée, ni socle.
    """

    block_id: str
    kind: str
    kind_confirmed: bool = False
    # `Document.scope_nodes(block_id)` — l'unique calcul de « la portée couvre le cas » (AD-2).
    # Vide = la clause n'a pas de portée déclarée.
    portee: set[str] = Field(default_factory=set)
    node_id: str = ""  # `Document.node_of(block_id)`
    socle: bool = False  # `Document.node_scope_kind(node_id) == "commun"` (AD-6, règle 3)
    # Revue Codex 1.8 (B3, tour 3) : les qualificatifs que **le texte de la clause** emploie
    # (« soudain », « subite », « intentionnellement »…), relus dans le corpus comme le `kind`. C'est
    # la seule source d'« la clause exige une qualité » qui ne vienne pas du modèle : sans elle,
    # `qualites_exigees: []` sur une clause qui exige un événement soudain passait pour « aucune
    # qualité exigée ». Orthographe d'origine — ces mots sont affichés dans les questions au client.
    qualificatifs: list[str] = Field(default_factory=list)
    # Story 5.6 (T18), même idiome que `qualificatifs` : les renvois contractuels que **le texte de la
    # clause** écrit (« dans la limite prévue dans vos conditions particulières », « si le pack … est
    # souscrit »), relus dans le corpus. Ce sont les racines du lexique de `steps.verifier`, pas les
    # mots du texte : elles ne sont jamais affichées, elles ne servent qu'à savoir laquelle des deux
    # pièces du dossier la clause subordonne (`steps.verifier.RENVOIS_CP` / `RENVOIS_OPTION`).
    renvois: list[str] = Field(default_factory=list)
    # Story 5.6 (T19), troisième lecture du même texte : les tournures par lesquelles la clause
    # subordonne son effet à une qualité de la **personne** (« ou ceux dont vous avez la garde »,
    # « incomber aux assurés »). Racines du lexique `steps.verifier.QUALITES_DE_PERSONNE`, jamais
    # affichées telles quelles — c'est lui qui porte le libellé rendu au client.
    qualites_personne: list[str] = Field(default_factory=list)
    # Story 5.7 (L1e) : la condition d'applicabilité de la section, lue sur l'arbre du document et non
    # sur les claims. `None` quand la section de la clause n'en porte pas — la garantie est alors
    # inconditionnelle au regard de la structure du contrat.
    condition_section: ConditionDeSection | None = None


class ClaimJugee(DomainModel):
    """Une claim retenue, vue par la table AD-6 : ses clauses, ses champs typés, ses défauts.

    `retenue=False` la laisse dans le calcul de l'applicabilité (le front l'affiche avec son statut)
    mais la sort de la table : AD-6 ne compte que les claims `retrouvee ∧ pertinente`, et D4 de la
    spec 1.8 restreint encore aux claims **affichées** — un verdict adossé à une clause que
    l'utilisateur ne voit pas contredirait « rien d'affiché sans preuve ».
    """

    claim_id: str
    clauses: list[ClauseCitee] = Field(default_factory=list)
    champs: ChampsApplicabilite | None = None
    retenue: bool = True
    renvoi_ouvert: bool = False  # `Block.unresolved_refs` sur l'un des blocs cités
    contredit: bool = False  # `Block.relation.contredit` vise un bloc cité par une autre claim retenue

    @property
    def kind(self) -> str | None:
        """Le kind décisionnel de la claim — un seul, garanti par le contrôle « une clause par
        affirmation » de *vérifier* (D6) ; `None` si elle ne cite aucune clause décisionnelle."""
        return self.clauses[0].kind if self.clauses else None


def applicable_de_claim(claim: ClaimJugee, *, noeuds_du_cas: set[str] | None = None) -> Applicable | None:
    """Découpage (a) d'AD-6 : `applicable` est **dérivé**, jamais rendu par le modèle.

    Ordre de dérivation (D1 de la spec 1.8), du plus prudent au plus engageant :

    1. aucune clause décisionnelle citée ⇒ `None` — une définition ou un paragraphe n'a pas
       d'applicabilité, et lui en prêter une ferait entrer dans la table une claim qui n'y a rien à
       faire ;
    2. un bloc cité sans `kind` confirmé ⇒ `humain` — AD-6, littéralement : « une claim décisionnelle
       dont le bloc n'a pas de `kind` confirmé est traitée `applicable="humain"` » ;
    3. une clause décisionnelle **sans portée** ⇒ `humain` — la table compare des portées (règle 1) et
       lit le socle sur le nœud (règle 3) ; une clause dont on ignore où elle s'applique ne peut être
       ni retenue ni écartée par du code ;
    4. champs typés non rendus ⇒ `humain` — jamais devinés (AC de la story) ;
    5. fait exigé **connu et contraire** (`fait_requis_present=false`, aucun `fait_manquant`) ⇒ `non`,
       **pour une garantie ou une exclusion seulement** (voir ci-dessous) ;
    6. option, conditions particulières, fait **inconnu**, ou **qualité exigée par la clause que les
       faits déclarés n'établissent pas** ⇒ `humain` ;
    7. sinon ⇒ `oui`.

    **(6) et les qualités exigées (revue Codex 1.8, B3).** `fait_requis_present=true` est une réponse
    du modèle que rien ne corroborait : c'est le seul chemin vers `oui`, donc vers `couvert`. La
    différence `qualites_exigees − qualites_etablies`, calculée par *vérifier*, le corrobore en code :
    une clause qui exige « le caractère subit de l'action de la chaleur » et des faits qui ne le disent
    pas donnent `humain`, quoi que vaille le booléen. C'est l'AC de la story lu à la lettre — « humain
    dès qu'une option, une CP ou **un fait manque** » —, et cela vaut *aussi* quand le modèle s'est
    contredit en cochant `fait_requis_present` après avoir nommé ce qu'il n'a pas trouvé.

    L'ordre compte : (5) précède (6) parce qu'une clause qui ne s'applique pas au cas rend sans objet
    l'option dont elle dépendrait par ailleurs.

    **Pourquoi (5) est fermé à `condition` et `franchise` (revue 1.8).** Sur une garantie ou une
    exclusion, « le fait exigé est connu et contraire » se lit sans ambiguïté : la clause ne vise pas
    ce cas (l'exclusion de la page 46 vise le bâtiment des extensions, le sinistre porte sur le
    contenu du domicile). Sur une **condition** — « le bien doit être occupé de manière permanente » —
    ou une **franchise**, le même jeu de champs ne distingue pas « cette condition ne concerne pas ce
    cas » de « cette condition n'est **pas remplie** ». La seconde lecture est la plus fréquente, et
    la traiter comme `non` sortirait la clause de la table : la règle (2) ne verrait plus de condition
    ouverte, et un `couvert` sortirait alors qu'une condition citée est explicitement en défaut. Une
    condition ou une franchise dont le fait exigé n'est pas établi vaut donc `humain`, quel que soit
    `fait_manquant` — politique conservatrice, comme la règle (2) elle-même.
    """
    if not claim.clauses:
        return None
    if any(not c.kind_confirmed for c in claim.clauses):
        return "humain"
    if any(not c.portee for c in claim.clauses):
        return "humain"
    # Une exclusion dont la portée déclarée ne couvre aucun nœud du cas est inapplicable par
    # construction, indépendamment du jugement du modèle sur les faits. Une portée absente a déjà
    # rendu `humain` ci-dessus ; un cas sans nœud prouvé ne permet pas non plus cette conclusion.
    if (claim.kind == "exclusion" and noeuds_du_cas
            and all(clause.portee.isdisjoint(noeuds_du_cas) for clause in claim.clauses)):
        return "non"
    champs = claim.champs
    if champs is None:
        return "humain"
    if not champs.fait_requis_present:
        fondatrice = claim.kind in KINDS_FONDATEURS
        if fondatrice and not (champs.fait_manquant or "").strip():
            return "non"
        return "humain"
    if (champs.option_requise or champs.cp_requise or (champs.fait_manquant or "").strip()
            or champs.qualites_non_etablies):
        return "humain"
    return "oui"


# Revue Codex 1.8 (B3, tour 3). Les qualificatifs par lesquels une clause d'assurance **subordonne**
# son effet à une qualité de l'événement, du bien ou de l'assuré. Lexique volontairement court et
# fermé : il ne sert pas à comprendre la clause, seulement à savoir que le modèle avait quelque chose
# à énumérer. Un mot du texte qui commence par l'un d'eux le porte (« soudaine », « subitement »,
# « intentionnellement »). Ce qui n'y figure pas ne déclenche rien — le contrôle n'ajoute jamais une
# qualité que le texte de la clause n'écrit pas.
#
# Il vit dans le domaine (story 5.6, T8) parce que **deux** appelants l'emploient et doivent
# l'employer à l'identique : *vérifier*, qui relit le texte de la clause, et le verdict lui-même, qui
# dédoublonne par ces racines ce qu'il demande au client. Deux copies auraient fini par diverger.
QUALIFICATIFS: frozenset[str] = frozenset({
    "soudain", "subit", "brusque", "instantane", "accidentel", "fortuit", "imprevisible", "imprevu",
    "involontaire", "intentionnel", "immediat", "direct", "permanent", "exceptionnel", "violent",
    "anormal", "malveillant", "effraction"})


def _mots_qualifiants(texte: str) -> dict[str, str]:
    """Les qualificatifs du lexique employés par un texte : `racine du lexique → mot du texte`.

    Le mot est rendu dans son **orthographe d'origine** (première occurrence) : il finit dans une
    question posée au client, où « immédiat » se lit mieux que sa forme normalisée.

    Pas de `normalize()` de `corpus` — la couche `domain` n'importe que la stdlib et pydantic
    (`tests/test_layers.py`) —, mais le repli est le même que celui de `profil._plat` sur les deux
    règles qui comptent ici : casse et diacritiques. Les racines du lexique sont en ASCII, et
    `re.findall` isole déjà les mots ; aucune autre règle de la Convention Texte ne les distingue.
    """
    trouves: dict[str, str] = {}
    for mot in re.findall(r"[^\W\d_]+", texte, flags=re.UNICODE):
        norme = "".join(c for c in unicodedata.normalize("NFD", mot.casefold())
                        if not unicodedata.combining(c))
        for racine in QUALIFICATIFS:
            if norme.startswith(racine):
                trouves.setdefault(racine, mot)
    return trouves


def _fusionner_par_qualificatif(libelles: list[str]) -> list[str]:
    """Dédoublonne des libellés de faits par **racine de qualificatif**, en gardant le plus complet.

    Lecture utilisateur des runs A16 (story 5.6, T8). Le run 1 demandait au client « action subite de
    la chaleur ou contact direct avec le foyer », puis « action subite de la chaleur » ; le run 3
    « caractère accidentel du bris », puis « caractère « accidentel » exigé par la clause citée ».
    Deux sources — ce que le modèle a nommé et ce que le code a composé faute qu'il le nomme
    (`steps.verifier._qualites_de_la_clause`) —, deux claims différentes, et une déduplication qui
    n'était que l'égalité de chaînes : la même exigence était posée deux à quatre fois de suite, et
    un gestionnaire lisant cette liste croit avoir quatre choses à établir là où il en a deux.

    La comparaison se fait donc sur ce que le libellé **exige** : les racines du lexique qu'il
    emploie. Un libellé dont toutes les racines sont déjà portées par un libellé retenu ne dit rien
    de neuf et disparaît ; celui qui en porte strictement plus absorbe ceux qu'il recouvre — c'est
    « la formulation la plus complète », mesurée en exigences et non en caractères, sans quoi la
    phrase composée par le code (la plus longue) chasserait les mots du modèle (les plus précis).
    Un libellé sans aucune racine n'est comparable à rien : il est gardé, dédoublonné à l'identique.
    """
    retenus: list[tuple[frozenset[str], str]] = []
    for libelle in libelles:
        texte = libelle.strip()
        if not texte:
            continue
        racines = frozenset(_mots_qualifiants(texte))
        if not racines:
            if all(texte != garde for _r, garde in retenus):
                retenus.append((racines, texte))
            continue
        if any(garde and racines <= garde for garde, _t in retenus):
            continue  # déjà demandé, dans des termes au moins aussi complets
        retenus = [(garde, t) for garde, t in retenus if not (garde and garde < racines)]
        retenus.append((racines, texte))
    return [texte for _racines, texte in retenus]


def _libelles_manquants(claims: list[ClaimJugee], *, etat: dict[str, Applicable], place: int) -> list[str]:
    """Les faits que le dossier ne dit pas, côté clauses : dédupliqués, dans l'ordre, bornés (D8).

    Deux sources, du même appel groupé et de la même nature — ce que la clause exige et que les faits
    déclarés ne donnent pas : le `fait_manquant` nommé par le modèle, et les `qualites_non_etablies`
    que le **code** a calculées par différence (revue Codex 1.8, B3 : « forcer `humain` **et produire
    une question bornée** »). Une qualité exigée qu'on rendrait `humain` sans jamais la demander
    laisserait le gestionnaire devant un verdict ouvert sans savoir quoi aller chercher.

    Seuls textes du modèle qui traversent jusqu'à l'utilisateur ; leur **longueur** est bornée par
    l'appelant (`fait_manquant_max_chars`, qui trace ce qu'il écarte), leur **nombre** ici.

    `place` est le nombre de questions encore disponibles **après** celles que le paquet manquant
    occupe déjà, jamais `ask_client_max` brut (revue 1.8) : borner sur le total laissait
    `missing.faits` annoncer un fait qu'aucune question de `ask_client` ne demandait, alors que le
    front affiche l'un sous l'autre. Ce qui n'entre pas ne figure nulle part.

    **Une claim tenue pour inapplicable n'exige rien de ce dossier** (reprise différée
    `libelles-manquants-verse-les-claims-inapplicables`, ouverte au tour 4). Le filtre manquait : le
    run 3 de la lecture utilisateur demandait au client d'établir « rayures, égratignures ou
    écaillements » et « défaut de réparation ou d'entretien des châssis » au titre d'une exclusion
    que la table venait précisément d'écarter (`applicable = "non"`). Peu importe la raison — portée
    disjointe (`hors_portee`) ou fait connu et contraire —, une clause écartée ne fonde aucun fait à
    établir : ce qu'elle exigeait est sans objet, et le demander donne au gestionnaire une piste que
    le verdict a déjà refermée. Le filtre est du code pur, postérieur à la réponse du modèle.

    La déduplication, elle, ne peut plus être l'égalité de chaînes : les libellés viennent de deux
    sources et de plusieurs claims, et la même exigence s'y écrit de plusieurs façons
    (`_fusionner_par_qualificatif`).
    """
    libelles: list[str] = []
    for claim in claims:
        if claim.champs is None or etat.get(claim.claim_id) == "non":
            continue
        libelles += [(claim.champs.fait_manquant or "").strip(),
                     *claim.champs.qualites_non_etablies]
    return _fusionner_par_qualificatif(libelles)[:max(place, 0)]


def _qualites_a_confirmer(claims: list[ClaimJugee], *, deja: list[str]) -> list[str]:
    """Les qualités qu'une clause exige et que le modèle a dites **établies** — à confirmer quand même.

    AC de la story : « `ask_client` mentionne les options/CP **et la nature « subite »** ». Le run réel
    du 24/08 a montré que rien ne le garantissait : le modèle a déclaré la qualité subite établie par
    des faits qui disent le contraire, `fait_manquant` est resté nul, et aucune question ne l'a
    mentionnée (revue Codex 1.8, B3). Le code ne peut pas juger si « soudain » est établi ; il peut en
    revanche poser la question **à chaque fois que la clause l'exige**, quelle qu'ait été la réponse du
    modèle. Un verdict « au regard des conditions générales seules » ne prouve de toute façon aucune
    qualité de l'événement : elle se confirme auprès du client.

    Les qualités **non** établies partent, elles, dans `missing.faits` — ce sont des faits à établir,
    pas des faits à confirmer, et le paquet manquant les annonce. `deja` porte donc ces faits-là : une
    qualité qu'une claim tient pour établie et qu'une autre déclare manquante est **une** exigence,
    et le client la lisait deux fois, sous deux formulations et deux préfixes (story 5.6, T8).
    """
    out: list[str] = []
    for claim in claims:
        if claim.champs is None:
            continue
        for libelle in claim.champs.qualites_exigees:
            if libelle and libelle not in claim.champs.qualites_non_etablies:
                out.append(libelle)
    fusion = _fusionner_par_qualificatif([*deja, *out])
    return [libelle for libelle in fusion if libelle not in deja]


QUESTION_OPTION = "Quelles options et extensions ont été souscrites ?"
QUESTION_CONDITIONS_PARTICULIERES = ("Que prévoient les conditions particulières "
                                     "(montants, franchises, biens désignés) ?")
QUESTION_AVENANT_DATE = (
    "À quelle date le contrat a-t-il pris effet, et un avenant l'a-t-il modifié depuis ? "
    "Le sinistre doit tomber dans la période garantie, dans la version alors en vigueur.")


def questions_du_paquet_typees(claims: list[ClaimJugee],
                               missing: MissingPackage) -> list[tuple[str, str]]:
    """Ce qu'il faut demander au client parce que le verdict ne lit que les conditions générales.

    Une question par pièce manquante d'AD-6, et rien qui dépende du modèle : un verdict rendu « au
    regard des conditions générales seules » ignore *par construction* les options souscrites, les
    conditions particulières, les avenants et la date d'effet. Les deux premières se **précisent**
    quand une clause citée en dépend explicitement (booléen typé), ce qui les fait passer d'une
    diligence à un préalable. La troisième couvre les deux pièces restantes : les annoncer manquantes
    dans `missing` sans jamais les demander laissait le gestionnaire devant quatre pièces absentes et
    deux questions (revue 1.8).

    Story 5.7 (L1g) : chaque question sort **typée** — le `QuestionKind` du fil de conversation —
    parce que deux lecteurs en ont besoin des deux moitiés. `ask_client` n'en garde que le texte ;
    `domain.conversation` a besoin du type pour rattacher la réponse à la bonne ligne de la table
    d'AD-6, et il reprend le texte sans le réécrire.
    """
    out: list[tuple[str, str]] = []
    if missing.options_souscrites:
        question = QUESTION_OPTION
        if any(c.champs is not None and c.champs.option_requise for c in claims):
            question += " Une clause citée ne joue qu'à cette condition."
        out.append(("option", question))
    if missing.conditions_particulieres:
        question = QUESTION_CONDITIONS_PARTICULIERES
        if any(c.champs is not None and c.champs.cp_requise for c in claims):
            question += " Une clause citée y renvoie."
        out.append(("conditions_particulieres", question))
    if missing.avenants or missing.date_effet:
        out.append(("avenant_date", QUESTION_AVENANT_DATE))
    return out


def _questions_du_paquet(claims: list[ClaimJugee], missing: MissingPackage) -> list[str]:
    return [texte for _kind, texte in questions_du_paquet_typees(claims, missing)]


def conditions_de_section_ouvertes(claims: list[ClaimJugee], *,
                                    etat: dict[str, Applicable]) -> list[ConditionDeSection]:
    """Les conditions d'applicabilité de section qu'aucune affirmation retenue n'établit (L1e).

    Une condition est **établie** quand une claim retenue cite son bloc et que la table la tient pour
    `applicable = "oui"`. Rien d'autre ne l'établit : ni le fait que le modèle l'ait citée sans la
    trancher, ni le silence de la réponse. En pratique, une condition qui renvoie aux conditions
    particulières ne peut jamais l'être au premier tour — les CP ne sont pas au dossier, `cp_requise`
    est forcé par le texte de la clause (`steps.verifier`, T18) et la claim vaut `humain` : c'est
    exactement ce que le verdict doit dire, au lieu de dépendre du hasard d'une citation.

    Seules les garanties sont interrogées, et pas celles que la table a écartées (`applicable="non"`):
    une clause sans objet ne subordonne rien, comme dans `_libelles_manquants`.
    """
    etablies = {clause.block_id for claim in claims if etat.get(claim.claim_id) == "oui"
                for clause in claim.clauses}
    ouvertes: list[ConditionDeSection] = []
    vues: set[str] = set()
    for claim in claims:
        if claim.kind != "garantie" or etat.get(claim.claim_id) == "non":
            continue
        for clause in claim.clauses:
            condition = clause.condition_section
            if condition is None or condition.block_id in etablies or condition.block_id in vues:
                continue
            vues.add(condition.block_id)
            ouvertes.append(condition)
    return ouvertes


def _conditions_de(claim: ClaimJugee, ouvertes: set[str]) -> list[ConditionDeSection]:
    """Les conditions de section **ouvertes** qui subordonnent les clauses d'une claim, dédupliquées."""
    trouvees: list[ConditionDeSection] = []
    for clause in claim.clauses:
        condition = clause.condition_section
        if condition is not None and condition.block_id in ouvertes and all(
                condition.block_id != vue.block_id for vue in trouvees):
            trouvees.append(condition)
    return trouvees


def question_de_section(condition: ConditionDeSection) -> str:
    """La question posée au client par une condition de section — le témoin choisit les mots.

    Story 5.7 (L1g) : cette formulation est **la** formulation. `ask_client` la publie et le fil de
    conversation la repose telle quelle (`domain.conversation`) ; il n'existe pas de seconde façon de
    demander la même pièce, donc pas de dérive possible entre ce que le verdict réclame et ce que la
    personne lit.
    """
    return (f"Vos conditions particulières mentionnent-elles la garantie « {condition.titre} » ?"
            if condition.renvoie_cp else
            f"La condition posée en tête de « {condition.titre} » est-elle remplie ?")


def question_de_fait(libelle: str) -> str:
    """Le fait qu'une clause exige et que les faits déclarés ne disent pas, demandé au client."""
    return f"Pouvez-vous confirmer ce fait : {libelle} ?"


def question_de_qualite(libelle: str) -> str:
    """La qualité qu'une clause subordonne à l'événement, demandée au client dans ses mots."""
    return f"L'événement présente-t-il cette qualité : {libelle} ?"


def _questions_de_section(conditions: list[ConditionDeSection]) -> list[str]:
    """Une question par condition de section ouverte, dans l'ordre où elles sont apparues."""
    return [question_de_section(condition) for condition in conditions]


def questions_du_paquet_manquant(missing: MissingPackage | None = None) -> list[str]:
    """Les questions dues **sans lire une seule clause** : le paquet manquant d'AD-6, et rien d'autre.

    Un refus de sinistre (hors périmètre, aucun bloc trouvé, toutes les citations rejetées) n'a aucune
    claim à interroger, mais il lui manque exactement les mêmes pièces qu'à un verdict ordinaire — et
    c'est le dossier qui a le plus besoin d'être complété. Le pipeline compose donc son `ask_client`
    ici, avec le **même** code et les mêmes mots, pour qu'un refus ne soit pas le seul verdict du
    système à ne rien réclamer (revue 1.8).
    """
    return _questions_du_paquet([], missing or MissingPackage())


def _escalades(claims: list[ClaimJugee], *, contradiction: bool, renvoi: bool) -> list[str]:
    """`escalate[]` composé par le code : ce qu'aucune règle ne peut trancher sans un humain."""
    out: list[str] = []
    if contradiction:
        out.append("Deux clauses citées se contredisent sans que les conditions générales les "
                   "départagent : arbitrage humain requis.")
    if renvoi:
        out.append("Un renvoi d'une clause décisionnelle n'a pas été résolu à l'ingestion : "
                   "la clause visée reste à lire.")
    if any(not clause.kind_confirmed for c in claims for clause in c.clauses):
        out.append("Le typage d'au moins une clause citée n'est pas confirmé : relecture humaine "
                   "requise avant toute conclusion.")
    if any(not clause.portee for c in claims for clause in c.clauses):
        out.append("La portée d'au moins une clause citée n'est pas déclarée : relecture humaine "
                   "requise pour savoir où elle s'applique.")
    return out


def _noeuds_du_cas(claims: list[ClaimJugee]) -> set[str]:
    """Les nœuds du contrat que le cas met en jeu (D3).

    Les nœuds parents des blocs cités par les claims `garantie` retenues. Sans garantie affichée,
    la branche contractuelle du cas reste indéterminée : une condition ou une exclusion citée ailleurs
    ne peut pas devenir le proxy positif du cas.
    """
    garanties = {clause.node_id for c in claims if c.kind == "garantie"
                 for clause in c.clauses if clause.node_id}
    return garanties


def applicabilites_des_claims(
        claims: list[ClaimJugee]) -> dict[str, tuple[Applicable | None, ApplicableReason | None]]:
    """Statut et raison issus du même état de portée que la table AD-6.

    Le calcul est par claim affichée. Seule une garantie affichée fixe les nœuds contractuels du
    cas ; sans elle, une exclusion reste humaine sauf si les faits typés suffisent à la contredire.
    """
    out: dict[str, tuple[Applicable | None, ApplicableReason | None]] = {}
    for claim in claims:
        case_nodes = _noeuds_du_cas(claims)
        applicable_sans_portee = applicable_de_claim(claim, noeuds_du_cas=set())
        if claim.kind == "exclusion" and not case_nodes and applicable_sans_portee != "non":
            applicable = "humain"
        else:
            applicable = applicable_de_claim(claim, noeuds_du_cas=case_nodes)
        reason: ApplicableReason | None = None
        if applicable == "non":
            out_of_scope = (
                claim.kind == "exclusion" and bool(case_nodes)
                and bool(claim.clauses)
                and all(clause.portee and clause.portee.isdisjoint(case_nodes)
                        for clause in claim.clauses)
            )
            reason = "hors_portee" if out_of_scope else "faits_contraires"
        out[claim.claim_id] = (applicable, reason)
    return out


def decider(claims: list[ClaimJugee], *, ask_client_max: int,
            missing: MissingPackage | None = None,
            resolutions: dict[str, tuple[Applicable | None, ApplicableReason | None]] | None = None,
            facettes_sans_reponse: int = 0) -> Verdict:
    """Découpage (b) d'AD-6 : la table exclusive, dans l'ordre, sur les claims **affichées** (D4).

    (0)   contradiction non résolue entre deux claims retenues, ou renvoi non résolu sur une claim
          décisionnelle ⇒ `ne_tranche_pas`, les deux passages restant affichés (AD-6) ;
    (0bis) aucune claim affichée de kind `garantie` ou `exclusion` ⇒ `ne_tranche_pas` : la table ne
          tranche que sur elles, et un verdict sans clause fondatrice serait une opinion ;
    (1)   exclusion `oui` dont la portée couvre les nœuds du cas ⇒ `non_couvert` ;
    (2)   garantie `oui` **et** (condition / franchise / exclusion `humain`, garantie hors socle, ou
          **condition d'applicabilité de sa section non établie**) ⇒ `sous_conditions` — politique
          conservatrice ;
    (2bis) garantie `humain` **par** option ou conditions particulières ⇒ `sous_conditions` : c'est
          le « dépend d'une option / CP inconnue » d'AD-6 vu depuis la **clause**, qu'une garantie
          `oui` ne peut pas exprimer (une garantie qui dépend d'une option est `humain` par
          construction, règle (6) de `applicable_de_claim`) ;
    (3)   garantie du socle `oui`, aucune claim retenue `humain`, **et chaque sous-question posée
          portée par une affirmation retenue** ⇒ `couvert` ;
    (4)   sinon ⇒ `ne_tranche_pas`.

    **La condition d'applicabilité de la section (story 5.7, L1e).** La règle (3) lisait le socle sur
    le seul `Node.scope.kind`, et tenait donc « 3.1.4 Dégâts des eaux » pour acquise parce qu'elle est
    rangée sous « 3.1 Garanties de base » — alors que le contrat écrit en tête de cette section même
    qu'elle n'est applicable que si les conditions particulières la mentionnent. Le même sinistre
    ressortait `sous_conditions` ou `couvert` selon que le modèle avait ou non cité cette clause : le
    verdict le plus fort du système dépendait du hasard d'une citation. La condition est désormais lue
    sur l'**arbre** (`Document.condition_de_section_applicable`, portée par `ClauseCitee`), et tant
    qu'aucune affirmation retenue ne l'établit `applicable="oui"`, la garantie n'est pas du socle au
    sens de la règle (3) : le verdict est plafonné à `sous_conditions`, la raison cite le bloc et son
    texte, et `ask_client` demande la pièce. Aucune combinaison de claims ne rend donc `couvert` sur
    une section conditionnée — une condition qui renvoie aux CP vaut `humain` par construction (T18),
    et une condition non citée n'est jamais établie.

    **`facettes_sans_reponse` (correctif du tour 6, F3).** `couvert` est le seul verdict qui affirme
    quelque chose de la totalité de la demande : « une garantie du socle s'applique et aucune clause
    citée ne reste ouverte ». Il ne peut donc pas se prononcer quand une sous-question posée n'a
    reçu aucune clause. Mesuré sur un run réel : une question à deux sous-questions — le bris d'une
    vitre et les dommages par la fumée — est ressortie `couvert` sur la **seule** clause des fumées,
    la sous-question du bris n'ayant aucune clause décisionnelle ; la réponse portait au même
    moment `complete=false` et « il reste 1 sous-question sans réponse ». Le compte vient de
    `Verification.facettes_couvertes`, c'est-à-dire de la mesure du code, jamais d'une déclaration.
    `sous_conditions` et `ne_tranche_pas` ne bougent pas : ils ne prétendent rien de ce qui manque.

    **`missing` accompagne le verdict ; il ne le décide pas (revue Codex 1.8, B1, tour 2).** Le tour 1
    avait lu « la garantie dépend d'une option / extension / **condition particulière inconnue** »
    (AD-6, règle 2) sur `MissingPackage`, c'est-à-dire sur le dossier **global** : tant que les
    conditions particulières n'étaient pas au dossier, aucune garantie n'était tenue pour acquise et
    `couvert` restait hors d'atteinte sans un argument `dossier`. L'AC de la story dit autre chose,
    mot à mot : « (2) garantie `oui` et (condition/franchise/exclusion `humain` ou **garantie hors
    socle / dépendant d'une option**) », puis « (3) garantie du socle `oui` sans condition ouverte ⇒
    `couvert` », et enfin « tests avec fixtures : … `couvert` (**garantie socle**) ». La dépendance se
    lit donc sur la **clause** — ses champs typés (`option_requise`, `cp_requise`) et son nœud
    (`socle`) —, jamais sur l'absence globale du paquet. `missing` est reporté tel quel dans le
    verdict et il alimente les questions au client, y compris sous un `couvert` : un verdict « au
    regard des conditions générales seules » annonce ce qu'il n'a pas lu.

    **Ce qui empêche alors un `couvert` de complaisance**, et qui est la vraie garde (revue Codex 1.8,
    B3) : la garantie ne vaut `oui` que si **toutes** les qualités que sa clause exige sont établies
    par les faits déclarés, et *vérifier* n'accepte une qualité pour établie que si le modèle cite un
    fragment des faits **relu mot pour mot** dans les faits soumis. Un modèle qui n'énumère rien laisse
    ses champs typés inexploitables, et la claim vaut `humain`. Le cas témoin de la bougie tient par là
    — la clause exige le caractère soudain de l'événement, les faits ne le disent pas —, et non par une
    politique qui fermerait la règle (3) à tout le monde.

    `applicable` est relu sur chaque claim par `applicable_de_claim()` : la table ne dépend d'aucun
    champ que l'appelant aurait pu remplir autrement.
    """
    connu = (missing or MissingPackage()).model_copy(deep=True)
    retenues = [c for c in claims if c.retenue]
    expected_claim_ids = {claim.claim_id for claim in retenues}
    if resolutions is None or set(resolutions) != expected_claim_ids:
        # Une map partielle ne doit ni lever `KeyError`, ni mélanger deux états de portée. On
        # recalcule alors l'ensemble depuis les claims affichées, avec le même code que le chemin
        # nominal ; les valeurs fournies ne sont pas complétées au hasard.
        resolutions = applicabilites_des_claims(retenues)
    etat = {claim_id: value for claim_id, (value, _reason) in resolutions.items()}
    # Les questions du paquet manquant d'abord : elles ne dépendent d'aucune sortie du modèle et
    # elles sont dues quel que soit le verdict — mais seulement pour les pièces réellement absentes.
    # Ce qu'elles laissent de place borne alors les libellés du modèle, si bien que `missing.faits` et
    # `ask_client` disent la même chose.
    paquet = _questions_du_paquet(retenues, connu)
    # L1e : la condition que le contrat écrit en tête de la section d'une garantie retenue passe
    # devant les faits — c'est elle qui plafonne le verdict, et elle est due quel que soit ce que le
    # modèle a cité. Elle entre dans le décompte de `place` pour la même raison que `paquet` : ce que
    # `missing.faits` annonce, `ask_client` doit pouvoir le demander.
    sections_ouvertes = conditions_de_section_ouvertes(retenues, etat=etat)
    conditions_ouvertes = {condition.block_id for condition in sections_ouvertes}
    questions_section = _questions_de_section(sections_ouvertes)
    manquants = _libelles_manquants(
        retenues, etat=etat, place=ask_client_max - len(paquet) - len(questions_section))
    missing_final = connu.model_copy(update={"faits": manquants})
    ask = (paquet
           + questions_section
           + [f"Fait à établir auprès du client : {libelle}" for libelle in manquants]
           + [f"Qualité exigée par une clause citée, à faire confirmer par le client : {libelle}"
              for libelle in _qualites_a_confirmer(retenues, deja=manquants)])[:ask_client_max]
    contradiction = any(c.contredit for c in retenues)
    renvoi = any(c.renvoi_ouvert for c in retenues if c.clauses)
    escalate = _escalades(retenues, contradiction=contradiction, renvoi=renvoi)

    def verdict(value: VerdictValue, reason: str,
                *, questions: list[str] | None = None) -> Verdict:
        return Verdict(value=value, reason=f"{reason} ({PORTEE})",
                       missing=missing_final.model_copy(deep=True),
                       ask_client=([*questions, *ask][:ask_client_max] if questions else ask),
                       escalate=escalate)

    # (0) — ni une contradiction ni un renvoi ouvert ne se tranchent par du code.
    if contradiction:
        return verdict("ne_tranche_pas", "Deux clauses citées se contredisent et rien dans les "
                                         "conditions générales ne les départage")
    if renvoi:
        return verdict("ne_tranche_pas", "Une clause décisionnelle renvoie à un passage que "
                                         "l'ingestion n'a pas résolu")

    fondatrices = [c for c in retenues if c.kind in KINDS_FONDATEURS]
    if not fondatrices:
        if retenues:
            return verdict("ne_tranche_pas", "Des passages ont été retrouvés et affichés, mais "
                                             "aucun n'est confirmé comme garantie ou exclusion "
                                             "fondatrice")
        return verdict("ne_tranche_pas", "Aucun passage n'a été retenu et affiché")

    exclusions = [c for c in retenues if c.kind == "exclusion"]
    garanties = [c for c in retenues if c.kind == "garantie"]

    # (1) — l'exclusion prime, à condition que sa portée couvre le cas (AD-2, `scope_nodes`).
    for exclusion in exclusions:
        if etat[exclusion.claim_id] != "oui":
            continue
        cas = _noeuds_du_cas(retenues)
        if any(clause.portee & cas for clause in exclusion.clauses):
            return verdict("non_couvert", "Une exclusion applicable couvre le cas décrit")

    ouvertes = [c for c in retenues
                if c.kind in ("condition", "franchise", "exclusion") and etat[c.claim_id] == "humain"]
    for garantie in garanties:
        if etat[garantie.claim_id] != "oui":
            continue
        # « … ou d'une **extension** » (AD-6, règle 2) : le nœud le dit, pas le modèle.
        hors_socle = any(not clause.socle for clause in garantie.clauses)
        conditionnee = _conditions_de(garantie, conditions_ouvertes)
        # (2) — politique conservatrice : un seul de ces trois motifs suffit à ouvrir le verdict.
        if ouvertes or hors_socle or conditionnee:
            motifs = []
            if ouvertes:
                motifs.append("une condition, une franchise ou une exclusion citée reste ouverte")
            if hors_socle:
                motifs.append("la garantie ne relève pas du socle commun")
            # L1e : la condition est **montrée**, pas seulement invoquée — le bloc et son texte, relus
            # dans le corpus. Sans cela, « sous conditions » ne dit pas au lecteur ce qu'il lui reste
            # à établir, et c'est précisément la clause que le modèle n'avait pas citée.
            for condition in conditionnee:
                motifs.append(f"le contrat ne rend « {condition.titre} » applicable qu'à une "
                              f"condition qu'aucun passage retenu n'établit — {condition.block_id} : "
                              f"« {condition.texte.strip()} »")
            return verdict("sous_conditions", "Une garantie s'applique, mais " + " ; ".join(motifs))

    # (2bis) — la garantie **elle-même** dépend d'une option ou de conditions particulières inconnues.
    for garantie in garanties:
        champs = garantie.champs
        if etat[garantie.claim_id] == "humain" and champs is not None and (champs.option_requise
                                                                           or champs.cp_requise):
            return verdict("sous_conditions", "La garantie citée ne joue que si une option ou les "
                                              "conditions particulières la prévoient")

    # (3) — le seul chemin vers `couvert` : garantie du socle et plus rien d'ouvert **nulle part**.
    # Le `oui` qui l'ouvre est corroboré par `applicable_de_claim()`, qui exige que toutes les qualités
    # exigées par la clause soient établies par les faits déclarés — et *vérifier* n'en tient une pour
    # établie que sur un fragment des faits relu mot pour mot (B3, tour 2).
    for garantie in garanties:
        # L1e : `socle` seul ne suffit plus. Une garantie rangée sous « 3.1 Garanties de base » relève
        # bien du socle commun au sens de la portée, et le contrat écrit pourtant en tête de sa
        # section qu'elle n'est applicable que si les conditions particulières la mentionnent. Tant
        # que cette condition n'est pas établie, la garantie n'est pas acquise : le socle est lu ici
        # comme la conjonction des deux — la portée **et** la condition d'applicabilité de la section.
        socle = (all(clause.socle for clause in garantie.clauses)
                 and not _conditions_de(garantie, conditions_ouvertes))
        if etat[garantie.claim_id] == "oui" and socle:
            if any(etat[c.claim_id] == "humain" for c in retenues):
                continue
            if facettes_sans_reponse > 0:
                # F3 : la règle (3) affirme quelque chose de **toute** la demande. Une sous-question
                # sans clause n'est pas une nuance à côté du verdict, c'est une part de la question
                # sur laquelle le contrat n'a rien dit — et la question passe devant les autres,
                # puisque c'est elle qui empêche de trancher.
                pluriel = "s" if facettes_sans_reponse > 1 else ""
                return verdict(
                    "ne_tranche_pas",
                    f"Une garantie du socle commun s'applique, mais {facettes_sans_reponse} "
                    f"sous-question{pluriel} de la demande n'a reçu aucune clause du contrat",
                    questions=[f"{facettes_sans_reponse} sous-question{pluriel} de votre demande "
                               f"n'a reçu aucune clause des conditions générales : précisez-la ou "
                               f"indiquez les pièces qui la concernent."])
            return verdict("couvert", "Une garantie du socle commun s'applique et aucune clause "
                                      "citée ne reste ouverte")

    # (4) — reste de la table : aucune règle n'a tranché.
    return verdict("ne_tranche_pas", "Aucune règle de la table ne tranche sur les clauses retrouvées")
