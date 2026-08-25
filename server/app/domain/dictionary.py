"""AD-5 / AD-7 — Le schéma **du fichier** `data/dictionary.json`, partagé par qui l'écrit et qui le lit.

AD-7 sépare les deux rôles : `server/ingest/enrich_dictionary.py` écrit, `server/app/corpus/dictionary.py`
lit, et rien d'autre ne touche ce fichier. Le contrat entre les deux est ce modèle, et il vit dans
`domain` pour la même raison que `Document` : c'est la seule couche que l'ingestion et le serveur
importent tous les deux.

**Strict, champ par champ** (`extra="forbid"` par `DomainModel`) : AD-5 énumère le contenu du fichier
et la spec interdit d'y ajouter quoi que ce soit. Un fichier qui porte un champ de plus n'est pas un
dictionnaire un peu enrichi, c'est un fichier qu'on ne sait pas lire — et le lire à moitié armerait
un refus sur des règles qu'on n'a pas comprises.

**`validated` est un `StrictBool`, et c'est le cœur d'AD-5.** `bool("false")` vaut `True` en Python,
et pydantic, en mode lâche, convertit la chaîne `"true"` en `True`. Le champ est écrit par un
générateur puis signé par un humain : un `validated: "true"` bricolé à la main — ou rendu en chaîne
par un outil tiers — aurait ré-armé le court-circuit « zéro hit » qu'AD-5 tient désactivé tant qu'une
personne n'a pas signé. Seul le `true` JSON strict compte (le contrôle existait déjà dans
`api/etat._dictionnaire_valide`, revue Codex 1.6 M1 ; il est désormais porté par le schéma, donc par
les deux lecteurs à la fois).

**Un « validé par personne » est une contradiction** : `validated=true` exige `validated_by` et
`validated_at` non vides. AD-5 fait de la validation humaine la seule chose qui arme le refus ; un
booléen sans signataire ni date ne prouve rien et ne se recoupe avec rien.
"""

from __future__ import annotations

from pydantic import Field, StrictBool, model_validator

from .document import DomainModel

# Les trois `intent` qu'AD-5 fait refuser avant l'étage `reason`. Le dictionnaire ne porte de
# déclencheurs que pour ceux-là : `question` et `suivi` ne se refusent pas, et lister leurs mots
# ferait croire qu'une présence lexicale vaut pertinence — exactement ce qu'AD-5 interdit
# (« les déclencheurs d'intention sont distincts des mots du corpus »).
# La même liste vit dans `pipelines/commun.INTENTS_REFUSES`, qui est le lieu de la **décision** ;
# ici c'est une contrainte de **schéma**, et `tests/test_dictionary.py` tient les deux ensemble
# (le domaine ne peut pas importer un pipeline, table des couches du spine).
INTENTS_DU_DICTIONNAIRE: frozenset[str] = frozenset({"meteo", "bavardage", "hors_perimetre"})

SCHEMA_VERSION = "1"


class DictionaryFile(DomainModel):
    """`data/dictionary.json` — exactement les huit champs d'AD-5, et pas un de plus."""

    schema_version: str
    # `{doc_id: source_hash}` du corpus qui a servi à l'enrichissement (AD-5 : « si
    # `corpus_source_hashes` ne correspond pas au corpus chargé, le court-circuit est désactivé »).
    corpus_source_hashes: dict[str, str] = Field(default_factory=dict)
    # `{canonique: [variantes]}` — la forme qu'`Index.chercher` accepte déjà.
    corpus: dict[str, list[str]] = Field(default_factory=dict)
    # `{intent: [déclencheurs]}`, bornée aux trois intents refusés.
    intents: dict[str, list[str]] = Field(default_factory=dict)
    # `{fiche_id: [questions]}` — les questions que la fiche sait traiter (FR29 : jamais son texte).
    candidate_questions: dict[str, list[str]] = Field(default_factory=dict)
    validated: StrictBool = False
    validated_by: str | None = None
    validated_at: str | None = None  # UTC ISO 8601

    @model_validator(mode="after")
    def _coherence(self) -> DictionaryFile:
        inconnus = sorted(set(self.intents) - INTENTS_DU_DICTIONNAIRE)
        if inconnus:
            raise ValueError(
                f"intents : clés inattendues {inconnus} — seuls {sorted(INTENTS_DU_DICTIONNAIRE)} "
                "se refusent avant l'étage reason (AD-5)")
        if self.validated and not ((self.validated_by or "").strip() and (self.validated_at or "").strip()):
            raise ValueError("validated=true exige validated_by et validated_at : un dictionnaire "
                             "« validé par personne » n'arme aucun refus (AD-5)")
        return self
