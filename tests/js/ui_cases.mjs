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
  // Story 5.6 (L2) : les deux conteneurs que `montrerFiche()` touche. Le reste de la page (les
  // onglets, la carte, la frise) n'est pas monté : ce qu'on vérifie ici est le corps de la fiche.
  { tag: "div", id: "fiche-detail" },
  { tag: "div", id: "fiches-liste" },
];

/**
 * Story 5.6 (L2) : la route de progression n'existe pas encore côté serveur. Le défaut du harnais
 * est donc **404**, l'état réel du service — les cas qui l'exercent fournissent leur propre double.
 */
function sansProgression(fetchDouble) {
  if (!fetchDouble) return fetchDouble;
  return (url, options) => {
    if (String(url).endsWith("/progression")) {
      return Promise.resolve({
        ok: false, status: 404,
        headers: { get: () => null },
        json: () => Promise.resolve({ error: { code: "not_found" } }),
        text: () => Promise.resolve(""),
      });
    }
    return fetchDouble(url, options);
  };
}

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
    scrollTo: () => {},
    // Aucun appel réseau n'est déclenché : le démarrage (donc la sonde) est sauté, et la seule
    // action exercée est la recherche simple, qui est purement locale. Un `fetch` appelé quand
    // même doit faire échouer le harnais, pas passer inaperçu.
    fetch: sansProgression(fetchDouble) ||
      (() => { throw new Error("aucun appel réseau n'est attendu dans ce harnais"); }),
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
    fetch: sansProgression(fetchDouble) ||
      (() => { throw new Error("aucun appel réseau n'est attendu dans ce harnais"); }),
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
    // Le titre porte du balisage : depuis la story 5.6 (L2), c'est **lui** que la puce affiche,
    // et c'est donc lui qui doit arriver littéralement dans le DOM (AD-15).
    // L'URL est **précise** (deux segments parlants) : la puce « source officielle » l'affiche.
    sources: [{ block_id: "b1", fiche_id: "arrivee",
                titre: "<script>alert(1)</script> Les huit premiers jours",
                url: "https://guichet.public.lu/fr/citoyens/citoyennete.html",
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
      puce: bulles[0].querySelector(".cite-puce").textContent,
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

  // --- story 5.6 (L2) : l'attente se rafraîchit **en place** ---------------
  {
    const { window, elements } = monter();
    const debut = 1000;
    window.UI.peindre(window.CHAT.vueAttente(window.CHAT.etatAttente(debut, debut, null)));
    const bulle = elements["chat-log"].querySelectorAll(".msg.bot.attente")[0];
    const premiere = bulle.querySelectorAll(".prog-etape")[0];
    // Un repère que seule une repeinture ferait disparaître : s'il survit, aucun `replaceChild`
    // n'a eu lieu — donc aucune relecture de la barre par un lecteur d'écran.
    premiere.setAttribute("data-repere", "1");
    // Ce que `chat.js` décide, tel que `ui.js` l'écrira : la fonction pure, d'abord.
    const decide = window.CHAT.majAttente(
      window.CHAT.etatAttente(debut, debut + 26000, null),
      bulle.querySelectorAll(".prog-etape").length);
    // Puis le chemin réel, par la boucle d'envoi (un `fetch` qui ne répond jamais).
    cas.attente_en_place = {
      decide,
      structure_changee: window.CHAT.majAttente(
        window.CHAT.etatAttente(debut, debut, { rang: 0, libelles: ["A", "B"] }),
        bulle.querySelectorAll(".prog-etape").length),
      repere: premiere.getAttribute("data-repere"),
      bulles: elements["chat-log"].querySelectorAll(".msg.bot.attente").length,
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

  // --- story 2.5 (M9) : une question sans réponse quitte l'historique ------
  //
  // `envoyer()` pousse le tour `user` **avant** l'appel, et `afficherErreur()` ne poussait rien :
  // après un 503, la question suivante partait avec deux tours `user` à la file, dont l'un n'avait
  // jamais reçu de réponse. On exerce donc le chemin **entier** — `envoyer()` puis l'échec, puis
  // une seconde question qui réussit — et on relève ce que le serveur aurait reçu, corps par corps.
  {
    const sante = { ok: true, version: "abc1234", documents_servis: ["lux-guide"],
                    gate_profile: null, gate_cases: null, gate_countersigned: null,
                    alerts: [], thresholds: {} };
    const postes = [];
    let rang = 0;
    const { window, document, elements } = monter((url, options) => {
      if (String(url).endsWith("/sante")) {
        return Promise.resolve({ ok: true, status: 200, headers: { get: () => null },
                                 json: () => Promise.resolve(sante) });
      }
      postes.push(JSON.parse(options.body));
      rang += 1;
      if (rang === 1) {
        return Promise.resolve({
          ok: false, status: 503, headers: { get: () => null },
          json: () => Promise.resolve({ error: { code: "llm_unavailable", message: "down",
                                                 request_id: "req-503" } }),
        });
      }
      return Promise.resolve({ ok: true, status: 200, headers: { get: () => null },
                               json: () => Promise.resolve(reponseAvecBalisage()) });
    });

    window.UI.envoyer("Quel délai pour déclarer mon arrivée ?");
    await respirer();
    const apresEchec = {
      historique: window.UI.historique().map((t) => ({ role: t.role, content: t.content })),
      // Le retrait est **dit** : la phrase est composée par `chat.js`, posée par le matérialiseur.
      retrait_peint: (document.querySelector(".retrait") || {}).textContent || null,
      bandeau: (document.querySelector(".msg.bot.indispo") || {}).textContent || null,
      boutons_de_repli: repliDans(document),
      badges: badges(elements),
    };
    window.UI.envoyer("Et pour l'école ?");
    await respirer();
    cas.tour_sans_reponse = {
      apres_echec: apresEchec,
      corps_postes: postes.map((c) => ({ question: c.question, historique: c.historique })),
      historique_final: window.UI.historique().map((t) => ({ role: t.role, content: t.content })),
    };
  }

  // --- story 2.5 (M4 / M8) : le bouton unique, et le clic qui ouvre le local ---
  //
  // Le cas `indisponible` plus haut peint `vueErreur` à la main ; celui-ci passe par `envoyer()`,
  // donc par le vrai chemin de la page : la question est poussée, l'appel échoue, le bandeau est
  // peint dans les **deux** journaux, et le clic — le seul — fait tourner le moteur lexical.
  {
    const sante = { ok: true, version: "abc1234", documents_servis: ["lux-guide"],
                    gate_profile: null, gate_cases: null, gate_countersigned: null,
                    alerts: [], thresholds: {} };
    const { window, document, elements } = monter((url) => {
      if (String(url).endsWith("/sante")) {
        return Promise.resolve({ ok: true, status: 200, headers: { get: () => null },
                                 json: () => Promise.resolve(sante) });
      }
      return Promise.resolve({
        ok: false, status: 503, headers: { get: () => null },
        json: () => Promise.resolve({ error: { code: "llm_unavailable", message: "down",
                                               request_id: "req-503" } }),
      });
    });
    window.UI.envoyer("Quel délai pour déclarer mon arrivée ?");
    await respirer();
    const avant = {
      bandeaux: document.querySelectorAll(".indispo").length,
      boutons_de_repli: repliDans(document),
      locales: document.querySelectorAll(".locale").length,
      badges: badges(elements),
      historique: window.UI.historique().length,
    };
    document.querySelectorAll("button")
      .filter((b) => b.textContent === "Consulter le guide en recherche simple")[0]
      .declencher("click");
    cas.repli_par_envoyer = {
      avant,
      apres: {
        locales: document.querySelectorAll(".locale").length,
        boutons_de_repli_restants: repliDans(document),
        badges: badges(elements),
        historique: window.UI.historique().map((t) => ({ role: t.role, local: !!t.local })),
        // `via: "local"` est visible : le badge le dit, dans les deux surfaces.
        texte_local: (document.querySelector(".locale") || { textContent: "" }).textContent,
      },
    };
  }

  // --- story 2.5 (M5) : une panne réseau ouvre la même porte, et pas une autre ---
  {
    // `fetch` **rejette** sur une panne réseau, il ne lève pas : un double qui lèverait
    // synchroniquement testerait un chemin que le navigateur n'emprunte jamais.
    const { window, document } = monter(() => Promise.reject(new TypeError("Failed to fetch")));
    window.UI.envoyer("Quel délai pour déclarer mon arrivée ?");
    await respirer();
    cas.panne_reseau = {
      bandeaux: document.querySelectorAll(".indispo").length,
      boutons_de_repli: repliDans(document),
      retrait_peint: (document.querySelector(".retrait") || {}).textContent || null,
      historique: window.UI.historique().map((t) => ({ role: t.role, content: t.content })),
    };
  }

  // --- story 2.5 (M6 / M7) : un 429 et un 400 ne retirent rien de plus, et n'ouvrent rien ---
  {
    const sante = { ok: true, version: "abc1234", documents_servis: ["lux-guide"],
                    gate_profile: "vertical", gate_cases: 2, gate_countersigned: true,
                    alerts: [], thresholds: {} };
    for (const [nom, statut, code, entete] of [["limite", 429, "rate_limited", "30"],
                                               ["refusee", 400, "invalid_request", null]]) {
      const { window, document, elements } = monter((url) => {
        if (String(url).endsWith("/sante")) {
          return Promise.resolve({ ok: true, status: 200, headers: { get: () => null },
                                   json: () => Promise.resolve(sante) });
        }
        return Promise.resolve({
          ok: false, status: statut,
          headers: { get: (n) => (n.toLowerCase() === "retry-after" ? entete : null) },
          json: () => Promise.resolve({ error: { code, message: "peu importe",
                                                 request_id: "req-" + statut } }),
        });
      });
      await window.CHAT.testerApi();
      window.UI.badgeMode("api/v1");
      const badgeAvant = badges(elements);
      window.UI.envoyer("Quel délai pour déclarer mon arrivée ?");
      await respirer();
      cas["erreur_" + nom] = {
        badge_avant: badgeAvant,
        // Le badge `mode api` est **conservé** : le serveur a répondu, il a refusé la requête.
        badge_apres: badges(elements),
        boutons_de_repli: repliDans(document),
        boutons_dans_les_journaux: ["chat-log", "widget-log"].reduce(
          (n, id) => n + document.querySelector("#" + id).querySelectorAll("button").length, 0),
        texte: (document.querySelector(".msg.bot.err") || { textContent: "" }).textContent,
        historique: window.UI.historique().map((t) => ({ role: t.role, content: t.content })),
      };
    }
  }

  // --- story 2.5 (M1) : le panneau est peint dans les **deux** journaux -----
  {
    const { window, document } = monter();
    const r = reponseAvecBalisage();
    r.trace = {
      request_id: "r-1", pipeline: "guide", variant: "deterministe", total_cost_eur: 0.0278,
      retries: 0, truncations: 0, thresholds: { max_opens: 8 },
      steps: [{ name: "retrouver", tier: "reason", ms: 3480,
                opened_block_ids: ["b1"], discarded_block_ids: [],
                checks: [{ name: "citations", ok: true, detail: "1 affirmation(s) retenue(s)" }] }],
      blocs: [{ block_id: "b1", doc_id: "lux-guide", node_id: "lux-guide:farrivee",
                fiche_id: "arrivee", titre: "Les huit premiers jours" }],
      gate: { profile: "vertical", cases: 2, countersigned: false, alerts: [] },
      dictionnaire: { charge: true, validated: false, corpus_ok: true, court_circuit_actif: false },
    };
    const bulles = window.UI.peindre(window.CHAT.vueReponse(r, QUESTION));
    const panneaux = document.querySelectorAll(".pourquoi");
    cas.panneau = {
      // Un `<details>` par journal : la conversation est la même des deux côtés.
      panneaux: panneaux.length,
      details_dans_la_bulle: bulles[0].querySelectorAll("details").length,
      summary: (bulles[0].querySelector("summary") || {}).textContent,
      // Replié par défaut : aucun `open` n'est posé.
      attribut_open: panneaux.map((p) => p.getAttribute("open")),
      // `aria-hidden` est posé par `setAttribute` depuis la description : c'est la seule voie.
      pictos: bulles[0].querySelectorAll(".pq-ok").concat(bulles[0].querySelectorAll(".pq-ko"))
        .map((n) => ({ texte: n.textContent, aria: n.getAttribute("aria-hidden") })),
      // Aucun `id` dans un arbre peint deux fois.
      ids: document.querySelectorAll(".pourquoi")
        .flatMap((p) => p.querySelectorAll("li").concat(p.querySelectorAll("span")))
        .filter((n) => n.getAttribute("id")).length,
      texte: bulles[0].querySelector(".pourquoi").textContent,
    };
  }

  // --- story 5.6 (L2) : la fiche s'ouvre sur la phrase citée, surlignée ----
  {
    const { window, elements } = monter();
    const fiche = window.KB.fiches.filter((f) => f.id === "arrivee")[0];
    // Le passage tel que le corpus le sert : une sous-chaîne d'un paragraphe du corps de la fiche.
    const passage = "vous disposez de huit jours pour déclarer votre arrivée";
    let defile = null;
    const marques = () => elements["fiche-detail"].querySelectorAll(".passage-cite");
    window.UI.montrerFiche("arrivee", true, passage);
    let marque = marques()[0];
    if (marque) { marque.scrollIntoView = (o) => { defile = o; }; }
    // `scrollIntoView` est appelé **pendant** `montrerFiche` : on rejoue avec le double en place.
    elements["fiche-detail"].textContent = "";
    const original = window.document.createElement;
    window.document.createElement = (tag) => {
      const n = original.call(window.document, tag);
      if (String(tag).toLowerCase() === "mark") n.scrollIntoView = (o) => { defile = o; };
      return n;
    };
    window.UI.montrerFiche("arrivee", true, passage);
    window.document.createElement = original;
    marque = marques()[0];
    cas.fiche_passage = {
      titre: fiche.titre,
      marques: marques().length,
      texte_marque: marque ? marque.textContent : null,
      defile,
      // Le paragraphe qui porte la marque est bien celui du corps, rendu entier.
      paragraphe: marque ? marque.parentElement.textContent : null,
    };

    // Un passage que la fiche ne contient pas : aucune marque, aucune erreur, la fiche s'ouvre.
    const b = monter();
    b.window.UI.montrerFiche("arrivee", true, "une phrase absente de cette fiche");
    cas.fiche_passage_introuvable = {
      marques: b.elements["fiche-detail"].querySelectorAll(".passage-cite").length,
      rendue: b.elements["fiche-detail"].querySelectorAll("p").length > 0,
      cachee: !!b.elements["fiche-detail"].hidden,
    };

    // Sans passage du tout : le comportement d'avant, inchangé.
    const c = monter();
    c.window.UI.montrerFiche("arrivee", true);
    cas.fiche_sans_passage = {
      marques: c.elements["fiche-detail"].querySelectorAll(".passage-cite").length,
      rendue: c.elements["fiche-detail"].querySelectorAll("p").length > 0,
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
