from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from server.app.config import REPO_ROOT, Settings
from server.app.domain.trace import Trace

THRESHOLD_VARS = [k.upper() for k in Settings.model_fields] + ["ENV", "ALLOW_UNGATED"]


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in THRESHOLD_VARS:
        monkeypatch.delenv(var, raising=False)


def test_defaults_match_spine_hypotheses() -> None:
    s = Settings(_env_file=None)
    assert s.deadline_s == 55 and s.llm_timeout_s == 40  # 40 s : AD-16 amendé en 1.9, sur mesure
    assert s.quote_min_chars == 25 and s.quote_min_ratio == 0.6
    assert s.max_opens == 6 and s.node_window == 30 and s.search_limit == 20 and s.max_llm_turns == 2
    assert s.max_cost_eur_per_request == 0.10 and s.cost_alert_eur == 0.05
    # story 1.10 : AD-9 remplace le plafond **par requête** par un plafond **par run** en évals ;
    # CLAUDE.md exige « la clé **et un plafond** ». `--max-cost` ne fait que surcharger celui-ci.
    assert s.evals_max_cost_eur == 1.0
    assert s.rate_limit_per_minute == 10 and s.rate_limit_per_day == 100
    assert s.coverage_threshold == 0.8 and s.kind_confidence_min == 0.7
    assert s.env == "dev" and s.allow_ungated is True
    # story 1.5 : pipeline guide, historique borné (AD-11), bornes de *vérifier* (AD-4)
    assert s.guide_doc_id == "lux-guide" and s.historique_max_turns == 6
    assert s.verifier_max_claims == 8 and s.verifier_max_tokens == 1024
    # story 1.8 : contrat servi par le pipeline sinistre, et les bornes de son appel groupé
    assert s.sinistre_doc_id == "axa-lu-optihome-2017"
    assert s.verifier_sinistre_max_tokens == 3072
    assert s.fait_manquant_max_chars == 200 and s.ask_client_max == 8


def test_the_served_documents_of_the_defaults_exist_in_the_real_corpus() -> None:
    """Les deux `*_doc_id` par défaut désignent des documents que le corpus livré sert vraiment.

    Tous les tests de pipeline les surchargent par un corpus synthétique : une faute de frappe dans le
    défaut ne se verrait donc nulle part, et **toute** requête non paramétrée ressortirait en 503
    `corpus_unavailable` — en production d'abord (revue 1.8). Le manifeste suffit à le dire, sans
    charger les documents.
    """
    s = Settings(_env_file=None)
    manifest = json.loads((REPO_ROOT / "data" / "manifest.json").read_text("utf-8"))
    servis = set(manifest.get("documents", manifest))
    assert {s.guide_doc_id, s.sinistre_doc_id} <= servis, sorted(servis)


def test_thresholds_feed_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUOTE_MIN_CHARS", "30")
    monkeypatch.setenv("ALLOW_UNGATED", "false")
    s = Settings(_env_file=None)
    assert s.quote_min_chars == 30 and s.allow_ungated is False
    t = Trace(request_id="r", pipeline="guide", thresholds=s.thresholds())
    assert t.thresholds["quote_min_chars"] == 30
    assert {"max_opens", "node_window", "search_limit", "max_llm_attempts", "max_cost_eur_per_request",
            "rate_limit_per_minute", "rate_limit_per_day", "deadline_s",
            # story 1.4 : plafonds de sortie par étape et borne en blocs de *retrouver*
            "comprendre_max_tokens", "rediger_max_tokens", "retrieval_max_blocks",
            # story 1.5 : bornes du pipeline et de *vérifier*
            "historique_max_turns", "verifier_max_claims", "verifier_max_tokens",
            # story 1.8 : les deux bornes posées sur ce que le modèle fait afficher au sinistre
            "fait_manquant_max_chars", "ask_client_max",
            # story 1.10 : le plafond de coût d'un run d'évals (AD-9, AD-14)
            "evals_max_cost_eur",
            # story 2.3 : les places réservées, parmi `max_opens`, aux nœuds que le profil désigne
            "profil_max_opens"} <= set(t.thresholds)
    assert all(isinstance(v, (int, float)) for v in t.thresholds.values())
    # `guide_doc_id` et `sinistre_doc_id` sont des slugs, pas des seuils : ils n'ont rien à faire dans
    # `Trace.thresholds` (typé `dict[str, float | int]` — les y mettre ferait échouer la sérialisation).
    assert "guide_doc_id" not in t.thresholds and "sinistre_doc_id" not in t.thresholds


