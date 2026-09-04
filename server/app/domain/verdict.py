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

from pydantic import Field, model_validator

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
    # Story 5.7 (L1m) : le libellé d'une exigence nommait la **couverture elle-même** (« caractère
    # couvert du sinistre »). Dérivé, jamais rendu par le modèle : il marque la clause dont l'effet
    # est subordonné au fait que la garantie joue — une clause d'**étendue** —, et c'est lui que
    # `applicabilites_des_claims` lit pour la faire suivre la garantie principale de son nœud.
    reference_a_la_couverture: bool = False

    @model_validator(mode="after")
    def _ecarter_les_references_a_la_couverture(self) -> "ChampsApplicabilite":
        """Une exigence qui nomme la couverture n'est ni une question, ni un fait manquant (L1m).

        Le filtre est posé **à la porte du type**, et non chez chacun de ses lecteurs : le paquet
        manquant, le fil de conversation et la table lisent tous les trois `fait_manquant` et
        `qualites_non_etablies`, et une garde recopiée trois fois aurait fini par manquer au
        quatrième. Ce qui entre ici n'a plus de libellé circulaire à demander à personne.

        `fait_manquant` circulaire ⇒ retiré **et** `fait_requis_present` remis à vrai : l'effacer
        seul aurait donné la signature du « fait connu et contraire » (`applicable="non"`, règle (5)
        d'`applicable_de_claim`), c'est-à-dire une clause écartée là où elle doit simplement suivre
        sa garantie. Le libellé reste dans `qualites_exigees`, qui n'est affiché nulle part et sert
        la trace : c'est là que se lit ce que la clause exigeait vraiment.
        """
        marque = any(nomme_la_couverture(libelle) for libelle in
                     (*self.qualites_exigees, *self.qualites_non_etablies))
        gardees = [q for q in self.qualites_non_etablies if not nomme_la_couverture(q)]
        if gardees != self.qualites_non_etablies:
            self.qualites_non_etablies = gardees
        if (self.fait_manquant or "").strip() and nomme_la_couverture(self.fait_manquant or ""):
            marque = True
            self.fait_manquant = None
            self.fait_requis_present = True
        if marque and not self.reference_a_la_couverture:
            self.reference_a_la_couverture = True
        return self


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
    # Story 5.7 (L1o) : ce bloc **ouvre** une énumération — il introduit ses items et n'énonce rien
    # par lui-même (`Index.est_amorce_denumeration`, lu sur l'arbre comme le `kind` et la portée).
    # Une amorce reste une clause citée, avec son texte et ses qualificatifs ; elle n'est pas une
    # clause qui **décide** (`ClaimJugee.clauses_decisionnelles`).
    amorce: bool = False
    # Story 5.7 (L1q) : le bloc de l'**amorce** dont cette clause est un item, ou une chaîne vide.
    # Lu sur l'arbre comme `amorce` (`Index.amorce_qui_introduit`), et c'est le seul signal dont la
    # table a besoin pour savoir que deux garanties sont **sœurs** : les items d'une même énumération
    # de périls sont des voies alternatives vers la couverture, jamais des exigences cumulées.
    amorce_de: str = ""


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
    # Story 5.7 (L1r) : le **rattachement** de l'affirmation, jugé soutenu par le contrôle groupé,
    # relie un fait de la déclaration au vocabulaire de la clause citée
    # (`steps.verifier._qualification_affirmee`, la porte de L1b/L1c). Ce n'est pas une déclaration
    # du modèle : le code relit une proposition du rattachement, exige qu'un de ses mots porteurs se
    # retrouve dans la citation vérifiée et qu'un **autre** se retrouve dans les faits déclarés.
    fait_rattache: bool = False

    @property
    def clauses_decisionnelles(self) -> list[ClauseCitee]:
        """Les clauses qui **décident** : toutes sauf les amorces d'énumération (story 5.7, L1o).

        Une amorce — « La Compagnie assure les biens désignés, contre les périls suivants : » —
        n'énonce ni garantie, ni exclusion, ni condition : elle annonce les items qui, eux, les
        énoncent. Citée **seule** par une affirmation, elle laisse donc cette liste vide et la claim
        vaut `applicable=None` : elle est affichée avec sa citation, hors de la table. Citée **avec**
        son item (L1f), elle reste ce qu'elle est — le contexte de l'item —, et l'item décide seul :
        l'applicabilité est exactement celle qu'il avait.

        Elle n'est pas retirée de `clauses` pour autant. Son texte reste lu partout ailleurs — les
        qualificatifs qu'elle écrit sont ceux de ses items (L1n), la condition de sa section est
        celle de la garantie (L1e) —, et l'affaiblir là aurait rendu `oui` des claims que ces deux
        règles ferment.
        """
        return [clause for clause in self.clauses if not clause.amorce]

    @property
    def kind(self) -> str | None:
        """Le kind décisionnel de la claim — un seul, garanti par le contrôle « une clause par
        affirmation » de *vérifier* (D6) ; `None` si elle ne cite aucune clause décisionnelle."""
        decisionnelles = self.clauses_decisionnelles
        return decisionnelles[0].kind if decisionnelles else None


