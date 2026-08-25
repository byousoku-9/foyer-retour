// Harnais Node du **matérialiseur** de `web/app/ui.js` (story 1.7, revue Codex tour 1).
//
// `chat.js` décrit ce qu'il faut peindre, `ui.js` le peint. La description était assertée par 80
// relevés ; la peinture ne l'était par rien — elle était lue dans le source, jamais exécutée. C'est
// ainsi qu'un badge de mode posé dans la seule section « Assistant » (donc `hidden` dès qu'on
// regarde un autre onglet, c'est-à-dire chaque fois que le widget flottant sert) a traversé la
// revue interne : aucun test ne pouvait le voir.
//
// On charge donc `kb.js`, `chat.js` puis `ui.js` dans un `node:vm` avec le DOM minimal de
// `dom_minimal.mjs`. `ui.js` expose `window.UI` **avant** son démarrage et saute celui-ci quand
// `window.__UI_SANS_DEMARRAGE` est posé : le site entier a besoin d'un `index.html`, le
// matérialiseur n'a besoin de rien.
//
// Comme `chat_cases.mjs`, ce fichier ne juge rien : il relève, et `tests/test_web_chat.py` asserte.

import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

import { Document, stockage } from "./dom_minimal.mjs";

const ICI = path.dirname(fileURLToPath(import.meta.url));
const RACINE = path.resolve(ICI, "..", "..");

// Les éléments que `ui.js` cherche dans le chat. Les identifiants sont ceux de `web/index.html` ;
// un test Python les y vérifie, pour qu'un renommage dans la page ne laisse pas ce harnais peindre
// dans le vide.
const ELEMENTS = [
  { tag: "div", id: "chat-log", cls: "chat-log" },
  { tag: "div", id: "widget-log", cls: "chat-log" },
  { tag: "span", id: "mode-badge", cls: "badge" },
  { tag: "span", id: "widget-badge", cls: "badge" },
  { tag: "input", id: "chat-input" },
  { tag: "input", id: "widget-input" },
  { tag: "button", id: "chat-send" },
  { tag: "button", id: "widget-send" },
];

function monter(fetchDouble) {
  const document = new Document();
  const elements = {};
  for (const spec of ELEMENTS) {
    const e = document.createElement(spec.tag);
    e.id = spec.id;
    if (spec.cls) e.className = spec.cls;
    document.body.appendChild(e);
    elements[spec.id] = e;
  }

  const localStorage = stockage();
  const window = {
    location: new URL("https://foyer-retour.example/guide/#assistant"),
    // Sous le seuil du rail : `guiderVersFiche()` s'arrête avant de toucher l'onglet Fiches, qui
    // n'existe pas dans ce DOM minimal. Ce n'est pas ce qu'on mesure ici.
    innerWidth: 800,
    document,
    localStorage,
    addEventListener: () => {},
    matchMedia: () => ({ matches: false, addEventListener: () => {} }),
    // Aucun appel réseau n'est déclenché : le démarrage (donc la sonde) est sauté, et la seule
    // action exercée est la recherche simple, qui est purement locale. Un `fetch` appelé quand
    // même doit faire échouer le harnais, pas passer inaperçu.
    fetch: fetchDouble || (() => { throw new Error("aucun appel réseau n'est attendu dans ce harnais"); }),
    __UI_SANS_DEMARRAGE: true,
  };

  const journal = new console.Console(process.stderr, process.stderr);
  const bac = {
    window, document, localStorage, console: journal, URL,
    setTimeout: () => 0, clearTimeout: () => {}, JSON, Math, Date, Number, String, Array, Object,
    // `chat.js` appelle `fetch` et `AbortController` **nus** : sur `window` seul, ils resteraient
    // introuvables dans le bac à sable. Sans double fourni, `fetch` lève — un appel réseau non
    // attendu dans ce harnais doit faire échouer, pas passer inaperçu.
    Promise, isFinite, AbortController,
    fetch: fetchDouble || (() => { throw new Error("aucun appel réseau n'est attendu dans ce harnais"); }),
  };
  bac.globalThis = bac;
  vm.createContext(bac);
  for (const fichier of ["web/app/kb.js", "web/app/chat.js", "web/app/ui.js"]) {
    vm.runInContext(readFileSync(path.join(RACINE, fichier), "utf8"), bac, { filename: fichier });
  }
  window.SIM = { comparatif: () => [], calcul: () => ({}) };
  window.CONTRATS_KB = { contrats: [], scenarios: [] };
  return { window, document, elements, localStorage };
}

