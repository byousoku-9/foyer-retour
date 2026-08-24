// Moteur du chatbot.
// Deux modes :
//   - api    : le serveur qui sert cette page repond, chaque phrase adossee a un passage verifie.
//   - local  : recherche lexicale dans la base de connaissances, sans reseau.
// foyer-retour (story 1.7, AD-11) : le mode api est le seul mode automatique. Le mode local n'est
// JAMAIS un repli silencieux — il ne tourne que sur clic explicite (rechercheSimple), et seulement
// quand le serveur est indisponible (503) ou injoignable (panne reseau). Un 4xx affiche un message,
// sans bouton : une recherche de mots-cles n'est pas une reponse verifiee, et la faire passer pour
// telle est exactement ce que ce projet promet de ne pas faire.

window.CHAT = (function () {

  // AD-12 : une seule origine sert la page, l'API et les fichiers du site. Pas de CORS a demander,
  // pas d'hote a deviner — l'API est la ou la page est.
  var API_BASE = window.location.origin;
  var apiDisponible = null; // null = pas encore teste

  // Les seuils vivent dans `server/app/config.py` et sont servis a chaque chargement de page par
  // `GET /sante` (`SanteResponse.thresholds`). Les recopier ici les ferait diverger en silence : on
  // les retient de la sonde. Ce qui suit n'est qu'un **repli**, pour la premiere requete quand la
  // sonde n'a pas encore repondu.
  var HISTORIQUE_MAX_TOURS_REPLI = 6;   // config.historique_max_turns
  var DEADLINE_SERVEUR_REPLI = 55;      // config.deadline_s
  // Marge au-dessus de la deadline du serveur avant que le navigateur n'abandonne : en dessous, on
  // couperait une requete a laquelle il aurait repondu ; bien au-dela, l'utilisateur attendrait
  // pour rien. Ce n'etait pas un seuil du serveur, c'en est un : `config.client_abort_margin_s`,
  // publie par `/sante`. Ce qui reste ici n'est, comme les deux autres, qu'un **repli**.
  var MARGE_ABANDON_S_REPLI = 10;       // config.client_abort_margin_s
  // `Turn.texte <= 2000` (server/app/domain/question.py) n'est **pas** dans `thresholds()` : c'est
  // une contrainte de schema, pas un seuil de configuration. Elle reste donc ecrite ici, et un test
  // l'amarre a `Turn.model_fields["texte"]` pour qu'une divergence soit bruyante.
  var TOUR_MAX_CARACTERES = 2000;
  var seuilsServeur = {};

  // Une page ouverte en `file://` a pour `origin` la chaine "null" : il n'y a aucun serveur a
  // joindre, et sonder `null/sante` ne produirait qu'une erreur console incomprehensible.
  function enLigne() {
    var p = window.location.protocol;
    return p === "http:" || p === "https:";
  }

  function historiqueMaxTours() {
    return seuil("historique_max_turns", HISTORIQUE_MAX_TOURS_REPLI);
  }

  function seuil(nom, repli) {
    var v = seuilsServeur[nom];
    return (typeof v === "number" && v > 0) ? v : repli;
  }

  function delaiAbandonMs() {
    return Math.round((seuil("deadline_s", DEADLINE_SERVEUR_REPLI) +
                       seuil("client_abort_margin_s", MARGE_ABANDON_S_REPLI)) * 1000);
  }

  // ---------- Profil progressif ----------

  var CHAMPS = [
    { cle: "situation", question: "Vous venez seul, en couple, ou en famille ?",
      options: ["Seul", "En couple", "En famille"] },
    { cle: "enfants", question: "Combien d'enfants vous accompagnent ?",
      options: ["Aucun", "1", "2", "3 ou plus"] },
    { cle: "statut", question: "Quel sera votre statut au Luxembourg ?",
      options: ["Salarie", "Independant", "Les deux", "Pas encore d'emploi"] },
    { cle: "logement", question: "Vous comptez louer ou acheter ?",
      options: ["Louer", "Acheter", "Pas encore decide"] },
    { cle: "vehicule", question: "Vous amenez un véhicule ?",
      options: ["Oui", "Non"] },
    { cle: "horizon", question: "Où en êtes-vous ?",
      options: ["Je prepare mon depart", "Je viens d'arriver", "Je suis installe"] }
  ];

  // Les valeurs de profil restent sans accent : elles servent d'identifiants
  // (conditions du parcours, profils deja enregistres dans les navigateurs).
  // Cette table ne sert qu'a l'affichage.
  var AFFICHAGE = {
    "Salarie": "Salarié",
    "Independant": "Indépendant",
    "Pas encore decide": "Pas encore décidé",
    "Je prepare mon depart": "Je prépare mon départ",
    "Je suis installe": "Je suis installé"
  };
  function afficher(v) { return AFFICHAGE[v] || v; }

  function profilVide() { return {}; }

  function prochainChamp(profil) {
    for (var i = 0; i < CHAMPS.length; i++) {
      if (profil[CHAMPS[i].cle] === undefined) return CHAMPS[i];
    }
    return null;
  }

  function profilComplet(profil) { return prochainChamp(profil) === null; }

  function decrireProfil(profil) {
    var p = [];
    if (profil.situation) p.push(afficher(profil.situation).toLowerCase());
    if (profil.enfants && profil.enfants !== "Aucun") p.push(profil.enfants + " enfant(s)");
    if (profil.statut) p.push(afficher(profil.statut).toLowerCase());
    if (profil.logement) p.push(afficher(profil.logement).toLowerCase());
    if (profil.vehicule === "Oui") p.push("avec véhicule");
    if (profil.horizon) p.push(afficher(profil.horizon).toLowerCase());
    return p.length ? p.join(", ") : "profil non renseigné";
  }

  // Fiches prioritaires selon le profil.
  function fichesPourProfil(profil) {
    var ids = ["arrivee", "matricule", "luxtrust", "banque"];
    if (profil.logement === "Louer" || profil.logement === "Pas encore decide") ids.push("bail");
    if (profil.logement === "Acheter") { ids.push("achat", "interets", "logement_abordable"); }
    if (profil.enfants && profil.enfants !== "Aucun") { ids.push("ecole", "garde"); }
    if (profil.statut === "Independant" || profil.statut === "Les deux") ids.push("independant");
    if (profil.statut === "Salarie" || profil.statut === "Les deux") { ids.push("impots_classes", "conseil_fiscal", "impatries"); }
    if (profil.vehicule === "Oui") { ids.push("vehicule", "permis"); }
    ids.push("transport", "telecom", "cout_vie");
    var vus = {}, out = [];
    ids.forEach(function (id) {
      if (vus[id]) return; vus[id] = 1;
      var f = window.KB.fiches.filter(function (x) { return x.id === id; })[0];
      if (f) out.push(f);
    });
    return out;
  }

  // ---------- Recherche locale ----------

  function normaliser(s) {
    return String(s || "").toLowerCase()
      .normalize("NFD").replace(/[̀-ͯ]/g, "")
      .replace(/[^a-z0-9\s]/g, " ").replace(/\s+/g, " ").trim();
  }

  var VIDES = ["le","la","les","un","une","des","de","du","au","aux","et","ou","a","en","pour",
    "dans","sur","par","avec","est","sont","il","elle","je","tu","nous","vous","ils","que","qui",
    "quoi","comment","quand","combien","dois","doit","puis","peux","peut","mon","ma","mes","mais",
    "ce","cette","ces","son","sa","ses","y","d","l","n","s","faut","etre","avoir"];

  // Coupe un texte a la fin d'une phrase, au plus pres de la longueur cible.
  function resumer(texte, max) {
    var t = String(texte || "").trim();
    if (t.length <= max) return t;
    var coupe = t.slice(0, max);
    var point = coupe.lastIndexOf(". ");
    if (point > max * 0.5) return coupe.slice(0, point + 1);
    return coupe.slice(0, coupe.lastIndexOf(" ")) + "…";
  }

  function mots(s) {
    return normaliser(s).split(" ").filter(function (m) {
      return m.length > 2 && VIDES.indexOf(m) === -1;
    });
  }

  function scoreFiche(fiche, termes) {
    var corpsTexte = (fiche.corps || []).map(function (p) {
      return typeof p === "string" ? p : (p && p.h) || "";
    }).join(" ");
    var hay = normaliser([fiche.titre, fiche.resume, (fiche.tags || []).join(" "),
      corpsTexte, fiche.cat].join(" "));
    var titre = normaliser(fiche.titre + " " + (fiche.tags || []).join(" "));
    var s = 0;
    termes.forEach(function (t) {
      if (titre.indexOf(t) !== -1) s += 5;
      else if (hay.indexOf(t) !== -1) s += 2;
    });
    return s;
  }

  function rechercher(question, limite) {
    var termes = mots(question);
    if (!termes.length) return [];
    var res = window.KB.fiches.map(function (f) {
      return { fiche: f, score: scoreFiche(f, termes) };
    }).filter(function (x) { return x.score > 0; });
    res.sort(function (a, b) { return b.score - a.score; });
    return res.slice(0, limite || 3);
  }

  function chercherFaq(question) {
    var termes = mots(question);
    if (!termes.length) return null;
    var best = null, bestScore = 0;
    window.KB.faq.forEach(function (item) {
      var hay = normaliser(item.q + " " + item.a);
      var s = 0;
      termes.forEach(function (t) { if (hay.indexOf(t) !== -1) s += 1; });
      s = s / Math.max(1, termes.length);
      if (s > bestScore) { bestScore = s; best = item; }
    });
    return bestScore >= 0.5 ? best : null;
  }

  // Petites phrases de conversation, pour que l'echange reste naturel.
  function petitePhrase(question, profil) {
    var q = normaliser(question);
    if (/^(bonjour|salut|hello|bonsoir|coucou|hey|bjr)\b/.test(q) && q.length < 30) {
      return {
        texte: "Bonjour. Posez-moi une question sur votre installation : démarches, logement, " +
          "impôts, école, véhicule... Je réponds à partir des fiches du guide, avec leurs sources.",
        sources: [], fiches: []
      };
    }
    if (/^(merci|super merci|top merci|parfait merci|merci beaucoup)\b/.test(q) && q.length < 30) {
      return {
        texte: "Avec plaisir. Autre chose ? Le parcours de l'onglet Parcours s'adapte à votre profil, " +
          "et le simulateur donne le salaire net.",
        sources: [], fiches: []
      };
    }
    if (/^(au revoir|bonne journee|bonne soiree|a bientot|bye)\b/.test(q) && q.length < 30) {
      return {
        texte: "Bonne installation au Luxembourg. Le guide reste là, revenez quand vous voulez.",
        sources: [], fiches: []
      };
    }
    return null;
  }

  // Reponse locale, avec sources.
  function reponseLocale(question, profil) {
    var q = normaliser(question);

    var pp = petitePhrase(question, profil);
    if (pp) return pp;

    // Intentions chiffrees renvoyees vers le simulateur.
    if (/(salaire|net|brut|combien.*gagne|paie)/.test(q) && /\d/.test(question)) {
      var m = question.replace(/\s/g, "").match(/(\d{4,7})/);
      if (m) {
        var brut = parseInt(m[1], 10);
        if (brut >= 10000 && brut <= 2000000) {
          var comp = window.SIM.comparatif(brut, 12);
          var lignes = comp.map(function (c) {
            return c.label + " : " + Math.round(c.res.netMensuel).toLocaleString("fr-FR") + " EUR net par mois";
          });
          return {
            texte: "Pour " + brut.toLocaleString("fr-FR") + " EUR brut par an, sur 12 mois :\n\n" +
              lignes.join("\n") + "\n\nCes montants correspondent à la retenue sur le seul salaire. " +
              "L'onglet Simulateur permet d'ajuster le nombre de mois, le régime des impatriés et les forfaits.",
            sources: [{ t: "Barème officiel ACD", u: "https://impotsdirects.public.lu/fr/baremes.html" }],
            fiches: ["impots_classes", "impatries"]
          };
        }
      }
    }

    var faq = chercherFaq(question);
    var hits = rechercher(question, 3);
    var pertinent = hits.length && hits[0].score >= 5;

    var toucheContrats = window.CONTRATS_KB &&
      /(assur|contrat|couvert|couverture|sinistre|franchise|indemnis|vole?\b|degat)/.test(q);

    if (!faq && !pertinent) {
      var suggestions = fichesPourProfil(profil).slice(0, 4)
        .map(function (f) { return f.titre; }).join(", ");
      return {
        texte: "Je n'ai pas d'élément sur ce point dans la base. Je préfère le dire plutôt que d'inventer.\n\n" +
          "Sujets couverts qui vous concernent : " + suggestions + ".\n" +
          "Reformulez, ou consultez directement les fiches.",
        sources: [], fiches: [],
        comparateur: toucheContrats
      };
    }

    var parts = [];
    if (faq) parts.push(faq.a);

    var principale = pertinent ? hits[0].fiche : null;
    if (principale && (!faq || faq.fiche !== principale.id)) {
      // Le corps peut contenir des sous-titres { h: ... } : on ne garde que les paragraphes.
      // Reponse courte : l'essentiel tient dans une bulle, la fiche donne le detail.
      var paras = (principale.corps || []).filter(function (p) { return typeof p === "string"; });
      parts.push("D'après la fiche « " + principale.titre + " » : " + resumer(paras.join(" "), 320));
    }
    if (principale && principale.aRetenir && principale.aRetenir.length) {
      parts.push("À retenir :\n" + principale.aRetenir.slice(0, 2)
        .map(function (x) { return "- " + x; }).join("\n"));
    }

    var srcs = [];
    if (principale && principale.sources) srcs = principale.sources.slice();

    var r = {
      texte: parts.join("\n\n"),
      sources: srcs,
      fiches: pertinent ? hits.map(function (h) { return h.fiche.id; }) : []
    };
    // Question qui touche aux contrats d'assurance : proposer le comparateur.
    if (toucheContrats) r.comparateur = true;
    return r;
  }

  // ---------- Recherche simple, sur clic seulement ----------

  // AD-11 / FR11 : le moteur lexical ci-dessus ne repond plus jamais tout seul. Il ne tourne que
  // par cette porte, ouverte par un bouton que l'utilisateur clique apres avoir lu « assistant
  // indisponible ». Le resultat porte `via: "local"` pour que le badge le dise.
  function rechercheSimple(question, profil) {
    var r = reponseLocale(question, profil || {});
    r.via = "local";
    return r;
  }

  // ---------- Ce que l'UI peindra : des fonctions pures ----------

  // L'historique du site est `{role, content}` ; le contrat d'AD-11 attend `{role, texte}`.
  //
  //   1. le dernier tour utilisateur est exclu s'il est identique a `question` — le site pousse la
  //      question dans l'historique avant l'appel, et l'envoyer deux fois la ferait resoudre contre
  //      elle-meme ;
  //   2. deux sortes de tours ne peuvent pas etre envoyees : celui que la **recherche simple** a
  //      produit (`local: true`) — l'expedier ferait traiter par *comprendre*, comme sa propre
  //      parole, une comparaison de mots-cles ; et celui qui depasse `Turn.texte` — le couper
  //      changerait ce qui a ete dit ;
  //   3. un tour qu'on ne peut pas envoyer **casse la chaine** : ce qui le precede parle d'un
  //      echange que le serveur ne verra pas, et deux tours du meme role se suivraient. On ne garde
  //      donc que la **queue contigue** qui suit le dernier trou, jamais un historique troue ;
  //   4. on borne aux plus recents (`historique_max_turns`, lu sur `/sante`) — au-dela le serveur
  //      rend 400, et il ne tronque jamais lui-meme.
  function historiquePourApi(historique, question, maxTours) {
    var max = (typeof maxTours === "number" && maxTours > 0) ? maxTours : historiqueMaxTours();
    var tours = (historique || []).map(function (t) {
      return {
        role: t && t.role === "assistant" ? "assistant" : "user",
        texte: String((t && (t.texte !== undefined ? t.texte : t.content)) || ""),
        local: !!(t && t.local)
      };
    }).filter(function (t) { return t.texte.trim() !== ""; });

    var q = String(question || "").trim();
    if (tours.length && tours[tours.length - 1].role === "user" &&
        tours[tours.length - 1].texte.trim() === q) {
      tours = tours.slice(0, -1);
    }

    var depart = 0;
    for (var i = 0; i < tours.length; i++) {
      if (tours[i].local || tours[i].texte.length > TOUR_MAX_CARACTERES) depart = i + 1;
    }
    return tours.slice(depart).slice(-max).map(function (t) {
      return { role: t.role, texte: t.texte };
    });
  }

  // Appariement citation ↔ phrase (AD-11, Design Notes de la story 1.7).
  //
  // `sources[]` est une liste **plate**, sans `claim_id`. Mais le serveur la construit par
  // l'enumeration `for claim in answer.claims for quote in claim.quotes` (api/presenter.py), et
  // publie `answer` en entier. Le front refait donc la **meme** enumeration et lit `sources[]` dans
  // l'ordre, en verifiant a chaque pas que `sources[i].block_id` est celui de la quote attendue.
  //
  // Au moindre desaccord — un `block_id` qui ne concorde pas, des longueurs differentes, un segment
  // qui cite une claim absente — l'appariement est **abandonne** (`null`) et l'UI peint la liste
  // plate sous la reponse. Une degradation visible vaut mieux qu'une citation attribuee a la
  // mauvaise phrase ; un test serveur verrouille l'ordre pour que la casse soit bruyante.
  function citationsParSegment(answer, sources) {
    var a = answer || {};
    var claims = a.claims || [];
    var segments = a.segments || [];
    var plates = sources || [];

    // Sans prototype : un `claim_id` valant "toString" ou "constructor" trouverait sinon un
    // heritage, et l'abandon — la seule protection contre une citation mal placee — ne se
    // declencherait pas.
    var parClaim = Object.create(null);
    var rang = 0;
    for (var c = 0; c < claims.length; c++) {
      var claim = claims[c] || {};
      var quotes = claim.quotes || [];
      var liste = [];
      for (var q = 0; q < quotes.length; q++) {
        var src = plates[rang];
        if (!src || src.block_id !== quotes[q].block_id) return null;
        liste.push({ source: src, status: claim.status || null, claim_id: claim.claim_id, rang: rang });
        rang++;
      }
      parClaim[claim.claim_id] = liste;
    }
    if (rang !== plates.length) return null;

    var out = [];
    for (var s = 0; s < segments.length; s++) {
      var ids = (segments[s] || {}).claim_ids || [];
      var vus = {}, citations = [];
      for (var i = 0; i < ids.length; i++) {
        var entrees = parClaim[ids[i]];
        if (!entrees) return null;  // segment citant une claim absente de claims[] : on n'invente pas
        for (var j = 0; j < entrees.length; j++) {
          if (vus[entrees[j].rang]) continue;  // jamais deux fois la meme citation sous un segment
          vus[entrees[j].rang] = 1;
          citations.push(entrees[j]);
        }
      }
      out.push(citations);
    }
    return out;
  }

  // AD-4 : `edition` s'affiche **avec sa reserve**, jamais comme un statut vert. `applicable` reste
  // null en guide (il est reserve au sinistre) et n'est donc pas affiche.
  function statutTexte(status) {
    if (!status) return "";
    var p = [];
    if (status.retrouvee === true) p.push("retrouvée");
    if (status.pertinente === true) p.push("pertinente");
    // `Document.edition` est un `str` sans `min_length` : elle peut etre vide. La reserve
    // d'actualite reste due — sans elle, la citation passerait pour a jour, ce qu'AD-4 refuse
    // precisement (« jamais comme statut vert »).
    p.push("édition " + (status.edition ? status.edition : "non précisée") +
           " — actualité non vérifiée");
    return p.join(" · ");
  }

  function pluriel(n, mot) { return n + " " + mot + (n > 1 ? "s" : ""); }

  function entier(v) {
    return (typeof v === "number" && isFinite(v) && v >= 0) ? Math.floor(v) : 0;
  }

  // AD-4 : la preuve chiffree d'une absence — termes **canoniques**, nombre de variantes, passages
  // parcourus. Jamais la liste des variantes ni des declencheurs : le contrat ne les transporte pas.
  //
  // Les compteurs s'affichent **meme a zero**. L'AC demande « N variantes essayees, M passages
  // parcourus » : zero est une reponse a cette question, et c'est meme la plus probante — « rien n'a
  // ete cherche » et « 312 passages ont ete lus sans rien trouver » sont deux refus differents, que
  // l'omission rendait indiscernables. Un refus `hors_perimetre` court-circuite avant tout retrieval
  // (AD-5) : ses deux compteurs sont nuls, et c'est precisement ce qu'il faut dire.
  //
  // La seule exception est `clarification_requise` : AD-4 pose que « `terms_searched` est alors vide
  // et `blocks_scanned` nul : rien n'a ete cherche ». Ce n'est pas un refus mais une question posee
  // en retour ; lui accrocher « 0 variante essayee » repondrait a une question que personne ne pose.
  function preuveAbsence(reason) {
    if (!reason) return "";
    if (reason.kind === "clarification_requise") return "";
    var termes = (reason.terms_searched || []).filter(function (t) { return String(t || "").trim(); });
    var variantes = entier(reason.variants_count);
    var blocs = entier(reason.blocks_scanned);
    var chiffres = [
      pluriel(variantes, "variante") + (variantes > 1 ? " essayées" : " essayée"),
      pluriel(blocs, "passage") + (blocs > 1 ? " parcourus" : " parcouru")
    ];
    // Aucun terme retenu se **dit** aussi : la question n'a produit aucun terme canonique, ce qui
    // explique a soi seul les deux zeros qui suivent.
    var debut = termes.length
      ? "Termes cherchés : " + termes.join(", ")
      : "Aucun terme du guide n'a été retenu";
    return debut + " — " + chiffres.join(", ");
  }

  // NFR4 : le cout reel de la reponse, en pied de reponse. Il vient de l'usage rendu par l'API
  // (`trace.total_cost_eur`), jamais d'une estimation du front.
  function coutTexte(trace) {
    var c = trace && typeof trace.total_cost_eur === "number" ? trace.total_cost_eur : null;
    if (c === null || isNaN(c)) return "";
    // Un total nul veut dire qu'aucun appel n'a ete facture (court-circuit avant tout appel, ou
    // reponse entierement servie du cache) : « 0,0000 € » ferait croire a un arrondi.
    if (c === 0) return "cette réponse n'a rien coûté (aucun appel facturé)";
    return "cette réponse a coûté " + c.toFixed(4).replace(".", ",") + " €";
  }

  // FR5 : les trois etats, lus sur les deux booleens que *verifier* calcule (AD-4).
  function etatReponse(answer) {
    var a = answer || {};
    if (!a.found) return { cle: "inconnu", texte: "inconnu" };
    if (!a.complete) return { cle: "partiel", texte: "partiel" };
    return { cle: "sur", texte: "sûr" };
  }

  // FR11 : un message **lisible**, compose a partir du `code` d'AD-16. Le `message` du serveur est
  // produit par pydantic, en anglais, avec le chemin du champ (`body.historique: List should have at
  // most 6 items`) : utile a un developpeur, pas a un arrivant. Il n'est jamais affiche.
  // Un code inconnu ne doit pas produire une page muette : il a sa phrase, lui aussi.
  function messageErreur(erreur) {
    var e = erreur || {};
    if (e.code === "reseau") {
      return "L'assistant est injoignable : la page n'a pas pu joindre le serveur.";
    }
    if (e.code === "hors_ligne") {
      return "Cette page est ouverte depuis un fichier local : l'assistant a besoin du serveur " +
        "qui sert le guide. Ouvrez le guide depuis son adresse.";
    }
    if (e.code === "timeout_client") {
      return "L'assistant n'a pas répondu dans le temps imparti. Réessayez, ou consultez le guide.";
    }
    if (e.code === "rate_limited") {
      var s = e.retry_after;
      return typeof s === "number" && s > 0
        ? "Trop de questions en peu de temps : réessayez dans " + pluriel(s, "seconde") + "."
        : "Trop de questions en peu de temps : réessayez dans un instant.";
    }
    if (e.code === "invalid_request") {
      return "Le serveur a refusé la question : elle sort de ce que le contrat accepte " +
        "(question trop longue, ou conversation trop longue). Reformulez plus court.";
    }
    if (e.code === "input_too_long") {
      return "La question envoyée est trop volumineuse pour le serveur. Raccourcissez-la.";
    }
    if (e.kind === "indisponible") {
      return "L'assistant est indisponible pour le moment.";
    }
    return "Le serveur n'a pas pu répondre à cette question. Réessayez plus tard.";
  }

  // ---------- Ce que l'UI peint : une description, pas du DOM ----------
  //
  // Toutes les promesses de la story sont des verbes d'affichage : « affiche chaque segment factuel
  // suivi de ses citations », « bandeau + bouton », « message sans repli », « le coût en pied ».
  // Les verifier demandait soit un faux DOM — qui teste la doublure —, soit que la **composition**
  // quitte `ui.js`. C'est celle-ci : `vueReponse`, `vueReponseLocale`, `vueErreur` et `vueAttente`
  // rendent un arbre de noeuds simples `{tag, cls, texte, enfants, action, href}` que `ui.js`
  // materialise sans decider de rien.
  //
  // Une `action` est **decrite** (`{nom: "recherche_simple", question}`), jamais une fermeture :
  // c'est ce qui permet d'affirmer « ce bandeau porte exactement une action de recherche simple, et
  // ce message zero » — la regle d'AD-16 qu'aucun test ne voyait auparavant.
  function noeud(tag, cls, texte, enfants) {
    var n = { tag: tag };
    if (cls) n.cls = cls;
    if (texte !== undefined && texte !== null) n.texte = String(texte);
    if (enfants && enfants.length) n.enfants = enfants;
    return n;
  }

  function ficheConnue(id) {
    if (!id || !window.KB || !window.KB.fiches) return null;
    return window.KB.fiches.filter(function (f) { return f.id === id; })[0] || null;
  }

  // Un lien ne s'ouvre que s'il est http(s). Les URL viennent de notre corpus, pas du modele — mais
  // c'est le genre de garantie qu'on ne veut pas devoir re-verifier a chaque ingestion.
  function lienHttp(url) {
    var u = String(url || "");
    return /^https?:\/\//i.test(u) ? u : null;
  }

  // Le statut d'une citation retrouve par son bloc : c'est ce qui rend la reserve d'AD-4 au mode
  // degrade, ou la liste est plate et ou l'appariement claim → citation a ete abandonne.
  function statutDeBloc(answer, blockId) {
    var claims = (answer && answer.claims) || [];
    for (var i = 0; i < claims.length; i++) {
      var quotes = claims[i].quotes || [];
      for (var j = 0; j < quotes.length; j++) {
        if (quotes[j].block_id === blockId) return claims[i].status || null;
      }
    }
    return null;
  }

  function citationsVue(entrees) {
    var enfants = [noeud("strong", null, entrees.length > 1 ? "Passages cités" : "Passage cité")];
    entrees.forEach(function (e) {
      var src = e.source || {};
      var meta = [];
      // `fiche_id` vient du corpus servi, qui peut diverger de `kb.js` : un bouton qui ouvre une
      // fiche inconnue retomberait sur la liste complete, sans explication. Titre en texte alors.
      var fiche = ficheConnue(src.fiche_id);
      if (fiche) {
        var b = noeud("button", "cite-fiche", src.titre || fiche.titre);
        b.action = { nom: "ouvrir_fiche", fiche_id: src.fiche_id };
        meta.push(b);
      } else if (src.titre) {
        meta.push(noeud("span", "cite-fiche-txt", src.titre));
      }
      var url = lienHttp(src.url);
      if (url) {
        var a = noeud("a", "cite-lien", "source officielle");
        a.href = url;
        meta.push(a);
      }
      var statut = statutTexte(e.status);
      if (statut) meta.push(noeud("span", "cite-statut", statut));
      enfants.push(noeud("div", "cite", null, [
        noeud("blockquote", "cite-q", "« " + String(src.quote || "") + " »"),
        noeud("div", "cite-meta", null, meta)
      ]));
    });
    return noeud("div", "cites", null, enfants);
  }

  function chipsVue(r, question) {
    var boutons = [];
    var fiches = (r && r.fiches) || [];
    fiches.slice(0, 3).forEach(function (id) {
      var f = ficheConnue(id);
      if (!f) return;
      var b = noeud("button", "chip", "Ouvrir : " + f.titre);
      b.action = { nom: "ouvrir_fiche", fiche_id: id };
      boutons.push(b);
    });
    // Relance : approfondir le sujet principal sans avoir a reformuler.
    var principale = ficheConnue(fiches[0]);
    if (principale) {
      var relance = noeud("button", "chip", "En savoir plus");
      relance.action = { nom: "poser", question: "Peux-tu détailler : " + principale.titre + " ?" };
      boutons.push(relance);
    }
    // Question d'assurance : la main passe au comparateur, qui sait construire le tableau que
    // l'assistant general ne construit pas. On lui transmet la question telle quelle.
    if (r && r.comparateur) {
      var comp = noeud("button", "chip", "Construire le tableau de comparaison");
      comp.action = { nom: "comparateur", question: String(question || "") };
      boutons.push(comp);
    }
    return boutons.length ? noeud("div", "chips", null, boutons) : null;
  }

  function vueAttente() {
    return noeud("div", "msg bot attente", null, [
      noeud("span", "attente-txt",
        "Je cherche dans le guide, puis je vérifie chaque phrase contre les passages cités…"),
      noeud("span", "points", null, [noeud("span"), noeud("span"), noeud("span")])
    ]);
  }

  function vueReponse(r, question) {
    var a = (r && r.answer) || {};
    var sources = (r && r.sources) || [];
    var enfants = [];

    // La clarification est une **question posee a l'utilisateur** : elle passe avant la phrase de
    // refus, qui explique seulement pourquoi rien n'a ete cherche.
    if (a.clarification) {
      enfants.push(noeud("div", "clarif", null, [
        noeud("strong", null, "Une précision, pour chercher au bon endroit"),
        noeud("p", "clarif-q", String(a.clarification))
      ]));
    }

    // `answer.segments` fait foi ; `segments[]` de premier niveau en est la copie du contrat. Si
    // l'un est vide et pas l'autre, l'appariement doit porter sur **ceux qu'on peint**, sans quoi
    // les citations disparaitraient des deux cotes.
    var segments = (a.segments && a.segments.length) ? a.segments : ((r && r.segments) || []);
    var appariees = citationsParSegment({ claims: a.claims || [], segments: segments }, sources);
    var placees = 0;
    if (appariees) appariees.forEach(function (c) { placees += c.length; });

    if (segments.length && appariees && placees === sources.length) {
      segments.forEach(function (seg, i) {
        var bloc = [noeud("p", "seg-txt", String(seg.text || ""))];
        var cites = appariees[i] || [];
        if (cites.length) bloc.push(citationsVue(cites));
        enfants.push(noeud("div", "seg" + (seg.kind === "factuel" ? " seg-factuel" : ""), null, bloc));
      });
    } else {
      // Degradation **visible**, et qui ne retire rien : le texte entier, la raison de la
      // degradation, puis la liste plate avec ses statuts — le mode degrade serait le dernier
      // endroit ou taire la reserve d'actualite.
      enfants.push(noeud("p", "seg-txt", String((r && r.texte) || "")));
      if (sources.length) {
        enfants.push(noeud("p", "degrade",
          "Les passages ci-dessous soutiennent cette réponse, mais je n'ai pas pu rattacher " +
          "chacun à la phrase exacte qu'il appuie : ils sont donnés ensemble."));
        enfants.push(citationsVue(sources.map(function (src) {
          return { source: src, status: statutDeBloc(a, src.block_id) };
        })));
      }
    }

    // AD-4 : la phrase de refus vient du serveur (elle est ci-dessus, dans les segments) ; ce que le
    // front ajoute, c'est la preuve chiffree — jamais les variantes ni les declencheurs.
    var preuve = preuveAbsence(a.reason);
    if (preuve) enfants.push(noeud("p", "preuve", preuve));

    var inconnus = (r && r.unknown) || [];
    if (inconnus.length) {
      enfants.push(noeud("div", "inconnu", null, [
        noeud("strong", null, "Ce que je ne sais pas"),
        noeud("ul", null, null, inconnus.map(function (x) { return noeud("li", null, String(x)); }))
      ]));
    }

    var etat = etatReponse(a);
    var pied = [noeud("span", "etat etat-" + etat.cle, etat.texte)];
    var cout = coutTexte(r && r.trace);
    if (cout) pied.push(noeud("span", "cout", cout));
    enfants.push(noeud("div", "pied", null, pied));

    var chips = chipsVue(r, question);
    if (chips) enfants.push(chips);
    return noeud("div", "msg bot", null, enfants);
  }

  // La reponse de la recherche simple. Son pied ne porte ni etat ni cout — elle n'a rien coute et
  // rien n'a ete verifie : elle le dit, plutot que d'emprunter la forme d'une reponse sourcee.
  function vueReponseLocale(r, question) {
    var enfants = [noeud("p", "seg-txt", String((r && r.texte) || ""))];
    var sources = (r && r.sources) || [];
    if (sources.length) {
      enfants.push(noeud("div", "srcs", null, [noeud("strong", null, "Sources")].concat(
        sources.map(function (src) {
          var a = noeud("a", null, src.t);
          var url = lienHttp(src.u);
          if (url) a.href = url;
          return a;
        }))));
    }
    enfants.push(noeud("div", "pied", null, [
      noeud("span", "etat etat-local", "recherche simple"),
      noeud("span", "sans-verif",
        "aucune vérification : ces passages viennent d'une comparaison de mots-clés")
    ]));
    var chips = chipsVue(r, question);
    if (chips) enfants.push(chips);
    return noeud("div", "msg bot locale", null, enfants);
  }

  // FR11 / AD-11 / AD-16 : l'action de recherche simple n'est portee que par une indisponibilite.
  // C'est ici, et nulle part ailleurs, que la regle « pas de repli sur un 4xx » se decide.
  function vueErreur(erreur, question) {
    var indispo = !!(erreur && erreur.kind === "indisponible");
    var enfants = [
      noeud("strong", "alerte-titre", indispo ? "Assistant indisponible" : "Question non traitée"),
      noeud("p", "alerte-txt", messageErreur(erreur))
    ];
    if (indispo) {
      enfants.push(noeud("p", "alerte-note",
        "Rien n'a été cherché : la recherche simple du guide compare des mots-clés, elle ne " +
        "vérifie rien. À vous de décider si elle vous suffit."));
    }
    if (erreur && erreur.request_id) {
      enfants.push(noeud("p", "ref", "référence : " + erreur.request_id));
    }
    if (indispo) {
      var bouton = noeud("button", "chip", "Consulter le guide en recherche simple");
      bouton.action = { nom: "recherche_simple", question: String(question || "") };
      enfants.push(noeud("div", "chips", null, [bouton]));
    }
    return noeud("div", "msg bot " + (indispo ? "indispo" : "err"), null, enfants);
  }

  // Le badge et le bandeau ne peuvent pas se contredire dans la meme vue : sur une indisponibilite,
  // le mode l'est aussi. Sur un 4xx, non — le serveur a repondu, il a refuse la requete.
  function modeApresErreur(erreur) {
    return (erreur && erreur.kind === "indisponible") ? "indisponible" : null;
  }

  // Le libelle du badge de mode. Il se compose **ici**, comme tout ce qui s'affiche : `ui.js` ne
  // fait que le poser, dans les deux surfaces (l'onglet Assistant et le widget flottant — le
  // panneau est `hidden` des qu'on regarde un autre onglet, et un indicateur invisible n'indique
  // rien).
  //
  // L'etat initial n'est **pas** « mode local » : avant que la sonde ait repondu, le mode n'est pas
  // connu, et annoncer le mode local avant meme d'avoir essaye le serveur est le contraire de ce
  // que la story promet — c'est meme le seul mode qui ne se declenche jamais tout seul.
  function libelleMode(via) {
    var m = String(via === null || via === undefined ? "" : via);
    if (!m) return { texte: "mode : vérification…", cls: "badge" };
    if (m.indexOf("api") === 0) return { texte: "mode api", cls: "badge on" };
    if (m === "indisponible") return { texte: "mode indisponible", cls: "badge off" };
    return { texte: "mode " + m, cls: "badge" };
  }

  // ---------- Mode API ----------

  // L'erreur typee que `repondre()` propage. `kind` decide de ce que l'UI propose :
  //   - "indisponible" (503 ou panne reseau) : bandeau + bouton « consulter le guide en recherche
  //     simple » — la **seule** porte vers le moteur lexical, et elle demande un clic ;
  //   - "requete" (4xx/429/500, corps illisible) : message seul, aucun bouton (AD-16 : « repli local
  //     sur une erreur 4xx » est nommement ce qu'il empeche).
  function erreurChat(d) {
    var e = new Error(d.code || d.kind);
    e.nom = "ErreurChat";
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
    return erreurChat({
      // Seul un 503 ouvre la porte du mode local (AD-11/AD-16). Un 500 n'est pas une
      // indisponibilite passagere que la recherche simple contournerait.
      kind: statut === 503 ? "indisponible" : "requete",
      code: typeof err.code === "string" ? err.code : "",
      statut: statut,
      retry_after: isFinite(retry) ? retry : null,
      request_id: typeof err.request_id === "string" ? err.request_id : ""
    });
  }

  function estObjet(v) { return !!v && typeof v === "object" && !Array.isArray(v); }

  // ---------- Les trois formes d'exigence du contrat ----------
  //
  // `ChatResponse` et les modeles du domaine distinguent trois choses ; la lecture du front doit les
  // distinguer aussi, sous peine d'etre soit plus permissive que le contrat (un corps que personne
  // n'a pu servir, peint en reponse : la « reponse vide presentee comme reponse » d'AD-16), soit
  // plus stricte que lui (une reponse servie transformee en « assistant indisponible ») :
  //
  //   - **obligatoire** (`texte`, `answer`, `trace`, `Trace.request_id`, `AbsenceProof.kind`,
  //     `AnswerSegment.kind`, `SourceItem.quote`…) : absent, pydantic refuse de monter l'objet —
  //     aucune route du projet n'a donc pu ecrire ce corps ;
  //   - **a valeur par defaut** (`sources`, `unknown`, `via`, `terms_searched`, `total_cost_eur`…) :
  //     l'**absence** est normale, mais `null` **n'est pas** l'absence. Aucun de ces champs n'est
  //     `| None` dans le contrat : pydantic refuse `null` comme il refuse une chaine a la place
  //     d'une liste. Le convertir en silence en valeur par defaut, c'etait a nouveau peindre une
  //     reponse a partir d'un corps qu'aucune route ne peut ecrire ;
  //   - **nullable** (`reason`, `clarification`, `fiche_id`, `url`, `pertinente`…) : `null` est une
  //     valeur du contrat, et se lit comme telle.
  //
  // Les types **imbriques** comptent autant que le premier etage, parce que ce sont eux que l'ecran
  // consomme : `reason.kind` decide de la preuve chiffree, `segments[].kind` de la mise en forme,
  // `sources[].block_id` de l'appariement citation ↔ phrase, `claims[].quotes[]` de son enumeration.
  // Un `reason: {}` passait ainsi pour un refus muni d'une preuve « 0 variante essayee, 0 passage
  // parcouru » que rien n'avait calculee (revue Codex 1.7, B2, tour 3).
  //
  // Ce qui n'est **pas** verifie ici l'est deliberement : pydantic lit en mode permissif (`"4"` vaut
  // `4`), et refuser ce qu'il accepte ferait disparaitre une reponse reellement servie.
  // `tests/test_web_chat.py` tient les deux bords — tout corps refuse ici doit l'etre par
  // `ChatResponse.model_validate()`, et tout corps serialise par `ChatResponse` doit etre peint.

  function exigerChaine(v, nom) { if (typeof v !== "string") throw illisible(nom); }

  function exigerObjet(v, nom) { if (!estObjet(v)) throw illisible(nom); }

  // Un champ a valeur par defaut : absent, on rend `undefined` ; `null`, c'est une casse.
  function defaut(v, nom) {
    if (v === null) throw illisible(nom);
    return v;
  }

  // Un compteur affiche par l'ecran doit etre un entier fini : un objet ou une chaine
  // passeraient sinon jusqu'au rendu (AD-16 : jamais de 200 incomplet presente comme reponse).
  function entierDefaut(v, nom) {
    if (v === undefined) return 0;
    if (typeof v !== "number" || !isFinite(v) || Math.floor(v) !== v || v < 0) throw illisible(nom);
    return v;
  }

  function listeDefaut(v, nom) {
    var l = defaut(v, nom);
    if (l === undefined) return [];
    if (!Array.isArray(l)) throw illisible(nom);
    return l;
  }

  function listeDeChaines(v, nom) {
    var l = listeDefaut(v, nom);
    for (var i = 0; i < l.length; i++) {
      if (typeof l[i] !== "string") throw illisible(nom + "[" + i + "]");
    }
    return l;
  }

  // Un champ nullable de type chaine : absent ou `null` ⇒ rien, sinon une chaine.
  function chaineNullable(v, nom) {
    if (v === undefined || v === null) return null;
    exigerChaine(v, nom);
    return v;
  }

  function litteral(v, valeurs, nom) {
    for (var i = 0; i < valeurs.length; i++) { if (v === valeurs[i]) return v; }
    throw illisible(nom);
  }

  var KINDS_ABSENCE = ["hors_perimetre", "zero_hit", "claims_rejetes", "clarification_requise"];
  var KINDS_SEGMENT = ["factuel", "transition", "limite"];

  // `AbsenceProof` (AD-4) : `kind` est le seul champ obligatoire, et c'est celui dont l'ecran depend
  // le plus — `clarification_requise` supprime la preuve chiffree, les trois autres l'affichent.
  function verifierPreuve(r, nom) {
    exigerObjet(r, nom);
    litteral(r.kind, KINDS_ABSENCE, nom + ".kind");
    listeDeChaines(r.terms_searched, nom + ".terms_searched");
    listeDeChaines(r.documents, nom + ".documents");
    entierDefaut(r.variants_count, nom + ".variants_count");
    entierDefaut(r.blocks_scanned, nom + ".blocks_scanned");
  }

  // `AnswerSegment` : `text` et `kind` sont obligatoires ; `claim_ids` porte l'appariement.
  function verifierSegments(v, nom) {
    var l = listeDefaut(v, nom);
    for (var i = 0; i < l.length; i++) {
      var ou = nom + "[" + i + "]";
      exigerObjet(l[i], ou);
      exigerChaine(l[i].text, ou + ".text");
      litteral(l[i].kind, KINDS_SEGMENT, ou + ".kind");
      listeDeChaines(l[i].claim_ids, ou + ".claim_ids");
    }
    return l;
  }

  // `SourceItem` : `block_id`, `quote` et `status` sont obligatoires — ce sont les trois que la
  // citation affiche, et `block_id` est ce sur quoi l'appariement se verifie a chaque pas.
  function verifierSources(v, nom) {
    var l = listeDefaut(v, nom);
    for (var i = 0; i < l.length; i++) {
      var ou = nom + "[" + i + "]";
      exigerObjet(l[i], ou);
      exigerChaine(l[i].block_id, ou + ".block_id");
      exigerChaine(l[i].quote, ou + ".quote");
      exigerChaine(l[i].status, ou + ".status");
      if (defaut(l[i].titre, ou + ".titre") !== undefined) exigerChaine(l[i].titre, ou + ".titre");
      chaineNullable(l[i].fiche_id, ou + ".fiche_id");
      chaineNullable(l[i].url, ou + ".url");
    }
    return l;
  }

  // `VerifiedClaim` : la forme d'une claim, sans son invariant de coherence (celui-ci depend de
  // `found` et se juge plus bas). `quotes` a un `min_length=1` — une claim sans citation relue ne
  // peut pas exister, et l'appariement lit `sources[]` au rythme de cette enumeration.
  function verifierClaims(v, nom) {
    var l = listeDefaut(v, nom);
    for (var i = 0; i < l.length; i++) {
      var ou = nom + "[" + i + "]";
      exigerObjet(l[i], ou);
      exigerChaine(l[i].claim_id, ou + ".claim_id");
      exigerChaine(l[i].text, ou + ".text");
      var quotes = l[i].quotes;
      if (!Array.isArray(quotes) || !quotes.length) throw illisible(ou + ".quotes");
      for (var q = 0; q < quotes.length; q++) {
        var oq = ou + ".quotes[" + q + "]";
        exigerObjet(quotes[q], oq);
        exigerChaine(quotes[q].block_id, oq + ".block_id");
        exigerChaine(quotes[q].quote, oq + ".quote");
      }
      var st = l[i].status;
      exigerObjet(st, ou + ".status");
      if (typeof st.retrouvee !== "boolean") throw illisible(ou + ".status.retrouvee");
      exigerChaine(st.edition, ou + ".status.edition");
    }
    return l;
  }

  // Les invariants d'`Answer._found_coherence` (`server/app/domain/answer.py`), refaits ici. Le
  // serveur ne peut pas servir un corps qui les viole — pydantic refuse de monter l'objet — donc un
  // corps qui les viole ne vient pas du serveur et n'est pas une reponse. Ils comptent a l'ecran :
  // l'etat affiche (« sûr » / « partiel » / « inconnu ») se calcule sur `found` et `complete`, et la
  // preuve d'absence sur `reason` ; un `found=true` sans claim peindrait « sûr » sur une reponse
  // sans une seule citation relue, un `found=false` sans `reason` un refus sans sa preuve chiffree.
  function verifierAnswer(a) {
    var claims = verifierClaims(a.claims, "answer.claims");
    var unknown = listeDeChaines(a.unknown, "answer.unknown");
    verifierSegments(a.segments, "answer.segments");
    if (defaut(a.texte, "answer.texte") !== undefined) exigerChaine(a.texte, "answer.texte");
    chaineNullable(a.clarification, "answer.clarification");
    // « found=False exige une preuve d'absence (reason) » — et cette preuve est un `AbsenceProof`
    // entier, pas un objet quelconque : c'est `reason.kind` qui decide de ce qui s'affiche.
    if (a.reason !== undefined && a.reason !== null) verifierPreuve(a.reason, "answer.reason");
    if (!a.found && !estObjet(a.reason)) throw illisible("answer.reason");
    // « found=True exige au moins une claim retrouvée et pertinente » ∧ « found=False exige claims=[] ».
    if (a.found !== (claims.length > 0)) throw illisible("answer.claims");
    // « claims[] ne contient que des claims retrouvee ∧ pertinente ».
    for (var i = 0; i < claims.length; i++) {
      var statut = claims[i].status;
      if (statut.retrouvee !== true || statut.pertinente !== true) throw illisible("answer.claims");
    }
    // « complete=True exige found=True et unknown=[] ».
    if (a.complete && (!a.found || unknown.length > 0)) throw illisible("answer.complete");
  }

  // `Trace` : deux champs sans valeur par defaut, `request_id` et `pipeline` — les deux qui relient
  // l'ecran a la ligne de log (AD-10). `total_cost_eur` porte le cout affiche en pied (NFR4).
  function verifierTrace(t) {
    exigerObjet(t, "trace");
    exigerChaine(t.request_id, "trace.request_id");
    exigerChaine(t.pipeline, "trace.pipeline");
    defaut(t.total_cost_eur, "trace.total_cost_eur");
  }

  // Lecture **stricte** du contrat d'AD-11. Plus jamais `j.reponse` (le serveur rend `texte`), et
  // les sources affichees sont **celles du serveur** : `rechercher()` n'intervient plus ici.
  //
  // « Stricte » se lit litteralement, et sur le contrat **entier** : les trois champs obligatoires
  // de `ChatResponse` (`texte`, `answer`, `trace`), les deux booleens obligatoires d'`Answer`, les
  // invariants de son `_found_coherence`, les deux champs obligatoires de `Trace`, et les champs
  // obligatoires des objets imbriques que l'ecran consomme (`AbsenceProof.kind`,
  // `AnswerSegment.text`/`kind`, `SourceItem.block_id`/`quote`/`status`, `VerifiedClaim.quotes`…).
  // Une valeur par defaut a leur place peignait un corps incomplet en reponse, l'ajoutait a
  // l'historique et le faisait repartir au serveur : c'est la « reponse vide presentee comme
  // reponse » qu'AD-16 nomme. Un 200 incomplet n'est pas une reponse degradee, c'est un serveur
  // casse — donc `reponse_illisible`, comme un corps non-JSON, et sans bouton de repli (ce n'est pas
  // une indisponibilite au sens d'AD-11).
  //
  // Les champs a valeur par defaut restent tolerants a l'**absence** — leur defaut est defini par le
  // contrat lui-meme — mais ni au **mauvais type**, ni a `null` : voir le commentaire des trois
  // formes d'exigence, plus haut.
  function lireReponse(j) {
    var o = j || {};
    exigerChaine(o.texte, "texte");
    exigerObjet(o.answer, "answer");
    if (typeof o.answer.found !== "boolean") throw illisible("answer.found");
    if (typeof o.answer.complete !== "boolean") throw illisible("answer.complete");
    verifierAnswer(o.answer);
    verifierTrace(o.trace);
    var segments = verifierSegments(o.segments, "segments");
    var sources = verifierSources(o.sources, "sources");
    var fiches = listeDeChaines(o.fiches, "fiches");
    var unknown = listeDeChaines(o.unknown, "unknown");
    var comparateur = defaut(o.comparateur, "comparateur");
    if (comparateur !== undefined && typeof comparateur !== "boolean") throw illisible("comparateur");
    var via = defaut(o.via, "via");
    if (via !== undefined) exigerChaine(via, "via");
    return {
      texte: o.texte,
      segments: segments,
      sources: sources,
      fiches: fiches,
      unknown: unknown,
      comparateur: comparateur === true,
      answer: o.answer,
      via: via === undefined ? "api/v1" : via,
      trace: o.trace
    };
  }

  // Le champ fautif ne va **pas** a l'ecran : `messageErreur()` compose la phrase depuis le seul
  // `code`. Il voyage dans l'erreur pour le harnais de test et la console du developpeur.
  function illisible(champ) {
    var e = erreurChat({ kind: "requete", code: "reponse_illisible", statut: 200 });
    e.champ = champ;
    return e;
  }

  function testerApi() {
    if (apiDisponible !== null) return Promise.resolve(apiDisponible);
    // La sonde vaut sur **toute** origine http(s) : le serveur qui sert cette page sert aussi l'API
    // (AD-12). L'ancienne garde « hors localhost, inutile d'essayer » eteignait le mode api
    // partout ailleurs — c'est-a-dire en production. Une page ouverte en `file://`, elle, n'a
    // aucun serveur a sonder.
    if (!enLigne()) { apiDisponible = false; return Promise.resolve(false); }
    // La sonde est ce qui **rapporte les seuils** : la premiere question l'attend (voir
    // `reponseApi`). Elle doit donc etre bornee, sinon un `/sante` qui pend verrouillerait la
    // saisie sans fin. La marge d'abandon du client suffit largement pour un simple etat de sante.
    var options = { method: "GET" };
    var ctrl = (typeof AbortController === "function") ? new AbortController() : null;
    if (ctrl) options.signal = ctrl.signal;
    var minuteur = ctrl
      ? setTimeout(function () { ctrl.abort(); },
                   Math.round(seuil("client_abort_margin_s", MARGE_ABANDON_S_REPLI) * 1000))
      : null;
    function finir() { if (minuteur !== null) clearTimeout(minuteur); }
    return fetch(API_BASE + "/sante", options)
      .then(function (r) { finir(); return r.ok ? r.json() : null; })
      .then(function (j) {
        apiDisponible = !!(j && j.ok);
        // Les seuils actifs du serveur, servis a chaque chargement de page : le front s'en sert
        // plutot que de recopier `config.py`.
        if (j && j.thresholds) seuilsServeur = j.thresholds;
        return apiDisponible;
      })
      .catch(function () { finir(); apiDisponible = false; return false; });
  }

  function reponseApi(question, profil, historique) {
    if (!enLigne()) {
      return Promise.reject(erreurChat({ kind: "indisponible", code: "hors_ligne", statut: 0 }));
    }
    // La sonde porte les seuils du serveur (`historique_max_turns`, `deadline_s`,
    // `client_abort_margin_s`). Sans cette attente, la **premiere** requete partait sur les replis
    // ecrits ici et ignorait une configuration differente — un serveur regle a 3 tours recevait
    // les 6 du repli, donc un 400. `testerApi()` est memoise : les requetes suivantes ne coutent
    // rien, et son resultat n'ouvre aucun repli (il ne sert qu'a lire les seuils).
    return testerApi().then(function () { return envoyerRequete(question, profil, historique); });
  }

  function envoyerRequete(question, profil, historique) {
    var options = {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question: question,
        // AD-11 : le profil part **brut**. `decrireProfil()` ne sert plus qu'a l'affichage — la
        // phrase qu'il compose n'est pas un objet, et le contrat en attend un.
        profil: profil || {},
        historique: historiquePourApi(historique, question)
        // Plus de `contexte` : il recopiait le corps de quatre fiches (couramment > 20 ko contre un
        // `request_max_bytes` de 65 536) que le serveur, `extra="ignore"`, ne lit jamais. Du poids
        // et un risque de 413 pour zero effet.
      })
    };
    // Sans borne de temps, une requete qui pend laisse l'attente affichee et la saisie verrouillee
    // sans fin. La deadline du serveur (`deadline_s`) plus une marge : au-dessous on couperait une
    // requete a laquelle il aurait repondu.
    var ctrl = (typeof AbortController === "function") ? new AbortController() : null;
    if (ctrl) options.signal = ctrl.signal;
    var minuteur = ctrl ? setTimeout(function () { ctrl.abort(); }, delaiAbandonMs()) : null;
    function finir() { if (minuteur !== null) clearTimeout(minuteur); }

    return fetch(API_BASE + "/chat", options).then(function (r) {
      finir();
      if (!r.ok) {
        return r.json().then(function (j) { return j; }, function () { return null; })
          .then(function (j) { throw erreurHttp(r.status, r.headers, j); });
      }
      return r.json().then(lireReponse, function () {
        // 200 dont le corps n'est pas lisible : le serveur est casse, mais ce n'est pas une
        // indisponibilite au sens d'AD-11 — pas de bouton de repli.
        throw erreurChat({ kind: "requete", code: "reponse_illisible", statut: r.status });
      });
    }, function () {
      finir();
      // Un abandon est bien une indisponibilite : le serveur n'a pas repondu a temps.
      throw erreurChat({
        kind: "indisponible",
        code: (ctrl && ctrl.signal.aborted) ? "timeout_client" : "reseau",
        statut: 0
      });
    });
  }

  // ---------- Point d'entree unique ----------

  // Aucun repli, nulle part. Une erreur remonte **typee** a l'UI, qui decide quoi peindre — et,
  // pour une indisponibilite seulement, propose un bouton. Meme quand la sonde a deja dit
  // « indisponible » : c'est plus lent d'un clic, et c'est la seule version qui ne fait pas passer
  // une recherche de mots-cles pour une reponse verifiee.
  function repondre(question, profil, historique) {
    return reponseApi(question, profil, historique).then(function (r) {
      apiDisponible = true;
      return r;
    }, function (e) {
      if (e && e.kind === "indisponible") apiDisponible = false;
      throw e;
    });
  }

  return {
    CHAMPS: CHAMPS,
    afficher: afficher,
    // Les mots porteurs de sens d'une question, sans accents : servent au
    // surlignage du passage correspondant dans la fiche ouverte.
    termesUtiles: function (q) { return mots(q); },
    profilVide: profilVide,
    prochainChamp: prochainChamp,
    profilComplet: profilComplet,
    decrireProfil: decrireProfil,
    fichesPourProfil: fichesPourProfil,
    rechercher: rechercher,
    repondre: repondre,
    reponseLocale: reponseLocale,
    rechercheSimple: rechercheSimple,
    testerApi: testerApi,
    // Composition de ce que l'UI peint : pur, sans DOM, donc testable sans navigateur.
    historiquePourApi: historiquePourApi,
    citationsParSegment: citationsParSegment,
    statutTexte: statutTexte,
    preuveAbsence: preuveAbsence,
    coutTexte: coutTexte,
    etatReponse: etatReponse,
    messageErreur: messageErreur,
    // Les vues : l'arbre de ce qui doit etre peint. `ui.js` ne fait plus que le materialiser.
    vueAttente: vueAttente,
    vueReponse: vueReponse,
    vueReponseLocale: vueReponseLocale,
    vueErreur: vueErreur,
    modeApresErreur: modeApresErreur,
    libelleMode: libelleMode,
    setApiBase: function (u) { API_BASE = u; apiDisponible = null; },
    apiBase: function () { return API_BASE; },
    // Pour les tests : ce que le front croit des bornes du serveur, et d'ou il le tient.
    bornes: function () {
      return {
        historique_max_tours: historiqueMaxTours(),
        tour_max_caracteres: TOUR_MAX_CARACTERES,
        delai_abandon_ms: delaiAbandonMs(),
        seuils_du_serveur: seuilsServeur
      };
    }
  };
})();
