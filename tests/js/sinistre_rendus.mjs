// Rendu de la page sinistre sur **trois corps figés** (story 5.6, L2).
//
// `sinistre_cases.mjs` éprouve des fixtures minimales, taillées pour un point de contrat à la fois.
// Celui-ci fait l'inverse : il prend trois réponses complètes — un cas couvert, un cas sous
// conditions, un cas où aucune clause ne s'applique —, construites sur les blocs réels du contrat
// AXA (`tests/data/axa/*.txt`), et relève **ce que chacun des quatre blocs montre**.
//
// C'est la preuve que la refonte tient sur des réponses entières, et c'est aussi ce qui rend
// descriptible, dans une passation, ce qu'un lecteur voit sans avoir à ouvrir un navigateur.
//
// Usage : `node tests/js/sinistre_rendus.mjs`. Comme les autres harnais, il ne juge rien : il
// relève, Python asserte (`tests/test_web_sinistre.py`).

import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

import { Document } from "./dom_minimal.mjs";

const ICI = path.dirname(fileURLToPath(import.meta.url));
const RACINE = path.resolve(ICI, "..", "..");
const CORPS = path.join(RACINE, "tests", "data", "front");

const ELEMENTS = ["formulaire", "contrat", "contrats-message", "contrat-source", "documents-audit",
                  "description", "analyser", "resultat"];

/** Charge `sinistre.js` sans démarrage : on ne peint que ce qu'on lui donne. */
function charger() {
  const document = new Document();
  document.readyState = "complete";
  const elements = {};
  for (const id of ELEMENTS) {
    const e = document.createElement("div");
    e.id = id;
    document.body.appendChild(e);
    elements[id] = e;
  }
  const window = {
    location: new URL("https://foyer-retour.example/sinistre/"), document,
    addEventListener: () => {}, __SINISTRE_SANS_DEMARRAGE: true,
    fetch: () => Promise.reject(new Error("aucun appel réseau dans ce harnais")),
  };
  const journal = new console.Console(process.stderr, process.stderr);
  const bac = {
    window, document, console: journal, URL, setTimeout, clearTimeout,
    setInterval: () => 0, clearInterval: () => {},
    JSON, Math, Date, Number, String, Array, Object, isFinite, parseInt, Error, Promise, RegExp,
    AbortController, TextDecoder, TextEncoder,
    fetch: window.fetch,
  };
  bac.globalThis = bac;
  vm.createContext(bac);
  vm.runInContext(readFileSync(path.join(RACINE, "tools/sinistre/sinistre.js"), "utf8"), bac,
                  { filename: "tools/sinistre/sinistre.js" });
  return { SINISTRE: window.SINISTRE, elements };
}

/** Le texte d'un élément peint, en une ligne lisible. */
function ligne(n) {
  return n.textContent.replace(/\s+/g, " ").trim();
}

function bloc(peint, classe) {
  return peint.querySelectorAll("." + classe)[0] || null;
}