def applicable_de_claim(claim: ClaimJugee, *, noeuds_du_cas: set[str] | None = None) -> Applicable | None:
    """Découpage (a) d'AD-6 : `applicable` est **dérivé**, jamais rendu par le modèle.

    Ordre de dérivation (D1 de la spec 1.8), du plus prudent au plus engageant :

    1. aucune clause décisionnelle citée ⇒ `None` — une définition, un paragraphe ou une **amorce
       d'énumération** n'a pas d'applicabilité, et lui en prêter une ferait entrer dans la table une
       claim qui n'y a rien à faire ;
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

    **(1) et les amorces d'énumération (story 5.7, L1o).** La règle est lue sur
    `clauses_decisionnelles`, jamais sur `clauses` : une amorce introduit des items, elle n'énonce
    rien. Citée seule, elle ne laisse aucune clause qui décide et la claim vaut `None` — c'est le
    même geste que le paragraphe et la définition, tenu cette fois sur la **structure** du document
    plutôt que sur le `kind` du bloc. Mesuré sur le gate AXA `-14` du 04/09 : jugée `non`, puis non
    soutenue, puis `oui` sur trois répétitions du même cas, `p34:6` faisait à elle seule diverger le
    verdict entre `ne_tranche_pas` et `sous_conditions`.

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
    clauses = claim.clauses_decisionnelles
    if not clauses:
        return None
    if any(not c.kind_confirmed for c in clauses):
        return "humain"
    if any(not c.portee for c in clauses):
        return "humain"
    # **Story 5.7 (L1u) : le rattachement ne conclut plus rien.** L1r avait donné ici une règle
    # (3bis) : une exclusion retenue dont le rattachement, jugé soutenu, nommait un fait de la
    # déclaration dans les mots de la clause valait `oui`, et aucun champ typé ne pouvait la rouvrir.
    # Elle est retirée. Mesuré le 04/09/2026 sur le gate Baloise `-18`, cas `b-invite-cigarette` :
    # `ne_tranche_pas`, **`non_couvert`**, `ne_tranche_pas` sur le même cas et le même corpus — un
    # « exclu » hors des valeurs admissibles, produit par une exclusion citée dans une seule des
    # trois répétitions. Le rattachement est de la prose libre du modèle : le code y recoupe des
    # mots, il n'en lit ni la négation ni la portée, et une phrase qui dit l'inverse de ce qu'elle
    # semble dire y passe entière. Une exclusion reste donc `humain` tant que ses champs typés
    # laissent quelque chose d'ouvert, et `non` par sa portée ou par un fait connu et contraire,
    # comme avant L1r. Prix payé, et assumé : « exclu » se dit moins souvent (`s10-intention`,
    # `s03-velo`, `s11-bijoux` de la batterie du 03/09 redeviennent `ne_tranche_pas`). Ce qui le
    # rouvrirait : un rattachement dont le code lise le sens, ou une exclusion qui conclue sur ses
    # seuls champs typés — jamais sur le choix de citations du modèle.
    #
    # Une exclusion dont la portée déclarée ne couvre aucun nœud du cas est inapplicable par
    # construction, indépendamment du jugement du modèle sur les faits. Une portée absente a déjà
    # rendu `humain` ci-dessus ; un cas sans nœud prouvé ne permet pas non plus cette conclusion.
    if (claim.kind == "exclusion" and noeuds_du_cas
            and all(clause.portee.isdisjoint(noeuds_du_cas) for clause in clauses)):
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
#
# **Story 5.7 (L1r) : « effraction » en est sorti.** Le lexique nomme des qualités qu'*aucune
# circonstance déclarée n'établit* — la vitesse à laquelle la chaleur a agi, le caractère soudain
# d'un événement : c'est ce qui justifie de les fermer au rattachement (`_qualifie_par_la_clause`).
# L'effraction n'est pas de cet ordre : c'est un **fait**, que la déclaration dit dans ses propres
# mots (« la porte de la cave a été forcée ») et que le rattachement relie au terme du contrat. Tant
# qu'elle y figurait, le code composait « caractère « effraction » exigé par la clause citée » et le
# posait au client alors que l'affirmation retenue venait de l'établir — mesuré le 03/09/2026 sur
# `b03-vol-cave`, où le dossier redemandait l'effraction que la réponse affichait. Un fait se prouve
# par les faits ; il n'a pas à passer par le lexique des qualités.
QUALIFICATIFS: frozenset[str] = frozenset({
    "soudain", "subit", "brusque", "instantane", "accidentel", "fortuit", "imprevisible", "imprevu",
    "involontaire", "intentionnel", "immediat", "direct", "permanent", "exceptionnel", "violent",
    "anormal", "malveillant"})


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


# Story 5.7 (L1m). Les mots par lesquels une clause nomme **la couverture elle-même**. Ils ne
# décrivent aucun fait du sinistre : « la perte d'eau subie à l'occasion d'un sinistre **couvert** est
# prise en charge » ne dit pas ce que le sinistre doit présenter, elle dit que la garantie doit jouer
# — c'est-à-dire exactement ce que la table calcule. Mesuré sur le parcours de prod du 04/09 : le
# modèle a rendu la qualité « caractère couvert du sinistre » pour `p38:2`, le code l'a tenue pour non
# établie, la clause a valu `humain`, et le client a lu « Le sinistre présente-t-il cette
# caractéristique : « caractère couvert du sinistre » ? » — une question dont la réponse est le
# verdict qu'il attend.
#
# Fermé et court comme les trois autres lexiques : il ne sert pas à comprendre la clause, seulement à
# reconnaître un libellé **circulaire**. Les racines sont cherchées en tête de mot (formes fléchies :
# « couverte », « garantis », « assurée », « indemnisable ») ; les tournures composées, telles quelles.
RACINES_DE_COUVERTURE: frozenset[str] = frozenset({
    "couvert", "garanti", "assur", "indemnis"})
TOURNURES_DE_COUVERTURE: frozenset[str] = frozenset({
    "pris en charge", "prise en charge", "prises en charge", "pris en compte"})

# Ce qu'un libellé circulaire a le droit de nommer **à côté** de la couverture : le sinistre lui-même
# et rien d'autre. C'est ce garde-fou qui tient le rayon du lexique — « qualité d'assuré de la
# personne en cause » porte bien la racine « assur », mais elle nomme aussi une *personne* et une
# *cause* : elle décrit un fait, elle reste due au client (`QUALITES_DE_PERSONNE`, T19). Seul un
# libellé qui ne dit rien de plus que « ce sinistre est couvert » est écarté.
MOTS_DU_SINISTRE: frozenset[str] = frozenset({
    "caractere", "nature", "sinistre", "sinistres", "dommage", "dommages", "evenement",
    "evenements", "cas", "declare", "declares", "survenu", "survenus"})

# Grammaire pure : ni un fait, ni une couverture. Séparée du lexique pour qu'on lise d'un coup d'œil
# ce que la règle regarde vraiment (les mots pleins) et ce qu'elle ignore.
_MOTS_OUTILS: frozenset[str] = frozenset({
    "a", "au", "aux", "ce", "cet", "cette", "d", "de", "des", "du", "en", "est", "et", "etre",
    "l", "la", "le", "les", "ne", "on", "ou", "par", "pas", "pour", "que", "qui", "sa", "ses",
    "soit", "son", "sont", "un", "une", "y"})


def _mots_normalises(texte: str) -> list[str]:
    """Les mots d'un libellé, sans casse ni diacritiques — même repli que `_mots_qualifiants`.

    La couche `domain` n'importe que la stdlib et pydantic (`tests/test_layers.py`) : pas de
    `normalize()` de `corpus`, mais les deux seules règles qui comptent ici (casse, diacritiques),
    appliquées aux mots que `re.findall` isole déjà — guillemets et apostrophes tombent avec.
    """
    mots: list[str] = []
    for mot in re.findall(r"[^\W\d_]+", texte, flags=re.UNICODE):
        mots.append("".join(c for c in unicodedata.normalize("NFD", mot.casefold())
                            if not unicodedata.combining(c)))
    return mots


def nomme_la_couverture(libelle: str) -> bool:
    """Le libellé ne dit-il rien d'autre que « ce sinistre est couvert » ? (story 5.7, L1m)

    Un tel libellé n'est **jamais** une question ni un fait manquant : la couverture est ce que la
    table conclut, la demander au client est circulaire. La règle est en deux temps, et c'est le
    second qui la tient étroite :

    1. le libellé porte une racine ou une tournure de couverture (`RACINES_DE_COUVERTURE`,
       `TOURNURES_DE_COUVERTURE`) ;
    2. **et** tout ce qu'il dit d'autre ne nomme que le sinistre (`MOTS_DU_SINISTRE`) ou n'est que
       de la grammaire.

    « caractère couvert du sinistre » et « sinistre garanti » sont donc écartés ; « qualité d'assuré
    de la personne en cause » (T19), « biens assurés désignés » ou « garantie souscrite » nomment
    autre chose que la couverture et restent des faits à établir.
    """
    plat = " ".join(_mots_normalises(libelle))
    porte = False
    for tournure in TOURNURES_DE_COUVERTURE:
        if tournure in plat:
            porte = True
            plat = plat.replace(tournure, " ")
    autres: list[str] = []
    for mot in plat.split():
        if mot in _MOTS_OUTILS or mot in MOTS_DU_SINISTRE:
            continue
        if any(mot.startswith(racine) for racine in RACINES_DE_COUVERTURE):
            porte = True
            continue
        autres.append(mot)
    return porte and not autres


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
    return [texte for texte, _variantes in fusionner_faits(libelles)]


def fusionner_faits(libelles: list[str]) -> list[tuple[str, list[str]]]:
    """Le même regroupement, mais qui **rend ses classes** : le fait retenu et tout ce qui le dit.

    Story 5.7 (L1k). `_fusionner_par_qualificatif` répondait à la seule question du paquet manquant :
    quels libellés afficher. Le fil de conversation en pose une seconde — *quelles clauses* une
    question posée une seule fois interroge —, et il ne peut y répondre qu'avec les classes. Deux
    clauses qui exigent la même qualité doivent donner **une** question rattachée aux deux ; poser la
    question au libellé retenu et rattacher par égalité de chaînes en aurait laissé une de côté, et
    la personne aurait répondu à une exigence sans que l'autre clause bouge.

    La règle de regroupement ne change pas d'un iota — c'est la même, écrite une fois ici : un
    libellé dont toutes les racines sont déjà portées rejoint la classe qui le porte, un libellé qui
    en porte strictement plus absorbe les classes qu'il recouvre et devient leur représentant. Le
    représentant est rendu en tête, les autres formulations derrière lui, dans leur ordre d'arrivée.
    """
    retenus: list[tuple[frozenset[str], list[str]]] = []
    for libelle in libelles:
        texte = libelle.strip()
        if not texte:
            continue
        racines = frozenset(_mots_qualifiants(texte))
        if not racines:
            classe = next((c for r, c in retenus if c[0] == texte), None)
            if classe is None:
                retenus.append((racines, [texte]))
            continue
        couvrante = next((c for garde, c in retenus if garde and racines <= garde), None)
        if couvrante is not None:  # déjà demandé, dans des termes au moins aussi complets
            if texte not in couvrante:
                couvrante.append(texte)
            continue
        absorbees = [c for garde, c in retenus if garde and garde < racines]
        retenus = [(garde, c) for garde, c in retenus if not (garde and garde < racines)]
        retenus.append((racines, [texte, *[v for classe in absorbees for v in classe
                                           if v != texte]]))
    return [(classe[0], classe) for _racines, classe in retenus]


def meme_fait(un: str, autre: str) -> bool:
    """Deux libellés exigent-ils la même chose ? La question du fil, tranchée par le regroupement.

    Une seule définition de « le même fait » dans tout le système : celle de `fusionner_faits`. Le
    fil s'en sert pour savoir si une question posée vise une clause (`_vise`) et si la réponse
    « oui » lève l'exigence qu'elle portait (`_recompute`) — sans quoi une question fusionnée aurait
    laissé sa clause d'origine ouverte, et l'affinage n'aurait rien changé au verdict.
    """
    return len(fusionner_faits([un, autre])) == 1


def faits_etablis_par_rattachement(claims: list[ClaimJugee], *,
                                   etat: dict[str, Applicable | None]) -> list[str]:
    """Les faits qu'un rattachement retenu a **établis** : ils ne se demandent plus (story 5.7, L1t).

    Née de la règle (3bis) de L1r, qu'elle rendait cohérente : une exclusion que le rattachement
    faisait conclure gardait, dans la même page, le fait qu'elle venait de tenir pour acquis — le
    03/09/2026, `s10-intention` rendait « Exclu » en citant « le fait d'avoir mis le feu exprès est
    une faute intentionnelle », et demandait au-dessous « Fait à établir auprès du client : faute
    intentionnelle ou dolosive de l'assuré ».

    **Story 5.7 (L1u) : la règle reste, son unique déclencheur d'alors est parti.** (3bis) est
    retirée — un rattachement ne rend plus rien `oui` —, mais la règle ne tenait pas à elle : elle
    dit que quand la table a conclu `oui` sur une claim dont le rattachement est soutenu, ce que
    cette claim déclarait manquant — le `fait_manquant` du modèle comme les `qualites_non_etablies`
    calculées par le code — ne se redemande plus. Le `oui` vient désormais des seuls chemins
    restants : les champs typés corroborés. Un fait établi ne se demande à personne, ni au titre de
    la clause qui l'a établi, ni au titre d'une autre qui l'exige dans d'autres termes : c'est
    `meme_fait` qui reconnaît la seconde, la définition unique du système (L1b/L1k).

    Le verrou est `etat == "oui"`, et il n'est pas décoratif : une garantie dont le rattachement est
    soutenu mais dont une qualité exigée n'est pas établie reste `humain`, et ses questions restent
    dues — le cas bougie garde les siennes. Rien ici n'établit une qualité que la table n'a pas déjà
    tenue pour acquise.
    """
    etablis: list[str] = []
    for claim in claims:
        if claim.champs is None or not claim.fait_rattache or etat.get(claim.claim_id) != "oui":
            continue
        etablis += [(claim.champs.fait_manquant or "").strip(),
                    *claim.champs.qualites_non_etablies]
    return [libelle for libelle in etablis if libelle.strip()]


def exclusion_decisive(claims: list[ClaimJugee], *,
                       etat: dict[str, Applicable | None]) -> ClaimJugee | None:
    """L'exclusion qui fonde `non_couvert` — règle (1) de la table, isolée pour être lue avant elle.

    Story 5.7 (L1t). La table décidait, puis les questions étaient composées ; elles l'étaient donc
    sans savoir que le verdict était `non_couvert`. `s11-bijoux` en montrait le prix le 03/09/2026 :
    l'exclusion des vols simples s'appliquait, et le dossier demandait quand même le dépôt de plainte
    et la souscription de la garantie vol — les deux conditions de la garantie que l'exclusion venait
    d'écarter. Le calcul de la règle (1) vit donc ici, appelé par `decider` **avant** les questions
    et **par** la table ensuite : une seule lecture, deux usages.

    Sa définition ne bouge pas d'un mot : une exclusion affichée que la table tient pour `oui`, et
    qui rencontre le cas — par sa portée déclarée (AD-2) ou par son rattachement aux faits (L1r).
    Depuis L1u, le `oui` ne s'obtient plus par le rattachement : celui-ci ne dit plus que *où*
    s'applique une exclusion que ses champs typés ont établie, et la règle devient inerte tant
    qu'aucune exclusion n'atteint `oui`. Elle reste juste, et elle reste lue.
    """
    cas = _noeuds_du_cas(claims)
    for exclusion in claims:
        if exclusion.kind != "exclusion" or etat.get(exclusion.claim_id) != "oui":
            continue
        if exclusion.fait_rattache or any(clause.portee & cas for clause in exclusion.clauses):
            return exclusion
    return None


def _libelles_manquants(claims: list[ClaimJugee], *, etat: dict[str, Applicable], place: int,
                        etablis: list[str] | None = None) -> list[str]:
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

    **Un fait déjà établi n'est pas un fait manquant** (story 5.7, L1t). `etablis` porte ce qu'un
    rattachement retenu a établi (`faits_etablis_par_rattachement`) : le filtre est le même geste que
    celui de la claim écartée, un cran plus tôt — la clause vise bien le cas, et ce qu'elle exigeait
    est déjà là. La reconnaissance passe par `meme_fait`, jamais par l'égalité de chaînes : une autre
    clause qui exige la même chose dans d'autres termes ne doit pas rouvrir ce que celle-ci a fermé.

    La déduplication, elle, ne peut plus être l'égalité de chaînes : les libellés viennent de deux
    sources et de plusieurs claims, et la même exigence s'y écrit de plusieurs façons
    (`_fusionner_par_qualificatif`).
    """
    acquis = [libelle for libelle in (etablis or []) if libelle.strip()]
    libelles: list[str] = []
    for claim in claims:
        if claim.champs is None or etat.get(claim.claim_id) == "non":
            continue
        libelles += [(claim.champs.fait_manquant or "").strip(),
                     *claim.champs.qualites_non_etablies]
    retenus = [libelle for libelle in _fusionner_par_qualificatif(libelles)
               if not any(meme_fait(libelle, etabli) for etabli in acquis)]
    return retenus[:max(place, 0)]


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
            # L1m : `qualites_exigees` garde le libellé circulaire pour la trace ; il ne se confirme
            # pas plus qu'il ne se demande — c'est le verdict lui-même que la clause y nomme.
            if libelle and libelle not in claim.champs.qualites_non_etablies and not (
                    nomme_la_couverture(libelle)):
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
    """Le fait qu'une **exclusion** exige, demandé au client : ce qui a eu lieu, ou non.

    Story 5.7 (L1k). La formulation était « Pouvez-vous confirmer ce fait : … ? ». Elle demande un
    aveu : elle présuppose le fait, elle nomme le lecteur comme celui qui le concède, et sur le
    parcours réel du 03/09 elle portait « défaut de réparation ou d'entretien » — une faute — à
    quelqu'un qui venait de décrire un robinet oublié. Une exclusion ne se confirme pas, elle a eu
    lieu ou elle n'a pas eu lieu ; la question le demande dans ces termes, et les mots de la clause
    restent entre guillemets parce qu'ils sont ceux du contrat, pas les nôtres.
    """
    return f"Y a-t-il eu « {libelle} » ?"


