// Harnais Node du front du guide (story 1.7) — aucun navigateur, aucun réseau, aucune dépendance.
//
// `web/app/chat.js` est un IIFE posé sur `window` : toute la logique testable — corps de la requête,
// mapping de l'historique, classification des erreurs, appariement des citations, textes composés —
// y vit. On le charge donc dans un `node:vm` avec `window`, `location` et `fetch` **doublés**, on
// exécute les cas de la matrice d'E/S, et on écrit sur la sortie standard le JSON de ce qui a été
// **observé**. Les assertions, elles, sont en Python (`tests/test_web_chat.py`) : ce fichier ne
// juge rien, il relève.
//
// `ui.js` n'est pas chargé : il ne fait plus que peindre, et la peinture se vérifie en live dans un
// Chrome headless (docs/tests-live.md).

import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const ICI = path.dirname(fileURLToPath(import.meta.url));
const RACINE = path.resolve(ICI, "..", "..");

// ---------- le bac à sable ----------

/** Une `Response` doublée : juste ce que `chat.js` en lit (`ok`, `status`, `headers.get`, `json`). */
function reponseHttp({ status = 200, corps = {}, entetes = {}, corpsIllisible = false } = {}) {
  const bas = {};
  for (const [k, v] of Object.entries(entetes)) bas[k.toLowerCase()] = String(v);
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (nom) => (nom.toLowerCase() in bas ? bas[nom.toLowerCase()] : null) },
    json: () => (corpsIllisible
      ? Promise.reject(new Error("corps non JSON"))
      : Promise.resolve(corps)),
  };
}

/**
 * Charge `kb.js` puis `chat.js` dans un contexte neuf.
 * @param {string} href      l'URL de la page servie (donne `window.location.origin`)
 * @param {Function} repondre  le double de `fetch` : (url, options) → Response doublée
 */
function chargerChat(href, repondre) {
  const appels = [];
  const fetchDouble = (url, options) => {
    appels.push({ url, options: options || {} });
    // `fetch` **rejette** sur une panne réseau, il ne lève pas : un double qui lèverait
    // synchroniquement testerait un chemin que le navigateur n'emprunte jamais.
    try {
      return Promise.resolve(repondre(url, options || {}, appels.length - 1));
    } catch (e) {
      return Promise.reject(e);
    }
  };
  const window = { location: new URL(href), fetch: fetchDouble };
  const bac = { window, fetch: fetchDouble, console, URL };
  bac.globalThis = bac;
  vm.createContext(bac);
  for (const fichier of ["web/app/kb.js", "web/app/chat.js"]) {
    vm.runInContext(readFileSync(path.join(RACINE, fichier), "utf8"), bac, { filename: fichier });
  }
  // `reponseLocale()` renvoie les questions chiffrées vers le simulateur ; il est chargé par une
  // autre balise du site, et aucun cas ici ne l'emprunte.
  window.SIM = { comparatif: () => [] };
  window.CONTRATS_KB = { contrats: [] };

  // Compteur d'accès au moteur lexical : `rechercher()`, `chercherFaq()` et `fichesPourProfil()`
  // passent tous par `window.KB`. Il prouve qu'**aucun** résultat local n'est calculé avant le clic.
  const compteur = { lectures: 0 };
  const kb = window.KB;
  window.KB = new Proxy(kb, {
    get(cible, prop) {
      if (prop === "fiches" || prop === "faq") compteur.lectures++;
      return cible[prop];
    },
  });
  return { CHAT: window.CHAT, appels, compteur, window };
}

// ---------- les données des cas ----------

const ORIGINE = "https://foyer-retour.example";
const PAGE = ORIGINE + "/guide/#assistant";

const PROFIL = {
  situation: "En famille", enfants: "2", statut: "Salarie",
  logement: "Louer", vehicule: "Oui", horizon: "Je viens d'arriver",
};

const QUESTION = "Quel délai ai-je pour déclarer mon arrivée à la commune ?";

