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

**`schema_version` est une valeur, pas un commentaire** (revue Codex 2.1, B4). Le champ était une
chaîne libre : un fichier `schema_version: "999"` — écrit par un outil futur dont le format aurait
changé de sens — traversait le schéma, se chargeait, élargissait la recherche et, signé, armait le
refus, en interprétant ses champs avec les règles d'aujourd'hui. Une version qu'on ne sait pas lire
est un fichier qu'on ne sait pas lire (même raison qu'`extra="forbid"` juste au-dessus) : seule
`SCHEMA_VERSION` est acceptée, tout le reste rend le dictionnaire inerte et le dit en alerte.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, StrictBool, field_validator, model_validator

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
# Le nom du fichier, à un seul endroit : `corpus/dictionary.py` le lit, `ingest/enrich_dictionary.py`
# l'écrit, `api/etat.py` le nomme dans une alerte et `scripts/smoke.py` le relit depuis le dépôt.
DICTIONARY_FILE = "dictionary.json"


class DictionaryFile(DomainModel):
    """`data/dictionary.json` — exactement les huit champs d'AD-5, et pas un de plus."""

    schema_version: str
    # `{doc_id: source_hash}` du corpus qui a servi à l'enrichissement (AD-5 : « si
    # `corpus_source_hashes` ne correspond pas au corpus chargé, le court-circuit est désactivé »).
    corpus_source_hashes: dict[str, str] = Field(default_factory=dict)
    # `{canonique: [variantes]}` — la forme qu'`Index.chercher` accepte déjà.
    corpus: dict[str, list[str]] = Field(default_factory=dict)
    # `{intent: [déclencheurs]}`, bornée aux trois intents refusés.
    #
    # **Aucun code ne lit ce champ, et c'est délibéré** (`target_story: 2.5`). AD-5 est explicite :
    # « les déclencheurs d'intention sont distincts des mots du corpus — la présence d'un mot n'est
    # jamais une preuve de pertinence ». Refuser une question parce qu'elle contient « météo »
    # ferait exactement ce que cette phrase interdit, et le refus par `intent` — décidé par
    # *comprendre*, qui a lu la question entière — est déjà en place et suffit. Le champ est produit
    # et versionné pour servir de **mesure** : la story 2.5 (« pourquoi cette réponse », mode
    # dégradé) pourra confronter l'`intent` rendu par le modèle aux déclencheurs pour dire quand les
    # deux divergent, sans jamais laisser les seconds trancher.
    intents: dict[str, list[str]] = Field(default_factory=dict)
    # `{fiche_id: [questions]}` — les questions que la fiche sait traiter (FR29 : jamais son texte).
    # **Aucun code ne le lit non plus** (`target_story: 2.5`) : ce sont des suggestions à proposer à
    # l'utilisateur, ce qui est une décision d'interface et non de pipeline.
    candidate_questions: dict[str, list[str]] = Field(default_factory=dict)
    validated: StrictBool = False
    validated_by: str | None = None
    validated_at: str | None = None  # UTC ISO 8601

    @field_validator("schema_version")
    @classmethod
    def _version_supportee(cls, valeur: str) -> str:
        """AD-7 : une version inconnue ne se lit pas « au mieux », elle ne se lit pas du tout.

        Le lecteur (`corpus/dictionary.load_dictionary`) en fait un `Dictionnaire` inerte avec sa
        `raison`, `/api/v1/sante` porte l'alerte `dictionnaire_non_valide`, et la page d'accueil
        l'écrit — le chemin exact d'un fichier illisible, parce que c'en est un.
        """
        if valeur != SCHEMA_VERSION:
            raise ValueError(
                f"schema_version {valeur!r} inconnue : ce code ne sait lire que {SCHEMA_VERSION!r} — "
                "régénérer le fichier avec `python -m server.ingest.enrich_dictionary`")
        return valeur

    @field_validator("validated_at")
    @classmethod
    def _date_reelle(cls, valeur: str | None) -> str | None:
        """AD-5 nomme une date **UTC ISO 8601** ; « non vide » n'en fait pas une date.

        Sans ce contrôle, `validated_at: "hier"` traversait le schéma, le serveur et le smoke : la
        signature humaine aurait porté une date que rien ne sait relire, donc rien ne sait recouper —
        alors que c'est précisément le rôle des trois champs de rendre le geste vérifiable.
        La forme est celle qu'écrit `enrich_dictionary` (`…Z`) ; `fromisoformat` accepte aussi
        `+00:00`, et on exige explicitement le fuseau UTC — une heure locale ne se compare à rien.
        """
        if valeur is None or not valeur.strip():
            return valeur
        try:
            quand = datetime.fromisoformat(valeur.strip().replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"validated_at doit être une date UTC ISO 8601 (ex. "
                             f"2026-08-25T10:00:00Z), reçu {valeur!r}") from None
        if quand.utcoffset() is None or quand.utcoffset().total_seconds() != 0:
            raise ValueError(f"validated_at doit être en UTC (suffixe Z ou +00:00), reçu {valeur!r}")
        return valeur

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