def question_de_qualite(libelle: str) -> str:
    """La qualité qu'une **garantie** subordonne à l'événement, demandée au client dans ses mots.

    « L'événement présente-t-il cette qualité : … ? » nommait la ligne de la table (une « qualité »
    d'un « événement ») ; la personne, elle, a déclaré un sinistre et lit une caractéristique que le
    contrat exige de lui (story 5.7, L1k).
    """
    return f"Le sinistre présente-t-il cette caractéristique : « {libelle} » ?"


def question_de_fait_exige(libelle: str, *, kind: str | None) -> str:
    """La question due par une clause pour un fait qu'elle exige — la forme suit son rôle.

    Un seul point d'entrée, parce qu'un seul endroit doit décider si l'on demande *ce qui a eu lieu*
    (une exclusion) ou *ce que le sinistre présente* (une garantie, une condition, une franchise :
    elles décrivent toutes le sinistre couvert). Le `kind` vient de la clause, jamais des mots du
    libellé : deux clauses peuvent exiger la même chose et ne pas la demander de la même façon.
    """
    return question_de_fait(libelle) if kind == "exclusion" else question_de_qualite(libelle)


def _questions_de_section(conditions: list[ConditionDeSection]) -> list[str]:
    """Une question par condition de section ouverte, dans l'ordre où elles sont apparues."""
    return [question_de_section(condition) for condition in conditions]


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


