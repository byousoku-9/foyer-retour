"""Matrice I/O de *comprendre* (spec 1.4) : conversion en `ParsedQuestion`, langue, requête envoyée —
préfixe statique 5 min, `temperature=0`, pas d'`effort`, `max_tokens=comprendre_max_tokens`, question /
historique / profil chacun sous `untrusted()` et aucun texte utilisateur hors balises (reprise (c))."""

from __future__ import annotations

import json
import re

import pytest

from server.app.config import Settings
from server.app.domain.errors import LlmParse, Timeout
from server.app.domain.profil import Profil
from server.app.domain.question import ClarificationRequise, ParsedQuestion, Turn
from server.app.llm.budget import RequestBudget
from server.app.llm.client import LlmClient
from server.app.llm.models import TIERS
from server.app.llm.prompting import load_prompt, render_prompt
from server.app.steps.comprendre import comprendre
from tests.llm_fake import FakeAnthropic, fake_message

HAIKU = TIERS["micro"]
UNTRUSTED = re.compile(r'<untrusted kind="([a-z0-9_]+)">\n(.*?)\n</untrusted>', re.DOTALL)


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, anthropic_api_key="", **kw)


def _budget(deadline_s: float = 30.0) -> RequestBudget:
    return RequestBudget(deadline_s=deadline_s, max_attempts=4, max_cost_eur=0.10)


def _sortie(**over) -> str:
    base = {"intent": "question", "question_resolue": "À quelle école inscrire mes enfants ?",
            "clarification": None,
            "language": "fr", "terms": ["école", "inscription scolaire"],
            "themes": ["école", "allocations"], "facettes": ["école des enfants"],
            "bien": None, "evenement": None, "lieu": None,
            "cause": None, "moment": None}
    return json.dumps(base | over, ensure_ascii=False)


def _client(script: list) -> tuple[LlmClient, FakeAnthropic]:
    fake = FakeAnthropic(script)
    return LlmClient(_settings(), anthropic_client=fake), fake


async def _comprendre(client: LlmClient, question: str = "à quelle école inscrire mes enfants ?",
                      historique: list[Turn] | None = None, profil: Profil | None = None,
                      budget: RequestBudget | None = None, **kw):
    return await comprendre(question, historique or [], profil or Profil(), client=client,
                            budget=budget or _budget(), settings=_settings(), **kw)


def _sections(content: str) -> dict[str, str]:
    return {kind: text for kind, text in UNTRUSTED.findall(content)}


async def test_nominal_builds_parsed_question_and_its_own_step_trace() -> None:
    client, _ = _client([fake_message(text=_sortie(), model=HAIKU)])
    parsed, step = await _comprendre(client, profil=Profil(enfants=True))
    assert parsed.intent == "question" and parsed.language == "fr"
    assert parsed.question_resolue == "À quelle école inscrire mes enfants ?"
    assert parsed.terms == ["école", "inscription scolaire"]  # toujours en français
    assert {"école", "allocations"} <= set(parsed.scope.themes)  # profil enfants → école/allocations
    assert step.name == "comprendre" and step.tier == "micro" and step.ms >= 0
    assert len(step.calls) == 1 and step.calls[0].model == HAIKU
    assert step.opened_block_ids == [] and step.discarded_block_ids == []


async def test_meteo_intent_alone_is_enough_to_decide_the_short_circuit() -> None:
    client, _ = _client([fake_message(text=_sortie(intent="meteo", terms=[], themes=[]), model=HAIKU)])
    parsed, step = await _comprendre(client, question="quel temps fera-t-il demain ?")
    assert parsed.intent == "meteo"
    assert len(step.calls) == 1  # un seul appel micro : aucune autre étape requise pour le refus


