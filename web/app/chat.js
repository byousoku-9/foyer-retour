// Moteur du chatbot.
// Deux modes :
//   - local  : recherche dans la base de connaissances, aucune dependance reseau.
//   - api    : appel au serveur local (server/server.js), qui relaie vers Azure OpenAI.
// Le mode api est detecte automatiquement ; a defaut, le mode local prend le relais.

window.CHAT = (function () {

  var API_BASE = "http://localhost:8787";
  var apiDisponible = null; // null = pas encore teste

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

  // ---------- Mode API ----------

  function contexteRag(question, profil) {
    var hits = rechercher(question, 4);
    if (!hits.length) hits = fichesPourProfil(profil).slice(0, 3).map(function (f) { return { fiche: f }; });
    return hits.map(function (h) {
      var f = h.fiche;
      return "### " + f.titre + " (" + f.cat + ")\n" + f.resume + "\n" +
        (f.corps || []).map(function (p) {
          return typeof p === "string" ? p : (p && p.h) || "";
        }).join("\n") +
        (f.aRetenir ? "\nA retenir : " + f.aRetenir.join(" ") : "") +
        (f.sources ? "\nSources : " + f.sources.map(function (s) { return s.t + " " + s.u; }).join(" | ") : "");
    }).join("\n\n");
  }

  function testerApi() {
    if (apiDisponible !== null) return Promise.resolve(apiDisponible);
    // Le relais tourne en local : hors localhost (site publie en statique),
    // inutile d'essayer, on reste en mode local sans bruit dans la console.
    var h = window.location.hostname;
    if (API_BASE.indexOf("localhost") !== -1 &&
        h !== "localhost" && h !== "127.0.0.1" && h !== "") {
      apiDisponible = false;
      return Promise.resolve(false);
    }
    return fetch(API_BASE + "/sante", { method: "GET" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) { apiDisponible = !!(j && j.ok); return apiDisponible; })
      .catch(function () { apiDisponible = false; return false; });
  }

  function reponseApi(question, profil, historique) {
    var ctx = contexteRag(question, profil);
    return fetch(API_BASE + "/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question: question,
        profil: decrireProfil(profil),
        contexte: ctx,
        historique: (historique || []).slice(-6)
      })
    }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    }).then(function (j) {
      var hits = rechercher(question, 3);
      return {
        texte: j.reponse,
        sources: hits.length && hits[0].fiche.sources ? hits[0].fiche.sources : [],
        fiches: hits.map(function (h) { return h.fiche.id; }),
        via: "api"
      };
    });
  }

  // ---------- Point d'entree unique ----------

  function repondre(question, profil, historique) {
    return testerApi().then(function (ok) {
      if (!ok) {
        var r = reponseLocale(question, profil);
        r.via = "local";
        return r;
      }
      return reponseApi(question, profil, historique).catch(function () {
        var r = reponseLocale(question, profil);
        r.via = "local (api indisponible)";
        return r;
      });
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
    testerApi: testerApi,
    setApiBase: function (u) { API_BASE = u; apiDisponible = null; }
  };
})();