def test_allow_ungated_est_ferme_en_production_et_libre_en_dev() -> None:
    """AC 1.10 : « `ALLOW_UNGATED` est **désactivé** en production à la fin de cette story ».

    Avant la revue Codex 1.10 (B3), `prod` ne dérivait `False` que lorsque la variable était absente :
    `ENV=prod ALLOW_UNGATED=true` — un `--set-env-vars` au déploiement — armait la dérogation en
    production. Elle y est maintenant refusée, et le refus reste dicible (`ungated_demande_en_prod`).
    """
    assert Settings(_env_file=None, env="prod").allow_ungated is False
    prod = Settings(_env_file=None, env="prod", allow_ungated=True)
    assert prod.allow_ungated is False and prod.ungated_demande_en_prod is True
    assert Settings(_env_file=None, env="prod", allow_ungated=False).ungated_demande_en_prod is False
    # En `dev`, rien ne change : la dérogation y est le mode de travail normal (AD-7).
    assert Settings(_env_file=None, env="dev").allow_ungated is True
    assert Settings(_env_file=None, env="dev", allow_ungated=False).allow_ungated is False
    assert Settings(_env_file=None, env="dev", allow_ungated=True).ungated_demande_en_prod is False


def test_bounds_and_coherence() -> None:
    with pytest.raises(ValidationError, match="llm_timeout_s"):
        Settings(_env_file=None, llm_timeout_s=60, deadline_s=55)
    with pytest.raises(ValidationError, match="llm_retry_margin_s"):
        Settings(_env_file=None, llm_retry_margin_s=60, deadline_s=55)
    # revue 1.4 : un plafond par étape ne peut pas dépasser le plafond de sortie du client — il part
    # tel quel au fournisseur et entre au tarif `output` dans le majorant `estimate_cost`.
    with pytest.raises(ValidationError, match="rediger_max_tokens"):
        Settings(_env_file=None, rediger_max_tokens=8192, llm_max_output_tokens=4096)
    with pytest.raises(ValidationError, match="comprendre_max_tokens"):
        Settings(_env_file=None, comprendre_max_tokens=8192, llm_max_output_tokens=4096)
    with pytest.raises(ValidationError, match="verifier_max_tokens"):
        Settings(_env_file=None, verifier_max_tokens=8192, llm_max_output_tokens=4096)
    # story 1.5 : *vérifier* doit pouvoir juger tout ce que *rédiger* peut produire, sinon des claims
    # retrouvées seraient rejetées « non évaluées » par pure configuration (dégradé silencieux).
    with pytest.raises(ValidationError, match="verifier_max_claims"):
        Settings(_env_file=None, verifier_max_claims=2, draft_max_claims=4)
    with pytest.raises(ValidationError, match="draft_max_claims.*draft_max_segments"):
        Settings(_env_file=None, draft_max_claims=4, draft_max_segments=3)
    Settings(_env_file=None, verifier_max_claims=4, draft_max_claims=4)
    for bad in ({"deadline_s": 0}, {"quote_min_ratio": 1.5}, {"max_opens": 0}, {"max_cost_eur_per_request": -1},
                {"evals_max_cost_eur": -1}, {"rate_limit_per_day": 0}):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **bad)


def test_env_file_is_read_from_repo_root(tmp_path: Path) -> None:
    assert Settings.model_config["env_file"] == REPO_ROOT / ".env"
    assert (REPO_ROOT / "pyproject.toml").is_file()
    env = tmp_path / ".env"
    env.write_text('ANTHROPIC_API_KEY="sk-test-123"\nUSD_EUR=0.5\n')
    s = Settings(_env_file=env)
    assert s.anthropic_api_key == "sk-test-123" and s.usd_eur == 0.5


def test_env_example_loads_as_is() -> None:
    s = Settings(_env_file=REPO_ROOT / ".env.example")
    assert s.anthropic_api_key == "" and s.env == "dev" and s.allow_ungated is True and s.usd_eur == 0.92


def test_empty_env_values_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOW_UNGATED", "")
    monkeypatch.setenv("MAX_OPENS", "")
    assert Settings(_env_file=None).max_opens == 6


# --- story 2.1 : les seuils du dictionnaire et du périmètre ------------------

