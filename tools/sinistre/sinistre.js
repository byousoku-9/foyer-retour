// L'outil sinistre (story 1.9) — HTML/CSS/JS vanilla, sans build, sans framework, sans requête tierce.
//
// Le partage est celui du front du guide (story 1.7), et pour la même raison : **ce qui décide est
// séparé de ce qui peint**. Les fonctions de composition (`vueFormulaire`, `vueVerdict`, `vueErreur`,
// `vueAttente`, `clausesParClaim`, `statutTexte`, `libelleVerdict`, `coutTexte`, `corpsSinistre`)
// sont **pures** : elles rendent un arbre de nœuds simples `{tag, cls, texte, enfants, attrs}` et ne
// touchent ni au DOM, ni au réseau. `materialiser()` transforme cet arbre en DOM et ne décide de
// rien — il pose du texte par `textContent`, jamais par `innerHTML` (AD-15).
//
// C'est ce qui rend les promesses de la story vérifiables sans navigateur : « le badge du verdict,
// la mention de portée, la raison, les faits compris, le paquet manquant, les clauses et leurs
// statuts sont affichés », « aucun verdict de remplacement sur une erreur », « aucune conversation
// ni aucun sinistre en localStorage » sont des assertions sur un arbre, pas sur des pixels.
//
// AD-6/AD-4 : la page **affiche** le verdict, elle n'en calcule aucun morceau. Aucune règle de la
// table AD-6 n'est ici ; `answer.verdict` arrive décidé, avec sa raison composée par le serveur.
// AD-16 : aucun repli, ni bouton « chercher autrement », ni valeur de remplacement sur erreur.

