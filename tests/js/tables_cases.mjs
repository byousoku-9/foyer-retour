// Harnais Node des **tables écrites deux fois** (story 2.5) — aucun navigateur, aucun réseau,
// aucune dépendance ajoutée à `pyproject.toml`.
//
// Trois règles vivent en double par décision antérieure (D8 : les pages `tools/` sont autonomes,
// elles n'importent rien de `web/app/` et n'empruntent pas sa feuille de style) :
//
//   1. les **phrases d'alerte** du serveur — `web/app/chat.js` et `tools/accueil/accueil.js` ;
//   2. les **phrases d'état** (sûr / partiel / inconnu) et la preuve d'absence chiffrée —
//      `web/app/chat.js` et `tools/sinistre/sinistre.js` ;
//   3. la **règle de profil** — `web/app/ui.js::etapeConcerne` (ce que le site garde à l'écran) et
//      `server/app/domain/profil.py::noeuds_du_profil` (ce que le serveur promeut dans la
//      recherche), qui lisent le même `si` de la même source.
//
// Aucune ne peut être factorisée sans casser l'autonomie des pages — mais toutes peuvent être
// **rejouées côte à côte sous test**. Ce fichier monte chaque module dans un contexte neuf, relève
// ses tables telles quelles, et rejoue `etapeConcerne` sur `tests/data/profil_cas.json`. Il ne juge
// rien : `tests/test_tables_partagees.py` asserte, et compare la troisième colonne au verdict que
// Python calcule de son côté.

import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

import { Document, stockage } from "./dom_minimal.mjs";

const ICI = path.dirname(fileURLToPath(import.meta.url));
const RACINE = path.resolve(ICI, "..", "..");

/** Un bac à sable minimal : ce que ces modules touchent au chargement, et rien de plus. */
function bacASable(href, extra) {
  const document = new Document();
  document.readyState = "complete";
  const localStorage = stockage();
  // Le JSON du harnais sort sur **stdout** : un `console.log` oublié le corromprait. Le `console`
  // du bac écrit donc sur stderr.
  const journal = new console.Console(process.stderr, process.stderr);
  const refuser = () => { throw new Error("aucun appel réseau n'est attendu dans ce harnais"); };
  const window = Object.assign({
    location: new URL(href), document, localStorage, fetch: refuser,
    addEventListener: () => {},
    matchMedia: () => ({ matches: false, addEventListener: () => {} }),
    innerWidth: 800,
  }, extra || {});
  const bac = {
    window, document, localStorage, console: journal, URL, fetch: refuser,
    setTimeout: () => 0, clearTimeout: () => {}, AbortController,
    JSON, Math, Date, Number, String, Array, Object, isFinite, parseInt, Error, Promise, RegExp,
  };
  bac.globalThis = bac;
  vm.createContext(bac);
  return { bac, window, document };
}

/** Monte les fichiers demandés dans un contexte neuf et rend son `window`. */
function monter(href, fichiers, extra) {
  const { bac, window } = bacASable(href, extra);
  for (const fichier of fichiers) {
    vm.runInContext(readFileSync(path.join(RACINE, fichier), "utf8"), bac, { filename: fichier });
  }
  return window;
}

const cas = {};

// --- les tables, relevées telles quelles ---------------------------------

// `web/app/chat.js` a besoin de `kb.js` (le moteur lexical y puise ses fiches).
const guide = monter("https://foyer-retour.example/guide/",
                     ["web/app/kb.js", "web/app/chat.js"]);
guide.SIM = { comparatif: () => [] };
guide.CONTRATS_KB = { contrats: [] };

const accueil = monter("https://foyer-retour.example/",
                       ["tools/accueil/accueil.js"], { __ACCUEIL_SANS_DEMARRAGE: true });

const sinistre = monter("https://foyer-retour.example/sinistre/",
                        ["tools/sinistre/sinistre.js"], { __SINISTRE_SANS_DEMARRAGE: true });

// `ui.js` a besoin de `chat.js` : il l'appelle pour composer. Monté à part, sans démarrage.
const site = monter("https://foyer-retour.example/guide/#assistant",
                    ["web/app/kb.js", "web/app/chat.js", "web/app/ui.js"],
                    { __UI_SANS_DEMARRAGE: true });
site.SIM = { comparatif: () => [], calcul: () => ({}) };
site.CONTRATS_KB = { contrats: [], scenarios: [] };