def test_les_seuils_du_dictionnaire_sont_ceux_de_la_spec() -> None:
    s = Settings(_env_file=None)
    assert s.dictionary_term_max_chars == 60 and s.dictionary_term_max_words == 4
    assert s.dictionary_max_variants_per_term == 8 and s.dictionary_max_terms_per_fiche == 20
    assert s.dictionary_question_max_chars == 160 and s.dictionary_max_questions_per_fiche == 5
    assert s.dictionary_max_intent_triggers == 30 and s.dictionary_max_output_tokens == 16000
    assert s.dictionary_max_cost_eur == 3.0
    assert s.dictionary_batch_poll_s == 20.0 and s.dictionary_batch_timeout_s == 3600.0
    assert s.perimetre_max_chars == 4000
    # Convention Seuils : un nombre nouveau vit ici **et** se publie.
    t = s.thresholds()
    for nom in ("dictionary_term_max_chars", "dictionary_term_max_words",
                "dictionary_max_variants_per_term", "dictionary_max_terms_per_fiche",
                "dictionary_question_max_chars", "dictionary_max_questions_per_fiche",
                "dictionary_max_intent_triggers", "dictionary_max_output_tokens",
                "dictionary_max_cost_eur", "dictionary_batch_poll_s",
                "dictionary_batch_timeout_s", "perimetre_max_chars"):
        assert t[nom] == getattr(s, nom), nom


@pytest.mark.parametrize("bad", [
    {"dictionary_term_max_chars": 0}, {"dictionary_term_max_words": 0},
    {"dictionary_max_variants_per_term": 0}, {"dictionary_max_terms_per_fiche": 0},
    {"dictionary_question_max_chars": 0}, {"dictionary_max_questions_per_fiche": 0},
    {"dictionary_max_intent_triggers": 0}, {"dictionary_max_output_tokens": 0},
    {"dictionary_max_cost_eur": 0}, {"dictionary_batch_poll_s": 0},
    {"dictionary_batch_timeout_s": 0}, {"perimetre_max_chars": 0},
])
def test_chaque_seuil_du_dictionnaire_est_borne(bad: dict) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **bad)


def test_le_plafond_de_sortie_de_lingestion_nest_pas_borne_par_celui_du_client() -> None:
    """`llm_max_output_tokens` borne les appels du **serveur** (deadline, plafond par requête, AD-9).

    L'ingestion est hors ligne, en Batch API, et son garde-fou est `dictionary_max_cost_eur`, vérifié
    avant toute soumission. Les confondre plafonnerait une requête de batch à 4 096 tokens pour une
    raison qui ne la concerne pas.
    """
    s = Settings(_env_file=None)
    assert s.dictionary_max_output_tokens > s.llm_max_output_tokens


# --- reprise différée 1.11 : les seuils **commentés** de `.env.example` ------

def _seuils_commentes() -> dict[str, str]:
    """Les paires `NOM=valeur` que `.env.example` cite dans ses commentaires.

    `test_env_example_loads_as_is` ne voit que les lignes **actives** ; tout ce qui est commenté est
    un second texte faisant autorité sur les mêmes nombres, sans garde. La story 1.11 y a corrigé
    `LLM_TIMEOUT_S=25` → `40`, périmé depuis l'amendement AD-16 de la story 1.9 : la dérive avait
    duré une story entière.
    """
    import re

    connus = {k.upper() for k in Settings.model_fields}
    # Une paire **citée** (entre accents graves) est de la prose qui décrit une situation
    # (« `ENV=prod` + `ALLOW_UNGATED=true` produit l'alerte… »), pas la documentation d'un défaut :
    # la comparer à `Settings` ferait rougir le garde-fou sur une phrase parfaitement juste.
    paire = re.compile(r"(?<!`)\b([A-Z][A-Z0-9_]*)=([^\s`]+)")
    trouves: dict[str, str] = {}
    for ligne in (REPO_ROOT / ".env.example").read_text("utf-8").splitlines():
        if not ligne.lstrip().startswith("#"):
            continue
        for nom, valeur in paire.findall(ligne):
            if nom in connus:
                trouves[nom] = valeur.rstrip(".,;)")
    return trouves


def test_les_seuils_cites_en_commentaire_valent_les_defauts_de_settings() -> None:
    defauts = Settings(_env_file=None)
    cites = _seuils_commentes()
    assert cites, ".env.example ne cite plus aucun seuil : le garde-fou ne garde plus rien"
    ecarts = []
    for nom, valeur in sorted(cites.items()):
        attendu = getattr(defauts, nom.lower())
        # La valeur commentée est du texte : on la relit par `Settings`, comme un `.env` le ferait.
        relu = getattr(Settings(_env_file=None, **{nom.lower(): valeur}), nom.lower())
        if relu != attendu:
            ecarts.append(f"{nom}={valeur} dans .env.example, {attendu!r} dans config.py")
    assert not ecarts, "\n".join(ecarts)


