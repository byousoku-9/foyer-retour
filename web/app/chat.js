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

  // Bornes du contrat d'AD-11, cote client : elles evitent un 400 previsible.
  // `historique_max_turns` = 6 et `Turn.texte` <= 2000 (server/app/config.py, domain/question.py).
  var HISTORIQUE_MAX_TOURS = 6;
  var TOUR_MAX_CARACTERES = 2000;

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
  // Trois regles, dans cet ordre :
  //   1. le dernier tour utilisateur est exclu s'il est identique a `question` — le site pousse la
  //      question dans l'historique avant l'appel, et l'envoyer deux fois la ferait resoudre contre
  //      elle-meme ;
  //   2. on garde les plus recents (`historique_max_turns` = 6) — au-dela, le serveur rend 400,
  //      jamais une troncature ;
  //   3. un tour de plus de 2 000 caracteres est **ecarte**, jamais coupe : le couper changerait ce
  //      qui a ete dit. L'ecart se fait apres l'etape 2, donc il ne peut pas faire remonter un tour
  //      plus ancien a la place.
  function historiquePourApi(historique, question) {
    var tours = (historique || []).map(function (t) {
      return {
        role: t && t.role === "assistant" ? "assistant" : "user",
        texte: String((t && (t.texte !== undefined ? t.texte : t.content)) || "")
      };
    }).filter(function (t) { return t.texte.trim() !== ""; });

    var q = String(question || "").trim();
    if (tours.length && tours[tours.length - 1].role === "user" &&
        tours[tours.length - 1].texte.trim() === q) {
      tours = tours.slice(0, -1);
    }
    return tours.slice(-HISTORIQUE_MAX_TOURS).filter(function (t) {
      return t.texte.length <= TOUR_MAX_CARACTERES;
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

    var parClaim = {};
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
    if (status.edition) p.push("édition " + status.edition + " — actualité non vérifiée");
    return p.join(" · ");
  }

  function pluriel(n, mot) { return n + " " + mot + (n > 1 ? "s" : ""); }

  // AD-4 : la preuve chiffree d'une absence — termes **canoniques**, nombre de variantes, passages
  // parcourus. Jamais la liste des variantes ni des declencheurs : le contrat ne les transporte pas.
  function preuveAbsence(reason) {
    if (!reason) return "";
    var termes = (reason.terms_searched || []).filter(function (t) { return String(t || "").trim(); });
    var variantes = reason.variants_count || 0;
    var blocs = reason.blocks_scanned || 0;
    var chiffres = [];
    if (variantes) chiffres.push(pluriel(variantes, "variante") + (variantes > 1 ? " essayées" : " essayée"));
    if (blocs) chiffres.push(pluriel(blocs, "passage") + (blocs > 1 ? " parcourus" : " parcouru"));
    if (!termes.length && !chiffres.length) return "";
    if (!termes.length) return chiffres.join(", ");
    var debut = "Termes cherchés : " + termes.join(", ");
    return chiffres.length ? debut + " — " + chiffres.join(", ") : debut;
  }

  // NFR4 : le cout reel de la reponse, en pied de reponse. Il vient de l'usage rendu par l'API
  // (`trace.total_cost_eur`), jamais d'une estimation du front.
  function coutTexte(trace) {
    var c = trace && typeof trace.total_cost_eur === "number" ? trace.total_cost_eur : null;
    if (c === null || isNaN(c)) return "";
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

  // Lecture **stricte** du contrat d'AD-11. Plus jamais `j.reponse` (le serveur rend `texte`), et
  // les sources affichees sont **celles du serveur** : `rechercher()` n'intervient plus ici.
  function lireReponse(j) {
    var o = j || {};
    return {
      texte: typeof o.texte === "string" ? o.texte : "",
      segments: Array.isArray(o.segments) ? o.segments : [],
      sources: Array.isArray(o.sources) ? o.sources : [],
      fiches: Array.isArray(o.fiches) ? o.fiches : [],
      unknown: Array.isArray(o.unknown) ? o.unknown : [],
      comparateur: o.comparateur === true,
      answer: o.answer || null,
      via: typeof o.via === "string" ? o.via : "api/v1",
      trace: o.trace || null
    };
  }

  function testerApi() {
    if (apiDisponible !== null) return Promise.resolve(apiDisponible);
    // La sonde vaut sur **toute** origine : le serveur qui sert cette page sert aussi l'API
    // (AD-12). L'ancienne garde « hors localhost, inutile d'essayer » eteignait le mode api
    // partout ailleurs — c'est-a-dire en production.
    return fetch(API_BASE + "/sante", { method: "GET" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) { apiDisponible = !!(j && j.ok); return apiDisponible; })
      .catch(function () { apiDisponible = false; return false; });
  }

  function reponseApi(question, profil, historique) {
    return fetch(API_BASE + "/chat", {
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
    }).then(function (r) {
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
      throw erreurChat({ kind: "indisponible", code: "reseau", statut: 0 });
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
    setApiBase: function (u) { API_BASE = u; apiDisponible = null; },
    apiBase: function () { return API_BASE; }
  };
})();