(function () {
  "use strict";

  // AD-12 : une seule origine. Le serveur qui sert cette page sert aussi l'API — aucune URL en dur,
  // aucun CORS, et la page fonctionne à l'identique en local et sur Cloud Run.
  var API_BASE = (window.location && /^https?:$/.test(window.location.protocol))
    ? window.location.origin : "";

  // Les bornes du serveur, recopiées de `api/schemas.SinistreRequest` et `domain/question.Faits`.
  // Elles arment les `maxlength` de la page : un rejet en 400 après quatre secondes d'attente est
  // une mauvaise façon d'apprendre qu'on a écrit trop long. Ce ne sont **pas** des troncatures
  // silencieuses côté client (AD-11 : « rejet plutôt que troncature ») — le champ refuse la frappe
  // au-delà, ce que l'utilisateur voit. Un test Python les épingle contre les schémas du serveur.
  var QUESTION_MAX = 1000;
  var DESCRIPTION_MAX = 2000;
  // `Faits.date` et `Faits.lieu` étaient les deux seuls champs de texte sans borne : ils partent au
  // modèle dans le même bloc `untrusted()` que la description, et seul `request_max_bytes` les
  // limitait (revue 1.9, tour 2). Bornés dans le domaine, reflétés ici.
  var DATE_MAX = 64;
  var LIEU_MAX = 200;

  // Borne d'abandon côté client : la deadline du serveur plus la marge qu'il annonce. Les deux sont
  // des **seuils de `config.py`** (`deadline_s`, `client_abort_margin_s`), publiés par
  // `thresholds()` et lus sur `/api/v1/sante` au démarrage — la page ne les recopie pas (convention
  // Seuils du spine, et patron de `web/app/chat.js` depuis 1.7). Les deux littéraux ci-dessous ne
  // sont qu'un **repli** pour la première requête si la sonde n'a pas répondu : une borne figée
  // ferait couper par le navigateur une requête à laquelle le serveur aurait répondu, le jour où
  // `deadline_s` monte (revue 1.9).
  var DEADLINE_S_REPLI = 55;
  var MARGE_ABANDON_S_REPLI = 10;
  var seuilsServeur = null;

  function seuil(nom, repli) {
    var v = seuilsServeur && seuilsServeur[nom];
    return (typeof v === "number" && isFinite(v) && v > 0) ? v : repli;
  }

  function abandonMs() {
    return Math.round((seuil("deadline_s", DEADLINE_S_REPLI) +
                       seuil("client_abort_margin_s", MARGE_ABANDON_S_REPLI)) * 1000);
  }

  var PORTEE = "au regard des conditions générales seules — verdict non validé par un expert assurance";

  // ---------- nœuds : la description de ce qu'il faut peindre ----------

  // `attrs` (story 2.5) : un objet **plat** de chaînes, posé tel quel par `materialiser()` avec
  // `setAttribute`. Le matérialiseur le lisait déjà ; `noeud()` ne savait pas le produire, si bien
  // que la branche était inatteignable. Elle sert désormais à l'`aria-hidden` du pictogramme d'un
  // contrôle, dont l'état est déjà écrit en toutes lettres à côté.
  function noeud(tag, cls, texte, enfants, attrs) {
    var n = { tag: tag };
    if (cls) n.cls = cls;
    if (texte !== undefined && texte !== null) n.texte = String(texte);
    if (enfants && enfants.length) n.enfants = enfants;
    if (attrs) n.attrs = attrs;
    return n;
  }

  function lienHttp(url) {
    var u = String(url || "");
    return /^https?:\/\//i.test(u) ? u : null;
  }

  // Le contrat est déjà validé par `lireReponse()` sur ce que l'écran consomme ; ces gardes valent
  // pour les **listes imbriquées** qu'il ne descend pas (`ask_client`, `missing.faits`,
  // `trace.steps`…). Un `TypeError` à la peinture, après un appel payé, laisserait la carte
  // d'attente à l'écran sans rien dire — l'exact inverse d'AD-16 (revue 1.9).
  function tableau(v) { return Array.isArray(v) ? v : []; }

  function liste(cls, items) {
    return noeud("ul", cls, null, items.map(function (t) { return noeud("li", null, String(t)); }));
  }

  function section(cls, titre, enfants) {
    return noeud("section", cls, null, [noeud("h3", null, titre)].concat(enfants));
  }

  // ---------- textes composés ----------

  // AD-6 : les quatre valeurs, et rien d'autre. Une valeur inconnue n'est **pas** traduite en
  // « ne tranche pas » : le serveur aurait rendu quelque chose que ce contrat ne prévoit pas, et
  // l'afficher comme un verdict connu serait le dégradé silencieux qu'AD-16 interdit. Elle se dit.
  var VERDICTS = {
    couvert: "couvert",
    non_couvert: "non couvert",
    sous_conditions: "sous conditions",
    ne_tranche_pas: "ne tranche pas"
  };

  function libelleVerdict(value) {
    var v = String(value || "");
    if (Object.prototype.hasOwnProperty.call(VERDICTS, v)) {
      return { cle: v, texte: VERDICTS[v] };
    }
    return { cle: "inconnu", texte: "verdict non reconnu" };
  }

  // AD-4 : les quatre statuts d'une affirmation. `applicable` est le statut **du sinistre** (il reste
  // null en guide) : « humain » veut dire que le code a refusé de trancher, et c'est l'information
  // la plus utile de la ligne. `edition` s'affiche **avec sa réserve**, jamais comme un statut vert.
  var APPLICABLE = {
    oui: "applicable",
    non: "non applicable",
    humain: "applicabilité à confirmer par un humain"
  };
  var RAISONS_APPLICABILITE = {
    hors_portee: "sa portée contractuelle ne couvre pas le cas déclaré",
    faits_contraires: "les faits déclarés ne correspondent pas aux conditions de la clause"
  };

  function statutTexte(status) {
    if (!status) return "";
    var p = [];
    if (status.retrouvee === true) p.push("retrouvée");
    if (status.pertinente === true) p.push("pertinente");
    if (status.applicable && Object.prototype.hasOwnProperty.call(APPLICABLE, status.applicable)) {
      p.push(APPLICABLE[status.applicable]);
      if (status.applicable === "non" && status.applicable_reason &&
          Object.prototype.hasOwnProperty.call(RAISONS_APPLICABILITE, status.applicable_reason)) {
        p.push(RAISONS_APPLICABILITE[status.applicable_reason]);
      }
    }
    p.push("édition " + (status.edition ? status.edition : "non précisée") +
      " — actualité non vérifiée");
    return p.join(" · ");
  }

  // Le `ClaimStatus` de la claim qui cite ce bloc. C'est ce qui rend la réserve d'AD-4 au **mode
  // dégradé** : D6 exige « une liste plate de citations **avec leurs statuts** », et sans ce
  // rattachement par bloc, l'abandon de l'appariement faisait aussi disparaître `retrouvée`,
  // `pertinente`, `applicable` et la réserve d'édition — sous un verdict (revue 1.9).
  /** Les statuts des affirmations affichées qui citent ce bloc (au plus un par affirmation). */
  function statutsDeBloc(answer, blockId) {
    var claims = tableau(answer && answer.claims);
    var trouves = [];
    for (var i = 0; i < claims.length; i++) {
      var quotes = tableau(claims[i] && claims[i].quotes);
      for (var j = 0; j < quotes.length; j++) {
        if (quotes[j] && quotes[j].block_id === blockId) {
          trouves.push(claims[i].status || null);
          break;  // une seule quote par bloc dans une claim (AD-3)
        }
      }
    }
    return trouves;
  }

  /**
   * Le statut d'une clause en mode dégradé, ou `null` si rien ne le fixe **sans deviner**.
   *
   * Deux affirmations peuvent citer le même bloc avec des statuts différents — une clause jugée
   * applicable pour l'une, à confirmer pour l'autre. Rendre le premier trouvé, c'était rattacher le
   * statut d'une affirmation à une autre : exactement le dommage que D6 refuse (« une clause
   * attribuée à la mauvaise affirmation sous un verdict »), réintroduit par le repli censé l'éviter
   * (revue 1.9, tour 2).
   */
  function statutDeBloc(answer, blockId) {
    var trouves = statutsDeBloc(answer, blockId);
    if (!trouves.length) return null;
    for (var k = 1; k < trouves.length; k++) {
      if (statutTexte(trouves[k]) !== statutTexte(trouves[0])) return null;
    }
    return trouves[0];
  }

  /** Le bloc est-il cité par plusieurs affirmations qui n'en disent pas la même chose ? */
  function statutAmbigu(answer, blockId) {
    return statutsDeBloc(answer, blockId).length > 1 && statutDeBloc(answer, blockId) === null;
  }

  // AD-2 : `Block.kind` vient de l'ingestion, jamais du modèle. Un kind hors table se dit tel quel
  // plutôt que d'être rangé dans la case la plus proche.
  var KINDS = {
    garantie: "garantie", exclusion: "exclusion", condition: "condition", franchise: "franchise",
    definition: "définition", renvoi: "renvoi", para: "paragraphe", list: "liste",
    table: "tableau", heading: "titre", autre: "autre"
  };

  function libelleKind(kind) {
    var k = String(kind || "");
    return Object.prototype.hasOwnProperty.call(KINDS, k) ? KINDS[k] : k || "type inconnu";
  }

  // D7 : les clauses **non retrouvées** s'affichent avec le motif du rejet, jamais avec leur
  // citation — les quotes d'une claim `non_retrouvee`/`ambigue` sont restées les chaînes du modèle.
  var REJETS = {
    non_retrouvee: "citation introuvable dans le contrat",
    non_pertinente: "passage réel, mais jugé étranger au sinistre décrit",
    ambigue: "citation présente à plusieurs endroits, ou clause à deux types",
    non_citee: "affirmation vérifiée qu'aucune phrase affichée ne reprend"
  };

  function motifRejet(kind) {
    var k = String(kind || "");
    return Object.prototype.hasOwnProperty.call(REJETS, k) ? REJETS[k] : "écartée par la vérification";
  }

  // ---------- ce que le guide et cette page disent de la même façon (story 2.5) ----------
  //
  // D8 : les deux pages sont **autonomes** — celle-ci n'importe rien de `web/app/`, et c'est ce qui
  // lui permet de vivre sans la feuille de 1 300 lignes du site. Le prix en est la duplication de
  // trois tables. Elles ne sont donc pas laissées libres : `tests/test_tables_partagees.py` monte
  // les deux fichiers côte à côte et exige que ces phrases soient **identiques mot pour mot** à
  // celles de `web/app/chat.js`. Une reprise différée de 2.3 disait précisément cela : « la même
  // donnée est rendue avec deux niveaux d'explicitation selon la page ».
  //
  // Les cinq phrases d'état ont d'ailleurs été écrites en 2.3 **sans jamais nommer le document**,
  // exactement pour pouvoir servir ici, sur un contrat, aussi bien que là, sur le guide.

  function pluriel(n, mot) { return n + " " + mot + (n > 1 ? "s" : ""); }

  function entier(v) {
    return (typeof v === "number" && isFinite(v) && v >= 0) ? Math.floor(v) : 0;
  }

  // AD-4 : la preuve chiffrée d'une absence — termes **canoniques**, nombre de variantes, passages
  // parcourus. Jamais la liste des variantes ni des déclencheurs : le contrat ne les transporte pas.
  // Les compteurs s'affichent **même à zéro** : « rien trouvé sur 1 457 passages » et « rien n'a été
  // cherché » sont deux refus différents, que l'omission rendait indiscernables (reprise 1.9).
  function preuveAbsence(reason) {
    if (!reason) return "";
    if (reason.kind === "clarification_requise") return "";
    var termes = tableau(reason.terms_searched).filter(function (t) { return String(t || "").trim(); });
    var variantes = entier(reason.variants_count);
    var blocs = entier(reason.blocks_scanned);
    var chiffres = [
      pluriel(variantes, "variante") + (variantes > 1 ? " essayées" : " essayée"),
      pluriel(blocs, "passage") + (blocs > 1 ? " parcourus" : " parcouru")
    ];
    var debut = termes.length
      ? "Termes cherchés : " + termes.join(", ")
      : "Aucun terme du guide n'a été retenu";
    return debut + " — " + chiffres.join(", ");
  }

  // FR5 : les trois états, lus sur les deux booléens que *vérifier* calcule (AD-4).
  function etatReponse(answer) {
    var a = answer || {};
    if (!a.found) return { cle: "inconnu", texte: "inconnu" };
    if (!a.complete) return { cle: "partiel", texte: "partiel" };
    return { cle: "sur", texte: "sûr" };
  }

  // La phrase qui rend l'état explicite. Elle ne décrit que ce que la vue contient **réellement** :
  // `contexte` est renseigné par `vueVerdict()` à partir des blocs qu'elle vient de poser.
  function phraseEtat(etat, contexte) {
    var cle = (etat && etat.cle) || "inconnu";
    var c = contexte || {};
    if (cle === "sur") {
      return "tout ce qui est affirmé ci-dessus est appuyé par un passage cité, " +
        "et la question est couverte";
    }
    if (cle === "partiel") {
      return c.liste
        ? "il manque des éléments : ils sont listés sous « Ce que je ne sais pas »"
        : "il manque des éléments, et rien n'indique lesquels";
    }
    return c.preuve
      ? "rien n'a été retenu : la preuve de cette absence est ci-dessus"
      : "rien n'a été cherché : la question doit d'abord être précisée";
  }

  // Les `CheckResult.name` du serveur, en français — **la même table** que `web/app/chat.js`. Un nom
  // inconnu n'est jamais masqué : il s'affiche tel quel.
  var CONTROLES = {
    applicabilite_contradictoire: "deux jeux de champs d'applicabilité pour une même affirmation",
    applicabilite_hors_borne: "des libellés d'applicabilité dépassent leur borne",
    applicabilite_incomplete: "applicabilité non rendue pour une clause décisionnelle",
    candidats_non_ouverts: "des passages trouvés n'ont pas été ouverts par la navigation",
    citations: "citations relues dans le corpus",
    claims_non_citees: "affirmations vérifiées qu'aucune phrase affichée ne reprend",
    clarification_langue_non_affirmee: "clarification retirée : sa langue n'est pas affirmable",
    cout_eleve: "coût de la requête au-dessus du seuil",
    dictionnaire: "variantes du dictionnaire ajoutées aux termes cherchés",
    facettes_non_couvertes: "des sous-questions posées ne sont pas couvertes",
    fait_cite_hors_sujet: "un fragment cité pour une qualité n'en emploie aucun mot",
    fait_cite_introuvable: "une qualité dite établie ne cite aucun fragment des faits déclarés",
    faits_compris_hors_borne: "des faits compris dépassent leur borne",
    hors_perimetre_desarme: "refus hors périmètre désarmé : la liste des rubriques était tronquée",
    intention_expliquee: "intention rendue par le modèle, et déclencheurs qui la confirment",
    libelles_hors_borne: "des libellés de portée dépassent leur borne",
    lignes_incompletes: "un bloc cité n'est pas la concaténation de ses lignes",
    limites_non_affichees: "des phrases de limite n'ont pas été affichées",
    noeuds_du_profil: "fiches désignées par le profil déclaré",
    parse_retry: "réponse du modèle relancée après un parse invalide",
    pertinence_incomplete: "des affirmations sont restées sans verdict de pertinence",
    qualite_de_la_clause_non_enumeree: "une qualité écrite par la clause n'a pas été énumérée",
    qualite_exigee_non_etablie: "une qualité exigée par une clause n'est pas établie par les faits",
    qualites_non_enumerees: "les qualités exigées ou établies n'ont pas été énumérées",
    quote_trop_longue: "des citations vérifiées dépassent la longueur maximale",
    refus: "refus composé, avec sa preuve d'absence",
    repli_deterministe: "navigation par outils incomplète : repli déterministe borné",
    relance_abandonnee: "relance de la rédaction abandonnée faute de budget",
    relance_moins_bonne: "relance rendue moins bonne que le premier essai : écartée",
    relance_sans_effet: "relance sans effet sur la réponse",
    segment_contradictoire: "deux verdicts opposés pour une même phrase",
    segments_non_soutenus: "des phrases avancent plus que les passages joints",
    segments_retires: "des phrases ont été retirées de la réponse",
    verdict: "verdict rendu sur les affirmations affichées",
    verdict_contradictoire: "deux verdicts opposés pour une même affirmation"
  };

  function libelleControle(nom) {
    var n = String(nom || "");
    return Object.prototype.hasOwnProperty.call(CONTROLES, n) ? CONTROLES[n] : n;
  }

  // Les alertes du serveur, en français — **la même table** que `web/app/chat.js` et
  // `tools/accueil/accueil.js`. Une alerte inconnue n'est pas traduite : elle se dit telle quelle.
  var ALERTES = {
    sans_gate: "aucune question-témoin ne valide ce document",
    gate_perime: "le gate a été obtenu avec un autre code, d'autres prompts ou d'autres modèles",
    source_absente: "le fichier source n'est pas présent à côté des artefacts",
    rapport_illisible: "le rapport d'ingestion est présent mais illisible",
    rapport_etranger: "le rapport d'ingestion décrit un autre document",
    quarantaine: "document écarté au chargement",
    perimetre_tronque:
      "la liste des rubriques annoncée au modèle a été tronquée : des sujets traités par ce " +
      "document seraient jugés hors périmètre",
    ungated_refuse_en_production:
      "ALLOW_UNGATED a été posé en production : la dérogation a été refusée",
    dictionnaire_non_valide:
      "le dictionnaire des variantes n'a été validé par personne : le refus « zéro hit » est " +
      "désactivé",
    dictionnaire_corpus_perime:
      "le dictionnaire des variantes décrit un autre corpus que celui qui est servi"
  };

  // NFR4 : le coût **réel**, rendu par l'usage de l'API (`trace.total_cost_eur`), jamais estimé ici.
  function coutTexte(trace) {
    var c = trace && typeof trace.total_cost_eur === "number" ? trace.total_cost_eur : null;
    // `isFinite` et non `!isNaN` : `Infinity` passe le second et afficherait « coûté Infinity € ».
    // Un coût négatif n'existe pas non plus — dans les deux cas on préfère ne rien dire (revue 1.9).
    if (c === null || !isFinite(c) || c < 0) return "";
    if (c === 0) return "cette analyse n'a rien coûté (aucun appel facturé)";
    return "cette analyse a coûté " + c.toFixed(4).replace(".", ",") + " €";
  }

  // FR11 / AD-16 : un message lisible composé depuis le **code** d'AD-16. Le `message` du serveur
  // n'est jamais affiché (pydantic, en anglais, avec le chemin du champ). Aucune de ces phrases ne
  // propose de repli : il n'y en a pas pour le sinistre.
  function messageErreur(erreur) {
    var e = erreur || {};
    // Le seul « échec » que la page constate elle-même, avant tout réseau : le détail est composé
    // par `manquant()`, jamais par le serveur, et il dit quoi corriger.
    if (e.code === "saisie_incomplete") {
      return String(e.detail || "La demande est incomplète.");
    }
    if (e.code === "hors_ligne") {
      return "Cette page est ouverte depuis un fichier local : l'outil a besoin du serveur qui la " +
        "sert. Ouvrez-la depuis son adresse.";
    }
    if (e.code === "reseau") {
      return "Le serveur n'a pas pu être joint : rien n'a été analysé.";
    }
    if (e.code === "timeout_client") {
      return "Le serveur n'a pas répondu dans le temps imparti : rien n'a été analysé.";
    }
    if (e.code === "rate_limited") {
      var s = e.retry_after;
      return typeof s === "number" && s > 0
        ? "Trop de demandes en peu de temps : réessayez dans " + s + " seconde" + (s > 1 ? "s" : "") + "."
        : "Trop de demandes en peu de temps : réessayez dans un instant.";
    }
    if (e.code === "invalid_request") {
      return "Le serveur a refusé la demande : elle sort de ce que le contrat de l'API accepte " +
        "(contrat non servi, description trop longue, ou champ manquant).";
    }
    if (e.code === "input_too_long") {
      return "La demande envoyée est trop volumineuse pour le serveur. Raccourcissez la description.";
    }
    if (e.code === "reponse_illisible") {
      return "Le serveur a répondu quelque chose que cette page ne sait pas lire : aucun verdict " +
        "n'est affiché plutôt qu'un verdict incomplet.";
    }
    if (e.code === "corpus_unavailable") {
      return "Le contrat demandé n'est pas servi en ce moment : rien n'a été analysé.";
    }
    // Les quatre autres 503 d'AD-16 disent la même chose à l'utilisateur — le serveur n'a pas pu
    // aller au bout —, et la phrase se compose sur le **code**, pas sur le `kind` : le kind est une
    // commodité de la couche réseau, le code est le contrat. Aucune de ces phrases ne propose de
    // repli : AD-16, « aucun repli pour le sinistre ».
    if (e.code === "llm_unavailable" || e.code === "llm_parse" || e.code === "timeout" ||
        e.code === "budget_exceeded" || e.kind === "indisponible") {
      return "L'analyse est indisponible pour le moment : rien n'a été analysé.";
    }
    return "Le serveur n'a pas pu traiter cette demande. Réessayez plus tard.";
  }

  // ---------- appariement clause ↔ affirmation (D6) ----------
  //
  // `ClauseSource` ne porte pas de `claim_id` : AD-11 n'en prévoit pas. Mais le serveur construit
  // `sources[]` par l'énumération `for claim in answer.claims for quote in claim.quotes`
  // (`api/presenter.clauses_de`) et publie `answer` en entier — la page refait donc **la même**
  // énumération et lit `sources[]` dans l'ordre, en vérifiant à chaque pas que le `block_id`
  // concorde. C'est exactement ce que fait `citationsParSegment` côté guide.
  //
  // Au moindre désaccord — un `block_id` qui ne concorde pas, des longueurs différentes —
  // l'appariement est **abandonné** (`null`) et la page le **dit**, puis affiche une liste plate.
  // Jamais un rattachement deviné : une clause attribuée à la mauvaise affirmation sous un verdict
  // est pire qu'une liste sans rattachement.
  function clausesParClaim(answer, sources) {
    var a = answer || {};
    var claims = tableau(a.claims);
    var plates = tableau(sources);
    var out = [];
    var rang = 0;
    for (var c = 0; c < claims.length; c++) {
      var claim = claims[c] || {};
      var quotes = tableau(claim.quotes);
      var clauses = [];
      for (var q = 0; q < quotes.length; q++) {
        var src = plates[rang];
        var attendue = quotes[q];
        // Une quote sans `block_id` lisible n'apparie rien : abandonner est la seule issue honnête
        // (D6), et c'est aussi ce qui évite un `TypeError` sur une entrée nulle (revue 1.9).
        if (!attendue || typeof attendue.block_id !== "string") return null;
        if (!src || src.block_id !== attendue.block_id) return null;
        clauses.push(src);
        rang++;
      }
      out.push({ claim_id: claim.claim_id, text: claim.text, status: claim.status || null,
                 clauses: clauses });
    }
    if (rang !== plates.length) return null;
    return out;
  }

  // ---------- le corps posté ----------
  //
  // AD-11 : les quatre champs du contrat, et pas un de plus. Pas de `dossier` (D1) : la route ne
  // l'expose pas, tout le paquet contractuel reste réputé inconnu, et c'est ce que « au regard des
  // conditions générales seules » veut dire. Les champs vides ne sont **pas** envoyés : `Faits.date`
  // et `Faits.lieu` sont `str | None`, et une chaîne vide n'est pas l'absence.
  /**
   * Le montant saisi, en nombre — ou `null` s'il n'y en a pas, ou `false` s'il est illisible.
   *
   * Trois issues, et non deux : le champ est **facultatif** (vide ⇒ `null`, rien à envoyer), mais
   * une saisie non vide qui n'est pas un nombre fini positif ou nul est une **erreur de saisie**
   * (`false`), pas une absence. `Faits.montant_eur` est un `float | None` : le confondre avec
   * l'absence faisait analyser un sinistre sans son montant sans que personne ne le dise.
   * La virgule décimale française est acceptée — la page lit « 1200,50 », et `Faits.montant_eur`
   * est un `float`.
   */
  function montantSaisi(brut) {
    var t = String(brut === undefined || brut === null ? "" : brut).trim();
    if (t === "") return null;
    var n = Number(t.replace(",", "."));
    return (isFinite(n) && n >= 0) ? n : false;
  }

  function corpsSinistre(saisie) {
    var s = saisie || {};
    var faits = { description: String(s.description || "") };
    ["date", "lieu"].forEach(function (nom) {
      var v = String(s[nom] || "").trim();
      if (v) faits[nom] = v;
    });
    var n = montantSaisi(s.montant_eur);
    // Un montant illisible n'est **pas** envoyé à 0 : le serveur le refuserait ou, pire,
    // l'accepterait comme un sinistre à zéro euro. Il n'est pas non plus **supprimé** en silence —
    // `manquant()` le refuse en amont (revue Codex 1.9, tour 1, I1) : avec `novalidate`, un `-100`
    // saisi repartait en requête sans `montant_eur`, et le pipeline analysait alors des faits qui
    // ne sont pas ceux qu'on a écrits. Ce corps ne compose que ce qui a été validé.
    if (typeof n === "number") faits.montant_eur = n;
    return {
      doc_id: String(s.doc_id || ""),
      question: String(s.question || ""),
      faits: faits
    };
  }

  // ---------- les vues ----------

  function vueAttente() {
    return noeud("div", "carte attente", null, [
      noeud("p", null, "Je cherche les clauses du contrat, puis je vérifie chaque citation mot pour " +
        "mot avant d'afficher quoi que ce soit…"),
      // Ni « quatre appels » ni « une dizaine de secondes » n'étaient vrais (revue 1.9, tour 2) :
      // la relance d'AD-3 ajoute un second *rédiger* **et** une seconde vérification (plafond
      // nominal : cinq appels, plus le retry de parse du client), et toutes les mesures live
      // tiennent entre 21 et 27 secondes, une à 47. Ce texte s'affiche à chaque soumission ; il
      // annonce donc un ordre de grandeur mesuré, et pas un compte que le pipeline peut dépasser.
      noeud("p", "attente-note",
        "Plusieurs appels au modèle s'enchaînent, et une vérification peut en relancer un : "
        + "comptez de vingt secondes à une minute.")
    ]);
  }

  function vueAudits(documents, echec) {
    var connus = tableau(documents).filter(function (d) {
      return d && typeof d.doc_id === "string" && d.doc_id;
    });
    if (echec) {
      return noeud("p", "audit-erreur",
        "La liste des rapports d'ingestion n'a pas pu être chargée.");
    }
    if (!connus.length) {
      return noeud("p", "audit-vide", "Aucun document n'est connu du loader.");
    }
    return noeud("ul", "audit-liste", null, connus.map(function (d) {
      var statut = d.status === "quarantaine" ? "quarantaine" : "servi";
      var meta = "statut effectif : " + statut;
      if (d.edition) meta += " · édition " + String(d.edition);
      var enfants = [
        noeud("span", "audit-titre", String(d.title || d.doc_id)),
        noeud("span", "audit-meta", meta)
      ];
      if (statut === "quarantaine") {
        enfants.push(noeud("span", "audit-raison",
          "raison : " + (d.raison ? String(d.raison) : "indisponible")));
      }
      var lien = noeud("a", "audit-lien", "rapport d'ingestion");
      lien.href = "/sinistre/ingestion/" + encodeURIComponent(String(d.doc_id));
      enfants.push(lien);
      var entree = noeud("li", "audit-entree", null, enfants);
      entree.cls += " audit-" + statut;
      return entree;
    }));
  }

  // L'état du sélecteur de contrat. Seuls les `kind="contrat"` y entrent : le guide **est** servi et
  // `GET /api/v1/documents` le liste (il ne ment pas sur ce qui est servi), mais lui soumettre un
  // sinistre n'a pas de sens — aucun de ses blocs n'est une garantie ou une exclusion —, et le
  // serveur le refuse aussi (D3). Aucun contrat ⇒ le formulaire est **désactivé** et le dit : c'est
  // le seul écran où « rien à analyser » doit se lire avant qu'on ait écrit une description.
  function vueFormulaire(documents, echec) {
    var contrats = tableau(documents).filter(function (d) {
      if (!d || d.kind !== "contrat") return false;
      // ``selectionnable`` vient du statut effectif du loader. Le repli maintient la lecture des
      // anciennes réponses pendant un déploiement progressif, mais refuse toujours une quarantaine.
      return d.selectionnable === true ||
        (d.selectionnable === undefined && d.status === "servi");
    });
    var options = contrats.map(function (d) {
      var titre = String(d.title || d.doc_id);
      // AD-4 : l'édition s'affiche **avec sa réserve**, jamais comme un statut vert. `edition` est
      // un `str` sans `min_length` : vide, elle faisait disparaître la réserve avec elle (revue
      // 1.9), alors que c'est justement là qu'on sait le moins de quoi on parle.
      var edition = " — édition " + (d.edition ? String(d.edition) : "non précisée") +
        " (actualité non vérifiée)";
      return { valeur: String(d.doc_id), texte: titre + edition };
    });
    var vue = {
      actif: contrats.length > 0,
      options: options,
      // La source publique du contrat sélectionné : c'est ce qui rend l'édition vérifiable par
      // celui à qui on l'annonce. Le PDF n'est pas redistribué par ce service (AD-7).
      sources: contrats.map(function (d) {
        return { doc_id: String(d.doc_id), url: lienHttp(d.source_url) };
      }),
      audits: vueAudits(documents, echec),
      // Deux situations, deux phrases (revue 1.9) : « le serveur dit qu'aucun contrat n'est servi »
      // et « le serveur n'a pas répondu » ne se corrigent pas de la même façon, et servir la
      // première pour la seconde ferait affirmer à la page quelque chose qu'elle ne sait pas.
      message: contrats.length
        ? null
        : (echec
          ? "La liste des contrats n'a pas pu être chargée : rien n'est proposé tant que le " +
            "serveur n'a pas répondu. Rechargez la page."
          : "Aucun contrat n'est servi en ce moment : il n'y a rien à confronter à un sinistre. " +
            "L'état du service est publié sur /api/v1/sante.")
    };
    return vue;
  }

  function clauseVue(src, status, contexte) {
    var meta = [noeud("span", "cl-kind", libelleKind(src.kind))];
    if (src.kind_confirmed === false) {
      // AD-6 : un `kind` non confirmé plafonne le verdict. Afficher « garantie » sans le dire
      // donnerait au lecteur une certitude que le pipeline n'a pas.
      meta.push(noeud("span", "cl-doute", "typage non confirmé"));
    }
    if (typeof src.page === "number") meta.push(noeud("span", "cl-page", "page " + src.page));
    var statut = statutTexte(status);
    if (statut) meta.push(noeud("span", "cl-statut", statut));
    var enfants = [
      noeud("blockquote", "cl-q", "« " + String(src.quote || "") + " »"),
      noeud("div", "cl-meta", null, meta)
    ];
    var c = contexte || {};
    if (typeof c.doc_id === "string" && c.doc_id && typeof src.page === "number" &&
        isFinite(src.page) && Math.floor(src.page) === src.page && src.page > 0) {
      var attrs = {
        "data-doc-id": c.doc_id,
        "data-page": String(src.page),
        "data-block-ids": JSON.stringify([String(src.block_id)]),
        "data-line-ids": JSON.stringify(tableau(src.line_ids))
      };
      var source = lienHttp(c.source_url);
      if (source) attrs["data-source-url"] = source;
      enfants.push(noeud("button", "cl-ouvrir", "Voir la page " + src.page + " dans le PDF",
                         null, attrs));
    }
    return noeud("div", "clause", null, enfants);
  }

  // AD-4/D4 : « les faits compris » sont ce que *comprendre* a extrait des faits déclarés, pas la
  // description renvoyée en écho. C'est le seul endroit où l'utilisateur peut constater qu'il a été
  // mal compris — et c'est pour cela que l'AC l'exige.
  var CHAMPS_COMPRIS = [
    { cle: "bien", libelle: "Bien concerné" },
    { cle: "evenement", libelle: "Événement" },
    { cle: "lieu", libelle: "Lieu" },
    { cle: "cause", libelle: "Cause" },
    { cle: "moment", libelle: "Moment" }
  ];

  function faitsComprisVue(faits) {
    if (!faits) return null;
    var lignes = [];
    CHAMPS_COMPRIS.forEach(function (c) {
      var v = faits[c.cle];
      if (v === undefined || v === null || String(v).trim() === "") return;
      lignes.push(noeud("div", "fc-ligne", null, [
        noeud("span", "fc-cle", c.libelle),
        noeud("span", "fc-val", String(v))
      ]));
    });
    var themes = tableau(faits.themes).filter(function (t) { return String(t || "").trim(); });
    if (themes.length) {
      lignes.push(noeud("div", "fc-ligne", null, [
        noeud("span", "fc-cle", "Thèmes"),
        noeud("span", "fc-val", themes.join(", "))
      ]));
    }
    if (!lignes.length) return null;
    return section("faits-compris", "Ce que j'ai compris du sinistre", [
      noeud("div", "fc", null, lignes),
      noeud("p", "fc-note", "Relu depuis votre description par le modèle. Si l'un de ces éléments " +
        "est faux, le verdict porte sur autre chose que votre sinistre.")
    ]);
  }

  var PIECES = [
    { cle: "conditions_particulieres", libelle: "les conditions particulières" },
    { cle: "options_souscrites", libelle: "les options souscrites" },
    { cle: "avenants", libelle: "les avenants" },
    { cle: "date_effet", libelle: "la date d'effet" }
  ];

  // AD-6 : `MissingPackage` accompagne **toujours** le verdict, y compris sous un « couvert ». C'est
  // la mesure de ce que le verdict ne pouvait pas voir.
  function paquetVue(missing) {
    if (!missing) return null;
    var absentes = PIECES.filter(function (p) { return missing[p.cle] !== false; })
      .map(function (p) { return p.libelle; });
    var faits = tableau(missing.faits).filter(function (f) { return String(f || "").trim(); });
    if (!absentes.length && !faits.length) return null;
    var enfants = [];
    if (absentes.length) {
      enfants.push(noeud("p", null, "Pièces du contrat non lues : " + absentes.join(", ") + "."));
    }
    if (faits.length) {
      enfants.push(noeud("p", null, "Faits que les clauses citées exigent et que la description " +
        "n'établit pas :"));
      enfants.push(liste("paquet-faits", faits));
    }
    return section("paquet", "Ce qui manque au dossier", enfants);
  }

  function estObjetPlat(v) { return !!v && typeof v === "object" && !Array.isArray(v); }

  function entierOuNull(v) {
    return (typeof v === "number" && isFinite(v) && Math.floor(v) === v) ? v : null;
  }

  /** Une ligne de trace, avec ou sans pictogramme d'état (le mot le dit déjà, lui ne fait que le répéter). */
  function ligneTrace(texte, etat) {
    if (!etat) return noeud("li", "pq-ligne", String(texte));
    return noeud("li", "pq-ligne", null, [
      noeud("span", etat === "ok" ? "pq-ok" : "pq-ko", etat === "ok" ? "✓" : "✗", null,
            { "aria-hidden": "true" }),
      noeud("span", "pq-txt", String(texte))
    ]);
  }

  function rubriqueTrace(titre, lignes) {
    if (!lignes.length) return null;
    return noeud("div", "pq-bloc", null, [
      noeud("strong", "pq-titre", titre),
      noeud("ul", "pq-liste", null, lignes)
    ]);
  }

  /**
   * Les blocs que les étapes nomment, résolus par `trace.blocs` (story 2.5).
   *
   * Un identifiant que `trace.blocs` ne résout pas s'affiche **seul** : la page n'a aucun moyen de
   * retrouver le titre d'une clause, et en deviner un sous un verdict serait pire que l'absence.
   */
  function lignesDeBlocs(steps, blocs, champ) {
    var titres = Object.create(null);
    blocs.forEach(function (b) {
      if (estObjetPlat(b) && typeof b.block_id === "string" && typeof b.titre === "string") {
        titres[b.block_id] = b.titre;
      }
    });
    var vus = Object.create(null);
    var out = [];
    steps.forEach(function (s) {
      tableau(s[champ]).forEach(function (id) {
        if (typeof id !== "string" || vus[id]) return;
        vus[id] = 1;
        out.push(ligneTrace(titres[id] ? id + " — " + titres[id] : id));
      });
    });
    return out;
  }

  /** Le gate du contrat interrogé, et les alertes que le serveur pose sur lui. */
  function lignesGate(g) {
    if (!estObjetPlat(g)) return [];
    var out = [];
    if (typeof g.profile === "string" && g.profile) {
      var cases = entierOuNull(g.cases);
      out.push(ligneTrace("profil de validation : " + g.profile +
                          (cases !== null ? " (" + cases + " cas)" : "")));
    } else if (g.profile === null && tableau(g.alerts).indexOf("sans_gate") === -1) {
      out.push(ligneTrace("aucune question-témoin ne valide ce document", "ko"));
    }
    if (typeof g.countersigned === "boolean") {
      out.push(ligneTrace(g.countersigned
        ? "relecture des cas contresignée à la main"
        : "relecture des cas non contresignée : elle est celle de la boucle autonome",
        g.countersigned ? "ok" : "ko"));
    }
    var alertesVues = {};
    tableau(g.alerts).forEach(function (a) {
      if (typeof a !== "string" || !a) return;
      if (Object.prototype.hasOwnProperty.call(alertesVues, a)) return;
      alertesVues[a] = true;
      var connue = Object.prototype.hasOwnProperty.call(ALERTES, a);
      out.push(ligneTrace(connue ? ALERTES[a] + " (" + a + ")" : a, "ko"));
    });
    return out;
  }

  /** Les seuils actifs, repliés : ils sont nombreux et rarement lus. */
  function vueSeuils(thresholds) {
    if (!estObjetPlat(thresholds)) return null;
    var lignes = [];
    Object.keys(thresholds).sort().forEach(function (nom) {
      var v = thresholds[nom];
      if (typeof v !== "number" || !isFinite(v)) return;
      lignes.push(ligneTrace(nom + " : " + String(v)));
    });
    if (!lignes.length) return null;
    return noeud("details", "pq-seuils", null, [
      noeud("summary", null, "Seuils actifs (" + lignes.length + ")"),
      noeud("ul", "pq-liste", null, lignes)
    ]);
  }

  /**
   * « Comment cette réponse a été obtenue » — le même contenu que le panneau du guide (story 2.5).
   *
   * La trace ne portait ici que trois lignes plates : la référence, le pipeline, une ligne par
   * étape avec les **noms bruts** de ses contrôles (« applicabilite_incomplete »), et le coût. Tout
   * le reste voyageait sans être montré : les clauses ouvertes et écartées, l'issue de chaque
   * contrôle, les relances, les seuils actifs, le gate du contrat.
   *
   * **Ce que la trace ne dit pas, l'écran ne le dit pas** (AD-16) : chaque rubrique naît de la
   * présence de son champ, et une trace pauvre affiche moins de rubriques, jamais des rubriques
   * vides ou remplies d'un défaut.
   */
  function traceVue(trace) {
    if (!estObjetPlat(trace)) return null;
    var t = trace;
    var steps = tableau(t.steps).filter(estObjetPlat);
    var enfants = [noeud("summary", null, "Comment cette réponse a été obtenue")];

    var etapes = rubriqueTrace("Étapes", steps.map(function (s) {
      var parts = [String(s.name || "")];
      parts.push(typeof s.tier === "string" && s.tier ? s.tier : "aucun appel");
      var ms = entierOuNull(s.ms);
      if (ms !== null) parts.push(ms + " ms");
      return ligneTrace(parts.join(" · "));
    }));
    if (etapes) enfants.push(etapes);

    var ouverts = rubriqueTrace("Clauses ouvertes",
                                lignesDeBlocs(steps, tableau(t.blocs), "opened_block_ids"));
    if (ouverts) enfants.push(ouverts);
    var ecartes = rubriqueTrace("Clauses écartées, non lues par le modèle",
                                lignesDeBlocs(steps, tableau(t.blocs), "discarded_block_ids"));
    if (ecartes) enfants.push(ecartes);

    var controles = [];
    steps.forEach(function (s) {
      tableau(s.checks).forEach(function (c) {
        if (!estObjetPlat(c) || typeof c.name !== "string") return;
        var detail = typeof c.detail === "string" && c.detail ? " — " + c.detail : "";
        controles.push(ligneTrace(libelleControle(c.name) + detail, c.ok === true ? "ok" : "ko"));
      });
    });
    var vueControles = rubriqueTrace("Contrôles", controles);
    if (vueControles) enfants.push(vueControles);

    var compteurs = [];
    var retries = entierOuNull(t.retries);
    if (retries !== null) compteurs.push(ligneTrace(pluriel(retries, "relance")));
    var troncatures = entierOuNull(t.truncations);
    if (troncatures !== null) compteurs.push(ligneTrace(pluriel(troncatures, "troncature")));
    var cout = coutTexte(t);
    if (cout) compteurs.push(ligneTrace(cout));
    var vueCompteurs = rubriqueTrace("Ce que l'analyse a coûté", compteurs);
    if (vueCompteurs) enfants.push(vueCompteurs);

    var seuils = vueSeuils(t.thresholds);
    if (seuils) enfants.push(seuils);

    var gate = rubriqueTrace("Validation du contrat interrogé", lignesGate(t.gate));
    if (gate) enfants.push(gate);

    var identite = [];
    if (typeof t.pipeline === "string" && t.pipeline) {
      identite.push(ligneTrace("pipeline : " + t.pipeline +
                               (t.variant ? " · variante " + t.variant : "")));
    }
    if (typeof t.request_id === "string" && t.request_id) {
      identite.push(ligneTrace("référence de requête : " + t.request_id));
    }
    var vueIdentite = rubriqueTrace("Cette requête", identite);
    if (vueIdentite) enfants.push(vueIdentite);

    if (enfants.length === 1) return null;
    // `<details>` natif : la trace est dépliable sans une ligne de JavaScript, donc sans état.
    return noeud("details", "trace", null, enfants);
  }

  function vueVerdict(reponse, contexte) {
    var r = reponse || {};
    var a = r.answer || {};
    var verdict = a.verdict || null;
    var sources = tableau(r.sources);
    var enfants = [];

    var v = libelleVerdict(verdict && verdict.value);
    enfants.push(noeud("div", "verdict-tete", null, [
      noeud("span", "badge verdict-" + v.cle, v.texte),
      noeud("span", "portee", PORTEE)
    ]));

    if (verdict && String(verdict.reason || "").trim()) {
      enfants.push(noeud("p", "verdict-raison", String(verdict.reason)));
    }

    // Le texte de la réponse est **rendu par le serveur** depuis les segments vérifiés (AD-3) : la
    // page ne recompose rien, elle le pose.
    if (String(a.texte || "").trim()) {
      enfants.push(section("analyse", "Ce que disent les clauses retenues", [
        noeud("p", "analyse-txt", String(a.texte))
      ]));
    }

    var compris = faitsComprisVue(a.faits_compris);
    if (compris) enfants.push(compris);

    var paquet = paquetVue(verdict && verdict.missing);
    if (paquet) enfants.push(paquet);

    var questions = tableau(verdict && verdict.ask_client)
      .filter(function (q) { return String(q || "").trim(); });
    if (questions.length) {
      enfants.push(section("ask", "Questions à poser au client", [liste("ask-liste", questions)]));
    }

    var escalade = tableau(verdict && verdict.escalate)
      .filter(function (q) { return String(q || "").trim(); });
    if (escalade.length) {
      enfants.push(section("escalate", "Points à faire trancher par un humain",
        [liste("escalate-liste", escalade)]));
    }

    // Les clauses citées, rattachées à l'affirmation qu'elles soutiennent quand l'appariement
    // retombe (D6) ; sinon une liste plate, et la page **le dit**.
    var appariees = clausesParClaim(a, sources);
    if (sources.length) {
      var corps = [];
      if (appariees) {
        appariees.forEach(function (entree) {
          if (!entree.clauses.length) return;
          corps.push(noeud("div", "affirmation", null,
            [noeud("p", "aff-txt", String(entree.text || ""))].concat(
              entree.clauses.map(function (src) { return clauseVue(src, entree.status, contexte); }))));
        });
      } else {
        corps.push(noeud("p", "degrade",
          "Les clauses ci-dessous fondent ce verdict, mais je n'ai pas pu rattacher chacune à " +
          "l'affirmation exacte qu'elle soutient : elles sont données ensemble."));
        // D6 : « avec leurs statuts ». Le mode dégradé serait le dernier endroit où taire
        // l'applicabilité d'une clause et la réserve d'actualité de son édition.
        var ambigus = 0;
        sources.forEach(function (src) {
          // Le compte ne porte que sur l'**ambiguïté** : une clause qu'aucune affirmation affichée
          // ne cite n'a pas de statut à taire, elle n'en a pas.
          if (statutAmbigu(a, src.block_id)) ambigus++;
          corps.push(clauseVue(src, statutDeBloc(a, src.block_id), contexte));
        });
        if (ambigus) {
          corps.push(noeud("p", "degrade",
            (ambigus > 1
              ? "Le statut de " + ambigus + " de ces clauses n'est pas affiché : plusieurs "
              : "Le statut de l'une de ces clauses n'est pas affiché : plusieurs ") +
            "affirmations la citent en n'en disant pas la même chose, et je ne devine pas " +
            "laquelle s'applique ici."));
        }
      }
      enfants.push(section("clauses", "Clauses citées, relues dans le contrat", corps));
    }

    // D7 : les clauses non retrouvées, **sans** leur citation — la quote d'une claim rejetée sur ses
    // citations est restée la chaîne du modèle, et rien ne prouve qu'elle existe dans le contrat.
    var rejetees = tableau(a.rejected_claims).filter(function (c) { return c; });
    if (rejetees.length) {
      // Le titre et la note valent pour **les quatre** `rejection_kind` d'AD-4 (revue 1.9) : dire
      // « non retrouvées » les couvrait mal — `non_pertinente` désigne un passage bien réel, et
      // `non_citee` une affirmation vérifiée qu'aucune phrase affichée ne reprend. Ce qui leur est
      // commun, et seulement cela, c'est qu'elles ont été écartées et que leur citation n'est pas
      // montrée (D7). Le motif exact est sur chaque ligne.
      enfants.push(section("rejetees", "Affirmations écartées par la vérification", [
        noeud("p", "rejetees-note",
          "Le modèle a avancé ces affirmations ; les contrôles les ont écartées. Aucune de leurs " +
          "citations n'est affichée : le motif de chacune est donné en dessous."),
        noeud("div", "rejetees-liste", null, rejetees.map(function (c) {
          return noeud("div", "rejetee", null, [
            noeud("p", "rej-txt", String(c.text || "")),
            noeud("p", "rej-motif", motifRejet(c.rejection_kind)),
            // D7 demande « le motif du rejet en français **et** le kind du rejet » : le premier est
            // la phrase ci-dessus, le second est la valeur typée du contrat, posée telle quelle.
            // `RejectedClaim.motif`, lui, reste dehors : il est composé pour la **relance** de
            // *rédiger* et cite des `block_id` qui, sur une claim `non_retrouvee`, sont ceux que le
            // modèle a inventés — un identifiant non fiable sous un verdict n'apprend rien.
            noeud("p", "rej-kind", String(c.rejection_kind || ""))
          ]);
        }))
      ]));
    }

    // M15 / reprise différée de 1.9 : la **preuve chiffrée** de l'absence. Le contrat la transporte
    // depuis toujours (`answer.reason` est publié entier) et le guide l'affiche depuis 1.7 ; ici,
    // seule la phrase composée par le serveur était rendue. « Rien trouvé sur 1 457 passages » et
    // « rien n'a été cherché » sont deux refus différents, et l'omission les rendait
    // indiscernables. `reason` absent ⇒ aucune preuve : rien n'est fabriqué (AD-16).
    var preuve = preuveAbsence(a.reason);
    if (preuve) enfants.push(noeud("p", "preuve", preuve));

    var inconnus = tableau(a.unknown).filter(function (x) { return String(x || "").trim(); });
    if (inconnus.length) {
      enfants.push(section("inconnu", "Ce que je ne sais pas", [liste("inconnu-liste", inconnus)]));
    }

    if (a.clarification) {
      enfants.push(section("clarif", "Une précision, pour chercher au bon endroit", [
        noeud("p", "clarif-q", String(a.clarification))
      ]));
    }

    // FR5 / reprise différée de 2.3 : le cadre des **trois états**, celui-là même que le chat pose
    // depuis 2.3. La page rendait déjà les phrases de lacune composées par le code, mais sans le
    // badge ni la phrase qui les encadrent : la même donnée était rendue avec deux niveaux
    // d'explicitation selon la page. Le badge d'état n'est pas le badge de **verdict** — l'un dit
    // ce que le contrat prévoit, l'autre ce que la vérification a pu établir — et les deux se
    // lisent : un « sous conditions » sur une réponse *partielle* est une réserve de plus.
    //
    // Il n'apparaît que si `answer.reason` est là ou si la réponse est trouvée, c'est-à-dire quand
    // `found`/`complete` ont un sens complet : un corps sans `reason` sur un `found=false` n'a pas
    // été écrit par la route, et le badge dirait « inconnu » sur rien (M15).
    if (a.found === true || a.reason) {
      var etat = etatReponse(a);
      enfants.push(noeud("div", "pied", null, [
        noeud("span", "etat etat-" + etat.cle, etat.texte),
        noeud("span", "etat-phrase",
              phraseEtat(etat, { liste: inconnus.length > 0, preuve: !!preuve }))
      ]));
    }

    var trace = traceVue(r.trace);
    if (trace) enfants.push(trace);

    return noeud("div", "carte resultat", null, enfants);
  }

  // AD-16 : une erreur affiche l'erreur, sa référence de requête, et **rien d'autre**. Aucun bouton,
  // aucun repli, aucun verdict de remplacement — et l'appelant efface le verdict précédent avant de
  // peindre celle-ci, pour qu'un ancien badge ne reste pas à l'écran sous un message d'échec.
  function vueErreur(erreur) {
    var e = erreur || {};
    var enfants = [
      noeud("strong", "err-titre", "Aucun verdict n'a été rendu"),
      noeud("p", "err-txt", messageErreur(e))
    ];
    if (e.request_id) {
      enfants.push(noeud("p", "err-ref", "référence de requête : " + String(e.request_id)));
    }
    enfants.push(noeud("p", "err-note",
      "Rien n'est deviné à la place du serveur : il n'y a pas de mode dégradé pour un sinistre."));
    return noeud("div", "carte erreur", null, enfants);
  }

  // ---------- le matérialiseur : il peint, il ne décide pas ----------
  //
  // AD-15 : tout ce qui vient du serveur est posé par `textContent`. `innerHTML` n'est employé que
  // pour **vider** un conteneur (chaîne vide) — le DOM minimal des tests lève sur toute autre pose.
  function materialiser(vue) {
    var e = document.createElement(vue.tag);
    if (vue.cls) e.className = vue.cls;
    if (vue.tag === "button") e.type = "button";
    if (vue.href) {
      e.href = vue.href;
      // Une source HTTP(S) quitte l'application : nouvel onglet et isolation de l'opener. Les
      // liens de navigation internes (dont les rapports d'ingestion) gardent la page courante et
      // n'annoncent pas à tort une navigation externe.
      if (lienHttp(vue.href)) {
        e.target = "_blank";
        e.rel = "noopener noreferrer";
      }
    }
    if (vue.texte !== undefined) e.textContent = vue.texte;
    if (vue.attrs) {
      Object.keys(vue.attrs).forEach(function (nom) { e.setAttribute(nom, String(vue.attrs[nom])); });
    }
    (vue.enfants || []).forEach(function (enfant) { e.appendChild(materialiser(enfant)); });
    return e;
  }

  function vider(cible) {
    if (cible) cible.innerHTML = "";
  }

  function peindre(vue, cible) {
    var hote = cible || document.getElementById("resultat");
    // La garde était à moitié posée : `vider()` tolérait un hôte absent, `appendChild` non. Un
    // `#resultat` renommé aurait donc levé un `TypeError` **après** un appel payé, en laissant la
    // carte d'attente à l'écran et la saisie verrouillée (revue 1.9, tour 2).
    if (!hote) return null;
    vider(hote);
    var e = materialiser(vue);
    hote.appendChild(e);
    return e;
  }

  // ---------- réseau ----------

  function erreurSinistre(d) {
    var e = new Error(d.code || d.kind);
    e.nom = "ErreurSinistre";
    e.kind = d.kind;
    e.code = d.code || "";
    e.statut = d.statut || 0;
    e.retry_after = (typeof d.retry_after === "number" && !isNaN(d.retry_after)) ? d.retry_after : null;
    e.request_id = d.request_id || "";
    return e;
  }

  function erreurHttp(statut, entetes, corps) {
    var err = (corps && corps.error) || {};
    var retry = parseInt(entetes && entetes.get ? entetes.get("Retry-After") : null, 10);
    return erreurSinistre({
      // `indisponible` n'ouvre **aucun** repli ici (AD-16 : « aucun repli pour le sinistre ») ; le
      // kind sert uniquement à composer une phrase honnête sur ce qui s'est passé.
      kind: statut === 503 ? "indisponible" : "requete",
      code: typeof err.code === "string" ? err.code : "",
      statut: statut,
      retry_after: isFinite(retry) ? retry : null,
      request_id: typeof err.request_id === "string" ? err.request_id : ""
    });
  }

  function enLigne() { return !!API_BASE; }

  function estObjet(v) { return !!v && typeof v === "object" && !Array.isArray(v); }
  function estChaine(v) { return typeof v === "string"; }
  function estBooleen(v) { return typeof v === "boolean"; }
  // `isFinite` et non `typeof === "number"` : `NaN` est un nombre pour JavaScript, et « page NaN »
  // est un numéro de page inventé — la même raison qu'à `coutTexte()`.
  function estNombre(v) { return typeof v === "number" && isFinite(v); }

  /**
   * Un champ `X | None` du contrat. `null` en est une **valeur**, pas une absence : les routes
   * publient avec `response_model_exclude_none=False` (`routes/sinistre.py:48`), donc pydantic
   * sérialise **toujours** la clé, `None` compris. Une clé absente n'est donc pas « le champ vaut
   * None », c'est un corps qu'aucune route n'a pu écrire — la même règle que partout ailleurs dans
   * ce lecteur, et le tour 2 l'appliquait à tout sauf ici (revue Codex 1.9, tour 3, I2).
   *
   * Ce que la tolérance laissait passer : une `page` absente affichait une clause sans son numéro
   * de page — indiscernable d'une clause du guide, qui n'en a légitimement pas —, une
   * `clarification` absente escamotait la seule question que le système pose en retour, et une
   * feuille de `faits_compris` absente retirait de « ce que j'ai compris » une ligne que le serveur
   * n'a jamais dite muette. Dans les trois cas la page **retranche** en silence, exactement le
   * défaut symétrique de la réserve fabriquée du tour 2.
   */
  function ouNul(predicat) {
    return function (v) { return v === null || predicat(v); };
  }

  // Les quatre natures de preuve d'absence d'AD-4, et la forme de ses deux compteurs affichés.
  var KINDS_ABSENCE = ["hors_perimetre", "zero_hit", "claims_rejetes", "clarification_requise"];

  function estCompteur(v) {
    return typeof v === "number" && isFinite(v) && Math.floor(v) === v && v >= 0;
  }

  function exiger(ok, champ) { if (!ok) throw illisible(champ); }

  function exigerListe(v, predicat, champ) {
    exiger(Array.isArray(v), champ);
    for (var i = 0; i < v.length; i++) exiger(predicat(v[i]), champ + "[" + i + "]");
  }

  // Le `ClaimStatus` d'AD-4, tel que `statutTexte()` le lit. `pertinente` et `applicable` sont
  // `… | None` dans le domaine ; `retrouvee` et `edition` ne le sont pas.
  function lireStatut(s, champ) {
    exiger(estObjet(s), champ);
    exiger(estBooleen(s.retrouvee), champ + ".retrouvee");
    exiger(ouNul(estBooleen)(s.pertinente), champ + ".pertinente");
    exiger(ouNul(estChaine)(s.applicable), champ + ".applicable");
    exiger(estChaine(s.edition), champ + ".edition");
  }

  // Une claim affichée. `quotes` porte l'appariement de D6 — c'est elle qui, énumérée dans l'ordre,
  // doit retomber sur `sources[]` : une quote sans `block_id` lisible fait perdre le rattachement de
  // **toutes** les clauses, et `Claim.quotes` a un `min_length=1` que le fil doit refléter.
  function lireClaim(c, champ) {
    exiger(estObjet(c), champ);
    exiger(estChaine(c.claim_id), champ + ".claim_id");
    exiger(estChaine(c.text), champ + ".text");
    exiger(Array.isArray(c.quotes) && c.quotes.length > 0, champ + ".quotes");
    for (var i = 0; i < c.quotes.length; i++) {
      exiger(estObjet(c.quotes[i]), champ + ".quotes[" + i + "]");
      exiger(estChaine(c.quotes[i].block_id), champ + ".quotes[" + i + "].block_id");
    }
    lireStatut(c.status, champ + ".status");
  }

  // Une affirmation écartée : la page en affiche le texte et le motif, et **jamais** sa citation
  // (D7). Ce sont donc les deux seuls champs qu'elle consomme, et les deux qu'elle exige.
  function lireRejetee(c, champ) {
    exiger(estObjet(c), champ);
    exiger(estChaine(c.text), champ + ".text");
    exiger(estChaine(c.rejection_kind), champ + ".rejection_kind");
  }

  // Les cinq champs d'AD-11 que la page lit, plus les deux de D5 et `line_ids` (story 3.4). Le
  // navigateur ne reçoit jamais de coordonnées à renvoyer : il transporte uniquement ces
  // identifiants strictement typés, que la route résout contre le corpus.
  function lireClause(s, champ) {
    exiger(estObjet(s), champ);
    exiger(estChaine(s.block_id), champ + ".block_id");
    exiger(estChaine(s.quote), champ + ".quote");
    exiger(estChaine(s.kind), champ + ".kind");
    exiger(estBooleen(s.kind_confirmed), champ + ".kind_confirmed");
    exiger(ouNul(estNombre)(s.page), champ + ".page");
    exigerListe(s.line_ids, estChaine, champ + ".line_ids");
    exiger(estChaine(s.status), champ + ".status");
  }

  // Une étape de la trace d'AD-10, telle que `traceVue()` la peint : « étape <name> · <tier> ·
  // <ms> ms · contrôles : <name>, … ». Chaque feuille lue est exigée, et typée comme `StepTrace` la
  // type (`domain/trace.py:36`) : `name: str`, `tier: str | None`, `ms: int`, `checks: list`. Sans
  // cela, `String(s.name || "")` peignait « étape  » pour une étape sans nom et « étape
  // [object Object] » pour un nom mal typé — une ligne de trace inventée sous « Comment cette
  // réponse a été obtenue », c'est-à-dire à l'endroit même qui répond de l'honnêteté du reste.
  function lireEtape(e, champ) {
    exiger(estObjet(e), champ);
    exiger(estChaine(e.name), champ + ".name");
    exiger(ouNul(estChaine)(e.tier), champ + ".tier");
    exiger(estCompteur(e.ms), champ + ".ms");
    if (e.opened_block_ids !== undefined) {
      exigerListe(e.opened_block_ids, estChaine, champ + ".opened_block_ids");
    }
    if (e.discarded_block_ids !== undefined) {
      exigerListe(e.discarded_block_ids, estChaine, champ + ".discarded_block_ids");
    }
    exiger(Array.isArray(e.checks), champ + ".checks");
    for (var i = 0; i < e.checks.length; i++) {
      exiger(estObjet(e.checks[i]), champ + ".checks[" + i + "]");
      exiger(estChaine(e.checks[i].name), champ + ".checks[" + i + "].name");
      exiger(estBooleen(e.checks[i].ok), champ + ".checks[" + i + "].ok");
      if (e.checks[i].detail !== undefined) {
        exiger(estChaine(e.checks[i].detail), champ + ".checks[" + i + "].detail");
      }
    }
  }

  function lireBlocTrace(b, champ) {
    exiger(estObjet(b), champ);
    exiger(estChaine(b.block_id), champ + ".block_id");
    exiger(estChaine(b.doc_id), champ + ".doc_id");
    exiger(estChaine(b.node_id), champ + ".node_id");
    exiger(ouNul(estChaine)(b.fiche_id), champ + ".fiche_id");
    exiger(estChaine(b.titre), champ + ".titre");
  }

  function lireGateTrace(g, champ) {
    exiger(g === null || estObjet(g), champ);
    if (g === null) return;
    exiger(ouNul(estChaine)(g.profile), champ + ".profile");
    exiger(g.cases === null || estCompteur(g.cases), champ + ".cases");
    exiger(g.countersigned === null || estBooleen(g.countersigned), champ + ".countersigned");
    exigerListe(g.alerts, estChaine, champ + ".alerts");
  }

  function lireReason(reason, champ) {
    // `Answer.reason` a une valeur par défaut `None` : absent et `null` signifient pareil ici.
    if (reason === undefined || reason === null) return;
    exiger(estObjet(reason), champ);
    exiger(KINDS_ABSENCE.indexOf(reason.kind) !== -1, champ + ".kind");
    exigerListe(reason.terms_searched, estChaine, champ + ".terms_searched");
    exiger(estCompteur(reason.variants_count), champ + ".variants_count");
    exiger(estCompteur(reason.blocks_scanned), champ + ".blocks_scanned");
    exigerListe(reason.documents, estChaine, champ + ".documents");
  }

  // Lecture **stricte** du contrat d'AD-11, sur ce que l'écran consomme — et **récursive** depuis la
  // revue Codex 1.9 (tour 2, I2). Un 200 incomplet n'est pas une réponse dégradée, c'est un serveur
  // cassé : la page n'en peint aucun morceau (`reponse_illisible`).
  //
  // Vérifier la présence des conteneurs ne suffisait pas, et le contre-exemple est `missing`. Un
  // `missing: {}` passait le contrôle d'objet, puis `paquetVue()` — qui liste la pièce dont le
  // booléen n'est pas `false` — annonçait **les quatre pièces manquantes**. Une réserve fabriquée,
  // exactement le symétrique de la réserve omise que le tour 1 avait corrigée : dans les deux cas la
  // page dit quelque chose que le serveur n'a pas dit. Même chose pour un `ask_client` d'objets
  // (« [object Object] » en question à poser au client), pour une claim sans `status` (une clause
  // affichée sans son applicabilité ni sa réserve d'édition) ou pour une `page` en chaîne.
  //
  // La règle est donc : **tout ce qu'UX-DR6 fait afficher est descendu jusqu'à la feuille**, et
  // chaque feuille est typée comme le domaine la type. Un défaut de schéma côté serveur
  // (`Field(default_factory=…)`) n'est pas un champ facultatif sur le fil — pydantic le sérialise
  // toujours. Les seuls champs vraiment facultatifs du contrat (`faits_compris`, `clarification`,
  // `reason`) valent `null` de plein droit : `ouNul()` le dit, et rien de plus.
  function lireReponse(j) {
    var o = j || {};
    exiger(estObjet(o.answer), "answer");
    exiger(estBooleen(o.answer.found), "answer.found");
    exiger(estBooleen(o.answer.complete), "answer.complete");
    exiger(estChaine(o.answer.texte), "answer.texte");
    // AD-16 : un refus sinistre **porte** un verdict, jamais rien. Un corps sans verdict n'a pas pu
    // être écrit par la route ; le peindre afficherait « verdict non reconnu » à la place d'une
    // erreur, c'est-à-dire un verdict de remplacement.
    exiger(estObjet(o.answer.verdict), "answer.verdict");
    exiger(estChaine(o.answer.verdict.value), "answer.verdict.value");
    // Les cinq champs du `Verdict` d'AD-6 : `value`, `reason`, `missing`, `ask_client`, `escalate`.
    // Aucun n'est facultatif côté serveur (`reason: str`, les trois autres ont un `default_factory`,
    // donc pydantic les sérialise toujours) : leur absence est un serveur cassé, pas un verdict
    // sobre. Tolérée, elle laissait peindre un verdict **privé de ses réserves** — sans le paquet
    // manquant, sans les questions à poser, sans la raison —, c'est-à-dire un verdict plus assuré
    // que celui que le serveur a rendu (revue Codex 1.9, tour 1, I2). UX-DR6 les exige à l'écran ;
    // AD-16 dit que ce qui manque ne se comble pas en silence.
    exiger(estChaine(o.answer.verdict.reason), "answer.verdict.reason");
    // `MissingPackage` d'AD-6, jusqu'à ses quatre booléens : c'est `paquetVue()` qui les lit un par
    // un, et l'absence de l'un d'eux se peignait en « pièce non lue » (revue Codex 1.9, tour 2).
    exiger(estObjet(o.answer.verdict.missing), "answer.verdict.missing");
    exiger(estBooleen(o.answer.verdict.missing.conditions_particulieres),
           "answer.verdict.missing.conditions_particulieres");
    exiger(estBooleen(o.answer.verdict.missing.options_souscrites),
           "answer.verdict.missing.options_souscrites");
    exiger(estBooleen(o.answer.verdict.missing.avenants), "answer.verdict.missing.avenants");
    exiger(estBooleen(o.answer.verdict.missing.date_effet), "answer.verdict.missing.date_effet");
    exigerListe(o.answer.verdict.missing.faits, estChaine, "answer.verdict.missing.faits");
    exigerListe(o.answer.verdict.ask_client, estChaine, "answer.verdict.ask_client");
    exigerListe(o.answer.verdict.escalate, estChaine, "answer.verdict.escalate");
    exiger(Array.isArray(o.answer.claims), "answer.claims");
    for (var c = 0; c < o.answer.claims.length; c++) {
      lireClaim(o.answer.claims[c], "answer.claims[" + c + "]");
    }
    // `rejected_claims` porte les affirmations écartées par la vérification : l'écran le plus
    // démuni — un `ne_tranche_pas` sans clause — n'a souvent que cette section à montrer.
    exiger(Array.isArray(o.answer.rejected_claims), "answer.rejected_claims");
    for (var r = 0; r < o.answer.rejected_claims.length; r++) {
      lireRejetee(o.answer.rejected_claims[r], "answer.rejected_claims[" + r + "]");
    }
    exigerListe(o.answer.unknown, estChaine, "answer.unknown");
    exiger(ouNul(estChaine)(o.answer.clarification), "answer.clarification");
    lireReason(o.answer.reason, "answer.reason");
    // `faits_compris` est le seul objet vraiment facultatif de la réponse (`QuestionScope | None` :
    // le guide n'en a pas, et AD-5 n'en publie pas sur une clarification). Présent, il est descendu
    // comme le reste — c'est l'endroit où l'utilisateur vérifie qu'il a été compris, et un
    // `evenement: {}` y aurait affiché « [object Object] » en événement du sinistre.
    exiger(ouNul(estObjet)(o.answer.faits_compris), "answer.faits_compris");
    if (o.answer.faits_compris) {
      exigerListe(o.answer.faits_compris.themes, estChaine, "answer.faits_compris.themes");
      exiger(ouNul(estChaine)(o.answer.faits_compris.bien), "answer.faits_compris.bien");
      exiger(ouNul(estChaine)(o.answer.faits_compris.evenement), "answer.faits_compris.evenement");
      exiger(ouNul(estChaine)(o.answer.faits_compris.lieu), "answer.faits_compris.lieu");
      exiger(ouNul(estChaine)(o.answer.faits_compris.cause), "answer.faits_compris.cause");
      exiger(ouNul(estChaine)(o.answer.faits_compris.moment), "answer.faits_compris.moment");
    }
    // `sources` **doit** être là : c'est la liste des clauses relues du corpus (AD-11), et un
    // verdict peint sans elle serait un verdict sans ses clauses — la promesse même de l'outil.
    // Vide, elle est légitime (un refus n'en a aucune) ; absente, elle n'a pas été écrite.
    exiger(Array.isArray(o.sources), "sources");
    for (var s = 0; s < o.sources.length; s++) lireClause(o.sources[s], "sources[" + s + "]");
    // La trace est **affichée** (`traceVue()`, un `<details>` que l'utilisateur déplie) : elle
    // tombe sous la même règle que le verdict, et le tour 2 s'était arrêté à `request_id` (revue
    // Codex 1.9, tour 3, I2). `Trace` (`domain/trace.py:47`) rend `pipeline: str`, `variant: str`,
    // `steps: list` et `total_cost_eur: float` sur toute réponse — aucun n'est facultatif. Tolérés
    // absents, ils se peignaient en « pipeline :  », en trace sans une seule étape, et surtout en
    // **coût tu** : NFR4 veut le coût réel à l'écran, et `coutTexte()` ne dit rien quand il ne le
    // trouve pas. Une analyse annoncée gratuite parce que le serveur a omis son prix est un
    // mensonge plus grave que l'absence de trace.
    exiger(estObjet(o.trace), "trace");
    exiger(estChaine(o.trace.request_id), "trace.request_id");
    exiger(estChaine(o.trace.pipeline), "trace.pipeline");
    exiger(estChaine(o.trace.variant), "trace.variant");
    // `estNombre` refuse `NaN` et `Infinity` — « coûté Infinity € » est un prix inventé. Le signe
    // n'est pas typé ici (le domaine ne le contraint pas) : `coutTexte()` garde sa garde sur les
    // valeurs négatives, et préfère se taire plutôt que d'afficher un coût impossible.
    exiger(estNombre(o.trace.total_cost_eur), "trace.total_cost_eur");
    exiger(Array.isArray(o.trace.steps), "trace.steps");
    for (var t = 0; t < o.trace.steps.length; t++) {
      lireEtape(o.trace.steps[t], "trace.steps[" + t + "]");
    }
    if (o.trace.blocs !== undefined) {
      exiger(Array.isArray(o.trace.blocs), "trace.blocs");
      for (var b = 0; b < o.trace.blocs.length; b++) {
        lireBlocTrace(o.trace.blocs[b], "trace.blocs[" + b + "]");
      }
    }
    if (o.trace.retries !== undefined) exiger(estCompteur(o.trace.retries), "trace.retries");
    if (o.trace.truncations !== undefined) {
      exiger(estCompteur(o.trace.truncations), "trace.truncations");
    }
    if (o.trace.thresholds !== undefined) {
      exiger(estObjet(o.trace.thresholds), "trace.thresholds");
      Object.keys(o.trace.thresholds).forEach(function (nom) {
        exiger(estNombre(o.trace.thresholds[nom]), "trace.thresholds." + nom);
      });
    }
    if (o.trace.gate !== undefined) lireGateTrace(o.trace.gate, "trace.gate");
    return {
      answer: o.answer,
      sources: o.sources,
      via: typeof o.via === "string" ? o.via : "api/v1",
      trace: o.trace
    };
  }

  function illisible(champ) {
    var e = erreurSinistre({ kind: "requete", code: "reponse_illisible", statut: 200 });
    e.champ = champ;
    return e;
  }

  function requete(chemin, options) {
    if (!enLigne()) {
      return Promise.reject(erreurSinistre({ kind: "requete", code: "hors_ligne", statut: 0 }));
    }
    var opts = options || { method: "GET" };
    var ctrl = (typeof AbortController === "function") ? new AbortController() : null;
    if (ctrl) opts.signal = ctrl.signal;
    var minuteur = ctrl ? setTimeout(function () { ctrl.abort(); }, abandonMs()) : null;
    function finir() { if (minuteur !== null) clearTimeout(minuteur); }
    return fetch(API_BASE + chemin, opts).then(function (r) {
      finir();
      if (!r.ok) {
        return r.json().then(function (j) { return j; }, function () { return null; })
          .then(function (j) { throw erreurHttp(r.status, r.headers, j); });
      }
      return r.json().then(function (j) { return j; }, function () {
        throw erreurSinistre({ kind: "requete", code: "reponse_illisible", statut: r.status });
      });
    }, function () {
      finir();
      throw erreurSinistre({
        kind: "indisponible",
        code: (ctrl && ctrl.signal.aborted) ? "timeout_client" : "reseau",
        statut: 0
      });
    });
  }

  // `GET /api/v1/sante` n'est jamais limitée et ne coûte rien : tout ce qu'elle publie a été calculé
  // au démarrage du serveur. Le sinistre n'a pas de badge de mode à tenir — il n'en lit que les
  // **seuils actifs**, pour que sa borne d'abandon soit celle du serveur et non une copie figée.
  // Un échec n'empêche rien : les replis prennent le relais, et la page le dira si la requête suit.
  function sonder() {
    return requete("/api/v1/sante").then(function (j) {
      if (j && j.thresholds && typeof j.thresholds === "object") seuilsServeur = j.thresholds;
      return j;
    }, function () { return null; });
  }

  function documents() {
    return requete("/api/v1/documents").then(function (j) {
      // Un 200 qui n'est pas un tableau n'a pas pu être écrit par cette route : le réduire à `[]`
      // faisait dire à la page « aucun contrat n'est servi », c'est-à-dire une affirmation sur le
      // **service** alors qu'elle n'a pas su lire la réponse (revue 1.9, tour 2). C'est la
      // distinction que `vueFormulaire` dit précisément ne pas devoir brouiller.
      if (!Array.isArray(j)) throw illisible("documents");
      return j;
    });
  }

  function soumettre(saisie) {
    return requete("/api/v1/sinistre", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(corpsSinistre(saisie))
    }).then(lireReponse);
  }

  // ---------- lecteur PDF paresseux (story 3.4) ----------

  var lecteurEtat = null;
  var lecteurGeneration = 0;
  var lecteurUrlObjet = null;
  var lecteurAbort = null;
  var lecteurMinuteur = null;

  function urlPage(doc_id, page, block_ids, line_ids) {
    var chemin = "/api/v1/documents/" + encodeURIComponent(String(doc_id || "")) +
      "/pages/" + encodeURIComponent(String(page)) + ".png";
    var blocs = tableau(block_ids);
    var query = [];
    if (blocs.length) {
      query.push("blocks=" + blocs.map(function (block_id) {
        return encodeURIComponent(String(block_id));
      }).join(","));
    }
    if (Array.isArray(line_ids)) {
      if (line_ids.length) {
        line_ids.forEach(function (line_id) {
          query.push("line_ids=" + encodeURIComponent(String(line_id)));
        });
      } else if (blocs.length) {
        // Différencie la précision explicitement vide d'une absence de précision, laquelle demande
        // au serveur toutes les lignes des blocs canoniques.
        query.push("line_ids=");
      }
    }
    return chemin + (query.length ? "?" + query.join("&") : "");
  }

  function annulerChargementLecteur() {
    if (lecteurAbort) lecteurAbort.abort();
    if (lecteurMinuteur !== null) clearTimeout(lecteurMinuteur);
    lecteurAbort = null;
    lecteurMinuteur = null;
  }

  function reglerSourceLecteur(source_url) {
    var lien = $("lecteur-source");
    if (!lien) return;
    var source = lienHttp(source_url);
    lien.hidden = !source;
    if (source) {
      lien.href = source;
      lien.target = "_blank";
      lien.rel = "noopener noreferrer";
    } else {
      lien.removeAttribute("href");
    }
  }

  function reglerNavigationLecteur() {
    var precedent = $("lecteur-precedent");
    var suivant = $("lecteur-suivant");
    if (precedent) precedent.disabled = !lecteurEtat || lecteurEtat.page <= 1;
    if (suivant) suivant.disabled = !lecteurEtat || lecteurEtat.total_pages === null ||
      lecteurEtat.page >= lecteurEtat.total_pages;
  }

  function selectionPageLecteur() {
    if (!lecteurEtat || lecteurEtat.page !== lecteurEtat.page_citee) {
      return { block_ids: [], line_ids: null };
    }
    return { block_ids: lecteurEtat.block_ids, line_ids: lecteurEtat.line_ids };
  }

  function chargerPageLecteur() {
    if (!lecteurEtat) return Promise.resolve(null);
    annulerChargementLecteur();
    var generation = ++lecteurGeneration;
    var pageDemandee = lecteurEtat.page;
    var docDemande = lecteurEtat.doc_id;
    var status = $("lecteur-statut");
    var image = $("lecteur-image");
    var sans = $("lecteur-sans-surlignage");
    var selection = selectionPageLecteur();
    var lignes = selection.line_ids;
    if (status) status.textContent = "Chargement de la page " + pageDemandee + "…";
    if (image) {
      image.hidden = true;
      image.alt = "Page " + pageDemandee + " du contrat";
    }
    if (sans) {
      sans.hidden = Array.isArray(lignes) && lignes.length !== 0;
      sans.textContent = !Array.isArray(lignes) || lignes.length === 0
        ? "Cette page est affichée sans surlignage : aucune ligne de cette page ne fait partie de la citation."
        : "";
    }
    reglerNavigationLecteur();

    var ctrl = (typeof AbortController === "function") ? new AbortController() : null;
    var minuteur = ctrl ? setTimeout(function () { ctrl.abort(); }, abandonMs()) : null;
    lecteurAbort = ctrl;
    lecteurMinuteur = minuteur;
    function finir() {
      if (minuteur !== null) clearTimeout(minuteur);
      if (lecteurAbort === ctrl) lecteurAbort = null;
      if (lecteurMinuteur === minuteur) lecteurMinuteur = null;
    }
    var chemin = urlPage(docDemande, pageDemandee, selection.block_ids, lignes);
    return fetch(API_BASE + chemin, ctrl ? { signal: ctrl.signal } : {}).then(function (r) {
      if (!r.ok || typeof r.blob !== "function") throw new Error("page_indisponible");
      var total = Number(r.headers && r.headers.get("X-Document-Pages"));
      if (!isFinite(total) || Math.floor(total) !== total || total < pageDemandee) {
        throw new Error("metadonnees_page_invalides");
      }
      return r.blob().then(function (blob) { return { blob: blob, total: total }; });
    }).then(function (chargee) {
      finir();
      if (!lecteurEtat || generation !== lecteurGeneration) return null;
      if (typeof URL.createObjectURL !== "function") throw new Error("image_locale_indisponible");
      if (lecteurUrlObjet && typeof URL.revokeObjectURL === "function") {
        URL.revokeObjectURL(lecteurUrlObjet);
      }
      lecteurUrlObjet = URL.createObjectURL(chargee.blob);
      lecteurEtat.total_pages = chargee.total;
      if (image) {
        image.src = lecteurUrlObjet;
        image.hidden = false;
        image.alt = "Page " + lecteurEtat.page + " sur " + chargee.total + " du contrat";
      }
      if (status) status.textContent = "Page " + lecteurEtat.page + " sur " + chargee.total + " chargée.";
      reglerNavigationLecteur();
      return chargee;
    }).catch(function () {
      finir();
      if (!lecteurEtat || generation !== lecteurGeneration) return null;
      if (status) status.textContent = "La page du PDF est indisponible. Le verdict reste affiché ci-dessous.";
      if (image) image.hidden = true;
      reglerNavigationLecteur();
      return null;
    });
  }

  function ouvrirLecteur(commande, declencheur) {
    var c = commande || {};
    var page = Number(c.page);
    if (!c.doc_id || !isFinite(page) || Math.floor(page) !== page || page < 1 ||
        !Array.isArray(c.block_ids) || !Array.isArray(c.line_ids)) return Promise.resolve(null);
    lecteurEtat = {
      doc_id: String(c.doc_id), page: page, page_citee: page,
      block_ids: c.block_ids.map(String),
      line_ids: c.line_ids.map(String), source_url: lienHttp(c.source_url),
      total_pages: null, declencheur: declencheur || null
    };
    var dialogue = $("lecteur-pdf");
    reglerSourceLecteur(lecteurEtat.source_url);
    if (dialogue) {
      dialogue.hidden = false;
      if (typeof dialogue.showModal === "function") dialogue.showModal();
      else dialogue.setAttribute("open", "");
    }
    var fermer = $("lecteur-fermer");
    if (fermer) fermer.focus();
    return chargerPageLecteur();
  }

  function fermerLecteur() {
    lecteurGeneration++;
    annulerChargementLecteur();
    var ancien = lecteurEtat;
    lecteurEtat = null;
    if (lecteurUrlObjet && typeof URL.revokeObjectURL === "function") {
      URL.revokeObjectURL(lecteurUrlObjet);
    }
    lecteurUrlObjet = null;
    var image = $("lecteur-image");
    if (image) { image.removeAttribute("src"); image.hidden = true; }
    var dialogue = $("lecteur-pdf");
    if (dialogue) {
      if (typeof dialogue.close === "function" && dialogue.open) dialogue.close();
      else dialogue.removeAttribute("open");
      dialogue.hidden = true;
    }
    if (ancien && ancien.declencheur && typeof ancien.declencheur.focus === "function") {
      ancien.declencheur.focus();
    }
  }

  function naviguerLecteur(delta) {
    if (!lecteurEtat) return Promise.resolve(null);
    var prochaine = lecteurEtat.page + delta;
    if (prochaine < 1 || (lecteurEtat.total_pages !== null && prochaine > lecteurEtat.total_pages)) {
      return Promise.resolve(null);
    }
    lecteurEtat.page = prochaine;
    return chargerPageLecteur();
  }

  function brancherLecteur(racine) {
    if (!racine) return;
    racine.querySelectorAll(".cl-ouvrir").forEach(function (bouton) {
      bouton.addEventListener("click", function () {
        var block_ids;
        var line_ids;
        try { block_ids = JSON.parse(bouton.getAttribute("data-block-ids") || "[]"); }
        catch (_) { block_ids = []; }
        try { line_ids = JSON.parse(bouton.getAttribute("data-line-ids") || "[]"); }
        catch (_) { line_ids = []; }
        ouvrirLecteur({
          doc_id: bouton.getAttribute("data-doc-id"),
          page: Number(bouton.getAttribute("data-page")),
          block_ids: Array.isArray(block_ids) ? block_ids : [],
          line_ids: Array.isArray(line_ids) ? line_ids : [],
          source_url: bouton.getAttribute("data-source-url")
        }, bouton);
      });
    });
  }

  function preparerLecteur() {
    var fermer = $("lecteur-fermer");
    var precedent = $("lecteur-precedent");
    var suivant = $("lecteur-suivant");
    var dialogue = $("lecteur-pdf");
    var image = $("lecteur-image");
    if (fermer) fermer.addEventListener("click", fermerLecteur);
    if (precedent) precedent.addEventListener("click", function () { naviguerLecteur(-1); });
    if (suivant) suivant.addEventListener("click", function () { naviguerLecteur(1); });
    if (image) image.addEventListener("error", function () {
      if (!lecteurEtat || image.src !== lecteurUrlObjet) return;
      image.hidden = true;
      var status = $("lecteur-statut");
      if (status) status.textContent = "L'image de la page est illisible. Le verdict reste affiché ci-dessous.";
    });
    if (dialogue) dialogue.addEventListener("cancel", function (ev) {
      ev.preventDefault();
      fermerLecteur();
    });
  }

  // ---------- démarrage : le seul endroit qui touche la page ----------

  function $(id) { return document.getElementById(id); }

  function saisieCourante() {
    return {
      doc_id: ($("contrat") || {}).value || "",
      question: ($("question") || {}).value || "",
      date: ($("date") || {}).value || "",
      lieu: ($("lieu") || {}).value || "",
      montant_eur: ($("montant") || {}).value || "",
      description: ($("description") || {}).value || ""
    };
  }

  // Le lien vers la source publique du contrat **sélectionné**, décrit comme tout le reste : c'est
  // le matérialiseur qui pose `href`, `target` et `rel`, pas cette fonction.
  function vueSource(vue, doc_id) {
    var trouve = tableau(vue && vue.sources).filter(function (s) {
      return s && s.doc_id === doc_id;
    })[0];
    if (!trouve || !trouve.url) return null;
    var a = noeud("a", "source-lien", "voir le contrat à sa source publique");
    a.href = trouve.url;
    return a;
  }

  /** Le lien de source seul — rejoué à chaque `change`, sans toucher au `<select>`. */
  function rafraichirSource(vue) {
    var source = $("contrat-source");
    if (!source) return;
    vider(source);
    var lien = vueSource(vue, ($("contrat") || {}).value || "");
    if (lien) source.appendChild(materialiser(lien));
  }

  /**
   * Pose le `<select>`, le message et le lien de source. Les `<option>` sont construites **une
   * seule fois** (revue 1.9) : rebâtir la liste à chaque `change` remettait `select.value` sur la
   * première option, si bien qu'avec deux contrats servis — ce qu'AD-14 prévoit — le choix de
   * l'utilisateur était annulé en silence et le sinistre partait contre le mauvais contrat.
   */
  function appliquerFormulaire(vue) {
    var select = $("contrat");
    var bouton = $("analyser");
    var message = $("contrats-message");
    if (select) {
      vider(select);
      vue.options.forEach(function (o) {
        var option = document.createElement("option");
        option.value = o.valeur;
        option.textContent = o.texte;  // AD-15 : titre et édition viennent du serveur
        select.appendChild(option);
      });
      select.disabled = !vue.actif;
      // `select.value` vaut la première option en HTML ; le DOM minimal des tests ne le dérive pas,
      // et le poser explicitement rend le comportement identique des deux côtés.
      if (vue.options.length && !select.value) select.value = vue.options[0].valeur;
    }
    if (bouton) bouton.disabled = !vue.actif;
    if (message) {
      message.textContent = vue.message || "";
      message.hidden = !vue.message;
    }
    var audits = $("documents-audit");
    if (audits && vue.audits) peindre(vue.audits, audits);
    rafraichirSource(vue);
  }

  function verrouiller(occupe) {
    ["contrat", "question", "date", "lieu", "montant", "description", "analyser"]
      .forEach(function (id) { var e = $(id); if (e) e.disabled = !!occupe; });
    var hote = $("resultat");
    if (hote) hote.setAttribute("aria-busy", occupe ? "true" : "false");
  }

  /** Ce qui manque à la saisie pour partir, ou `null`. Le bouton n'est jamais muet (revue 1.9). */
  function manquant(saisie) {
    if (!String(saisie.doc_id || "").trim()) {
      return "Choisissez le contrat auquel confronter ce sinistre.";
    }
    if (!String(saisie.question || "").trim()) {
      return "La question posée au contrat ne peut pas être vide.";
    }
    if (!String(saisie.description || "").trim()) {
      return "Décrivez les faits : sans description, il n'y a rien à confronter aux clauses.";
    }
    // `novalidate` a rendu la page responsable de tout ce que le navigateur validait pour elle
    // (revue 1.9), et le montant en faisait partie : `min="0"` bloquait `-100` tant que la
    // validation native jouait, plus personne ne le bloque ensuite (`type="number"` ramène bien
    // `douze` à la chaîne vide, mais il tient `-100` pour une valeur). Le supprimer du corps aurait
    // été pire que le refuser —
    // le sinistre partait, analysé sur des faits amputés de ce qu'on avait écrit (revue Codex
    // 1.9, tour 1, I1). Le montant reste **facultatif** : c'est la saisie illisible qui est dite.
    if (montantSaisi(saisie.montant_eur) === false) {
      return "Le montant doit être un nombre en euros, zéro ou davantage (par exemple 1200,50) — "
        + "ou laissez le champ vide.";
    }
    return null;
  }

  function demarrer() {
    // Les `maxlength` de la page sont posés **ici**, depuis les constantes du script : la page en
    // porte aussi la valeur en dur, mais comme repli sans JavaScript. Une seule source à
    // l'exécution (revue 1.9) ; un test Python épingle les deux contre les schémas du serveur.
    // `date` n'y est pas : `maxlength` ne s'applique pas à `<input type="date">` et le navigateur
    // l'ignore. `DATE_MAX` reste publié par `bornes()` — c'est la borne du domaine, appliquée par
    // le serveur, et le test l'y compare — mais la page ne fait pas mine de la poser.
    var bornes = [{ id: "question", max: QUESTION_MAX }, { id: "description", max: DESCRIPTION_MAX },
                  { id: "lieu", max: LIEU_MAX }];
    bornes.forEach(function (b) { var e = $(b.id); if (e) e.maxLength = b.max; });
    preparerLecteur();

    var vueForm = vueFormulaire([]);
    // La sonde d'abord : elle porte `deadline_s` et `client_abort_margin_s`, donc la borne
    // d'abandon de toutes les requêtes qui suivent. Elle ne coûte rien et n'est pas limitée.
    sonder()
      .then(documents)
      .then(function (docs) {
        vueForm = vueFormulaire(docs);
        appliquerFormulaire(vueForm);
      })
      .catch(function (e) {
        // `.catch` et non le second argument de `.then` : une exception levée **dans** le
        // gestionnaire de succès (une réponse à la forme inattendue) laisserait sinon la page
        // muette et le formulaire désactivé sans un mot (revue 1.9).
        vueForm = vueFormulaire([], true);
        appliquerFormulaire(vueForm);
        peindre(vueErreur(e));
      });

    var select = $("contrat");
    if (select) {
      // Seul le lien de source est rejoué : reconstruire les options annulerait la sélection.
      select.addEventListener("change", function () { rafraichirSource(vueForm); });
    }

    var form = $("formulaire");
    if (form) {
      form.addEventListener("submit", function (ev) {
        ev.preventDefault();
        var saisie = saisieCourante();
        var defaut = manquant(saisie);
        if (defaut) {
          // Un bouton qui ne fait rien et ne dit rien est un bouton cassé. La carte porte le même
          // titre que les autres échecs — aucun verdict n'a été rendu — et aucune action.
          peindre(vueErreur({ kind: "saisie", code: "saisie_incomplete", detail: defaut }));
          return;
        }
        // Le verdict précédent quitte l'écran **avant** l'appel : sur une erreur, l'AC exige qu'il
        // n'y reste pas, et le plus simple est qu'il ne survive à aucune soumission.
        peindre(vueAttente());
        verrouiller(true);
        soumettre(saisie)
          .then(function (r) {
            verrouiller(false);
            var source = tableau(vueForm.sources).filter(function (s) {
              return s && s.doc_id === saisie.doc_id;
            })[0];
            var resultat = peindre(vueVerdict(r, {
              doc_id: saisie.doc_id,
              source_url: source && source.url
            }));
            brancherLecteur(resultat);
          })
          .catch(function (e) {
            verrouiller(false);
            peindre(vueErreur(e));
          });
      });
    }
  }

  window.SINISTRE = {
    // Composition pure : testable sans navigateur (`tests/js/sinistre_cases.mjs`).
    corpsSinistre: corpsSinistre,
    montantSaisi: montantSaisi,
    clausesParClaim: clausesParClaim,
    statutTexte: statutTexte,
    libelleKind: libelleKind,
    libelleVerdict: libelleVerdict,
    motifRejet: motifRejet,
    preuveAbsence: preuveAbsence,
    etatReponse: etatReponse,
    phraseEtat: phraseEtat,
    traceVue: traceVue,
    ALERTES: ALERTES,
    CONTROLES: CONTROLES,
    coutTexte: coutTexte,
    messageErreur: messageErreur,
    vueAttente: vueAttente,
    vueAudits: vueAudits,
    vueFormulaire: vueFormulaire,
    vueVerdict: vueVerdict,
    vueErreur: vueErreur,
    // Réseau et peinture.
    documents: documents,
    soumettre: soumettre,
    materialiser: materialiser,
    peindre: peindre,
    demarrer: demarrer,
    apiBase: function () { return API_BASE; },
    setApiBase: function (u) { API_BASE = u; },
    bornes: function () {
      return {
        question_max: QUESTION_MAX, description_max: DESCRIPTION_MAX,
        date_max: DATE_MAX, lieu_max: LIEU_MAX,
        abandon_ms: abandonMs(), seuils_du_serveur: seuilsServeur,
        deadline_s_repli: DEADLINE_S_REPLI, marge_abandon_s_repli: MARGE_ABANDON_S_REPLI
      };
    },
    sonder: sonder,
    manquant: manquant,
    statutDeBloc: statutDeBloc,
    statutAmbigu: statutAmbigu,
    vueSource: vueSource,
    urlPage: urlPage,
    ouvrirLecteur: ouvrirLecteur,
    fermerLecteur: fermerLecteur,
    naviguerLecteur: naviguerLecteur,
    brancherLecteur: brancherLecteur,
    libelleControle: libelleControle,
    PORTEE: PORTEE
  };

  // Le harnais de test charge ce fichier sans page : il pose ce drapeau pour obtenir `window.SINISTRE`
  // sans démarrage. Même mécanique que `__UI_SANS_DEMARRAGE` de `web/app/ui.js`.
  if (!window.__SINISTRE_SANS_DEMARRAGE) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", demarrer);
    } else {
      demarrer();
    }
  }
})();
