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
  var QUESTION_SINISTRE = "Ce sinistre est-il couvert par les conditions générales du contrat ?";

  // Borne d'abandon côté client : la deadline du serveur plus la marge qu'il annonce. Les deux sont
  // des **seuils de `config.py`** (`deadline_s`, `client_abort_margin_s`), publiés par
  // `thresholds()` et lus sur `/api/v1/sante` au démarrage — la page ne les recopie pas (convention
  // Seuils du spine, et patron de `web/app/chat.js` depuis 1.7). Les deux littéraux ci-dessous ne
  // sont qu'un **repli** pour la première requête si la sonde n'a pas répondu : une borne figée
  // ferait couper par le navigateur une requête à laquelle le serveur aurait répondu, le jour où
  // `deadline_s` monte (revue 1.9).
  // Story 5.6 (T3, 03/09/2026) : 165 et 150, les valeurs de `config.py` re-dérivées pour la
  // navigation par le modèle. Les deux replis avaient dérivé (55 et 10 pour une deadline à 100) —
  // sans conséquence tant qu'ils ne servent qu'avant la sonde, mais un repli qui ment sur la
  // patience du serveur est exactement ce que cette page dit ne pas faire.
  var DEADLINE_S_REPLI = 165;
  var MARGE_ABANDON_S_REPLI = 150;
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

  function hotePrive(hostname) {
    var hote = String(hostname || "").toLowerCase().replace(/^\[|\]$/g, "");
    if (!hote || hote === "localhost" || /\.(?:localhost|local|internal|home|lan)$/.test(hote) ||
        hote === "::" || hote === "::1") {
      return true;
    }
    if (/^(?:fc|fd|fe8|fe9|fea|feb)[0-9a-f:]*$/.test(hote)) return true;
    var ipv4 = hote.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);
    if (!ipv4) return false;
    var octets = ipv4.slice(1).map(Number);
    if (octets.some(function (n) { return n > 255; })) return true;
    return octets[0] === 0 || octets[0] === 10 || octets[0] === 127 ||
      (octets[0] === 100 && octets[1] >= 64 && octets[1] <= 127) ||
      (octets[0] === 169 && octets[1] === 254) ||
      (octets[0] === 172 && octets[1] >= 16 && octets[1] <= 31) ||
      (octets[0] === 192 && octets[1] === 168) || octets[0] >= 224;
  }

  function lienHttp(url) {
    var u = String(url || "");
    if (!u || u.length > 2048 || /\s/.test(u)) return null;
    try {
      var analyse = new URL(u);
      if ((analyse.protocol !== "http:" && analyse.protocol !== "https:") ||
          analyse.username || analyse.password || hotePrive(analyse.hostname)) return null;
      return u;
    } catch (_) {
      return null;
    }
  }

  function editionAvecReserve(edition) {
    var libelle = (edition === undefined || edition === null)
      ? "indisponible" : (edition === "" ? "valeur vide" : String(edition));
    return libelle + " — actualité non vérifiée";
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

  function sectionRepliee(cls, titre, enfants) {
    return noeud("details", cls + " details-preuves", null,
      [noeud("summary", null, titre)].concat(enfants));
  }

  // ---------- textes composés ----------

  // AD-6 : les quatre valeurs de `VerdictValue` (`domain/verdict.py:29`), et rien d'autre. Une
  // valeur inconnue n'est **pas** traduite en « ne tranche pas » : le serveur aurait rendu quelque
  // chose que ce contrat ne prévoit pas, et l'afficher comme un verdict connu serait le dégradé
  // silencieux qu'AD-16 interdit. Elle se dit.
  //
  // Story 5.6 (L2) : les libellés sont ceux qu'un assuré lit, pas les identifiants du domaine.
  // « non_couvert » se dit « Exclu » — le mot du contrat —, et « ne_tranche_pas » se dit de deux
  // façons parce qu'il recouvre deux situations que l'utilisateur ne confond jamais : aucune clause
  // retenue (« Pas de clause qui s'applique », la lecture a abouti et le contrat ne prévoit rien)
  // et des clauses retenues sur lesquelles la table AD-6 ne conclut pas (« Je ne peux pas
  // trancher »). Aucune valeur brute ne reste à l'écran.
  var VERDICTS = {
    couvert: "Couvert",
    non_couvert: "Exclu",
    sous_conditions: "Sous conditions",
    ne_tranche_pas: "Je ne peux pas trancher"
  };
  var NE_TRANCHE_PAS_SANS_CLAUSE = "Pas de clause qui s'applique";

  // Story 5.6 (L2c) — le filtre des chaînes techniques, en **défense en profondeur**.
  //
  // Ce que Lancelot a lu en prod : la réponse s'ouvrait sur « Verdict recalculé : sous conditions. »
  // suivie d'un identifiant de bloc (`axa-lu-optihome-2017:p37:11 : « … »`). Ce sont des chaînes de
  // service — elles disent à un développeur *par quel chemin* le verdict est tombé, jamais ce que le
  // contrat prévoit pour le sinistre. Le tour moteur les retire du contrat ; la page ne les affiche
  // plus **quoi qu'il arrive**, parce qu'un serveur qui régresse ne doit pas pouvoir remettre une
  // référence de bloc en tête de la réponse d'un assuré.
  //
  // Deux signatures, et deux seulement — on ne devine pas « ça fait technique » :
  //   - un `block_id` du corpus, de la forme `document:pNN:MM` (AD-2) ;
  //   - le préfixe « Verdict recalculé », que la table AD-6 compose au recalcul.
  // Ce qui est filtré n'est pas perdu : la raison du verdict se range dans « Comment cette réponse
  // a été obtenue », avec le reste de ce qui documente la décision.
  var RE_BLOCK_ID = /[^\s:«»"]+:p\d+:\d+/;
  var RE_VERDICT_RECALCULE = /^\s*verdict\s+recalcul/i;

  function estTechnique(texte) {
    var t = String(texte || "");
    return RE_BLOCK_ID.test(t) || RE_VERDICT_RECALCULE.test(t);
  }

  /** La chaîne, ou `""` si elle est technique. Un seul point de passage pour tout ce qui s'affiche. */
  function enClair(texte) {
    var t = String(texte || "").trim();
    return estTechnique(t) ? "" : t;
  }

  // Le bloc 1 sans une seule phrase du modèle. Ce n'est pas une phrase de repli qui *ressemble* à
  // une réponse : c'est le constat, à côté du verdict, que rien n'a pu être retenu.
  var AUCUNE_PHRASE_RETENUE = "Aucune clause n'a pu être retenue";

  // « Ce que je ne sais pas » ne se compose pas ici. Le moteur écrivait une ligne générique, vraie
  // de tous les « sous conditions » et qui n'apprenait rien — elle est retirée du contrat par le
  // tour moteur, et la page ne la réaffiche pas si elle revenait.
  var RE_INCONNU_GENERIQUE = /^\s*le verdict conserve des conditions/i;

  /**
   * Le libellé d'un verdict. `sansClause` distingue les deux « ne tranche pas ».
   *
   * Il vaut `true` quand **aucune affirmation n'a été retenue** — pas quand `sources[]` est vide :
   * une claim retenue dont l'appariement a échoué reste une claim retenue, et dire « pas de clause
   * qui s'applique » sous des clauses affichées serait un mensonge d'affichage.
   */
  function libelleVerdict(value, sansClause) {
    var v = String(value || "");
    if (v === "ne_tranche_pas" && sansClause === true) {
      return { cle: v, texte: NE_TRANCHE_PAS_SANS_CLAUSE };
    }
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
    p.push("édition " + editionAvecReserve(status.edition));
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

  /**
   * La classe de rôle d'une clause (story 5.6, L2b) : c'est elle qui donne au bord gauche de la
   * carte et à son étiquette la couleur de ce que la clause **fait** — une garantie n'a pas la même
   * portée qu'une exclusion, et l'écran doit le dire avant qu'on ait lu le paragraphe.
   *
   * Seuls les `kind` de la table d'AD-2 en reçoivent une : un kind hors table garde le fond
   * d'accent par défaut, et son libellé se dit tel quel. Inventer une couleur pour un rôle qu'on ne
   * connaît pas serait ranger la clause dans la case la plus proche.
   */
  function classeRole(kind) {
    var k = String(kind || "");
    return Object.prototype.hasOwnProperty.call(KINDS, k) ? " appui-role-" + k : "";
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

  // Story 4.2f : ce que la lecture du contrat a couvert, chiffré — **la même phrase** que
  // `web/app/chat.js`. Le pendant de `preuveAbsence()` pour le second porteur d'un `found=false`, et
  // la différence entre les deux est tout le sujet : une preuve d'absence annonce des passages
  // parcourus (le contrat entier), celle-ci annonce des passages lus.
  function lectureLue(lecture) {
    if (!lecture) return "";
    var noeuds = entier(lecture.nodes_read);
    var blocs = entier(lecture.blocks_read);
    return "Lecture partielle : " +
      pluriel(noeuds, "section") + (noeuds > 1 ? " lues" : " lue") + ", " +
      pluriel(blocs, "passage") + " transmis au modèle" +
      " — le reste n'a pas été lu, et rien n'en est affirmé";
  }

  // FR5 : les états, lus sur les deux booléens que *vérifier* calcule (AD-4) et sur le porteur qui
  // accompagne un `found=false` (story 4.2f). « inconnu » dit un refus fondé sur une recherche menée
  // à son terme ; « lecture partielle » dit que la lecture du contrat s'est arrêtée avant de
  // conclure. Sur un contrat, confondre les deux est exactement la faute que la story corrige.
  function etatReponse(answer) {
    var a = answer || {};
    if (!a.found) {
      return estObjet(a.lecture_partielle)
        ? { cle: "lecture-partielle", texte: "lecture partielle" }
        : { cle: "inconnu", texte: "inconnu" };
    }
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
    if (cle === "lecture-partielle") {
      // Story 4.2f — **la même phrase** que le guide. Elle n'affirme aucune absence et ne parle
      // d'aucune indisponibilité ; comme les trois autres, elle ne décrit que ce que la vue a
      // réellement peint : le chiffre n'est promis « ci-dessus » que s'il y est.
      var lu = c.lecture
        ? "ma lecture s'est arrêtée avant de conclure : ce qui a été lu est chiffré ci-dessus"
        : "ma lecture s'est arrêtée avant de conclure, sans que ce qui a été lu soit chiffré";
      return c.liste ? lu + ", et ce qui manque est listé sous « Ce que je ne sais pas »" : lu;
    }
    return c.preuve
      ? "rien n'a été retenu : la preuve de cette absence est ci-dessus"
      : "rien n'a été cherché : la question doit d'abord être précisée";
  }

  // Les `CheckResult.name` du serveur, en français — **la même table** que `web/app/chat.js`. Un nom
  // inconnu n'est jamais masqué : il s'affiche tel quel.
  var CONTROLES = {
    acquis_reconduits: "affirmations de la relance qui ne citent rien de neuf : non dupliquées",
    applicabilite_contradictoire: "deux jeux de champs d'applicabilité pour une même affirmation",
    applicabilite_hors_borne: "des libellés d'applicabilité dépassent leur borne",
    applicabilite_incomplete: "applicabilité non rendue pour une clause décisionnelle",
    citation_ajustee_au_mot: "des citations coupaient un mot en deux : étendues jusqu'à la fin du mot",
    citation_amorce_liee:
      "des citations reprennent la phrase qui ouvre une énumération : lues comme le contexte de la clause",
    citations: "citations relues dans le corpus",
    claims_non_citees: "affirmations vérifiées qu'aucune phrase affichée ne reprend",
    clarification_refus_neutralisee:
      "clarification conservée : la compréhension portait aussi une intention refusée",
    clarification_retablie_perimetre_tronque:
      "clarification servie : la liste tronquée ne permet pas de confirmer le refus hors périmètre",
    clarification_langue_non_affirmee: "clarification retirée : sa langue n'est pas affirmable",
    cout_eleve: "coût de la requête au-dessus du seuil",
    deadline_depassee: "délai dépassé avant une étape qui n'appelle aucun modèle : la réponse est servie",
    demande_cible_inconnue: "le contrôle a demandé un contexte que rien de ce qui lui a été soumis ne désigne",
    demande_contexte: "le contrôle a demandé le contexte qui lui manquait pour juger une affirmation",
    demande_hors_vocabulaire: "une demande de contexte hors du vocabulaire fermé : aucune demande formée",
    demande_insatisfaite: "le contexte demandé n'a pas pu être rouvert : aucune relecture",
    demande_satisfaite: "le contexte demandé a été rouvert dans le contrat, sans appel modèle",
    couverture_declaree_sans_candidat: "le contrôle déclare couverte une sous-question dont la lecture n'a rien retrouvé",
    relance_facette_sans_place: "sous-question sans réponse : aucune place pour une relance qui conserve les acquis",
    facettes_non_couvertes: "des sous-questions posées ne sont pas couvertes",
    fait_cite_hors_sujet: "un fragment cité pour une qualité n'en emploie aucun mot",
    fait_cite_introuvable: "une qualité dite établie ne cite aucun fragment des faits déclarés",
    faits_compris_hors_borne: "des faits compris dépassent leur borne",
    hors_objet_incoherent: "une affirmation dite hors de l'objet de la question porte une applicabilité qui vise ce cas, ou répond à une sous-question : le motif est écarté",
    hors_perimetre_desarme: "refus hors périmètre désarmé : la liste des rubriques était tronquée",
    intention_expliquee: "intention rendue par le modèle, et déclencheurs qui la confirment",
    lecture_partielle: "lecture bornée sans affirmation retenue : ce qui a été lu est chiffré, aucune absence n'est affirmée",
    libelles_hors_borne: "des libellés de portée dépassent leur borne",
    lignes_incompletes: "un bloc cité n'est pas la concaténation de ses lignes",
    limites_non_affichees: "des phrases de limite n'ont pas été affichées",
    parse_retry: "réponse du modèle relancée après un parse invalide",
    claims_hors_borne_ecartees: "des affirmations au-delà de la borne de rédaction ont été écartées",
    corrections_non_retenues: "des corrections de la relance dépassaient la borne : écartées après les acquis",
    limites_non_reconduites: "des réserves n'ont pas pu être reconduites sous la borne de segments",
    pertinence_incomplete: "des affirmations sont restées sans verdict de pertinence",
    phrases_de_claim_retirees: "des phrases d'une affirmation retenue avancent plus que ses passages : retirées, le reste est affiché",
    phrases_rattachees:
      "des phrases que les passages joints n'établissaient pas sont soutenues par un passage lu ailleurs : conservées, la citation a été ajoutée",
    qualite_de_la_clause_non_enumeree: "une qualité écrite par la clause n'a pas été énumérée",
    qualite_etablie_par_qualification: "une qualité que la clause nomme est remplie par le fait déclaré",
    qualite_exigee_non_etablie: "une qualité exigée par une clause n'est pas établie par les faits",
    qualites_non_enumerees: "les qualités exigées ou établies n'ont pas été énumérées",
    rattachement_contradictoire: "une phrase rattachée à deux passages différents : aucun n'est retenu",
    rattachements_ignores:
      "des rattachements proposés n'ont pas été suivis (passage non lu, ou citation non prouvable) : les phrases restent retirées",
    rattachement_fondu_dans_la_clause: "le lien avec les faits est écrit dans la phrase de la clause, où il est jugé contre les citations",
    rattachement_hors_borne: "un lien avec les faits dépasse la borne d'affichage : ignoré, la clause reste",
    renvoi_cp_non_enumere: "la clause renvoie aux conditions particulières ou à une option, ce que la lecture n'avait pas rendu",
    quotes_fusionnees: "deux extraits d'un même bloc réunis en un seul passage",
    amorce_jointe: "la phrase qui ouvre une énumération a été jointe à l'item cité",
    segment_orphelin_joint: "une phrase sans antécédent a été jointe à la précédente",
    claims_par_facette: "les sous-questions qui portent au moins une affirmation rédigée",
    blocs_decisionnels_ecartes: "des clauses lues ont été écartées par la rédaction, avec leur motif",
    quote_trop_longue: "des citations vérifiées dépassent la longueur maximale",
    raison_hors_vocabulaire: "une raison de rejet hors du vocabulaire fermé écarte l'affirmation",
    refus: "refus composé, avec sa preuve d'absence",
    navigation: "lecture du document par le modèle : tours, sections ouvertes et budget",
    noeuds_du_profil: "fiches suggérées par votre profil : celles que le modèle a ouvertes, celles qu'il a laissées",
    lecture_refusee: "des sections sont restées fermées : le budget de lecture n'en laissait pas la place",
    tours_epuises: "plafond de tours de lecture atteint : la lecture est déclarée bornée",
    tour_terminal_force: "une dernière section a été ouverte avant la rédaction, dans les bornes de lecture",
    tour_terminal_repris: "un tour de lecture tronqué par sa propre sortie a été redemandé une fois dans le même fil",
    ebauche_dans_la_conversation: "réponse rédigée dans la conversation de lecture, sur les seules sections ouvertes",
    relance_abandonnee: "relance de la rédaction abandonnée faute de budget",
    relance_moins_bonne: "relance rendue moins bonne que le premier essai : écartée",
    relance_fondatrice_sans_place: "clause décisionnelle jamais citée : aucune place pour une relance qui conserve les acquis",
    relance_sans_place_pour_les_limites: "relance non lancée : elle aurait tronqué une réserve acquise",
    relance_sans_effet: "relance sans effet sur la réponse",
    reprise_moins_bonne: "relecture après contexte moins bonne que le premier contrôle : écartée",
    reprise_sans_place: "aucune place sous le budget pour relire après la demande de contexte",
    reprise_unique: "une seule relecture après la satisfaction de la demande de contexte",
    satisfaction_demande: "blocs rouverts pour satisfaire la demande de contexte du contrôle",
    seconde_demande_refusee: "la relecture redemande du contexte : refusée, jamais satisfaite",
    segment_contradictoire: "deux verdicts opposés pour une même phrase",
    segments_derives: "phrases identiques à leur affirmation : un seul jugement, celui de la pertinence",
    segments_derives_masques: "phrases identiques à une affirmation rejetée ou sans verdict : masquées avec elle",
    segments_non_soutenus: "des phrases avancent plus que les passages joints",
    segments_retires: "des phrases ont été retirées de la réponse",
    verdict: "verdict rendu sur les affirmations affichées",
    verdict_contradictoire: "deux verdicts opposés pour une même affirmation",
    reponse_liee: "réponse liée à la question active",
    corpus_reutilise: "corpus vérifié du premier tour réutilisé sans nouvelle recherche",
    sans_modele: "suivi déterministe sans appel modèle",
    etat_signe: "état décisif transporté vérifié par le serveur",
    verdict_recalcule: "verdict recalculé par la table AD-6"
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
    cle_fournisseur_absente:
      "aucune clé fournisseur n'est configurée : le service ne peut répondre à aucune question",
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
                 rattachement: claim.rattachement, clauses: clauses });
    }
    if (rang !== plates.length) return null;
    return out;
  }

  // ---------- l'amorce d'une énumération (story 5.6, L2d) ----------
  //
  // Le contrat écrit ses garanties en deux blocs : une phrase qui **appelle** la liste (« La
  // Compagnie assure les biens désignés contre les dégâts des eaux c'est-à-dire : ») puis les items
  // qui la complètent. Le moteur cite les deux sous la **même** affirmation, et la page en faisait
  // deux cartes — même chemin, même phrase en clair, deux boutons « Voir la page ». Le lecteur y
  // lisait deux appuis là où il n'y en a qu'un, coupé en son milieu par le PDF.
  //
  // L'amorce se reconnaît sans deviner, et les trois conditions sont exigées ensemble :
  //   1. **la même affirmation** — deux blocs qu'aucune claim ne réunit ne se fondent jamais ;
  //   2. **le bloc immédiatement précédent du même chemin** — même document, même page, rang `n`
  //      puis `n + 1`, et le même intitulé de section : c'est l'adjacence typographique du PDF ;
  //   3. **un texte qui appelle une suite** — il finit par « : », ou le moteur l'a publié en
  //      `status: "contexte"`, ce qui est exactement ce qu'il dit d'un bloc qui n'est pas décisif
  //      par lui-même.
  //
  // Faute d'une seule des trois, les deux clauses restent deux cartes : une fusion devinée
  // masquerait un appui distinct sous un autre.
  var RE_BLOC_ID = /^(.+):p(\d+):(\d+)$/;

  function reperesBloc(id) {
    var m = RE_BLOC_ID.exec(String(id || ""));
    return m ? { doc: m[1], page: m[2], rang: Number(m[3]) } : null;
  }

  function cheminJoint(src) {
    return tableau(src && src.chemin).map(function (t) { return String(t || "").trim(); }).join(" › ");
  }

  /** Le texte d'un bloc, tel qu'il se lit : le paragraphe entier s'il est publié, sinon la quote. */
  function texteDeBloc(src) {
    var s = src || {};
    var brut = typeof s.texte_bloc === "string" && s.texte_bloc ? s.texte_bloc : s.quote;
    return String(brut || "").replace(/\s+/g, " ").trim();
  }

  // `memeClaim` : `true` quand l'appelant tient déjà les deux clauses **d'une même affirmation** —
  // l'appariement positionnel les groupe par claim sans que les sources portent un `claim_id`.
  function estAmorce(amorce, item, memeClaim) {
    if (!estObjetPlat(amorce) || !estObjetPlat(item)) return false;
    if (memeClaim !== true && (!amorce.claim_id || amorce.claim_id !== item.claim_id)) return false;
    var a = reperesBloc(amorce.block_id);
    var b = reperesBloc(item.block_id);
    if (!a || !b || a.doc !== b.doc || a.page !== b.page || a.rang + 1 !== b.rang) return false;
    if (cheminJoint(amorce) !== cheminJoint(item)) return false;
    return /:$/.test(texteDeBloc(amorce)) || amorce.status === "contexte";
  }

  /**
   * La citation **principale** d'une affirmation : la première qui n'est pas l'amorce de la
   * suivante. Une amorce annonce ce que dit le bloc d'après ; c'est le bloc d'après qui porte ce
   * que le contrat prévoit, et donc le `kind` sur lequel se décide ce qui répond.
   */
  function citationPrincipale(clauses) {
    var liste = tableau(clauses);
    for (var i = 0; i < liste.length; i++) {
      if (i + 1 < liste.length && estAmorce(liste[i], liste[i + 1], true)) continue;
      return liste[i];
    }
    return null;
  }

  /**
   * `claim_id → ses clauses`, par les deux chemins sûrs de `appuisDe()` et jamais autrement :
   * le `claim_id` que le moteur publie sur chaque source, sinon l'appariement positionnel d'AD-11.
   * Au moindre désaccord, `null` — et ce qui s'en sert retombe sur son comportement d'avant.
   */
  function citationsParClaim(reponse) {
    var r = reponse || {};
    var a = r.answer || {};
    var sources = tableau(r.sources);
    if (!sources.length) return null;
    var connus = Object.create(null);
    tableau(a.claims).forEach(function (c) {
      if (c && typeof c.claim_id === "string") connus[c.claim_id] = true;
    });
    var parClaim = Object.create(null);
    var tousIdentifies = sources.every(function (s) {
      return s && typeof s.claim_id === "string" && s.claim_id &&
        Object.prototype.hasOwnProperty.call(connus, s.claim_id);
    });
    if (tousIdentifies) {
      sources.forEach(function (s) {
        if (!parClaim[s.claim_id]) parClaim[s.claim_id] = [];
        parClaim[s.claim_id].push(s);
      });
      return parClaim;
    }
    var appariees = clausesParClaim(a, sources);
    if (!appariees) return null;
    appariees.forEach(function (e) {
      if (typeof e.claim_id === "string") parClaim[e.claim_id] = tableau(e.clauses);
    });
    return parClaim;
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

  // ---------- retrouver la citation dans son paragraphe (story 5.6, L2) ----------
  //
  // Le bloc 2 affiche le **paragraphe entier** (`ClauseSource.texte_bloc`) avec la partie citée
  // surlignée. La `quote` est ré-extraite du corpus par le serveur aux offsets prouvés (AD-3) :
  // elle est donc bien une sous-chaîne de son bloc — mais pas forcément **caractère pour
  // caractère** sur le fil, où passent des apostrophes typographiques, des espaces insécables et
  // des retours à la ligne que la mise en page du PDF a semés. Chercher `indexOf(quote)` échouait
  // sur des paragraphes où la citation est pourtant là.
  //
  // La recherche se fait donc sur une **carte normalisée** : chaque caractère de la source produit
  // exactement un caractère normalisé (apostrophes et guillemets ramenés à l'ASCII, tirets longs
  // ramenés au trait d'union, toute suite d'espaces ramenée à un espace, minuscules), et un index
  // renvoie chaque position normalisée à sa position d'origine. Le surlignage porte donc sur le
  // texte **original**, jamais sur une copie retouchée. Si la citation reste introuvable, la page
  // affiche la quote seule : elle ne fabrique aucun surlignage approximatif.
  var EQUIVALENTS = {
    "‘": "'", "’": "'", "ʼ": "'", "′": "'", "´": "'", "`": "'",
    "“": "\"", "”": "\"", "„": "\"", "«": "\"", "»": "\"",
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-",
    "−": "-", "…": "…"
  };

  /** Un caractère normalisé, **toujours** de longueur 1 : l'index de position en dépend. */
  function normaliserCaractere(c) {
    var n = Object.prototype.hasOwnProperty.call(EQUIVALENTS, c) ? EQUIVALENTS[c] : c;
    var bas = n.toLocaleLowerCase();
    // `İ` (U+0130) se minuscule en deux points de code : le garder tel quel préserve la bijection
    // dont l'index a besoin, au prix d'une insensibilité à la casse en moins sur ce seul caractère.
    return bas.length === 1 ? bas : n;
  }

  /** `{texte, index}` : le texte normalisé, et la position d'origine de chacun de ses caractères. */
  function carteNormalisee(brut) {
    var s = String(brut === undefined || brut === null ? "" : brut);
    var texte = "";
    var index = [];
    var espacePrecedent = false;
    for (var i = 0; i < s.length; i++) {
      var c = normaliserCaractere(s.charAt(i));
      if (/\s/.test(c)) {
        if (espacePrecedent) continue;  // une suite d'espaces vaut un espace
        c = " ";
        espacePrecedent = true;
      } else {
        espacePrecedent = false;
      }
      texte += c;
      index.push(i);
    }
    index.push(s.length);
    return { texte: texte, index: index };
  }

  /** Les bornes de `aiguille` dans `texte`, en coordonnées **d'origine**, ou `null`. */
  function trouverPassage(texte, aiguille) {
    var cible = carteNormalisee(aiguille).texte.trim();
    if (!cible) return null;
    var source = carteNormalisee(texte);
    var p = source.texte.indexOf(cible);
    if (p < 0) return null;
    return { debut: source.index[p], fin: source.index[p + cible.length] };
  }

  /** Le texte, coupé en trois nœuds dont celui du milieu est `<mark>`. Les vides sont omis. */
  function texteSurligne(texte, bornes, classeMarque) {
    var s = String(texte || "");
    var parts = [
      noeud("span", null, s.slice(0, bornes.debut)),
      noeud("mark", classeMarque, s.slice(bornes.debut, bornes.fin)),
      noeud("span", null, s.slice(bornes.fin))
    ];
    return parts.filter(function (n) { return n.texte !== ""; });
  }

  // Le paragraphe entier est affiché **par défaut** : c'est ce qui permet de juger une clause. Un
  // bloc très long (une énumération de définitions, une liste d'exclusions) noierait pourtant la
  // phrase qui décide, et un écran qu'on ne lit pas ne vaut pas mieux qu'un écran replié. Au-delà
  // de ce seuil, la page montre la phrase citée avec **une** phrase avant et **une** après, et
  // offre le paragraphe entier au clic — dépliage à la demande, jamais l'inverse (aucun contenu ne
  // se referme sous le lecteur).
  var BLOC_LONG_CARACTERES = 600;

  /** Les bornes des phrases de `texte` : `[{debut, fin}]`, couvrant tout le texte. */
  function bornesPhrases(texte) {
    var s = String(texte || "");
    var out = [];
    var debut = 0;
    for (var i = 0; i < s.length; i++) {
      var c = s.charAt(i);
      if (c !== "." && c !== "!" && c !== "?" && c !== "…" && c !== ";" && c !== "\n") continue;
      // La fin d'une phrase est le dernier signe d'une suite (« … ! », « ?! »), suivie d'un blanc
      // ou de la fin du texte : « 3.1.4.1 » et « n° 2 » ne coupent donc pas une phrase en deux.
      var j = i;
      while (j + 1 < s.length && ".!?…;".indexOf(s.charAt(j + 1)) !== -1) j++;
      if (j + 1 < s.length && !/\s/.test(s.charAt(j + 1))) continue;
      out.push({ debut: debut, fin: j + 1 });
      var k = j + 1;
      while (k < s.length && /\s/.test(s.charAt(k))) k++;
      debut = k;
      i = k - 1;
    }
    if (debut < s.length) out.push({ debut: debut, fin: s.length });
    return out;
  }

  /** Les bornes de l'extrait « une phrase avant, la citation, une phrase après ». */
  function extraitAutour(texte, bornes) {
    var phrases = bornesPhrases(texte);
    if (!phrases.length) return { debut: 0, fin: String(texte || "").length };
    var premiere = -1;
    var derniere = -1;
    for (var i = 0; i < phrases.length; i++) {
      if (phrases[i].fin > bornes.debut && phrases[i].debut < bornes.fin) {
        if (premiere < 0) premiere = i;
        derniere = i;
      }
    }
    if (premiere < 0) return { debut: 0, fin: String(texte || "").length };
    var a = Math.max(0, premiere - 1);
    var b = Math.min(phrases.length - 1, derniere + 1);
    return { debut: phrases[a].debut, fin: phrases[b].fin };
  }

  /** Les phrases du texte, groupées par paquets d'au plus `taille` phrases. */
  function paragraphes(texte, taille) {
    var s = String(texte || "");
    var phrases = bornesPhrases(s).map(function (b) { return s.slice(b.debut, b.fin).trim(); })
      .filter(function (t) { return t; });
    var out = [];
    for (var i = 0; i < phrases.length; i += taille) {
      out.push(phrases.slice(i, i + taille).join(" "));
    }
    return out;
  }

  // ---------- les vues ----------

  // ---------- l'attente, habillée (story 5.6, L2) ----------
  //
  // Une soumission coûte de vingt secondes à une minute. La page affichait une phrase et rien ne
  // bougeait : un écran figé se lit comme un écran cassé. Elle affiche désormais les trois étapes
  // du travail réel, celle en cours marquée, un chronomètre, et l'ordre de grandeur attendu.
  //
  // Deux sources d'avancement, dans cet ordre :
  //   1. le flux `POST /api/v1/sinistre/progression` (SSE), qui dit l'étape que le serveur exécute ;
  //   2. à défaut, l'**estimation** par le temps écoulé sur les durées ci-dessous.
  //
  // Les durées sont des estimations mesurées sur les tours live consignés dans `docs/tests-live.md`
  // (21 à 47 s au total). Elles ne pilotent rien d'autre que l'apparence de la barre : aucune
  // décision, aucun abandon, aucun message ne s'y adosse — la borne d'abandon reste `abandonMs()`,
  // lue sur les seuils du serveur.
  var ETAPES_SINISTRE = [
    { nom: "comprendre", libelle: "Je lis le contrat", ms: 14000 },
    { nom: "rediger", libelle: "J'écris la réponse", ms: 22000 },
    { nom: "verifier", libelle: "Je vérifie chaque citation", ms: 16000 }
  ];

  // La durée annoncée à l'utilisateur. `/sante` ne publie aucun seuil de durée **typique** — les
  // seuils qu'il publie sont des bornes (`deadline_s`) et non des attentes —, donc la phrase se
  // compose sur cette constante, qui est la somme des estimations ci-dessus arrondie.
  var DUREE_ANNONCEE_S = 60;

  function dureeTotaleEstimee() {
    return ETAPES_SINISTRE.reduce(function (t, e) { return t + e.ms; }, 0);
  }

  function chrono(ms) {
    var s = Math.max(0, Math.floor(ms / 1000));
    var m = Math.floor(s / 60);
    var r = s % 60;
    return m + ":" + (r < 10 ? "0" + r : String(r));
  }

  // Les trois états d'une étape, et leurs mots. Ils sont composés **une fois** et lus par la vue
  // comme par la mise à jour en place : deux tables auraient divergé au premier changement.
  var ETATS_ETAPE = { faite: "terminé", encours: "en cours", attente: "à venir" };

  function etatEtape(rang, i) { return i < rang ? "faite" : (i === rang ? "encours" : "attente"); }

  function libellesEtapes(etat) {
    var e = etat || {};
    return tableau(e.libelles).length
      ? tableau(e.libelles).map(String)
      : ETAPES_SINISTRE.map(function (x) { return x.libelle; });
  }

  function noteAttente(serveur) {
    return serveur
      ? "Le serveur annonce l'étape en cours ; comptez environ " + DUREE_ANNONCEE_S +
        " secondes en tout."
      : "Plusieurs appels au modèle s'enchaînent, et une vérification peut en relancer un : " +
        "comptez environ " + DUREE_ANNONCEE_S + " secondes. L'avancement ci-dessus est estimé " +
        "sur le temps écoulé.";
  }

  /**
   * L'état de la barre après `msEcoule` millisecondes, sans aucun événement du serveur.
   *
   * Le rang estimé n'est **jamais** annoncé comme un fait du serveur : la dernière étape reste « en
   * cours » indéfiniment plutôt que d'afficher un travail terminé qui ne l'est pas.
   */
  function rangEstime(msEcoule) {
    var cumul = 0;
    for (var i = 0; i < ETAPES_SINISTRE.length; i++) {
      cumul += ETAPES_SINISTRE[i].ms;
      if (msEcoule < cumul) return i;
    }
    return ETAPES_SINISTRE.length - 1;
  }

  /**
   * La vue d'attente : les étapes, le chronomètre, la phrase de durée.
   *
   * `etat` = `{rang, total, libelles, msEcoule, serveur}`. `serveur` dit si l'avancement vient du
   * flux ou d'une estimation, et l'écran le **dit** : une barre qui avance toute seule ne doit pas
   * se faire passer pour une mesure.
   */
  function vueAttente(etat) {
    var e = etat || {};
    var rang = typeof e.rang === "number" && isFinite(e.rang) ? e.rang : 0;
    var etapes = libellesEtapes(e).map(function (libelle, i) {
      var cle = etatEtape(rang, i);
      return noeud("li", "prog-etape prog-" + cle, null, [
        noeud("span", "prog-puce", "", null, { "aria-hidden": "true" }),
        noeud("span", "prog-libelle", libelle),
        // L'état de chaque étape est écrit en toutes lettres : la puce colorée ne le porte pas
        // seule, et un lecteur d'écran entend « terminé », « en cours », « à venir ».
        noeud("span", "prog-etat", ETATS_ETAPE[cle])
      ]);
    });
    return noeud("div", "carte attente", null, [
      // La carte d'attente occupe **la place de la réponse** : c'est le même conteneur, et elle
      // le dit. Une barre posée au-dessus du formulaire aurait fait attendre à un endroit et
      // apparaître la réponse à un autre.
      noeud("h3", "attente-titre", "La réponse s'écrit ici"),
      noeud("ol", "prog", null, etapes),
      noeud("div", "prog-pied", null, [
        noeud("span", "prog-chrono", chrono(e.msEcoule || 0)),
        noeud("span", "attente-note", noteAttente(e.serveur))
      ])
    ]);
  }

  /**
   * Met la barre à jour **en place**, ou rend `false` si sa structure a changé.
   *
   * Repeindre la carte entière chaque seconde était deux fautes en une. `#resultat` porte
   * `aria-live="polite"` : un lecteur d'écran aurait relu la barre et son chronomètre **à chaque
   * seconde**, pendant une minute. Et un nœud remplacé perd le focus qu'il portait. Ici seuls les
   * textes et les classes changent ; le nombre d'étapes, lui, ne change que si le serveur en
   * annonce un autre — et c'est le seul cas où la carte est repeinte.
   */
  function majAttente(racine, etat) {
    if (!racine || typeof racine.querySelectorAll !== "function") return false;
    var e = etat || {};
    var libelles = libellesEtapes(e);
    var rang = typeof e.rang === "number" && isFinite(e.rang) ? e.rang : 0;
    var etapes = racine.querySelectorAll(".prog-etape");
    if (etapes.length !== libelles.length) return false;
    etapes.forEach(function (n, i) {
      var cle = etatEtape(rang, i);
      n.className = "prog-etape prog-" + cle;
      var mot = n.querySelector(".prog-etat");
      if (mot) mot.textContent = ETATS_ETAPE[cle];
      var libelle = n.querySelector(".prog-libelle");
      if (libelle) libelle.textContent = libelles[i];
    });
    var horloge = racine.querySelector(".prog-chrono");
    if (horloge) horloge.textContent = chrono(e.msEcoule || 0);
    var note = racine.querySelector(".attente-note");
    if (note) note.textContent = noteAttente(e.serveur);
    return true;
  }

  /**
   * Peint l'attente et la tient à jour jusqu'au résultat.
   *
   * Rend `{etape, fin}` : `etape(evt)` prend un événement `etape` du flux, `fin()` arrête tout.
   * Aucun état global — un second envoi crée un second minuteur, et le premier est arrêté par
   * l'appelant avant de repeindre.
   */
  function suivreAttente(hote) {
    var debut = Date.now();
    var etat = { rang: 0, libelles: null, msEcoule: 0, serveur: false };
    var minuteur = null;
    var carte = null;

    function peindreEtat() {
      etat.msEcoule = Date.now() - debut;
      if (!etat.serveur) etat.rang = rangEstime(etat.msEcoule);
      // Mise à jour en place tant que la structure tient ; repeinture seulement quand le serveur
      // annonce un autre nombre d'étapes (voir `majAttente`).
      if (carte && majAttente(carte, etat)) return;
      carte = peindre(vueAttente(etat), hote);
    }

    peindreEtat();
    if (typeof setInterval === "function") {
      minuteur = setInterval(peindreEtat, 1000);
    }

    return {
      etape: function (evt) {
        var e = evt || {};
        if (typeof e.rang === "number" && isFinite(e.rang) && e.rang >= 0) {
          etat.serveur = true;
          etat.rang = Math.floor(e.rang);
        }
        if (typeof e.total === "number" && isFinite(e.total) && e.total > 0 &&
            typeof e.libelle === "string" && e.libelle) {
          // Le serveur nomme ses propres étapes : la barre suit ses libellés dès qu'elle les a
          // tous, et garde les siens tant que le flux n'en a annoncé qu'une partie.
          var total = Math.floor(e.total);
          if (!etat.libelles || etat.libelles.length !== total) {
            etat.libelles = ETAPES_SINISTRE.map(function (x) { return x.libelle; }).slice(0, total);
            while (etat.libelles.length < total) etat.libelles.push("Étape " + (etat.libelles.length + 1));
          }
          if (etat.rang < total) etat.libelles[etat.rang] = e.libelle;
        }
        peindreEtat();
      },
      fin: function () {
        if (minuteur !== null && typeof clearInterval === "function") clearInterval(minuteur);
        minuteur = null;
      }
    };
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
      var meta = "statut effectif : " + statut + " · édition " + editionAvecReserve(d.edition);
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
      var edition = " — édition " + editionAvecReserve(d.edition);
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

  function texteDecisif(texte, termes) {
    var source = String(texte || "");
    var candidats = tableau(termes).map(function (t) { return String(t || "").trim(); })
      .filter(function (t) { return t.length >= 3; })
      .sort(function (a, b) { return b.length - a.length; });
    var trouve = null;
    var position = -1;
    candidats.some(function (terme) {
      var i = source.toLocaleLowerCase().indexOf(terme.toLocaleLowerCase());
      if (i < 0) return false;
      trouve = terme; position = i; return true;
    });
    if (position < 0) return [noeud("span", null, "« " + source + " »")];
    return [
      noeud("span", null, "« " + source.slice(0, position)),
      noeud("mark", "mot-decisif", source.slice(position, position + trouve.length)),
      noeud("span", null, source.slice(position + trouve.length) + " »")
    ];
  }

  // AD-4/D4 : « les faits compris » sont ce que *comprendre* a extrait des faits déclarés, pas la
  // description renvoyée en écho. C'est le seul endroit où l'utilisateur peut constater qu'il a été
  // mal compris — et c'est pour cela que l'AC l'exige.
  var CHAMPS_COMPRIS = [
    { cle: "bien", libelle: "Bien concerné" },
    { cle: "evenement", libelle: "Événement" },
    { cle: "lieu", libelle: "Lieu" },
    { cle: "cause", libelle: "Cause" },
    { cle: "moment", libelle: "Moment" },
    { cle: "montant_eur", libelle: "Montant déclaré" }
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

  var SOURCES_FAIT = {
    declaration_initiale: "déclaration initiale du client",
    extraction: "extrait par le modèle au premier tour",
    reponse_client: "réponse du client",
    correction: "correction explicite",
    resolution: "version choisie pour résoudre un conflit"
  };

  function faitsRetenus(conversation) {
    var c = conversation || {};
    var exclus = {};
    tableau(c.facts).forEach(function (f) {
      if (f && f.replaces_event_id) exclus[f.replaces_event_id] = true;
    });
    tableau(c.conflicts).forEach(function (conflit) {
      if (!conflit) return;
      if (conflit.status === "ouvert") {
        tableau(conflit.event_ids).forEach(function (id) { exclus[id] = true; });
      } else {
        tableau(conflit.event_ids).forEach(function (id) {
          if (id !== conflit.chosen_event_id) exclus[id] = true;
        });
      }
    });
    return tableau(c.facts).filter(function (f) { return f && !exclus[f.event_id]; });
  }

  // Story 5.6 (L2c) — un seul intitulé pour les questions, quel que soit le chemin qui les porte :
  // la conversation (`TargetQuestion.text`) ou, à défaut, `verdict.ask_client`. « Prochaines
  // questions décisives » et « Questions à poser au client » nommaient l'objet du moteur ; l'assuré
  // veut savoir à quoi elles servent — elles affinent le verdict qu'il vient de lire.
  var TITRE_QUESTIONS = "Pour affiner le verdict";

  /**
   * Les questions décisives et leur mécanique de réponse : sélection, Oui / Non / Je ne sais pas,
   * champ « Préciser un fait ». Extrait de `conversationVue()` sans rien changer à ses classes ni à
   * ses `data-*` — c'est **le** mécanisme existant, déplacé, pas un second formulaire.
   */
  function questionsVue(conversation) {
    var actives = tableau(conversation && conversation.questions).filter(function (q) {
      return q && q.status === "active";
    });
    if (!actives.length) return null;
    var selections = actives.map(function (q, index) {
      return noeud("button", "conv-selection-question", String(q.text), null, {
        "type": "button", "data-question-id": String(q.question_id),
        "data-question-text": String(q.text), "aria-pressed": index === 0 ? "true" : "false"
      });
    });
    return section("conv-questions", TITRE_QUESTIONS, [
      // Story 5.6 (L2c) — la phrase d'explication vient **avant** les questions, parce que c'est
      // elle qui dit ce qu'un clic déclenche. Lancelot l'a demandée mot pour mot en lisant l'écran
      // de prod : « quand je soumets, ça met à jour la réponse sans appel au modèle ? pas clair ».
      // Elle remplace « ces questions viennent des exigences des clauses… », qui disait d'où elles
      // venaient — ce que personne ne demandait — et taisait ce qui allait se passer.
      noeud("p", "conv-explication",
            "Vos réponses sont ajoutées au dossier comme des faits et le verdict est recalculé, " +
            "sans relire le contrat ni rappeler le modèle."),
      noeud("div", "conv-liste-questions", null, selections),
      noeud("div", "conv-reponse-commune", null, [
        noeud("p", "conv-question-contexte", String(actives[0].text), null,
              { "data-selected-question-id": String(actives[0].question_id) }),
        noeud("div", "conv-reponses", null, [
          noeud("button", "conv-repondre", "Oui", null, { "type": "button", "data-value": "oui" }),
          noeud("button", "conv-repondre", "Non", null, { "type": "button", "data-value": "non" }),
          // « Je ne sais pas » : c'est l'assuré qui répond, pas un dossier qu'on annote.
          noeud("button", "conv-repondre", "Je ne sais pas", null,
                { "type": "button", "data-value": "inconnu" })
        ]),
        // « Envoyer la réponse libre » ne disait pas *à quoi* le champ répondait — Lancelot a
        // demandé « réponse ouverte à quoi ? ». Le champ est nommé par ce qu'il attend, et son
        // exemple montre la forme d'un fait ; le bouton dit ce que le clic ajoute.
        noeud("div", "conv-libre", null, [
          noeud("label", "conv-libre-nom", "Préciser un fait", null,
                { "for": "conv-reponse-libre" }),
          noeud("input", "conv-reponse-libre", null, null,
                { "type": "text", "id": "conv-reponse-libre", "maxlength": "500",
                  "aria-label": "Préciser un fait",
                  "placeholder": "ex. : la garantie dégâts des eaux figure dans mes conditions " +
                                 "particulières" }),
          noeud("button", "conv-envoyer-libre", "Ajouter ce fait", null, { "type": "button" })
        ])
      ])
    ]);
  }

  function conversationVue(conversation, options) {
    if (!conversation) return null;
    var opts = options || {};
    var remplaces = {};
    tableau(conversation.facts).forEach(function (f) {
      if (f && f.replaces_event_id) remplaces[f.replaces_event_id] = true;
    });
    var faits = tableau(conversation.facts).map(function (f) {
      if (!f) return null;
      var statut = remplaces[f.event_id] ? " · remplacé (historique conservé)" : "";
      var attrs = { "data-fact-key": String(f.key), "data-event-id": String(f.event_id) };
      return noeud("li", "conv-fait" + (remplaces[f.event_id] ? " conv-remplace" : ""), null, [
        noeud("span", "conv-fait-val", String(f.key) + " : " + String(f.value)),
        noeud("span", "conv-source", "source : " +
          (SOURCES_FAIT[f.source] || String(f.source)) + " · tour " + String(f.turn) + statut),
        noeud("button", "conv-corriger", "Corriger ce fait", null, attrs)
      ]);
    }).filter(Boolean);
    var enfants = [section("conv-faits", "Faits et provenance", [
      noeud("ul", "conv-faits-liste", null, faits)
    ])];

    var conflits = tableau(conversation.conflicts).filter(function (c) {
      return c && c.status === "ouvert";
    });
    if (conflits.length) {
      enfants.push(section("conv-conflits", "Contradictions à résoudre", conflits.map(function (c) {
        var versions = tableau(c.event_ids).map(function (eventId) {
          var fait = tableau(conversation.facts).filter(function (f) { return f.event_id === eventId; })[0];
          return noeud("button", "conv-resoudre", fait ? String(fait.value) : "Version inconnue", null, {
            "data-conflict-id": String(c.conflict_id), "data-event-id": String(eventId)
          });
        });
        return noeud("div", "conv-conflit", null, [
          noeud("p", null, "Deux versions restent actives pour « " + String(c.key) +
            " ». Le verdict ne se resserre pas avant votre choix."),
          noeud("div", "conv-choix", null, versions)
        ]);
      })));
    }

    var actives = tableau(conversation.questions).filter(function (q) {
      return q && q.status === "active";
    });
    // Story 5.6 (L2) : les questions décisives ne vivent plus **dans** le dossier quand la page les
    // a déjà posées en bloc 3 (« Ce qu'il me manque pour aller plus loin »). `questionsVue()` les
    // compose seule, avec exactement les mêmes classes et les mêmes `data-*` : `brancherConversation`
    // branche par sélecteur sur toute la racine peinte, donc il les trouve où qu'elles soient — mais
    // les poser **deux fois** doublerait les boutons de réponse et le champ libre sous une seule
    // question sélectionnée, donc l'appelant dit lequel des deux endroits les porte.
    if (actives.length && !opts.sansQuestions) enfants.push(questionsVue(conversation));

    var historique = tableau(conversation.history).map(function (h) {
      var causes = tableau(h.causal_events).length
        ? " · événement(s) causal(aux) : " + tableau(h.causal_events).join(" ; ") : "";
      return noeud("li", "conv-tour", "Tour " + String(h.turn) + " — " +
        String(h.value).replace(/_/g, " ") + (h.changed ? " · verdict modifié" : " · verdict stable") +
        causes + " — " + String(h.reason));
    });
    enfants.push(noeud("details", "conv-historique", null, [
      noeud("summary", null, "Historique causal du verdict"),
      noeud("ol", null, null, historique)
    ]));
    enfants.push(noeud("div", "conv-actions", null, [
      noeud("button", "conv-copier", "Copier le dossier"),
      noeud("span", "conv-statut", "", null, { "role": "status", "aria-live": "polite" })
    ]));
    return section("conversation", "Dossier conversationnel — tour " + String(conversation.turn), enfants);
  }

  function dossierTexte(reponse) {
    var r = reponse || {};
    var a = r.answer || {};
    var v = a.verdict || {};
    var c = r.conversation || {};
    var lignes = [
      "Verdict : " + String(v.value || "indisponible").replace(/_/g, " "),
      "Raison : " + String(v.reason || "indisponible"),
      "Faits retenus et provenances :"
    ];
    faitsRetenus(c).forEach(function (f) {
      lignes.push("- " + String(f.key) + " : " + String(f.value) + " [" +
        (SOURCES_FAIT[f.source] || String(f.source)) + ", tour " + String(f.turn) + "]");
    });
    lignes.push("Clauses et pages :");
    tableau(r.sources).forEach(function (s) {
      lignes.push("- " + String(s.kind) + (s.page ? ", page " + String(s.page) : "") +
        " : « " + String(s.quote) + " »");
    });
    lignes.push("Paquet manquant :");
    PIECES.forEach(function (p) { if (v.missing && v.missing[p.cle]) lignes.push("- " + p.libelle); });
    tableau(v.missing && v.missing.faits).forEach(function (f) { lignes.push("- fait : " + String(f)); });
    lignes.push("Questions restantes :");
    tableau(c.questions).filter(function (q) { return q.status === "active"; })
      .forEach(function (q) { lignes.push("- " + String(q.text)); });
    lignes.push("Référence de requête : " + String(r.trace && r.trace.request_id || "indisponible"));
    return lignes.join("\n");
  }

  var PIECES = [
    { cle: "conditions_particulieres", libelle: "les conditions particulières" },
    { cle: "options_souscrites", libelle: "les options souscrites" },
    { cle: "avenants", libelle: "les avenants" },
    { cle: "date_effet", libelle: "la date d'effet" }
  ];

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

  // ---------- les quatre blocs de la page (story 5.6, L2) ----------
  //
  // La page rendait un rapport : un badge, une raison, puis quatorze sections dans l'ordre où le
  // contrat les publie, dont les deux qui portent la preuve — les clauses citées et le texte de la
  // réponse — **repliées** dès qu'une conversation était ouverte. Un lecteur qui n'ouvre rien voyait
  // « ne tranche pas », des questions, et la liste de ce qui n'avait pas été lu.
  //
  // L'ordre est désormais fixe et il n'y a plus rien de replié au-dessus de la réponse :
  //
  //   1. La réponse                          — ce que je conclus, et le verdict traduit ;
  //   2. Sur quoi je m'appuie                — les clauses, paragraphe entier, citation surlignée ;
  //   3. Ce qu'il me manque pour aller plus loin — les questions décisives, les pièces non lues ;
  //   4. Garde-fous                          — ce qui a été relu, retiré, et par quoi c'est décidé.
  //
  // Ce qui explique **comment** la réponse a été faite reste consultable et vient après : le
  // dossier conversationnel (« Dossier »), les affirmations écartées, la trace. Rien n'a disparu ;
  // ce qui décide est passé devant ce qui documente.

  /**
   * Le titre d'un bloc : son pictogramme, puis son intitulé.
   *
   * Story 5.6 (L2b) — le numéro dans sa pastille disait le rang du bloc, pas sa nature : quatre
   * chiffres alignés sont une table des matières, pas des repères. Le pictogramme est porté par la
   * **feuille** (un masque SVG par classe de bloc, `--icone`), jamais par le script : un `<svg>`
   * composé ici obligerait `materialiser` à connaître un second espace de noms, et `className` n'est
   * pas assignable sur un élément SVG. Il est purement décoratif — l'intitulé est écrit à côté.
   */
  function titreBloc(texte) {
    return noeud("h3", "bloc-titre", null, [
      noeud("span", "bloc-icone", null, null, { "aria-hidden": "true" }),
      noeud("span", "bloc-nom", texte)
    ]);
  }

  function bloc(cls, titre, enfants) {
    var utiles = enfants.filter(Boolean);
    if (!utiles.length) return null;
    return noeud("section", "bloc " + cls, null, [titreBloc(titre)].concat(utiles));
  }

  // --- bloc 1 : la réponse -------------------------------------------------

  /**
   * Les phrases du modèle, telles qu'il les a écrites — jamais `answer.texte` recomposé.
   *
   * Story 5.6 (L2c). `answer.texte` est la concaténation de **tous** les segments : il porte donc
   * la transition « Faits compris — cause : … ; puis événement : … », qui est un écho de la saisie
   * et que le dossier redit déjà champ par champ. Elle ouvrait la réponse et repoussait la première
   * phrase du contrat sous la ligne de flottaison. Le bloc 1 lit désormais `segments` :
   *
   *   - `factuel` — ce que le contrat prévoit, et, juste derrière chaque clause, le **rattachement**
   *     aux faits que *restituer* pose comme son propre segment (`steps/restituer.py`). Deux phrases
   *     qui se suivent, la clause puis le lien avec le sinistre : c'est l'ordre de lecture naturel ;
   *   - `limite` — ce que la lecture n'a pas trouvé. Ce n'est pas un fait retenu, donc cela ne fait
   *     jamais la première phrase, mais c'est une phrase du modèle : elle s'explique en dessous ;
   *   - `transition` — écarté.
   *
   * Tout ce qui est technique est retiré ici (`enClair`), avant même d'être mis en forme.
   */
  function phrasesModele(answer) {
    var factuels = [];
    var limites = [];
    tableau(answer && answer.segments).forEach(function (seg) {
      if (!estObjetPlat(seg)) return;
      var texte = enClair(seg.text);
      if (!texte) return;
      if (seg.kind === "factuel") {
        factuels.push({ texte: texte, claims: tableau(seg.claim_ids).filter(function (id) {
          return typeof id === "string" && id;
        }) });
      } else if (seg.kind === "limite") {
        limites.push(texte);
      }
    });
    return { factuels: factuels, limites: limites };
  }

  /**
   * Story 5.6 (L2d) — la phrase en grand est celle qui **répond**, pas la première rendue.
   *
   * Le tour du robinet ouvrait sur « Les présentes conditions spéciales sont applicables si les
   * conditions particulières mentionnent que la garantie « dégâts des eaux » est souscrite. » : le
   * modèle énonce d'abord la clause d'ouverture de la section, et le titre disait donc une
   * condition d'application là où le lecteur cherche s'il est couvert.
   *
   * La règle est déterministe et ne lit que ce que le moteur publie : la première phrase factuelle
   * rattachée à une affirmation dont la **citation principale** est une `garantie` ; à défaut une
   * `exclusion` — ce qui exclut est aussi une réponse — ; à défaut la première phrase, comme avant.
   * La phrase choisie ne quitte pas la réponse pour autant : toutes les autres restent dans le
   * corps **dans leur ordre**, la condition à sa place, juste sous le titre.
   */
  var ORDRE_PHRASE_REPONSE = ["garantie", "exclusion"];

  function indexPhraseReponse(factuels, parClaim) {
    if (!parClaim || !factuels.length) return 0;
    for (var k = 0; k < ORDRE_PHRASE_REPONSE.length; k++) {
      for (var i = 0; i < factuels.length; i++) {
        var ids = factuels[i].claims;
        for (var j = 0; j < ids.length; j++) {
          var principale = citationPrincipale(parClaim[ids[j]]);
          if (principale && principale.kind === ORDRE_PHRASE_REPONSE[k]) return i;
        }
      }
    }
    return 0;
  }

  /**
   * La réponse en clair : la phrase qui répond en grand, puis l'explication.
   *
   * Les phrases viennent **entières** du serveur et la page n'en recompose aucune — elle leur donne
   * une hiérarchie de lecture. Toutes restent affichées : les autres se groupent par trois dans
   * l'ordre du modèle, ce qui donne des paragraphes qui se lisent, sans jamais retrancher la fin
   * d'une réponse longue ni déplacer une phrase que le titre a laissée derrière lui.
   */
  function reponseVue(phrases, iTitre) {
    if (!phrases.length) return [];
    var i = (typeof iTitre === "number" && iTitre >= 0 && iTitre < phrases.length) ? iTitre : 0;
    var reste = phrases.slice(0, i).concat(phrases.slice(i + 1));
    var corps = [noeud("p", "reponse-phrase", phrases[i])];
    for (var k = 0; k < reste.length; k += 3) {
      corps.push(noeud("p", "reponse-suite", reste.slice(k, k + 3).join(" ")));
    }
    return corps;
  }

  /**
   * Les faits que l'assuré a lui-même apportés, dans l'ordre où il les a donnés.
   *
   * Ce sont les réponses aux questions du bloc 3 et les corrections : ce qui distingue le verdict
   * affiché de celui du premier tour. Les faits extraits de la description initiale n'en sont pas —
   * ils sont déjà dits par « Ce que j'ai compris du sinistre ».
   */
  var SOURCES_REPONSE_CLIENT = { reponse_client: true, correction: true, resolution: true };

  function faitsApportes(conversation) {
    return faitsRetenus(conversation).filter(function (f) {
      return f && SOURCES_REPONSE_CLIENT[f.source] === true;
    });
  }

  /**
   * Story 5.6 (L2c) — après une réponse, le bloc 1 dit **d'où vient** le verdict affiché.
   *
   * « Verdict recalculé : sous conditions. » était la seule trace du recalcul, écrite comme un log
   * et posée à la place de la réponse. Le recalcul se dit ici, en tête du bloc, avec les faits que
   * l'assuré a apportés et le bouton qui les corrige — et la réponse du modèle reste entière en
   * dessous. Le bouton porte les mêmes classe et `data-*` que celui du dossier : `brancherConversation`
   * le trouve par sélecteur sur toute la racine peinte, il n'y a pas un second mécanisme.
   */
  function majParReponses(conversation) {
    var apportes = faitsApportes(conversation);
    if (!apportes.length) return null;
    var lignes = apportes.map(function (f) {
      return noeud("li", "maj-fait", null, [
        noeud("span", "maj-fait-val", String(f.key) + " : " + String(f.value)),
        noeud("button", "conv-corriger", "Corriger", null,
              { "data-fact-key": String(f.key), "data-event-id": String(f.event_id) })
      ]);
    });
    return noeud("div", "reponse-maj", null, [
      noeud("p", "maj-tete", "Verdict mis à jour avec vos réponses"),
      noeud("ul", "maj-liste", null, lignes)
    ]);
  }

  /**
   * Story 5.6 (L2b) — trois lignes grises se suivaient sous la pastille et disaient la même chose
   * que la phrase au-dessus d'elles. Deux d'entre elles s'en vont d'ici :
   *
   *   - la **portée** (« conditions générales seules, pas un avis d'expert ») était répétée deux
   *     fois sur l'écran, ici et dans les garde-fous. Elle ne vit plus que là-bas, une fois ;
   *   - la **raison de la table** est une reformulation de la règle que la pastille nomme déjà :
   *     elle dit *par quelle règle* le verdict est tombé, jamais un fait sur le sinistre. Dès qu'il
   *     y a une première phrase, cette phrase porte déjà la conclusion en français — la raison
   *     n'ajoute alors rien à l'écran et se replie dans « Comment cette réponse a été obtenue ».
   *
   * Ce n'est pas une suppression : sans première phrase, il n'y a plus rien pour dire pourquoi, et
   * la raison **reste** sous la pastille. Et si la trace ne se peint pas — un corps sans `trace` —,
   * il n'y a nulle part où la replier : elle reste ici aussi. Elle ne disparaît jamais des deux.
   *
   * Story 5.6 (L2c) — une exception à cette dernière règle, et une seule : une raison **technique**
   * ne remonte jamais à l'écran principal, même sans trace où la ranger. C'est le défaut lu en
   * prod ; le garder « au cas où » reviendrait à laisser une porte ouverte à ce qu'on corrige.
   */
  function blocReponse(reponse, sansClause, replier) {
    var r = reponse || {};
    var a = r.answer || {};
    var verdict = a.verdict || null;
    var v = libelleVerdict(verdict && verdict.value, sansClause);
    var phrases = phrasesModele(a);
    var factuels = phrases.factuels.map(function (f) { return f.texte; });
    // Sans une seule phrase factuelle, le bloc dit le constat et le verdict — jamais une chaîne
    // de service à la place d'une réponse. Ce que le modèle a écrit sur les limites de sa lecture
    // reste dessous, en explication : le constat le résume, il ne le remplace pas. Les phrases
    // `limite` viennent après les factuelles, donc le rang du titre vaut sur la liste entière.
    var corps = factuels.length
      ? reponseVue(factuels.concat(phrases.limites),
                   indexPhraseReponse(phrases.factuels, citationsParClaim(r)))
      : reponseVue([AUCUNE_PHRASE_RETENUE].concat(phrases.limites), 0);
    var phrase = corps.length ? corps[0] : null;
    var badge = noeud("span", "badge verdict-" + v.cle, v.texte);
    // La pastille se lit **à côté** de la première phrase : le verdict et la phrase qui l'énonce
    // sont une seule information, et les séparer par trois paragraphes en faisait deux.
    var enfants = [noeud("div", "reponse-tete", null, phrase ? [phrase, badge] : [badge])];
    var maj = majParReponses(r.conversation);
    if (maj) enfants.push(maj);
    enfants = enfants.concat(corps.slice(1));

    var raisonBrute = verdict ? String(verdict.reason || "").trim() : "";
    var raison = enClair(raisonBrute);
    // Rangée dans « Comment cette réponse a été obtenue » dès qu'il y a une première phrase — ou,
    // si elle est technique, **toujours** : elle documente encore la décision, elle ne se lit
    // simplement plus comme la réponse. Faute de trace où la ranger, une raison en clair reste
    // sous la pastille ; une raison technique, elle, ne remonte jamais.
    var rangee = !!(raisonBrute && replier && (phrase || !raison) && replier(raisonBrute));
    if (raison && !rangee) {
      enfants.push(noeud("p", "verdict-raison", raison));
    }

    // « Ce que je ne sais pas » : une ligne sous la réponse. Le tour moteur le rend en une phrase ;
    // tant qu'il en rend plusieurs, la liste reste compacte plutôt que d'être tronquée. Elle ne
    // s'affiche que si le moteur l'a **écrite** : ni chaîne technique, ni ligne générique.
    var inconnus = tableau(a.unknown).map(enClair).filter(function (x) {
      return x && !RE_INCONNU_GENERIQUE.test(x);
    });
    if (inconnus.length === 1) {
      enfants.push(noeud("p", "inconnu-ligne", "Ce que je ne sais pas : " + String(inconnus[0])));
    } else if (inconnus.length > 1) {
      enfants.push(noeud("div", "inconnu", null, [
        noeud("span", "inconnu-tete", "Ce que je ne sais pas"),
        liste("inconnu-liste", inconnus)
      ]));
    }
    return { vue: bloc("bloc-reponse", "La réponse", enfants), inconnus: inconnus.length };
  }

  // --- bloc 2 : sur quoi je m'appuie --------------------------------------

  /** `block_id → titre`, tel que `trace.blocs` le résout. Repli du `chemin` du moteur. */
  function titresDeBlocs(trace) {
    var titres = Object.create(null);
    tableau(trace && trace.blocs).forEach(function (b) {
      if (estObjetPlat(b) && typeof b.block_id === "string" && typeof b.titre === "string") {
        titres[b.block_id] = b.titre;
      }
    });
    return titres;
  }

  /**
   * Les clauses à afficher, chacune avec l'affirmation qui la cite.
   *
   * Trois chemins, du plus sûr au plus pauvre, et jamais de rattachement deviné (D6) :
   *   1. `sources[i].claim_id` — le moteur le publie : chaque clause **dit** qui la cite ;
   *   2. l'appariement positionnel de `clausesParClaim()` — le contrat d'AD-11 seul ;
   *   3. la liste plate, avec le seul statut que `statutDeBloc()` peut fixer sans deviner.
   */
  /**
   * Story 5.6 (L2c) — la ligne en clair d'une clause est **la phrase du modèle**, ou rien.
   *
   * `claim.text` puis `claim.rattachement`, dans cet ordre : ce que le contrat écrit, puis le lien
   * avec les faits déclarés. Si `text` manque ou porte une chaîne technique, la clause se lit avec
   * son chemin et sa citation, sans phrase inventée — et son rattachement s'en va avec elle, parce
   * qu'un lien avec les faits sous une affirmation qu'on ne peut pas énoncer n'a rien à quoi se
   * rattacher. Un rattachement technique tombe seul, la clause et sa phrase restent.
   */
  function enClairEntree(entree) {
    var texte = enClair(entree.texte);
    entree.texte = texte;
    entree.rattachement = texte ? enClair(entree.rattachement) : "";
    return entree;
  }

  /**
   * Story 5.6 (L2d) — l'amorce se fond dans la carte de son item.
   *
   * Les entrées sont dans l'ordre de `sources[]`, donc une amorce précède **immédiatement** l'item
   * qu'elle annonce : la reconnaître ici suffit, il n'y a rien à réordonner. La carte de l'item
   * porte alors l'amorce en petite ligne au-dessus de son paragraphe, et une seule fois le reste —
   * une phrase en clair, un statut, un bouton « Voir la page » qui ouvre les **deux** blocs.
   *
   * Une amorce dont le texte en clair est vide n'est jamais fondue : elle disparaîtrait de l'écran
   * sans que rien ne la reprenne, et une citation qui s'évapore est pire qu'une carte de trop.
   * Une amorce déjà porteuse d'une amorce ne se fond pas non plus — la chaîne s'arrête à un cran.
   */
  function fondreAmorces(entrees, memeClaim) {
    var out = [];
    for (var i = 0; i < entrees.length; i++) {
      var e = entrees[i];
      var suivante = entrees[i + 1];
      if (suivante && !e.amorce && estAmorce(e.src, suivante.src, memeClaim === true)) {
        var texte = enClair(texteDeBloc(e.src));
        if (texte) {
          suivante.amorce = e.src;
          suivante.amorceTexte = texte;
          continue;
        }
      }
      out.push(e);
    }
    return out;
  }

  function appuisDe(reponse) {
    var r = reponse || {};
    var a = r.answer || {};
    var sources = tableau(r.sources);
    if (!sources.length) return { entrees: [], degrade: false, ambigus: 0 };

    var claims = tableau(a.claims);
    var parId = Object.create(null);
    claims.forEach(function (c) {
      if (c && typeof c.claim_id === "string") parId[c.claim_id] = c;
    });
    var tousIdentifies = sources.every(function (s) {
      return s && typeof s.claim_id === "string" && s.claim_id &&
        Object.prototype.hasOwnProperty.call(parId, s.claim_id);
    });
    if (tousIdentifies) {
      return { degrade: false, ambigus: 0, entrees: fondreAmorces(sources.map(function (s) {
        var c = parId[s.claim_id];
        return enClairEntree({ src: s, texte: c.text, status: c.status || null,
                               rattachement: c.rattachement });
      })) };
    }

    var appariees = clausesParClaim(a, sources);
    if (appariees) {
      var entrees = [];
      appariees.forEach(function (e) {
        // Fondues **par affirmation** : ici les sources ne portent pas de `claim_id`, c'est le
        // groupe qui atteste qu'une même claim les cite (D6, chemin 2).
        entrees = entrees.concat(fondreAmorces(e.clauses.map(function (s) {
          return enClairEntree({ src: s, texte: e.text, status: e.status || null,
                                 rattachement: e.rattachement });
        }), true));
      });
      return { entrees: entrees, degrade: false, ambigus: 0 };
    }

    var ambigus = 0;
    return { degrade: true, ambigus: sources.filter(function (s) {
      return statutAmbigu(a, s.block_id);
    }).length, entrees: sources.map(function (s) {
      return { src: s, texte: "", status: statutDeBloc(a, s.block_id) };
    }) };
  }

  /** Une clause dont la table a jugé qu'elle **ne s'applique pas** se lit en retrait. */
  function appuiEcarte(entree) {
    return !!(entree.status && entree.status.applicable === "non");
  }

  /** Le chemin d'une clause : `chemin` du moteur, sinon le titre du nœud lu dans `trace.blocs`. */
  function cheminDe(src, titres) {
    var chemin = tableau(src.chemin).map(function (t) { return String(t || "").trim(); })
      .filter(function (t) { return t; });
    if (chemin.length) return chemin.join(" › ");
    var titre = titres[src.block_id];
    return titre ? String(titre) : "";
  }

  /**
   * Le corps d'une clause : le paragraphe entier, citation surlignée.
   *
   * Sans `texte_bloc`, ou quand la citation ne s'y retrouve pas malgré la normalisation, la page
   * affiche **la quote seule** : elle ne surligne rien au jugé et n'invente aucun paragraphe.
   */
  function corpsClause(src, decisifs) {
    var quote = String(src.quote || "");
    var texteBloc = typeof src.texte_bloc === "string" ? src.texte_bloc : "";
    // La citation seule, avec le terme décisif de la conversation surligné quand il y en a un
    // (story 3.7) : c'est le repli tant que le moteur ne publie pas `texte_bloc`, et c'est aussi ce
    // que la page affiche si la citation reste introuvable dans son paragraphe.
    function seule() {
      return tableau(decisifs).length
        ? noeud("blockquote", "appui-texte", null, texteDecisif(quote, decisifs))
        : noeud("blockquote", "appui-texte", "« " + quote + " »");
    }
    if (!texteBloc) return { enfants: [seule()], tronque: null };
    var bornes = trouverPassage(texteBloc, quote);
    if (!bornes) {
      return { enfants: [seule(),
                         noeud("p", "appui-note",
                               "La citation n'a pas pu être localisée dans son paragraphe : " +
                               "elle est donnée seule, sans surlignage inventé.")],
               tronque: null };
    }
    var entier = noeud("blockquote", "appui-texte", null,
                       texteSurligne(texteBloc, bornes, "appui-mark"));
    if (texteBloc.length <= BLOC_LONG_CARACTERES) return { enfants: [entier], tronque: null };
    var extrait = extraitAutour(texteBloc, bornes);
    var court = texteBloc.slice(extrait.debut, extrait.fin);
    var courtes = { debut: bornes.debut - extrait.debut, fin: bornes.fin - extrait.debut };
    return {
      enfants: [noeud("blockquote", "appui-texte appui-extrait", null,
                      texteSurligne(court, courtes, "appui-mark")),
                noeud("button", "appui-plus", "Voir le paragraphe entier")],
      tronque: entier
    };
  }

  function appuiVue(entree, titres, contexte) {
    var src = entree.src || {};
    var tete = [];
    var chemin = cheminDe(src, titres);
    if (chemin) tete.push(noeud("span", "appui-chemin", chemin));
    if (typeof src.page === "number") tete.push(noeud("span", "appui-page", "page " + src.page));
    tete.push(noeud("span", "appui-kind", libelleKind(src.kind)));
    if (src.kind_confirmed === false) {
      // AD-6 : un `kind` non confirmé plafonne le verdict. Le taire donnerait au lecteur une
      // certitude que le pipeline n'a pas.
      tete.push(noeud("span", "appui-doute", "typage non confirmé"));
    }
    var enfants = [noeud("div", "appui-tete", null, tete)];
    // L'amorce, au-dessus du paragraphe qu'elle annonce : c'est la phrase du contrat qui ouvre
    // l'énumération, en petit et en gris, parce qu'elle introduit l'appui sans être l'appui.
    if (entree.amorceTexte) {
      enfants.push(noeud("p", "appui-amorce", entree.amorceTexte));
    }
    var corps = corpsClause(src, (contexte || {}).decisive);
    enfants = enfants.concat(corps.enfants);
    // Posé **masqué**, pas absent : le paragraphe entier est déjà dans le document, et le
    // bouton ne fait que le montrer — aucune recomposition, aucun aller-retour serveur.
    if (corps.tronque) {
      enfants.push(noeud("div", "appui-entier", null, [corps.tronque], { "hidden": "hidden" }));
    }
    if (entree.texte) enfants.push(noeud("p", "appui-clair", entree.texte));
    // Story 5.6 (L1c) — le **rattachement aux faits**, sur sa propre ligne, sous ce que la clause
    // dit. Deux phrases, deux lignes, parce que ce sont deux choses : la première est ce que le
    // contrat écrit et que la citation au-dessus soutient ; la seconde dit que ce qui est arrivé
    // au déclarant est ce que cette clause nomme. Les fondre en un paragraphe laisserait croire
    // que la citation prouve aussi la seconde — c'est précisément ce que le moteur a cessé de
    // faire. Absente, il n'y a rien à afficher et la clause se lit comme avant.
    if (entree.rattachement) {
      enfants.push(noeud("p", "appui-rattachement", entree.rattachement));
    }
    var statut = statutTexte(entree.status);
    if (statut) enfants.push(noeud("p", "appui-statut", statut));

    var c = contexte || {};
    if (typeof c.doc_id === "string" && c.doc_id && typeof src.page === "number" &&
        isFinite(src.page) && Math.floor(src.page) === src.page && src.page > 0) {
      // Un seul bouton par carte, et il ouvre **tout** ce que la carte montre : l'amorce fondue
      // est sur la même page, immédiatement avant l'item, et son surlignage part avec le sien.
      var blocs = entree.amorce ? [String(entree.amorce.block_id)] : [];
      var lignes = entree.amorce ? tableau(entree.amorce.line_ids) : [];
      var attrs = {
        "data-doc-id": c.doc_id,
        "data-page": String(src.page),
        "data-block-ids": JSON.stringify(blocs.concat([String(src.block_id)])),
        "data-line-ids": JSON.stringify(lignes.concat(tableau(src.line_ids)))
      };
      var source = lienHttp(c.source_url);
      if (source) attrs["data-source-url"] = source;
      enfants.push(noeud("button", "cl-ouvrir", "Voir la page " + src.page + " dans le PDF",
                         null, attrs));
    }
    return noeud("div", "appui" + classeRole(src.kind) +
                 (appuiEcarte(entree) ? " appui-ecarte" : ""), null, enfants);
  }

  function blocAppuis(reponse, appuis, contexte) {
    if (!appuis.entrees.length) return null;
    var titres = titresDeBlocs((reponse || {}).trace);
    var corps = [];
    if (appuis.degrade) {
      corps.push(noeud("p", "degrade",
        "Les clauses ci-dessous fondent ce verdict, mais je n'ai pas pu rattacher chacune à " +
        "l'affirmation exacte qu'elle soutient : elles sont données ensemble."));
    }
    // Les clauses qui **s'appliquent** d'abord, celles que la table a écartées ensuite et en
    // retrait : l'ordre de lecture suit la décision, pas l'ordre de publication.
    var retenues = appuis.entrees.filter(function (e) { return !appuiEcarte(e); });
    var ecartees = appuis.entrees.filter(appuiEcarte);
    retenues.concat(ecartees).forEach(function (e) {
      corps.push(appuiVue(e, titres, contexte));
    });
    if (appuis.ambigus) {
      corps.push(noeud("p", "degrade",
        (appuis.ambigus > 1
          ? "Le statut de " + appuis.ambigus + " de ces clauses n'est pas affiché : plusieurs "
          : "Le statut de l'une de ces clauses n'est pas affiché : plusieurs ") +
        "affirmations la citent en n'en disant pas la même chose, et je ne devine pas " +
        "laquelle s'applique ici."));
    }
    corps.push(noeud("p", "appui-aide",
                     "Cliquer une clause ouvre la page du contrat, passage surligné."));
    return bloc("bloc-appuis", "Sur quoi je m'appuie", corps);
  }

  // --- bloc 3 : ce qu'il me manque ----------------------------------------

  function blocManques(reponse) {
    var r = reponse || {};
    var a = r.answer || {};
    var verdict = a.verdict || null;
    var enfants = [];

    if (a.clarification) {
      enfants.push(section("clarif", "Une précision, pour chercher au bon endroit",
                           [noeud("p", "clarif-q", String(a.clarification))]));
    }

    var questions = questionsVue(r.conversation);
    if (questions) {
      enfants.push(questions);
    } else {
      var demandes = tableau(verdict && verdict.ask_client)
        .filter(function (q) { return String(q || "").trim(); });
      if (demandes.length) {
        // Sans conversation ouverte, rien n'est cliquable : les questions du moteur se lisent
        // telles qu'il les écrit, sous le même intitulé, et l'écran ne promet aucun recalcul.
        enfants.push(section("ask", TITRE_QUESTIONS, [liste("ask-liste", demandes)]));
      }
    }

    var faits = tableau(verdict && verdict.missing && verdict.missing.faits)
      .filter(function (f) { return String(f || "").trim(); });
    if (faits.length) {
      enfants.push(section("paquet", "Ce que les clauses exigent et que la description n'établit pas",
                           [liste("paquet-faits", faits)]));
    }

    var escalade = tableau(verdict && verdict.escalate)
      .filter(function (q) { return String(q || "").trim(); });
    if (escalade.length) {
      enfants.push(section("escalate", "Points à faire trancher par un humain",
                           [liste("escalate-liste", escalade)]));
    }

    // AD-6 : `MissingPackage` accompagne **toujours** le verdict, y compris sous un « couvert ».
    // C'est la mesure de ce que le verdict ne pouvait pas voir — une ligne, pas un formulaire.
    var absentes = verdict && verdict.missing
      ? PIECES.filter(function (p) { return verdict.missing[p.cle] !== false; })
          .map(function (p) { return p.libelle; })
      : [];
    if (absentes.length) {
      enfants.push(noeud("p", "pieces-ligne", "Pièces non lues : " + absentes.join(", ") + "."));
    }
    return bloc("bloc-manques", "Ce qu'il me manque pour aller plus loin", enfants);
  }

  // --- bloc 4 : garde-fous -------------------------------------------------

  /**
   * Le nombre de phrases retirées de la réponse, ou `null` si rien ne le chiffre.
   *
   * *restituer* pose un `CheckResult` nommé `segments_retires` quand il retire des phrases dont
   * plus aucune affirmation ne survit, et son `detail` commence par le compte. C'est aujourd'hui le
   * **seul** porteur de ce nombre sur le fil : `answer.unknown` n'en porte que la phrase. Absence de
   * contrôle ⇒ aucune phrase retirée, donc zéro ; contrôle présent mais détail non chiffré ⇒ `null`,
   * et la page dit la chose sans le nombre plutôt que d'en inventer un. Une reprise différée demande
   * au moteur de publier ce compteur typé.
   */
  function phrasesRetirees(trace) {
    var vu = false;
    var n = null;
    tableau(trace && trace.steps).filter(estObjetPlat).forEach(function (s) {
      tableau(s.checks).forEach(function (c) {
        if (!estObjetPlat(c) || c.name !== "segments_retires") return;
        vu = true;
        var m = String(c.detail || "").match(/^\s*(\d+)\b/);
        if (m) n = (n === null ? 0 : n) + parseInt(m[1], 10);
      });
    });
    if (!vu) return 0;
    return n;
  }

  /**
   * Bloc 4 — les garde-fous, et **tout** ce qui documente la réponse.
   *
   * Story 5.6 (L2b) : la pastille d'état (« partiel », « inconnu ») et le panneau « Comment cette
   * réponse a été obtenue » flottaient après les quatre blocs, séparés d'eux par un filet — deux
   * objets orphelins en bas d'écran, dont rien ne disait à quoi ils se rapportaient. Ils sont ici,
   * dans le bloc qui parle déjà de ce que la vérification a établi. `contexte.trace` est la vue de
   * la trace, construite avant ce bloc parce que la raison du verdict s'y replie.
   */
  function blocGardeFous(reponse, contexte) {
    var r = reponse || {};
    var a = r.answer || {};
    var sources = tableau(r.sources);
    var enfants = [];

    enfants.push(noeud("p", "gf gf-ok",
      pluriel(sources.length, "citation") + " relue" + (sources.length > 1 ? "s" : "") +
      " mot pour mot dans le contrat"));

    var retirees = phrasesRetirees(r.trace);
    enfants.push(noeud("p", "gf gf-retire", retirees === null
      ? "Des phrases ont été retirées : aucun passage ne les soutenait"
      : pluriel(retirees, "phrase") + " retirée" + (retirees > 1 ? "s" : "") +
        (retirees ? (retirees > 1 ? " : aucun passage ne les soutenait"
                                  : " : aucun passage ne la soutenait") : "")));

    // La portée ne se dit qu'**ici**, une fois. Elle était écrite deux fois sur le même écran —
    // sous la pastille et dans cette ligne, en deux formulations différentes de la même réserve :
    // « trois formulations d'une même promesse font trois promesses » (AD-15). Le libellé retenu
    // est celui du domaine (`PORTEE`), pas une paraphrase.
    enfants.push(noeud("p", "gf gf-regle", "Verdict calculé par des règles fixes, pas par le modèle",
                       [noeud("span", "portee", PORTEE)]));

    // Story 5.6 (L1i) : les avis de service — ce que le contrôle a fait de l'ébauche, une relance
    // qui n'a pas eu lieu, une lecture bornée. Ils se lisent **ici**, avec les garde-fous, et non
    // sous « Ce que je ne sais pas » : ce ne sont pas des trous dans ce qui a été demandé, et les y
    // ranger faisait badger « partiel » une réponse qui traitait toutes ses sous-questions.
    var avis = tableau(a.avis).map(enClair).filter(function (x) { return x; });
    for (var v = 0; v < avis.length; v++) {
      enfants.push(noeud("p", "gf gf-avis", String(avis[v])));
    }

    // M15 : la preuve chiffrée d'une absence, et son pendant sous lecture bornée (story 4.2f).
    var preuve = preuveAbsence(a.reason);
    if (preuve) enfants.push(noeud("p", "preuve", preuve));
    var lecture = estObjet(a.lecture_partielle) ? lectureLue(a.lecture_partielle) : "";
    if (lecture) enfants.push(noeud("p", "lecture-partielle", lecture));

    // FR5 : le cadre des quatre états — « sûr », « partiel », « lecture partielle », « inconnu ».
    if (a.found === true || a.reason || estObjet(a.lecture_partielle)) {
      var etat = etatReponse(a);
      enfants.push(noeud("div", "pied", null, [
        noeud("span", "etat etat-" + etat.cle, etat.texte),
        noeud("span", "etat-phrase",
              phraseEtat(etat, { liste: (contexte || {}).inconnus > 0, preuve: !!preuve,
                                 lecture: !!lecture }))
      ]));
    }

    // D7 : les affirmations écartées restent consultables — **derrière** ce bloc, dépliables, et
    // plus jamais au-dessus de la réponse. Leur citation n'est pas montrée : la quote d'une claim
    // rejetée sur ses citations est restée la chaîne du modèle, et rien ne prouve qu'elle existe.
    var rejetees = tableau(a.rejected_claims).filter(function (c) { return c; });
    if (rejetees.length) {
      enfants.push(sectionRepliee("rejetees", "Affirmations écartées par la vérification", [
        noeud("p", "rejetees-note",
          "Le modèle a avancé ces affirmations ; les contrôles les ont écartées. Aucune de leurs " +
          "citations n'est affichée : le motif de chacune est donné en dessous."),
        noeud("div", "rejetees-liste", null, rejetees.map(function (c) {
          return noeud("div", "rejetee", null, [
            noeud("p", "rej-txt", String(c.text || "")),
            noeud("p", "rej-motif", motifRejet(c.rejection_kind)),
            noeud("p", "rej-kind", String(c.rejection_kind || ""))
          ]);
        }))
      ]));
    }
    // Le panneau de la trace ferme le bloc : ce qui documente vient après ce qui décide, mais il
    // reste **dans** le bloc qui l'introduit.
    if ((contexte || {}).trace) enfants.push(contexte.trace);
    return bloc("bloc-gardefous", "Garde-fous", enfants);
  }

  // --- le dossier, après les quatre blocs ---------------------------------

  function dossierVue(reponse) {
    var r = reponse || {};
    var a = r.answer || {};
    var enfants = [];
    var compris = faitsComprisVue(a.faits_compris);
    if (compris) enfants.push(compris);
    if (String(a.texte || "").trim()) {
      enfants.push(section("analyse", "Ce que disent les clauses retenues", [
        noeud("p", "analyse-txt", String(a.texte))
      ]));
    }
    var conversation = conversationVue(r.conversation, { sansQuestions: true });
    if (conversation) enfants.push(conversation);
    if (!enfants.length) return null;
    return sectionRepliee("dossier", "Dossier", enfants);
  }

  function vueVerdict(reponse, contexte) {
    var r = reponse || {};
    var a = r.answer || {};
    var enfants = [];
    var contexteClauses = Object.assign({}, contexte || {});
    // Les termes que la conversation a rendus décisifs (story 3.7) : ils restent surlignés dans la
    // citation seule, là où le paragraphe entier ne porte pas déjà son propre surlignage.
    contexteClauses.decisive = tableau(r.conversation && r.conversation.history)
      .reduce(function (out, h) {
        tableau(h && h.decisive_terms).forEach(function (term) { out.push(term); });
        return out;
      }, []);

    var appuis = appuisDe(r);
    // La trace est construite **avant** le bloc 1 : c'est elle qui accueille la raison du verdict
    // quand la première phrase la rend inutile à l'écran, et sans elle il n'y a nulle part où la
    // replier — le bloc 1 la garde alors. `replierRaison` rend `true` seulement si la raison a
    // réellement été rangée quelque part.
    var trace = traceVue(r.trace);
    function replierRaison(raison) {
      if (!trace) return false;
      var rubrique = rubriqueTrace("Ce qui a décidé le verdict", [ligneTrace(raison)]);
      if (!rubrique) return false;
      // Juste après le `<summary>` : c'est la première chose qu'on cherche en ouvrant le panneau.
      trace.enfants.splice(1, 0, rubrique);
      return true;
    }

    // « Pas de clause qui s'applique » se dit quand **aucune affirmation n'a été retenue**, pas
    // quand la liste des clauses est vide : une claim retenue reste une claim retenue.
    var reponseEtInconnus = blocReponse(r, tableau(a.claims).length === 0, replierRaison);
    enfants.push(reponseEtInconnus.vue);
    var appuisVue = blocAppuis(r, appuis, contexteClauses);
    if (appuisVue) enfants.push(appuisVue);
    var manques = blocManques(r);
    if (manques) enfants.push(manques);
    var gardeFous = blocGardeFous(r, { inconnus: reponseEtInconnus.inconnus, trace: trace });
    if (gardeFous) enfants.push(gardeFous);
    // Un corps sans garde-fous n'existe pas en pratique (la ligne des citations relues est
    // toujours composée) ; si le bloc 4 venait à manquer, la trace ne serait pour autant pas
    // perdue — elle reprend sa place au niveau du résultat.
    else if (trace) enfants.push(trace);

    var dossier = dossierVue(r);
    if (dossier) enfants.push(dossier);

    return noeud("div", "carte resultat", null, enfants.filter(Boolean));
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
    // Story 5.6 (L1c). Facultatif — une clause qui ne nomme aucun fait déclaré n'en porte pas —,
    // mais **typé** dès qu'il est là : la page l'affiche sous ce que la clause dit.
    if (c.rattachement !== undefined) {
      exiger(ouNul(estChaine)(c.rattachement), champ + ".rattachement");
    }
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
    // Story 5.6 (L2) : les trois champs que le tour moteur ajoute — `texte_bloc` (le paragraphe
    // entier d'où la citation est extraite), `chemin` (les titres du plus général au plus précis)
    // et `claim_id` (l'affirmation qui cite ce bloc, sans passer par l'appariement positionnel de
    // D6). Ils sont lus **quand ils sont là** : tant que le moteur ne les publie pas, la page
    // affiche la quote seule et le titre du nœud lu dans `trace.blocs`. Absents, ils ne rendent
    // donc rien illisible ; présents, ils sont typés comme le reste, parce qu'un `chemin` d'objets
    // peindrait « [object Object] › [object Object] » au-dessus d'une clause.
    if (s.texte_bloc !== undefined) exiger(ouNul(estChaine)(s.texte_bloc), champ + ".texte_bloc");
    if (s.chemin !== undefined) exigerListe(s.chemin, estChaine, champ + ".chemin");
    if (s.claim_id !== undefined) exiger(ouNul(estChaine)(s.claim_id), champ + ".claim_id");
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

  // `LecturePartielle` (story 4.2f), lue avec la **même** exigence que `AbsenceProof`. Ses deux
  // compteurs n'ont pas de valeur par défaut côté serveur et ce sont eux que l'écran affiche : un
  // compteur absent, négatif ou non entier peindrait « 0 section lue » sur une lecture que rien n'a
  // mesurée — le contraire de ce que ce porteur promet, puisqu'il n'existe que pour la chiffrer.
  function lireLecturePartielle(lecture, champ) {
    if (lecture === undefined || lecture === null) return;
    exiger(estObjet(lecture), champ);
    // Plancher à 1 sur les **deux** compteurs, comme le domaine : zéro passage transmis est
    // l'erreur terminale d'AD-1/NFR2, et zéro section pour au moins un passage est un état
    // impossible (AD-2 rattache chaque bloc à exactement un nœud). Les accepter affichait au
    // gestionnaire deux chiffres qui se contredisent.
    exiger(estCompteur(lecture.nodes_read) && lecture.nodes_read >= 1, champ + ".nodes_read");
    exiger(estCompteur(lecture.blocks_read) && lecture.blocks_read >= 1, champ + ".blocks_read");
    exigerListe(lecture.documents, estChaine, champ + ".documents");
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
    // Story 5.6 (L1i) : le canal des avis de service — comment cette réponse a été composée. Il est
    // additif, donc lu comme tel : absent, c'est un corps d'avant ce champ ; présent, il est une
    // liste de chaînes comme `unknown`.
    if (o.answer.avis !== undefined) exigerListe(o.answer.avis, estChaine, "answer.avis");
    exiger(ouNul(estChaine)(o.answer.clarification), "answer.clarification");
    lireReason(o.answer.reason, "answer.reason");
    lireLecturePartielle(o.answer.lecture_partielle, "answer.lecture_partielle");
    // Story 4.2f : « exactement un porteur sur `found=false` », l'invariant du domaine refait ici.
    // Sans porteur, la page peindrait un badge « inconnu » sur rien ; avec les deux, elle
    // afficherait à la fois « le contrat n'en dit rien » et « je n'ai pas fini de le lire », dont
    // l'un ment. Et un `found=true` qui porterait une lecture partielle annoncerait deux états.
    var porteurs = (estObjet(o.answer.reason) ? 1 : 0) +
                   (estObjet(o.answer.lecture_partielle) ? 1 : 0);
    exiger(porteurs <= 1, "answer.lecture_partielle");
    exiger(o.answer.found === true || porteurs === 1, "answer.reason");
    // Sur `found=true`, **aucun** des deux : une clause retenue n'est ni une absence prouvée ni une
    // lecture restée sans conclusion. Ne fermer que le second laissait afficher un verdict fondé
    // sur des clauses **et** la preuve chiffrée qu'aucune n'existe.
    exiger(o.answer.found !== true || !estObjet(o.answer.lecture_partielle),
           "answer.lecture_partielle");
    exiger(o.answer.found !== true || !estObjet(o.answer.reason), "answer.reason");
    // « Une lecture partielle dit ce qui lui manque » : aucune route ne peut servir ce corps, et le
    // peindre donnerait un compteur de lecture sans la moindre réserve en face. Story 5.6 (L1i) :
    // la borne d'une lecture est un avis de service (`lecture_bornee`), donc la réserve peut se dire
    // dans l'un ou l'autre canal — c'est le silence complet qui reste refusé.
    exiger(!estObjet(o.answer.lecture_partielle)
           || o.answer.unknown.length > 0
           || (Array.isArray(o.answer.avis) && o.answer.avis.length > 0), "answer.unknown");
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

  function lireConversation(c) {
    exiger(estObjet(c), "conversation");
    exiger(estChaine(c.token), "conversation.token");
    exiger(estCompteur(c.turn), "conversation.turn");
    ["facts", "conflicts", "questions", "history"].forEach(function (nom) {
      exiger(Array.isArray(c[nom]), "conversation." + nom);
    });
    c.facts.forEach(function (f, i) {
      var champ = "conversation.facts[" + i + "]";
      exiger(estObjet(f), champ); exiger(estChaine(f.event_id), champ + ".event_id");
      exiger(estChaine(f.key), champ + ".key"); exiger(estChaine(f.value), champ + ".value");
      exiger(estChaine(f.source), champ + ".source"); exiger(estCompteur(f.turn), champ + ".turn");
      exiger(ouNul(estChaine)(f.question_id), champ + ".question_id");
      exiger(ouNul(estChaine)(f.replaces_event_id), champ + ".replaces_event_id");
    });
    c.conflicts.forEach(function (conflit, i) {
      var champ = "conversation.conflicts[" + i + "]";
      exiger(estObjet(conflit), champ); exiger(estChaine(conflit.conflict_id), champ + ".conflict_id");
      exiger(estChaine(conflit.key), champ + ".key");
      exigerListe(conflit.event_ids, estChaine, champ + ".event_ids");
      exiger(estChaine(conflit.status), champ + ".status");
      exiger(ouNul(estChaine)(conflit.chosen_event_id), champ + ".chosen_event_id");
    });
    c.questions.forEach(function (q, i) {
      var champ = "conversation.questions[" + i + "]";
      exiger(estObjet(q), champ); exiger(estChaine(q.question_id), champ + ".question_id");
      exiger(estChaine(q.text), champ + ".text"); exiger(estChaine(q.kind), champ + ".kind");
      exiger(estChaine(q.fact_key), champ + ".fact_key"); exiger(estChaine(q.status), champ + ".status");
      exiger(ouNul(estChaine)(q.claim_id), champ + ".claim_id");
      exiger(ouNul(estChaine)(q.expected_value), champ + ".expected_value");
      exiger(ouNul(estChaine)(q.answered_event_id), champ + ".answered_event_id");
    });
    c.history.forEach(function (h, i) {
      var champ = "conversation.history[" + i + "]";
      exiger(estObjet(h), champ); exiger(estCompteur(h.turn), champ + ".turn");
      exiger(estChaine(h.value), champ + ".value"); exiger(estChaine(h.reason), champ + ".reason");
      exiger(estBooleen(h.changed), champ + ".changed");
      exigerListe(h.causal_event_ids, estChaine, champ + ".causal_event_ids");
      if (h.causal_events !== undefined) {
        exigerListe(h.causal_events, estChaine, champ + ".causal_events");
      }
      if (h.decisive_terms !== undefined) {
        exigerListe(h.decisive_terms, estChaine, champ + ".decisive_terms");
      }
      exiger(estChaine(h.request_id), champ + ".request_id");
    });
    return c;
  }

  function lireReponseConversation(j) {
    var response = lireReponse(j);
    // Tolérance de déploiement progressif : l'ancien serveur reste un one-shot honnête. Il n'est
    // jamais transformé en conversation locale ; aucune question active n'est fabriquée côté page.
    response.conversation = j && j.conversation ? lireConversation(j.conversation) : null;
    return response;
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

  function soumettreConversation(saisie) {
    return requete("/api/v1/sinistre", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Sinistre-Conversation": "1" },
      body: JSON.stringify(corpsSinistre(saisie))
    }).then(lireReponseConversation);
  }

  // ---------- le flux de progression (story 5.6, L2) ----------
  //
  // `POST /api/v1/sinistre/progression` rend le **même** corps que la route classique, précédé des
  // étapes que le serveur exécute, en SSE. La page le consomme quand il existe, et retombe sur la
  // route classique quand il n'existe pas — ce qui est le cas tant que le tour moteur n'est pas
  // livré, et le restera pour tout serveur plus ancien.
  //
  // **Un seul appel facturé par soumission.** La bascule vers la route classique n'a lieu que dans
  // deux situations, et elle ne peut avoir lieu qu'une fois :
  //   - la route de progression répond 404 ou 405 : elle n'existe pas, rien n'a tourné, rien n'a
  //     coûté — l'appel classique qui suit est le premier et le seul ;
  //   - le flux se coupe **avant** d'avoir livré son `resultat`.
  // Dès qu'un `resultat` est arrivé, plus aucune requête n'est envoyée, quoi qu'il arrive ensuite au
  // flux. Toute autre réponse (503, 429, 400…) est une erreur d'AD-16 : elle remonte telle quelle,
  // sans repli — « aucun repli pour le sinistre ».

  function decoupeurSSE() {
    var reste = "";
    return {
      /** Rend les événements `{type, data}` complets contenus dans ce fragment. */
      pousser: function (fragment) {
        reste += String(fragment || "");
        var out = [];
        // Les trois fins de bloc que la spécification SSE admet.
        var blocs = reste.split(/\r\n\r\n|\n\n|\r\r/);
        reste = blocs.pop();
        blocs.forEach(function (bloc) {
          var type = "";
          var donnees = [];
          bloc.split(/\r\n|\n|\r/).forEach(function (l) {
            if (l.charAt(0) === ":") return;  // commentaire de maintien en vie
            var i = l.indexOf(":");
            var champ = i < 0 ? l : l.slice(0, i);
            var valeur = i < 0 ? "" : l.slice(i + 1).replace(/^ /, "");
            if (champ === "event") type = valeur;
            else if (champ === "data") donnees.push(valeur);
          });
          if (!donnees.length) return;
          var brut = donnees.join("\n");
          var charge = null;
          try { charge = JSON.parse(brut); } catch (_) { charge = null; }
          // Le nom de l'événement peut voyager en champ `event:` ou dans le corps : les deux formes
          // sont lues, aucune n'est devinée quand ni l'une ni l'autre n'est là.
          if (!type && charge && typeof charge.type === "string") type = charge.type;
          out.push({ type: type, data: charge });
        });
        return out;
      }
    };
  }

  /**
   * Le corps d'une réponse SSE peut-il être lu **au fil de l'eau** ?
   *
   * Sinon il reste lisible d'un bloc (`r.text()`) : on perd l'avancement en direct, pas la réponse.
   * C'est la distinction qui compte pour le portefeuille — un corps qu'on ne lit pas est un appel
   * payé pour rien, et le renvoyer sur la route classique le paierait deux fois.
   */
  function fluxLisible(reponse) {
    return !!(reponse && reponse.body && typeof reponse.body.getReader === "function" &&
              typeof TextDecoder === "function");
  }

  // La route de progression n'existe pas sur tous les serveurs. Un 404 une fois vaut pour la page :
  // la sonder à chaque soumission ajouterait un aller-retour inutile à chaque soumission.
  var progressionAbsente = false;

  /**
   * Soumet via le flux de progression, en appelant `sur.etape` à chaque étape annoncée.
   *
   * Rend la réponse validée, ou rejette avec `{bascule: true}` pour dire à l'appelant — et à lui
   * seul — que la route classique doit prendre le relais.
   */
  function soumettreProgression(saisie, sur) {
    if (!enLigne()) {
      return Promise.reject(erreurSinistre({ kind: "requete", code: "hors_ligne", statut: 0 }));
    }
    if (progressionAbsente) {
      var absente = new Error("bascule");
      absente.bascule = true;
      return Promise.reject(absente);
    }
    var ctrl = (typeof AbortController === "function") ? new AbortController() : null;
    var options = {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Sinistre-Conversation": "1",
                 "Accept": "text/event-stream" },
      body: JSON.stringify(corpsSinistre(saisie))
    };
    if (ctrl) options.signal = ctrl.signal;
    var minuteur = ctrl ? setTimeout(function () { ctrl.abort(); }, abandonMs()) : null;
    function finir() { if (minuteur !== null) clearTimeout(minuteur); minuteur = null; }
    function bascule() { var e = new Error("bascule"); e.bascule = true; return e; }

    return fetch(API_BASE + "/api/v1/sinistre/progression", options).then(function (r) {
      // 404/405 : la route n'existe pas sur ce serveur. Rien n'a tourné, rien n'a coûté.
      if (r.status === 404 || r.status === 405) {
        progressionAbsente = true;
        finir();
        throw bascule();
      }
      if (!r.ok) {
        return r.json().then(function (j) { return j; }, function () { return null; })
          .then(function (j) { finir(); throw erreurHttp(r.status, r.headers, j); });
      }
      var decoupeur = decoupeurSSE();
      var resultat = null;
      var erreurServeur = null;

      function traiter(evt) {
        if (evt.type === "etape") {
          if (typeof sur === "function") sur(evt.data || {});
          return;
        }
        if (evt.type === "resultat" && evt.data) { resultat = evt.data; return; }
        if (evt.type === "erreur" && evt.data) { erreurServeur = evt.data; }
      }

      // Le serveur a répondu : son corps est **payé**. On le lit — au fil de l'eau si le navigateur
      // sait le faire, d'un bloc sinon. Le jeter pour redemander la même chose à la route classique
      // paierait deux fois le même travail.
      function lire() {
        if (fluxLisible(r)) {
          var lecteur = r.body.getReader();
          var decodeur = new TextDecoder("utf-8");
          return (function boucle() {
            return lecteur.read().then(function (pas) {
              if (pas && pas.value) {
                decoupeur.pousser(decodeur.decode(pas.value, { stream: true })).forEach(traiter);
              }
              if (pas && pas.done) {
                decoupeur.pousser(decodeur.decode()).forEach(traiter);
                return null;
              }
              return boucle();
            });
          })();
        }
        if (typeof r.text === "function") {
          return r.text().then(function (t) { decoupeur.pousser(t).forEach(traiter); return null; });
        }
        // Ni flux ni corps : l'environnement ne permet pas de lire ce que le serveur a envoyé. Ce
        // n'est pas une route absente — redemander ferait payer deux fois.
        throw erreurSinistre({ kind: "requete", code: "reponse_illisible", statut: r.status });
      }

      // `Promise.resolve().then(lire)` et non `lire()` : un `lire()` qui lève **avant** de
      // rendre sa promesse court-circuiterait le gestionnaire d'échec ci-dessous, donc
      // `finir()` — la minuterie d'abandon resterait armée jusqu'à sa borne, plusieurs
      // minutes après que la page a fini d'attendre.
      return Promise.resolve().then(lire).then(function () {
        finir();
        // Un `resultat` reçu clôt la soumission : plus jamais de seconde requête, même si le flux
        // s'est ensuite coupé. C'est **la** garde contre le double appel payant.
        if (resultat) return lireReponseConversation(resultat);
        if (erreurServeur) {
          var err = erreurServeur.error || erreurServeur;
          throw erreurSinistre({
            kind: err && err.kind === "indisponible" ? "indisponible" : "requete",
            code: typeof (err && err.code) === "string" ? err.code : "",
            statut: 200,
            request_id: typeof (err && err.request_id) === "string" ? err.request_id : ""
          });
        }
        // Flux coupé avant le résultat : le serveur n'a rien livré, la route classique reprend.
        throw bascule();
      }, function (e) {
        finir();
        if (resultat) return lireReponseConversation(resultat);
        if (e && e.nom === "ErreurSinistre") throw e;
        throw bascule();
      });
    }, function () {
      finir();
      // Aucune réponse du tout : c'est le réseau, pas une route absente. Une bascule enverrait une
      // seconde requête vers un serveur injoignable, sans rien y gagner.
      throw erreurSinistre({
        kind: "indisponible",
        code: (ctrl && ctrl.signal.aborted) ? "timeout_client" : "reseau",
        statut: 0
      });
    });
  }

  /**
   * La soumission complète : le flux d'abord, la route classique en repli — **une seule fois**.
   */
  function soumettreAvecProgression(saisie, sur) {
    return soumettreProgression(saisie, sur).catch(function (e) {
      if (!(e && e.bascule)) throw e;
      return soumettreConversation(saisie);
    });
  }

  function suivre(conversation, docId, action) {
    var corps = Object.assign({ doc_id: String(docId || ""), token: conversation.token }, action || {});
    return requete("/api/v1/sinistre/suivi", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(corps)
    }).then(lireReponseConversation);
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

  /**
   * Les deux gestes du bloc 2 : déplier le paragraphe entier, et ouvrir la page du contrat.
   *
   * Le dépliage est **irréversible** dans la vue courante — le bouton disparaît avec l'extrait.
   * Un contenu qui se referme sous le lecteur est exactement ce que cette refonte retire ; et le
   * paragraphe entier est de toute façon reconstruit à chaque nouvelle réponse.
   *
   * La carte entière est cliquable, comme la maquette le montre, mais le bouton « Voir la page … »
   * reste posé : c'est lui qui rend le geste atteignable au clavier et annonçable par un lecteur
   * d'écran. Le clic sur la carte est donc ignoré dès qu'il vient d'une commande.
   */
  function brancherAppuis(racine) {
    if (!racine || typeof racine.querySelectorAll !== "function") return;
    racine.querySelectorAll(".appui-plus").forEach(function (bouton) {
      bouton.addEventListener("click", function () {
        var carte = bouton.closest ? bouton.closest(".appui") : null;
        if (!carte) return;
        var entier = carte.querySelector(".appui-entier");
        var extrait = carte.querySelector(".appui-extrait");
        if (entier) entier.removeAttribute("hidden");
        if (extrait) extrait.setAttribute("hidden", "hidden");
        bouton.setAttribute("hidden", "hidden");
      });
    });
    racine.querySelectorAll(".appui").forEach(function (carte) {
      var ouvrir = carte.querySelector(".cl-ouvrir");
      if (!ouvrir) return;
      carte.addEventListener("click", function (ev) {
        var cible = ev && ev.target;
        // Un clic sur une commande de la carte (le bouton lui-même, « Voir le paragraphe entier »)
        // ne doit pas ouvrir le lecteur une seconde fois.
        if (cible && typeof cible.closest === "function" && cible.closest("button")) return;
        if (typeof ouvrir.click === "function") ouvrir.click();
      });
    });
  }

  function copierDossier(reponse) {
    var texte = dossierTexte(reponse);
    var pressePapiers = window.navigator && window.navigator.clipboard;
    if (!pressePapiers || typeof pressePapiers.writeText !== "function") {
      return Promise.reject(new Error("presse_papiers_indisponible"));
    }
    return pressePapiers.writeText(texte).then(function () { return texte; });
  }

  var suiviGeneration = 0;
  var suiviEnCours = false;

  function verrouillerSuivi(racine, occupe) {
    if (!racine) return;
    ["conv-selection-question", "conv-repondre", "conv-envoyer-libre", "conv-reponse-libre",
     "conv-corriger", "conv-resoudre", "conv-copier"].forEach(function (classe) {
      racine.querySelectorAll("." + classe)
        .forEach(function (controle) { controle.disabled = !!occupe; });
    });
  }

  function brancherConversation(racine, reponse, contexte, onUpdate) {
    if (!racine || !reponse || !reponse.conversation) return;
    // Toute nouvelle vue invalide les promesses encore attachées à l'ancienne. Le jeton de la
    // réponse obsolète ne peut donc jamais remplacer le dernier état rendu.
    suiviGeneration++;
    suiviEnCours = false;
    var statut = racine.querySelector(".conv-statut");
    function annoncer(texte) { if (statut) statut.textContent = texte; }
    function questionSelectionnee() {
      var contexteQuestion = racine.querySelector(".conv-question-contexte");
      return contexteQuestion && contexteQuestion.getAttribute("data-selected-question-id");
    }
    function envoyer(action) {
      if (suiviEnCours) return Promise.resolve(null);
      suiviEnCours = true;
      var generation = ++suiviGeneration;
      verrouillerSuivi(racine, true);
      annoncer("Réponse en cours de vérification…");
      return suivre(reponse.conversation, contexte.doc_id, action).then(function (nouvelle) {
        if (generation !== suiviGeneration) return null;
        suiviEnCours = false;
        verrouillerSuivi(racine, false);
        if (typeof onUpdate === "function") onUpdate(nouvelle);
        return nouvelle;
      }).catch(function (erreur) {
        if (generation !== suiviGeneration) return null;
        suiviEnCours = false;
        verrouillerSuivi(racine, false);
        // Le dernier état valide reste intégralement à l'écran : seul ce statut change.
        annoncer("Le suivi a été refusé : " + messageErreur(erreur) +
                 " Le dernier verdict valide reste affiché.");
        return null;
      });
    }
    racine.querySelectorAll(".conv-selection-question").forEach(function (bouton) {
      bouton.addEventListener("click", function () {
        var questionId = bouton.getAttribute("data-question-id");
        var texteQuestion = bouton.getAttribute("data-question-text") || bouton.textContent;
        racine.querySelectorAll(".conv-selection-question").forEach(function (autre) {
          autre.setAttribute("aria-pressed", autre === bouton ? "true" : "false");
        });
        var cible = racine.querySelector(".conv-question-contexte");
        if (cible) {
          cible.textContent = texteQuestion;
          cible.setAttribute("data-selected-question-id", questionId);
        }
        var libre = racine.querySelector(".conv-reponse-libre");
        if (libre && typeof libre.focus === "function") libre.focus();
      });
    });
    racine.querySelectorAll(".conv-repondre").forEach(function (bouton) {
      bouton.addEventListener("click", function () {
        envoyer({ action: "reponse", question_id: questionSelectionnee(),
                  value: bouton.getAttribute("data-value") });
      });
    });
    racine.querySelectorAll(".conv-envoyer-libre").forEach(function (bouton) {
      bouton.addEventListener("click", function () {
        var qid = questionSelectionnee();
        var input = racine.querySelector(".conv-reponse-libre");
        var value = input && String(input.value || "").trim();
        if (!value) { annoncer("Écrivez une réponse avant de l'envoyer."); return; }
        envoyer({ action: "reponse", question_id: qid, value: value });
      });
    });
    racine.querySelectorAll(".conv-corriger").forEach(function (bouton) {
      bouton.addEventListener("click", function () {
        var value = window.prompt ? window.prompt("Nouvelle valeur de ce fait :") : null;
        if (value === null || !String(value).trim()) return;
        envoyer({ action: "correction", fact_key: bouton.getAttribute("data-fact-key"),
                  replaces_event_id: bouton.getAttribute("data-event-id"), value: String(value).trim() });
      });
    });
    racine.querySelectorAll(".conv-resoudre").forEach(function (bouton) {
      bouton.addEventListener("click", function () {
        envoyer({ action: "resolution", conflict_id: bouton.getAttribute("data-conflict-id"),
                  chosen_event_id: bouton.getAttribute("data-event-id") });
      });
    });
    var copier = racine.querySelector(".conv-copier");
    if (copier) copier.addEventListener("click", function () {
      copierDossier(reponse).then(function () {
        annoncer("Dossier copié dans le presse-papiers.");
      }).catch(function () {
        annoncer("La copie a échoué : le presse-papiers n'est pas accessible. Rien n'a été annoncé comme copié.");
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
      question: QUESTION_SINISTRE,
      date: "",
      lieu: "",
      montant_eur: "",
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
    ["contrat", "description", "analyser"]
      .forEach(function (id) { var e = $(id); if (e) e.disabled = !!occupe; });
    var hote = $("resultat");
    if (hote) hote.setAttribute("aria-busy", occupe ? "true" : "false");
  }

  /** Ce qui manque à la saisie pour partir, ou `null`. Le bouton n'est jamais muet (revue 1.9). */
  function manquant(saisie) {
    if (!String(saisie.doc_id || "").trim()) {
      return "Choisissez le contrat auquel confronter ce sinistre.";
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
    var bornes = [{ id: "description", max: DESCRIPTION_MAX }];
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
        suiviGeneration++;
        suiviEnCours = false;
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
        // La barre d'étapes remplace la phrase figée : elle se repeint chaque seconde et suit le
        // flux de progression quand le serveur en sert un. `attente.fin()` arrête son minuteur sur
        // **les deux** issues — sans quoi il repeindrait l'attente par-dessus le verdict.
        // `verrouiller(true)` **avant** la première peinture : c'est lui qui pose `aria-busy` sur
        // `#resultat`, et une région `aria-live` qui reçoit la barre avant d'être marquée occupée
        // la ferait annoncer, puis relire à chaque mise à jour.
        verrouiller(true);
        var attente = suivreAttente(null);
        soumettreAvecProgression(saisie, attente.etape)
          .then(function (r) {
            attente.fin();
            verrouiller(false);
            var source = tableau(vueForm.sources).filter(function (s) {
              return s && s.doc_id === saisie.doc_id;
            })[0];
            var contexte = {
              doc_id: saisie.doc_id,
              source_url: source && source.url
            };
            function afficher(valide) {
              // Story 5.6 (L2b) : une réponse est à l'écran. En desktop, la carte de saisie se
              // compacte (la feuille s'en charge) pour que la réponse commence plus haut, sans
              // jamais quitter l'écran : on peut relancer une autre description sans remonter.
              // C'est une classe sur `<body>` et rien d'autre — aucun style calculé ici.
              if (document.body) document.body.classList.add("a-repondu");
              var resultat = peindre(vueVerdict(valide, contexte));
              brancherLecteur(resultat);
              brancherAppuis(resultat);
              brancherConversation(resultat, valide, contexte, afficher);
            }
            afficher(r);
          })
          .catch(function (e) {
            attente.fin();
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
    lectureLue: lectureLue,
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
    // Story 5.6 (L2) : la lecture stricte d'AD-11, exposée pour que les corps figés du harnais de
    // rendu la traversent avant d'être peints — un corps figé qui ne tiendrait pas le contrat
    // serait une preuve sans valeur.
    lireReponse: lireReponse,
    soumettre: soumettre,
    soumettreConversation: soumettreConversation,
    soumettreProgression: soumettreProgression,
    soumettreAvecProgression: soumettreAvecProgression,
    decoupeurSSE: decoupeurSSE,
    suivreAttente: suivreAttente,
    majAttente: majAttente,
    rangEstime: rangEstime,
    ETAPES: ETAPES_SINISTRE.map(function (e) { return { nom: e.nom, libelle: e.libelle, ms: e.ms }; }),
    DUREE_ANNONCEE_S: DUREE_ANNONCEE_S,
    dureeTotaleEstimee: dureeTotaleEstimee,
    suivre: suivre,
    conversationVue: conversationVue,
    dossierTexte: dossierTexte,
    copierDossier: copierDossier,
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
    // Story 5.6 (L2) : la mécanique des quatre blocs, testable sans navigateur.
    trouverPassage: trouverPassage,
    bornesPhrases: bornesPhrases,
    paragraphes: paragraphes,
    appuisDe: appuisDe,
    phrasesRetirees: phrasesRetirees,
    questionsVue: questionsVue,
    dossierVue: dossierVue,
    brancherAppuis: brancherAppuis,
    VERDICTS: VERDICTS,
    vueSource: vueSource,
    urlPage: urlPage,
    ouvrirLecteur: ouvrirLecteur,
    fermerLecteur: fermerLecteur,
    naviguerLecteur: naviguerLecteur,
    brancherLecteur: brancherLecteur,
    brancherConversation: brancherConversation,
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