async def test_an_irresolvable_anaphora_yields_a_clarification_and_never_a_parsed_question() -> None:
    """AD-5, mot pour mot : « une anaphore non résoluble avec l'historique produit
    `Answer.clarification` (question à l'utilisateur) — *comprendre* ne fabrique jamais une
    `question_resolue` » (revue Codex 1.4, B4, tour 3). Les deux issues sont **des types distincts** :
    porter la clarification par un champ de `ParsedQuestion` (tour 2) laissait subsister une
    `question_resolue` non autonome (« et pour eux ? ») que rien n'empêchait de partir à *retrouver*."""
    client, _ = _client([fake_message(
        text=_sortie(question_resolue=None, clarification="De quelles personnes parlez-vous ?",
                     terms=[], themes=[]), model=HAIKU)])
    sortie, step = await _comprendre(client, question="et pour eux ?")  # aucun historique
    assert isinstance(sortie, ClarificationRequise)
    assert not isinstance(sortie, ParsedQuestion)  # aucune question résolue n'existe dans le résultat
    assert sortie.clarification == "De quelles personnes parlez-vous ?"
    assert sortie.intent == "question" and sortie.language == "fr"
    assert "et pour eux" not in sortie.model_dump_json()  # la question non autonome ne voyage pas
    assert len(step.calls) == 1  # une clarification coûte un seul appel micro, comme un refus
    # cas courant : la question se comprend seule, aucune clarification
    client, _ = _client([fake_message(text=_sortie(clarification="   "), model=HAIKU)])
    sortie, _ = await _comprendre(client)
    assert isinstance(sortie, ParsedQuestion)  # une chaîne vide n'est pas une demande de clarification
    assert sortie.question_resolue == "À quelle école inscrire mes enfants ?"


@pytest.mark.parametrize("resolue, clarification", [
    (None, None),          # aucune des deux issues
    (None, "   "),         # ni l'une ni l'autre, à l'espace près
    ("et pour eux ?", "De quelles personnes parlez-vous ?"),  # les deux à la fois
])
async def test_neither_or_both_outcomes_is_a_validation_error_not_an_arbitrary_choice(
        resolue: str | None, clarification: str | None) -> None:
    """L'exclusivité des deux issues est portée par le schéma de sortie, pas par le code de l'étape :
    sa violation emprunte la relance motivée du client (AD-9, « 1 retry sur parse invalide »), qui
    nomme le champ fautif. Le premier appel invalide est suivi d'un second, valide."""
    invalide = fake_message(text=_sortie(question_resolue=resolue, clarification=clarification), model=HAIKU)
    client, _ = _client([invalide, fake_message(text=_sortie(), model=HAIKU)])
    sortie, step = await _comprendre(client)
    assert isinstance(sortie, ParsedQuestion) and len(step.calls) == 2
    assert any("question_resolue" in (c.detail or "") for c in step.checks), step.checks
    # sans seconde chance, l'étape échoue plutôt que de trancher elle-même entre les deux issues
    client, _ = _client([invalide, invalide])
    with pytest.raises(LlmParse):
        await _comprendre(client)


async def test_the_prompt_asks_for_a_clarification_rather_than_a_fabricated_question() -> None:
    """La propriété sémantique vit dans le prompt, pas dans le code (AD-5)."""
    prefixe = render_prompt("comprendre", question_min_terms=2, question_max_terms=6,
                            question_max_facettes=4, perimetre_guide="- Logement : Signer un bail")
    assert "deux issues exclusives" in prefixe
    assert "`clarification` est alors renseignée à sa place" in prefixe
    assert "que l'historique ne dit pas" in prefixe
    # mesuré en réel : sans cette consigne, une question météo revenait avec les **deux** champs à
    # `null` (le modèle jugeait la question résolue inutile hors périmètre) — un appel perdu en
    # relance motivée à chaque refus (revue Codex 1.4, B4, tour 3 ; `docs/tests-live.md`)
    assert "quel que soit l'`intent`" in prefixe


async def test_forced_lang_wins_over_detection() -> None:
    client, _ = _client([fake_message(text=_sortie(language="fr"), model=HAIKU)])
    parsed, _step = await _comprendre(client, lang="en")
    assert parsed.language == "en"


@pytest.mark.parametrize("forced, expected", [("EN", "en"), ("fr-LU", "fr"), ("anglais", "fr")])
async def test_a_forced_lang_is_normalized_like_a_detected_one(forced: str, expected: str) -> None:
    # La normalisation vit sur `ParsedQuestion` : `lang` forcé et langue détectée passent par la même
    # règle, et un `lang` mal formé retombe sur `fr` au lieu de partir tel quel (revue 1.4).
    client, _ = _client([fake_message(text=_sortie(language="de"), model=HAIKU)])
    parsed, _step = await _comprendre(client, lang=forced)
    assert parsed.language == expected


@pytest.mark.parametrize("detected, expected", [("EN", "en"), ("", "fr"), ("anglais", "fr"), ("fr-LU", "fr")])
async def test_detected_language_is_normalized_with_fr_fallback(detected: str, expected: str) -> None:
    client, _ = _client([fake_message(text=_sortie(language=detected), model=HAIKU)])
    parsed, _step = await _comprendre(client)
    assert parsed.language == expected


