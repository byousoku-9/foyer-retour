"""AD-6 — Verdict « au regard des conditions générales seules », décidé par table.

Ce module est **du code pur** : il ne connaît ni le modèle, ni le corpus, ni les étapes. AD-6 confie
au modèle l'extraction de *valeurs typées* et au code la décision — le découpage d'exécution est donc
littéral :

(a) `applicable_de_claim()` dérive `ClaimStatus.applicable ∈ {oui, non, humain}` d'une claim retenue,
    à partir du typage des blocs qu'elle cite (`Block.kind`, seule source de typage) et des quatre
    champs typés rendus par l'unique appel `micro` de *vérifier* ;
(b) `decider()` applique la table exclusive d'AD-6 aux claims **affichées** et compose le `Verdict`
    — sa valeur, sa raison, le paquet manquant, les questions à poser, les points à escalader.

Rien de ce que le modèle rend n'est une décision : il dit si le fait exigé par la clause est présent,
si une option ou des conditions particulières conditionnent la clause, et quel fait lui manque. Le
seul texte du modèle qui traverse jusqu'à l'utilisateur est le libellé d'un `fait_manquant`, borné et
dédupliqué par l'appelant, et il n'entre jamais dans `Answer.texte`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .document import DomainModel

VerdictValue = Literal["couvert", "non_couvert", "sous_conditions", "ne_tranche_pas"]
Applicable = Literal["oui", "non", "humain"]

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


def applicable_de_claim(claim: ClaimJugee) -> Applicable | None:
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


def _libelles_manquants(claims: list[ClaimJugee], *, place: int) -> list[str]:
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
    """
    out: list[str] = []
    for claim in claims:
        if claim.champs is None:
            continue
        for libelle in [(claim.champs.fait_manquant or "").strip(),
                        *claim.champs.qualites_non_etablies]:
            if libelle and libelle not in out:
                out.append(libelle)
    return out[:max(place, 0)]


def _qualites_a_confirmer(claims: list[ClaimJugee]) -> list[str]:
    """Les qualités qu'une clause exige et que le modèle a dites **établies** — à confirmer quand même.

    AC de la story : « `ask_client` mentionne les options/CP **et la nature « subite »** ». Le run réel
    du 24/08 a montré que rien ne le garantissait : le modèle a déclaré la qualité subite établie par
    des faits qui disent le contraire, `fait_manquant` est resté nul, et aucune question ne l'a
    mentionnée (revue Codex 1.8, B3). Le code ne peut pas juger si « soudain » est établi ; il peut en
    revanche poser la question **à chaque fois que la clause l'exige**, quelle qu'ait été la réponse du
    modèle. Un verdict « au regard des conditions générales seules » ne prouve de toute façon aucune
    qualité de l'événement : elle se confirme auprès du client.

    Les qualités **non** établies partent, elles, dans `missing.faits` — ce sont des faits à établir,
    pas des faits à confirmer, et le paquet manquant les annonce.
    """
    out: list[str] = []
    for claim in claims:
        if claim.champs is None:
            continue
        for libelle in claim.champs.qualites_exigees:
            if libelle and libelle not in claim.champs.qualites_non_etablies and libelle not in out:
                out.append(libelle)
    return out


def _questions_du_paquet(claims: list[ClaimJugee], missing: MissingPackage) -> list[str]:
    """Ce qu'il faut demander au client parce que le verdict ne lit que les conditions générales.

    Une question par pièce manquante d'AD-6, et rien qui dépende du modèle : un verdict rendu « au
    regard des conditions générales seules » ignore *par construction* les options souscrites, les
    conditions particulières, les avenants et la date d'effet. Les deux premières se **précisent**
    quand une clause citée en dépend explicitement (booléen typé), ce qui les fait passer d'une
    diligence à un préalable. La troisième couvre les deux pièces restantes : les annoncer manquantes
    dans `missing` sans jamais les demander laissait le gestionnaire devant quatre pièces absentes et
    deux questions (revue 1.8).
    """
    out: list[str] = []
    if missing.options_souscrites:
        question = "Quelles options et extensions ont été souscrites ?"
        if any(c.champs is not None and c.champs.option_requise for c in claims):
            question += " Une clause citée ne joue qu'à cette condition."
        out.append(question)
    if missing.conditions_particulieres:
        question = "Que prévoient les conditions particulières (montants, franchises, biens désignés) ?"
        if any(c.champs is not None and c.champs.cp_requise for c in claims):
            question += " Une clause citée y renvoie."
        out.append(question)
    if missing.avenants or missing.date_effet:
        out.append("À quelle date le contrat a-t-il pris effet, et un avenant l'a-t-il modifié depuis ? "
                   "Le sinistre doit tomber dans la période garantie, dans la version alors en vigueur.")
    return out


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


