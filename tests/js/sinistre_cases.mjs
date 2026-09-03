// Harnais Node du front de l'outil sinistre (story 1.9) — aucun navigateur, aucun réseau, aucune
// dépendance ajoutée à `pyproject.toml`.
//
// Décalque de `tests/js/chat_cases.mjs` et `tests/js/ui_cases.mjs` réunis : `tools/sinistre/sinistre.js`
// est un IIFE posé sur `window`, chargé dans un `node:vm` avec `window`, `location`, `document`
// (le DOM minimal de `dom_minimal.mjs`) et `fetch` **doublés**. On exécute les cas de la matrice
// d'E/S et on écrit sur la sortie standard le JSON de ce qui a été **observé**. Les assertions,
// elles, sont en Python (`tests/test_web_sinistre.py`) : ce fichier ne juge rien, il relève.
//
// Le front du guide (1.7) n'était vérifié par rien avant sa revue ; celui-ci l'est dès l'écriture,
// et sur les deux moitiés à la fois — la composition **et** la matérialisation.

import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

import { Document, stockage } from "./dom_minimal.mjs";

const ICI = path.dirname(fileURLToPath(import.meta.url));
const RACINE = path.resolve(ICI, "..", "..");

const ORIGINE = "https://foyer-retour.example";
const PAGE = ORIGINE + "/sinistre/";

// Les identifiants que `sinistre.js` cherche dans la page. Un test Python les vérifie contre
// `tools/sinistre/index.html`, pour qu'un renommage dans la page ne laisse pas ce harnais piloter
// un formulaire qui n'existe plus.
const ELEMENTS = [
  { tag: "form", id: "formulaire" },
  { tag: "select", id: "contrat" },
  { tag: "p", id: "contrats-message" },
  { tag: "p", id: "contrat-source" },
  { tag: "div", id: "documents-audit" },
  { tag: "input", id: "question" },
  { tag: "input", id: "date" },
  { tag: "input", id: "lieu" },
  { tag: "input", id: "montant" },
  { tag: "textarea", id: "description" },
  { tag: "button", id: "analyser" },
  { tag: "div", id: "resultat" },
  { tag: "dialog", id: "lecteur-pdf" },
  { tag: "h2", id: "lecteur-titre" },
  { tag: "p", id: "lecteur-statut" },
  { tag: "p", id: "lecteur-sans-surlignage" },
  { tag: "img", id: "lecteur-image" },
  { tag: "button", id: "lecteur-precedent" },
  { tag: "button", id: "lecteur-suivant" },
  { tag: "a", id: "lecteur-source" },
  { tag: "button", id: "lecteur-fermer" },
];

/** Une `Response` doublée : juste ce que `sinistre.js` en lit. */
const SEUILS = { deadline_s: 165.0, client_abort_margin_s: 150.0 };

/** La sonde `/api/v1/sante` : ce que `sinistre.js` en lit, et rien de plus. */
function reponseSante(thresholds) {
  return { ok: true, version: "test", documents_servis: ["cg-mini"],
           thresholds: thresholds === undefined ? SEUILS : thresholds };
}

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
    blob: () => Promise.resolve({ type: "image/png", size: 12 }),
  };
}

/** Charge `sinistre.js` dans un contexte neuf, avec un DOM minimal monté. */
function charger(href, repondre, { demarrage = false } = {}) {
  const appels = [];
  const fetchDouble = (url, options) => {
    appels.push({ url, options: options || {} });
    try {
      return Promise.resolve(repondre(url, options || {}, appels.length - 1));
    } catch (e) {
      return Promise.reject(e);
    }
  };

  const document = new Document();
  document.readyState = "complete";
  const elements = {};
  for (const spec of ELEMENTS) {
    const e = document.createElement(spec.tag);
    e.id = spec.id;
    document.body.appendChild(e);
    elements[spec.id] = e;
  }

  const localStorage = stockage();
  // Le JSON du harnais sort sur **stdout** : un `console.log` laissé dans `sinistre.js` le
  // corromprait. Le `console` du bac à sable écrit donc sur stderr.
  const journal = new console.Console(process.stderr, process.stderr);
  let prochainObjet = 0;
  function URLDouble(...args) { return new URL(...args); }
  URLDouble.createObjectURL = () => "blob:lecteur-" + (++prochainObjet);
  URLDouble.revokeObjectURL = () => {};
  const window = {
    location: new URL(href), document, localStorage, fetch: fetchDouble,
    addEventListener: () => {},
  };
  if (!demarrage) window.__SINISTRE_SANS_DEMARRAGE = true;
  const bac = {
    window, document, localStorage, fetch: fetchDouble, console: journal, URL: URLDouble,
    setTimeout, clearTimeout, AbortController,
    JSON, Math, Date, Number, String, Array, Object, isFinite, parseInt, Error, Promise, RegExp,
  };
  bac.globalThis = bac;
  vm.createContext(bac);
  vm.runInContext(readFileSync(path.join(RACINE, "tools/sinistre/sinistre.js"), "utf8"), bac,
                  { filename: "tools/sinistre/sinistre.js" });
  return { SINISTRE: window.SINISTRE, appels, document, elements, localStorage, window };
}

function tick() { return new Promise((r) => setTimeout(r, 0)); }

// ---------- relevés ----------

/** Arbre relevé d'un nœud du DOM : tag, classes, attributs posés, texte propre, enfants. */
function releverNoeud(n) {
  if (n.estTexte) return { tag: "#texte", texte: n.textContent };
  return {
    tag: n.tagName.toLowerCase(),
    cls: n.className || null,
    attributs: Object.fromEntries(n.attributs),
    texte: n.childNodes.length ? null : n.textContent,
    enfants: n.childNodes.map(releverNoeud),
  };
}

function aplatir(releve) {
  return [releve].concat((releve.enfants || []).flatMap(aplatir));
}

/** Arbre relevé d'une **vue** (avant matérialisation) : ce que la composition a décidé. */
function aplatirVue(vue) {
  return [vue].concat((vue.enfants || []).flatMap(aplatirVue));
}

function textesDe(vue, cls) {
  return aplatirVue(vue).filter((n) => n.cls === cls).map((n) => n.texte);
}

function texteEntier(vue) {
  return aplatirVue(vue).map((n) => (n.texte === undefined ? "" : n.texte)).join(" ");
}

/**
 * Résumé assertable du panneau « Comment cette réponse a été obtenue » (story 2.5).
 *
 * L'AC est une liste de rubriques : elle se vérifie rubrique par rubrique et ligne par ligne, pour
 * qu'une rubrique **absente** se distingue d'une rubrique vide et un contrôle échoué d'un contrôle
 * passé. Le relevé plat de la 1.9 ne pouvait dire ni l'un ni l'autre.
 */
function resumerTrace(vue) {
  const panneau = aplatirVue(vue).filter((n) => n.cls === "trace")[0];
  if (!panneau) return null;
  const lignesDe = (bloc) => aplatirVue(bloc).filter((n) => (n.cls || "").split(" ")[0] === "pq-ligne")
    .map((l) => {
      const plat = aplatirVue(l);
      const ok = plat.filter((n) => n.cls === "pq-ok")[0];
      const ko = plat.filter((n) => n.cls === "pq-ko")[0];
      return {
        etat: ok ? "ok" : (ko ? "ko" : null),
        texte: l.texte !== undefined ? l.texte
          : plat.filter((n) => n.cls === "pq-txt").map((n) => n.texte).join(" "),
        picto: (ok || ko || {}).attrs || null,
      };
    });
  const seuils = aplatirVue(panneau).filter((n) => n.cls === "pq-seuils")[0] || null;
  return {
    tag: panneau.tag,
    summary: (panneau.enfants || []).filter((n) => n.tag === "summary").map((n) => n.texte)[0],
    rubriques: aplatirVue(panneau).filter((n) => n.cls === "pq-bloc").map((b) => ({
      titre: aplatirVue(b).filter((n) => n.cls === "pq-titre").map((n) => n.texte)[0],
      lignes: lignesDe(b),
    })),
    seuils: seuils ? {
      tag: seuils.tag,
      summary: (seuils.enfants || []).filter((n) => n.tag === "summary").map((n) => n.texte)[0],
      lignes: lignesDe(seuils).map((l) => l.texte),
    } : null,
  };
}

// ---------- les données ----------

const DOC_ID = "cg-mini";
const QUESTION = "Ce sinistre est-il couvert par les conditions générales du contrat ?";
const Q_GARANTIE = "événement soudain, résultant de l'action subite de la chaleur";

const DOC_ID_2 = "cg-second";
const DOCUMENTS = [
  { doc_id: DOC_ID, title: "Mini conditions générales", edition: "juin 2017", kind: "contrat",
    status: "servi", selectionnable: true, source_url: "https://example.invalid/cg.pdf" },
  // AD-14 prévoit « ≥ 2 contrats » : le second est là pour que le `change` du sélecteur ait
  // quelque chose à changer, et pour que la sélection puisse être annulée si elle l'est.
  { doc_id: DOC_ID_2, title: "Autres conditions générales", edition: "", kind: "contrat",
    status: "servi", selectionnable: true, source_url: "https://example.invalid/second.pdf" },
  { doc_id: "lux-guide", title: "S'installer au Luxembourg", edition: "git:a8e8593", kind: "guide",
    status: "servi", selectionnable: false, source_url: "https://lux-guide.github.io/app/kb.js" },
  // Un contrat dont la source est le bucket **privé** d'AD-7 : `lienHttp()` doit rendre `null`.
  { doc_id: "cg-privee", title: "Conditions à source privée", edition: null, kind: "contrat",
    status: "servi", selectionnable: true,
    source_url: "gs://foyer-retour-sources/cg-privee.pdf" },
  { doc_id: "cg-quarantaine", title: "cg-quarantaine", edition: "2024", kind: null,
    status: "quarantaine", selectionnable: false,
    raison: "bloquant_statique : <script>page_sans_texte</script>", source_url: null,
    report_status: "disponible" },
];

function clause(block_id, quote, extra) {
  return Object.assign({
    block_id, page: 9, bbox: [70, 120.5, 520, 160], line_ids: ["p9:2:l1"],
    kind: "garantie", kind_confirmed: true, quote, status: "verifiee",
  }, extra || {});
}

function claim(claim_id, text, quotes, status) {
  // `status` est **copié** : `STATUT_HUMAIN` et `STATUT_OUI` sont des constantes du module, et un
  // cas qui altère le statut d'une claim pour éprouver le lecteur strict (revue Codex 1.9, tour 2)
  // les altérait pour tous les cas suivants — un relevé faux, sans que rien ne le dise.
  return { claim_id, text, quotes: quotes.map((b) => ({ block_id: b, quote: "peu importe" })),
           status: Object.assign({}, status) };
}

const STATUT_HUMAIN = { retrouvee: true, pertinente: true, applicable: "humain",
                        edition: "juin 2017" };
const STATUT_OUI = { retrouvee: true, pertinente: true, applicable: "oui", edition: "juin 2017" };