/** Une réponse 200 conforme au contrat d'AD-11 : 2 segments factuels, 3 claims, 3 citations. */
function reponseSourcee() {
  const claims = [
    {
      claim_id: "c1", text: "Le délai est de huit jours.",
      quotes: [{ block_id: "lux-guide:farrivee:2", quote: "huit jours pour déclarer votre arrivée",
                 start: 0, end: 10, text_start: 0, text_end: 10 }],
      status: { retrouvee: true, pertinente: true, applicable: null, edition: "git:a8e8593" },
    },
    {
      claim_id: "c2", text: "La déclaration se fait au Biergercenter.",
      quotes: [{ block_id: "lux-guide:farrivee:3", quote: "au bureau de la population, souvent appelé Biergercenter",
                 start: 0, end: 10, text_start: 0, text_end: 10 }],
      status: { retrouvee: true, pertinente: true, applicable: null, edition: "git:a8e8593" },
    },
    {
      claim_id: "c3", text: "Elle produit le certificat de résidence.",
      quotes: [{ block_id: "lux-guide:farrivee:5", quote: "le certificat de résidence",
                 start: 0, end: 10, text_start: 0, text_end: 10 }],
      status: { retrouvee: true, pertinente: true, applicable: null, edition: "git:a8e8593" },
    },
  ];
  const segments = [
    { text: "Vous avez huit jours pour déclarer votre arrivée, au Biergercenter de votre commune.",
      kind: "factuel", claim_ids: ["c1", "c2"] },
    { text: "Cette déclaration produit le certificat de résidence.", kind: "factuel", claim_ids: ["c3"] },
  ];
  const answer = {
    found: true, complete: true, lang: "fr", lang_fallback: false,
    texte: segments.map((s) => s.text).join(" "),
    segments, claims, rejected_claims: [], reason: null, verdict: null, unknown: [], clarification: null,
  };
  return {
    texte: answer.texte,
    segments,
    sources: [
      { block_id: "lux-guide:farrivee:2", fiche_id: "arrivee", titre: "Les huit premiers jours",
        url: "https://guichet.public.lu/arrivee", quote: "huit jours pour déclarer votre arrivée",
        status: "verifiee" },
      { block_id: "lux-guide:farrivee:3", fiche_id: "arrivee", titre: "Les huit premiers jours",
        url: "https://guichet.public.lu/arrivee",
        quote: "au bureau de la population, souvent appelé Biergercenter", status: "verifiee" },
      { block_id: "lux-guide:farrivee:5", fiche_id: "arrivee", titre: "Les huit premiers jours",
        url: "https://guichet.public.lu/arrivee", quote: "le certificat de résidence", status: "verifiee" },
    ],
    fiches: ["arrivee"],
    unknown: [],
    comparateur: false,
    answer,
    via: "api/v1",
    trace: { request_id: "r-1", pipeline: "guide", intent: "question", total_cost_eur: 0.0278, steps: [] },
  };
}

function refus() {
  const phrase = "Cette question sort de ce que couvre le guide : je n'y réponds pas plutôt que " +
    "d'y répondre à côté.";
  const answer = {
    found: false, complete: false, lang: "fr", lang_fallback: false, texte: phrase,
    segments: [{ text: phrase, kind: "limite", claim_ids: [] }],
    claims: [], rejected_claims: [],
    reason: { kind: "hors_perimetre", terms_searched: ["météo"], variants_count: 4,
              blocks_scanned: 312, documents: ["lux-guide"] },
    verdict: null, unknown: [], clarification: null,
  };
  return {
    texte: phrase, segments: answer.segments, sources: [], fiches: [], unknown: [],
    comparateur: false, answer, via: "api/v1",
    trace: { request_id: "r-2", pipeline: "guide", intent: "hors_perimetre", total_cost_eur: 0.0009, steps: [] },
  };
}

/** Sérialisation lisible en Python de ce que `citationsParSegment()` a rendu. */
function aplatir(parSegment) {
  if (parSegment === null) return null;
  return parSegment.map((cites) => cites.map((e) => ({
    block_id: e.source.block_id, quote: e.source.quote, claim_id: e.claim_id, rang: e.rang,
  })));
}

