// Compose la vue des résultats de `/` à partir d'un corps lu sur **stdin**, et écrit sur stdout le
// JSON de ce qui a été observé.
//
// Pourquoi ce harnais existe séparément d'`accueil_cases.mjs` : l'AC 4 compare quatre surfaces « à
// l'octet des chiffres près », et la quatrième est *la composition de `/`*. La prouver avec un corps
// fabriqué en JavaScript ne prouverait rien — il faut le corps que `GET /api/v1/evals/latest` rend
// **réellement**, produit par le runner. Le test Python le sert, le passe ici, et compare.
//
// Il ne juge rien : tout ce qui est affirmé l'est dans `tests/test_publication_evals.py`.

import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

import { Document } from "./dom_minimal.mjs";

const ICI = path.dirname(fileURLToPath(import.meta.url));
const RACINE = path.resolve(ICI, "..", "..");

function charger() {
  const document = new Document();
  document.readyState = "complete";
  const journal = new console.Console(process.stderr, process.stderr);
  const window = {
    location: new URL("https://foyer-retour.example/"), document,
    fetch: () => Promise.reject(new Error("aucune sonde ici")),
    addEventListener: () => {},
    __ACCUEIL_SANS_DEMARRAGE: true,
  };
  const bac = {
    window, document, console: journal, URL,
    setTimeout, clearTimeout, AbortController,
    JSON, Math, Date, Number, String, Array, Object, isFinite, parseInt, Error, Promise, RegExp,
  };
  bac.globalThis = bac;
  vm.createContext(bac);
  vm.runInContext(readFileSync(path.join(RACINE, "tools/accueil/accueil.js"), "utf8"), bac,
                  { filename: "tools/accueil/accueil.js" });
  return window.ACCUEIL;
}

function aplatir(vue) {
  return [vue].concat((vue.enfants || []).flatMap(aplatir));
}

function main() {
  const brut = readFileSync(0, "utf8");
  const corps = JSON.parse(brut);
  const ACCUEIL = charger();
  const lu = ACCUEIL.lireEvals(corps);
  const vue = ACCUEIL.vueEvals(lu);
  return {
    lisible: lu !== null,
    lu,
    textes: aplatir(vue).map((n) => n.texte).filter((t) => t !== undefined && t !== null),
  };
}

try {
  process.stdout.write(JSON.stringify({ ok: true, cas: main() }, null, 1));
} catch (erreur) {
  process.stdout.write(JSON.stringify({
    ok: false, erreur: String((erreur && erreur.stack) || erreur),
  }, null, 1));
  process.exitCode = 1;
}