# `non` < `humain` < `oui` : l'ordre de force d'une applicabilité, dont le minimum sert à combiner
# deux lectures d'une même clause sans jamais rendre la plus engageante des deux.
_FORCE: dict[str, int] = {"non": 0, "humain": 1, "oui": 2}


def _garantie_principale_du_noeud(claim: ClaimJugee, claims: list[ClaimJugee], *,
                                  noeuds_du_cas: set[str]) -> Applicable:
    """L'applicabilité de la garantie principale du nœud d'une clause d'**étendue** (L1m).

    Une clause qui écrit « la perte d'eau subie à l'occasion d'un sinistre **couvert** est prise en
    charge à concurrence de 1.000 € » ne pose aucune condition au client : elle étend la garantie de
    sa propre section, et elle joue exactement quand celle-ci joue. Son applicabilité est donc
    **empruntée** aux garanties du même nœud qui, elles, disent à quelles conditions le contrat
    couvre — jamais calculée sur ses propres champs, qui ne portent que la référence circulaire.

    `oui` dès qu'une garantie du nœud s'applique, `humain` si l'une reste ouverte, `non` si toutes
    sont écartées. Aucune garantie principale au nœud ⇒ `humain` : rien n'établit alors que le
    sinistre est couvert, et c'est le seul cas où la clause d'étendue reste une question d'humain.
    Les clauses d'étendue ne s'empruntent rien entre elles — sans quoi deux d'entre elles se
    porteraient mutuellement.
    """
    noeuds = {clause.node_id for clause in claim.clauses if clause.node_id}
    valeurs = [
        applicable_de_claim(autre, noeuds_du_cas=noeuds_du_cas)
        for autre in claims
        if autre.claim_id != claim.claim_id and autre.kind == "garantie"
        and not (autre.champs is not None and autre.champs.reference_a_la_couverture)
        and any(clause.node_id in noeuds for clause in autre.clauses)]
    valeurs = [v for v in valeurs if v is not None]
    if not valeurs:
        return "humain"
    if "oui" in valeurs:
        return "oui"
    return "humain" if "humain" in valeurs else "non"