function releverBlocs(peint) {
  const reponse = bloc(peint, "bloc-reponse");
  const appuis = bloc(peint, "bloc-appuis");
  const manques = bloc(peint, "bloc-manques");
  const gardefous = bloc(peint, "bloc-gardefous");
  return {
    // Bloc 1 — la réponse : la phrase d'ouverture, l'explication, le verdict, l'inconnue.
    bloc1: reponse && {
      phrase: ligne(peint.querySelectorAll(".reponse-phrase")[0] || { textContent: "" }),
      explications: peint.querySelectorAll(".reponse-suite").map(ligne),
      verdict: ligne(peint.querySelectorAll(".badge")[0] || { textContent: "" }),
      verdict_cls: (peint.querySelectorAll(".badge")[0] || { className: "" }).className,
      raison: peint.querySelectorAll(".verdict-raison").map(ligne),
      inconnu: peint.querySelectorAll(".inconnu-ligne").map(ligne),
      // Story 5.6 (L2e) — ce qui a été examiné puis écarté : l'intitulé, et chaque phrase avec sa
      // raison courte. Le rang du bloc dans le bloc 1 dit qu'il vient bien **après** le corps.
      // Le bandeau de recalcul : ce que l'assuré a apporté, tel qu'il se lit.
      maj_tete: peint.querySelectorAll(".maj-tete").map(ligne),
      maj_faits: peint.querySelectorAll(".maj-fait-val").map(ligne),
      ecartees_tete: peint.querySelectorAll(".ecartees-tete").map(ligne),
      ecartees: peint.querySelectorAll(".ecartee").map((li) => [
        ligne(li.querySelectorAll(".ecartee-raison")[0] || { textContent: "" }),
        ligne(li.querySelectorAll(".ecartee-txt")[0] || { textContent: "" }),
      ]),
      apres_le_corps: (() => {
        const enfants = reponse.childNodes.filter((n) => !n.estTexte);
        const rang = enfants.findIndex((n) => n.className.indexOf("reponse-ecartees") !== -1);
        if (rang < 0) return null;
        return enfants.slice(0, rang).filter(
          (n) => n.className.indexOf("reponse-suite") !== -1).length;
      })(),
      // Aucune phrase du corps n'est écrite deux fois, à la lettre près.
      corps_sans_doublon: (() => {
        const dits = peint.querySelectorAll(".reponse-phrase").concat(
          peint.querySelectorAll(".reponse-suite")).flatMap(
            (n) => ligne(n).split(/(?<=\.)\s+(?=[A-ZÀ-Þ])/));
        return dits.length === new Set(dits).size;
      })(),
    },
    // Bloc 2 — les clauses : chemin, paragraphe, partie surlignée, phrase en clair, retrait.
    bloc2: appuis && appuis.querySelectorAll(".appui").map((carte) => ({
      ecartee: carte.className.indexOf("appui-ecarte") !== -1,
      chemin: ligne(carte.querySelectorAll(".appui-chemin")[0] || { textContent: "" }),
      page: ligne(carte.querySelectorAll(".appui-page")[0] || { textContent: "" }),
      type: ligne(carte.querySelectorAll(".appui-kind")[0] || { textContent: "" }),
      // L'amorce fondue (L2d) : la phrase qui appelle l'énumération, au-dessus du paragraphe.
      amorce: ligne(carte.querySelectorAll(".appui-amorce")[0] || { textContent: "" }),
      paragraphe: ligne(carte.querySelectorAll(".appui-texte")[0] || { textContent: "" }),
      // Un bloc long : l'extrait porte sa marque, et le paragraphe entier — posé masqué — porte
      // la même. Les deux se relèvent, et le bouton dit lequel est visible.
      surligne: carte.querySelectorAll(".appui-mark").map(ligne),
      bouton_entier: carte.querySelectorAll(".appui-plus").map(ligne),
      paragraphe_entier_masque: carte.querySelectorAll(".appui-entier")
        .map((n) => n.getAttribute("hidden")),
      en_clair: ligne(carte.querySelectorAll(".appui-clair")[0] || { textContent: "" }),
      ouvre_le_pdf: carte.querySelectorAll(".cl-ouvrir").map(ligne),
      // Ce que le bouton ouvre : une carte qui a fondu son amorce ouvre les **deux** blocs.
      blocs_ouverts: carte.querySelectorAll(".cl-ouvrir").map(
        (b) => JSON.parse(b.getAttribute("data-block-ids") || "[]")),
    })),
    // Bloc 3 — ce qui manque : questions, faits exigés, pièces non lues.
    bloc3: manques && {
      questions: manques.querySelectorAll(".conv-selection-question").map(ligne),
      // Story 5.6 (L2f) — la zone de réponse commune : les trois boutons, masqués ou non, et
      // l'exemple que le champ libre propose. Ce que la question sélectionnée attend.
      boutons: manques.querySelectorAll(".conv-repondre").map(ligne),
      boutons_masques: manques.querySelectorAll(".conv-reponses").map(
        (n) => n.getAttribute("hidden")),
      placeholder: manques.querySelectorAll(".conv-reponse-libre").map(
        (n) => n.getAttribute("placeholder")),
      // Ce que chaque question demande, dans l'ordre où elles sont posées : « » pour un oui/non.
      attendus: manques.querySelectorAll(".conv-selection-question").map(
        (n) => n.getAttribute("data-placeholder") || ""),
      demandes: manques.querySelectorAll(".ask-liste").flatMap(
        (u) => u.querySelectorAll("li").map(ligne)),
      faits_exiges: manques.querySelectorAll(".paquet-faits").flatMap(
        (u) => u.querySelectorAll("li").map(ligne)),
      escalade: manques.querySelectorAll(".escalate-liste").flatMap(
        (u) => u.querySelectorAll("li").map(ligne)),
      pieces: manques.querySelectorAll(".pieces-ligne").map(ligne),
    },
    // Bloc 4 — les garde-fous, et ce qui reste derrière eux.
    bloc4: gardefous && {
      lignes: gardefous.querySelectorAll(".gf").map(ligne),
      preuve: gardefous.querySelectorAll(".preuve").map(ligne),
      etat: gardefous.querySelectorAll(".etat").map(ligne),
      ecartees: gardefous.querySelectorAll(".rejetee").map(ligne),
      ecartees_repliees: gardefous.querySelectorAll(".rejetees").map((d) => d.tagName.toLowerCase()),
    },
    // L'ordre des blocs, et le fait que rien ne soit replié avant les garde-fous.
    ordre: peint.childNodes.filter((n) => !n.estTexte).map((n) => n.className),
    replies_avant_les_gardefous: (() => {
      const enfants = peint.childNodes.filter((n) => !n.estTexte);
      const rang = enfants.findIndex((n) => n.className.indexOf("bloc-gardefous") !== -1);
      return enfants.slice(0, rang).filter((n) => n.tagName.toLowerCase() === "details").length;
    })(),
  };
}

function main() {
  const rendus = {};
  for (const fichier of readdirSync(CORPS).filter((f) => f.endsWith(".json")).sort()) {
    const corps = JSON.parse(readFileSync(path.join(CORPS, fichier), "utf8"));
    const { SINISTRE, elements } = charger();
    // Le corps passe par la **lecture stricte** d'AD-11 avant d'être peint : un corps figé qui ne
    // tiendrait pas le contrat serait une preuve sans valeur.
    // Un corps de **suivi** porte un dossier : il passe par la lecture qui le lit, sans quoi le
    // tour 1 se peindrait comme un tour 0 et ne prouverait rien de ce que le tour ajoute.
    const lire = corps.conversation ? SINISTRE.lireReponseConversation : SINISTRE.lireReponse;
    const lu = lire ? lire(corps) : corps;
    const peint = SINISTRE.peindre(
      SINISTRE.vueVerdict(lu, { doc_id: "axa", source_url: "https://example.invalid/axa.pdf" }),
      elements.resultat);
    SINISTRE.brancherAppuis(peint);
    rendus[fichier.replace(/\.json$/, "")] = releverBlocs(peint);
  }
  return rendus;
}

try {
  process.stdout.write(JSON.stringify({ ok: true, rendus: main() }, null, 1));
} catch (e) {
  process.stdout.write(JSON.stringify({ ok: false, erreur: String((e && e.stack) || e) }, null, 1));
  process.exitCode = 1;
}