def _noeuds_du_cas(claims: list[ClaimJugee], *, hors: set[str]) -> set[str]:
    """Les nœuds du contrat que le cas met en jeu (D3).

    Les nœuds parents des blocs cités par les claims `garantie` retenues ; à défaut de garantie, ceux
    de **toutes** les claims retenues, moins les blocs de l'exclusion testée — une exclusion ne
    s'auto-couvre pas, sans quoi toute exclusion citée serait « applicable au cas » par le seul fait
    d'avoir été citée.
    """
    garanties = {clause.node_id for c in claims if c.kind == "garantie"
                 for clause in c.clauses if clause.node_id}
    if garanties:
        return garanties
    return {clause.node_id for c in claims for clause in c.clauses
            if clause.node_id and clause.block_id not in hors}


def decider(claims: list[ClaimJugee], *, ask_client_max: int,
            missing: MissingPackage | None = None) -> Verdict:
    """Découpage (b) d'AD-6 : la table exclusive, dans l'ordre, sur les claims **affichées** (D4).

    (0)   contradiction non résolue entre deux claims retenues, ou renvoi non résolu sur une claim
          décisionnelle ⇒ `ne_tranche_pas`, les deux passages restant affichés (AD-6) ;
    (0bis) aucune claim affichée de kind `garantie` ou `exclusion` ⇒ `ne_tranche_pas` : la table ne
          tranche que sur elles, et un verdict sans clause fondatrice serait une opinion ;
    (1)   exclusion `oui` dont la portée couvre les nœuds du cas ⇒ `non_couvert` ;
    (2)   garantie `oui` **et** (condition / franchise / exclusion `humain`, ou garantie hors socle,
          **ou paquet manquant non établi**) ⇒ `sous_conditions` — politique conservatrice ;
    (2bis) garantie `humain` **par** option ou conditions particulières ⇒ `sous_conditions` : c'est
          le « dépend d'une option / CP inconnue » d'AD-6 vu depuis la **clause**, qu'une garantie
          `oui` ne peut pas exprimer (une garantie qui dépend d'une option est `humain` par
          construction, règle (6) de `applicable_de_claim`) ;
    (3)   garantie du socle `oui`, aucune claim retenue `humain`, **paquet établi** ⇒ `couvert` ;
    (4)   sinon ⇒ `ne_tranche_pas`.

    **`missing` est une entrée, et il décide (revue 1.8 tour 2 ; maintenu contre la revue Codex 1.8,
    B1 — voir la mesure ci-dessous).** AD-6 écrit la seconde branche de la règle (2) « ou la garantie
    dépend d'une option / extension / **condition particulière inconnue** », et il définit
    `MissingPackage` dans la même phrase comme l'objet qui dit si elles le sont. Tant que l'appelant
    ne fournit pas le dossier, aucune garantie ne peut être tenue pour acquise : la branche est
    satisfaite, et `couvert` est hors d'atteinte. C'est ce que veut dire « au regard des conditions
    générales seules », et c'est pourquoi l'AC du cas témoin n'admet que
    `{sous_conditions, ne_tranche_pas}`.

    **Ce que la mesure dit, et pourquoi la lecture « par clause » ne suffit pas.** La revue Codex 1.8
    (B1) demande de lire la branche sur la seule clause (`option_requise` / `cp_requise` / hors socle)
    et de laisser `couvert` sortir d'une garantie du socle `oui`. Essayé, et joué en vrai sur le cas
    bougie (run du 24/08, `tests/llm_fixtures/test_sinistre_live.*.json`) : le modèle a énuméré les
    trois qualités que la clause exige — « caractère soudain de l'événement », « action subite de la
    chaleur », « contact direct et immédiat avec un foyer » — et les a **toutes trois** déclarées
    établies par des faits qui disent le contraire (« sans embrasement ni commencement d'incendie »).
    Verdict rendu : `couvert`, la valeur que l'AC de la story interdit sur ce cas. Aucune règle de code
    ne peut trancher, sur du texte libre, si un événement fut « soudain » : seule une politique le
    peut, et AD-6 en désigne une, annotée « **politique conservatrice, décision Lancelot** ».

    **La règle (3) n'est pas morte pour autant, et elle est atteignable par le pipeline** : `run()`
    accepte un `dossier` (`MissingPackage` renseigné à `False`), le passe jusqu'ici, et `couvert`
    sort — c'est la fixture « garantie du socle » que l'AC exige, jouée de bout en bout. Ce qui
    manquait au tour 2 n'était pas la règle mais le chemin d'entrée. Seuls les `faits[]` sont ignorés
    de ce qu'on reçoit : ils sont dérivés des libellés rendus par le modèle.

    `applicable` est relu sur chaque claim par `applicable_de_claim()` : la table ne dépend d'aucun
    champ que l'appelant aurait pu remplir autrement.
    """
    connu = (missing or MissingPackage()).model_copy(deep=True)
    retenues = [c for c in claims if c.retenue]
    etat = {c.claim_id: applicable_de_claim(c) for c in retenues}
    # Les questions du paquet manquant d'abord : elles ne dépendent d'aucune sortie du modèle et
    # elles sont dues quel que soit le verdict — mais seulement pour les pièces réellement absentes.
    # Ce qu'elles laissent de place borne alors les libellés du modèle, si bien que `missing.faits` et
    # `ask_client` disent la même chose.
    paquet = _questions_du_paquet(retenues, connu)
    manquants = _libelles_manquants(retenues, place=ask_client_max - len(paquet))
    missing_final = connu.model_copy(update={"faits": manquants})
    ask = (paquet
           + [f"Fait à établir auprès du client : {libelle}" for libelle in manquants]
           + [f"Qualité exigée par une clause citée, à faire confirmer par le client : {libelle}"
              for libelle in _qualites_a_confirmer(retenues)])[:ask_client_max]
    contradiction = any(c.contredit for c in retenues)
    renvoi = any(c.renvoi_ouvert for c in retenues if c.clauses)
    escalate = _escalades(retenues, contradiction=contradiction, renvoi=renvoi)

    def verdict(value: VerdictValue, reason: str) -> Verdict:
        return Verdict(value=value, reason=f"{reason} ({PORTEE})",
                       missing=missing_final.model_copy(deep=True), ask_client=ask, escalate=escalate)

    # (0) — ni une contradiction ni un renvoi ouvert ne se tranchent par du code.
    if contradiction:
        return verdict("ne_tranche_pas", "Deux clauses citées se contredisent et rien dans les "
                                         "conditions générales ne les départage")
    if renvoi:
        return verdict("ne_tranche_pas", "Une clause décisionnelle renvoie à un passage que "
                                         "l'ingestion n'a pas résolu")

    fondatrices = [c for c in retenues if c.kind in KINDS_FONDATEURS]
    if not fondatrices:
        return verdict("ne_tranche_pas", "Aucune garantie ni exclusion n'a été retrouvée et affichée")

    exclusions = [c for c in retenues if c.kind == "exclusion"]
    garanties = [c for c in retenues if c.kind == "garantie"]

    # (1) — l'exclusion prime, à condition que sa portée couvre le cas (AD-2, `scope_nodes`).
    for exclusion in exclusions:
        if etat[exclusion.claim_id] != "oui":
            continue
        hors = {clause.block_id for clause in exclusion.clauses}
        cas = _noeuds_du_cas(retenues, hors=hors)
        if any(clause.portee & cas for clause in exclusion.clauses):
            return verdict("non_couvert", "Une exclusion applicable couvre le cas décrit")

    ouvertes = [c for c in retenues
                if c.kind in ("condition", "franchise", "exclusion") and etat[c.claim_id] == "humain"]
    # « La garantie dépend d'une option / extension / condition particulière **inconnue** » (AD-6,
    # règle 2), lu sur l'objet qu'AD-6 définit pour le dire : tant que les conditions particulières ou
    # les options souscrites ne sont pas au dossier, aucune garantie ne peut être tenue pour acquise,
    # quoi qu'en dise la clause. L'appelant qui **a** ces pièces les passe (`dossier`), et la règle (3)
    # reprend la main.
    pieces_inconnues = [libelle for libelle, absente in
                        (("les conditions particulières", connu.conditions_particulieres),
                         ("les options souscrites", connu.options_souscrites)) if absente]
    for garantie in garanties:
        if etat[garantie.claim_id] != "oui":
            continue
        # « … ou d'une **extension** » (AD-6, règle 2) : le nœud le dit, pas le modèle.
        hors_socle = any(not clause.socle for clause in garantie.clauses)
        # (2) — politique conservatrice : un seul de ces trois motifs suffit à ouvrir le verdict.
        if ouvertes or hors_socle or pieces_inconnues:
            motifs = []
            if ouvertes:
                motifs.append("une condition, une franchise ou une exclusion citée reste ouverte")
            if hors_socle:
                motifs.append("la garantie ne relève pas du socle commun")
            if pieces_inconnues:
                motifs.append(f"{' et '.join(pieces_inconnues)} ne sont pas au dossier — la garantie "
                              "ne peut pas être tenue pour acquise")
            return verdict("sous_conditions", "Une garantie s'applique, mais " + " ; ".join(motifs))

    # (2bis) — la garantie **elle-même** dépend d'une option ou de conditions particulières inconnues.
    for garantie in garanties:
        champs = garantie.champs
        if etat[garantie.claim_id] == "humain" and champs is not None and (champs.option_requise
                                                                           or champs.cp_requise):
            return verdict("sous_conditions", "La garantie citée ne joue que si une option ou les "
                                              "conditions particulières la prévoient")

    # (3) — le seul chemin vers `couvert` : garantie du socle, plus rien d'ouvert **nulle part**, et
    # le dossier au complet (sans quoi la règle (2) a déjà tranché plus haut). Le `oui` qui l'ouvre est
    # doublement corroboré : par le dossier, et par `applicable_de_claim()`, qui exige que toutes les
    # qualités exigées par la clause soient établies par les faits déclarés (B3).
    for garantie in garanties:
        if etat[garantie.claim_id] == "oui" and all(clause.socle for clause in garantie.clauses):
            if not any(etat[c.claim_id] == "humain" for c in retenues):
                return verdict("couvert", "Une garantie du socle commun s'applique et aucune clause "
                                          "citée ne reste ouverte")

    # (4) — reste de la table : aucune règle n'a tranché.
    return verdict("ne_tranche_pas", "Aucune règle de la table ne tranche sur les clauses retrouvées")
