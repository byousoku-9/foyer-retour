"""AD-5 / AD-7 — Chargement en lecture seule de `data/dictionary.json`, et les deux verrous qu'il arme.

**Deux verrous distincts, parce que deux risques distincts.**

- `corpus_ok` — les `corpus_source_hashes` du fichier décrivent-ils le document **auquel il sera
  appliqué** ? — commande l'emploi des **variantes** par *retrouver*. Élargir la recherche ne fait
  qu'ajouter des candidats, et chaque phrase affichée reste vérifiée contre le corpus (AD-3) ; mais
  un dictionnaire qui décrit un *autre* corpus ne dit rien de celui-ci, et ses variantes ouvriraient
  des fiches au hasard.

  **Le verrou nomme le document, il ne se contente pas d'en trouver un** (revue Codex 2.1, B3). La
  règle était « chaque empreinte déclarée correspond à un document servi, et au moins une » : un
  fichier ne déclarant que l'empreinte du **contrat AXA** passait donc `corpus_ok`, était publié
  armé par `/sante`, élargissait la recherche du **guide** et — signé — y armait le refus « zéro
  hit », sur un vocabulaire qui ne décrit pas le guide. `load_dictionary` reçoit désormais le
  `doc_id` que le pipeline lui appliquera (`settings.guide_doc_id`), l'exige parmi les empreintes,
  et le porte : `utilisable_pour(doc_id)` / `court_circuit_pour(doc_id)` refusent tout autre
  document — le jour où un contrat aura son dictionnaire, ce sera un autre objet, pas celui-ci.
- `validated ∧ corpus_ok` — `court_circuit_actif` — commande le **court-circuit** d'AD-5. Ce que la
  signature humaine garde, c'est le *refus* : une affirmation négative, visible, irréversible pour
  celui qui la reçoit. AD-5 ne désarme littéralement que celui-là (« si `validated=false` ou
  `corpus_source_hashes` ne correspond pas au corpus chargé, le court-circuit « zéro hit » est
  **désactivé** … et la requête poursuit vers *retrouver* »).

**Un troisième contenu, qui n'arme rien** (story 2.5) : `intents` — les déclencheurs de chacune des
trois intentions refusées. Ils ne sont commandés par aucun des deux verrous, parce qu'ils ne
commandent rien : AD-5 interdit qu'une présence lexicale tranche (« la présence d'un mot n'est jamais
une preuve de pertinence »), et `confirme()` ne rend qu'un **compte**, celui des déclencheurs qui
corroborent l'intention déjà rendue par *comprendre*. C'est ce qui fait passer ce champ de « donnée
générée que rien ne lit » à « explication publiée », sans lui donner le moindre pouvoir de décision.

**Rien ici ne lève jamais.** AD-7 : « un fichier absent, illisible ou non conforme désactive une
optimisation, il n'empêche jamais de servir et ne lève jamais au démarrage. » Toute erreur devient une
`raison`, que `/api/v1/sante` publie en alerte et que la page d'accueil affiche — dite, jamais tue
(AD-16).

`corpus` n'importe que `domain` et la stdlib (jamais pydantic en direct) : la validation passe par
`DictionaryFile.model_validate`, et le message d'erreur par `_first_error()`, exactement comme
`loader.py` le fait pour `Document`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from server.app.domain.dictionary import DICTIONARY_FILE, DictionaryFile

from .index import words
from .loader import Corpus, _first_error
from .racine import Lecture, lecture_de
from .text import normalize


def forme(texte: str) -> str:
    """Forme normalisée d'un terme, **exactement** celle qu'`Index.chercher` compare.

    Un terme ne se compare que par `normalize()` puis `words()` (Code Map de la story 2.1) : les deux
    tables — celle du dictionnaire et celle de l'index — doivent produire la même clé, sinon une
    variante indexée ici ne trouverait rien là-bas et le compte de variantes annoncé à l'utilisateur
    dans `AbsenceProof.variants_count` serait un chiffre que rien n'a cherché.
    """
    return " ".join(words(normalize(texte)))


@dataclass
class Dictionnaire:
    """Objet d'exécution : l'état du dictionnaire, et l'élargissement qu'il sait faire.

    Le défaut est le dictionnaire **inerte** — celui d'un fichier absent : rien n'est chargé, rien
    n'est validé, `expand` rend les termes inchangés. C'est ce que le serveur utilise tant que
    l'ingestion n'a pas tourné, et c'est ce qui rend `dictionnaire=None` inutile ailleurs.
    """

    charge: bool = False
    # Le document auquel ce dictionnaire s'applique — celui que l'appelant a exigé au chargement, et
    # le seul que `utilisable_pour` reconnaisse. Vide pour le dictionnaire inerte.
    doc_id: str = ""
    validated: bool = False
    validated_by: str = ""
    validated_at: str = ""
    corpus_ok: bool = False
    raison: str = ""
    canoniques: int = 0
    # forme normalisée (canonique **ou** variante) → toutes les formes du groupe, ordre stable.
    # Indexer aussi les variantes est la moitié utile d'AD-5 : *comprendre* rend des termes
    # « toujours en français » que le guide peut ne pas employer — c'est en les reconnaissant comme
    # variantes qu'on retrouve la fiche du canonique.
    _groupes: dict[str, tuple[str, ...]] = field(default_factory=dict, repr=False)
    # `intent` → ses déclencheurs, dans l'ordre du fichier, dédupliqués par forme normalisée
    # (story 2.5). **Ils n'ont aucun pouvoir de décision** : AD-5 est explicite — « les déclencheurs
    # d'intention sont distincts des mots du corpus — la présence d'un mot n'est jamais une preuve de
    # pertinence ». Le refus par intent reste celui de *comprendre*, qui a lu la question entière ;
    # ce que le pipeline en tire est un **compte**, pour dire d'où vient un refus (`CheckResult`
    # `intention_expliquee`), jamais pour en prononcer un.
    #
    # Dédupliqués à la lecture pour que `confirme()` rende deux nombres qui parlent du même ensemble :
    # « 2 sur 30 » n'a de sens que si les 30 sont 30 formes distinctes.
    _intents: dict[str, tuple[str, ...]] = field(default_factory=dict, repr=False)
    # forme normalisée → les **canoniques** (texte du fichier, non normalisé) dont elle relève.
    # AD-4 veut `terms_searched[] (canoniques)` : c'est cette table qui le rend possible, et c'est la
    # seule chose du dictionnaire qui ait le droit de sortir dans une `AbsenceProof` — jamais les
    # variantes, jamais les déclencheurs.
    _canoniques: dict[str, tuple[str, ...]] = field(default_factory=dict, repr=False)

    @property
    def utilisable(self) -> bool:
        """Le fichier est chargé **et** décrit le corpus servi : ses variantes valent quelque chose."""
        return self.charge and self.corpus_ok

    @property
    def court_circuit_actif(self) -> bool:
        """AD-5 : le refus « zéro hit » n'est armé que par une signature humaine sur le bon corpus.

        C'est le booléen **d'état**, celui que `/api/v1/sante` publie et que la page d'accueil
        écrit : il parle du document que ce dictionnaire décrit. La décision par requête passe par
        `court_circuit_pour(doc_id)`, qui ajoute « et c'est bien ce document-là qu'on interroge ».
        """
        return self.utilisable and self.validated

    def utilisable_pour(self, doc_id: str | None) -> bool:
        """Les variantes valent-elles quelque chose **pour ce document-ci** ? (revue Codex 2.1, B3)

        `doc_id=None` — une recherche sur tout le corpus — ne reconnaît aucun dictionnaire : rien ne
        dit que les autres documents sont ceux qu'il décrit, et élargir la recherche d'un contrat
        avec le vocabulaire d'un guide d'installation ouvrirait des fiches au hasard.
        """
        return self.utilisable and doc_id is not None and doc_id == self.doc_id

    def court_circuit_pour(self, doc_id: str | None) -> bool:
        """AD-5, par requête : signé, décrivant le corpus servi, **et** appliqué à son document."""
        return self.utilisable_pour(doc_id) and self.validated

    def expand(self, termes: list[str]) -> dict[str, list[str]]:
        """`{terme de la question: [variantes ajoutées]}` — la forme qu'`Index.chercher` accepte.

        Les **clés restent les termes de la question**, jamais les canoniques du dictionnaire : AD-4
        veut que `terms_searched` dise ce que *comprendre* a produit, et publier les clés du
        dictionnaire ferait fuir, terme par terme, une partie de ce qu'AD-4 interdit d'exposer.

        Une variante déjà égale (à la normalisation près) au terme cherché n'est pas « ajoutée » :
        elle ne changerait rien à la recherche et gonflerait le compte annoncé à l'utilisateur.
        Dictionnaire inutilisable ⇒ chaque terme sort seul : `chercher` fait alors exactement ce
        qu'il faisait avant cette story.

        Une forme partagée par deux canoniques élargit vers **les deux** (revue Codex 2.1, I1) —
        voir `load_dictionary`.
        """
        sortie: dict[str, list[str]] = {}
        for terme in termes:
            cle = forme(terme)
            groupe = self._groupes.get(cle, ()) if self.utilisable else ()
            sortie[terme] = [f for f in groupe if f != cle]
        return sortie

    def variants_count(self, termes: list[str]) -> int:
        """Nombre de formes **ajoutées** effectivement cherchées (AD-4), jamais leur liste.

        Distinctes et hors des termes de la question : deux termes qui partagent une variante ne la
        comptent qu'une fois, et une variante qui est déjà l'un des termes cherchés n'ajoute rien.
        """
        base = {forme(t) for t in termes} - {""}
        ajoutees: set[str] = set()
        for variantes in self.expand(termes).values():
            ajoutees |= {v for v in variantes if v and v not in base}
        return len(ajoutees)


    def canoniser(self, termes: list[str]) -> list[str]:
        """Les termes cherchés, rendus **canoniques** — ce qu'AD-4 nomme `terms_searched[] (canoniques)`.

        AD-4, mot pour mot : `AbsenceProof(…, terms_searched[] (canoniques), variants_count, …)` —
        « jamais la liste des variantes ni des déclencheurs d'intention ». Un terme que le
        dictionnaire reconnaît comme **variante** sort donc sous le canonique de son groupe : la
        preuve d'absence dit « voici les notions cherchées », pas « voici l'orthographe que vous avez
        employée ». Publier la variante telle quelle (revue Codex 2.1, B5) contredisait la
        parenthèse d'AD-4 — mesuré sur l'artefact livré : « Arbeitsamt », reconnu dans le groupe
        « ADEM », ressortait dans `terms_searched`.

        Ce qui ne fuit pas pour autant : les **variantes** restent invisibles (seul le canonique du
        groupe touché sort, et seulement pour un terme que l'utilisateur a effectivement fait
        chercher), et les déclencheurs d'intention ne sont lus par personne. Un terme inconnu du
        dictionnaire est son propre canonique et sort inchangé ; dictionnaire inutilisable ⇒ tous les
        termes sortent inchangés, comme avant cette story.

        Une forme ambiguë relève de plusieurs canoniques : ils sortent tous, dans l'ordre du fichier
        — c'est exactement l'ensemble des groupes que `expand` a fait chercher.
        """
        sortie: list[str] = []
        for terme in termes:
            cle = forme(terme)
            canons = self._canoniques.get(cle, ()) if self.utilisable else ()
            for candidat in (canons or (terme,)):
                if candidat not in sortie:
                    sortie.append(candidat)
        return sortie


    def declencheurs(self, intent: str) -> tuple[str, ...]:
        """Les déclencheurs que le fichier associe à cette intention, tels qu'il les écrit.

        Rendus dès que le fichier est **chargé**, sans attendre `corpus_ok` ni `validated`, et c'est
        délibéré : les deux verrous d'AD-5 gardent l'un les **variantes du corpus** (qui ouvrent des
        fiches), l'autre le **refus** (que l'utilisateur reçoit). Un déclencheur d'intention ne fait
        ni l'un ni l'autre — il ne décide rien, il compte —, et il ne décrit pas un corpus mais une
        façon de poser une question. Le désarmer au motif que le dictionnaire décrit un autre
        document reviendrait à cacher une explication sans rien protéger.

        Intention inconnue du fichier ⇒ tuple vide : le compte vaudra « 0 sur 0 », ce qui est vrai.
        """
        return self._intents.get(intent, ())

    def confirme(self, intent: str, texte: str) -> tuple[int, int]:
        """`(déclencheurs reconnus dans le texte, déclencheurs connus)` — un compte, jamais les mots.

        La reconnaissance est **exactement** celle d'`Index.chercher` : `normalize()` puis `words()`
        des deux côtés, et correspondance en **mots entiers** (`_hit`) — « météorologie » ne contient
        pas le déclencheur « météo », et « la Météo demain ? » contient « météo demain ». Deux règles
        de comparaison différentes dans le même serveur feraient annoncer un compte que rien d'autre
        ne saurait reproduire.

        Les accents et la casse sont neutralisés par `normalize()` (convention Texte du spine) : c'est
        ce qui permet à un déclencheur « météo » d'être reconnu dans « METEO » comme dans « meteo ».

        Ce que la fonction ne fait pas : elle ne rend jamais **quels** déclencheurs ont été reconnus.
        Un déclencheur reconnu dans une question est un fragment de cette question, et AD-4 interdit
        déjà de publier la liste des déclencheurs dans une preuve d'absence ; le seul chiffre suffit
        à dire si le refus est corroboré (AD-10 : le fait, jamais le texte).
        """
        declencheurs = self.declencheurs(intent)
        if not declencheurs:
            return 0, 0
        mots = words(normalize(texte))
        tokens = frozenset(mots)
        rembourre = f" {' '.join(mots)} "
        reconnus = 0
        for declencheur in declencheurs:
            f = forme(declencheur)
            if not f:
                continue
            if (f in tokens) if " " not in f else (f" {f} " in rembourre):
                reconnus += 1
        return reconnus, len(declencheurs)


def _corpus_ok(hashes: dict[str, str], corpus: Corpus, doc_id: str) -> tuple[bool, str]:
    """Les empreintes du dictionnaire décrivent-elles le document `doc_id` **tel qu'il est servi** ?

    Trois conditions, et la première est celle que la revue Codex 2.1 (B3) a ajoutée :

    1. `doc_id` — le document auquel ce dictionnaire sera appliqué — figure parmi les empreintes.
       Sans elle, « au moins une empreinte valide » suffisait : un fichier ne décrivant que le
       contrat AXA était déclaré conforme, puis employé sur le guide.
    2. chaque empreinte déclarée nomme un document **servi** ;
    3. chacune vaut le `source_hash` du manifest.

    Le dictionnaire ne couvre pas forcément tout le corpus — celui de cette story ne décrit que le
    guide — mais il doit couvrir celui dont on se sert.
    """
    if not hashes:
        return False, "corpus_source_hashes vide : le dictionnaire ne dit pas quel corpus il décrit"
    if doc_id not in hashes:
        return False, (f"corpus_source_hashes ne décrit pas {doc_id!r} (mais {sorted(hashes)}) : "
                       "ce dictionnaire parle d'un autre document que celui qu'il servirait")
    for autre, source_hash in sorted(hashes.items()):
        if autre not in corpus.documents:
            return False, f"corpus_source_hashes nomme {autre!r}, qui n'est pas servi"
        entree = corpus.manifest.get(autre)
        if entree is None or entree.source_hash != source_hash:
            return False, f"source_hash de {autre!r} différent de celui du manifest"
    return True, ""


def load_dictionary(data_dir: Path | str, corpus: Corpus, doc_id: str, *,
                    lecture: Lecture | None = None) -> Dictionnaire:
    """Dictionnaire du document → `Dictionnaire`. Absent ou invalide ⇒ inerte, jamais d'exception.

    `doc_id` est le document auquel ce dictionnaire sera **appliqué** (`settings.guide_doc_id` pour
    le serveur) : il est exigé parmi les `corpus_source_hashes` et porté par l'objet, de sorte que
    `utilisable_pour` / `court_circuit_pour` refusent tout autre document (revue Codex 2.1, B3).
    Le paramètre est obligatoire, et c'est le point : un appelant qui l'oublierait rouvrirait le trou.

    `lecture` est le **repère pincé** de l'opération de lecture qui englobe cet appel (N1, story
    4.5) : le dictionnaire servi et le corpus auquel il est opposé viennent alors de la même
    génération. Sans lui, un repère est pincé pour la durée de cet appel seul.
    """
    if lecture is None:
        with lecture_de(Path(data_dir)) as pincee:
            return load_dictionary(data_dir, corpus, doc_id, lecture=pincee)
    racine = Path(data_dir)
    document = corpus.documents.get(doc_id)
    # Le guide conserve le chemin historique public. Chaque contrat possède sa donnée lexicale :
    # deux assureurs ne partagent jamais leurs variantes par accident.
    chemin = (racine / DICTIONARY_FILE
              if document is None or document.kind == "guide"
              else racine / doc_id / DICTIONARY_FILE)
    if not lecture.fichier(chemin):
        return Dictionnaire(doc_id=doc_id,
                            raison=f"{DICTIONARY_FILE} absent : lancer "
                                   "`python -m server.ingest.enrich_dictionary`")
    try:
        brut = json.loads(lecture.reel(chemin).read_bytes())
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return Dictionnaire(doc_id=doc_id,
                            raison=f"{DICTIONARY_FILE} illisible : {_first_error(exc)}"[:500])
    try:
        fichier = DictionaryFile.model_validate(brut)
    except ValueError as exc:  # ValidationError en hérite ; pydantic n'est pas importé ici
        return Dictionnaire(doc_id=doc_id,
                            raison=f"{DICTIONARY_FILE} non conforme : {_first_error(exc)}"[:500])

    # L'applicabilité au corpus précède les contrôles qui supposent le document servi. Sans cet
    # ordre, un document absent ou en quarantaine a un ensemble de nœuds vide, et la première clé
    # candidate produit le faux diagnostic « nœud inexistant » au lieu de nommer l'absence du
    # document. `_corpus_ok` devient ainsi la cause primaire publiée par `/sante` (AD-7/AD-16).
    corpus_ok, raison = _corpus_ok(fichier.corpus_source_hashes, corpus, doc_id)

    # Le domaine contrôle la **forme** d'un identifiant sans importer le corpus. Quand le corpus est
    # applicable, le lecteur referme l'autre moitié : chaque question candidate doit viser une unité
    # répondante (`f…` ou `qN`) du document demandé, et cette unité doit réellement exister. Le
    # champ reste dormant jusqu'à la story 4.2 ; le valider aujourd'hui empêche précisément qu'une
    # donnée générée dérive en silence avant son premier lecteur.
    document = corpus.documents.get(doc_id)
    if corpus_ok and document is not None:
        noeuds = {node.node_id for node in document.nodes}
        for node_id in sorted(fichier.candidate_questions):
            document_nomme = node_id.split(":", 1)[0]
            if document_nomme != doc_id:
                return Dictionnaire(
                    doc_id=doc_id,
                    raison=(f"{DICTIONARY_FILE} non conforme : candidate_questions nomme "
                            f"{node_id!r}, qui appartient au document {document_nomme!r} et non "
                            f"au document demandé {doc_id!r}")[:500])
            if node_id not in noeuds:
                return Dictionnaire(
                    doc_id=doc_id,
                    raison=(f"{DICTIONARY_FILE} non conforme : candidate_questions nomme le nœud "
                            f"inexistant {node_id!r} dans le document {doc_id!r}")[:500])
    groupes: dict[str, list[str]] = {}
    canoniques: dict[str, list[str]] = {}
    for canonique, variantes in fichier.corpus.items():
        formes: list[str] = []
        for texte in (canonique, *variantes):
            f = forme(texte)
            if f and f not in formes:
                formes.append(f)
        if not formes:
            continue
        for f in formes:
            # **Une forme partagée par deux canoniques élargit vers les deux** (revue Codex 2.1, I1).
            # Elle gardait le premier groupe rencontré, pour ne pas mêler deux sens (« assurance » de
            # l'habitation et du véhicule) ; mais l'artefact livré porte 62 formes ambiguës, et les
            # variantes du second groupe devenaient inatteignables — donc une fiche qui existe restée
            # fermée, et, dictionnaire signé, un refus « zéro hit » sur une question que le guide
            # traite. C'est littéralement le « faux refus » qu'AD-5 dit prévenir, et le prix inverse
            # est nul : élargir n'affirme rien, `chercher` classe par nombre de groupes touchés, et
            # chaque phrase affichée reste vérifiée contre le corpus (AD-3). L'ordre reste celui du
            # fichier, que l'ingestion écrit trié : la réunion est déterministe.
            groupe = groupes.setdefault(f, [])
            groupe.extend(g for g in formes if g not in groupe)
            canons = canoniques.setdefault(f, [])
            if canonique not in canons:
                canons.append(canonique)
    # Story 2.5 : les déclencheurs d'intention, dédupliqués par forme normalisée et dans l'ordre du
    # fichier. Un doublon d'orthographe (« Météo » et « météo ») compterait deux fois dans le total
    # annoncé à l'utilisateur, et une seule dans les reconnus : le compte se contredirait lui-même.
    intents: dict[str, tuple[str, ...]] = {}
    for intent, mots in fichier.intents.items():
        gardes: list[str] = []
        formes_vues: set[str] = set()
        for mot in mots:
            f = forme(mot)
            if not f or f in formes_vues:
                continue
            formes_vues.add(f)
            gardes.append(mot)
        if gardes:
            intents[intent] = tuple(gardes)
    return Dictionnaire(
        charge=True, doc_id=doc_id, validated=fichier.validated,
        validated_by=(fichier.validated_by or ""), validated_at=(fichier.validated_at or ""),
        corpus_ok=corpus_ok, raison=raison if not corpus_ok else "", canoniques=len(fichier.corpus),
        _groupes={f: tuple(g) for f, g in groupes.items()},
        _intents=intents,
        _canoniques={f: tuple(c) for f, c in canoniques.items()})