async def test_scope_fields_and_term_cleanup_are_converted() -> None:
    client, _ = _client([fake_message(text=_sortie(terms=[" école ", "", "cantine"], themes=["", " auto "],
                                                   bien="vélo", evenement="vol", lieu="cave",
                                                   cause="", moment=None), model=HAIKU)])
    parsed, _step = await _comprendre(client)
    assert parsed.terms == ["école", "cantine"] and parsed.scope.themes == ["auto"]
    assert parsed.scope.bien == "vélo" and parsed.scope.evenement == "vol" and parsed.scope.lieu == "cave"
    assert parsed.scope.cause is None and parsed.scope.moment is None


async def test_request_shape_static_prefix_untrusted_sections_and_thresholds() -> None:
    client, fake = _client([fake_message(text=_sortie(), model=HAIKU)])
    historique = [Turn(role="user", texte="on arrive en mars"), Turn(role="assistant", texte="bien noté")]
    question = "et pour l'école des enfants ?"
    await _comprendre(client, question=question, historique=historique, profil=Profil(enfants=True, autre="x"))
    (req,) = fake.requests
    s = _settings()
    # préfixe statique byte-identique (pas de sommaire : micro, cache 5 min), tier micro
    assert req["model"] == HAIKU
    attendu = load_prompt("commun") + "\n\n" + render_prompt(
        "comprendre", question_min_terms=s.question_min_terms, question_max_terms=s.question_max_terms,
        question_max_facettes=s.question_max_facettes, perimetre_guide="")
    assert req["system"] == [{"type": "text", "text": attendu,
                              "cache_control": {"type": "ephemeral"}}]
    assert req["extra_body"] == {"temperature": 0}
    assert "effort" not in req["output_config"]
    assert req["max_tokens"] == s.comprendre_max_tokens == 1024
    # le schéma dédié est plat et tout est requis (aucun défaut)
    assert set(req["output_config"]["format"]["schema"]["required"]) == {
        "intent", "question_resolue", "clarification", "language", "terms", "themes", "facettes",
        "bien", "evenement", "lieu", "cause", "moment"}
    # question, historique et profil chacun sous untrusted() ; rien hors balises
    (msg,) = req["messages"]
    sections = _sections(msg["content"])
    assert set(sections) == {"historique", "profil", "question"}
    assert sections["question"] == question
    assert json.loads(sections["profil"]) == {"enfants": True}  # profil filtré (PROFIL_KEYS)
    assert json.loads(sections["historique"]) == [{"role": "user", "texte": "on arrive en mars"},
                                                  {"role": "assistant", "texte": "bien noté"}]
    outside = UNTRUSTED.sub("", msg["content"])
    assert outside.strip() == ""
    for fragment in (question, "on arrive en mars", "bien noté", "enfants"):
        assert fragment not in outside


async def test_budget_errors_from_the_client_bubble_up_unchanged() -> None:
    client, fake = _client([])
    with pytest.raises(Timeout):
        await _comprendre(client, budget=RequestBudget(deadline_s=0, max_attempts=4, max_cost_eur=0.10))
    assert fake.requests == []


async def test_the_prompt_announces_the_configured_term_bounds() -> None:
    """Convention Seuils (revue Codex 1.4, I1) : une borne chiffrée annoncée au modèle est un seuil de
    `config.py`, pas un nombre écrit dans le prompt — sinon la surcharge ne change que la moitié du
    système. Le rendu reste déterministe, donc le préfixe reste byte-identique et cacheable."""
    client, fake = _client([fake_message(text=_sortie(), model=HAIKU)])
    await comprendre("q", [], Profil(), client=client, budget=_budget(),
                     settings=_settings(question_min_terms=3, question_max_terms=9))
    prefixe = fake.requests[0]["system"][0]["text"]
    assert "3 à 9 termes de recherche" in prefixe
    assert "2 à 6 termes de recherche" not in prefixe


# --- story 2.1 : le périmètre vient du corpus, et les listes sont bornées ----