/** Une réponse 200 conforme au contrat d'AD-11 : deux affirmations, trois clauses, un verdict. */
function reponseVerdict(surcharge) {
  const answer = {
    found: true, complete: false, lang: "fr", lang_fallback: false,
    texte: "La garantie vise l'action subite de la chaleur. Une condition reste ouverte.",
    segments: [
      { text: "La garantie vise l'action subite de la chaleur.", kind: "factuel", claim_ids: ["c1"] },
      { text: "Une condition reste ouverte.", kind: "factuel", claim_ids: ["c2"] },
    ],
    claims: [
      claim("c1", "La garantie vise l'action subite de la chaleur.", ["cg-mini:p9:2"], STATUT_HUMAIN),
      claim("c2", "Une condition reste ouverte.", ["cg-mini:p12:3"], STATUT_OUI),
    ],
    rejected_claims: [
      { claim_id: "c3", text: "Une exclusion écarte les dommages du canapé.",
        quotes: [{ block_id: "cg-mini:p46:1", quote: "CETTE QUOTE NE DOIT JAMAIS S'AFFICHER" }],
        status: { retrouvee: false, edition: "juin 2017" },
        rejection_kind: "non_retrouvee", motif: "citation introuvable" },
    ],
    reason: null,
    verdict: {
      value: "sous_conditions",
      reason: "Une clause conditionne la garantie (au regard des conditions générales seules)",
      missing: { conditions_particulieres: true, options_souscrites: true, avenants: true,
                 date_effet: true, faits: ["caractère subit de l'action de la chaleur"] },
      ask_client: ["Le contrat porte-t-il l'option correspondante ?",
                   "Le caractère subit de l'action de la chaleur est-il établi ?"],
      escalate: ["Faire relire la clause par un gestionnaire."],
    },
    faits_compris: { themes: ["habitation", "incendie"], bien: "mobilier de salon",
                     evenement: "brûlure sans embrasement", lieu: "domicile", cause: "bougie",
                     moment: "2026-08-01" },
    unknown: ["La franchise applicable n'est pas dite."],
    clarification: null,
  };
  const r = {
    answer,
    sources: [clause("cg-mini:p9:2", Q_GARANTIE),
              clause("cg-mini:p12:3", "le bien est occupé de manière permanente",
                     { page: 12, kind: "condition", kind_confirmed: false, line_ids: [] })],
    via: "api/v1",
    trace: { request_id: "r-1", pipeline: "sinistre", variant: "deterministe",
             total_cost_eur: 0.0336,
             steps: [{ name: "comprendre", tier: "micro", ms: 900, checks: [] },
                     { name: "verifier", tier: "micro", ms: 1200,
                       checks: [{ name: "applicabilite_incomplete", ok: false, detail: "" }] },
                     { name: "restituer", tier: null, ms: 1, checks: [] }] },
  };
  return Object.assign(r, surcharge || {});
}

function reponseConversation() {
  const r = reponseVerdict();
  r.conversation = {
    token: "etat.signe", turn: 0,
    facts: [
      { event_id: "f-1", key: "cause", value: "bougie", source: "extraction", turn: 0,
        question_id: null, replaces_event_id: null },
      { event_id: "f-2", key: "montant_eur", value: "1200",
        source: "declaration_initiale", turn: 0, question_id: null, replaces_event_id: null },
    ],
    conflicts: [],
    questions: [
      { question_id: "q-1", text: "Le caractère subit est-il établi ?", kind: "fait",
        fact_key: "caractère subit", claim_id: "c1", expected_value: "subite",
        status: "active", answered_event_id: null },
      { question_id: "q-2", text: "Le dommage est-il accidentel ?", kind: "fait",
        fact_key: "caractère accidentel", claim_id: "c1", expected_value: "accidentel",
        status: "active", answered_event_id: null },
      { question_id: "q-3", text: "Une option a-t-elle été souscrite ?", kind: "option",
        fact_key: "option_requise", claim_id: "c1", expected_value: null,
        status: "active", answered_event_id: null },
    ],
    history: [{ turn: 0, value: "sous_conditions", reason: r.answer.verdict.reason,
                changed: false, causal_event_ids: [], causal_events: [], decisive_terms: [],
                request_id: "r-1" }],
  };
  return r;
}

/** Un refus : 200, `ne_tranche_pas`, aucune clause. */
function reponseRefus() {
  const phrase = "Je n'ai trouvé aucune clause du contrat qui traite du sinistre décrit.";
  return {
    answer: {
      found: false, complete: false, lang: "fr", texte: phrase,
      segments: [{ text: phrase, kind: "limite", claim_ids: [] }],
      claims: [], rejected_claims: [],
      reason: { kind: "zero_hit", terms_searched: ["mobilier"], variants_count: 0,
                blocks_scanned: 3, documents: [DOC_ID] },
      verdict: { value: "ne_tranche_pas",
                 reason: "Aucune clause du contrat n'a été retrouvée sur les termes du sinistre "
                         + "décrit (au regard des conditions générales seules)",
                 missing: { conditions_particulieres: true, options_souscrites: true,
                            avenants: true, date_effet: true, faits: [] },
                 ask_client: ["Le contrat porte-t-il des conditions particulières ?"],
                 escalate: ["Reprendre le dossier à la main."] },
      faits_compris: { themes: [], bien: "mobilier de salon", evenement: null, lieu: null,
                       cause: null, moment: null },
      unknown: [], clarification: null,
    },
    sources: [], via: "api/v1",
    trace: { request_id: "r-2", pipeline: "sinistre", variant: "deterministe",
             total_cost_eur: 0.0071, steps: [] },
  };
}

/**
 * Story 4.2f : une **lecture partielle** — 200, `found=false`, aucune preuve d'absence, mais des
 * compteurs, une clause écartée, la lacune qui dit la borne et le `ne_tranche_pas` calculé par
 * AD-6. C'est le corps que le pipeline rend là où il levait `TruncatedRead`, et que la page
 * recevait en 503 « L'analyse est indisponible pour le moment ».
 */
function reponseLecturePartielle() {
  const phrase = "Je n'ai pas pu lire tout ce qui pouvait concerner ce sinistre, et aucune des "
    + "clauses citées n'a passé la vérification.";
  const manque = "Je n'ai pas pu lire tout ce qui pouvait concerner votre question : ma lecture a "
    + "été bornée, et des passages sont restés fermés.";
  return {
    answer: {
      found: false, complete: false, lang: "fr", texte: phrase,
      segments: [{ text: phrase, kind: "limite", claim_ids: [] }],
      claims: [],
      rejected_claims: [{
        claim_id: "c9", text: "Une clause que la vérification a écartée.",
        quotes: [{ block_id: "cg-mini:p46:1", quote: "CETTE QUOTE NE DOIT JAMAIS S'AFFICHER" }],
        status: { retrouvee: false, edition: "juin 2017" },
        rejection_kind: "non_retrouvee", motif: "citation introuvable",
      }],
      reason: null,
      lecture_partielle: { nodes_read: 1, blocks_read: 4, documents: [DOC_ID] },
      verdict: { value: "ne_tranche_pas",
                 reason: "Aucune clause vérifiée (au regard des conditions générales seules)",
                 missing: { conditions_particulieres: true, options_souscrites: true,
                            avenants: true, date_effet: true, faits: [] },
                 ask_client: [], escalate: [] },
      faits_compris: { themes: [], bien: "mobilier de salon", evenement: null, lieu: null,
                       cause: null, moment: null },
      unknown: [manque], clarification: null,
    },
    sources: [], via: "api/v1",
    trace: { request_id: "r-4-2f", pipeline: "sinistre", variant: "deterministe",
             total_cost_eur: 0.0181, steps: [] },
  };
}

/** AD-5 : `ClarificationRequise` — le seul chemin où le système **pose une question** en retour. */
function reponseClarification() {
  const phrase = "Je n'ai pas pu déterminer sur quoi porte la demande ; précisez-la et je "
    + "chercherai dans le contrat.";
  return {
    answer: {
      found: false, complete: false, lang: "fr", texte: phrase,
      segments: [{ text: phrase, kind: "limite", claim_ids: [] }],
      claims: [], rejected_claims: [],
      reason: { kind: "clarification_requise", terms_searched: [], variants_count: 0,
                blocks_scanned: 0, documents: [] },
      verdict: { value: "ne_tranche_pas",
                 reason: "La demande n'a pas pu être rendue autonome : rien n'a été cherché "
                         + "(au regard des conditions générales seules)",
                 missing: { conditions_particulieres: true, options_souscrites: true,
                            avenants: true, date_effet: true, faits: [] },
                 ask_client: [], escalate: [] },
      // AD-5 : cette sortie n'a pas de portée — il n'y a rien à publier, pas même partiellement.
      faits_compris: null,
      unknown: [],
      clarification: "De quel bien parlez-vous : le mobilier, ou le bâtiment ?",
    },
    sources: [], via: "api/v1",
    trace: { request_id: "r-3", pipeline: "sinistre", variant: "deterministe",
             total_cost_eur: 0.0028, steps: [] },
  };
}

const SAISIE = {
  doc_id: DOC_ID, question: QUESTION, date: "2026-08-01", lieu: "salon du domicile assuré",
  montant_eur: "1200", description: "Une bougie allumée est tombée sur le canapé.",
};

// ---------- les cas ----------

