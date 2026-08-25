"""AD-5 / AD-7 — Chargement en lecture seule de `data/dictionary.json`, et les deux verrous qu'il arme.

**Deux verrous distincts, parce que deux risques distincts.**

- `corpus_ok` — les `corpus_source_hashes` du fichier décrivent-ils le corpus **servi** ? — commande
  l'emploi des **variantes** par *retrouver*. Élargir la recherche ne fait qu'ajouter des candidats,
  et chaque phrase affichée reste vérifiée contre le corpus (AD-3) ; mais un dictionnaire qui décrit
  un *autre* corpus ne dit rien de celui-ci, et ses variantes ouvriraient des fiches au hasard.
- `validated ∧ corpus_ok` — `court_circuit_actif` — commande le **court-circuit** d'AD-5. Ce que la
  signature humaine garde, c'est le *refus* : une affirmation négative, visible, irréversible pour
  celui qui la reçoit. AD-5 ne désarme littéralement que celui-là (« si `validated=false` ou
  `corpus_source_hashes` ne correspond pas au corpus chargé, le court-circuit « zéro hit » est
  **désactivé** … et la requête poursuit vers *retrouver* »).

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

    @property
    def utilisable(self) -> bool:
        """Le fichier est chargé **et** décrit le corpus servi : ses variantes valent quelque chose."""
        return self.charge and self.corpus_ok

    @property
    def court_circuit_actif(self) -> bool:
        """AD-5 : le refus « zéro hit » n'est armé que par une signature humaine sur le bon corpus."""
        return self.utilisable and self.validated

    def expand(self, termes: list[str]) -> dict[str, list[str]]:
        """`{terme de la question: [variantes ajoutées]}` — la forme qu'`Index.chercher` accepte.

        Les **clés restent les termes de la question**, jamais les canoniques du dictionnaire : AD-4
        veut que `terms_searched` dise ce que *comprendre* a produit, et publier les clés du
        dictionnaire ferait fuir, terme par terme, une partie de ce qu'AD-4 interdit d'exposer.

        Une variante déjà égale (à la normalisation près) au terme cherché n'est pas « ajoutée » :
        elle ne changerait rien à la recherche et gonflerait le compte annoncé à l'utilisateur.
        Dictionnaire inutilisable ⇒ chaque terme sort seul : `chercher` fait alors exactement ce
        qu'il faisait avant cette story.
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


def _corpus_ok(hashes: dict[str, str], corpus: Corpus) -> tuple[bool, str]:
    """Les empreintes du dictionnaire décrivent-elles les documents **servis** ? (AD-5, AD-7)

    Le dictionnaire ne couvre pas forcément tout le corpus — celui de cette story ne décrit que le
    guide, et le contrat AXA n'a rien à y faire. La règle est donc « chaque empreinte déclarée
    correspond à un document servi », plus « au moins une » : un fichier sans empreinte ne décrit
    aucun corpus, et le croire sur parole reviendrait à supprimer le verrou.
    """
    if not hashes:
        return False, "corpus_source_hashes vide : le dictionnaire ne dit pas quel corpus il décrit"
    for doc_id, source_hash in sorted(hashes.items()):
        if doc_id not in corpus.documents:
            return False, f"corpus_source_hashes nomme {doc_id!r}, qui n'est pas servi"
        entree = corpus.manifest.get(doc_id)
        if entree is None or entree.source_hash != source_hash:
            return False, f"source_hash de {doc_id!r} différent de celui du manifest"
    return True, ""


def load_dictionary(data_dir: Path | str, corpus: Corpus) -> Dictionnaire:
    """`data/dictionary.json` → `Dictionnaire`. Absent, illisible ou non conforme ⇒ inerte, jamais d'exception."""
    chemin = Path(data_dir) / DICTIONARY_FILE
    if not chemin.is_file():
        return Dictionnaire(raison=f"{DICTIONARY_FILE} absent : lancer "
                                   "`python -m server.ingest.enrich_dictionary`")
    try:
        brut = json.loads(chemin.read_bytes())
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return Dictionnaire(raison=f"{DICTIONARY_FILE} illisible : {_first_error(exc)}"[:500])
    try:
        fichier = DictionaryFile.model_validate(brut)
    except ValueError as exc:  # ValidationError en hérite ; pydantic n'est pas importé ici
        return Dictionnaire(raison=f"{DICTIONARY_FILE} non conforme : {_first_error(exc)}"[:500])

    corpus_ok, raison = _corpus_ok(fichier.corpus_source_hashes, corpus)
    groupes: dict[str, tuple[str, ...]] = {}
    for canonique, variantes in fichier.corpus.items():
        formes: list[str] = []
        for texte in (canonique, *variantes):
            f = forme(texte)
            if f and f not in formes:
                formes.append(f)
        if not formes:
            continue
        groupe = tuple(formes)
        for f in formes:
            # Une forme partagée par deux canoniques garde le premier groupe rencontré : élargir vers
            # les deux mêlerait deux sens (« assurance » de l'habitation et du véhicule) et ferait
            # ouvrir des fiches que la question ne vise pas. L'ordre est celui du fichier, donc
            # déterministe — l'ingestion l'écrit trié.
            groupes.setdefault(f, groupe)
    return Dictionnaire(
        charge=True, validated=fichier.validated, validated_by=(fichier.validated_by or ""),
        validated_at=(fichier.validated_at or ""), corpus_ok=corpus_ok,
        raison=raison if not corpus_ok else "", canoniques=len(fichier.corpus), _groupes=groupes)