/** Arbre relevé d'un nœud du DOM : tag, classes, attributs posés, texte propre, enfants. */
function releverNoeud(n) {
  if (n.estTexte) return { tag: "#texte", texte: n.textContent };
  return {
    tag: n.tagName.toLowerCase(),
    cls: n.className || null,
    attributs: Object.fromEntries(n.attributs),
    ecouteurs: [...n.ecouteurs.keys()].sort(),
    texte: n.childNodes.length ? null : n.textContent,
    enfants: n.childNodes.map(releverNoeud),
  };
}

function aplatir(releve) {
  return [releve].concat((releve.enfants || []).flatMap(aplatir));
}

function boutons(releve) {
  return aplatir(releve).filter((n) => n.tag === "button");
}

function releverLien(n) {
  if (!n) return null;
  return { href: n.href || null, rel: n.rel || null, target: n.target || null,
           texte: n.textContent };
}

/** Les boutons de repli présents dans le document, où qu'ils soient. */
function repliDans(document) {
  return document.querySelectorAll("button")
    .filter((b) => b.textContent === "Consulter le guide en recherche simple").length;
}

function badges(elements) {
  return {
    onglet: { texte: elements["mode-badge"].textContent, cls: elements["mode-badge"].className },
    widget: { texte: elements["widget-badge"].textContent, cls: elements["widget-badge"].className },
  };
}

/**
 * Laisse se régler les promesses du bac à sable. `envoyer()` ne rend rien : il enchaîne la sonde,
 * la requête, la lecture du corps puis l'affichage, tous en microtâches. Le `setTimeout` du bac est
 * un bouchon (les minuteurs d'abandon ne doivent pas tourner ici) : on rend donc la main à la
 * boucle d'événements **de l'hôte**, ce qui draine la file de microtâches du même isolat.
 */
async function respirer(tours = 8) {
  for (let i = 0; i < tours; i++) await new Promise((r) => setImmediate(r));
}

// ---------- les données ----------

const QUESTION = "Quel délai ai-je pour déclarer mon arrivée à la commune ?";

/** Une réponse dont une citation porte du balisage : `textContent` doit le rendre **littéral**. */
function reponseAvecBalisage() {
  const claims = [{
    claim_id: "c1", text: "Le délai est de huit jours.",
    quotes: [{ block_id: "b1", quote: "<script>alert(1)</script> huit jours" }],
    status: { retrouvee: true, pertinente: true, applicable: null, edition: "git:a8e8593" },
  }];
  const segments = [{ text: "Vous avez huit jours.", kind: "factuel", claim_ids: ["c1"] }];
  const answer = {
    found: true, complete: true, texte: segments[0].text, segments, claims,
    rejected_claims: [], reason: null, unknown: [], clarification: null,
  };
  return {
    texte: answer.texte, segments,
    sources: [{ block_id: "b1", fiche_id: "arrivee", titre: "Les huit premiers jours",
                url: "https://guichet.public.lu/arrivee",
                quote: "<script>alert(1)</script> huit jours", status: "verifiee" }],
    fiches: [], unknown: [], comparateur: false, answer, via: "api/v1",
    // `pipeline` est obligatoire au sens de la lecture stricte d'AD-11 (`chat.js::verifierTrace`) :
    // le cas `boucle_complete` fait passer ce corps par `lireReponse`, pas seulement par `vueReponse`.
    trace: { request_id: "r-1", pipeline: "guide", total_cost_eur: 0.0278 },
  };
}

/**
 * Une réponse dont *comprendre* a demandé une précision (AD-5 : `ClarificationRequise`, `found`
 * faux, rien de cherché). `texte` est la phrase générique de `restituer`, `clarification` la
 * question réellement posée à l'utilisateur — celle qui n'entrait nulle part avant la story 2.2.
 */