cas.alertes = {
  chat: guide.CHAT.ALERTES,
  accueil: accueil.ACCUEIL.ALERTES,
  sinistre: sinistre.SINISTRE.ALERTES,
};

cas.controles = {
  chat: guide.CHAT.CONTROLES,
  sinistre: sinistre.SINISTRE.CONTROLES,
};

// Les phrases d'état, jouées sur les **mêmes** entrées des deux côtés. Un grep sur deux sources ne
// vérifierait pas une sémantique : on appelle les deux fonctions et on compare leurs sorties.
const ENTREES_ETAT = [
  { cle: "sur", contexte: { liste: false, preuve: false } },
  { cle: "sur", contexte: { liste: true, preuve: true } },
  { cle: "partiel", contexte: { liste: true, preuve: false } },
  { cle: "partiel", contexte: { liste: false, preuve: false } },
  { cle: "inconnu", contexte: { liste: false, preuve: true } },
  { cle: "inconnu", contexte: { liste: false, preuve: false } },
  { cle: "farfelu", contexte: { liste: false, preuve: true } },
  { cle: null, contexte: null },
];

cas.phrases_etat = ENTREES_ETAT.map((e) => ({
  entree: e,
  chat: guide.CHAT.phraseEtat(e.cle === null ? null : { cle: e.cle }, e.contexte),
  sinistre: sinistre.SINISTRE.phraseEtat(e.cle === null ? null : { cle: e.cle }, e.contexte),
}));

const ETATS = [
  { found: true, complete: true },
  { found: true, complete: false },
  { found: false, complete: false },
  null,
];
cas.etats = ETATS.map((a) => ({
  entree: a,
  chat: guide.CHAT.etatReponse(a),
  sinistre: sinistre.SINISTRE.etatReponse(a),
}));

const PREUVES = [
  { kind: "zero_hit", terms_searched: ["bail"], variants_count: 1, blocks_scanned: 1 },
  { kind: "zero_hit", terms_searched: ["mobilier"], variants_count: 0, blocks_scanned: 1457 },
  { kind: "hors_perimetre", terms_searched: [], variants_count: 0, blocks_scanned: 0 },
  { kind: "claims_rejetes", terms_searched: ["bail", "préavis"], variants_count: 3,
    blocks_scanned: 506 },
  { kind: "clarification_requise", terms_searched: [], variants_count: 0, blocks_scanned: 0 },
  null,
];
cas.preuves = PREUVES.map((r) => ({
  entree: r,
  chat: guide.CHAT.preuveAbsence(r),
  sinistre: sinistre.SINISTRE.preuveAbsence(r),
}));

// Un nom de contrôle inconnu se dit tel quel des deux côtés : jamais masqué, jamais traduit.
cas.controle_inconnu = {
  chat: guide.CHAT.libelleControle("controle_de_demain"),
  sinistre: sinistre.SINISTRE.libelleControle("controle_de_demain"),
};

// --- la règle de profil, rejouée sur la table de cas ----------------------

const table = JSON.parse(readFileSync(path.join(RACINE, "tests/data/profil_cas.json"), "utf8"));
cas.profil = table.cas.map((c) => ({
  nom: c.nom,
  // `etapeConcerne(item, profil)` : l'item est une étape de la frise, qui porte son `si`.
  site_garde: site.UI.etapeConcerne({ t: "une étape de la frise", si: c.si }, c.profil),
}));

// Les bords que `etapeConcerne` traite **avant** de regarder `si` : une étape sans condition, une
// étape écrite comme une simple chaîne, et rien du tout. Toutes restent à l'écran.
cas.profil_bords = {
  sans_si: site.UI.etapeConcerne({ t: "une étape sans condition" }, {}),
  chaine: site.UI.etapeConcerne("une étape écrite comme une chaîne", {}),
  nul: site.UI.etapeConcerne(null, {}),
  si_vide: site.UI.etapeConcerne({ t: "x", si: {} }, {}),
};

// Les neuf conditions du parcours livré, telles que la table les déclare : le test Python les
// confronte à `data/lux-guide/document.json`, pour qu'une source qui change fasse rougir la table.
cas.conditions = table.conditions_du_parcours_livre;

process.stdout.write(JSON.stringify({ ok: true, node: process.version, cas }, null, 1));