async function main() {
  const cas = {};

  // --- le corps posté (AD-11 : quatre champs, pas un de plus) --------------
  {
    const { SINISTRE, appels } = charger(PAGE, (url) => {
      if (String(url).endsWith("/sante")) return reponseHttp({ corps: reponseSante() });
      if (String(url).endsWith("/documents")) return reponseHttp({ corps: DOCUMENTS });
      return reponseHttp({ corps: reponseVerdict() });
    });
    cas.api_base = SINISTRE.apiBase();
    cas.bornes_avant_sonde = SINISTRE.bornes();
    cas.portee = SINISTRE.PORTEE;
    // La borne d'abandon vient des seuils du serveur (convention Seuils) ; sans sonde, du repli.
    await SINISTRE.sonder();
    cas.bornes = SINISTRE.bornes();
    cas.sonde_url = appels.filter((a) => String(a.url).endsWith("/sante"))[0].url;

    await SINISTRE.soumettre(SAISIE);
    const requete = appels.filter((a) => String(a.url).endsWith("/api/v1/sinistre"))[0];
    cas.corps_url = requete.url;
    cas.corps_methode = requete.options.method;
    cas.corps_entetes = requete.options.headers;
    cas.corps = JSON.parse(requete.options.body);

    const conversation = reponseConversation();
    cas.conversation_vue = SINISTRE.conversationVue(conversation.conversation);
    const avecBascule = JSON.parse(JSON.stringify(conversation));
    avecBascule.conversation.history.push({
      turn: 1, value: "couvert", reason: "qualité confirmée", changed: true,
      causal_event_ids: ["f-r"], causal_events: ["caractère subit = oui (tour client)"],
      decisive_terms: ["subite"], request_id: "r-2",
    });
    cas.fragments_decisifs = aplatirVue(SINISTRE.vueVerdict(avecBascule, { doc_id: DOC_ID }))
      .filter((n) => n.cls === "mot-decisif").map((n) => n.texte);
    cas.dossier_copie = SINISTRE.dossierTexte(conversation);
    const dossierConflit = JSON.parse(JSON.stringify(conversation));
    dossierConflit.conversation.facts.push(
      { event_id: "f-3", key: "cause", value: "court-circuit", source: "reponse_client",
        turn: 1, question_id: "q-1", replaces_event_id: null });
    dossierConflit.conversation.conflicts = [
      { conflict_id: "c-1", key: "cause", event_ids: ["f-1", "f-3"], status: "ouvert",
        chosen_event_id: null }];
    const ouvert = SINISTRE.dossierTexte(dossierConflit);
    dossierConflit.conversation.conflicts[0].status = "resolu";
    dossierConflit.conversation.conflicts[0].chosen_event_id = "f-3";
    const resolu = SINISTRE.dossierTexte(dossierConflit);
    cas.dossier_conflit = {
      ouvert_bougie: ouvert.includes("bougie"),
      ouvert_court_circuit: ouvert.includes("court-circuit"),
      resolu_bougie: resolu.includes("bougie"),
      resolu_court_circuit: resolu.includes("court-circuit"),
    };
    const suiviCharge = charger(PAGE, () => reponseHttp({ corps: conversation }));
    await suiviCharge.SINISTRE.suivre(conversation.conversation, DOC_ID,
      { action: "reponse", question_id: "q-1", value: "oui" });
    const appelSuivi = suiviCharge.appels.filter((a) =>
      String(a.url).endsWith("/api/v1/sinistre/suivi"))[0];
    cas.suivi = {
      url: appelSuivi.url,
      corps: JSON.parse(appelSuivi.options.body),
    };

    // Les vrais handlers rendus : sélection d'une question, choix rapide, réponse libre et copie.
    const handlers = charger(PAGE, () => reponseHttp({ corps: conversation }));
    const racine = handlers.SINISTRE.materialiser(
      handlers.SINISTRE.conversationVue(conversation.conversation));
    handlers.document.body.appendChild(racine);
    let misesAJour = 0;
    handlers.SINISTRE.brancherConversation(racine, conversation, { doc_id: DOC_ID },
      () => { misesAJour++; });
    const selections = racine.querySelectorAll(".conv-selection-question");
    selections[1].declencher("click");
    const contexteSelection = racine.querySelector(".conv-question-contexte");
    racine.querySelector(".conv-repondre").declencher("click");
    const verrouilles = racine.querySelectorAll(".conv-repondre").map((b) => !!b.disabled);
    // Un second clic pendant la même promesse ne produit pas un second suivi.
    racine.querySelectorAll(".conv-repondre")[1].declencher("click");
    await tick(); await tick();
    const apresDouble = handlers.appels.filter((a) => String(a.url).endsWith("/sinistre/suivi")).length;
    const libre = racine.querySelector(".conv-reponse-libre");
    selections[2].declencher("click");
    libre.value = "preuve jointe";
    racine.querySelector(".conv-envoyer-libre").declencher("click");
    await tick(); await tick();
    const appelsHandlers = handlers.appels.filter((a) => String(a.url).endsWith("/sinistre/suivi"));
    handlers.window.navigator = { clipboard: { writeText: () => Promise.resolve() } };
    racine.querySelector(".conv-copier").declencher("click");
    await tick();
    const copieSucces = racine.querySelector(".conv-statut").textContent;
    handlers.window.navigator = {};
    racine.querySelector(".conv-copier").declencher("click");
    await tick();
    const copieEchec = racine.querySelector(".conv-statut").textContent;
    handlers.window.prompt = () => "nouvelle cause";
    racine.querySelector(".conv-corriger").declencher("click");
    await tick(); await tick();

    const avecConflit = JSON.parse(JSON.stringify(conversation));
    avecConflit.conversation.facts.push(
      { event_id: "f-conflit", key: "cause", value: "court-circuit", source: "reponse_client",
        turn: 1, question_id: "q-1", replaces_event_id: null });
    avecConflit.conversation.conflicts.push(
      { conflict_id: "conflit-1", key: "cause", event_ids: ["f-1", "f-conflit"],
        status: "ouvert", chosen_event_id: null });
    const racineConflit = handlers.SINISTRE.materialiser(
      handlers.SINISTRE.conversationVue(avecConflit.conversation));
    handlers.SINISTRE.brancherConversation(racineConflit, avecConflit, { doc_id: DOC_ID }, () => {});
    racineConflit.querySelector(".conv-resoudre").declencher("click");
    await tick(); await tick();
    cas.handlers_conversation = {
      questions: selections.length,
      selection: contexteSelection.getAttribute("data-selected-question-id"),
      choix_corps: JSON.parse(appelsHandlers[0].options.body),
      libre_corps: JSON.parse(appelsHandlers[1].options.body),
      appels_apres_double_clic: apresDouble,
      verrouilles,
      mises_a_jour: misesAJour,
      correction_corps: JSON.parse(handlers.appels.filter((a) =>
        String(a.url).endsWith("/sinistre/suivi"))[2].options.body),
      resolution_corps: JSON.parse(handlers.appels.filter((a) =>
        String(a.url).endsWith("/sinistre/suivi"))[3].options.body),
      copie_succes: copieSucces,
      copie_echec: copieEchec,
    };

    // Une réponse de l'ancienne vue est ignorée après qu'une nouvelle vue a été branchée.
    let resoudreAncienne;
    const anciennePromise = new Promise((resolve) => { resoudreAncienne = resolve; });
    const stale = charger(PAGE, () => anciennePromise);
    const ancienne = stale.SINISTRE.materialiser(stale.SINISTRE.conversationVue(conversation.conversation));
    const nouvelle = stale.SINISTRE.materialiser(stale.SINISTRE.conversationVue(conversation.conversation));
    let staleUpdates = 0;
    stale.SINISTRE.brancherConversation(ancienne, conversation, { doc_id: DOC_ID },
      () => { staleUpdates++; });
    ancienne.querySelector(".conv-repondre").declencher("click");
    stale.SINISTRE.brancherConversation(nouvelle, conversation, { doc_id: DOC_ID },
      () => { staleUpdates++; });
    resoudreAncienne(reponseHttp({ corps: conversation }));
    await tick(); await tick();
    cas.suivi_obsolete = { mises_a_jour: staleUpdates };

    const refuse = charger(PAGE, () => reponseHttp({ status: 400,
      corps: { error: { code: "invalid_request", message: "périmé", request_id: "r-refus" } } }));
    const racineRefus = refuse.SINISTRE.materialiser(
      refuse.SINISTRE.conversationVue(conversation.conversation));
    let refusUpdates = 0;
    refuse.SINISTRE.brancherConversation(racineRefus, conversation, { doc_id: DOC_ID },
      () => { refusUpdates++; });
    racineRefus.querySelector(".conv-repondre").declencher("click");
    await tick(); await tick();
    cas.suivi_refuse = {
      mises_a_jour: refusUpdates,
      faits_restants: racineRefus.querySelectorAll(".conv-fait").length,
      statut: racineRefus.querySelector(".conv-statut").textContent,
      deverrouille: !racineRefus.querySelector(".conv-repondre").disabled,
    };

    // Les champs facultatifs vides ne partent **pas** : `Faits.date`/`lieu` sont `str | None`, et
    // une chaîne vide n'est pas l'absence.
    cas.corps_minimal = SINISTRE.corpsSinistre({
      doc_id: DOC_ID, question: QUESTION, date: "", lieu: "  ", montant_eur: "",
      description: "Deux mots.",
    });
    // Un montant illisible n'est pas envoyé à zéro : le champ reste absent.
    cas.corps_montant_illisible = SINISTRE.corpsSinistre({
      doc_id: DOC_ID, question: QUESTION, montant_eur: "beaucoup", description: "Deux mots.",
    });
    cas.corps_montant_virgule = SINISTRE.corpsSinistre({
      doc_id: DOC_ID, question: QUESTION, montant_eur: "1200,50", description: "Deux mots.",
    });
    // Revue Codex 1.9 (I1) : un montant illisible ne se supprime plus en silence — il se dit.
    // `montantSaisi()` a trois issues : le nombre, `null` (champ vide, facultatif), `false`
    // (saisie illisible, refusée par `manquant()` avant tout appel).
    cas.montant_saisi = {};
    [["vide", ""], ["blancs", "   "], ["zero", "0"], ["entier", "1200"],
     ["virgule", "1200,50"], ["point", "1200.50"], ["negatif", "-100"],
     ["mots", "douze"], ["infini", "Infinity"], ["nan", "NaN"]].forEach(function (p) {
      const v = SINISTRE.montantSaisi(p[1]);
      cas.montant_saisi[p[0]] = (v === false) ? "illisible" : v;
    });

    const liste = await SINISTRE.documents();
    cas.documents_url = appels.filter((a) => String(a.url).endsWith("/api/v1/documents"))[0].url;
    cas.documents_recus = liste.length;
  }

  // --- le sélecteur : les contrats seulement ------------------------------
  {
    const { SINISTRE } = charger(PAGE, () => reponseHttp({ corps: DOCUMENTS }));
    cas.formulaire = SINISTRE.vueFormulaire(DOCUMENTS);
    cas.selectionnable_prod = DOCUMENTS.filter((d) => d.kind === "contrat" && d.status === "servi")
      .map((d) => ({ doc_id: d.doc_id, selectionnable: d.selectionnable }));
    cas.selectionnable_compat = SINISTRE.vueFormulaire([
      { doc_id: "ancien-servi", title: "Ancien servi", kind: "contrat", status: "servi" },
      { doc_id: "ancienne-quarantaine", title: "Ancienne quarantaine", kind: "contrat",
        status: "quarantaine" },
    ]).options.map((o) => o.valeur);
    cas.sources_non_publiques = SINISTRE.vueFormulaire([
      { doc_id: "public", title: "Public", kind: "contrat", status: "servi",
        selectionnable: true, source_url: "https://example.invalid/cg.pdf" },
      { doc_id: "localhost", title: "Local", kind: "contrat", status: "servi",
        selectionnable: true, source_url: "http://localhost/admin" },
      { doc_id: "prive", title: "Privé", kind: "contrat", status: "servi",
        selectionnable: true, source_url: "http://192.168.1.8/cg.pdf" },
      { doc_id: "malforme", title: "Malformé", kind: "contrat", status: "servi",
        selectionnable: true, source_url: "https://exa mple.invalid/cg.pdf" },
    ]).sources;
    cas.formulaire_sans_contrat = SINISTRE.vueFormulaire(
      DOCUMENTS.filter((d) => d.kind !== "contrat"));
    cas.formulaire_vide = SINISTRE.vueFormulaire([]);
  }

  // --- l'appariement clause ↔ affirmation (D6) ----------------------------
  {
    const { SINISTRE } = charger(PAGE, () => reponseHttp({ corps: {} }));
    const r = reponseVerdict();
    const apparie = SINISTRE.clausesParClaim(r.answer, r.sources);
    cas.appariement = apparie.map((e) => ({
      claim_id: e.claim_id, blocs: e.clauses.map((c) => c.block_id),
      applicable: e.status && e.status.applicable,
    }));
    // Longueurs incompatibles : l'appariement est **abandonné**, jamais deviné.
    cas.appariement_trop_de_sources =
      SINISTRE.clausesParClaim(r.answer, r.sources.concat([clause("cg-mini:p99:1", "en trop")]));
    // Un `block_id` qui ne concorde pas : idem.
    const desordre = [r.sources[1], r.sources[0]];
    cas.appariement_desordre = SINISTRE.clausesParClaim(r.answer, desordre);
    cas.appariement_sans_source = SINISTRE.clausesParClaim(r.answer, []);
  }

  // --- les textes composés ------------------------------------------------
  {
    const { SINISTRE } = charger(PAGE, () => reponseHttp({ corps: {} }));
    cas.verdicts = ["couvert", "non_couvert", "sous_conditions", "ne_tranche_pas", "inventé", ""]
      .map((v) => SINISTRE.libelleVerdict(v));
    cas.statuts = {
      humain: SINISTRE.statutTexte(STATUT_HUMAIN),
      oui: SINISTRE.statutTexte(STATUT_OUI),
      non: SINISTRE.statutTexte({ retrouvee: true, pertinente: true, applicable: "non",
                                  edition: "juin 2017" }),
      non_hors_portee: SINISTRE.statutTexte({
        retrouvee: true, pertinente: true, applicable: "non",
        applicable_reason: "hors_portee", edition: "juin 2017",
      }),
      sans_applicable: SINISTRE.statutTexte({ retrouvee: true, pertinente: true, applicable: null,
                                              edition: "juin 2017" }),
      sans_edition: SINISTRE.statutTexte({ retrouvee: true, pertinente: true, edition: "" }),
      absent: SINISTRE.statutTexte(null),
    };
    cas.kinds = ["garantie", "exclusion", "condition", "franchise", "definition", "zzz"]
      .map((k) => SINISTRE.libelleKind(k));
    cas.rejets = ["non_retrouvee", "non_pertinente", "ambigue", "non_citee", "inconnu"]
      .map((k) => SINISTRE.motifRejet(k));
    cas.couts = {
      nominal: SINISTRE.coutTexte({ total_cost_eur: 0.0336 }),
      zero: SINISTRE.coutTexte({ total_cost_eur: 0 }),
      absent: SINISTRE.coutTexte({}),
      sans_trace: SINISTRE.coutTexte(null),
    };
  }

  // --- la vue du verdict --------------------------------------------------
  {
    const { SINISTRE } = charger(PAGE, () => reponseHttp({ corps: {} }));
    const vue = SINISTRE.vueVerdict(reponseVerdict());
    const plat = aplatirVue(vue);
    cas.verdict = {
      badge: plat.filter((n) => n.cls && n.cls.indexOf("badge") === 0)
        .map((n) => ({ cls: n.cls, texte: n.texte })),
      portee: textesDe(vue, "portee"),
      raison: textesDe(vue, "verdict-raison"),
      analyse: textesDe(vue, "analyse-txt"),
      faits_compris: plat.filter((n) => n.cls === "fc-ligne")
        .map((n) => n.enfants.map((e) => e.texte)),
      paquet: plat.filter((n) => n.cls === "paquet").flatMap((n) => aplatirVue(n).map((x) => x.texte))
        .filter((t) => t),
      ask: plat.filter((n) => n.cls === "ask-liste")
        .flatMap((n) => (n.enfants || []).map((e) => e.texte)),
      escalate: plat.filter((n) => n.cls === "escalate-liste")
        .flatMap((n) => (n.enfants || []).map((e) => e.texte)),
      clauses: plat.filter((n) => n.cls === "clause").map((n) => ({
        quote: aplatirVue(n).filter((x) => x.cls === "cl-q").map((x) => x.texte)[0],
        meta: aplatirVue(n)
          .filter((x) => x.cls && x.cls.indexOf("cl-") === 0 && x.cls !== "cl-q" && x.cls !== "cl-meta")
          .map((x) => x.texte),
      })),
      affirmations: textesDe(vue, "aff-txt"),
      rejetees: plat.filter((n) => n.cls === "rejetee")
        .map((n) => aplatirVue(n).map((x) => x.texte).filter((t) => t)),
      rejetees_titre: plat.filter((n) => n.cls === "rejetees")
        .flatMap((n) => (n.enfants || []).filter((e) => e.tag === "h3").map((e) => e.texte)),
      rejetees_note: textesDe(vue, "rejetees-note"),
      inconnu: plat.filter((n) => n.cls === "inconnu-liste")
        .flatMap((n) => (n.enfants || []).map((e) => e.texte)),
      degrade: textesDe(vue, "degrade"),
      trace_tags: plat.filter((n) => n.cls === "trace").map((n) => n.tag),
      trace: resumerTrace(vue),
      preuve: textesDe(vue, "preuve"),
      etat: plat.filter((n) => (n.cls || "").split(" ")[0] === "etat").map((n) => n.cls),
      etat_texte: plat.filter((n) => (n.cls || "").split(" ")[0] === "etat").map((n) => n.texte),
      etat_phrase: textesDe(vue, "etat-phrase"),
      // La quote d'une claim rejetée ne doit **jamais** apparaître, nulle part dans l'arbre.
      texte_entier: texteEntier(vue),
    };

    // Appariement impossible : la page le dit, et affiche une liste plate.
    const casse = reponseVerdict();
    casse.sources = casse.sources.concat([clause("cg-mini:p99:1", "clause en trop")]);
    const vueCassee = SINISTRE.vueVerdict(casse);
    cas.verdict_degrade = {
      degrade: textesDe(vueCassee, "degrade"),
      clauses: aplatirVue(vueCassee).filter((n) => n.cls === "clause").length,
      affirmations: textesDe(vueCassee, "aff-txt"),
      // D6 : « une liste plate de citations **avec leurs statuts** ». Le mode dégradé serait le
      // dernier endroit où taire l'applicabilité d'une clause et la réserve de son édition.
      statuts: textesDe(vueCassee, "cl-statut"),
    };

    // Un bloc cité par **deux** affirmations aux statuts différents : en mode dégradé, la page ne
    // devine pas lequel s'applique — et elle le dit (D6, revue 1.9, tour 2).
    const ambigu = reponseVerdict();
    ambigu.answer.claims[1].quotes = [{ block_id: "cg-mini:p9:2", quote: "peu importe" }];
    ambigu.sources = [clause("cg-mini:p9:2", Q_GARANTIE), clause("cg-mini:p99:1", "en trop")];
    const vueAmbigue = SINISTRE.vueVerdict(ambigu);
    cas.verdict_statut_ambigu = {
      appariement: SINISTRE.clausesParClaim(ambigu.answer, ambigu.sources),
      statuts: textesDe(vueAmbigue, "cl-statut"),
      degrade: textesDe(vueAmbigue, "degrade"),
      clauses: aplatirVue(vueAmbigue).filter((n) => n.cls === "clause").length,
      // La fonction pure, appelée directement sur les deux formes.
      statut_pour_bloc_partage: SINISTRE.statutDeBloc(ambigu.answer, "cg-mini:p9:2"),
      statut_pour_bloc_unique: SINISTRE.statutDeBloc(reponseVerdict().answer, "cg-mini:p9:2"),
      ambigu_partage: SINISTRE.statutAmbigu(ambigu.answer, "cg-mini:p9:2"),
      ambigu_unique: SINISTRE.statutAmbigu(reponseVerdict().answer, "cg-mini:p9:2"),
      // Un bloc que personne ne cite n'est pas « ambigu » : il n'a simplement pas de statut.
      ambigu_non_cite: SINISTRE.statutAmbigu(ambigu.answer, "cg-mini:p99:1"),
    };

    // Le refus : un verdict `ne_tranche_pas`, aucune clause, et les faits compris quand même.
    const vueRefus = SINISTRE.vueVerdict(reponseRefus());
    cas.verdict_refus = {
      badge: aplatirVue(vueRefus).filter((n) => n.cls && n.cls.indexOf("badge") === 0)
        .map((n) => ({ cls: n.cls, texte: n.texte })),
      clauses: aplatirVue(vueRefus).filter((n) => n.cls === "clause").length,
      faits_compris: aplatirVue(vueRefus).filter((n) => n.cls === "fc-ligne")
        .map((n) => n.enfants.map((e) => e.texte)),
      analyse: textesDe(vueRefus, "analyse-txt"),
      portee: textesDe(vueRefus, "portee"),
    };

    // La clarification : le seul chemin où le système pose une question à l'utilisateur. Sans elle
    // à l'écran, il reste devant un « ne tranche pas » sans issue (revue 1.9, tour 2).
    const vueClarif = SINISTRE.vueVerdict(reponseClarification());
    cas.verdict_clarification = {
      question: textesDe(vueClarif, "clarif-q"),
      titre: aplatirVue(vueClarif).filter((n) => n.cls === "clarif")
        .flatMap((n) => (n.enfants || []).filter((e) => e.tag === "h3").map((e) => e.texte)),
      badge: aplatirVue(vueClarif).filter((n) => n.cls && n.cls.indexOf("badge") === 0)
        .map((n) => n.texte),
      faits_compris: aplatirVue(vueClarif).filter((n) => n.cls === "fc-ligne").length,
      clauses: aplatirVue(vueClarif).filter((n) => n.cls === "clause").length,
    };

    // Story 4.2f : la lecture partielle — badge, phrase, compteurs, clauses écartées, et surtout
    // aucun message d'indisponibilité ni bouton de repli.
    const vueLecture = SINISTRE.vueVerdict(reponseLecturePartielle());
    const platLecture = aplatirVue(vueLecture);
    cas.verdict_lecture_partielle = {
      badge: platLecture.filter((n) => n.cls && n.cls.indexOf("badge") === 0)
        .map((n) => ({ cls: n.cls, texte: n.texte })),
      analyse: textesDe(vueLecture, "analyse-txt"),
      preuve: textesDe(vueLecture, "preuve"),
      lecture: textesDe(vueLecture, "lecture-partielle"),
      inconnu: platLecture.filter((n) => n.cls === "inconnu-liste")
        .flatMap((n) => (n.enfants || []).map((e) => e.texte)),
      clauses: platLecture.filter((n) => n.cls === "clause").length,
      rejetees: platLecture.filter((n) => n.cls === "rejetee")
        .map((n) => aplatirVue(n).map((x) => x.texte).filter((t) => t)),
      etat: platLecture.filter((n) => (n.cls || "").split(" ")[0] === "etat").map((n) => n.cls),
      etat_texte: platLecture.filter((n) => (n.cls || "").split(" ")[0] === "etat")
        .map((n) => n.texte),
      etat_phrase: textesDe(vueLecture, "etat-phrase"),
      faits_compris: platLecture.filter((n) => n.cls === "fc-ligne")
        .map((n) => n.enfants.map((e) => e.texte)),
      boutons: platLecture.filter((n) => n.tag === "button").length,
      actions: platLecture.filter((n) => n.action).length,
      texte_entier: texteEntier(vueLecture),
    };
    cas.lecture_textes = {
      pluriel: SINISTRE.lectureLue({ nodes_read: 3, blocks_read: 12, documents: [] }),
      singulier: SINISTRE.lectureLue({ nodes_read: 1, blocks_read: 1, documents: [] }),
      plancher: SINISTRE.lectureLue({ nodes_read: 1, blocks_read: 1, documents: [] }),
      absente: SINISTRE.lectureLue(null),
    };
    cas.etats_lecture_partielle = {
      porteur: SINISTRE.etatReponse({ found: false, complete: false,
                                      lecture_partielle: { nodes_read: 1, blocks_read: 1 } }),
      sans_porteur: SINISTRE.etatReponse({ found: false, complete: false }),
    };
    cas.phrases_lecture_partielle = {
      avec_liste: SINISTRE.phraseEtat({ cle: "lecture-partielle" }, { liste: true, lecture: true }),
      sans_liste: SINISTRE.phraseEtat({ cle: "lecture-partielle" }, { liste: false, lecture: true }),
      sans_chiffre: SINISTRE.phraseEtat({ cle: "lecture-partielle" },
                                        { liste: true, lecture: false }),
      sans_rien: SINISTRE.phraseEtat({ cle: "lecture-partielle" },
                                     { liste: false, lecture: false }),
    };

    // Un verdict dont la valeur n'est pas au contrat : dit, jamais traduit en valeur connue.
    const inconnu = reponseVerdict();
    inconnu.answer.verdict.value = "peut-etre";
    cas.verdict_inconnu = aplatirVue(SINISTRE.vueVerdict(inconnu))
      .filter((n) => n.cls && n.cls.indexOf("badge") === 0)
      .map((n) => ({ cls: n.cls, texte: n.texte }));
  }

  // --- story 2.5 : la trace enrichie, la preuve d'absence, les trois états --
  {
    const { SINISTRE } = charger(PAGE, () => reponseHttp({ corps: {} }));

    // Une trace complète, telle que le Lot A la publie pour le pipeline sinistre : `blocs` et
    // `gate` renseignés, `dictionnaire` absent (l'outil sinistre n'en a pas). Écrite à la main
    // contre le contrat de la spec — les deux lots sont implémentés en parallèle.
    const traceRiche = {
      request_id: "r-riche", pipeline: "sinistre", variant: "deterministe",
      total_cost_eur: 0.0336, retries: 1, truncations: 0,
      thresholds: { max_opens: 8, quote_min_chars: 24 },
      steps: [
        { name: "comprendre", tier: "micro", ms: 900, opened_block_ids: [],
          discarded_block_ids: [], checks: [] },
        { name: "retrouver", tier: "reason", ms: 4200,
          opened_block_ids: ["cg-mini:p9:2", "cg-mini:p12:3"],
          discarded_block_ids: ["cg-mini:p46:1"], checks: [] },
        { name: "verifier", tier: "micro", ms: 1200, opened_block_ids: [], discarded_block_ids: [],
          checks: [{ name: "applicabilite_incomplete", ok: false,
                     detail: "1 affirmation(s) sans champs typés" },
                   { name: "verdict", ok: true, detail: "sous_conditions sur 2 affirmation(s)" }] },
        { name: "restituer", tier: null, ms: 1, opened_block_ids: [], discarded_block_ids: [],
          checks: [] },
      ],
      blocs: [
        { block_id: "cg-mini:p9:2", doc_id: "cg-mini", node_id: "cg-mini:garanties",
          fiche_id: null, titre: "Les garanties incendie" },
        { block_id: "cg-mini:p46:1", doc_id: "cg-mini", node_id: "cg-mini:exclusions",
          fiche_id: null, titre: "Les exclusions" },
      ],
      gate: { profile: "vertical", cases: 1, countersigned: false, alerts: ["source_absente"] },
    };
    const riche = reponseVerdict({ trace: traceRiche });
    cas.trace_riche = resumerTrace(SINISTRE.vueVerdict(riche));

    // Trace pauvre : les champs de la story sont **absents**. Les rubriques disparaissent, rien
    // n'est inventé, aucune ligne vide.
    const pauvre = reponseVerdict({
      trace: { request_id: "r-pauvre", pipeline: "sinistre", variant: "deterministe",
               total_cost_eur: 0.004, steps: [] } });
    cas.trace_pauvre = resumerTrace(SINISTRE.vueVerdict(pauvre));

    // Un `block_id` que `trace.blocs` ne résout pas : la ligne porte l'identifiant **seul**.
    const nonResolu = reponseVerdict({
      trace: Object.assign({}, traceRiche, { blocs: [] }) });
    cas.trace_bloc_non_resolu = resumerTrace(SINISTRE.vueVerdict(nonResolu));

    // Un contrôle dont le `name` n'est pas dans la table : affiché tel quel, jamais masqué.
    cas.trace_controle_inconnu = resumerTrace(SINISTRE.vueVerdict(reponseVerdict({
      trace: Object.assign({}, traceRiche, {
        steps: [{ name: "verifier", tier: "micro", ms: 3, opened_block_ids: [],
                  discarded_block_ids: [],
                  checks: [{ name: "controle_de_demain", ok: false, detail: "détail" }] }] }) })));

    // Une trace qui n'a rien à dire n'ouvre pas un `<details>` vide.
    // Une trace dont **rien** n'est lisible : pas d'identité, pas d'étape, pas même un coût (un
    // total non numérique n'est pas « gratuit », c'est un total qu'on n'a pas su lire).
    cas.trace_muette = resumerTrace(SINISTRE.vueVerdict(reponseVerdict({
      trace: { request_id: "", pipeline: "", variant: "", total_cost_eur: null, steps: [] } })));
    // Un coût **nul**, lui, est une mesure : aucun appel n'a été facturé, et cela se dit.
    cas.trace_cout_nul = resumerTrace(SINISTRE.vueVerdict(reponseVerdict({
      trace: { request_id: "", pipeline: "", variant: "", total_cost_eur: 0, steps: [] } })));
    cas.trace_absente = resumerTrace(SINISTRE.vueVerdict(reponseVerdict({ trace: null })));

    // M15 — le refus porte sa **preuve chiffrée** et son badge d'état.
    const vueRefus = SINISTRE.vueVerdict(reponseRefus());
    cas.refus_preuve = {
      preuve: textesDe(vueRefus, "preuve"),
      etat: aplatirVue(vueRefus).filter((n) => (n.cls || "").split(" ")[0] === "etat")
        .map((n) => ({ cls: n.cls, texte: n.texte })),
      phrase: textesDe(vueRefus, "etat-phrase"),
      // L'ordre de lecture : la preuve avant le pied, le pied avant la trace.
      ordre: (SINISTRE.vueVerdict(reponseRefus()).enfants || []).map((n) => n.cls),
    };

    // Une clarification : `AD-4` pose que « rien n'a été cherché ». Aucune preuve chiffrée, et la
    // phrase d'état le dit — lui accrocher « 0 variante essayée » répondrait à une question que
    // personne ne pose.
    const vueClar = SINISTRE.vueVerdict(reponseClarification());
    cas.clarification_preuve = {
      preuve: textesDe(vueClar, "preuve"),
      etat: aplatirVue(vueClar).filter((n) => (n.cls || "").split(" ")[0] === "etat")
        .map((n) => n.texte),
      phrase: textesDe(vueClar, "etat-phrase"),
    };

    // Un verdict trouvé mais incomplet : « partiel », et la phrase renvoie à la liste peinte.
    const vuePartielle = SINISTRE.vueVerdict(reponseVerdict());
    cas.etat_partiel = {
      etat: aplatirVue(vuePartielle).filter((n) => (n.cls || "").split(" ")[0] === "etat")
        .map((n) => ({ cls: n.cls, texte: n.texte })),
      phrase: textesDe(vuePartielle, "etat-phrase"),
      preuve: textesDe(vuePartielle, "preuve"),
    };

    // Et une réponse complète : « sûr ».
    const sure = reponseVerdict();
    sure.answer.complete = true;
    sure.answer.unknown = [];
    cas.etat_sur = {
      etat: aplatirVue(SINISTRE.vueVerdict(sure))
        .filter((n) => (n.cls || "").split(" ")[0] === "etat").map((n) => n.texte),
      phrase: textesDe(SINISTRE.vueVerdict(sure), "etat-phrase"),
    };

    // `reason` **absent** (une réponse trouvée) : ni preuve, ni badge inventé sur du vide.
    const sansReason = reponseVerdict();
    delete sansReason.answer.reason;
    sansReason.answer.found = false;
    sansReason.answer.claims = [];
    sansReason.sources = [];
    cas.sans_reason = {
      preuve: textesDe(SINISTRE.vueVerdict(sansReason), "preuve"),
      etat: aplatirVue(SINISTRE.vueVerdict(sansReason))
        .filter((n) => (n.cls || "").split(" ")[0] === "etat").map((n) => n.texte),
    };

    // Les fonctions pures, appelées directement : c'est elles que `test_tables_partagees.py`
    // confronte à celles du guide.
    cas.preuves = {
      zero_hit: SINISTRE.preuveAbsence({ kind: "zero_hit", terms_searched: ["mobilier"],
                                         variants_count: 0, blocks_scanned: 1457 }),
      singuliers: SINISTRE.preuveAbsence({ kind: "zero_hit", terms_searched: ["bail"],
                                           variants_count: 1, blocks_scanned: 1 }),
      sans_terme: SINISTRE.preuveAbsence({ kind: "hors_perimetre", terms_searched: [],
                                           variants_count: 0, blocks_scanned: 0 }),
      clarification: SINISTRE.preuveAbsence({ kind: "clarification_requise", terms_searched: [],
                                              variants_count: 0, blocks_scanned: 0 }),
      absente: SINISTRE.preuveAbsence(null),
    };
    cas.etats = {
      sur: SINISTRE.etatReponse({ found: true, complete: true }),
      partiel: SINISTRE.etatReponse({ found: true, complete: false }),
      inconnu: SINISTRE.etatReponse({ found: false, complete: false }),
      absent: SINISTRE.etatReponse(null),
    };
    cas.phrases_etat = {
      sur: SINISTRE.phraseEtat({ cle: "sur" }, { liste: false, preuve: false }),
      partiel_avec_liste: SINISTRE.phraseEtat({ cle: "partiel" }, { liste: true, preuve: false }),
      partiel_sans_liste: SINISTRE.phraseEtat({ cle: "partiel" }, { liste: false, preuve: false }),
      inconnu_avec_preuve: SINISTRE.phraseEtat({ cle: "inconnu" }, { liste: false, preuve: true }),
      inconnu_sans_preuve: SINISTRE.phraseEtat({ cle: "inconnu" }, { liste: false, preuve: false }),
      sans_contexte: SINISTRE.phraseEtat({ cle: "partiel" }, null),
      sans_etat: SINISTRE.phraseEtat(null, null),
      etat_inconnu: SINISTRE.phraseEtat({ cle: "farfelu" }, { liste: false, preuve: true }),
    };
    cas.tables = { controles: SINISTRE.CONTROLES, alertes: SINISTRE.ALERTES,
                   controle_inconnu: SINISTRE.libelleControle("controle_de_demain") };
  }

  // --- story 2.5 : `answer.reason` est lu **strictement** ------------------
  {
    const { SINISTRE } = charger(PAGE, () => reponseHttp({ corps: {} }));
    const corps = {
      reason_vide: (() => { const r = reponseRefus(); r.answer.reason = {}; return r; })(),
      reason_kind_inconnu: (() => { const r = reponseRefus();
                                    r.answer.reason.kind = "autre"; return r; })(),
      reason_termes_nuls: (() => { const r = reponseRefus();
                                   r.answer.reason.terms_searched = null; return r; })(),
      reason_compteur_chaine: (() => { const r = reponseRefus();
                                       r.answer.reason.blocks_scanned = "3"; return r; })(),
      reason_non_objet: (() => { const r = reponseRefus(); r.answer.reason = "zero_hit"; return r; })(),
    };
    cas.reason_illisible = {};
    for (const [nom, c] of Object.entries(corps)) {
      const { SINISTRE: s } = charger(PAGE, () => reponseHttp({ corps: c }));
      let erreur = null;
      let reponse = null;
      try { reponse = await s.soumettre(SAISIE); } catch (e) { erreur = e; }
      cas.reason_illisible[nom] = { a_repondu: reponse !== null, code: erreur && erreur.code,
                                    champ: erreur && erreur.champ };
    }
    // Et le contraire : `reason: null` est une **valeur** du contrat (une réponse trouvée).
    const trouvee = reponseVerdict();
    const { SINISTRE: s2 } = charger(PAGE, () => reponseHttp({ corps: trouvee }));
    const lue = await s2.soumettre(SAISIE);
    cas.reason_nul_est_lisible = lue.answer.reason === null;
    cas.reason_lisible_bornes = SINISTRE.bornes().question_max > 0;
  }

  // --- les erreurs : aucun repli, aucun verdict de remplacement ------------
  {
    const { SINISTRE } = charger(PAGE, () => reponseHttp({ corps: {} }));
    const codes = [
      { code: "invalid_request", statut: 400, kind: "requete" },
      { code: "input_too_long", statut: 413, kind: "requete" },
      { code: "rate_limited", statut: 429, kind: "requete", retry_after: 42 },
      { code: "llm_unavailable", statut: 503, kind: "indisponible" },
      { code: "timeout", statut: 503, kind: "indisponible" },
      { code: "budget_exceeded", statut: 503, kind: "indisponible" },
      { code: "corpus_unavailable", statut: 503, kind: "indisponible" },
      { code: "internal", statut: 500, kind: "requete" },
      { code: "reponse_illisible", statut: 200, kind: "requete" },
      { code: "reseau", statut: 0, kind: "indisponible" },
      { code: "timeout_client", statut: 0, kind: "indisponible" },
      { code: "hors_ligne", statut: 0, kind: "requete" },
      { code: "", statut: 0, kind: "requete" },
    ];
    cas.erreurs = {};
    for (const e of codes) {
      const erreur = Object.assign({ request_id: "r-err" }, e);
      const vue = SINISTRE.vueErreur(erreur);
      const plat = aplatirVue(vue);
      cas.erreurs[e.code || "sans_code"] = {
        message: SINISTRE.messageErreur(erreur),
        titre: textesDe(vue, "err-titre")[0],
        reference: textesDe(vue, "err-ref")[0] || null,
        boutons: plat.filter((n) => n.tag === "button").length,
        actions: plat.filter((n) => n.action).length,
        // Aucun badge de verdict, aucune clause, aucune portée : rien qui ressemble à un résultat.
        badges: plat.filter((n) => n.cls && n.cls.indexOf("badge") === 0).length,
        texte_entier: texteEntier(vue),
      };
    }
    cas.erreur_sans_reference = textesDe(SINISTRE.vueErreur({ code: "internal" }), "err-ref");
  }

  // --- la matérialisation : `textContent` seul (AD-15) --------------------
  {
    const { SINISTRE, document, elements, localStorage } = charger(PAGE, () => reponseHttp({ corps: {} }));
    // Une citation qui porte du balisage : `textContent` doit la rendre **littérale**, et le DOM
    // minimal lève sur toute pose d'`innerHTML` non vide — arriver ici le démontre.
    const r = reponseVerdict();
    r.sources[0].quote = "<script>alert(1)</script> action subite";
    r.answer.faits_compris.bien = "<img onerror=alert(1)>";
    const peint = SINISTRE.peindre(SINISTRE.vueVerdict(r), elements.resultat);
    const arbre = releverNoeud(peint);
    cas.dom = {
      badge: peint.querySelector(".badge").textContent,
      badge_cls: peint.querySelector(".badge").className,
      citation: peint.querySelector(".cl-q").textContent,
      fait_compris: peint.querySelectorAll(".fc-val").map((n) => n.textContent)[0],
      // Aucun bouton : la page ne propose aucune action de repli (AD-16).
      boutons: aplatir(arbre).filter((n) => n.tag === "button").length,
      // La trace est un `<details>` natif : dépliable sans une ligne de JavaScript.
      details: peint.querySelectorAll("details").length,
      summary: (peint.querySelector("summary") || {}).textContent,
      dans_le_conteneur: elements.resultat.childNodes.length,
      // AD-15 : rien de la conversation ni du sinistre n'atteint le navigateur.
      stockage: localStorage.entrees(),
      // `aria-live` est posé par la page, pas par le matérialiseur : il n'invente aucun attribut.
      attributs_racine: Object.fromEntries(peint.attributs),
    };

    // Une erreur **efface** le verdict précédent : aucun badge ne reste à l'écran.
    SINISTRE.peindre(SINISTRE.vueErreur({ code: "llm_unavailable", request_id: "r-err" }),
                     elements.resultat);
    cas.dom_apres_erreur = {
      badges: elements.resultat.querySelectorAll(".badge").length,
      clauses: elements.resultat.querySelectorAll(".clause").length,
      portees: elements.resultat.querySelectorAll(".portee").length,
      texte: elements.resultat.textContent,
      boutons: elements.resultat.querySelectorAll("button").length,
      msg_dans_le_document: document.querySelectorAll(".carte").length,
    };
  }

  // --- le formulaire piloté : de la frappe au verdict ----------------------
  {
    const { SINISTRE, appels, document, elements, localStorage } = charger(
      PAGE,
      (url) => {
        if (String(url).endsWith("/sante")) return reponseHttp({ corps: reponseSante() });
        if (String(url).endsWith("/documents")) return reponseHttp({ corps: DOCUMENTS });
        if (String(url).includes("/pages/")) {
          return reponseHttp({ entetes: { "X-Document-Pages": "12" } });
        }
        return reponseHttp({ corps: reponseVerdict() });
      },
      { demarrage: true });
    await tick();
    await tick();
    const options = elements.contrat.querySelectorAll("option");
    cas.demarrage = {
      options: options.map((o) => ({ valeur: o.value, texte: o.textContent })),
      select_desactive: !!elements.contrat.disabled,
      bouton_desactive: !!elements.analyser.disabled,
      message: elements["contrats-message"].textContent,
      // Les `maxlength` sont posés par le script depuis ses constantes (une seule source à
      // l'exécution) ; la page en porte aussi la valeur, comme repli sans JavaScript.
      maxlength: Object.fromEntries(["question", "description", "lieu", "date"]
        .map((id) => [id, elements[id].maxLength === undefined ? null : elements[id].maxLength])),
      // La borne d'abandon a été lue sur `/sante` au démarrage, pas recopiée.
      bornes: SINISTRE.bornes(),
      ordre_des_appels: appels.map((a) => String(a.url).replace(ORIGINE, "")),
      // Le lien vers la source publique du contrat sélectionné, **matérialisé**.
      source: (() => {
        const a = elements["contrat-source"].querySelector("a");
        return a ? { href: a.href, target: a.target, rel: a.rel, texte: a.textContent } : null;
      })(),
      audits: elements["documents-audit"].querySelectorAll(".audit-entree").map((e) => ({
        texte: e.textContent,
        href: (e.querySelector("a") || {}).href,
        target: (e.querySelector("a") || {}).target || "",
        rel: (e.querySelector("a") || {}).rel || "",
      })),
    };

    // Le sélecteur ne se réinitialise pas quand on change de contrat : le choix tient, et seul le
    // lien de source suit. Avec deux contrats servis, c'est ce qui décide du `doc_id` posté.
    elements.contrat.value = DOC_ID_2;
    elements.contrat.declencher("change");
    cas.changement = {
      valeur: elements.contrat.value,
      options: elements.contrat.querySelectorAll("option").map((o) => o.value),
      source: (() => {
        const a = elements["contrat-source"].querySelector("a");
        return a ? { href: a.href, texte: a.textContent } : null;
      })(),
    };

    elements.contrat.value = DOC_ID;
    elements.question.value = QUESTION;
    elements.description.value = SAISIE.description;
    elements.date.value = SAISIE.date;
    elements.lieu.value = SAISIE.lieu;
    elements.montant.value = SAISIE.montant_eur;
    const evenement = elements.formulaire.declencher("submit");
    cas.soumission_defaut_empeche = evenement.defautEmpeche;
    // L'attente est peinte **avant** l'appel : le verdict précédent quitte l'écran tout de suite.
    cas.attente_peinte = elements.resultat.querySelectorAll(".attente").length;
    cas.verrouille_pendant = ["contrat", "description", "analyser"]
      .map((id) => !!elements[id].disabled);
    await tick();
    await tick();
    await tick();
    const poste = appels.filter((a) => String(a.url).endsWith("/api/v1/sinistre"))[0];
    cas.soumission = {
      corps: JSON.parse(poste.options.body),
      badge: (elements.resultat.querySelector(".badge") || {}).textContent,
      attente_restante: elements.resultat.querySelectorAll(".attente").length,
      verrouille_apres: ["contrat", "description", "analyser"].map((id) => !!elements[id].disabled),
      stockage: localStorage.entrees(),
      cartes: document.querySelectorAll(".carte").length,
      commandes_pdf: elements.resultat.querySelectorAll(".cl-ouvrir").length,
    };

    // Le lecteur ne charge rien avant l'activation explicite de la clause. Au clic, l'URL ne
    // transporte que le document, la page et les identifiants de lignes encodés.
    const avantLecteur = appels.filter((a) => String(a.url).includes("/pages/")).length;
    const commande = elements.resultat.querySelector(".cl-ouvrir");
    commande.focus();
    commande.declencher("click");
    await tick();
    await tick();
    const apresOuverture = appels.filter((a) => String(a.url).includes("/pages/"));
    cas.lecteur = {
      appels_avant_clic: avantLecteur,
      urls: apresOuverture.map((a) => String(a.url).replace(ORIGINE, "")),
      statut: elements["lecteur-statut"].textContent,
      image_src: elements["lecteur-image"].src,
      image_alt: elements["lecteur-image"].alt,
      precedent_desactive: !!elements["lecteur-precedent"].disabled,
      suivant_desactive: !!elements["lecteur-suivant"].disabled,
      source: {
        href: elements["lecteur-source"].href,
        target: elements["lecteur-source"].target,
        rel: elements["lecteur-source"].rel,
      },
      focus_ouverture: document.actif && document.actif.id,
    };
    elements["lecteur-suivant"].declencher("click");
    await tick();
    await tick();
    cas.lecteur.navigation_url = appels.filter((a) => String(a.url).includes("/pages/"))
      .map((a) => String(a.url).replace(ORIGINE, "")).slice(-1)[0];
    cas.lecteur.sans_surlignage = elements["lecteur-sans-surlignage"].textContent;
    elements["lecteur-fermer"].declencher("click");
    cas.lecteur.focus_fermeture = document.actif && document.actif.className;
    cas.url_page_encodee = SINISTRE.urlPage(
      DOC_ID, 9, ["cg-mini:p9:2", "cg-mini:p9:3"], ["p9:2:l 1/é", "p9:2:l2"]);
  }

  // Une citation sans lignes ouvre honnêtement la page sans query de surlignage.
  {
    const { SINISTRE, appels, elements } = charger(PAGE, (url) => {
      if (String(url).includes("/pages/")) {
        return reponseHttp({ entetes: { "X-Document-Pages": "12" } });
      }
      return reponseHttp({ corps: {} });
    });
    const r = reponseVerdict();
    const racine = SINISTRE.peindre(SINISTRE.vueVerdict(r, {
      doc_id: DOC_ID, source_url: DOCUMENTS[0].source_url
    }), elements.resultat);
    SINISTRE.brancherLecteur(racine);
    const commandes = racine.querySelectorAll(".cl-ouvrir");
    commandes[1].declencher("click");
    await tick();
    await tick();
    cas.lecteur_sans_lignes = {
      url: String(appels[0].url).replace(ORIGINE, ""),
      explication: elements["lecteur-sans-surlignage"].textContent,
      visible: !elements["lecteur-sans-surlignage"].hidden,
      suivant_desactive: !!elements["lecteur-suivant"].disabled,
    };
  }

  // Une réponse image en échec reste dans le lecteur, sans effacer le verdict ni le lien public.
  {
    const { SINISTRE, elements } = charger(PAGE, (url) => {
      if (String(url).includes("/pages/")) return reponseHttp({ status: 503 });
      return reponseHttp({ corps: {} });
    });
    const r = reponseVerdict();
    const racine = SINISTRE.peindre(SINISTRE.vueVerdict(r, {
      doc_id: DOC_ID, source_url: DOCUMENTS[0].source_url
    }), elements.resultat);
    SINISTRE.brancherLecteur(racine);
    racine.querySelector(".cl-ouvrir").declencher("click");
    await tick();
    await tick();
    cas.lecteur_en_echec = {
      statut: elements["lecteur-statut"].textContent,
      badge: elements.resultat.querySelector(".badge").textContent,
      source: elements["lecteur-source"].href,
      image_cachee: !!elements["lecteur-image"].hidden,
    };
  }

  // Navigation rapide : la réponse de la page quittée arrive après la plus récente. Même si le
  // double ignore l'abort réseau, la génération obsolète ne peut remplacer ni image ni statut.
  {
    const attentes = [];
    const { SINISTRE, elements } = charger(PAGE, (url, options) => new Promise((resolve) => {
      attentes.push({ url: String(url), options, resolve });
    }));
    const premiere = SINISTRE.ouvrirLecteur({
      doc_id: DOC_ID, page: 9, block_ids: ["cg-mini:p9:2"], line_ids: ["p9:2:l1"],
      source_url: DOCUMENTS[0].source_url,
    });
    const seconde = SINISTRE.naviguerLecteur(1);
    attentes[1].resolve(reponseHttp({ entetes: { "X-Document-Pages": "12" } }));
    await seconde;
    const srcRecent = elements["lecteur-image"].src;
    attentes[0].resolve(reponseHttp({ entetes: { "X-Document-Pages": "12" } }));
    await premiere;
    cas.lecteur_reponses_inversees = {
      urls: attentes.map((a) => a.url.replace(ORIGINE, "")),
      premiere_annulee: attentes[0].options.signal.aborted,
      src_recent: srcRecent,
      src_final: elements["lecteur-image"].src,
      statut: elements["lecteur-statut"].textContent,
    };
  }

  // Le timeout/abort couvre aussi la consommation du blob, pas seulement l'arrivée des headers.
  {
    let blobCommence = false;
    let blobAnnule = false;
    let signal = null;
    const { SINISTRE } = charger(PAGE, (_url, options) => {
      signal = options.signal;
      const response = reponseHttp({ entetes: { "X-Document-Pages": "12" } });
      response.blob = () => new Promise((_resolve, reject) => {
        blobCommence = true;
        options.signal.addEventListener("abort", () => {
          blobAnnule = true;
          reject(new Error("blob annulé"));
        });
      });
      return response;
    });
    const charge = SINISTRE.ouvrirLecteur({
      doc_id: DOC_ID, page: 9, block_ids: ["cg-mini:p9:2"], line_ids: ["p9:2:l1"],
      source_url: DOCUMENTS[0].source_url,
    });
    await tick();
    SINISTRE.fermerLecteur();
    await charge;
    cas.lecteur_blob_annule = {
      blob_commence: blobCommence,
      signal_annule: signal && signal.aborted,
      blob_annule: blobAnnule,
    };
  }

  // --- une description vide ne part jamais --------------------------------
  {
    const { SINISTRE, appels, elements } = charger(
      PAGE,
      (url) => {
        if (String(url).endsWith("/sante")) return reponseHttp({ corps: reponseSante() });
        if (String(url).endsWith("/documents")) return reponseHttp({ corps: DOCUMENTS });
        return reponseHttp({ corps: reponseVerdict() });
      },
      { demarrage: true });
    await tick();
    await tick();
    cas.saisie_incomplete = {};
    const incompletes = [
      { nom: "description_vide", doc_id: DOC_ID, question: QUESTION, description: "   " },
      { nom: "question_vide", doc_id: DOC_ID, question: "  ", description: SAISIE.description },
      // Pas de cas « contrat vide » ici : un `<select>` peuplé a toujours une valeur, dans le
      // navigateur comme dans le DOM minimal. La branche est couverte par `cas.manquant`, qui
      // appelle la fonction pure — c'est la seule façon honnête de l'atteindre.
    ];
    for (const c of incompletes) {
      elements.contrat.value = c.doc_id;
      elements.question.value = c.question;
      elements.description.value = c.description;
      elements.formulaire.declencher("submit");
      await tick();
      cas.saisie_incomplete[c.nom] = {
        appels: appels.filter((a) => String(a.url).endsWith("/api/v1/sinistre")).length,
        // Un bouton qui ne fait rien et ne dit rien est un bouton cassé : la page **dit** ce qui manque.
        texte: elements.resultat.textContent,
        cartes_erreur: elements.resultat.querySelectorAll(".erreur").length,
        badges: elements.resultat.querySelectorAll(".badge").length,
        boutons: elements.resultat.querySelectorAll("button").length,
      };
    }
    // Le champ pré-rempli vidé à la main : la page le refuse localement, sans aller-retour payé.
    cas.manquant = {
      complet: SINISTRE.manquant(SAISIE),
      sans_description: SINISTRE.manquant(Object.assign({}, SAISIE, { description: " " })),
      sans_question: SINISTRE.manquant(Object.assign({}, SAISIE, { question: "" })),
      sans_contrat: SINISTRE.manquant(Object.assign({}, SAISIE, { doc_id: "" })),
      // Facultatif, donc vide passe ; illisible ne passe pas (revue Codex 1.9, I1).
      montant_vide: SINISTRE.manquant(Object.assign({}, SAISIE, { montant_eur: "" })),
      montant_negatif: SINISTRE.manquant(Object.assign({}, SAISIE, { montant_eur: "-100" })),
      montant_mots: SINISTRE.manquant(Object.assign({}, SAISIE, { montant_eur: "douze" })),
    };

    // Et la soumission DOM elle-même : un montant négatif ne part pas, et ne part surtout pas
    // **amputé de son montant** — ce que la suppression silencieuse produisait.
    elements.contrat.value = DOC_ID;
    elements.question.value = QUESTION;
    elements.description.value = SAISIE.description;
    elements.montant.value = "-100";
    const avant = appels.filter((a) => String(a.url).endsWith("/api/v1/sinistre")).length;
    elements.formulaire.declencher("submit");
    await tick();
    cas.montant_negatif_soumis = {
      appels: appels.filter((a) => String(a.url).endsWith("/api/v1/sinistre")).length - avant,
      texte: elements.resultat.textContent,
      cartes_erreur: elements.resultat.querySelectorAll(".erreur").length,
      badges: elements.resultat.querySelectorAll(".badge").length,
    };
    elements.montant.value = "";
  }

  // --- le serveur ne répond pas au chargement : le formulaire le dit --------
  {
    const { elements } = charger(
      PAGE,
      (url) => {
        if (String(url).endsWith("/sante")) return reponseHttp({ corps: reponseSante() });
        return reponseHttp({ status: 503,
          corps: { error: { code: "llm_unavailable", message: "en anglais", request_id: "r-503" } } });
      },
      { demarrage: true });
    await tick();
    await tick();
    await tick();
    cas.documents_en_echec = {
      message: elements["contrats-message"].textContent,
      bouton_desactive: !!elements.analyser.disabled,
      cartes_erreur: elements.resultat.querySelectorAll(".erreur").length,
      texte: elements.resultat.textContent,
      badges: elements.resultat.querySelectorAll(".badge").length,
    };
  }

  // --- `/documents` rend un 200 illisible : ce n'est pas « aucun contrat servi » ---
  {
    const { elements } = charger(
      PAGE,
      (url) => {
        if (String(url).endsWith("/sante")) return reponseHttp({ corps: reponseSante() });
        if (String(url).endsWith("/documents")) return reponseHttp({ corps: { oups: true } });
        return reponseHttp({ corps: reponseVerdict() });
      },
      { demarrage: true });
    await tick();
    await tick();
    await tick();
    cas.documents_illisibles = {
      message: elements["contrats-message"].textContent,
      bouton_desactive: !!elements.analyser.disabled,
      texte: elements.resultat.textContent,
      badges: elements.resultat.querySelectorAll(".badge").length,
    };
  }

  // --- la sonde ne répond pas : le repli tient, et la page marche quand même -
  {
    const { SINISTRE, elements } = charger(
      PAGE,
      (url) => {
        if (String(url).endsWith("/sante")) throw new Error("sonde injoignable");
        if (String(url).endsWith("/documents")) return reponseHttp({ corps: DOCUMENTS });
        return reponseHttp({ corps: reponseVerdict() });
      },
      { demarrage: true });
    await tick();
    await tick();
    await tick();
    cas.sonde_en_echec = {
      bornes: SINISTRE.bornes(),
      options: elements.contrat.querySelectorAll("option").length,
      bouton_desactive: !!elements.analyser.disabled,
    };
  }

  // --- des seuils absurdes ne déplacent pas la borne d'abandon --------------
  {
    const { SINISTRE } = charger(PAGE, (url) => (String(url).endsWith("/sante")
      ? reponseHttp({ corps: reponseSante({ deadline_s: "beaucoup", client_abort_margin_s: -3 }) })
      : reponseHttp({ corps: {} })));
    await SINISTRE.sonder();
    cas.seuils_absurdes = SINISTRE.bornes();
  }

  // --- un 200 illisible n'est pas un verdict ------------------------------
  {
    const incomplets = {
      sans_answer: {},
      sans_verdict: (() => { const r = reponseVerdict(); delete r.answer.verdict; return r; })(),
      verdict_null: (() => { const r = reponseVerdict(); r.answer.verdict = null; return r; })(),
      sans_valeur: (() => {
        const r = reponseVerdict(); delete r.answer.verdict.value; return r;
      })(),
      sans_trace: (() => { const r = reponseVerdict(); delete r.trace; return r; })(),
      sources_null: (() => { const r = reponseVerdict(); r.sources = null; return r; })(),
      clause_sans_kind: (() => {
        const r = reponseVerdict(); delete r.sources[0].kind; return r;
      })(),
      // Revue Codex 1.9 (I2) : un 200 amputé de ce qu'UX-DR6 fait afficher n'est pas un verdict
      // sobre, c'est un serveur cassé. Aucun de ces champs n'est facultatif côté serveur.
      sans_sources: (() => { const r = reponseVerdict(); delete r.sources; return r; })(),
      sans_raison: (() => {
        const r = reponseVerdict(); delete r.answer.verdict.reason; return r;
      })(),
      sans_paquet: (() => {
        const r = reponseVerdict(); delete r.answer.verdict.missing; return r;
      })(),
      paquet_null: (() => { const r = reponseVerdict(); r.answer.verdict.missing = null; return r; })(),
      sans_ask_client: (() => {
        const r = reponseVerdict(); delete r.answer.verdict.ask_client; return r;
      })(),
      sans_escalate: (() => {
        const r = reponseVerdict(); delete r.answer.verdict.escalate; return r;
      })(),
      sans_claims: (() => { const r = reponseVerdict(); delete r.answer.claims; return r; })(),
      sans_rejetees: (() => {
        const r = reponseVerdict(); delete r.answer.rejected_claims; return r;
      })(),
      // Revue Codex 1.9 (tour 2, I2) : la présence des conteneurs ne suffit pas. Un conteneur bien
      // formé dont les **feuilles** ne le sont pas fabrique ou omet une réserve tout aussi bien.
      // `missing: {}` est le contre-exemple : il passait le contrôle d'objet, et `paquetVue()`
      // annonçait alors les quatre pièces du contrat comme non lues.
      paquet_vide: (() => { const r = reponseVerdict(); r.answer.verdict.missing = {}; return r; })(),
      piece_absente: (() => {
        const r = reponseVerdict(); delete r.answer.verdict.missing.avenants; return r;
      })(),
      piece_en_chaine: (() => {
        const r = reponseVerdict(); r.answer.verdict.missing.date_effet = "oui"; return r;
      })(),
      fait_manquant_objet: (() => {
        const r = reponseVerdict(); r.answer.verdict.missing.faits = [{ libelle: "subit" }]; return r;
      })(),
      question_objet: (() => {
        const r = reponseVerdict(); r.answer.verdict.ask_client[1] = { texte: "?" }; return r;
      })(),
      escalade_nombre: (() => {
        const r = reponseVerdict(); r.answer.verdict.escalate[0] = 3; return r;
      })(),
      claim_null: (() => { const r = reponseVerdict(); r.answer.claims[1] = null; return r; })(),
      claim_sans_texte: (() => {
        const r = reponseVerdict(); delete r.answer.claims[0].text; return r;
      })(),
      claim_sans_statut: (() => {
        const r = reponseVerdict(); delete r.answer.claims[1].status; return r;
      })(),
      claim_statut_sans_edition: (() => {
        const r = reponseVerdict(); delete r.answer.claims[0].status.edition; return r;
      })(),
      claim_statut_applicable_objet: (() => {
        const r = reponseVerdict(); r.answer.claims[0].status.applicable = { v: "oui" }; return r;
      })(),
      claim_sans_quotes: (() => {
        const r = reponseVerdict(); r.answer.claims[1].quotes = []; return r;
      })(),
      claim_quote_sans_bloc: (() => {
        const r = reponseVerdict(); delete r.answer.claims[0].quotes[0].block_id; return r;
      })(),
      rejetee_sans_kind: (() => {
        const r = reponseVerdict(); delete r.answer.rejected_claims[0].rejection_kind; return r;
      })(),
      clause_null: (() => { const r = reponseVerdict(); r.sources[1] = null; return r; })(),
      clause_page_en_chaine: (() => {
        const r = reponseVerdict(); r.sources[0].page = "9"; return r;
      })(),
      clause_sans_lignes: (() => {
        const r = reponseVerdict(); delete r.sources[0].line_ids; return r;
      })(),
      clause_lignes_non_liste: (() => {
        const r = reponseVerdict(); r.sources[0].line_ids = "p9:2:l1"; return r;
      })(),
      clause_ligne_nombre: (() => {
        const r = reponseVerdict(); r.sources[0].line_ids = [4]; return r;
      })(),
      clause_sans_statut: (() => {
        const r = reponseVerdict(); delete r.sources[1].status; return r;
      })(),
      clause_typage_non_booleen: (() => {
        const r = reponseVerdict(); r.sources[0].kind_confirmed = "oui"; return r;
      })(),
      inconnu_objet: (() => {
        const r = reponseVerdict(); r.answer.unknown = [{ texte: "?" }]; return r;
      })(),
      sans_inconnus: (() => { const r = reponseVerdict(); delete r.answer.unknown; return r; })(),
      faits_compris_en_chaine: (() => {
        const r = reponseVerdict(); r.answer.faits_compris = "mobilier de salon"; return r;
      })(),
      fait_compris_objet: (() => {
        const r = reponseVerdict(); r.answer.faits_compris.evenement = { v: "brûlure" }; return r;
      })(),
      themes_non_liste: (() => {
        const r = reponseVerdict(); r.answer.faits_compris.themes = "habitation"; return r;
      })(),
      theme_objet: (() => {
        const r = reponseVerdict(); r.answer.faits_compris.themes[1] = { t: "incendie" }; return r;
      })(),
      clarification_objet: (() => {
        const r = reponseVerdict(); r.answer.clarification = { q: "quel bien ?" }; return r;
      })(),
      // Revue Codex 1.9 (tour 3, I2), premier volet : une clé **absente** n'est pas un champ à
      // `None`. Les routes publient avec `response_model_exclude_none=False`, donc pydantic écrit
      // toujours la clé : son absence est un serveur cassé, et la tolérer faisait **retrancher** en
      // silence — une clause sans son numéro de page, une clarification escamotée, une ligne de
      // « ce que j'ai compris » disparue.
      sans_clarification: (() => {
        const r = reponseVerdict(); delete r.answer.clarification; return r;
      })(),
      sans_faits_compris: (() => {
        const r = reponseVerdict(); delete r.answer.faits_compris; return r;
      })(),
      fait_compris_absent: (() => {
        const r = reponseVerdict(); delete r.answer.faits_compris.cause; return r;
      })(),
      clause_sans_cle_page: (() => {
        const r = reponseVerdict(); delete r.sources[0].page; return r;
      })(),
      claim_statut_sans_cle_applicable: (() => {
        const r = reponseVerdict(); delete r.answer.claims[0].status.applicable; return r;
      })(),
      // Second volet : la trace est **affichée**, et le tour 2 ne l'avait durcie que sur son
      // `request_id`. Une trace amputée se peignait en lignes vides — et un coût absent se peignait
      // en **rien du tout**, alors que NFR4 exige le coût réel à l'écran.
      trace_sans_pipeline: (() => {
        const r = reponseVerdict(); delete r.trace.pipeline; return r;
      })(),
      trace_pipeline_objet: (() => {
        const r = reponseVerdict(); r.trace.pipeline = { nom: "sinistre" }; return r;
      })(),
      trace_sans_variante: (() => {
        const r = reponseVerdict(); delete r.trace.variant; return r;
      })(),
      trace_sans_cout: (() => {
        const r = reponseVerdict(); delete r.trace.total_cost_eur; return r;
      })(),
      trace_cout_en_chaine: (() => {
        const r = reponseVerdict(); r.trace.total_cost_eur = "0,0336"; return r;
      })(),
      trace_cout_infini: (() => {
        const r = reponseVerdict(); r.trace.total_cost_eur = Infinity; return r;
      })(),
      trace_sans_etapes: (() => {
        const r = reponseVerdict(); delete r.trace.steps; return r;
      })(),
      trace_etape_null: (() => {
        const r = reponseVerdict(); r.trace.steps[0] = null; return r;
      })(),
      trace_etape_sans_nom: (() => {
        const r = reponseVerdict(); delete r.trace.steps[1].name; return r;
      })(),
      trace_etape_nom_objet: (() => {
        const r = reponseVerdict(); r.trace.steps[0].name = { n: "comprendre" }; return r;
      })(),
      trace_etape_tier_objet: (() => {
        const r = reponseVerdict(); r.trace.steps[0].tier = { t: "micro" }; return r;
      })(),
      trace_etape_ms_en_chaine: (() => {
        const r = reponseVerdict(); r.trace.steps[1].ms = "1200"; return r;
      })(),
      trace_etape_ms_negatif: (() => {
        const r = reponseVerdict(); r.trace.steps[1].ms = -1; return r;
      })(),
      trace_etape_ms_fractionnaire: (() => {
        const r = reponseVerdict(); r.trace.steps[1].ms = 1.5; return r;
      })(),
      trace_etape_sans_controles: (() => {
        const r = reponseVerdict(); delete r.trace.steps[0].checks; return r;
      })(),
      trace_controle_en_chaine: (() => {
        const r = reponseVerdict(); r.trace.steps[1].checks[0] = "applicabilite_incomplete"; return r;
      })(),
      trace_controle_sans_nom: (() => {
        const r = reponseVerdict(); delete r.trace.steps[1].checks[0].name; return r;
      })(),
      trace_controle_sans_ok: (() => {
        const r = reponseVerdict(); delete r.trace.steps[1].checks[0].ok; return r;
      })(),
      trace_bloc_titre_nombre: (() => {
        const r = reponseVerdict();
        r.trace.blocs = [{ block_id: "cg:p1:2", doc_id: "cg", node_id: "cg:socle",
                           fiche_id: null, titre: 42 }];
        return r;
      })(),
      trace_seuil_chaine: (() => {
        const r = reponseVerdict(); r.trace.thresholds = { max_opens: "8" }; return r;
      })(),
      trace_seuil_booleen: (() => {
        const r = reponseVerdict(); r.trace.thresholds = { max_opens: true }; return r;
      })(),
      trace_seuil_infini: (() => {
        const r = reponseVerdict(); r.trace.thresholds = { max_opens: Infinity }; return r;
      })(),
      // Story 4.2f : le second porteur d'un `found=false`, lu aussi strictement que le premier.
      lecture_sans_compteur: (() => {
        const r = reponseLecturePartielle();
        delete r.answer.lecture_partielle.nodes_read; return r;
      })(),
      lecture_compteur_negatif: (() => {
        const r = reponseLecturePartielle();
        r.answer.lecture_partielle.blocks_read = -1; return r;
      })(),
      lecture_compteur_chaine: (() => {
        const r = reponseLecturePartielle();
        r.answer.lecture_partielle.nodes_read = "2"; return r;
      })(),
      lecture_non_objet: (() => {
        const r = reponseLecturePartielle();
        r.answer.lecture_partielle = "partielle"; return r;
      })(),
      lecture_documents_nuls: (() => {
        const r = reponseLecturePartielle();
        r.answer.lecture_partielle.documents = null; return r;
      })(),
      deux_porteurs: (() => {
        const r = reponseLecturePartielle();
        r.answer.reason = { kind: "claims_rejetes", terms_searched: [], variants_count: 0,
                            blocks_scanned: 3, documents: [DOC_ID] };
        return r;
      })(),
      aucun_porteur: (() => {
        const r = reponseRefus(); r.answer.reason = null; return r;
      })(),
      lecture_sans_manque: (() => {
        const r = reponseLecturePartielle(); r.answer.unknown = []; return r;
      })(),
      // I1 : les deux porteurs sont interdits sous `found: true`, pas seulement le second.
      reason_sur_reponse_trouvee: (() => {
        const r = reponseVerdict();
        r.answer.reason = { kind: "claims_rejetes", terms_searched: [], variants_count: 0,
                            blocks_scanned: 3, documents: [DOC_ID] };
        return r;
      })(),
      // I2 : les deux compteurs ont un plancher à 1 — zéro section pour au moins un passage est un
      // état impossible, zéro passage est une erreur terminale d'AD-1/NFR2.
      lecture_sans_section: (() => {
        const r = reponseLecturePartielle();
        r.answer.lecture_partielle.nodes_read = 0; return r;
      })(),
      lecture_sans_passage: (() => {
        const r = reponseLecturePartielle();
        r.answer.lecture_partielle.blocks_read = 0; return r;
      })(),
      lecture_sur_reponse_trouvee: (() => {
        const r = reponseVerdict();
        r.answer.lecture_partielle = { nodes_read: 1, blocks_read: 2, documents: [DOC_ID] };
        return r;
      })(),
    };
    cas.illisibles = {};
    for (const [nom, corps] of Object.entries(incomplets)) {
      const { SINISTRE } = charger(PAGE, () => reponseHttp({ corps }));
      let erreur = null;
      try { await SINISTRE.soumettre(SAISIE); } catch (e) { erreur = e; }
      cas.illisibles[nom] = erreur
        ? { kind: erreur.kind, code: erreur.code, champ: erreur.champ || null }
        : null;
    }
  }

  // --- ce qu'un lecteur strict ne doit **pas** refuser -------------------
  //
  // Le symétrique du relevé précédent, et la seule chose qui empêche « plus strict » de devenir
  // « inutilisable » : les champs `X | None` du contrat valent `null` de plein droit, et les listes
  // vides sont des réponses ordinaires (un refus n'a ni clause, ni question, ni thème).
  {
    const conformes = {
      refus: reponseRefus(),
      clarification: reponseClarification(),
      faits_compris_null: (() => {
        const r = reponseVerdict(); r.answer.faits_compris = null; return r;
      })(),
      faits_compris_partiel: (() => {
        const r = reponseVerdict();
        r.answer.faits_compris = { themes: [], bien: null, evenement: null, lieu: null,
                                   cause: null, moment: null };
        return r;
      })(),
      clause_sans_page: (() => { const r = reponseVerdict(); r.sources[0].page = null; return r; })(),
      statut_sans_applicable: (() => {
        const r = reponseVerdict();
        r.answer.claims[0].status = { retrouvee: true, pertinente: null, applicable: null,
                                      edition: "juin 2017" };
        return r;
      })(),
      listes_vides: (() => {
        const r = reponseVerdict();
        r.answer.verdict.ask_client = [];
        r.answer.verdict.escalate = [];
        r.answer.verdict.missing.faits = [];
        r.answer.unknown = [];
        return r;
      })(),
      // Le garde-fou du tour 3 : ce que la trace a de légitimement creux. `restituer` n'appelle
      // aucun modèle (`tier: null`), une trace peut n'avoir aucune étape (`reponseRefus`), et une
      // analyse qui n'a rien coûté vaut `0` — pas « pas de coût ».
      trace_sans_etape: (() => {
        const r = reponseVerdict(); r.trace.steps = []; return r;
      })(),
      trace_cout_nul: (() => {
        const r = reponseVerdict(); r.trace.total_cost_eur = 0; return r;
      })(),
      trace_etape_sans_appel: (() => {
        const r = reponseVerdict();
        r.trace.steps = [{ name: "restituer", tier: null, ms: 1, checks: [] }];
        return r;
      })(),
      // Story 4.2f : le corps que la story fait naître **est** un corps servable. Un lecteur qui le
      // refuserait remplacerait le 503 par un écran illisible — une régression pire encore.
      lecture_partielle: reponseLecturePartielle(),
    };
    cas.lisibles = {};
    for (const [nom, corps] of Object.entries(conformes)) {
      const { SINISTRE } = charger(PAGE, () => reponseHttp({ corps }));
      let erreur = null;
      let vue = null;
      try { vue = await SINISTRE.soumettre(SAISIE); } catch (e) { erreur = e; }
      cas.lisibles[nom] = erreur
        ? { refuse: true, champ: erreur.champ || null }
        : { refuse: false, verdict: vue.answer.verdict.value };
    }
  }

  // --- les codes HTTP remontent typés -------------------------------------
  {
    const situations = [
      { nom: "invalid_request", status: 400, code: "invalid_request" },
      { nom: "rate_limited", status: 429, code: "rate_limited", entetes: { "Retry-After": "42" } },
      { nom: "llm_unavailable", status: 503, code: "llm_unavailable" },
      { nom: "internal", status: 500, code: "internal" },
    ];
    cas.http = {};
    for (const s of situations) {
      const { SINISTRE } = charger(PAGE, () => reponseHttp({
        status: s.status, entetes: s.entetes || {},
        corps: { error: { code: s.code, message: "message serveur en anglais", request_id: "r-9" } },
      }));
      let erreur = null;
      try { await SINISTRE.soumettre(SAISIE); } catch (e) { erreur = e; }
      cas.http[s.nom] = {
        kind: erreur.kind, code: erreur.code, statut: erreur.statut,
        retry_after: erreur.retry_after, request_id: erreur.request_id,
        // Le `message` du serveur n'est **jamais** affiché : la phrase vient du code d'AD-16.
        message: SINISTRE.messageErreur(erreur),
      };
    }
    // Panne réseau : `fetch` rejette, la page n'invente rien.
    const { SINISTRE } = charger(PAGE, () => { throw new Error("réseau coupé"); });
    let erreur = null;
    try { await SINISTRE.soumettre(SAISIE); } catch (e) { erreur = e; }
    cas.http.reseau = { kind: erreur.kind, code: erreur.code,
                        message: SINISTRE.messageErreur(erreur) };
  }

  // --- page ouverte en file:// : rien à poster ----------------------------
  {
    const { SINISTRE, appels } = charger("file:///Users/quelquun/tools/sinistre/index.html",
                                          () => reponseHttp({ corps: {} }));
    let erreur = null;
    try { await SINISTRE.soumettre(SAISIE); } catch (e) { erreur = e; }
    cas.hors_ligne = {
      api_base: SINISTRE.apiBase(),
      code: erreur && erreur.code,
      appels_reseau: appels.length,
      message: SINISTRE.messageErreur(erreur),
    };
  }

  return cas;
}

main().then(
  (resultat) => {
    process.stdout.write(JSON.stringify({ ok: true, node: process.version, cas: resultat }, null, 1));
  },
  (erreur) => {
    process.stdout.write(JSON.stringify({
      ok: false, node: process.version,
      erreur: String((erreur && erreur.stack) || erreur),
    }, null, 1));
    process.exitCode = 1;
  },
);