def test_les_seuils_de_la_story_sont_documentes_dans_env_example() -> None:
    """Convention Seuils : « documenté dans `.env.example` ». Un seuil que personne ne sait régler
    n'est pas un réglage, c'est une constante cachée."""
    cites = set(_seuils_commentes())
    attendus = {"DICTIONARY_TERM_MAX_CHARS", "DICTIONARY_TERM_MAX_WORDS",
                "DICTIONARY_MAX_VARIANTS_PER_TERM", "DICTIONARY_MAX_TERMS_PER_FICHE",
                "DICTIONARY_QUESTION_MAX_CHARS", "DICTIONARY_MAX_QUESTIONS_PER_FICHE",
                "DICTIONARY_MAX_INTENT_TRIGGERS", "DICTIONARY_MAX_OUTPUT_TOKENS",
                "DICTIONARY_MAX_COST_EUR", "DICTIONARY_BATCH_POLL_S",
                "DICTIONARY_BATCH_TIMEOUT_S", "PERIMETRE_MAX_CHARS"}
    assert attendus <= cites, sorted(attendus - cites)


def test_la_cle_posee_mais_vide_fait_foi(monkeypatch: pytest.MonkeyPatch) -> None:
    """AD-14, et depuis la story 2.1 l'ingestion du dictionnaire aussi : « sans clé, ça refuse ».

    `Settings` laisse tomber une variable **vide** (`env_ignore_empty=True`) et retombe sur le `.env`
    du poste : sans cette règle, `ANTHROPIC_API_KEY= uv run …` tournait et facturait — l'inverse
    exact de ce que la commande dit vouloir. Mesuré en 2.1 : la version naïve de l'ingestion a
    réellement soumis un lot de Batch API sous cette commande.
    """
    from server.app.config import cle_absente

    avec = Settings(_env_file=None, anthropic_api_key="sk-x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    assert cle_absente(avec) is True          # posée et vide : elle fait foi
    monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")
    assert cle_absente(avec) is True
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-y")
    assert cle_absente(Settings(_env_file=None, anthropic_api_key="")) is False
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert cle_absente(avec) is False         # non posée : c'est `.env` qui répond
    assert cle_absente(Settings(_env_file=None, anthropic_api_key="")) is True


def test_les_deux_commandes_qui_exigent_la_cle_appliquent_la_meme_regle() -> None:
    """Une promesse identique ne peut pas être tenue par deux codes différents."""
    from server.app.config import cle_absente
    from server.evals.run import cle_absente as evals_cle_absente

    s = Settings(_env_file=None, anthropic_api_key="")
    assert evals_cle_absente(s) is cle_absente(s)


def test_la_borne_de_nombre_des_listes_de_comprendre_vit_ici_et_se_publie() -> None:
    """Revue Codex 2.2 (I2) : la Convention Seuils ne souffre pas d'exception de domicile.

    `LISTE_MAX = 32` vivait en dur dans `steps/comprendre.py`, au motif qu'elle entre dans le schéma
    JSON envoyé au modèle et qu'un `.env` la ferait varier d'un poste à l'autre. Le motif est bon,
    la conclusion ne l'était pas : `comprendre_max_tokens` entre lui aussi dans la requête — donc
    dans la clé des fixtures — et il est un champ de `Settings` depuis la story 1.4. Ce qui protège
    le schéma n'est pas le fichier, c'est de ne pas être un champ `.env`. La borne est donc une
    **constante de module** de `config.py`, publiée dans `Trace.thresholds` comme tout seuil actif,
    et l'étape n'en garde qu'un alias de lecture.
    """
    from server.app.config import LISTE_MAX_ITEMS
    from server.app.steps import comprendre as etape

    assert LISTE_MAX_ITEMS == 32
    assert etape.LISTE_MAX is LISTE_MAX_ITEMS  # l'étape lit, elle ne décide plus
    assert Settings(_env_file=None).thresholds()["liste_max_items"] == LISTE_MAX_ITEMS
    # Pas un champ `.env` : le schéma de sortie de *comprendre* ne doit pas dépendre du poste.
    assert "liste_max_items" not in Settings.model_fields
    # Et plus aucun littéral de borne dans le corps de l'étape (Convention Seuils, lettre exacte).
    source = (REPO_ROOT / "server" / "app" / "steps" / "comprendre.py").read_text("utf-8")
    corps = [ligne for ligne in source.splitlines()
             if not ligne.lstrip().startswith("#") and "= LISTE_MAX_ITEMS" not in ligne]
    assert not [ligne for ligne in corps if re.search(r"=\s*\d{2,}\s*$", ligne)], corps