function reponseAvecClarification() {
  const phrase = "Je n'ai pas pu déterminer à quoi votre question fait référence ; précisez-la " +
    "et je chercherai.";
  const answer = {
    found: false, complete: false, lang: "fr", lang_fallback: false, texte: phrase,
    segments: [{ text: phrase, kind: "limite", claim_ids: [] }],
    claims: [], rejected_claims: [],
    reason: { kind: "clarification_requise", terms_searched: [], variants_count: 0,
              blocks_scanned: 0, documents: [] },
    verdict: null, unknown: [],
    clarification: "De quel document ou démarche parlez-vous ?",
  };
  return {
    texte: phrase, segments: answer.segments, sources: [], fiches: [], unknown: [],
    comparateur: false, answer, via: "api/v1",
    trace: { request_id: "r-2", pipeline: "guide", total_cost_eur: 0.0009 },
  };
}

function erreur(kind, code, extra) {
  return Object.assign({ nom: "ErreurChat", kind, code, statut: 0, retry_after: null,
                         request_id: "" }, extra || {});
}

// ---------- les cas ----------

const cas = {};

async function main() {
  // --- une réponse sourcée devient du DOM, et rien que du texte ------------
  {
    const { window, document, elements } = monter();
    const vue = window.CHAT.vueReponse(reponseAvecBalisage(), QUESTION);
    const bulles = window.UI.peindre(vue);
    cas.reponse = {
      // `peindre()` rend une bulle **par journal** : la conversation est la même des deux côtés.
      bulles: bulles.length,
      journaux: ["chat-log", "widget-log"].map((id) => ({
        id, enfants: elements[id].childNodes.length,
        texte: elements[id].textContent,
      })),
      arbre: releverNoeud(bulles[0]),
      boutons: boutons(releverNoeud(bulles[0])).length,
      // Une réponse servie ne propose **jamais** la recherche simple : le serveur a répondu.
      boutons_de_repli: repliDans(document),
      // Le balisage d'une citation est rendu **littéralement** : c'est la propriété d'AD-15, et le
      // DOM minimal lève sur toute pose d'`innerHTML` non vide, donc arriver ici la démontre.
      citation: bulles[0].querySelector(".cite-q").textContent,
      lien: releverLien(bulles[0].querySelector(".cite-lien")),
      dans_le_document: document.querySelectorAll(".msg").length,
    };
  }

  // --- le badge de mode, dans les deux surfaces ---------------------------
  {
    const { window, elements } = monter();
    const releve = { initial: badges(elements) };
    window.UI.badgeMode("api/v1");
    releve.api = badges(elements);
    window.UI.badgeMode("indisponible");
    releve.indisponible = badges(elements);
    window.UI.badgeMode("local");
    releve.local = badges(elements);
    // L'état avant la sonde, tel que `chat.js` le compose : ni « api », ni « local ».
    releve.avant_sonde = window.CHAT.libelleMode(null);
    cas.badges = releve;
  }

  // --- story 1.10 : le badge posé par `ui.js` porte le niveau de validation --
  //
  // `ui.js` ne le lit pas : il passe à `libelleMode()` ce que `chat.js` a retenu de la sonde. Ce cas
  // est ce qui prouve que le suffixe **atteint les deux surfaces** — le défaut de 1.7 (B6) était
  // précisément un badge composé et jamais posé dans le widget flottant.
  {
    const sante = { ok: true, version: "abc1234", documents_servis: ["lux-guide"],
                    gate_profile: "vertical", gate_cases: 2, gate_countersigned: true,
                    alerts: [], thresholds: {} };
    const reponse = {
      ok: true, status: 200, headers: { get: () => null },
      json: () => Promise.resolve(sante),
    };
    const { window, elements } = monter(() => Promise.resolve(reponse));
    await window.CHAT.testerApi();
    window.UI.badgeMode("api/v1");
    cas.badge_validation = { avec_gate: badges(elements) };

    const sansGate = Object.assign({}, sante,
                                   { gate_profile: null, gate_cases: null, gate_countersigned: null });
    const { window: w2, elements: e2 } = monter(() => Promise.resolve({
      ok: true, status: 200, headers: { get: () => null },
      json: () => Promise.resolve(sansGate),
    }));
    await w2.CHAT.testerApi();
    w2.UI.badgeMode("api/v1");
    cas.badge_validation.sans_gate = badges(e2);

    // Sonde en panne : le badge ne suffixe rien — aucun niveau n'a été lu.
    const { window: w3, elements: e3 } = monter(() => Promise.reject(new TypeError("Failed to fetch")));
    await w3.CHAT.testerApi();
    w3.UI.badgeMode("api/v1");
    cas.badge_validation.sonde_morte = badges(e3);

    // Contresignature due : la réserve doit atteindre **les deux** surfaces, comme le suffixe.
    const nonContresigne = Object.assign({}, sante, { gate_countersigned: false });
    const { window: w4, elements: e4 } = monter(() => Promise.resolve({
      ok: true, status: 200, headers: { get: () => null },
      json: () => Promise.resolve(nonContresigne),
    }));
    await w4.CHAT.testerApi();
    w4.UI.badgeMode("api/v1");
    cas.badge_validation.non_contresigne = badges(e4);
  }

  // --- 503 : exactement un bouton, et le clic seul ouvre le mode local -----
  {
    const { window, document, elements } = monter();
    const vue = window.CHAT.vueErreur(erreur("indisponible", "llm_unavailable"), QUESTION);
    const bulles = window.UI.peindre(vue);
    const releve = releverNoeud(bulles[0]);
    const bouton = bulles[0].querySelector("button");
    const avant = {
      boutons: boutons(releve).length,
      // `type="button"` : sans lui, un `<button>` dans un formulaire soumet la page.
      type: bouton ? bouton.type : null,
      texte: bouton ? bouton.textContent : null,
      ecouteurs: bouton ? [...bouton.ecouteurs.keys()].sort() : [],
      badges: badges(elements),
      msg_dans_le_document: document.querySelectorAll(".msg").length,
      historique: window.UI.historique().length,
    };
    // Le clic **réel** sur le bouton matérialisé : c'est la seule porte vers le moteur lexical.
    bouton.declencher("click");
    cas.indisponible = {
      avant,
      apres: {
        badges: badges(elements),
        // La bulle locale est peinte dans les **deux** journaux.
        locales: document.querySelectorAll(".locale").length,
        texte_local: (document.querySelector(".locale") || { textContent: "" }).textContent,
            // La puce cliquée disparaît partout : le choix a été fait, dans les deux journaux.
        boutons_de_repli_restants: repliDans(document),
        historique: window.UI.historique().map((t) => ({ role: t.role, local: !!t.local })),
      },
    };
  }

  // --- 4xx : aucun bouton, jamais ----------------------------------------
  {
    const { window, document } = monter();
    const vues = {
      invalid_request: erreur("requete", "invalid_request", { statut: 400 }),
      input_too_long: erreur("requete", "input_too_long", { statut: 413 }),
      rate_limited: erreur("requete", "rate_limited", { statut: 429, retry_after: 60 }),
      reponse_illisible: erreur("requete", "reponse_illisible", { statut: 200 }),
    };
    cas.refusees = {};
    for (const [nom, e] of Object.entries(vues)) {
      const bulles = window.UI.peindre(window.CHAT.vueErreur(e, QUESTION));
      cas.refusees[nom] = {
        boutons: boutons(releverNoeud(bulles[0])).length,
        texte: bulles[0].textContent,
      };
    }
    // Aucun bouton peint dans les journaux : ceux du document sont les deux « Envoyer » de la page.
    cas.refusees.boutons_dans_les_journaux =
      ["chat-log", "widget-log"].reduce(
        (n, id) => n + document.querySelector("#" + id).querySelectorAll("button").length, 0);
  }

  // --- l'attente : saisie verrouillée, journaux annoncés occupés ----------
  {
    const { window, elements } = monter();
    window.UI.peindre(window.CHAT.vueAttente());
    window.UI.verrouillerSaisie(true);
    const pendant = {
      desactives: ["chat-input", "widget-input", "chat-send", "widget-send"]
        .map((id) => !!elements[id].disabled),
      busy: ["chat-log", "widget-log"].map((id) => elements[id].getAttribute("aria-busy")),
    };
    window.UI.verrouillerSaisie(false);
    cas.attente = {
      pendant,
      apres: {
        desactives: ["chat-input", "widget-input", "chat-send", "widget-send"]
          .map((id) => !!elements[id].disabled),
        busy: ["chat-log", "widget-log"].map((id) => elements[id].getAttribute("aria-busy")),
      },
      texte: elements["chat-log"].textContent,
    };
  }

  // --- story 2.2 : le tour conservé porte la question posée ---------------
  //
  // Le défaut corrigé vit **ici**, dans le matérialiseur : `chat.js` composait déjà la vue avec sa
  // clarification, mais `afficherReponse` ne poussait que `r.texte` dans l'historique de page. Un
  // test de `chat.js` seul ne l'aurait pas vu — c'est la leçon de la revue 1.7. On exécute donc le
  // vrai `afficherReponse`, puis on relève ce que la page a retenu et ce qui repartirait au serveur.
  {
    const { window, document, elements } = monter();
    const r = reponseAvecClarification();
    const question = "Et celui-là, il faut le faire quand ?";
    window.UI.afficherReponse(question, r);
    const bulle = document.querySelectorAll(".msg.bot")[0];
    cas.tour_clarification = {
      historique: window.UI.historique().map((t) => ({ role: t.role, content: t.content,
                                                       local: !!t.local })),
      // Ce que `chat.js` compose et ce que `ui.js` conserve sont la même chaîne : `ui.js` ne
      // décide pas de ce que l'assistant a dit.
      compose: window.CHAT.tourAssistant(r),
      // La question est bien peinte comme une question, avant la phrase (règle 1.7).
      clarification_peinte: bulle.querySelector(".clarif-q").textContent,
      texte_peint: bulle.querySelector(".seg-txt").textContent,
      badges: badges(elements),
      // Et le tour suivant : l'arrivant répond en trois mots, la question posée part au serveur.
      envoye: window.CHAT.historiquePourApi(
        window.UI.historique().concat([{ role: "user", content: "du permis de conduire" }]),
        "du permis de conduire"),
    };
  }

  // --- story 2.2 : une réponse ordinaire conserve son texte, inchangé -----
  {
    const { window } = monter();
    const r = reponseAvecBalisage();
    window.UI.afficherReponse(QUESTION, r);
    cas.tour_ordinaire = {
      historique: window.UI.historique().map((t) => t.content),
      texte: r.texte,
    };
  }

  // --- story 2.2 : la boucle entière, par `envoyer()` (revue P4) ----------
  //
  // Les deux cas ci-dessus appellent `afficherReponse` en direct : leur historique commence donc
  // par un tour `assistant`, ce qui n'arrive jamais dans la page — c'est `envoyer()` qui pousse
  // d'abord le tour `user`. On exerce ici le chemin **entier**, deux tours d'affilée, avec un
  // double de `fetch` : la clarification d'abord, une réponse ordinaire ensuite. Ce qui est relevé
  // est ce que le serveur aurait reçu, corps par corps.
  {
    const corps = [reponseAvecClarification(), reponseAvecBalisage()];
    const postes = [];
    let rang = 0;
    const sante = { ok: true, version: "abc1234", documents_servis: ["lux-guide"],
                    gate_profile: null, gate_cases: null, gate_countersigned: null,
                    alerts: [], thresholds: {} };
    const repondre = (charge) => Promise.resolve({
      ok: true, status: 200, headers: { get: () => null }, json: () => Promise.resolve(charge) });
    const { window, elements } = monter((url, options) => {
      if (String(url).endsWith("/sante")) return repondre(sante);
      postes.push(JSON.parse(options.body));
      return repondre(corps[Math.min(rang++, corps.length - 1)]);
    });

    window.UI.envoyer("Et celui-là, il faut le faire quand ?");
    await respirer();
    window.UI.envoyer("du permis de conduire");
    await respirer();

    cas.boucle_complete = {
      corps_postes: postes.map((c) => ({ question: c.question, historique: c.historique })),
      historique_final: window.UI.historique().map((t) => ({ role: t.role, content: t.content,
                                                             local: !!t.local })),
      badges: badges(elements),
    };
  }

  // --- rien de la conversation n'atteint le navigateur --------------------
  {
    const { window, localStorage } = monter();
    window.UI.peindre(window.CHAT.vueReponse(reponseAvecBalisage(), QUESTION));
    window.UI.badgeMode("api/v1");
    cas.stockage = localStorage.entrees();
  }

  process.stdout.write(JSON.stringify({ ok: true, cas }, null, 1));
}

main().catch((e) => {
  process.stdout.write(JSON.stringify({ ok: false, erreur: String((e && e.stack) || e) }, null, 1));
  process.exitCode = 1;
});
