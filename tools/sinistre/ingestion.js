// Story 3.5 — lecteur autonome du rapport d'ingestion. Toute chaîne distante passe par textContent.
(function () {
  "use strict";

  var API_BASE = (window.location && /^https?:$/.test(window.location.protocol))
    ? window.location.origin : "";
  // Une lecture d'audit ne doit pas rester indéfiniment en ``aria-busy``. La borne vient du même
  // seuil serveur que les autres lectures du front (`thresholds.client_abort_margin_s`) ; le
  // littéral n'est qu'un repli pour la sonde `/sante` elle-même ou si elle est indisponible.
  var FETCH_TIMEOUT_MS_REPLI = 10000;
  var dernierTimeoutMs = FETCH_TIMEOUT_MS_REPLI;

  function timeoutDepuisSante(sante) {
    var marge = sante && sante.thresholds && sante.thresholds.client_abort_margin_s;
    return (typeof marge === "number" && isFinite(marge) && marge > 0)
      ? Math.min(Math.round(marge * 1000), 2147483647) : FETCH_TIMEOUT_MS_REPLI;
  }

  function timeoutOption(options) {
    var v = options && options.timeoutMs;
    return (typeof v === "number" && isFinite(v) && v >= 0 && v <= 60000) ? v : null;
  }

  function element(tag, texte, cls) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (texte !== undefined && texte !== null) e.textContent = String(texte);
    return e;
  }

  function valeur(v) {
    if (v === undefined || v === null) return "indisponible";
    if (v === "") return "valeur vide";
    if (typeof v === "boolean") return v ? "oui" : "non";
    if (typeof v === "object") return JSON.stringify(v);
    return String(v);
  }

  function hotePrive(hostname) {
    var hote = String(hostname || "").toLowerCase().replace(/^\[|\]$/g, "");
    if (!hote || hote === "localhost" || /\.localhost$/.test(hote) || hote === "::" || hote === "::1") {
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

  function urlHttp(v) {
    var u = String(v || "");
    if (!u || u.length > 2048 || /\s/.test(u)) return null;
    try {
      var url = new URL(u);
      if ((url.protocol !== "http:" && url.protocol !== "https:") ||
          url.username || url.password || hotePrive(url.hostname)) return null;
      return u;
    } catch (_) {
      return null;
    }
  }

  function docIdDepuisChemin(pathname) {
    var marque = "/sinistre/ingestion/";
    var chemin = String(pathname || "");
    var rang = chemin.indexOf(marque);
    if (rang < 0) return null;
    var brut = chemin.slice(rang + marque.length).replace(/\/+$/, "");
    if (!brut || brut.indexOf("/") >= 0) return null;
    try { return decodeURIComponent(brut); } catch (_) { return null; }
  }

  function ligne(dl, nom, v) {
    var dt = element("dt", nom);
    var absent = v === undefined || v === null;
    var dd = element("dd", valeur(v), absent ? "indisponible" : null);
    dl.appendChild(dt);
    dl.appendChild(dd);
  }

  function editionAvecReserve(v) {
    var edition = (v === undefined || v === null)
      ? "indisponible" : (v === "" ? "valeur vide" : String(v));
    return edition + " — actualité non vérifiée";
  }

  var LIBELLES_ALERTES_GATE = {
    gate_perime: "Ce gate est périmé : il ne valide pas le code actuellement servi.",
    sans_gate: "Aucun gate courant ne valide ce document.",
    ungated_refuse_en_production: "Ce document sans gate courant est refusé en production."
  };

  function alertesGate(sante, docId) {
    var alertes = sante && Array.isArray(sante.alerts) ? sante.alerts : [];
    return alertes.filter(function (alerte) {
      return alerte && alerte.doc_id === docId &&
        Object.prototype.hasOwnProperty.call(LIBELLES_ALERTES_GATE, alerte.alerte);
    });
  }

  function carteMetadonnees(documentAudit) {
    var d = documentAudit || {};
    var section = element("section", null, "carte metadonnees");
    section.appendChild(element("h2", "État du document"));
    var badge = element("span", d.status === "quarantaine" ? "quarantaine" : "servi",
                        "statut " + (d.status === "quarantaine" ? "quarantaine" : "servi"));
    section.appendChild(badge);
    if (d.status === "quarantaine") {
      section.appendChild(element("p", "Raison : " + valeur(d.raison), "raison"));
      section.appendChild(element(
        "p",
        "Les empreintes et le gate ci-dessous décrivent le manifest observé au démarrage. " +
          "Ils ne constituent pas une validation courante tant que ce document reste en quarantaine.",
        "avertissement manifest-avertissement"));
    }
    var dl = element("dl");
    ligne(dl, "Identifiant", d.doc_id);
    ligne(dl, "Titre", d.title);
    ligne(dl, "Type", d.kind);
    ligne(dl, "Édition", editionAvecReserve(d.edition));
    ligne(dl, "Empreinte source", d.source_hash);
    ligne(dl, "Empreinte d'ingestion", d.ingest_fingerprint);
    ligne(dl, "Empreinte document", d.document_hash);
    ligne(dl, "Empreinte du typage manuel", d.overlay_hash);
    section.appendChild(dl);
    var source = urlHttp(d.source_url);
    if (source) {
      var p = element("p");
      var a = element("a", "Voir le document à sa source publique");
      a.href = source;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      p.appendChild(a);
      section.appendChild(p);
    } else {
      section.appendChild(element("p", "Source publique : indisponible", "indisponible"));
    }
    return section;
  }

  function carteGate(gate, alertes) {
    var section = element("section", null, "carte gate");
    section.appendChild(element("h2", "Gate"));
    (Array.isArray(alertes) ? alertes : []).forEach(function (alerte) {
      section.appendChild(element(
        "p", LIBELLES_ALERTES_GATE[alerte.alerte], "avertissement gate-alerte"));
    });
    if (!gate || typeof gate !== "object") {
      section.appendChild(element("p", "Gate indisponible", "indisponible"));
      return section;
    }
    var dl = element("dl");
    [
      ["Profil", "profile"], ["Évaluation réussie", "evals_ok"], ["Date", "date"],
      ["Cas exécutés", "cases"], ["Contresignature humaine", "countersigned"],
      ["Empreinte source du gate", "source_hash"],
      ["Empreinte d'ingestion du gate", "ingest_fingerprint"],
      ["Empreinte des cas", "cases_hash"], ["Empreinte du pipeline", "pipeline_digest"],
      ["Empreinte des prompts", "prompts_digest"], ["Modèles", "model_ids"],
      ["Empreinte du typage manuel", "overlay_hash"]
    ].forEach(function (spec) { ligne(dl, spec[0], gate[spec[1]]); });
    section.appendChild(dl);
    return section;
  }

  var RAPPORTS = {
    absent: "Le rapport d'ingestion est absent.",
    illisible: "Le rapport d'ingestion est présent mais illisible.",
    etranger: "Le rapport d'ingestion décrit un autre document et n'est pas publié."
  };

  function carteRapport(documentAudit, rapport) {
    var section = element("section", null, "carte rapport");
    section.appendChild(element("h2", "Contrôles d'ingestion"));
    if (!rapport) {
      var etat = documentAudit && documentAudit.report_status;
      section.appendChild(element("p", RAPPORTS[etat] || "Rapport indisponible.", "indisponible"));
      return section;
    }
    var checks = Array.isArray(rapport.checks) ? rapport.checks : null;
    if (checks === null) {
      section.appendChild(element("p", "Le rapport reçu est illisible.", "indisponible"));
      return section;
    }
    if (!checks.length) {
      section.appendChild(element("p", "Aucun contrôle dans ce rapport."));
    } else {
      var enveloppe = element("div", null, "tableau");
      var table = element("table");
      var thead = element("thead");
      var trh = element("tr");
      ["Nom", "Niveau", "Détail"].forEach(function (titre) {
        trh.appendChild(element("th", titre));
      });
      thead.appendChild(trh);
      table.appendChild(thead);
      var tbody = element("tbody");
      checks.forEach(function (check) {
        var tr = element("tr");
        tr.appendChild(element("td", valeur(check && check.name)));
        tr.appendChild(element("td", valeur(check && check.level)));
        tr.appendChild(element("td", valeur(check && check.detail)));
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      enveloppe.appendChild(table);
      section.appendChild(enveloppe);
    }
    var brut = element("a", "Voir le JSON brut", "brut");
    brut.href = "/api/v1/documents/" + encodeURIComponent(String(documentAudit.doc_id)) + "/report";
    section.appendChild(brut);
    return section;
  }

  function rendre(documentAudit, rapport, alertes) {
    var hote = document.getElementById("rapport");
    if (!hote) return;
    hote.innerHTML = "";
    var titre = document.getElementById("titre");
    if (titre) titre.textContent = "Rapport d'ingestion — " + String(documentAudit.title || documentAudit.doc_id);
    hote.appendChild(carteMetadonnees(documentAudit));
    hote.appendChild(carteGate(documentAudit.gate, alertes));
    hote.appendChild(carteRapport(documentAudit, rapport));
    hote.setAttribute("aria-busy", "false");
  }

  function rendreErreur(texte) {
    var hote = document.getElementById("rapport");
    if (!hote) return;
    hote.innerHTML = "";
    var section = element("section", null, "carte erreur");
    section.appendChild(element("h2", "Rapport indisponible"));
    section.appendChild(element("p", texte));
    hote.appendChild(section);
    hote.setAttribute("aria-busy", "false");
  }

  function json(chemin, timeoutMs) {
    var ctrl = typeof AbortController === "function" ? new AbortController() : null;
    return new Promise(function (resolve, reject) {
      var termine = false;
      var minuteur = setTimeout(function () {
        if (termine) return;
        termine = true;
        if (ctrl) ctrl.abort();
        reject(new Error("timeout"));
      }, timeoutMs);
      function finir(fn, valeur) {
        if (termine) return;
        termine = true;
        clearTimeout(minuteur);
        fn(valeur);
      }
      fetch(API_BASE + chemin, ctrl ? { signal: ctrl.signal } : {}).then(function (reponse) {
        if (!reponse.ok) throw new Error("http");
        return reponse.json();
      }).then(function (corps) {
        finir(resolve, corps);
      }, function (erreur) {
        finir(reject, erreur);
      });
    });
  }

  function chargerAudit(docId, timeoutMs, sante) {
    dernierTimeoutMs = timeoutMs;
    return json("/api/v1/documents", timeoutMs).then(function (documents) {
      if (!Array.isArray(documents)) {
        rendreErreur("La liste des documents n'a pas pu être chargée.");
        return null;
      }
      var documentAudit = documents.filter(function (d) { return d && d.doc_id === docId; })[0];
      if (!documentAudit) {
        rendreErreur("Ce document n'est pas connu du loader.");
        return null;
      }
      var alertes = alertesGate(sante, docId);
      if (documentAudit.report_status !== "disponible") {
        rendre(documentAudit, null, alertes);
        return null;
      }
      return json("/api/v1/documents/" + encodeURIComponent(docId) + "/report", timeoutMs)
        .then(function (rapport) { rendre(documentAudit, rapport, alertes); }, function () {
          // Le statut chargé au boot reste l'autorité ; une erreur HTTP n'est pas transformée en
          // faux rapport vide et aucun message potentiellement hostile du serveur n'est réfléchi.
          rendreErreur("Le rapport validé au démarrage n'a pas pu être consulté.");
        });
    }, function () {
      rendreErreur("La liste des documents n'a pas pu être chargée.");
      return null;
    }).catch(function () {
      rendreErreur("Les données ont été chargées, mais le rapport n'a pas pu être affiché.");
    });
  }

  function demarrer(options) {
    var docId = docIdDepuisChemin(window.location && window.location.pathname);
    if (!docId) {
      rendreErreur("L'identifiant du document est invalide.");
      return Promise.resolve();
    }
    var injecte = timeoutOption(options);
    if (injecte !== null) return chargerAudit(docId, injecte, null);
    return json("/api/v1/sante", FETCH_TIMEOUT_MS_REPLI).then(function (sante) {
      return chargerAudit(docId, timeoutDepuisSante(sante), sante);
    }, function () {
      return chargerAudit(docId, FETCH_TIMEOUT_MS_REPLI, null);
    });
  }

  window.INGESTION = {
    valeur: valeur,
    editionAvecReserve: editionAvecReserve,
    urlHttp: urlHttp,
    docIdDepuisChemin: docIdDepuisChemin,
    carteMetadonnees: carteMetadonnees,
    carteGate: carteGate,
    alertesGate: alertesGate,
    carteRapport: carteRapport,
    rendre: rendre,
    demarrer: demarrer,
    timeoutDepuisSante: timeoutDepuisSante,
    apiBase: function () { return API_BASE; },
    fetchTimeoutMs: function () { return dernierTimeoutMs; }
  };

  if (!window.__INGESTION_SANS_DEMARRAGE) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", function () { demarrer(); });
    }
    else demarrer();
  }
})();
