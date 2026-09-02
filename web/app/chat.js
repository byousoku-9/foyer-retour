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
  var DEADLINE_SERVEUR_REPLI = 100;     // config.deadline_s
  // Marge au-dessus de la deadline du serveur avant que le navigateur n'abandonne : en dessous, on
  // couperait une requete a laquelle il aurait repondu ; bien au-dela, l'utilisateur attendrait
  // pour rien. Ce n'etait pas un seuil du serveur, c'en est un : `config.client_abort_margin_s`,
  // publie par `/sante`. Ce qui reste ici n'est, comme les deux autres, qu'un **repli**.
  var MARGE_ABANDON_S_REPLI = 10;       // config.client_abort_margin_s
  // `Turn.texte <= 2000` (server/app/domain/question.py) n'est **pas** dans `thresholds()` : c'est
  // une contrainte de schema, pas un seuil de configuration. Elle reste donc ecrite ici, et un test
  // l'amarre a `Turn.model_fields["texte"]` pour qu'une divergence soit bruyante.
  var TOUR_MAX_CARACTERES = 2000;
  // Contrat de `Answer.lang`, amarré à `domain/langue.py` par `tests/test_web_chat.py` comme la
  // borne d'un tour l'est au modèle `Turn`. Ce n'est pas une liste de détection : ce sont les
  // seules langues qu'un 200 du serveur peut annoncer comme langue de réponse.
  var LANGUES_SERVIES = ["fr", "en", "de", "pt"];
  var seuilsServeur = {};
  // Ce que la sonde a dit du **niveau de validation** du corpus servi (story 1.10, reprise de 1.7).
  // `null` tant qu'elle n'a pas repondu, ou quand sa reponse n'etait pas lisible : le badge ne
  // suffixe alors rien, plutot que d'annoncer un niveau que personne n'a lu.
  var validationServeur = null;

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

  // Ce que l'assistant **a dit**, tel que l'historique de page doit le conserver (story 2.2).
  //
  // Jusqu'ici la page ne poussait que `Answer.texte`. Quand *comprendre* demande une precision
  // (AD-5 : deux sorties typees exclusives, `ClarificationRequise` n'est jamais une
  // `question_resolue` reconstituee), `texte` est la phrase generique de refus — « Je n'ai pas pu
  // determiner a quoi votre question fait reference… » — et **la question posee**
  // (`Answer.clarification`) n'entrait nulle part. Au tour suivant, *comprendre* recevait donc un
  // historique ou sa propre question ne figurait pas, la reponse d'un mot de l'arrivant (« du permis
  // de conduire ») restait irresoluble, et on lui reposait la meme question. C'est le defaut que
  // cette story corrige, et la mesure du 2026-08-25 (§ Design Notes de la spec 2.2) montre que la
  // boucle se referme des que l'historique porte la question.
  //
  // Le tour conserve est ce que l'assistant a **dit** : sa phrase et, le cas echeant, la question
  // qu'il a posee — la clarification d'abord, dans l'ordre ou `vueReponse` les peint. Ce n'est pas
  // l'ecran (revue 2.2, P10) : la page ajoute autour la preuve chiffree d'AD-4 et le bloc « Ce que
  // je ne sais pas », qui sont des annexes de l'interface, pas des phrases de l'assistant, et qui
  // n'ont donc rien a faire dans un historique de conversation. La phrase generique, elle, reste :
  // elle coute 94 caracteres et dit au modele que l'assistant n'a **pas** repondu ; la question
  // seule laisserait croire a un echange normal. Une reponse ordinaire (`clarification` nulle) rend
  // `texte` inchange, octet pour octet.
  //
  // **La borne d'un tour (`Turn.texte <= 2 000`) se decide ici, pas au moment de l'envoi** (revue
  // 2.2, P1, mesure a l'appui). Rien ne borne `ClarificationRequise.clarification` cote serveur, et
  // `comprendre_max_tokens` vaut 1 024 : une clarification de 1 968 caracteres composait un tour de
  // 2 063 (la phrase generique en ajoute 94, plus l'espace). `historiquePourApi` ecarte alors ce
  // tour **et tout ce qui le precede** — sa regle « un tour qu'on ne peut pas envoyer casse la
  // chaine » —, si bien que *comprendre* recevait un historique vide avec « du permis de conduire »
  // et reposait la meme question : la boucle que cette story referme se rouvrait en silence.
  //
  // On compose donc avec les morceaux **entiers** qui tiennent, jamais en coupant un morceau
  // (regle de la maison, specs 1.8 D8 et 1.9 D4 : hors borne ⇒ ecarte, jamais tronque — couper
  // changerait ce qui a ete dit). La **clarification** est prioritaire : c'est elle, et elle seule,
  // qui rend le tour suivant resoluble. Si elle ne tient pas a elle seule, le tour redevient
  // `texte` : la question est perdue, mais l'echange reste envoyable et le fil ne se coupe pas.
  //
  // Fonction pure, sans DOM, donc testable sans navigateur : `ui.js` pose ce tour, il ne decide pas
  // de ce que l'assistant a dit. Un tour vide n'est pas fabrique (`historiquePourApi` le filtre
  // deja) et rien ici ne contourne ses regles — la marque `local` d'AD-11/FR11 et la coupe de la
  // queue restent les siennes ; un `texte` seul plus long que la borne reste rendu tel quel, pour
  // que ce soit encore elle qui tranche.
  function tourAssistant(r) {
    var reponse = r || {};
    var clarification = String((reponse.answer || {}).clarification || "");
    var texte = String(reponse.texte || "");
    var question = clarification.trim() === "" ? "" : clarification;
    var phrase = texte.trim() === "" ? "" : texte;
    if (!question) return phrase;
    if (!phrase) return question;
    if (question.length + 1 + phrase.length <= TOUR_MAX_CARACTERES) return question + " " + phrase;
    if (question.length <= TOUR_MAX_CARACTERES) return question;
    return phrase;
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
    if (c === null || !isFinite(c) || c < 0) return "";
    // Un total nul veut dire qu'aucun appel n'a ete facture (court-circuit avant tout appel, ou
    // reponse entierement servie du cache) : « 0,0000 € » ferait croire a un arrondi.
    if (c === 0) return "cette réponse n'a rien coûté (aucun appel facturé)";
    return "cette réponse a coûté " + c.toFixed(4).replace(".", ",") + " €";
  }

  // Story 4.2f : ce que la lecture a couvert, chiffre. C'est le pendant de `preuveAbsence()` pour le
  // second porteur d'un `found=false` — et la difference entre les deux est tout le sujet : une
  // preuve d'absence annonce des passages **parcourus** (le document entier), celle-ci annonce des
  // passages **lus**. Les compteurs s'affichent meme a zero, pour la meme raison que la preuve : « la
  // navigation n'a rien fait entrer » et « douze passages sont partis au modele sans qu'aucune
  // affirmation ne tienne » sont deux situations differentes.
  function lectureLue(lecture) {
    if (!lecture) return "";
    var noeuds = entier(lecture.nodes_read);
    var blocs = entier(lecture.blocks_read);
    return "Lecture partielle : " +
      pluriel(noeuds, "section") + (noeuds > 1 ? " lues" : " lue") + ", " +
      pluriel(blocs, "passage") + " transmis au modèle" +
      " — le reste n'a pas été lu, et rien n'en est affirmé";
  }

  // FR5 : les etats, lus sur les deux booleens que *verifier* calcule (AD-4) et sur le porteur qui
  // accompagne un `found=false`. Story 4.2f : « inconnu » et « lecture partielle » ne disent pas la
  // meme chose — le premier est un refus fonde sur une recherche menee a son terme, le second dit
  // que la lecture s'est arretee avant de conclure. Les confondre sous un seul badge redonnerait a
  // l'utilisateur l'exhaustivite que la troncature dement.
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

  // FR5 / UX-DR : l'etat ne peut pas rester un mot nu. « PARTIEL » seul est un badge orange qui ne
  // dit ni ce qui manque ni ou le lire ; « INCONNU » seul ressemble a une panne. La phrase qui suit
  // le badge nomme ce que l'etat engage, et **renvoie a ce qui est deja peint** — la liste « Ce que
  // je ne sais pas » quand elle est la, la preuve d'absence quand elle l'est.
  //
  // Elle est composee **ici**, par le code, jamais par le modele (AD-16 / NFR2) ; et elle ne decrit
  // que ce que la vue contient reellement : `contexte` est renseigne par `vueReponse()` a partir des
  // blocs qu'elle vient de poser. Une clarification, par exemple, n'a pas de preuve chiffree
  // (AD-4) — lui promettre « la preuve est ci-dessus » designerait un bloc absent.
  //
  // Deux prudences, l'une et l'autre exigees par la revue de la story 2.3 :
  //
  // 1. **Aucune phrase ne nomme le document.** C'est la meme regle que *verifier* s'impose pour les
  //    phrases de lacune qu'il depose dans `unknown[]` : la page sinistre rend le meme pied et la
  //    meme section, sur un contrat et non sur le guide. Un « du guide » ecrit ici obligerait a
  //    reecrire la phrase la-bas, et les deux jeux divergeraient au premier changement.
  // 2. **Le repli du « partiel » n'affirme aucune cause.** L'incompletude nait de six causes
  //    (facettes non couvertes, lecture bornee, renvoi non resolu, phrases ecartees, relance
  //    abandonnee, limite declaree) ; sans la liste, la page ne sait pas laquelle. Elle dit donc ce
  //    qu'elle sait — il manque quelque chose, et rien n'indique quoi — et rien de plus.
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
      // Story 4.2f. La phrase dit les deux moitiés du fait, et **aucune** absence : la lecture a été
      // bornée, et rien de ce qui a été lu n'a tenu. Elle ne décrit que ce que la vue a réellement
      // peint — comme les trois autres : le chiffre n'est promis « ci-dessus » que s'il y est.
      var lu = c.lecture
        ? "ma lecture s'est arrêtée avant de conclure : ce qui a été lu est chiffré ci-dessus"
        : "ma lecture s'est arrêtée avant de conclure, sans que ce qui a été lu soit chiffré";
      return c.liste ? lu + ", et ce qui manque est listé sous « Ce que je ne sais pas »" : lu;
    }
    return c.preuve
      ? "rien n'a été retenu : la preuve de cette absence est ci-dessus"
      : "rien n'a été cherché : la question doit d'abord être précisée";
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
  // `attrs` (story 2.5) est un objet **plat** de chaines, pose tel quel par `ui.js::materialiser`
  // avec `setAttribute`. Il n'existe que pour ce que le texte ne peut pas dire : un `aria-hidden`
  // sur le pictogramme d'un controle, dont la valeur est deja ecrite en toutes lettres a cote —
  // sans quoi un lecteur d'ecran annoncerait « coche » puis « reussi ». Aucun `id` n'y entre :
  // l'arbre est peint **deux fois** (l'onglet Assistant et le widget), et un `id` unique en double
  // est un document invalide (`materialiser` l'ecarte, et un test l'exige des vues).
  function noeud(tag, cls, texte, enfants, attrs) {
    var n = { tag: tag };
    if (cls) n.cls = cls;
    if (texte !== undefined && texte !== null) n.texte = String(texte);
    if (enfants && enfants.length) n.enfants = enfants;
    if (attrs) n.attrs = attrs;
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

  // ---------- « Pourquoi cette réponse » (story 2.5) ----------
  //
  // AD-10 pose que la trace est « consultable » ; jusqu'ici le front n'en lisait que
  // `total_cost_eur`. Tout le reste — étapes, tiers, durées, blocs ouverts et écartés, contrôles
  // passés et échoués, relances, troncatures, seuils actifs, gate du document, état du dictionnaire
  // — voyageait sur le fil sans qu'aucun écran ne le montre.
  //
  // **Ce que la trace ne dit pas, le panneau ne le dit pas** (AD-16). Chaque rubrique naît de la
  // présence de son champ : une trace sans `gate` n'affiche pas « gate : inconnu », elle n'affiche
  // pas la rubrique. Aucun défaut n'est présenté comme une mesure, aucun titre de fiche n'est
  // deviné — un `block_id` que `trace.blocs` ne résout pas s'affiche **seul**.

  // Les alertes du serveur, en français. Cette table est **la même**, mot pour mot, que celle de
  // `tools/accueil/accueil.js` : les deux pages sont autonomes par décision (D8), elles ne peuvent
  // pas partager un module, et `tests/test_tables_partagees.py` les rejoue côte à côte pour qu'une
  // dérive rougisse. Une alerte inconnue n'est **pas** traduite : elle se dit telle quelle (M14).
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

  // Les `CheckResult.name` du serveur, en français. Un nom **inconnu** n'est jamais masqué : il
  // s'affiche tel quel — le panneau répond de l'honnêteté du reste, il serait le pire endroit où
  // taire un contrôle parce que le front ne le connaît pas encore.
  var CONTROLES = {
    applicabilite_contradictoire: "deux jeux de champs d'applicabilité pour une même affirmation",
    applicabilite_hors_borne: "des libellés d'applicabilité dépassent leur borne",
    applicabilite_incomplete: "applicabilité non rendue pour une clause décisionnelle",
    candidats_non_ouverts: "des passages trouvés n'ont pas été ouverts par la navigation",
    citations: "citations relues dans le corpus",
    claims_non_citees: "affirmations vérifiées qu'aucune phrase affichée ne reprend",
    clarification_refus_neutralisee:
      "clarification conservée : la compréhension portait aussi une intention refusée",
    clarification_retablie_perimetre_tronque:
      "clarification servie : la liste tronquée ne permet pas de confirmer le refus hors périmètre",
    clarification_langue_non_affirmee: "clarification retirée : sa langue n'est pas affirmable",
    cout_eleve: "coût de la requête au-dessus du seuil",
    demande_cible_inconnue: "le contrôle a demandé un contexte que rien de ce qui lui a été soumis ne désigne",
    demande_contexte: "le contrôle a demandé le contexte qui lui manquait pour juger une affirmation",
    demande_hors_vocabulaire: "une demande de contexte hors du vocabulaire fermé : aucune demande formée",
    demande_insatisfaite: "le contexte demandé n'a pas pu être rouvert : aucune relecture",
    demande_satisfaite: "le contexte demandé a été rouvert dans le contrat, sans appel modèle",
    dictionnaire: "variantes du dictionnaire ajoutées aux termes cherchés",
    faq: "formulation de FAQ reconnue comme candidate",
    facettes_non_couvertes: "des sous-questions posées ne sont pas couvertes",
    fait_cite_hors_sujet: "un fragment cité pour une qualité n'en emploie aucun mot",
    fait_cite_introuvable: "une qualité dite établie ne cite aucun fragment des faits déclarés",
    faits_compris_hors_borne: "des faits compris dépassent leur borne",
    hors_perimetre_desarme: "refus hors périmètre désarmé : la liste des rubriques était tronquée",
    intention_expliquee: "intention rendue par le modèle, et déclencheurs qui la confirment",
    lecture_partielle: "lecture bornée sans affirmation retenue : ce qui a été lu est chiffré, aucune absence n'est affirmée",
    libelles_hors_borne: "des libellés de portée dépassent leur borne",
    lignes_incompletes: "un bloc cité n'est pas la concaténation de ses lignes",
    limites_non_affichees: "des phrases de limite n'ont pas été affichées",
    noeuds_du_profil: "fiches désignées par le profil déclaré",
    parse_retry: "réponse du modèle relancée après un parse invalide",
    claims_hors_borne_ecartees: "des affirmations au-delà de la borne de rédaction ont été écartées",
    corrections_non_retenues: "des corrections de la relance dépassaient la borne : écartées après les acquis",
    limites_non_reconduites: "des réserves n'ont pas pu être reconduites sous la borne de segments",
    pertinence_incomplete: "des affirmations sont restées sans verdict de pertinence",
    qualite_de_la_clause_non_enumeree: "une qualité écrite par la clause n'a pas été énumérée",
    qualite_exigee_non_etablie: "une qualité exigée par une clause n'est pas établie par les faits",
    qualites_non_enumerees: "les qualités exigées ou établies n'ont pas été énumérées",
    quote_trop_longue: "des citations vérifiées dépassent la longueur maximale",
    raison_hors_vocabulaire: "une raison de rejet hors du vocabulaire fermé écarte l'affirmation",
    refus: "refus composé, avec sa preuve d'absence",
    repli_deterministe: "navigation par outils incomplète : repli déterministe borné",
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
    verdict_semantique: "la navigation n'a pas conclu dans la forme attendue (la lecture, elle, n'a pas été bornée)",
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

  // Les quatre `rejection_kind` d'AD-4, en français. La **citation** d'une affirmation écartée n'est
  // jamais affichée : une claim `non_retrouvee` ou `ambigue` porte la chaîne du modèle, qu'aucun
  // corpus n'a confirmée (AD-3, AD-11). Le motif, lui, est dû.
  var REJETS = {
    non_retrouvee: "citation introuvable dans les passages relus",
    non_pertinente: "passage réel, mais jugé étranger à la question",
    ambigue: "citation présente à plusieurs endroits, ou impossible à situer",
    non_citee: "affirmation vérifiée qu'aucune phrase affichée ne reprend"
  };

  function motifRejet(kind) {
    var k = String(kind || "");
    return Object.prototype.hasOwnProperty.call(REJETS, k) ? REJETS[k] : "écartée par la vérification";
  }

  function tableau(v) { return Array.isArray(v) ? v : []; }

  function entierOuNull(v) {
    return (typeof v === "number" && isFinite(v) && Math.floor(v) === v) ? v : null;
  }

  /** Une ligne du panneau : un texte, éventuellement précédé d'un pictogramme d'état. */
  function ligne(texte, etat) {
    if (!etat) return noeud("li", "pq-ligne", String(texte));
    // Le pictogramme est **décoratif** : l'état est déjà écrit en toutes lettres dans le texte de la
    // ligne. `aria-hidden` évite qu'un lecteur d'écran annonce « coche » avant de le relire.
    return noeud("li", "pq-ligne", null, [
      noeud("span", etat === "ok" ? "pq-ok" : "pq-ko", etat === "ok" ? "✓" : "✗", null,
            { "aria-hidden": "true" }),
      noeud("span", "pq-txt", String(texte))
    ]);
  }

  function rubrique(titre, lignes) {
    if (!lignes.length) return null;
    return noeud("div", "pq-bloc", null, [
      noeud("strong", "pq-titre", titre),
      noeud("ul", "pq-liste", null, lignes)
    ]);
  }

  /** « comprendre · micro · 900 ms » — le tier absent se dit « aucun appel », il ne se devine pas. */
  function ligneEtape(s) {
    var parts = [String(s.name || "")];
    parts.push(typeof s.tier === "string" && s.tier ? s.tier : "aucun appel");
    var ms = entierOuNull(s.ms);
    if (ms !== null) parts.push(ms + " ms");
    return ligne(parts.join(" · "));
  }

  /**
   * Les blocs ouverts et les blocs écartés, dans l'ordre où les étapes les nomment.
   *
   * `trace.blocs` **résout** ce que les étapes nomment déjà (`{block_id, doc_id, node_id, fiche_id,
   * titre}`) ; un identifiant qu'elle ne résout pas s'affiche **seul** (M3). Rien n'est deviné : le
   * front n'a aucun moyen de retrouver le titre d'une fiche du corpus servi, et `kb.js` peut en
   * diverger.
   */
  function lignesDeBlocs(steps, blocs, champ) {
    var titres = Object.create(null);
    blocs.forEach(function (b) {
      if (estObjet(b) && typeof b.block_id === "string" && typeof b.titre === "string") {
        titres[b.block_id] = b.titre;
      }
    });
    var vus = Object.create(null);
    var out = [];
    steps.forEach(function (s) {
      tableau(s[champ]).forEach(function (id) {
        if (typeof id !== "string" || vus[id]) return;
        vus[id] = 1;
        out.push(ligne(titres[id] ? id + " — " + titres[id] : id));
      });
    });
    return out;
  }

  /** L'état du dictionnaire des variantes, et le sort du refus « zéro hit » (M12). */
  function lignesDictionnaire(d) {
    if (!estObjet(d)) return [];
    var out = [];
    if (typeof d.charge === "boolean") {
      out.push(ligne(d.charge ? "dictionnaire des variantes : chargé"
                              : "dictionnaire des variantes : aucun dictionnaire n'est chargé",
                     d.charge ? "ok" : "ko"));
    }
    if (typeof d.validated === "boolean") {
      out.push(ligne(d.validated ? "signé par un humain" : "signé par personne",
                     d.validated ? "ok" : "ko"));
    }
    if (typeof d.corpus_ok === "boolean") {
      out.push(ligne(d.corpus_ok ? "ses empreintes décrivent le corpus servi"
                                 : "ses empreintes ne décrivent pas le corpus servi",
                     d.corpus_ok ? "ok" : "ko"));
    }
    if (typeof d.court_circuit_actif === "boolean") {
      var raisonDesarmement = "l'état publié ne permet pas de l'armer";
      if (d.charge === false) {
        raisonDesarmement = "aucun dictionnaire n'est chargé";
      } else if (d.corpus_ok === false) {
        raisonDesarmement = "le dictionnaire ne décrit pas le corpus servi";
      } else if (d.validated === false) {
        raisonDesarmement = "le dictionnaire n'a pas de validation humaine";
      }
      out.push(ligne(d.court_circuit_actif
        ? "le refus « zéro hit » est armé : une question dont aucun terme ni aucune variante n'a " +
          "de passage est refusée avec sa preuve"
        : "le refus « zéro hit » est désarmé : une question sans aucun passage poursuit quand même " +
          "la recherche — " + raisonDesarmement,
        d.court_circuit_actif ? "ok" : "ko"));
    }
    return out;
  }

  /** Le gate du document interrogé, et les alertes que le serveur pose sur lui. */
  function lignesGate(g) {
    if (!estObjet(g)) return [];
    var out = [];
    if (typeof g.profile === "string" && g.profile) {
      var cases = entierOuNull(g.cases);
      out.push(ligne("profil de validation : " + g.profile +
                     (cases !== null ? " (" + cases + " cas)" : "")));
    } else if (g.profile === null && tableau(g.alerts).indexOf("sans_gate") === -1) {
      out.push(ligne("aucune question-témoin ne valide ce document", "ko"));
    }
    if (typeof g.countersigned === "boolean") {
      out.push(ligne(g.countersigned
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
      out.push(ligne(connue ? ALERTES[a] + " (" + a + ")" : a, "ko"));
    });
    return out;
  }

  /** Les seuils actifs, repliés dans un sous-panneau : ils sont nombreux et rarement lus. */
  function vueSeuils(thresholds) {
    if (!estObjet(thresholds)) return null;
    var noms = Object.keys(thresholds).sort();
    var lignes = [];
    noms.forEach(function (nom) {
      var v = thresholds[nom];
      if (typeof v !== "number" || !isFinite(v)) return;
      lignes.push(ligne(nom + " : " + String(v)));
    });
    if (!lignes.length) return null;
    return noeud("details", "pq-seuils", null, [
      noeud("summary", null, "Seuils actifs (" + lignes.length + ")"),
      noeud("ul", "pq-liste", null, lignes)
    ]);
  }

  /**
   * Le panneau replié « Pourquoi cette réponse », ou `null` si la réponse ne porte pas de trace.
   *
   * Il lit `r.trace` et `r.answer` — jamais le corpus, jamais `kb.js`, jamais un défaut. Le même
   * arbre est peint dans les deux journaux : aucun `id` n'y est posé.
   */
  function vuePourquoi(r) {
    var reponse = r || {};
    var t = reponse.trace;
    if (!estObjet(t)) return null;
    var a = reponse.answer || {};
    var steps = tableau(t.steps).filter(estObjet);
    var enfants = [noeud("summary", "pq-sum", "Pourquoi cette réponse")];

    var etapes = rubrique("Étapes", steps.map(ligneEtape));
    if (etapes) enfants.push(etapes);

    var ouverts = rubrique("Passages ouverts", lignesDeBlocs(steps, tableau(t.blocs), "opened_block_ids"));
    if (ouverts) enfants.push(ouverts);
    var ecartes = rubrique("Passages écartés, non lus par le modèle",
                           lignesDeBlocs(steps, tableau(t.blocs), "discarded_block_ids"));
    if (ecartes) enfants.push(ecartes);

    var controles = [];
    steps.forEach(function (s) {
      tableau(s.checks).forEach(function (c) {
        if (!estObjet(c) || typeof c.name !== "string") return;
        var detail = typeof c.detail === "string" && c.detail ? " — " + c.detail : "";
        controles.push(ligne(libelleControle(c.name) + detail, c.ok === true ? "ok" : "ko"));
      });
    });
    var vueControles = rubrique("Contrôles", controles);
    if (vueControles) enfants.push(vueControles);

    var rejetees = tableau(a.rejected_claims).filter(estObjet);
    if (rejetees.length) {
      // AD-3/AD-11 : le **texte** de l'affirmation et le motif, jamais sa citation — la quote d'une
      // claim écartée est restée une chaîne du modèle, qu'aucun corpus n'a confirmée.
      enfants.push(noeud("div", "pq-bloc", null, [
        noeud("strong", "pq-titre", "Affirmations écartées par la vérification"),
        noeud("p", "pq-note",
          "Le modèle a avancé ces affirmations ; les contrôles les ont écartées. Aucune de leurs " +
          "citations n'est affichée."),
        noeud("ul", "pq-liste", null, rejetees.map(function (c) {
          var kind = typeof c.rejection_kind === "string" ? c.rejection_kind : "";
          return noeud("li", "pq-ligne pq-rejetee", null, [
            noeud("span", "pq-rej-txt", String(c.text || "")),
            noeud("span", "pq-rej-motif", motifRejet(kind) + (kind ? " (" + kind + ")" : ""))
          ]);
        }))
      ]));
    }

    var compteurs = [];
    var retries = entierOuNull(t.retries);
    if (retries !== null) compteurs.push(ligne(pluriel(retries, "relance")));
    var troncatures = entierOuNull(t.truncations);
    if (troncatures !== null) compteurs.push(ligne(pluriel(troncatures, "troncature")));
    if (typeof t.deadline_remaining_s === "number" && isFinite(t.deadline_remaining_s)) {
      var delai = Math.abs(t.deadline_remaining_s).toFixed(1).replace(".", ",");
      compteurs.push(ligne(t.deadline_remaining_s < 0
        ? "délai dépassé de " + delai + " s"
        : "délai restant : " + delai + " s"));
    }
    var cout = coutTexte(t);
    if (cout) compteurs.push(ligne(cout));
    var vueCompteurs = rubrique("Ce que la requête a coûté", compteurs);
    if (vueCompteurs) enfants.push(vueCompteurs);

    var seuils = vueSeuils(t.thresholds);
    if (seuils) enfants.push(seuils);

    var gate = rubrique("Validation du document interrogé", lignesGate(t.gate));
    if (gate) enfants.push(gate);
    var dico = rubrique("Dictionnaire des variantes", lignesDictionnaire(t.dictionnaire));
    if (dico) enfants.push(dico);

    var identite = [];
    var pipeline = typeof t.pipeline === "string" && t.pipeline ? t.pipeline : "";
    var variante = typeof t.variant === "string" && t.variant ? t.variant : "";
    if (pipeline) identite.push(ligne("pipeline : " + pipeline + (variante ? " · variante " + variante : "")));
    if (typeof t.intent === "string" && t.intent) identite.push(ligne("intention : " + t.intent));
    if (typeof t.request_id === "string" && t.request_id) {
      identite.push(ligne("référence de requête : " + t.request_id));
    }
    var vueIdentite = rubrique("Cette requête", identite);
    if (vueIdentite) enfants.push(vueIdentite);

    // Un `<details>` qui n'a que son `<summary>` n'apprendrait rien : la trace n'a rien dit.
    if (enfants.length === 1) return null;
    return noeud("details", "pourquoi", null, enfants);
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

    // Story 4.2f : l'autre porteur, au même endroit et sous la même règle — le serveur écrit la
    // phrase, la page ajoute le chiffre. Les deux sont exclusifs par le contrat : jamais deux
    // paragraphes.
    var lecture = estObjet(a.lecture_partielle) ? lectureLue(a.lecture_partielle) : "";
    if (lecture) enfants.push(noeud("p", "lecture-partielle", lecture));

    var inconnus = (r && r.unknown) || [];
    if (inconnus.length) {
      enfants.push(noeud("div", "inconnu", null, [
        noeud("strong", null, "Ce que je ne sais pas"),
        noeud("ul", null, null, inconnus.map(function (x) { return noeud("li", null, String(x)); }))
      ]));
    }

    var etat = etatReponse(a);
    // Le badge, puis la phrase qui le rend explicite : elle se compose sur ce que cette vue vient de
    // poser (la liste des inconnues, la preuve d'absence), jamais sur ce que le corps promettait.
    var pied = [
      noeud("span", "etat etat-" + etat.cle, etat.texte),
      noeud("span", "etat-phrase",
        phraseEtat(etat, { liste: inconnus.length > 0, preuve: !!preuve, lecture: !!lecture }))
    ];
    // `Answer.lang` a un défaut pydantic (`fr`) : un corps minimal qui l'omet doit se peindre comme
    // le même objet sérialisé avec son défaut, sans inventer une traduction.
    if ((a.lang || "fr") !== "fr") {
      pied.push(noeud("span", "langue-mention",
        "traduit depuis le guide (français) — les passages cités restent tels qu'ils sont écrits"));
    }
    if (a.lang_fallback === true) {
      pied.push(noeud("span", "langue-mention langue-repli",
        "langue non prise en charge ou non détectée : réponse en français"));
    }
    // **Ce qui explique une réponse est montré ; ce qui la comptabilise ne l'est qu'à la demande.**
    // Le prix en euros a quitté le pied de la réponse : quelqu'un qui lit « dans quel délai dois-je
    // me déclarer à la commune » n'a rien à faire de « cette réponse a coûté 0,0278 € » — c'est de
    // la comptabilité d'équipe, posée sous les yeux de l'utilisateur. Il n'a pas disparu pour
    // autant : il est dans « Pourquoi cette réponse », rubrique « Ce que la requête a coûté », avec
    // les seuils, les compteurs et le gate — c'est-à-dire à l'endroit où l'on va quand on demande
    // **comment** la réponse a été faite. Même règle sur les pages d'audit d'ingestion, qui gardent
    // empreintes et identifiants de modèles : elles existent pour être recoupées.
    enfants.push(noeud("div", "pied", null, pied));

    // AD-10 : la trace est consultable. Le panneau vient **après** le pied — il explique ce qui
    // précède — et il n'existe que si la trace existe.
    var pourquoi = vuePourquoi(r);
    if (pourquoi) enfants.push(pourquoi);

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
  // `contexte.tour_retire` (story 2.5, M9) : `ui.js` a retiré de l'historique de page la question
  // restée sans réponse. Le retrait n'est **jamais silencieux** — la phrase est composée ici, comme
  // tout ce qui s'affiche, et `ui.js` ne fait que dire ce qu'il a fait.
  function vueErreur(erreur, question, contexte) {
    var indispo = !!(erreur && erreur.kind === "indisponible");
    var c = contexte || {};
    var enfants = [
      noeud("strong", "alerte-titre", indispo ? "Assistant indisponible" : "Question non traitée"),
      noeud("p", "alerte-txt", messageErreur(erreur))
    ];
    if (indispo) {
      enfants.push(noeud("p", "alerte-note",
        "Rien n'a été cherché : la recherche simple du guide compare des mots-clés, elle ne " +
        "vérifie rien. À vous de décider si elle vous suffit."));
    }
    if (c.tour_retire) {
      enfants.push(noeud("p", "retrait",
        "Cette question n'a pas reçu de réponse : elle est retirée de la conversation, et ne " +
        "repartira pas au serveur avec la suivante."));
    }
    if (erreur && erreur.request_id) {
      enfants.push(noeud("p", "ref", "référence : " + erreur.request_id));
    }
    if (indispo) {
      var bouton = noeud("button", "chip", "Consulter le guide en recherche simple");
      bouton.action = { nom: "recherche_simple", question: String(question || "") };
      enfants.push(noeud("div", "chips", null, [bouton]));
    }
    // AD-10 : « une erreur porte `trace` si le pipeline a commencé ». Quand l'enveloppe d'AD-16 en
    // porte une (et qu'elle tient le contrat, voir `erreurHttp`), le bandeau porte aussi le panneau
    // — c'est l'écran d'un échec qui a le plus besoin d'être expliqué.
    var pourquoi = vuePourquoi({ trace: erreur && erreur.trace, answer: null });
    if (pourquoi) enfants.push(pourquoi);
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
  //
  // Story 1.10 (reprise differee de 1.7, D10) : le badge du mode api porte desormais le **niveau de
  // validation** du corpus servi. `testerApi()` lisait `gate_profile` et `alerts` sur `/sante` et
  // les jetait, si bien que le badge disait « mode api » de la meme facon que le corpus soit valide
  // par des questions-temoins ou pas du tout — c'est-a-dire dans l'etat ou les reponses reposent sur
  // des documents que rien n'a mesures. `validation` est ce que la sonde a rendu, ou `null` tant
  // qu'elle n'a pas repondu (ou que sa reponse n'etait pas lisible) : **aucun** suffixe alors, jamais
  // un niveau par defaut (AD-11 : la bascule silencieuse est ce qu'on empeche).
  // Le suffixe dit aussi la **peremption** quand le serveur la signale (revue 1.10). Sans elle, le
  // badge ecrivait « mode api · vertical (2 cas) » pendant que `/sante` portait `gate_perime`,
  // c'est-a-dire pendant que les empreintes du gate ne sont plus celles de l'image qui repond. La
  // page d'accueil pose deja sa reserve dans ce cas ; le badge est le seul ecran ou l'on pose
  // reellement une question, et c'est celui qui l'affirmait a jour.
  function estPerime(validation) {
    return reserves(validation).indexOf("périmé") !== -1;
  }

  // Les réserves que le badge porte, dans l'ordre où le serveur les rend graves (story 2.5, M14).
  //
  // Reprise différée de 1.10 : `testerApi()` retenait `alerts` et le badge n'en disait qu'une, la
  // péremption du gate. Or **la quarantaine d'un document** (il n'est plus servi du tout) et
  // **l'absence de son fichier source** (l'édition annoncée n'est vérifiable nulle part) sont des
  // faits publiés par le serveur, qui portent sur ce à quoi les réponses s'adossent, dans le seul
  // écran où l'on pose une question. Elles ne sont pas déduites : `/sante` les nomme, le badge les
  // relaie. Une alerte que cette table ne connaît pas n'invente **aucune** réserve — c'est le sens
  // d'AD-16 : ce que le serveur n'a pas dit, l'écran ne le dit pas.
  var RESERVES = {
    quarantaine: "document écarté",
    source_absente: "source absente",
    gate_perime: "périmé"
  };

  function reserves(validation) {
    var alerts = (validation && Array.isArray(validation.alerts)) ? validation.alerts : [];
    var vues = {};
    var out = [];
    // L'ordre est celui de `RESERVES`, pas celui du serveur : deux corps qui portent les mêmes
    // alertes dans un ordre différent doivent écrire le même badge.
    Object.keys(RESERVES).forEach(function (nom) {
      for (var i = 0; i < alerts.length; i++) {
        if (alerts[i] && alerts[i].alerte === nom && !vues[nom]) {
          vues[nom] = 1;
          out.push(RESERVES[nom]);
        }
      }
    });
    return out;
  }

  // La **contresignature** entre dans le suffixe pour la meme raison que la peremption (revue Codex
  // 1.10 tour 2, B2) : AD-14 definit `vertical` comme « un cas guide et un cas sinistre relus a la
  // main », si bien que le seul nom du profil affirme au lecteur du badge une relecture humaine.
  // Tant que `gate_countersigned` est faux, cette relecture est celle de la boucle autonome. Le
  // badge est court : il pose la reserve, l'accueil la dit en toutes lettres.
  function suffixeValidation(validation) {
    if (!validation) return "";
    var reservees = reserves(validation);
    // Sans gate, il n'y a pas de niveau à suffixer — mais les réserves du serveur, elles, portent
    // sur le corpus servi et restent dues : « non validé » seul tairait une quarantaine.
    if (validation.gate_profile === null) {
      return " · non validé" + (reservees.length ? " (" + reservees.join(", ") + ")" : "");
    }
    if (validation.gate_countersigned === false) reservees.push("non contresigné");
    return " · " + validation.gate_profile + " (" + validation.gate_cases + " cas" +
      (reservees.length ? ", " + reservees.join(", ") : "") + ")";
  }

  function libelleMode(via, validation) {
    var m = String(via === null || via === undefined ? "" : via);
    if (!m) return { texte: "mode : vérification…", cls: "badge" };
    // Le suffixe ne vaut que pour le mode api : c'est le seul ou une reponse s'appuie sur le corpus
    // servi. « mode local · non validé » melangerait deux choses sans rapport.
    if (m.indexOf("api") === 0) {
      return { texte: "mode api" + suffixeValidation(validation), cls: "badge on" };
    }
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
    e.retry_after = (typeof d.retry_after === "number" && isFinite(d.retry_after) &&
                     d.retry_after >= 0) ? d.retry_after : null;
    e.request_id = d.request_id || "";
    // AD-10 : l'enveloppe d'erreur d'AD-16 porte `trace` quand le pipeline a commencé. Elle voyage
    // avec l'erreur pour que `vueErreur` puisse déplier « Pourquoi cette réponse » (M10).
    e.trace = d.trace || null;
    return e;
  }

  // La trace d'une **enveloppe d'erreur** n'a pas franchi `lireReponse` : elle est donc relue ici,
  // par la même lecture stricte, et écartée si elle ne tient pas le contrat. Un 503 ne devient pas
  // `reponse_illisible` pour autant — l'échec reste celui du serveur, il perd seulement son
  // panneau : « ce que la trace ne dit pas, l'écran ne le dit pas » (AD-16).
  function traceDErreur(corps) {
    var t = corps && corps.trace;
    if (!estObjet(t)) return null;
    try { verifierTrace(t); } catch (e) { return null; }
    return t;
  }

  function retryApres(valeur, maintenantMs) {
    if (typeof valeur !== "string") return null;
    var brut = valeur.trim();
    // RFC 9110 : `delay-seconds` vaut uniquement 1*DIGIT. `parseInt("30 secondes")` rendait 30
    // et transformait donc un en-tête invalide en mesure affichée.
    if (/^\d+$/.test(brut)) {
      var secondes = Number(brut);
      return isFinite(secondes) && Math.floor(secondes) === secondes ? secondes : null;
    }
    // La forme HTTP-date émise aujourd'hui est IMF-fixdate. La regex écarte les chaînes que
    // `Date.parse` devine avec complaisance ; la reconstruction élimine les dates impossibles.
    if (!/^(Mon|Tue|Wed|Thu|Fri|Sat|Sun), \d{2} (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{4} \d{2}:\d{2}:\d{2} GMT$/.test(brut)) return null;
    var date = Date.parse(brut);
    if (!isFinite(date) || new Date(date).toUTCString() !== brut) return null;
    var maintenant = typeof maintenantMs === "number" ? maintenantMs : Date.now();
    return Math.max(0, Math.ceil((date - maintenant) / 1000));
  }

  function erreurHttp(statut, entetes, corps) {
    var err = (corps && corps.error) || {};
    var retry = retryApres(entetes && entetes.get ? entetes.get("Retry-After") : null);
    return erreurChat({
      // Seul un 503 ouvre la porte du mode local (AD-11/AD-16). Un 500 n'est pas une
      // indisponibilite passagere que la recherche simple contournerait.
      kind: statut === 503 ? "indisponible" : "requete",
      code: typeof err.code === "string" ? err.code : "",
      statut: statut,
      retry_after: retry,
      request_id: typeof err.request_id === "string" ? err.request_id : "",
      trace: traceDErreur(corps)
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

  // `LecturePartielle` (story 4.2f) : le second porteur possible d'un `found=false`. Ses deux
  // compteurs sont **obligatoires** et non a valeur par defaut — cote serveur ils n'ont pas de
  // defaut non plus, et ce sont eux que l'ecran affiche. Un compteur absent, negatif ou non entier
  // peindrait « 0 section lue » sur une lecture que rien n'a mesuree : c'est le contraire meme de ce
  // que ce porteur promet, puisqu'il n'existe que pour **chiffrer** ce qui a ete lu.
  function verifierLecturePartielle(lp, nom) {
    exigerObjet(lp, nom);
    if (lp.nodes_read === undefined) throw illisible(nom + ".nodes_read");
    if (lp.blocks_read === undefined) throw illisible(nom + ".blocks_read");
    // Les deux compteurs ont un **plancher a 1**, comme le domaine : zero passage transmis est
    // l'erreur terminale d'AD-1/NFR2 (le budget n'a rien laisse passer), et zero section pour au
    // moins un passage est un etat impossible — AD-2 rattache chaque bloc a exactement un nœud.
    // Les accepter peignait « 0 section lue, N passages transmis » : deux chiffres qui se
    // contredisent, sous le porteur qui n'existe que pour chiffrer.
    if (entierDefaut(lp.nodes_read, nom + ".nodes_read") < 1) throw illisible(nom + ".nodes_read");
    if (entierDefaut(lp.blocks_read, nom + ".blocks_read") < 1) throw illisible(nom + ".blocks_read");
    listeDeChaines(lp.documents, nom + ".documents");
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
  // l'etat affiche (« sûr » / « partiel » / « lecture partielle » / « inconnu ») se calcule sur
  // `found`, `complete` et le porteur qui accompagne un `found=false` ; un `found=true` sans claim
  // peindrait « sûr » sur une reponse sans une seule citation relue, et un `found=false` sans aucun
  // porteur un refus que rien n'explique (story 4.2f : ils sont deux, exclusifs).
  function verifierAnswer(a) {
    var claims = verifierClaims(a.claims, "answer.claims");
    var unknown = listeDeChaines(a.unknown, "answer.unknown");
    verifierSegments(a.segments, "answer.segments");
    if (defaut(a.texte, "answer.texte") !== undefined) exigerChaine(a.texte, "answer.texte");
    var lang = defaut(a.lang, "answer.lang");
    if (lang !== undefined) {
      exigerChaine(lang, "answer.lang");
      if (LANGUES_SERVIES.indexOf(lang) === -1) throw illisible("answer.lang");
    }
    var langFallback = defaut(a.lang_fallback, "answer.lang_fallback");
    if (langFallback !== undefined && typeof langFallback !== "boolean") {
      throw illisible("answer.lang_fallback");
    }
    // Un repli de détection force `language="fr"` côté serveur. Annoncer simultanément une langue
    // traduite et un repli français ne tient donc pas le contrat, même si les deux types sont bons.
    if (langFallback === true && (lang || "fr") !== "fr") {
      throw illisible("answer.lang_fallback");
    }
    chaineNullable(a.clarification, "answer.clarification");
    // « found=False exige une preuve d'absence (reason) » — et cette preuve est un `AbsenceProof`
    // entier, pas un objet quelconque : c'est `reason.kind` qui decide de ce qui s'affiche.
    if (a.reason !== undefined && a.reason !== null) verifierPreuve(a.reason, "answer.reason");
    if (a.lecture_partielle !== undefined && a.lecture_partielle !== null) {
      verifierLecturePartielle(a.lecture_partielle, "answer.lecture_partielle");
    }
    // Story 4.2f : « exactement un porteur sur `found=false` », l'invariant du domaine refait ici.
    // Aucun des deux : un refus sans rien pour l'expliquer, l'ecran peindrait « inconnu » sur du
    // vide. Les deux : deux comptes rendus du meme refus, dont l'un — la preuve d'absence — annonce
    // le balayage exhaustif que l'autre dement. Sur `found=true` : une reponse retenue n'est pas une
    // lecture restee sans conclusion, et la page afficherait deux etats a la fois.
    if (!a.found && !estObjet(a.reason) && !estObjet(a.lecture_partielle)) {
      throw illisible("answer.reason");
    }
    if (estObjet(a.reason) && estObjet(a.lecture_partielle)) {
      throw illisible("answer.lecture_partielle");
    }
    // Et sur `found=true`, **aucun** des deux : ni preuve d'absence, ni lecture partielle. Ne
    // fermer que le second laissait peindre en meme temps une reponse « sûre » et la preuve
    // chiffree d'une absence, que `preuveAbsence()` rend juste sous les segments.
    if (a.found && estObjet(a.lecture_partielle)) throw illisible("answer.lecture_partielle");
    if (a.found && estObjet(a.reason)) throw illisible("answer.reason");
    // « Une lecture partielle dit ce qui lui manque » : le domaine l'exige, donc aucune route ne
    // peut servir ce corps. Peindre un compteur de lecture sans la moindre réserve donnerait à lire
    // « je n'ai pas tout lu » sans jamais dire ce qui manque — la moitié muette de la réponse.
    if (estObjet(a.lecture_partielle) && unknown.length === 0) throw illisible("answer.unknown");
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
  //
  // Story 2.5 : tout ce que le panneau « Pourquoi cette réponse » consomme est descendu, avec la
  // règle **tolérante à l'absence, stricte sur le type**. Elle n'est pas la même que celle des
  // champs à valeur par défaut du contrat HTTP, et c'est délibéré : les deux lots de cette story
  // sont écrits **en parallèle**, et le front ne peut pas exiger un champ que le serveur n'a pas
  // encore posé. Un champ absent fait donc disparaître sa rubrique (M2) ; un champ **présent et mal
  // typé**, lui, est un serveur cassé — l'afficher, ce serait peindre « 0 relance » sur un compteur
  // que rien n'a calculé, ou « étape [object Object] » sous le panneau qui répond de l'honnêteté du
  // reste. `null` n'est jamais accepté non plus : aucun de ces champs n'est nullable côté serveur,
  // sauf `gate` et `dictionnaire`, que `Trace` déclare `… | None`.
  function objetNullable(v, nom) {
    if (v === undefined || v === null) return;
    exigerObjet(v, nom);
  }

  function listeDObjets(v, nom) {
    if (v === undefined) return [];
    if (!Array.isArray(v)) throw illisible(nom);
    for (var i = 0; i < v.length; i++) exigerObjet(v[i], nom + "[" + i + "]");
    return v;
  }

  function verifierEtape(s, nom) {
    exigerChaine(s.name, nom + ".name");
    if (s.tier !== undefined && s.tier !== null) exigerChaine(s.tier, nom + ".tier");
    if (s.ms !== undefined) entierDefaut(s.ms, nom + ".ms");
    if (s.opened_block_ids !== undefined) {
      listeDeChaines(s.opened_block_ids, nom + ".opened_block_ids");
    }
    if (s.discarded_block_ids !== undefined) {
      listeDeChaines(s.discarded_block_ids, nom + ".discarded_block_ids");
    }
    var checks = listeDObjets(s.checks, nom + ".checks");
    for (var i = 0; i < checks.length; i++) {
      var ou = nom + ".checks[" + i + "]";
      exigerChaine(checks[i].name, ou + ".name");
      if (typeof checks[i].ok !== "boolean") throw illisible(ou + ".ok");
      if (checks[i].detail !== undefined) exigerChaine(checks[i].detail, ou + ".detail");
    }
  }

  function verifierBloc(b, nom) {
    exigerChaine(b.block_id, nom + ".block_id");
    exigerChaine(b.doc_id, nom + ".doc_id");
    exigerChaine(b.node_id, nom + ".node_id");
    // `titre` est ce que la ligne affiche à côté de l'identifiant ; `fiche_id` est nullable.
    exigerChaine(b.titre, nom + ".titre");
    chaineNullable(b.fiche_id, nom + ".fiche_id");
  }

  function verifierGate(g) {
    objetNullable(g, "trace.gate");
    if (!estObjet(g)) return;
    chaineNullable(g.profile, "trace.gate.profile");
    if (g.cases !== null) entierDefaut(g.cases, "trace.gate.cases");
    if (g.countersigned !== undefined && g.countersigned !== null &&
        typeof g.countersigned !== "boolean") {
      throw illisible("trace.gate.countersigned");
    }
    if (!Array.isArray(g.alerts)) throw illisible("trace.gate.alerts");
    listeDeChaines(g.alerts, "trace.gate.alerts");
  }

  function verifierDictionnaire(d) {
    objetNullable(d, "trace.dictionnaire");
    if (!estObjet(d)) return;
    ["charge", "validated", "corpus_ok", "court_circuit_actif"].forEach(function (cle) {
      if (typeof d[cle] !== "boolean") {
        throw illisible("trace.dictionnaire." + cle);
      }
    });
  }

  function verifierTrace(t) {
    exigerObjet(t, "trace");
    exigerChaine(t.request_id, "trace.request_id");
    exigerChaine(t.pipeline, "trace.pipeline");
    var cout = defaut(t.total_cost_eur, "trace.total_cost_eur");
    if (cout !== undefined &&
        (typeof cout !== "number" || !isFinite(cout) || cout < 0)) {
      throw illisible("trace.total_cost_eur");
    }
    if (t.variant !== undefined) exigerChaine(t.variant, "trace.variant");
    if (t.intent !== undefined && t.intent !== null) exigerChaine(t.intent, "trace.intent");
    var steps = listeDObjets(t.steps, "trace.steps");
    for (var i = 0; i < steps.length; i++) verifierEtape(steps[i], "trace.steps[" + i + "]");
    var blocs = listeDObjets(t.blocs, "trace.blocs");
    for (var b = 0; b < blocs.length; b++) verifierBloc(blocs[b], "trace.blocs[" + b + "]");
    verifierGate(t.gate);
    verifierDictionnaire(t.dictionnaire);
    if (t.retries !== undefined) entierDefaut(t.retries, "trace.retries");
    if (t.truncations !== undefined) entierDefaut(t.truncations, "trace.truncations");
    if (t.deadline_remaining_s !== undefined && t.deadline_remaining_s !== null &&
        (typeof t.deadline_remaining_s !== "number" || !isFinite(t.deadline_remaining_s))) {
      throw illisible("trace.deadline_remaining_s");
    }
    // `thresholds` est un `dict` sans `| None` : présent, c'est un objet ; `null` est un corps que
    // personne n'a écrit.
    if (t.thresholds !== undefined) {
      exigerObjet(t.thresholds, "trace.thresholds");
      Object.keys(t.thresholds).forEach(function (nom) {
        var v = t.thresholds[nom];
        if (typeof v !== "number" || !isFinite(v)) {
          throw illisible("trace.thresholds." + nom);
        }
      });
    }
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

  // Le niveau de validation, lu **strictement** sur le corps de `/sante` (story 1.10).
  //
  // Meme regle qu'ailleurs depuis la revue Codex 1.9 : une cle **absente** n'est pas « le champ vaut
  // null ». `routes/sante.py` publie avec le defaut de FastAPI, donc pydantic serialise toujours la
  // cle, `null` compris ; un corps sans `gate_profile` n'a pas ete ecrit par cette route. Et les deux
  // champs sont **lies** cote serveur (`EtatApp.gate_cases` rend `null` des que `gate_profile` l'est)
  // : un corps qui les dissocie ferait afficher « vertical (null cas) », c'est-a-dire une phrase que
  // le serveur n'a jamais dite. Dans les deux cas : `null`, donc aucun suffixe.
  function lireValidation(j) {
    if (!j || typeof j !== "object" || Array.isArray(j)) return null;
    var p = j.gate_profile;
    var n = j.gate_cases;
    var c = j.gate_countersigned;
    // **La** regle du couple (profil, compte), identique a celle de `tools/accueil/accueil.js` —
    // `tests/js/sante_corpus.mjs` rejoue les memes corps dans les deux lecteurs et exige le meme
    // verdict, corps par corps (un grep sur deux sources ne verifie pas une semantique) :
    //   - `gate_profile` : chaine **non vide**, ou `null` — « mode api ·  (2 cas) » ne dit rien ;
    //   - `gate_cases` : entier **>= 1**, ou `null` — `evals/run.py` refuse de tourner sur zero cas,
    //     donc aucun gate ne porte `cases: 0`, et « vertical (0 cas) » ne peut pas etre produit ;
    //   - `gate_countersigned` : booleen, ou `null` — c'est lui, et non le nom du profil, qui dit
    //     si la relecture qu'AD-14 met dans la definition de `vertical` a ete contresignee ;
    //   - les trois nuls ou les trois non nuls (`EtatApp.gate_cases` et
    //     `EtatApp.gate_countersigned` rendent `null` des que `gate_profile` l'est).
    // Ce que la regle refuse ne suffixe **rien** : jamais un niveau a moitie lu.
    if (!(p === null || (typeof p === "string" && p.length > 0))) return null;
    if (!(n === null || (typeof n === "number" && isFinite(n) && Math.floor(n) === n && n >= 1))) {
      return null;
    }
    if (!(c === null || typeof c === "boolean")) return null;
    if ((p === null) !== (n === null)) return null;
    if ((p === null) !== (c === null)) return null;
    // Les alertes sont lues **sans conditionner** le niveau : une entree mal formee ne doit pas
    // supprimer un `gate_profile` parfaitement lisible (le badge perdrait tout son suffixe pour un
    // champ qu'il n'affiche meme pas). Ce qui n'est pas lisible est ecarte, le reste est retenu.
    var alerts = [];
    var brutes = Array.isArray(j.alerts) ? j.alerts : [];
    for (var i = 0; i < brutes.length; i++) {
      var a = brutes[i];
      if (a && typeof a === "object" && typeof a.alerte === "string" &&
          typeof a.doc_id === "string") {
        alerts.push({ doc_id: a.doc_id, alerte: a.alerte });
      }
    }
    return { gate_profile: p, gate_cases: n, gate_countersigned: c, alerts: alerts };
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
    // `finir()` n'est appele qu'au **reglement** de la promesse, jamais a la reception des en-tetes :
    // `r.json()` attend le corps, et couper la minuterie plus tot laissait un serveur qui envoie un
    // 200 puis bloque sur le corps pendre sans borne — la premiere question attend `testerApi()`
    // (voir `reponseApi`), donc la saisie restait verrouillee indefiniment, contre la borne annoncee
    // (AD-11/AD-16). Meme forme que `tools/accueil/accueil.js::sonder()` (revue Codex 1.10, I1).
    return fetch(API_BASE + "/sante", options)
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) {
        finir();
        apiDisponible = !!(j && j.ok);
        // Les seuils actifs du serveur, servis a chaque chargement de page : le front s'en sert
        // plutot que de recopier `config.py`.
        if (j && j.thresholds) seuilsServeur = j.thresholds;
        // Et le niveau de validation du corpus servi (story 1.10) : la sonde le publie deja, il
        // etait lu puis jete (reprise differee de 1.7).
        validationServeur = lireValidation(j);
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

    function abandon() {
      // Un abandon est bien une indisponibilite : le serveur n'a pas repondu a temps.
      return erreurChat({
        kind: "indisponible",
        code: (ctrl && ctrl.signal.aborted) ? "timeout_client" : "reseau",
        statut: 0
      });
    }

    // `finir()` n'est appele qu'au **reglement**, jamais a la reception des en-tetes : `r.json()`
    // attend le corps, et couper la minuterie plus tot laissait un serveur qui repond puis bloque
    // sur le corps pendre sans borne, attente affichee et saisie verrouillee (meme defaut que la
    // sonde, revue Codex 1.10, I1). Consequence assumee : le corps aussi peut etre abandonne, et un
    // `json()` rejete apres un `abort` est un abandon, pas un corps illisible.
    return fetch(API_BASE + "/chat", options).then(function (r) {
      if (!r.ok) {
        return r.json().then(function (j) { return j; }, function () { return null; })
          .then(function (j) { throw erreurHttp(r.status, r.headers, j); });
      }
      return r.json().then(lireReponse, function () {
        if (ctrl && ctrl.signal.aborted) throw abandon();
        // 200 dont le corps n'est pas lisible : le serveur est casse, mais ce n'est pas une
        // indisponibilite au sens d'AD-11 — pas de bouton de repli.
        throw erreurChat({ kind: "requete", code: "reponse_illisible", statut: r.status });
      });
    }, function () {
      throw abandon();
    }).then(function (v) { finir(); return v; },
            function (e) { finir(); throw e; });
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
    // Ce que la page conserve de chaque reponse (story 2.2) : compose ici, pose par `ui.js`.
    tourAssistant: tourAssistant,
    citationsParSegment: citationsParSegment,
    statutTexte: statutTexte,
    preuveAbsence: preuveAbsence,
    lectureLue: lectureLue,
    coutTexte: coutTexte,
    etatReponse: etatReponse,
    phraseEtat: phraseEtat,
    messageErreur: messageErreur,
    // Les vues : l'arbre de ce qui doit etre peint. `ui.js` ne fait plus que le materialiser.
    vueAttente: vueAttente,
    vueReponse: vueReponse,
    vueReponseLocale: vueReponseLocale,
    vueErreur: vueErreur,
    // Story 2.5 : le panneau « Pourquoi cette réponse », et les tables qu'il consomme. L'AC est une
    // liste de rubriques : elle se vérifie sur l'arbre composé, pas sur du DOM.
    vuePourquoi: vuePourquoi,
    libelleControle: libelleControle,
    motifRejet: motifRejet,
    reserves: reserves,
    ALERTES: ALERTES,
    CONTROLES: CONTROLES,
    REJETS: REJETS,
    RESERVES: RESERVES,
    modeApresErreur: modeApresErreur,
    libelleMode: libelleMode,
    suffixeValidation: suffixeValidation,
    estPerime: estPerime,
    // Ce que la sonde a dit du niveau de validation, ou `null` : `ui.js` le passe a `libelleMode`
    // au moment de poser le badge, il ne le retient pas.
    validation: function () { return validationServeur; },
    lireValidation: lireValidation,
    // Parseur pur du champ HTTP `Retry-After`, exposé pour tester secondes et HTTP-date sans réseau.
    retryApres: retryApres,
    setApiBase: function (u) { API_BASE = u; apiDisponible = null; validationServeur = null; },
    apiBase: function () { return API_BASE; },
    // Pour les tests : ce que le front croit des bornes du serveur, et d'ou il le tient.
    bornes: function () {
      return {
        historique_max_tours: historiqueMaxTours(),
        tour_max_caracteres: TOUR_MAX_CARACTERES,
        langues_servies: LANGUES_SERVIES.slice(),
        delai_abandon_ms: delaiAbandonMs(),
        seuils_du_serveur: seuilsServeur,
        validation_du_serveur: validationServeur
      };
    }
  };
})();