def _soeurs_denumeration_ouvertes(garantie: ClaimJugee, claims: list[ClaimJugee], *,
                                  etat: dict[str, Applicable | None]) -> list[ClaimJugee]:
    """Les garanties **sœurs** de celle-ci, restées ouvertes — items de la même énumération (L1q).

    Story 5.7 (L1q). Mesuré en prod le 04/09/2026 sur les plaques à induction : le modèle cite
    `3.1.1.1.1 L'incendie` **et** `3.1.1.1.6 Les dégâts occasionnés … par un événement soudain`, deux
    des six périls que `p34:6` annonce (« contre les périls suivants : »). L'incendie porte la
    couverture ; le sixième item exige un caractère soudain que les faits n'établissent pas et reste
    `humain`. La règle (3) refusait alors de trancher — « aucune claim retenue `humain` » —, et le
    verdict tombait en `ne_tranche_pas` : une garantie établie était rétrogradée par les qualités
    manquantes d'une garantie **citée en surplus**.

    Un contrat qui assure « contre les périls suivants » énumère des voies **alternatives** : qu'un
    second péril ne soit pas établi ne retire rien au premier. La sororité se lit donc sur l'amorce
    partagée (`ClauseCitee.amorce_de`), c'est-à-dire sur la structure du document, jamais sur une
    proximité de titres ou de numéros — et c'est ce qui la garde étroite : une garantie qui répond à
    une **autre** sous-question vit ailleurs dans l'arbre, sous une autre amorce, et continue de
    peser sur le verdict comme avant.

    Elles ne disparaissent pas pour autant : la clause sœur reste affichée avec son statut, ses
    qualités restent des questions au client (`_libelles_manquants`), et le verdict que l'appelant en
    tire est `sous_conditions` — jamais `couvert`. Une garantie s'applique, quelque chose reste
    ouvert, et la réponse le dit.
    """
    amorces = {clause.amorce_de for clause in garantie.clauses_decisionnelles if clause.amorce_de}
    if not amorces:
        return []
    return [autre for autre in claims
            if autre.claim_id != garantie.claim_id and autre.kind == "garantie"
            and etat.get(autre.claim_id) == "humain"
            and any(clause.amorce_de in amorces for clause in autre.clauses_decisionnelles)]