// ---------- les cas ----------

const cas = {};

async function main() {
  // --- l'origine et la sonde ---------------------------------------------
  {
    const santeVue = [];
    const { CHAT, appels } = chargerChat(PAGE, (url) => {
      santeVue.push(url);
      return reponseHttp({ corps: { ok: true, version: "abc1234", documents_servis: ["lux-guide"] } });
    });
    cas.api_base = CHAT.apiBase();
    cas.sonde_ok = await CHAT.testerApi();
    cas.sonde_url = appels.length ? appels[0].url : null;
    cas.sonde_methode = appels.length ? appels[0].options.method : null;
  }

  // --- le corps envoyé ----------------------------------------------------
  {
    const { CHAT, appels } = chargerChat(PAGE, () => reponseHttp({ corps: reponseSourcee() }));
    // Le site pousse la question dans l'historique **avant** l'appel : le dernier tour la répète.
    const historique = [
      { role: "user", content: "Bonjour" },
      { role: "assistant", content: "Bonjour, posez votre question." },
      { role: "user", content: QUESTION },
    ];
    await CHAT.repondre(QUESTION, PROFIL, historique);
    cas.corps_url = appels[0].url;
    cas.corps_methode = appels[0].options.method;
    cas.corps_entetes = appels[0].options.headers;
    cas.corps_envoye = JSON.parse(appels[0].options.body);
  }

  // --- l'historique -------------------------------------------------------
  {
    const { CHAT } = chargerChat(PAGE, () => reponseHttp({ corps: reponseSourcee() }));
    const neuf = [];
    for (let i = 1; i <= 9; i++) {
      neuf.push({ role: i % 2 ? "user" : "assistant", content: "tour " + i });
    }
    cas.historique_long = CHAT.historiquePourApi(neuf, "une nouvelle question");

    const avecEcho = neuf.concat([{ role: "user", content: QUESTION }]);
    cas.historique_dernier_tour_exclu = CHAT.historiquePourApi(avecEcho, QUESTION);

    const surdimensionne = [
      { role: "user", content: "petit 1" },
      { role: "assistant", content: "x".repeat(2400) },
      { role: "user", content: "petit 2" },
      { role: "assistant", content: "y".repeat(2000) },
    ];
    cas.historique_tour_surdimensionne = CHAT.historiquePourApi(surdimensionne, "autre chose");
    cas.historique_vide = CHAT.historiquePourApi(null, QUESTION);
  }

  // --- la lecture stricte du contrat -------------------------------------
  {
    const { CHAT, compteur } = chargerChat(PAGE, () => reponseHttp({ corps: reponseSourcee() }));
    compteur.lectures = 0;
    const r = await CHAT.repondre(QUESTION, PROFIL, []);
    cas.reponse_lue = {
      texte: r.texte,
      segments: r.segments.map((s) => ({ text: s.text, kind: s.kind, claim_ids: s.claim_ids })),
      sources: r.sources.map((s) => ({ block_id: s.block_id, quote: s.quote, fiche_id: s.fiche_id,
                                       url: s.url, status: s.status })),
      fiches: r.fiches,
      unknown: r.unknown,
      comparateur: r.comparateur,
      via: r.via,
      cout: CHAT.coutTexte(r.trace),
      etat: CHAT.etatReponse(r.answer),
      lectures_du_moteur_lexical: compteur.lectures,
    };
    cas.citations_nominal = aplatir(CHAT.citationsParSegment(r.answer, r.sources));
    cas.statuts = r.answer.claims.map((c) => CHAT.statutTexte(c.status));
  }

  // --- l'ancien champ `reponse` n'est plus lu, les sources viennent du serveur
  {
    const ancien = {
      reponse: "Texte de l'ancien contrat, celui d'avant la 1.7.",
      sources: [{ block_id: "lux-guide:fbanque:1", fiche_id: "banque", titre: "Ouvrir un compte",
                  url: "https://guichet.public.lu/banque", quote: "un compte au Luxembourg",
                  status: "verifiee" }],
      answer: { found: true, complete: false, texte: "", segments: [], claims: [], rejected_claims: [],
                reason: null, unknown: [], clarification: null },
      via: "api/v1",
      trace: { request_id: "r-3", pipeline: "guide", intent: "question", total_cost_eur: 0.01, steps: [] },
    };
    const { CHAT } = chargerChat(PAGE, () => reponseHttp({ corps: ancien }));
    const r = await CHAT.repondre("Comment ouvrir un compte bancaire ?", PROFIL, []);
    cas.ancien_contrat = {
      texte: r.texte,
      a_champ_reponse: "reponse" in r,
      sources: r.sources.map((s) => s.block_id),
      quotes: r.sources.map((s) => s.quote),
    };
  }

  // --- l'appariement citation ↔ segment, et ses trois abandons ------------
  {
    const { CHAT } = chargerChat(PAGE, () => reponseHttp({ corps: reponseSourcee() }));
    const bon = reponseSourcee();

    // Une même claim citée deux fois par le même segment : une seule citation sous la phrase.
    const doublon = reponseSourcee();
    doublon.answer.segments = [{ text: "Une phrase.", kind: "factuel", claim_ids: ["c1", "c1", "c2"] }];
    cas.citations_sans_doublon = aplatir(CHAT.citationsParSegment(doublon.answer, doublon.sources));

    // Un `block_id` qui ne concorde pas : l'appariement est abandonné, pas deviné.
    const decale = reponseSourcee();
    decale.sources[1] = { ...decale.sources[1], block_id: "lux-guide:fautre:9" };
    cas.citations_block_id_decale = CHAT.citationsParSegment(decale.answer, decale.sources);

    // Plus de sources que de quotes énumérées.
    const enTrop = reponseSourcee();
    enTrop.sources.push({ ...enTrop.sources[0], block_id: "lux-guide:fbanque:1" });
    cas.citations_source_en_trop = CHAT.citationsParSegment(enTrop.answer, enTrop.sources);

    // Moins de sources que de quotes énumérées.
    const manquante = reponseSourcee();
    manquante.sources = manquante.sources.slice(0, 2);
    cas.citations_source_manquante = CHAT.citationsParSegment(manquante.answer, manquante.sources);

    // Un segment qui cite une claim absente de `claims[]`.
    const orpheline = reponseSourcee();
    orpheline.answer.segments[1] = { ...orpheline.answer.segments[1], claim_ids: ["c9"] };
    cas.citations_claim_absente = CHAT.citationsParSegment(orpheline.answer, orpheline.sources);

    // Un refus : aucune claim, aucune source, un segment.
    const r = refus();
    cas.citations_refus = aplatir(CHAT.citationsParSegment(r.answer, r.sources));
    cas.appariement_bon_ordre = aplatir(CHAT.citationsParSegment(bon.answer, bon.sources)) !== null;
  }

  // --- le refus, sa preuve, la clarification ------------------------------
  {
    const { CHAT } = chargerChat(PAGE, () => reponseHttp({ corps: refus() }));
    const r = await CHAT.repondre("Quel temps fera-t-il demain ?", PROFIL, []);
    cas.refus = {
      texte: r.texte,
      segments_kind: r.segments.map((s) => s.kind),
      sources: r.sources.length,
      preuve: CHAT.preuveAbsence(r.answer.reason),
      etat: CHAT.etatReponse(r.answer),
      cout: CHAT.coutTexte(r.trace),
    };
    cas.preuve_singuliers = CHAT.preuveAbsence({
      kind: "zero_hit", terms_searched: ["bail"], variants_count: 1, blocks_scanned: 1, documents: [],
    });
    cas.preuve_clarification = CHAT.preuveAbsence({
      kind: "clarification_requise", terms_searched: [], variants_count: 0, blocks_scanned: 0, documents: [],
    });
    cas.preuve_absente = CHAT.preuveAbsence(null);
  }

  // --- réponse incomplète : `unknown` et l'état « partiel » ---------------
  {
    const partielle = reponseSourcee();
    partielle.answer.complete = false;
    partielle.answer.unknown = ["montant exact"];
    partielle.unknown = ["montant exact"];
    const { CHAT } = chargerChat(PAGE, () => reponseHttp({ corps: partielle }));
    const r = await CHAT.repondre(QUESTION, PROFIL, []);
    cas.partielle = { unknown: r.unknown, etat: CHAT.etatReponse(r.answer) };
  }

  // --- clarification ------------------------------------------------------
  {
    const c = refus();
    c.answer.reason.kind = "clarification_requise";
    c.answer.reason.terms_searched = [];
    c.answer.reason.variants_count = 0;
    c.answer.reason.blocks_scanned = 0;
    c.answer.clarification = "Parlez-vous du bail de votre logement ou de votre contrat de travail ?";
    const { CHAT } = chargerChat(PAGE, () => reponseHttp({ corps: c }));
    const r = await CHAT.repondre("Et celui-là ?", PROFIL, []);
    cas.clarification = {
      clarification: r.answer.clarification,
      texte: r.texte,
      preuve: CHAT.preuveAbsence(r.answer.reason),
    };
  }

  // --- comparateur --------------------------------------------------------
  {
    const c = reponseSourcee();
    c.comparateur = true;
    const { CHAT } = chargerChat(PAGE, () => reponseHttp({ corps: c }));
    const r = await CHAT.repondre("Mon assurance couvre-t-elle un dégât des eaux ?", PROFIL, []);
    cas.comparateur = r.comparateur;
  }

  // --- 503 : aucune recherche locale avant le clic ------------------------
  {
    const { CHAT, compteur } = chargerChat(PAGE, () => reponseHttp({
      status: 503,
      corps: { error: { code: "llm_unavailable", message: "provider down", request_id: "req-503" } },
    }));
    compteur.lectures = 0;
    let erreur = null;
    let reponse = null;
    try { reponse = await CHAT.repondre(QUESTION, PROFIL, []); } catch (e) { erreur = e; }
    cas.erreur_503 = {
      a_repondu: reponse !== null,
      nom: erreur && erreur.nom, kind: erreur && erreur.kind, code: erreur && erreur.code,
      statut: erreur && erreur.statut, request_id: erreur && erreur.request_id,
      message: CHAT.messageErreur(erreur),
      lectures_du_moteur_lexical_avant_clic: compteur.lectures,
    };
    // Le clic, et lui seul, fait tourner le moteur lexical.
    const locale = CHAT.rechercheSimple(QUESTION, PROFIL);
    cas.recherche_simple = {
      via: locale.via,
      texte_non_vide: typeof locale.texte === "string" && locale.texte.length > 0,
      fiches: locale.fiches,
      lectures_du_moteur_lexical_apres_clic: compteur.lectures,
    };
  }

  // --- panne réseau -------------------------------------------------------
  {
    const { CHAT, compteur } = chargerChat(PAGE, () => { throw new TypeError("Failed to fetch"); });
    compteur.lectures = 0;
    let erreur = null;
    try { await CHAT.repondre(QUESTION, PROFIL, []); } catch (e) { erreur = e; }
    cas.erreur_reseau = {
      kind: erreur && erreur.kind, code: erreur && erreur.code, statut: erreur && erreur.statut,
      message: CHAT.messageErreur(erreur),
      lectures_du_moteur_lexical: compteur.lectures,
    };
  }

  // --- 429, 400, 413, 500, code inconnu : message, jamais de repli --------
  {
    const echecs = {
      erreur_429: { status: 429, code: "rate_limited", entetes: { "Retry-After": "60" } },
      erreur_429_sans_entete: { status: 429, code: "rate_limited", entetes: {} },
      erreur_400: { status: 400, code: "invalid_request", entetes: {} },
      erreur_413: { status: 413, code: "input_too_long", entetes: {} },
      erreur_500: { status: 500, code: "internal", entetes: {} },
      erreur_code_inconnu: { status: 418, code: "theiere", entetes: {} },
    };
    for (const [nom, d] of Object.entries(echecs)) {
      const { CHAT, compteur } = chargerChat(PAGE, () => reponseHttp({
        status: d.status, entetes: d.entetes,
        corps: { error: { code: d.code, message: "body.historique: List should have at most 6 items",
                          request_id: "req-" + d.status } },
      }));
      compteur.lectures = 0;
      let erreur = null;
      try { await CHAT.repondre(QUESTION, PROFIL, []); } catch (e) { erreur = e; }
      cas[nom] = {
        kind: erreur && erreur.kind, code: erreur && erreur.code, statut: erreur && erreur.statut,
        retry_after: erreur && erreur.retry_after, request_id: erreur && erreur.request_id,
        message: CHAT.messageErreur(erreur),
        lectures_du_moteur_lexical: compteur.lectures,
      };
    }
  }

  // --- un 200 dont le corps n'est pas lisible ----------------------------
  {
    const { CHAT } = chargerChat(PAGE, () => reponseHttp({ corpsIllisible: true }));
    let erreur = null;
    try { await CHAT.repondre(QUESTION, PROFIL, []); } catch (e) { erreur = e; }
    cas.corps_illisible = {
      kind: erreur && erreur.kind, code: erreur && erreur.code,
      message: CHAT.messageErreur(erreur),
    };
  }

  // --- la sonde en échec n'ouvre aucune porte ----------------------------
  {
    const { CHAT, compteur } = chargerChat(PAGE, (url) => {
      if (String(url).endsWith("/sante")) throw new TypeError("Failed to fetch");
      return reponseHttp({ status: 503, corps: { error: { code: "llm_unavailable", message: "",
                                                          request_id: "req-x" } } });
    });
    cas.sonde_en_echec = await CHAT.testerApi();
    compteur.lectures = 0;
    let erreur = null;
    let reponse = null;
    try { reponse = await CHAT.repondre(QUESTION, PROFIL, []); } catch (e) { erreur = e; }
    cas.apres_sonde_en_echec = {
      a_repondu_en_local: reponse !== null,
      kind: erreur && erreur.kind,
      lectures_du_moteur_lexical: compteur.lectures,
    };
  }

  // --- les textes composés, isolément ------------------------------------
  {
    const { CHAT } = chargerChat(PAGE, () => reponseHttp({ corps: reponseSourcee() }));
    cas.statut_textes = {
      complet: CHAT.statutTexte({ retrouvee: true, pertinente: true, applicable: null,
                                  edition: "git:a8e8593" }),
      edition_pdf: CHAT.statutTexte({ retrouvee: true, pertinente: true, applicable: "oui",
                                      edition: "juin 2017" }),
      sans_statut: CHAT.statutTexte(null),
    };
    cas.cout_textes = {
      nominal: CHAT.coutTexte({ total_cost_eur: 0.0278 }),
      zero: CHAT.coutTexte({ total_cost_eur: 0 }),
      absent: CHAT.coutTexte(null),
      sans_champ: CHAT.coutTexte({}),
    };
    cas.etats = {
      sur: CHAT.etatReponse({ found: true, complete: true }),
      partiel: CHAT.etatReponse({ found: true, complete: false }),
      inconnu: CHAT.etatReponse({ found: false, complete: false }),
      absent: CHAT.etatReponse(null),
    };
    cas.exporte = Object.keys(CHAT).sort();
  }

  return cas;
}

main().then(
  (resultat) => {
    process.stdout.write(JSON.stringify({ ok: true, node: process.version, cas: resultat }, null, 2));
  },
  (erreur) => {
    process.stdout.write(JSON.stringify({
      ok: false, node: process.version,
      erreur: String((erreur && erreur.stack) || erreur),
    }, null, 2));
    process.exitCode = 1;
  },
);