async def test_le_perimetre_du_prompt_vient_du_corpus_et_non_dune_liste_ecrite_a_la_main() -> None:
    """Reprise différée `target_story: 2.1` — faux refus mesuré le 2026-08-24.

    « Comment obtenir LuxTrust au meilleur prix ? » ressortait `hors_perimetre` parce que la liste de
    périmètre de `prompts/comprendre.md` énumérait « démarches administratives (commune, matricule,
    titres de séjour)… » sans nommer l'identité numérique, alors que le guide a une fiche entière
    dessus. Le périmètre est désormais une projection des titres du corpus : une fiche ajoutée entre
    dans le périmètre sans qu'on réécrive une phrase.
    """
    client, fake = _client([fake_message(text=_sortie(), model=HAIKU)])
    perimetre = "- Administratif : LuxTrust et MyGuichet, Les huit premiers jours"
    await _comprendre(client, perimetre=perimetre)
    prefixe = fake.requests[0]["system"][0]["text"]
    assert perimetre in prefixe
    # L'énumération écrite à la main a disparu : c'est elle qui produisait le faux refus.
    assert "titres de séjour" not in prefixe
    # …et la consigne qui dit que la fiche décide, pas le contexte d'installation.
    assert "c'est la fiche qui décide" in prefixe


async def test_le_perimetre_ne_change_pas_le_reste_du_prefixe_et_reste_deterministe() -> None:
    """AD-9 : le préfixe reste **byte-identique** d'un appel à l'autre à corpus constant (cacheable)."""
    perimetre = "- Famille : Allocations familiales"
    rendus = []
    for _ in range(2):
        client, fake = _client([fake_message(text=_sortie(), model=HAIKU)])
        await _comprendre(client, perimetre=perimetre)
        rendus.append(fake.requests[0]["system"][0]["text"])
    assert rendus[0] == rendus[1]


async def test_le_code_tronque_terms_et_themes_aux_seuils_de_config() -> None:
    """Reprise différée `target_story: 2.1` : le prompt demandait 2 à 6 termes, rien ne l'appliquait.

    Tronqué **par la fin** et jamais coupé au milieu d'un libellé : l'ordre du modèle est celui de la
    pertinence qu'il leur prête, et un terme amputé chercherait autre chose. Comme `facettes`, qui
    l'était déjà.
    """
    client, fake = _client([fake_message(
        text=_sortie(terms=[f"t{i}" for i in range(12)], themes=[f"h{i}" for i in range(12)],
                     facettes=[f"f{i}" for i in range(12)]), model=HAIKU)])
    s = _settings()
    parsed, _step = await comprendre("q", [], Profil(), client=client, budget=_budget(), settings=s)
    assert parsed.terms == [f"t{i}" for i in range(s.question_max_terms)]
    assert parsed.scope.themes == [f"h{i}" for i in range(s.scope_max_themes)]
    assert parsed.facettes == [f"f{i}" for i in range(s.question_max_facettes)]
    assert fake.remaining_script == 0


async def test_le_schema_de_sortie_interdit_un_champ_surnumeraire_et_borne_les_listes() -> None:
    """Reprise différée `target_story: 2.1` : `SortieComprendre` héritait de `BaseModel`.

    `extra="forbid"` fait d'un champ inventé une violation de contrat, qui emprunte la relance motivée
    du client au lieu d'être ignorée en silence. Les listes portent une borne de **forme** généreuse :
    au-delà, ce n'est plus une liste de termes, c'est un déversement — et le rejet est alors le bon
    comportement, puisque la troncature du code ne saurait plus de quoi elle tronque.
    """
    from server.app.steps.comprendre import LISTE_MAX, SortieComprendre

    assert SortieComprendre.model_config["extra"] == "forbid"
    with pytest.raises(ValueError, match="extra_forbidden|Extra inputs"):
        SortieComprendre.model_validate_json(_sortie(inconnu="valeur"))
    with pytest.raises(ValueError, match="too_long|at most"):
        SortieComprendre.model_validate_json(_sortie(terms=[f"t{i}" for i in range(LISTE_MAX + 1)]))
    # Le schéma envoyé au modèle porte la borne **et** l'interdiction des champs surnuméraires :
    # `anthropic.transform_schema` reporte les contraintes non supportées dans la `description`.
    client, fake = _client([fake_message(text=_sortie(), model=HAIKU)])
    await _comprendre(client)
    schema = fake.requests[0]["output_config"]["format"]["schema"]
    assert schema["additionalProperties"] is False
    assert f"maxItems: {LISTE_MAX}" in schema["properties"]["terms"]["description"]