def applicabilites_des_claims(
        claims: list[ClaimJugee]) -> dict[str, tuple[Applicable | None, ApplicableReason | None]]:
    """Statut et raison issus du même état de portée que la table AD-6.

    Le calcul est par claim affichée. Seule une garantie affichée fixe les nœuds contractuels du
    cas ; sans elle, une exclusion reste humaine sauf si les faits typés suffisent à la contredire.

    C'est aussi le seul endroit où une claim est jugée **avec les autres** : une clause d'étendue,
    qui ne subordonne son effet qu'au fait que le sinistre soit couvert, n'a rien à dire d'elle-même
    et emprunte son applicabilité à la garantie principale de son nœud (story 5.7, L1m).
    """
    out: dict[str, tuple[Applicable | None, ApplicableReason | None]] = {}
    for claim in claims:
        case_nodes = _noeuds_du_cas(claims)
        applicable_sans_portee = applicable_de_claim(claim, noeuds_du_cas=set())
        # D3 : **sans garantie affichée, une exclusion ne conclut jamais.** Une affirmation isolée
        # qui rattache une circonstance déclarée au vocabulaire d'une exclusion — « une bougie tombée
        # sur le canapé est une action subite de la chaleur » — n'a, à elle seule, montré aucune
        # branche du contrat : rien ne dit que la clause citée est celle qui régit ce sinistre, et
        # « Exclu » y serait une conclusion tirée d'une seule phrase. Le garde-fou tenait la porte
        # que L1r ouvrait au rattachement (règle 3bis) ; L1u a retiré la porte, il reste la règle.
        if claim.kind == "exclusion" and not case_nodes and applicable_sans_portee != "non":
            applicable = "humain"
        else:
            applicable = applicable_de_claim(claim, noeuds_du_cas=case_nodes)
        if (applicable is not None and claim.kind == "garantie" and claim.champs is not None
                and claim.champs.reference_a_la_couverture):
            # L1m : une clause d'étendue suit la garantie principale de son nœud, sans jamais la
            # dépasser. Le minimum garde ce que ses **autres** exigences disent encore d'elle (une
            # portée absente, un qualificatif non établi) : la référence à la couverture est retirée
            # du calcul, elle n'y est pas remplacée par un blanc-seing.
            suivi = _garantie_principale_du_noeud(claim, claims, noeuds_du_cas=case_nodes)
            applicable = min((applicable, suivi), key=lambda v: _FORCE[v])
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
    (1)   exclusion `oui` dont la portée couvre les nœuds du cas, **ou** dont le rattachement
          soutenu énonce un fait de la déclaration (L1r) ⇒ `non_couvert` — une exclusion n'atteint
          plus `oui` par son seul rattachement depuis L1u, cette seconde branche ne dit donc plus
          que *où* s'applique une exclusion que ses champs typés ont établie ;
    (2)   garantie `oui` **et** (condition / franchise / exclusion `humain`, garantie hors socle,
          **condition d'applicabilité de sa section non établie**, ou **garantie sœur de la même
          énumération restée ouverte**) ⇒ `sous_conditions` — politique conservatrice ;
    (2bis) garantie `humain` **par** option ou conditions particulières ⇒ `sous_conditions` : c'est
          le « dépend d'une option / CP inconnue » d'AD-6 vu depuis la **clause**, qu'une garantie
          `oui` ne peut pas exprimer (une garantie qui dépend d'une option est `humain` par
          construction, règle (6) de `applicable_de_claim`) ;
    (3)   garantie du socle `oui`, aucune claim retenue `humain`, **et chaque sous-question posée
          portée par une affirmation retenue** ⇒ `couvert` ;
    (4)   sinon ⇒ `ne_tranche_pas`.

    **Ce que le verdict laisse encore à demander (story 5.7, L1t).** La table décidait, et les
    questions étaient composées avant elle, donc sans elle. Un dossier `non_couvert` posait alors les
    conditions de la garantie que l'exclusion venait d'écarter — le dépôt de plainte, la souscription
    de la garantie vol —, questions auxquelles aucune réponse ne pouvait rien rouvrir. La règle (1)
    est donc calculée d'abord (`exclusion_decisive`) : quand elle tient, les seules questions dues
    sont celles qui pourraient **renverser** le verdict — celles de l'exclusion qui le fonde — plus le
    paquet contractuel, dû quel que soit le verdict parce qu'il dit ce que la lecture n'a pas lu. Et
    ce qu'un rattachement retenu a établi n'est plus demandé nulle part
    (`faits_etablis_par_rattachement`), quel que soit le verdict.

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
    contradiction = any(c.contredit for c in retenues)
    renvoi = any(c.renvoi_ouvert for c in retenues if c.clauses)
    fondatrices = [c for c in retenues if c.kind in KINDS_FONDATEURS]
    # Story 5.7 (L1t) : la règle (1) est **lue avant les questions**, parce que c'est elle qui dit
    # lesquelles ont encore un objet. Les questions restantes sont celles qui pourraient renverser le
    # verdict — celles de l'exclusion qui le fonde, qu'un « non » contredirait — plus le paquet
    # contractuel, dû quel que soit le verdict. Les conditions de la garantie écartée (le dépôt de
    # plainte, la souscription de la garantie vol) ne sont plus posées : l'exclusion s'applique, elles
    # n'ont plus d'objet, et y répondre ne pouvait rien rouvrir. Aucune règle antérieure n'est
    # déplacée — l'exclusion décisive n'existe que si (0) et (0bis) ont laissé passer.
    decisive = (None if contradiction or renvoi or not fondatrices
                else exclusion_decisive(retenues, etat=etat))
    interrogeables = [decisive] if decisive is not None else retenues
    etablis = faits_etablis_par_rattachement(retenues, etat=etat)
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
    # L1t : elles restent lues par la table (`conditions_ouvertes`, règles 2 et 3) et ne sont plus
    # **demandées** sous une exclusion qui conclut — la garantie qu'elles subordonnent est écartée.
    questions_section = [] if decisive is not None else _questions_de_section(sections_ouvertes)
    manquants = _libelles_manquants(
        interrogeables, etat=etat, place=ask_client_max - len(paquet) - len(questions_section),
        etablis=etablis)
    missing_final = connu.model_copy(update={"faits": manquants})
    ask = (paquet
           + questions_section
           + [f"Fait à établir auprès du client : {libelle}" for libelle in manquants]
           + [f"Qualité exigée par une clause citée, à faire confirmer par le client : {libelle}"
              for libelle in _qualites_a_confirmer(interrogeables, deja=manquants)])[:ask_client_max]
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

    if not fondatrices:
        if retenues:
            return verdict("ne_tranche_pas", "Des passages ont été retrouvés et affichés, mais "
                                             "aucun n'est confirmé comme garantie ou exclusion "
                                             "fondatrice")
        return verdict("ne_tranche_pas", "Aucun passage n'a été retenu et affiché")

    garanties = [c for c in retenues if c.kind == "garantie"]

    # (1) — l'exclusion prime, à condition qu'elle couvre le cas : par sa portée déclarée (AD-2,
    # `scope_nodes`) ou par son **rattachement** aux faits (L1r).
    #
    # La portée seule ne suffisait pas à la lire. `Block.scope_node_id` d'une exclusion est le nœud
    # de l'exclusion elle-même — « 3.1.6.2.1 les vols simples » —, jamais le nœud de la garantie
    # qu'elle écarte : l'intersection avec les nœuds du cas était vide sur les cinq exclusions de la
    # batterie du 03/09/2026, y compris les exclusions communes du socle. Un rattachement soutenu qui
    # nomme un fait déclaré dans les mots de la clause dit la même chose que la portée, en plus
    # direct : cette clause-là a rencontré ce sinistre-là.
    #
    # L1t : la raison le dit en clair. « Une exclusion applicable couvre le cas décrit » laissait le
    # lecteur devant une page qui affichait « Exclu » et lui demandait au-dessous d'établir le dépôt
    # de plainte : rien ne disait que ces conditions-là étaient devenues sans objet.
    if decisive is not None:
        return verdict("non_couvert", "Une exclusion applicable couvre le cas décrit : les "
                                      "conditions de la garantie n'ont plus d'objet")

    ouvertes = [c for c in retenues
                if c.kind in ("condition", "franchise", "exclusion") and etat[c.claim_id] == "humain"]
    for garantie in garanties:
        if etat[garantie.claim_id] != "oui":
            continue
        # « … ou d'une **extension** » (AD-6, règle 2) : le nœud le dit, pas le modèle.
        hors_socle = any(not clause.socle for clause in garantie.clauses)
        conditionnee = _conditions_de(garantie, conditions_ouvertes)
        # L1q : et les garanties **sœurs** de celle-ci — les autres items de la même énumération de
        # périls — restées ouvertes. Elles n'ajoutent aucune exigence à la garantie qui s'applique
        # (ce sont des voies alternatives), mais elles ne s'effacent pas non plus : elles ouvrent le
        # verdict comme le ferait une condition, au lieu de le faire tomber en `ne_tranche_pas` par
        # la règle (3).
        soeurs = _soeurs_denumeration_ouvertes(garantie, retenues, etat=etat)
        # (2) — politique conservatrice : un seul de ces quatre motifs suffit à ouvrir le verdict.
        if ouvertes or hors_socle or conditionnee or soeurs:
            motifs = []
            if ouvertes:
                motifs.append("une condition, une franchise ou une exclusion citée reste ouverte")
            if hors_socle:
                motifs.append("la garantie ne relève pas du socle commun")
            if soeurs:
                motifs.append("une autre garantie de la même énumération, citée en surplus, reste "
                              "ouverte sans retirer quoi que ce soit à celle qui s'applique")
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
