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

import { CORPS_PARTAGES } from "./sante_corpus.mjs";

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
function chargerChat(href, repondre, { minuteurs = null } = {}) {
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
  // Le JSON du harnais sort sur **stdout** : un `console.log` laissé dans `chat.js` le corromprait
  // silencieusement. Le `console` du bac à sable écrit donc sur stderr, où il est visible sans
  // casser le contrat de sortie.
  const journal = new console.Console(process.stderr, process.stderr);
  const window = { location: new URL(href), fetch: fetchDouble };
  const bac = {
    window, fetch: fetchDouble, console: journal, URL,
    // `chat.js` borne ses requêtes (AbortController + setTimeout) : le bac à sable est un realm
    // neuf, il n'hérite d'aucun global de Node.
    setTimeout: minuteurs ? minuteurs.setTimeout : setTimeout,
    clearTimeout: minuteurs ? minuteurs.clearTimeout : clearTimeout,
    AbortController,
  };
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

/** Laisse tourner les microtâches en attente : `reponseApi()` attend la sonde avant de poster. */
function tick() {
  return new Promise((r) => setTimeout(r, 0));
}

/** La requête `POST /chat` parmi les appels relevés — la sonde `/sante` la précède désormais. */
function requeteChat(appels) {
  return appels.filter((a) => String(a.url).endsWith("/chat"))[0] || null;
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

/**
 * Story 4.2f : une **lecture partielle** — 200, `found=false`, aucune preuve d'absence, mais des
 * compteurs, une affirmation écartée et la lacune qui dit la borne. C'est le corps que le pipeline
 * rend là où il levait `TruncatedRead`, et que la page recevait en 503 « assistant indisponible ».
 */
function lecturePartielle() {
  const phrase = "Je n'ai pas pu lire tout ce qui pouvait concerner votre question, et rien de ce " +
    "que j'ai lu n'a passé la vérification.";
  const manque = "Je n'ai pas pu lire tout ce qui pouvait concerner votre question : ma lecture a " +
    "été bornée, et des passages sont restés fermés.";
  const answer = {
    found: false, complete: false, lang: "fr", lang_fallback: false, texte: phrase,
    segments: [{ text: phrase, kind: "limite", claim_ids: [] }],
    claims: [],
    rejected_claims: [{
      claim_id: "c9", text: "Une affirmation que la vérification a écartée.",
      quotes: [{ block_id: "lux-guide:farrivee:2", quote: "CETTE QUOTE NE DOIT JAMAIS S'AFFICHER" }],
      status: { retrouvee: false, pertinente: null, applicable: null, edition: "git:a8e8593" },
      rejection_kind: "non_retrouvee", motif: "citation introuvable",
    }],
    reason: null,
    lecture_partielle: { nodes_read: 2, blocks_read: 5, documents: ["lux-guide"] },
    verdict: null, unknown: [manque], clarification: null,
  };
  return {
    texte: phrase, segments: answer.segments, sources: [], fiches: [], unknown: [manque],
    comparateur: false, answer, via: "api/v1",
    trace: { request_id: "r-4-2f", pipeline: "guide", intent: "question",
             total_cost_eur: 0.0181, steps: [] },
  };
}

/** Tous les nœuds d'un arbre de vue, à plat, dans l'ordre du document. */
function noeuds(vue) {
  if (!vue) return [];
  return [vue].concat((vue.enfants || []).flatMap(noeuds));
}

/** Les nœuds d'une classe donnée (la classe est un mot de `cls`). */
function parClasse(vue, cls) {
  return noeuds(vue).filter((n) => (n.cls || "").split(" ").indexOf(cls) !== -1);
}

/** Toutes les actions décrites par l'arbre, dans l'ordre. */
function actions(vue) {
  return noeuds(vue).filter((n) => n.action).map((n) => n.action);
}

/** Le texte d'un nœud de classe donnée, ou null. */
function texteDe(vue, cls) {
  const n = parClasse(vue, cls)[0];
  return n ? (n.texte !== undefined ? n.texte : null) : null;
}

/**
 * Résumé assertable du panneau « Pourquoi cette réponse » (story 2.5), ou `null` s'il n'est pas là.
 *
 * L'AC de la story est une **liste de rubriques** : elle se vérifie sur l'arbre composé, rubrique
 * par rubrique et ligne par ligne. Ce relevé garde donc la structure (un titre, ses lignes, l'état
 * ✓/✗ de chacune) au lieu d'aplatir en une chaîne, pour qu'une rubrique absente se distingue d'une
 * rubrique vide et qu'un contrôle échoué se distingue d'un contrôle passé.
 */
function resumerPourquoi(vue) {
  const panneau = parClasse(vue, "pourquoi")[0];
  if (!panneau) return null;
  const lignesDe = (bloc) => parClasse(bloc, "pq-ligne").map((l) => {
    const ok = parClasse(l, "pq-ok")[0];
    const ko = parClasse(l, "pq-ko")[0];
    return {
      etat: ok ? "ok" : (ko ? "ko" : null),
      texte: l.texte !== undefined ? l.texte
        : noeuds(l).filter((n) => n !== l && n.texte !== undefined && !(n.cls || "").match(/pq-(ok|ko)/))
            .map((n) => n.texte).join(" | "),
      // Le pictogramme est décoratif : sa valeur n'a de sens que masquée aux lecteurs d'écran.
      picto: (ok || ko || {}).attrs || null,
    };
  });
  const seuils = parClasse(panneau, "pq-seuils")[0] || null;
  return {
    tag: panneau.tag,
    summary: texteDe(panneau, "pq-sum"),
    rubriques: parClasse(panneau, "pq-bloc").map((b) => ({
      titre: texteDe(b, "pq-titre"),
      note: texteDe(b, "pq-note"),
      lignes: lignesDe(b),
    })),
    seuils: seuils ? {
      tag: seuils.tag,
      summary: (seuils.enfants || []).filter((n) => n.tag === "summary").map((n) => n.texte)[0],
      lignes: lignesDe(seuils).map((l) => l.texte),
    } : null,
    rejetees: parClasse(panneau, "pq-rejetee").map((r) => ({
      texte: texteDe(r, "pq-rej-txt"), motif: texteDe(r, "pq-rej-motif"),
    })),
    // Aucun `id` : l'arbre est peint **deux fois** dans le même document.
    attrs: noeuds(panneau).filter((n) => n.attrs).map((n) => n.attrs),
    ids: noeuds(panneau).filter((n) => n.attrs && n.attrs.id).length,
  };
}

/**
 * Résumé assertable d'une vue de réponse : ce que l'AC promet, et rien d'autre.
 * Chaque segment avec ses citations placées, la preuve, les inconnus, l'état, le coût, les actions.
 */
function resumerVue(vue) {
  return {
    pourquoi: resumerPourquoi(vue),
    retrait: texteDe(vue, "retrait"),
    cls_racine: vue.cls,
    clarification: texteDe(vue, "clarif-q"),
    ordre_des_blocs: (vue.enfants || []).map((n) => n.cls),
    segments: parClasse(vue, "seg").map((seg) => ({
      factuel: (seg.cls || "").split(" ").indexOf("seg-factuel") !== -1,
      texte: texteDe(seg, "seg-txt"),
      citations: parClasse(seg, "cite").map((c) => ({
        quote: texteDe(c, "cite-q"),
        fiche: texteDe(c, "cite-fiche"),
        fiche_texte: texteDe(c, "cite-fiche-txt"),
        lien: (parClasse(c, "cite-lien")[0] || {}).href || null,
        statut: texteDe(c, "cite-statut"),
      })),
    })),
    degrade: texteDe(vue, "degrade"),
    citations_plates: parClasse(vue, "cites")
      .filter((box) => !parClasse(vue, "seg").some((seg) => noeuds(seg).indexOf(box) !== -1))
      .flatMap((box) => parClasse(box, "cite").map((c) => ({
        quote: texteDe(c, "cite-q"), statut: texteDe(c, "cite-statut"),
      }))),
    preuve: texteDe(vue, "preuve"),
    inconnus: parClasse(vue, "inconnu").flatMap(
      (b) => noeuds(b).filter((n) => n.tag === "li").map((n) => n.texte)),
    etat: (parClasse(vue, "etat")[0] || {}).cls || null,
    etat_texte: texteDe(vue, "etat"),
    etat_phrase: texteDe(vue, "etat-phrase"),
    pied: parClasse(vue, "pied").flatMap((p) => (p.enfants || []).map((n) => n.cls)),
    mentions_langue: parClasse(vue, "langue-mention").map((n) => n.texte),
    repli_langue: texteDe(vue, "langue-repli"),
    cout: texteDe(vue, "cout"),
    sans_verification: texteDe(vue, "sans-verif"),
    attente: texteDe(vue, "attente-txt"),
    actions: actions(vue),
    tags_porteurs_de_texte: [...new Set(noeuds(vue).filter((n) => n.texte !== undefined)
      .map((n) => n.tag))].sort(),
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
    const post = requeteChat(appels);
    cas.corps_url = post.url;
    cas.corps_methode = post.options.method;
    cas.corps_entetes = post.options.headers;
    cas.corps_envoye = JSON.parse(post.options.body);
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
    let statutVu = null;
    const { CHAT, compteur } = chargerChat(PAGE, () => {
      const rep = reponseHttp({ corps: reponseSourcee() });
      statutVu = rep.status;
      return rep;
    });
    compteur.lectures = 0;
    const r = await CHAT.repondre(QUESTION, PROFIL, []);
    cas.statut_http_nominal = statutVu;
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
    // Un corps qui porte **les deux** : `texte` (le contrat servi) et `reponse` (l'ancien champ,
    // que rien n'oblige un intermédiaire à retirer). C'est `texte` qui est lu, et `reponse` ne
    // ressort nulle part.
    const avecEcho = {
      texte: "Texte du contrat servi, celui d'après la 1.7.",
      reponse: "Texte de l'ancien contrat, celui d'avant la 1.7.",
      sources: [{ block_id: "lux-guide:fbanque:1", fiche_id: "banque", titre: "Ouvrir un compte",
                  url: "https://guichet.public.lu/banque", quote: "un compte au Luxembourg",
                  status: "verifiee" }],
      // `found=true` exige au moins une claim retrouvée ∧ pertinente (`Answer._found_coherence`) :
      // un corps qui n'en porte pas n'est pas servable, et le front le refuse désormais.
      answer: {
        found: true, complete: false, texte: "", clarification: null, reason: null, unknown: [],
        segments: [{ text: "Vous pouvez ouvrir un compte au Luxembourg.", kind: "factuel",
                     claim_ids: ["c1"] }],
        claims: [{ claim_id: "c1", text: "Un compte peut être ouvert au Luxembourg.",
                   quotes: [{ block_id: "lux-guide:fbanque:1", quote: "un compte au Luxembourg",
                              start: 0, end: 10, text_start: 0, text_end: 10 }],
                   status: { retrouvee: true, pertinente: true, applicable: null,
                             edition: "git:a8e8593" } }],
        rejected_claims: [],
      },
      via: "api/v1",
      trace: { request_id: "r-3", pipeline: "guide", intent: "question", total_cost_eur: 0.01, steps: [] },
    };
    const { CHAT } = chargerChat(PAGE, () => reponseHttp({ corps: avecEcho }));
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

    // Un `claim_id` qui est un nom du prototype d'Object : sur un dictionnaire nu, `parClaim["toString"]`
    // trouverait un héritage, et l'abandon — la seule protection contre une citation mal placée —
    // ne se déclencherait pas (il lèverait, ou pire, laisserait passer).
    const heritee = reponseSourcee();
    heritee.answer.segments[1] = { ...heritee.answer.segments[1], claim_ids: ["toString"] };
    try {
      cas.citations_claim_id_herite = CHAT.citationsParSegment(heritee.answer, heritee.sources);
    } catch (e) {
      cas.citations_claim_id_herite = "exception: " + String(e && e.message);
    }
    const prototype = reponseSourcee();
    prototype.answer.claims[0] = { ...prototype.answer.claims[0], claim_id: "constructor" };
    prototype.answer.segments[0] = { ...prototype.answer.segments[0], claim_ids: ["constructor", "c2"] };
    try {
      cas.citations_claim_id_prototype = aplatir(
        CHAT.citationsParSegment(prototype.answer, prototype.sources));
    } catch (e) {
      cas.citations_claim_id_prototype = "exception: " + String(e && e.message);
    }

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

  // --- story 4.2f : la lecture partielle, lue puis peinte -----------------
  {
    const { CHAT, compteur } = chargerChat(PAGE, () => reponseHttp({ corps: lecturePartielle() }));
    compteur.lectures = 0;
    const r = await CHAT.repondre(QUESTION, PROFIL, []);
    cas.lecture_partielle = {
      texte: r.texte,
      segments_kind: r.segments.map((s) => s.kind),
      sources: r.sources.length,
      unknown: r.unknown,
      // Les deux porteurs, tels que la page les lit : l'un est là, l'autre non.
      preuve: CHAT.preuveAbsence(r.answer.reason),
      lecture: CHAT.lectureLue(r.answer.lecture_partielle),
      etat: CHAT.etatReponse(r.answer),
      rejetees: r.answer.rejected_claims.map((c) => c.rejection_kind),
      // Aucun repli n'est ouvert : ce n'est pas une indisponibilité (AD-11/AD-16).
      lectures_du_moteur_lexical: compteur.lectures,
    };
    cas.lecture_textes = {
      pluriel: CHAT.lectureLue({ nodes_read: 3, blocks_read: 12, documents: [] }),
      singulier: CHAT.lectureLue({ nodes_read: 1, blocks_read: 1, documents: [] }),
      plancher: CHAT.lectureLue({ nodes_read: 1, blocks_read: 1, documents: [] }),
      absente: CHAT.lectureLue(null),
    };
    cas.phrases_lecture_partielle = {
      avec_liste: CHAT.phraseEtat({ cle: "lecture-partielle" }, { liste: true, lecture: true }),
      sans_liste: CHAT.phraseEtat({ cle: "lecture-partielle" }, { liste: false, lecture: true }),
      // Les deux drapeaux comptent : la phrase ne promet « ci-dessus » que ce que la vue a peint.
      sans_chiffre: CHAT.phraseEtat({ cle: "lecture-partielle" }, { liste: true, lecture: false }),
      sans_rien: CHAT.phraseEtat({ cle: "lecture-partielle" }, { liste: false, lecture: false }),
    };
    cas.etats_lecture_partielle = {
      porteur: CHAT.etatReponse({ found: false, complete: false,
                                  lecture_partielle: { nodes_read: 1, blocks_read: 1 } }),
      sans_porteur: CHAT.etatReponse({ found: false, complete: false }),
    };
    cas.vue_lecture_partielle = resumerVue(CHAT.vueReponse(lecturePartielle(), QUESTION));
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

  // --- un 200 dont le corps ne tient pas le contrat ----------------------
  {
    // Le corps est du JSON valide, mais il ne tient pas le contrat **réellement servi** : champ
    // obligatoire absent, au premier étage (`texte`, `answer`, `trace`) comme dans les objets
    // imbriqués que l'écran consomme (`Trace.request_id`/`pipeline`, `AbsenceProof.kind`,
    // `AnswerSegment.text`/`kind`, `SourceItem.block_id`/`quote`/`status`, `VerifiedClaim.quotes`
    // non vide et `.status`) ; `null` sur un champ à valeur par défaut (aucun n'est `| None` dans le
    // contrat) ; ou invariant d'`Answer._found_coherence` violé.
    // Une valeur par défaut à leur place peignait un `{}` en réponse « inconnu », l'ajoutait à
    // l'historique et la faisait repartir au serveur : c'est la « réponse vide présentée comme
    // réponse » d'AD-16. Chacun de ces corps est **aussi** rejeté par `ChatResponse.model_validate()`
    // — le test Python le vérifie corps par corps, pour que la lecture du front ne dérive ni vers
    // plus strict (il refuserait une réponse servie) ni vers plus permissif (le cas d'ici).
    const TRACE = { request_id: "r-x", pipeline: "guide" };
    const REASON = { kind: "hors_perimetre", terms_searched: [], variants_count: 0, blocks_scanned: 0 };
    const CLAIM = {
      claim_id: "c1", text: "Le délai est de huit jours.",
      quotes: [{ block_id: "lux-guide:farrivee:2", quote: "huit jours", start: 0, end: 10,
                 text_start: 0, text_end: 10 }],
      status: { retrouvee: true, pertinente: true, applicable: null, edition: "git:a8e8593" },
    };
    const ANSWER_OK = { found: true, complete: true, claims: [CLAIM] };
    const corps = {
      vide: {},
      sans_answer: { texte: "Vous avez huit jours.", trace: TRACE },
      sans_trace: { texte: "x", answer: { found: true, complete: true, claims: [CLAIM] } },
      sans_texte: { answer: { found: true, complete: true, claims: [CLAIM] }, trace: TRACE },
      answer_nul: { texte: "x", answer: null, trace: TRACE },
      answer_sans_found: { texte: "x", answer: { complete: true }, trace: TRACE },
      answer_sans_complete: { texte: "x", answer: { found: true, claims: [CLAIM] }, trace: TRACE },
      // `Trace` n'a que deux champs sans valeur par défaut, et ce sont eux qui identifient la
      // requête : une trace qui ne les porte pas n'a été montée par aucun pipeline du projet.
      trace_sans_request_id: { texte: "x", answer: { found: true, complete: true, claims: [CLAIM] },
                               trace: { pipeline: "guide" } },
      trace_sans_pipeline: { texte: "x", answer: { found: true, complete: true, claims: [CLAIM] },
                             trace: { request_id: "r-x" } },
      // Les cinq invariants d'`Answer._found_coherence`, un par corps.
      answer_trouve_sans_claim: { texte: "x", answer: { found: true, complete: true, claims: [] },
                                  trace: TRACE },
      answer_absent_sans_reason: { texte: "x", answer: { found: false, complete: false, claims: [] },
                                   trace: TRACE },
      answer_absent_avec_claims: { texte: "x", answer: { found: false, complete: false,
                                                         claims: [CLAIM], reason: REASON },
                                   trace: TRACE },
      answer_claim_non_pertinente: {
        texte: "x", trace: TRACE,
        answer: { found: true, complete: true,
                  claims: [{ ...CLAIM, status: { retrouvee: true, pertinente: false,
                                                 applicable: null, edition: "git:a8e8593" } }] },
      },
      answer_complete_sans_found: { texte: "x", answer: { found: false, complete: true, claims: [],
                                                          reason: REASON },
                                    trace: TRACE },
      answer_complete_avec_unknown: { texte: "x", answer: { found: true, complete: true,
                                                            claims: [CLAIM], unknown: ["le coût"] },
                                      trace: TRACE },
      answer_claims_non_liste: { texte: "x", answer: { found: true, complete: true, claims: { 0: {} } },
                                 trace: TRACE },
      sources_non_liste: { texte: "x", answer: { found: true, complete: true, claims: [CLAIM] },
                           trace: TRACE, sources: { 0: {} } },
      // --- `null` n'est pas l'absence -----------------------------------
      // Aucun champ a valeur par defaut du contrat n'est `| None` : pydantic refuse `null` comme il
      // refuse une chaine a la place d'une liste. Les convertir en silence en valeur par defaut
      // peignait une reponse a partir d'un corps qu'aucune route ne peut ecrire (revue Codex 1.7,
      // B2, tour 3).
      segments_nuls: { texte: "x", answer: ANSWER_OK, trace: TRACE, segments: null },
      sources_nulles: { texte: "x", answer: ANSWER_OK, trace: TRACE, sources: null },
      fiches_nulles: { texte: "x", answer: ANSWER_OK, trace: TRACE, fiches: null },
      unknown_nul: { texte: "x", answer: ANSWER_OK, trace: TRACE, unknown: null },
      comparateur_nul: { texte: "x", answer: ANSWER_OK, trace: TRACE, comparateur: null },
      via_nul: { texte: "x", answer: ANSWER_OK, trace: TRACE, via: null },
      cout_nul: { texte: "x", answer: ANSWER_OK,
                  trace: { request_id: "r-x", pipeline: "guide", total_cost_eur: null } },
      answer_claims_nulles: { texte: "x", trace: TRACE,
                              answer: { found: false, complete: false, reason: REASON,
                                        claims: null } },
      answer_unknown_nul: { texte: "x", trace: TRACE,
                            answer: { found: true, complete: false, claims: [CLAIM],
                                      unknown: null } },
      answer_lang_nombre: { texte: "x", trace: TRACE,
                            answer: { found: true, complete: true, claims: [CLAIM], lang: 42 } },
      answer_lang_nul: { texte: "x", trace: TRACE,
                         answer: { found: true, complete: true, claims: [CLAIM], lang: null } },
      answer_lang_inconnu: { texte: "x", trace: TRACE,
                             answer: { found: true, complete: true, claims: [CLAIM], lang: "xx" } },
      answer_repli_nul: { texte: "x", trace: TRACE,
                          answer: { found: true, complete: true, claims: [CLAIM],
                                    lang_fallback: null } },
      vue_traduite_et_repliee: { texte: "x", trace: TRACE,
                                 answer: { found: true, complete: true, claims: [CLAIM], lang: "pt",
                                           lang_fallback: true } },
      // --- la preuve d'absence est un `AbsenceProof`, pas un objet quelconque ---
      // `reason.kind` est le seul champ obligatoire d'`AbsenceProof`, et celui dont l'ecran depend :
      // `clarification_requise` supprime la preuve chiffree, les trois autres l'affichent. Un
      // `reason: {}` passait pour un refus muni d'une preuve « 0 variante, 0 passage » que rien
      // n'avait calculee.
      answer_reason_vide: { texte: "x", trace: TRACE,
                            answer: { found: false, complete: false, reason: {} } },
      answer_reason_kind_inconnu: { texte: "x", trace: TRACE,
                                    answer: { found: false, complete: false,
                                              reason: { kind: "autre" } } },
      answer_reason_termes_nuls: { texte: "x", trace: TRACE,
                                   answer: { found: false, complete: false,
                                             reason: { kind: "zero_hit", terms_searched: null } } },
      // --- les champs obligatoires des objets imbriques que l'ecran consomme ---
      segment_sans_kind: { texte: "x", answer: ANSWER_OK, trace: TRACE,
                           segments: [{ text: "Vous avez huit jours." }] },
      source_sans_quote: { texte: "x", answer: ANSWER_OK, trace: TRACE,
                           sources: [{ block_id: "lux-guide:farrivee:2", status: "verifiee" }] },
      claim_sans_quote: { texte: "x", trace: TRACE,
                          answer: { found: true, complete: true,
                                    claims: [{ ...CLAIM, quotes: [] }] } },
      claim_sans_status: { texte: "x", trace: TRACE,
                           answer: { found: true, complete: true,
                                     claims: [{ claim_id: CLAIM.claim_id, text: CLAIM.text,
                                                quotes: CLAIM.quotes }] } },
      ancien_contrat: { reponse: "Vous avez huit jours.", sources: [] },
      // Un compteur de la preuve d'absence qui n'est pas un entier : il serait **affiche** tel quel.
      preuve_compteur_objet: { texte: "x", trace: TRACE,
                               answer: { found: false, complete: false,
                                         reason: { kind: "zero_hit", terms_searched: [], documents: [],
                                                   variants_count: {}, blocks_scanned: 0 } } },
      preuve_compteur_chaine: { texte: "x", trace: TRACE,
                                answer: { found: false, complete: false,
                                          reason: { kind: "zero_hit", terms_searched: [], documents: [],
                                                    variants_count: 3, blocks_scanned: "12" } } },
      // --- story 4.2f : le second porteur d'un `found=false`, lu strictement ---
      // Les compteurs sont **affichés** : absents, non entiers ou négatifs, ils peindraient une
      // lecture que rien n'a mesurée. Et « exactement un porteur » vaut ici comme dans le domaine :
      // aucun des deux, ou les deux, sont deux corps qu'aucune route ne peut écrire.
      answer_lecture_sans_compteur: {
        texte: "x", trace: TRACE,
        answer: { found: false, complete: false, unknown: ["il manque des passages"],
                  lecture_partielle: { blocks_read: 5, documents: [] } } },
      answer_lecture_compteur_negatif: {
        texte: "x", trace: TRACE,
        answer: { found: false, complete: false, unknown: ["il manque des passages"],
                  lecture_partielle: { nodes_read: -1, blocks_read: 5, documents: [] } } },
      answer_lecture_compteur_chaine: {
        texte: "x", trace: TRACE,
        answer: { found: false, complete: false, unknown: ["il manque des passages"],
                  lecture_partielle: { nodes_read: "2", blocks_read: 5, documents: [] } } },
      answer_lecture_compteur_fractionnaire: {
        texte: "x", trace: TRACE,
        answer: { found: false, complete: false, unknown: ["il manque des passages"],
                  lecture_partielle: { nodes_read: 1.5, blocks_read: 5, documents: [] } } },
      answer_deux_porteurs: {
        texte: "x", trace: TRACE,
        answer: { found: false, complete: false, unknown: ["il manque des passages"],
                  reason: REASON,
                  lecture_partielle: { nodes_read: 2, blocks_read: 5, documents: [] } } },
      // « Une lecture partielle dit ce qui lui manque » : le domaine l'exige, la page le refait.
      answer_lecture_sans_manque: {
        texte: "x", trace: TRACE,
        answer: { found: false, complete: false, unknown: [],
                  lecture_partielle: { nodes_read: 2, blocks_read: 5, documents: [] } } },
      // I1 : « `found=True` n'en porte aucun » vaut pour **les deux** porteurs. Un `found: true`
      // muni d'une preuve d'absence peindrait en même temps une réponse « sûre » et la preuve
      // chiffrée d'une absence.
      answer_reason_sur_reponse_trouvee: {
        texte: "x", trace: TRACE,
        answer: { found: true, complete: true, claims: [CLAIM], reason: REASON } },
      // I2 : zéro section pour au moins un passage est un état **impossible** (AD-2 rend la
      // résolution bloc → nœud totale), et zéro bloc reste une erreur terminale d'AD-1/NFR2.
      answer_lecture_sans_section: {
        texte: "x", trace: TRACE,
        answer: { found: false, complete: false, unknown: ["il manque des passages"],
                  lecture_partielle: { nodes_read: 0, blocks_read: 5, documents: [] } } },
      answer_lecture_sans_passage: {
        texte: "x", trace: TRACE,
        answer: { found: false, complete: false, unknown: ["il manque des passages"],
                  lecture_partielle: { nodes_read: 2, blocks_read: 0, documents: [] } } },
      answer_lecture_sur_reponse_trouvee: {
        texte: "x", trace: TRACE,
        answer: { found: true, complete: false, claims: [CLAIM],
                  unknown: ["il manque des passages"],
                  lecture_partielle: { nodes_read: 2, blocks_read: 5, documents: [] } } },
      // --- story 2.5 : ce que le panneau consomme est **strict sur le type** ---
      // Tolérant à l'absence (les deux lots sont écrits en parallèle, le front n'exige aucun de ces
      // champs), mais un champ **présent et mal typé** est un serveur cassé : l'afficher peindrait
      // « 0 relance » sur un compteur que rien n'a calculé, ou une ligne d'étape sans nom, sous le
      // panneau qui répond justement de l'honnêteté du reste. Chacun de ces corps est aussi refusé
      // par `ChatResponse.model_validate()` — le test Python le vérifie corps par corps.
      trace_blocs_non_liste: { texte: "x", answer: ANSWER_OK,
                               trace: { request_id: "r-x", pipeline: "guide", blocs: {} } },
      trace_bloc_titre_nombre: { texte: "x", answer: ANSWER_OK,
                                 trace: { request_id: "r-x", pipeline: "guide",
                                          blocs: [{ block_id: "b1", doc_id: "mini",
                                                    node_id: "mini:f1", fiche_id: "f1",
                                                    titre: 42 }] } },
      trace_gate_nombre: { texte: "x", answer: ANSWER_OK,
                           trace: { request_id: "r-x", pipeline: "guide", gate: 42 } },
      trace_gate_alerts_non_liste: { texte: "x", answer: ANSWER_OK,
                                     trace: { request_id: "r-x", pipeline: "guide",
                                              gate: { profile: "vertical", alerts: {} } } },
      trace_dictionnaire_non_booleen: { texte: "x", answer: ANSWER_OK,
                                        trace: { request_id: "r-x", pipeline: "guide",
                                                 dictionnaire: { charge: 5 } } },
      trace_retries_objet: { texte: "x", answer: ANSWER_OK,
                             trace: { request_id: "r-x", pipeline: "guide", retries: {} } },
      trace_steps_non_liste: { texte: "x", answer: ANSWER_OK,
                               trace: { request_id: "r-x", pipeline: "guide", steps: {} } },
      trace_step_check_ok_chaine: {
        texte: "x", answer: ANSWER_OK,
        trace: { request_id: "r-x", pipeline: "guide",
                 steps: [{ name: "verifier", checks: [{ name: "citations", ok: "peut-être" }] }] } },
      trace_thresholds_non_objet: { texte: "x", answer: ANSWER_OK,
                                    trace: { request_id: "r-x", pipeline: "guide",
                                             thresholds: [] } },
      trace_cout_negatif: { texte: "x", answer: ANSWER_OK,
                            trace: { request_id: "r-x", pipeline: "guide",
                                     total_cost_eur: -0.01 } },
      trace_cout_infini: { texte: "x", answer: ANSWER_OK,
                           trace: { request_id: "r-x", pipeline: "guide",
                                    total_cost_eur: Infinity } },
      trace_seuil_chaine: { texte: "x", answer: ANSWER_OK,
                            trace: { request_id: "r-x", pipeline: "guide",
                                     thresholds: { max_opens: "8" } } },
      trace_seuil_booleen: { texte: "x", answer: ANSWER_OK,
                             trace: { request_id: "r-x", pipeline: "guide",
                                      thresholds: { max_opens: true } } },
    };
    cas.contrat_incomplet = {};
    for (const [nom, c] of Object.entries(corps)) {
      const { CHAT, compteur } = chargerChat(PAGE, () => reponseHttp({ corps: c }));
      compteur.lectures = 0;
      let erreur = null;
      let reponse = null;
      try { reponse = await CHAT.repondre(QUESTION, PROFIL, []); } catch (e) { erreur = e; }
      cas.contrat_incomplet[nom] = {
        a_repondu: reponse !== null,
        kind: erreur && erreur.kind, code: erreur && erreur.code, champ: erreur && erreur.champ,
        message: CHAT.messageErreur(erreur),
        lectures_du_moteur_lexical: compteur.lectures,
        corps: c,  // relu par le test Python, qui le passe à `ChatResponse.model_validate()`
      };
    }
    // Et le contraire : un corps que le serveur **peut** servir passe, y compris quand tous les
    // champs à valeur par défaut sont absents. Le plus petit corps servable est un refus : `found`
    // à faux avec sa preuve d'absence, et les deux champs obligatoires de la trace.
    const minimal = { texte: "Cette question sort de ce que couvre le guide.",
                      answer: { found: false, complete: false, reason: REASON },
                      trace: { request_id: "r-min", pipeline: "guide" } };
    const { CHAT } = chargerChat(PAGE, () => reponseHttp({ corps: minimal }));
    const r = await CHAT.repondre(QUESTION, PROFIL, []);
    cas.contrat_minimal = { texte: r.texte, via: r.via, sources: r.sources, segments: r.segments,
                            comparateur: r.comparateur, corps: minimal };
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
    // AD-4 / AC : « N variantes essayées, M passages parcourus ». Zéro est une réponse à cette
    // question — un `hors_perimetre` court-circuite avant tout retrieval, et ses deux compteurs
    // nuls sont précisément ce qui le distingue d'un `zero_hit` qui a lu 312 passages.
    cas.preuve_zeros = {
      hors_perimetre: CHAT.preuveAbsence({ kind: "hors_perimetre", terms_searched: ["météo"],
                                           variants_count: 0, blocks_scanned: 0 }),
      sans_terme: CHAT.preuveAbsence({ kind: "hors_perimetre", terms_searched: [],
                                       variants_count: 0, blocks_scanned: 0 }),
      zero_hit: CHAT.preuveAbsence({ kind: "zero_hit", terms_searched: ["bail"],
                                     variants_count: 0, blocks_scanned: 506 }),
      claims_rejetes: CHAT.preuveAbsence({ kind: "claims_rejetes", terms_searched: ["bail", "préavis"],
                                           variants_count: 0, blocks_scanned: 506 }),
      clarification: CHAT.preuveAbsence({ kind: "clarification_requise", terms_searched: [],
                                          variants_count: 0, blocks_scanned: 0 }),
    };
    cas.etats = {
      sur: CHAT.etatReponse({ found: true, complete: true }),
      partiel: CHAT.etatReponse({ found: true, complete: false }),
      inconnu: CHAT.etatReponse({ found: false, complete: false }),
      absent: CHAT.etatReponse(null),
    };
    // Story 2.3 : la phrase qui accompagne le badge. Elle se compose sur ce que la vue a **peint**
    // (la liste des inconnues, la preuve d'absence), pas sur ce que le corps annonçait.
    cas.phrases_etat = {
      sur: CHAT.phraseEtat({ cle: "sur" }, { liste: false, preuve: false }),
      partiel_avec_liste: CHAT.phraseEtat({ cle: "partiel" }, { liste: true, preuve: false }),
      partiel_sans_liste: CHAT.phraseEtat({ cle: "partiel" }, { liste: false, preuve: false }),
      inconnu_avec_preuve: CHAT.phraseEtat({ cle: "inconnu" }, { liste: false, preuve: true }),
      inconnu_sans_preuve: CHAT.phraseEtat({ cle: "inconnu" }, { liste: false, preuve: false }),
      // Les bords : ni état ni contexte ne sont fabriqués, et rien ne lève.
      sans_contexte: CHAT.phraseEtat({ cle: "partiel" }, null),
      sans_etat: CHAT.phraseEtat(null, null),
      etat_inconnu_du_front: CHAT.phraseEtat({ cle: "farfelu" }, { liste: false, preuve: true }),
    };
    cas.exporte = Object.keys(CHAT).sort();
  }

  // --- ce que l'UI peindra : les arbres de vue -------------------------
  {
    const { CHAT } = chargerChat(PAGE, () => reponseHttp({ corps: reponseSourcee() }));
    const Q = QUESTION;

    cas.vue_attente = resumerVue(CHAT.vueAttente());

    const nominale = reponseSourcee();
    cas.vue_nominale = resumerVue(CHAT.vueReponse(nominale, Q));
    const langueAbsente = reponseSourcee();
    delete langueAbsente.answer.lang;
    delete langueAbsente.answer.lang_fallback;
    cas.vue_langue_absente = resumerVue(CHAT.vueReponse(langueAbsente, Q));

    // Story 2.4 : les trois états cohérents du pied. Un repli avec une langue non française est
    // couvert plus haut comme contrat cassé : le serveur force toujours `language="fr"` au repli.
    const traduite = reponseSourcee();
    traduite.answer.lang = "de";
    cas.vue_traduite = resumerVue(CHAT.vueReponse(traduite, Q));
    const repliee = reponseSourcee();
    repliee.answer.lang_fallback = true;
    cas.vue_repliee = resumerVue(CHAT.vueReponse(repliee, Q));
    const partielle = reponseSourcee();
    partielle.answer.complete = false;
    partielle.answer.unknown = ["montant exact", "délai de recours"];
    partielle.unknown = partielle.answer.unknown;
    cas.vue_partielle = resumerVue(CHAT.vueReponse(partielle, Q));

    // Story 2.3 : « partiel » sans liste. Le domaine l'interdit désormais (`found ∧ ¬complete ⇒
    // unknown ≠ []`), donc le serveur ne peut plus le servir — mais la page ne **refuse** pas une
    // réponse qu'il a jugée servable : elle dit ce qu'elle a, sans renvoyer à une liste absente.
    const partielleSansListe = reponseSourcee();
    partielleSansListe.answer.complete = false;
    partielleSansListe.answer.unknown = [];
    partielleSansListe.unknown = [];
    cas.vue_partielle_sans_liste = resumerVue(CHAT.vueReponse(partielleSansListe, Q));

    cas.vue_refus = resumerVue(CHAT.vueReponse(refus(), "Quel temps fera-t-il ?"));

    const clar = refus();
    clar.answer.reason.kind = "clarification_requise";
    clar.answer.reason.terms_searched = [];
    clar.answer.reason.variants_count = 0;
    clar.answer.reason.blocks_scanned = 0;
    clar.answer.clarification = "Parlez-vous du bail de votre logement ou de votre contrat de travail ?";
    cas.vue_clarification = resumerVue(CHAT.vueReponse(clar, "Et celui-là ?"));

    // Appariement abandonné (un segment cite une claim absente) : les `block_id` sont intacts, donc
    // le mode dégradé doit rendre les citations **et** leurs statuts — c'est là qu'on en dit le
    // moins, ce serait le pire endroit où taire la réserve d'actualité.
    const degradee = reponseSourcee();
    degradee.answer.segments[1] = { ...degradee.answer.segments[1], claim_ids: ["c9"] };
    cas.vue_degradee = resumerVue(CHAT.vueReponse(degradee, Q));

    // `answer.segments` vide alors que `segments[]` de premier niveau ne l'est pas.
    const sansSegmentsDansAnswer = reponseSourcee();
    sansSegmentsDansAnswer.answer.segments = [];
    cas.vue_segments_premier_niveau = resumerVue(CHAT.vueReponse(sansSegmentsDansAnswer, Q));

    // `fiche_id` que `kb.js` ne connaît pas : titre en texte, pas un bouton qui ouvrirait la liste.
    const ficheInconnue = reponseSourcee();
    ficheInconnue.sources = ficheInconnue.sources.map((s) => ({ ...s, fiche_id: "fiche_disparue" }));
    ficheInconnue.fiches = ["fiche_disparue"];
    cas.vue_fiche_inconnue = resumerVue(CHAT.vueReponse(ficheInconnue, Q));

    // Édition vide : la réserve d'actualité reste due (AD-4).
    const sansEdition = reponseSourcee();
    sansEdition.answer.claims = sansEdition.answer.claims.map((c) => ({
      ...c, status: { ...c.status, edition: "" } }));
    cas.vue_sans_edition = resumerVue(CHAT.vueReponse(sansEdition, Q));

    const compar = reponseSourcee();
    compar.comparateur = true;
    cas.vue_comparateur = resumerVue(CHAT.vueReponse(compar, Q));

    cas.vue_locale = resumerVue(CHAT.vueReponseLocale(CHAT.rechercheSimple(Q, PROFIL), Q));

    const err = (kind, code, extra = {}) => ({ kind, code, request_id: "req-1", ...extra });
    cas.vue_erreur_503 = resumerVue(CHAT.vueErreur(err("indisponible", "llm_unavailable"), Q));
    cas.vue_erreur_reseau = resumerVue(CHAT.vueErreur(err("indisponible", "reseau"), Q));
    cas.vue_erreur_400 = resumerVue(CHAT.vueErreur(err("requete", "invalid_request"), Q));
    cas.vue_erreur_429 = resumerVue(
      CHAT.vueErreur(err("requete", "rate_limited", { retry_after: 60 }), Q));
    cas.vue_erreur_500 = resumerVue(CHAT.vueErreur(err("requete", "internal"), Q));
    cas.vue_erreur_sans_request_id = resumerVue(
      CHAT.vueErreur({ kind: "requete", code: "internal" }, Q));
    cas.mode_apres_erreur = {
      indisponible: CHAT.modeApresErreur({ kind: "indisponible" }),
      requete: CHAT.modeApresErreur({ kind: "requete" }),
      aucune: CHAT.modeApresErreur(null),
    };
  }

  // --- l'historique n'emporte pas ce que la recherche simple a dit -------
  {
    const { CHAT } = chargerChat(PAGE, () => reponseHttp({ corps: reponseSourcee() }));
    const avecLocal = [
      { role: "user", content: "Quel délai pour déclarer mon arrivée ?" },
      { role: "assistant", content: "Réponse lexicale du guide, non vérifiée.", local: true },
      { role: "user", content: "Et pour l'école ?" },
      { role: "assistant", content: "Réponse vérifiée du serveur." },
    ];
    cas.historique_tour_local = CHAT.historiquePourApi(avecLocal, "Et ensuite ?");

    // Un tour surdimensionné au **milieu** : on garde la queue contiguë, jamais un trou.
    const troue = [
      { role: "user", content: "tour 1" },
      { role: "assistant", content: "tour 2" },
      { role: "user", content: "tour 3" },
      { role: "assistant", content: "z".repeat(2400) },
      { role: "user", content: "tour 5" },
      { role: "assistant", content: "tour 6" },
    ];
    cas.historique_queue_contigue = CHAT.historiquePourApi(troue, "autre chose");

    // Deux trous : seule la queue qui suit le **dernier** est envoyée.
    const deuxTrous = [
      { role: "user", content: "tour 1" },
      { role: "assistant", content: "y".repeat(2400) },
      { role: "user", content: "tour 3" },
      { role: "assistant", content: "réponse locale", local: true },
      { role: "user", content: "tour 5" },
    ];
    cas.historique_deux_trous = CHAT.historiquePourApi(deuxTrous, "autre chose");
    cas.historique_max_explicite = CHAT.historiquePourApi(
      [1, 2, 3, 4, 5].map((i) => ({ role: "user", content: "t" + i })), "q", 2);
  }

  // --- story 2.2 : le tour assistant conservé, et la boucle refermée -------
  //
  // `tourAssistant` compose **ce que l'assistant a dit**, dans l'ordre affiché. C'est la seule
  // pièce nouvelle de l'ensemble A ; ce qui suit la relève sur les trois formes qu'une réponse
  // prend, puis rejoue le tour suivant de bout en bout : la clarification entre dans l'historique
  // de page, l'utilisateur répond en trois mots, et l'on regarde ce qui part au serveur.
  {
    const { CHAT } = chargerChat(PAGE, () => reponseHttp({ corps: reponseSourcee() }));

    // (a) réponse ordinaire : le tour est `texte`, octet pour octet.
    const nominale = reponseSourcee();
    cas.tour_assistant_ordinaire = {
      tour: CHAT.tourAssistant(nominale),
      texte: nominale.texte,
      clarification: nominale.answer.clarification,
    };

    // (b) clarification : la question posée, puis la phrase générique du serveur.
    const clar = refus();
    clar.answer.reason.kind = "clarification_requise";
    clar.answer.reason.terms_searched = [];
    clar.answer.reason.variants_count = 0;
    clar.answer.reason.blocks_scanned = 0;
    clar.answer.clarification = "De quel document ou démarche parlez-vous ?";
    clar.answer.texte = "Je n'ai pas pu déterminer à quoi votre question fait référence ; " +
      "précisez-la et je chercherai.";
    clar.answer.segments = [{ text: clar.answer.texte, kind: "limite", claim_ids: [] }];
    clar.texte = clar.answer.texte;
    clar.segments = clar.answer.segments;
    cas.tour_assistant_clarification = {
      tour: CHAT.tourAssistant(clar),
      clarification: clar.answer.clarification,
      texte: clar.texte,
    };

    // (c) les formes vides et absentes : rien n'est fabriqué, et rien ne lève.
    cas.tour_assistant_bords = {
      vide: CHAT.tourAssistant({ texte: "", answer: { clarification: null } }),
      sans_answer: CHAT.tourAssistant({ texte: "Réponse lexicale du guide, non vérifiée." }),
      absent: CHAT.tourAssistant(null),
      clarification_seule: CHAT.tourAssistant({ texte: "", answer: { clarification: "Lequel ?" } }),
    };

    // (d) la boucle complète : le tour composé est poussé dans l'historique de page — comme
    // `ui.js` le fait —, puis l'utilisateur répond « du permis de conduire ».
    const historique = [
      { role: "user", content: "Et celui-là, il faut le faire quand ?" },
      { role: "assistant", content: CHAT.tourAssistant(clar) },
      { role: "user", content: "du permis de conduire" },
    ];
    cas.boucle_refermee = CHAT.historiquePourApi(historique, "du permis de conduire");

    // (e) la recherche simple, composée par la **même** fonction, reste marquée `local` : son tour
    // ne repart pas, et il coupe ce qui le précède (règle 1.7, inchangée).
    const locale = CHAT.rechercheSimple("Quel délai pour déclarer mon arrivée ?", PROFIL);
    cas.tour_assistant_local = {
      tour_egale_texte: CHAT.tourAssistant(locale) === locale.texte,
      texte_non_vide: typeof locale.texte === "string" && locale.texte.length > 0,
      envoye: CHAT.historiquePourApi([
        { role: "user", content: "Quel délai pour déclarer mon arrivée ?" },
        { role: "assistant", content: CHAT.tourAssistant(locale), local: true },
        { role: "user", content: "Et celui-là, il faut le faire quand ?" },
        { role: "assistant", content: CHAT.tourAssistant(clar) },
      ], "Et ensuite ?"),
    };

    // (f) une clarification trop longue pour tenir dans un tour (revue 2.2, P1). Rien ne borne
    // `ClarificationRequise.clarification` côté serveur et `comprendre_max_tokens` vaut 1 024 : le
    // cas est atteignable. Composé sans borne, le tour dépassait `Turn.texte` et
    // `historiquePourApi` l'écartait **avec tout ce qui le précédait** — la question de
    // l'utilisateur comprise —, si bien que *comprendre* recevait un historique vide et reposait la
    // même question. On relève ici les longueurs et ce qui part vraiment au serveur.
    const longue = refus();
    longue.answer.reason.kind = "clarification_requise";
    longue.answer.reason.terms_searched = [];
    longue.answer.reason.variants_count = 0;
    longue.answer.reason.blocks_scanned = 0;
    longue.answer.texte = clar.answer.texte;
    longue.texte = clar.texte;
    // 1 968 caractères : la mesure de la revue. Avec la phrase générique (94) et l'espace, le tour
    // composé sans borne faisait 2 063 — au-delà des 2 000 de `Turn.texte`.
    longue.answer.clarification = "De quel document parlez-vous ? " + "x".repeat(1937);
    const tourLong = CHAT.tourAssistant(longue);
    cas.tour_assistant_clarification_longue = {
      clarification: longue.answer.clarification.length,
      texte: longue.texte.length,
      compose_sans_borne: longue.answer.clarification.length + 1 + longue.texte.length,
      tour: tourLong.length,
      // Le morceau gardé l'est **entier** : jamais un préfixe de la clarification.
      tour_est_la_clarification_entiere: tourLong === longue.answer.clarification,
      tour_ne_coupe_rien: tourLong === longue.answer.clarification || tourLong === longue.texte,
      envoye: CHAT.historiquePourApi([
        { role: "user", content: "Et celui-là, il faut le faire quand ?" },
        { role: "assistant", content: tourLong },
        { role: "user", content: "du permis de conduire" },
      ], "du permis de conduire"),
    };

    // Et le cas où même la clarification seule ne tient pas : le tour redevient `texte`, l'échange
    // reste envoyable, et le fil ne se coupe pas — la question est perdue, pas la conversation.
    const enorme = refus();
    enorme.answer.reason.kind = "clarification_requise";
    enorme.answer.texte = clar.answer.texte;
    enorme.texte = clar.texte;
    enorme.answer.clarification = "y".repeat(2400);
    const tourEnorme = CHAT.tourAssistant(enorme);
    cas.tour_assistant_clarification_enorme = {
      clarification: enorme.answer.clarification.length,
      tour: tourEnorme.length,
      tour_est_le_texte: tourEnorme === enorme.texte,
      envoye: CHAT.historiquePourApi([
        { role: "user", content: "Et celui-là, il faut le faire quand ?" },
        { role: "assistant", content: tourEnorme },
        { role: "user", content: "du permis de conduire" },
      ], "du permis de conduire"),
    };

    // Le cas que le contrat autorise **au plus** (revue Codex 2.2, B1) : depuis que le serveur borne
    // `Answer.clarification` par la valeur de `Turn.texte`, une clarification a la borne exacte est
    // le pire que la page puisse recevoir. Elle ne tient pas avec la phrase generique — le tour vaut
    // alors la clarification **entiere**, donc la question posee repart quand meme au serveur.
    const aLaBorne = refus();
    aLaBorne.answer.reason.kind = "clarification_requise";
    aLaBorne.answer.texte = clar.answer.texte;
    aLaBorne.texte = clar.texte;
    aLaBorne.answer.clarification = "De quel document parlez-vous ? " +
      "z".repeat(2000 - "De quel document parlez-vous ? ".length);
    const tourBorne = CHAT.tourAssistant(aLaBorne);
    cas.tour_assistant_clarification_a_la_borne = {
      clarification: aLaBorne.answer.clarification.length,
      clarification_texte: aLaBorne.answer.clarification,
      compose_sans_borne: aLaBorne.answer.clarification.length + 1 + aLaBorne.texte.length,
      tour: tourBorne.length,
      tour_est_la_clarification_entiere: tourBorne === aLaBorne.answer.clarification,
      envoye: CHAT.historiquePourApi([
        { role: "user", content: "Et celui-là, il faut le faire quand ?" },
        { role: "assistant", content: tourBorne },
        { role: "user", content: "du permis de conduire" },
      ], "du permis de conduire"),
    };

    // (g) la composition que `tests/test_suivi_live.py` écrit en Python (revue 2.2, P5). Deux
    // implémentations d'une même règle qu'aucune assertion ne relierait divergeraient un jour :
    // c'est `tourAssistant` qui fait autorité, et le test Python compare les deux chaînes.
    cas.tour_assistant_du_live = CHAT.tourAssistant({
      texte: "Je n'ai pas pu déterminer à quoi votre question fait référence ; précisez-la et " +
        "je chercherai.",
      answer: { clarification: "De quel document ou démarche parlez-vous ?" },
    });
  }

  // --- story 2.5 : une trace complète, telle que le Lot A la publie -------
  //
  // Écrite **à la main**, contre le contrat décrit par la spec (`BlocTrace`, `GateTrace`,
  // `DictionnaireTrace` et les trois champs de `Trace`) : les deux lots sont implémentés en
  // parallèle, et le harnais du front ne peut pas attendre que le serveur pose ses champs pour
  // exister. `tests/test_api.py` (Lot A) vérifie l'autre bord — que la route écrit bien ce contrat.
  function traceRiche(surcharge) {
    return Object.assign({
      request_id: "r-trace", pipeline: "guide", variant: "deterministe", intent: "question",
      steps: [
        { name: "comprendre", tier: "micro", ms: 912,
          opened_block_ids: [], discarded_block_ids: [],
          checks: [{ name: "intention_expliquee", ok: true,
                     detail: "intention « question » — 3 déclencheur(s) du dictionnaire sur 30 la confirment" }] },
        { name: "retrouver", tier: "reason", ms: 3480,
          opened_block_ids: ["lux-guide:farrivee:2", "lux-guide:farrivee:3"],
          discarded_block_ids: ["lux-guide:fbanque:1"],
          checks: [{ name: "noeuds_du_profil", ok: true, detail: "2 fiche(s) désignée(s)" },
                   { name: "dictionnaire", ok: true, detail: "7 variante(s) ajoutée(s) à 3 terme(s)" }] },
        { name: "rediger", tier: "reason", ms: 12800, opened_block_ids: [], discarded_block_ids: [],
          checks: [] },
        { name: "verifier", tier: "micro", ms: 1450, opened_block_ids: [], discarded_block_ids: [],
          checks: [{ name: "citations", ok: true, detail: "3 affirmation(s) retenue(s), 1 rejetée(s) sur 4" },
                   { name: "facettes_non_couvertes", ok: false,
                     detail: "1 facette(s) couverte(s) sur 2 posée(s)" }] },
        { name: "restituer", tier: null, ms: 2, opened_block_ids: [], discarded_block_ids: [],
          checks: [] },
      ],
      total_cost_eur: 0.0278,
      thresholds: { max_opens: 8, search_limit: 40, quote_min_chars: 24 },
      retries: 1,
      truncations: 0,
      deadline_remaining_s: 36.25,
      blocs: [
        { block_id: "lux-guide:farrivee:2", doc_id: "lux-guide", node_id: "lux-guide:farrivee",
          fiche_id: "arrivee", titre: "Les huit premiers jours" },
        { block_id: "lux-guide:farrivee:3", doc_id: "lux-guide", node_id: "lux-guide:farrivee",
          fiche_id: "arrivee", titre: "Les huit premiers jours" },
        { block_id: "lux-guide:fbanque:1", doc_id: "lux-guide", node_id: "lux-guide:fbanque",
          fiche_id: "banque", titre: "Ouvrir un compte" },
      ],
      gate: { profile: "vertical", cases: 2, countersigned: false, alerts: ["gate_perime"] },
      dictionnaire: { charge: true, validated: false, corpus_ok: true, court_circuit_actif: false },
    }, surcharge || {});
  }

  {
    const { CHAT } = chargerChat(PAGE, () => reponseHttp({ corps: reponseSourcee() }));
    const Q = QUESTION;

    // M1 — le panneau complet : toutes les rubriques que l'AC énumère.
    const complete = reponseSourcee();
    complete.trace = traceRiche();
    complete.answer.rejected_claims = [
      { claim_id: "c9", text: "Le certificat coûte dix euros.",
        quotes: [{ block_id: "lux-guide:fautre:1", quote: "CETTE QUOTE NE DOIT JAMAIS S'AFFICHER" }],
        status: { retrouvee: false, pertinente: null, applicable: null, edition: "git:a8e8593" },
        rejection_kind: "non_retrouvee", motif: "citation introuvable" },
    ];
    cas.vue_pourquoi_complet = resumerVue(CHAT.vueReponse(complete, Q));
    cas.pourquoi_texte_entier = noeuds(CHAT.vueReponse(complete, Q))
      .filter((n) => n.texte !== undefined).map((n) => n.texte).join(" ");

    // M2 — trace pauvre : les trois champs de la story sont **absents**, pas nuls. Aucune rubrique
    // inventée, aucun « inconnu », aucune ligne vide.
    const pauvre = reponseSourcee();
    pauvre.trace = { request_id: "r-pauvre", pipeline: "guide", total_cost_eur: 0.004 };
    cas.vue_pourquoi_pauvre = resumerVue(CHAT.vueReponse(pauvre, Q));

    // Et le cas où `gate` et `dictionnaire` sont **présents et nuls** (ce que `Trace` déclare) :
    // `null` n'est pas une mesure non plus.
    const nuls = reponseSourcee();
    nuls.trace = traceRiche({ gate: null, dictionnaire: null, blocs: [] });
    cas.vue_pourquoi_champs_nuls = resumerVue(CHAT.vueReponse(nuls, Q));

    // M3 — un `block_id` que `trace.blocs` ne résout pas : la ligne porte l'id **seul**.
    const nonResolu = reponseSourcee();
    nonResolu.trace = traceRiche({
      blocs: [{ block_id: "lux-guide:farrivee:2", doc_id: "lux-guide", node_id: "lux-guide:farrivee",
                fiche_id: "arrivee", titre: "Les huit premiers jours" }],
    });
    cas.vue_pourquoi_bloc_non_resolu = resumerVue(CHAT.vueReponse(nonResolu, Q));

    // M12 — dictionnaire signé : la phrase du refus « zéro hit » bascule, elle n'est pas recalculée.
    const arme = reponseSourcee();
    arme.trace = traceRiche({
      dictionnaire: { charge: true, validated: true, corpus_ok: true, court_circuit_actif: true },
    });
    cas.vue_pourquoi_dictionnaire_arme = resumerVue(CHAT.vueReponse(arme, Q));

    // Le motif du désarmement suit les faits publiés : absence, corpus périmé, puis signature.
    cas.pourquoi_dictionnaires_desarmes = {};
    for (const [nom, dictionnaire] of Object.entries({
      absent: { charge: false, validated: false, corpus_ok: false, court_circuit_actif: false },
      perime: { charge: true, validated: true, corpus_ok: false, court_circuit_actif: false },
      non_signe: { charge: true, validated: false, corpus_ok: true, court_circuit_actif: false },
    })) {
      const r = reponseSourcee();
      r.trace = traceRiche({ dictionnaire });
      cas.pourquoi_dictionnaires_desarmes[nom] = resumerVue(CHAT.vueReponse(r, Q));
    }

    // Un délai négatif est un dépassement mesuré, jamais une « durée restante » négative.
    const depassee = reponseSourcee();
    depassee.trace = traceRiche({ deadline_remaining_s: -2.25 });
    cas.vue_pourquoi_deadline_depassee = resumerVue(CHAT.vueReponse(depassee, Q));

    // `profile=null` et l'alerte `sans_gate` décrivent le même fait : une seule ligne suffit.
    const sansGate = reponseSourcee();
    sansGate.trace = traceRiche({
      gate: { profile: null, cases: null, countersigned: null, alerts: ["sans_gate"] },
    });
    cas.vue_pourquoi_sans_gate = resumerVue(CHAT.vueReponse(sansGate, Q));

    // Une trace qui ne porte **que** ses deux champs obligatoires n'a rien à expliquer : pas de
    // panneau plutôt qu'un `<details>` vide.
    const muette = reponseSourcee();
    muette.trace = { request_id: "", pipeline: "" };
    cas.vue_pourquoi_muette = resumerVue(CHAT.vueReponse(muette, Q));

    // Un contrôle dont le `name` n'est pas dans la table : affiché **tel quel**, jamais masqué.
    const inconnu = reponseSourcee();
    inconnu.trace = traceRiche({
      steps: [{ name: "verifier", tier: "micro", ms: 10, opened_block_ids: [],
                discarded_block_ids: [],
                checks: [{ name: "controle_de_demain", ok: false, detail: "détail" }] }],
    });
    cas.vue_pourquoi_controle_inconnu = resumerVue(CHAT.vueReponse(inconnu, Q));

    // Une alerte de gate que la table ne connaît pas : dite telle quelle, sans phrase inventée.
    const alerteInconnue = reponseSourcee();
    alerteInconnue.trace = traceRiche({
      gate: { profile: null, cases: null, countersigned: null, alerts: ["alerte_de_demain"] },
    });
    cas.vue_pourquoi_alerte_inconnue = resumerVue(CHAT.vueReponse(alerteInconnue, Q));

    // Les tables, relevées telles quelles : `tests/test_tables_partagees.py` les confronte à celles
    // de l'accueil et du sinistre, et `tests/test_web_chat.py` à celles du serveur.
    cas.tables = {
      alertes: CHAT.ALERTES, controles: CHAT.CONTROLES, rejets: CHAT.REJETS, reserves: CHAT.RESERVES,
      controle_inconnu: CHAT.libelleControle("controle_de_demain"),
      controle_connu: CHAT.libelleControle("refus"),
      motif_inconnu: CHAT.motifRejet("kind_de_demain"),
    };
  }

  // --- M10 : une enveloppe d'erreur qui porte une trace ------------------
  {
    const enveloppe = (trace) => ({
      error: { code: "llm_unavailable", message: "provider down", request_id: "req-503" },
      trace: trace,
    });
    const { CHAT } = chargerChat(PAGE, () => reponseHttp({ status: 503, corps: enveloppe(traceRiche({
      steps: [{ name: "comprendre", tier: "micro", ms: 900, opened_block_ids: [],
                discarded_block_ids: [], checks: [] }],
    })) }));
    let erreur = null;
    try { await CHAT.repondre(QUESTION, PROFIL, []); } catch (e) { erreur = e; }
    cas.erreur_avec_trace = {
      kind: erreur && erreur.kind, code: erreur && erreur.code,
      a_une_trace: !!(erreur && erreur.trace),
      vue: resumerVue(CHAT.vueErreur(erreur, QUESTION)),
    };

    // Une trace qui ne tient pas le contrat ne devient pas un panneau : l'échec reste celui du
    // serveur (503), il perd seulement son panneau.
    const { CHAT: c2 } = chargerChat(PAGE, () => reponseHttp({
      status: 503, corps: enveloppe({ request_id: 42, pipeline: "guide" }) }));
    let e2 = null;
    try { await c2.repondre(QUESTION, PROFIL, []); } catch (e) { e2 = e; }
    cas.erreur_trace_cassee = {
      kind: e2 && e2.kind, code: e2 && e2.code, a_une_trace: !!(e2 && e2.trace),
      vue: resumerVue(c2.vueErreur(e2, QUESTION)),
    };

    // Et sans trace du tout : pas de panneau (« trace absente ⇒ pas de panneau »).
    const { CHAT: c3 } = chargerChat(PAGE, () => reponseHttp({
      status: 503, corps: { error: { code: "llm_unavailable", message: "", request_id: "req-x" } } }));
    let e3 = null;
    try { await c3.repondre(QUESTION, PROFIL, []); } catch (e) { e3 = e; }
    cas.erreur_sans_trace = {
      a_une_trace: !!(e3 && e3.trace), vue: resumerVue(c3.vueErreur(e3, QUESTION)),
    };
  }

  // --- M6 / M7 : le message, zéro action, le badge conservé ---------------
  {
    const { CHAT } = chargerChat(PAGE, () => reponseHttp({
      status: 429, entetes: { "Retry-After": "30" },
      corps: { error: { code: "rate_limited", message: "slow down", request_id: "req-429" } },
    }));
    let erreur = null;
    try { await CHAT.repondre(QUESTION, PROFIL, []); } catch (e) { erreur = e; }
    cas.limite_de_debit = {
      retry_after: erreur && erreur.retry_after,
      message: CHAT.messageErreur(erreur),
      mode_apres: CHAT.modeApresErreur(erreur),
      vue: resumerVue(CHAT.vueErreur(erreur, QUESTION)),
    };
    const maintenant = Date.parse("Tue, 25 Aug 2026 10:00:00 GMT");
    cas.retry_after_strict = {
      secondes: CHAT.retryApres("30", maintenant),
      date: CHAT.retryApres("Tue, 25 Aug 2026 10:00:30 GMT", maintenant),
      date_passee: CHAT.retryApres("Tue, 25 Aug 2026 09:59:30 GMT", maintenant),
      suffixe: CHAT.retryApres("30 secondes", maintenant),
      decimal: CHAT.retryApres("1.5", maintenant),
      signe: CHAT.retryApres("+30", maintenant),
      date_impossible: CHAT.retryApres("Tue, 32 Aug 2026 10:00:30 GMT", maintenant),
    };
    const { CHAT: c2 } = chargerChat(PAGE, () => reponseHttp({
      status: 400, corps: { error: { code: "invalid_request", message: "body.historique: …",
                                     request_id: "req-400" } } }));
    let e2 = null;
    try { await c2.repondre(QUESTION, PROFIL, []); } catch (e) { e2 = e; }
    cas.requete_refusee = {
      message: c2.messageErreur(e2), mode_apres: c2.modeApresErreur(e2),
      message_du_serveur_affiche: c2.messageErreur(e2).indexOf("body.historique") !== -1,
      vue: resumerVue(c2.vueErreur(e2, QUESTION)),
    };
  }

  // --- M9 : la phrase du retrait, composée par `chat.js` ------------------
  {
    const { CHAT } = chargerChat(PAGE, () => reponseHttp({ corps: reponseSourcee() }));
    cas.vue_erreur_avec_retrait = resumerVue(
      CHAT.vueErreur({ kind: "indisponible", code: "reseau" }, QUESTION, { tour_retire: true }));
    cas.vue_erreur_sans_retrait = resumerVue(
      CHAT.vueErreur({ kind: "indisponible", code: "reseau" }, QUESTION, { tour_retire: false }));
  }

  // --- les bornes viennent du serveur, pas d'une copie ------------------
  {
    const { CHAT } = chargerChat(PAGE, (url) => (String(url).endsWith("/sante")
      ? reponseHttp({ corps: { ok: true, version: "abc", documents_servis: ["lux-guide"],
                               thresholds: { historique_max_turns: 3, deadline_s: 20 } } })
      : reponseHttp({ corps: reponseSourcee() })));
    cas.bornes_avant_sonde = CHAT.bornes();
    await CHAT.testerApi();
    cas.bornes_apres_sonde = CHAT.bornes();
    const dix = [1,2,3,4,5,6,7,8,9,10].map((i) => ({ role: "user", content: "tour " + i }));
    cas.historique_borne_par_le_serveur = CHAT.historiquePourApi(dix, "q").length;
  }

  // --- la **première** requête part déjà sur les seuils du serveur -------
  {
    // Sans attente de la sonde, la première question utilisait les replis écrits dans `chat.js` et
    // ignorait une configuration différente : un serveur réglé à 3 tours recevait les 6 du repli,
    // donc un 400. Ici la sonde n'a **pas** été appelée avant : c'est `reponseApi()` qui l'attend.
    const { CHAT, appels } = chargerChat(PAGE, (url) => (String(url).endsWith("/sante")
      ? reponseHttp({ corps: { ok: true, version: "abc", documents_servis: ["lux-guide"],
                               thresholds: { historique_max_turns: 2, deadline_s: 20,
                                             client_abort_margin_s: 4 } } })
      : reponseHttp({ corps: reponseSourcee() })));
    const dix = [1,2,3,4,5,6,7,8,9,10].map((i) => ({ role: "user", content: "tour " + i }));
    await CHAT.repondre(QUESTION, PROFIL, dix);
    cas.premiere_requete = {
      // La sonde d'abord, la question ensuite : deux appels, dans cet ordre.
      urls: appels.map((a) => String(a.url).replace(ORIGINE, "")),
      historique_envoye: JSON.parse(requeteChat(appels).options.body).historique.length,
      bornes: CHAT.bornes(),
    };
  }

  // --- la marge d'abandon vient de `config.py`, pas d'un nombre écrit ici -
  {
    const poses = [];
    const minuteurs = {
      setTimeout: (fn, ms) => { poses.push({ fn, ms, annule: false }); return poses.length; },
      clearTimeout: (n) => { if (poses[n - 1]) poses[n - 1].annule = true; },
    };
    const { CHAT } = chargerChat(PAGE, (url) => (String(url).endsWith("/sante")
      ? reponseHttp({ corps: { ok: true, version: "abc",
                               thresholds: { deadline_s: 20, client_abort_margin_s: 4 } } })
      : reponseHttp({ corps: reponseSourcee() })), { minuteurs });
    await CHAT.testerApi();
    cas.marge_du_serveur = CHAT.bornes().delai_abandon_ms;
  }

  // --- le questionnaire du site et le filtre du serveur ------------------
  {
    const { CHAT } = chargerChat(PAGE, () => reponseHttp({ corps: reponseSourcee() }));
    cas.champs_du_questionnaire = CHAT.CHAMPS.map((c) => c.cle);
  }

  // --- une requête qui pend est abandonnée, et c'est une indisponibilité --
  {
    const poses = [];
    const minuteurs = {
      setTimeout: (fn, ms) => { poses.push({ fn, ms, annule: false }); return poses.length; },
      clearTimeout: (n) => { if (poses[n - 1]) poses[n - 1].annule = true; },
    };
    const { CHAT } = chargerChat(PAGE, (url, options) => {
      // La sonde répond (elle porte les seuils) ; c'est la **question** qui pend.
      if (String(url).endsWith("/sante")) {
        return reponseHttp({ corps: { ok: true, version: "abc", documents_servis: ["lux-guide"] } });
      }
      return new Promise((_, rej) => {
        // `fetch` ne résout jamais ; seul l'abandon met fin à l'attente.
        options.signal.addEventListener("abort", () => rej(new Error("abandon")));
      });
    }, { minuteurs });
    await CHAT.testerApi();
    const posesAvantQuestion = poses.length;
    const promesse = CHAT.repondre(QUESTION, PROFIL, []);
    await tick();  // la requête ne part qu'après la sonde : son minuteur n'existe pas avant
    // Le minuteur laissé en vol après la sonde est celui de la question.
    const enVol = poses.slice(posesAvantQuestion).filter((p) => !p.annule);
    cas.abandon_delai_pose_ms = enVol.length ? enVol[0].ms : null;
    enVol.forEach((p) => p.fn());
    let erreur = null;
    try { await promesse; } catch (e) { erreur = e; }
    cas.abandon = {
      kind: erreur && erreur.kind, code: erreur && erreur.code,
      message: CHAT.messageErreur(erreur),
    };
  }

  // --- 200 sur `/sante` puis un corps qui ne vient jamais ----------------
  // Revue Codex 1.10, I1 : la minuterie de la sonde était coupée à la réception des **en-têtes**,
  // avant `r.json()`. Un serveur qui envoie un 200 puis bloque sur le corps laissait la sonde
  // pendre sans abandon armé — et la première question attend la sonde (`reponseApi`), donc la
  // saisie restait verrouillée sans fin, contre la borne annoncée. Même montage pour la question.
  {
    const poses = [];
    const minuteurs = {
      setTimeout: (fn, ms) => { poses.push({ fn, ms, annule: false }); return poses.length; },
      clearTimeout: (n) => { if (poses[n - 1]) poses[n - 1].annule = true; },
    };
    const corpsQuiPend = (options) => ({
      ok: true, status: 200,
      headers: { get: () => null },
      json: () => new Promise((_, rej) => {
        options.signal.addEventListener("abort", () => rej(new Error("abandon")));
      }),
    });
    const { CHAT } = chargerChat(PAGE, (url, options) => Promise.resolve(corpsQuiPend(options)),
                                 { minuteurs });
    const promesse = CHAT.testerApi();
    await tick();
    const enVol = poses.filter((p) => !p.annule);
    cas.sonde_corps_qui_pend_minuteur_arme = enVol.length;
    enVol.forEach((p) => p.fn());
    cas.sonde_corps_qui_pend = await promesse;
  }

  // --- 200 sur `/chat` puis un corps qui ne vient jamais -----------------
  {
    const poses = [];
    const minuteurs = {
      setTimeout: (fn, ms) => { poses.push({ fn, ms, annule: false }); return poses.length; },
      clearTimeout: (n) => { if (poses[n - 1]) poses[n - 1].annule = true; },
    };
    const { CHAT } = chargerChat(PAGE, (url, options) => {
      if (String(url).endsWith("/sante")) {
        return reponseHttp({ corps: { ok: true, version: "abc", documents_servis: ["lux-guide"] } });
      }
      return Promise.resolve({
        ok: true, status: 200,
        headers: { get: () => null },
        json: () => new Promise((_, rej) => {
          options.signal.addEventListener("abort", () => rej(new Error("abandon")));
        }),
      });
    }, { minuteurs });
    await CHAT.testerApi();
    const avant = poses.length;
    const promesse = CHAT.repondre(QUESTION, PROFIL, []);
    await tick();
    const enVol = poses.slice(avant).filter((p) => !p.annule);
    cas.question_corps_qui_pend_minuteur_arme = enVol.length;
    enVol.forEach((p) => p.fn());
    let erreur = null;
    try { await promesse; } catch (e) { erreur = e; }
    cas.question_corps_qui_pend = { kind: erreur && erreur.kind, code: erreur && erreur.code };
  }

  // --- page ouverte en file:// : rien à sonder, rien à poster ------------
  {
    const { CHAT, appels } = chargerChat("file:///Users/quelquun/web/index.html",
      () => reponseHttp({ corps: reponseSourcee() }));
    cas.hors_ligne_origine = CHAT.apiBase();
    cas.hors_ligne_sonde = await CHAT.testerApi();
    let erreur = null;
    try { await CHAT.repondre(QUESTION, PROFIL, []); } catch (e) { erreur = e; }
    cas.hors_ligne = {
      kind: erreur && erreur.kind, code: erreur && erreur.code,
      message: CHAT.messageErreur(erreur),
      appels_reseau: appels.length,
    };
  }

  // --- story 1.10 : le badge dit le niveau de validation du corpus servi -----
  //
  // Reprise différée de 1.7 (D10) : `testerApi()` lisait `gate_profile` et `alerts` sur `/sante` et
  // les jetait. Le badge disait « mode api » de la même façon que le corpus soit validé ou non.
  {
    const sante = (extra) => Object.assign({
      ok: true, version: "abc1234", documents_servis: ["lux-guide"],
      gate_profile: "vertical", gate_cases: 2, gate_countersigned: false,
      alerts: [], thresholds: {},
    }, extra || {});

    const situations = {
      // `gate_countersigned: false` est l'état du dépôt aujourd'hui (revue Codex 1.10 tour 2, B2) :
      // le badge porte la réserve tant que la contresignature humaine est due.
      gate: sante(),
      contresigne: sante({ gate_countersigned: true }),
      un_cas: sante({ gate_cases: 1, gate_countersigned: true }),
      sans_gate: sante({ gate_profile: null, gate_cases: null, gate_countersigned: null,
                         alerts: [{ doc_id: "lux-guide", alerte: "sans_gate", detail: "" }] }),
      // Corps qu'aucune route n'écrit : clé absente, ou profil et compte dissociés. Aucun suffixe.
      profil_absent: (() => { const c = sante(); delete c.gate_profile; return c; })(),
      profil_sans_compte: sante({ gate_cases: null }),
      contresignature_absente: (() => { const c = sante(); delete c.gate_countersigned; return c; })(),
      profil_sans_contresignature: sante({ gate_countersigned: null }),
      contresignature_non_booleenne: sante({ gate_countersigned: "true" }),
      gate_cases_fractionnaire: sante({ gate_cases: 1.5 }),
      gate_profile_vide: sante({ gate_profile: "" }),
      gate_perime: sante({ alerts: [{ doc_id: "lux-guide", alerte: "gate_perime", detail: "" }] }),
      // Les deux réserves à la fois : le badge les liste dans l'ordre, sans en taire une.
      perime_et_non_contresigne: sante({
        gate_countersigned: false,
        alerts: [{ doc_id: "lux-guide", alerte: "gate_perime", detail: "" }] }),
      perime_et_contresigne: sante({
        gate_countersigned: true,
        alerts: [{ doc_id: "lux-guide", alerte: "gate_perime", detail: "" }] }),
      perime_et_autres_alertes: sante({ alerts: [
        { doc_id: "axa-lu-optihome-2017", alerte: "source_absente", detail: "" },
        { doc_id: "lux-guide", alerte: "gate_perime", detail: "" }] }),
      alertes_sans_peremption: sante({ alerts: [
        { doc_id: "lux-guide", alerte: "source_absente", detail: "" }] }),
      // Story 2.5 (M14) : les deux réserves que le badge taisait encore. Ce sont des **faits
      // publiés** par le serveur sur ce à quoi les réponses s'adossent, dans le seul écran où l'on
      // pose une question ; l'accueil les montre depuis 1.10, le badge non.
      quarantaine: sante({ alerts: [
        { doc_id: "axa-lu-optihome-2017", alerte: "quarantaine", detail: "gate_echoue" }] }),
      source_absente: sante({ alerts: [
        { doc_id: "lux-guide", alerte: "source_absente", detail: "" }] }),
      quarantaine_et_source_absente: sante({ gate_countersigned: true, alerts: [
        { doc_id: "lux-guide", alerte: "source_absente", detail: "" },
        { doc_id: "axa-lu-optihome-2017", alerte: "quarantaine", detail: "gate_echoue" }] }),
      // L'ordre du serveur ne change pas le badge : deux corps qui portent les mêmes alertes dans
      // un ordre différent écrivent la même phrase.
      alertes_dans_lautre_ordre: sante({ gate_countersigned: true, alerts: [
        { doc_id: "axa-lu-optihome-2017", alerte: "quarantaine", detail: "" },
        { doc_id: "lux-guide", alerte: "source_absente", detail: "" }] }),
      // Une alerte que le badge ne connaît pas n'invente **aucune** réserve (M14).
      alerte_inconnue: sante({ gate_countersigned: true, alerts: [
        { doc_id: "lux-guide", alerte: "alerte_de_demain", detail: "" }] }),
      // Sans gate, les réserves restent dues : « non validé » seul tairait une quarantaine.
      sans_gate_mais_quarantaine: sante({
        gate_profile: null, gate_cases: null, gate_countersigned: null,
        alerts: [{ doc_id: "axa-lu-optihome-2017", alerte: "quarantaine", detail: "" }] }),
      gate_cases_zero: sante({ gate_cases: 0 }),
      gate_cases_negatif: sante({ gate_cases: -3 }),
    };
    // Les alertes ne conditionnent **pas** la lecture du niveau : mal formées, elles sont écartées,
    // et `gate_profile`/`gate_cases` restent lus (le badge ne les affiche pas).
    const tolerees = {
      alerts_absent: (() => { const c = sante(); delete c.alerts; return c; })(),
      alerte_non_objet: sante({ alerts: ["sans_gate"] }),
    };
    cas.alertes_tolerees = {};
    for (const [nom, corps] of Object.entries(tolerees)) {
      const { CHAT } = chargerChat(PAGE, () => reponseHttp({ corps }));
      await CHAT.testerApi();
      cas.alertes_tolerees[nom] = { lue: CHAT.validation(),
                                    badge: CHAT.libelleMode("api/v1", CHAT.validation()) };
    }
    // Le verdict de `lireValidation()` sur la table partagée avec l'accueil : un test Python
    // compare les deux relevés corps par corps.
    {
      const { CHAT } = chargerChat(PAGE, () => reponseHttp({ corps: sante() }));
      cas.corpus_partage = {};
      for (const [nom, entree] of Object.entries(CORPS_PARTAGES)) {
        cas.corpus_partage[nom] = { lisible: CHAT.lireValidation(entree.corps) !== null,
                                    attendu: entree.lisible };
      }
    }
    cas.validation = {};
    for (const [nom, corps] of Object.entries(situations)) {
      const { CHAT } = chargerChat(PAGE, () => reponseHttp({ corps }));
      await CHAT.testerApi();
      cas.validation[nom] = {
        lue: CHAT.validation(),
        badge: CHAT.libelleMode("api/v1", CHAT.validation()),
        bornes: CHAT.bornes().validation_du_serveur,
      };
    }
    // Sonde en panne : le badge ne suffixe rien plutôt que d'annoncer un niveau que personne n'a lu.
    const { CHAT } = chargerChat(PAGE, () => { throw new TypeError("Failed to fetch"); });
    await CHAT.testerApi();
    cas.validation.sonde_morte = {
      lue: CHAT.validation(),
      badge: CHAT.libelleMode("indisponible", CHAT.validation()),
      badge_api: CHAT.libelleMode("api/v1", CHAT.validation()),
    };
    // Les autres modes ne portent **jamais** le suffixe : il ne parle que du corpus servi.
    const { CHAT: c2 } = chargerChat(PAGE, () => reponseHttp({ corps: sante() }));
    await c2.testerApi();
    cas.validation.autres_modes = {
      local: c2.libelleMode("local", c2.validation()),
      indisponible: c2.libelleMode("indisponible", c2.validation()),
      avant_sonde: c2.libelleMode(null, c2.validation()),
    };
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
