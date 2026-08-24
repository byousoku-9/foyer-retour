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

  function noeud(tag, cls, texte, enfants) {
    var n = { tag: tag };
    if (cls) n.cls = cls;
    if (texte !== undefined && texte !== null) n.texte = String(texte);
    if (enfants && enfants.length) n.enfants = enfants;
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

  function statutTexte(status) {
    if (!status) return "";
    var p = [];
    if (status.retrouvee === true) p.push("retrouvée");
    if (status.pertinente === true) p.push("pertinente");
    if (status.applicable && Object.prototype.hasOwnProperty.call(APPLICABLE, status.applicable)) {
      p.push(APPLICABLE[status.applicable]);
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

  // L'état du sélecteur de contrat. Seuls les `kind="contrat"` y entrent : le guide **est** servi et
  // `GET /api/v1/documents` le liste (il ne ment pas sur ce qui est servi), mais lui soumettre un
  // sinistre n'a pas de sens — aucun de ses blocs n'est une garantie ou une exclusion —, et le
  // serveur le refuse aussi (D3). Aucun contrat ⇒ le formulaire est **désactivé** et le dit : c'est
  // le seul écran où « rien à analyser » doit se lire avant qu'on ait écrit une description.
  function vueFormulaire(documents, echec) {
    var contrats = tableau(documents).filter(function (d) { return d && d.kind === "contrat"; });
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

  function clauseVue(src, status) {
    var meta = [noeud("span", "cl-kind", libelleKind(src.kind))];
    if (src.kind_confirmed === false) {
      // AD-6 : un `kind` non confirmé plafonne le verdict. Afficher « garantie » sans le dire
      // donnerait au lecteur une certitude que le pipeline n'a pas.
      meta.push(noeud("span", "cl-doute", "typage non confirmé"));
    }
    if (typeof src.page === "number") meta.push(noeud("span", "cl-page", "page " + src.page));
    var statut = statutTexte(status);
    if (statut) meta.push(noeud("span", "cl-statut", statut));
    return noeud("div", "clause", null, [
      noeud("blockquote", "cl-q", "« " + String(src.quote || "") + " »"),
      noeud("div", "cl-meta", null, meta)
    ]);
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

  function traceVue(trace) {
    if (!trace) return null;
    var t = trace;
    var lignes = [
      "référence de requête : " + String(t.request_id || ""),
      "pipeline : " + String(t.pipeline || "") + (t.variant ? " · variante " + t.variant : "")
    ];
    tableau(t.steps).forEach(function (s) {
      s = s || {};
      var checks = tableau(s.checks).map(function (c) { return String((c || {}).name || ""); })
        .filter(function (n) { return n; });
      lignes.push("étape " + String(s.name || "") +
        (s.tier ? " · " + s.tier : " · aucun appel") +
        (typeof s.ms === "number" ? " · " + s.ms + " ms" : "") +
        (checks.length ? " · contrôles : " + checks.join(", ") : ""));
    });
    var cout = coutTexte(t);
    if (cout) lignes.push(cout);
    // `<details>` natif : la trace est dépliable sans une ligne de JavaScript, donc sans état.
    return noeud("details", "trace", null, [
      noeud("summary", null, "Comment cette réponse a été obtenue"),
      liste("trace-lignes", lignes)
    ]);
  }

  function vueVerdict(reponse) {
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
              entree.clauses.map(function (src) { return clauseVue(src, entree.status); }))));
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
          corps.push(clauseVue(src, statutDeBloc(a, src.block_id)));
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

    var inconnus = tableau(a.unknown).filter(function (x) { return String(x || "").trim(); });
    if (inconnus.length) {
      enfants.push(section("inconnu", "Ce que je ne sais pas", [liste("inconnu-liste", inconnus)]));
    }

    if (a.clarification) {
      enfants.push(section("clarif", "Une précision, pour chercher au bon endroit", [
        noeud("p", "clarif-q", String(a.clarification))
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
    if (vue.href) { e.href = vue.href; e.target = "_blank"; e.rel = "noopener noreferrer"; }
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

  // Les cinq champs d'AD-11 que la page lit, plus les deux de D5. `page` est `int | None` (le guide
  // n'en a pas) ; `bbox` et `line_ids` ne sont pas listés : rien ne les affiche ici.
  function lireClause(s, champ) {
    exiger(estObjet(s), champ);
    exiger(estChaine(s.block_id), champ + ".block_id");
    exiger(estChaine(s.quote), champ + ".quote");
    exiger(estChaine(s.kind), champ + ".kind");
    exiger(estBooleen(s.kind_confirmed), champ + ".kind_confirmed");
    exiger(ouNul(estNombre)(s.page), champ + ".page");
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
    exiger(estNombre(e.ms), champ + ".ms");
    exiger(Array.isArray(e.checks), champ + ".checks");
    for (var i = 0; i < e.checks.length; i++) {
      exiger(estObjet(e.checks[i]), champ + ".checks[" + i + "]");
      exiger(estChaine(e.checks[i].name), champ + ".checks[" + i + "].name");
    }
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
            peindre(vueVerdict(r));
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
    coutTexte: coutTexte,
    messageErreur: messageErreur,
    vueAttente: vueAttente,
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
