// Harnais Node du **sens inverse** de la lecture stricte (story 1.7, revue Codex tour 2).
//
// `chat_cases.mjs` prouve que le front refuse ce que le serveur ne peut pas servir. Il ne prouve
// pas le contraire — qu'il lit tout ce que le serveur sert réellement — et c'est précisément le
// risque qu'une lecture stricte introduit : une exigence de trop, et une réponse servie devient un
// « assistant indisponible » à l'écran. Les corps que ce harnais oppose à `chat.js` ne sont pas
// écrits à la main : ils sont sérialisés par `ChatResponse.model_dump(mode="json")` dans
// `tests/test_web_chat.py`, c'est-à-dire par le même code que celui qui répond à `POST /chat`.
//
// Usage : `node tests/js/corps_servi.mjs <fichier.json>` où le fichier porte une liste de corps.
// Comme les deux autres harnais, celui-ci ne juge rien : il relève, Python asserte.

import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const ICI = path.dirname(fileURLToPath(import.meta.url));
const RACINE = path.resolve(ICI, "..", "..");
const PAGE = "https://foyer-retour.example/guide/#assistant";

/** Charge `kb.js` puis `chat.js` dans un contexte neuf, avec un `fetch` qui rend toujours `corps`. */
function chargerChat(corps) {
  const reponse = (url) => Promise.resolve({
    ok: true,
    status: 200,
    headers: { get: () => null },
    json: () => Promise.resolve(String(url).endsWith("/sante") ? { ok: true } : corps),
  });
  const journal = new console.Console(process.stderr, process.stderr);
  const window = { location: new URL(PAGE), fetch: reponse };
  const bac = { window, fetch: reponse, console: journal, URL, setTimeout, clearTimeout,
                AbortController };
  bac.globalThis = bac;
  vm.createContext(bac);
  for (const fichier of ["web/app/kb.js", "web/app/chat.js"]) {
    vm.runInContext(readFileSync(path.join(RACINE, fichier), "utf8"), bac, { filename: fichier });
  }
  window.SIM = { comparatif: () => [] };
  window.CONTRATS_KB = { contrats: [] };
  return window.CHAT;
}

/** Le nombre de citations placées sous chaque segment, ou `null` si l'appariement a été abandonné. */
function apparier(CHAT, r) {
  const par = CHAT.citationsParSegment(r.answer, r.sources);
  return par ? par.map((seg) => seg.length) : null;
}

async function principal() {
  const corpsServis = JSON.parse(readFileSync(process.argv[2], "utf8"));
  const releves = [];
  for (const corps of corpsServis) {
    const CHAT = chargerChat(corps);
    let erreur = null;
    let r = null;
    try { r = await CHAT.repondre("Quel délai après mon arrivée ?", {}, []); } catch (e) { erreur = e; }
    releves.push({
      lu: r !== null,
      code: erreur ? erreur.code : null,
      champ: erreur ? erreur.champ || null : null,
      texte: r ? r.texte : null,
      via: r ? r.via : null,
      sources: r ? r.sources.map((s) => s.block_id) : null,
      // Ce que l'écran en fait : l'état affiché et le nombre de citations placées sous un segment.
      etat: r ? CHAT.etatReponse(r.answer) : null,
      // `null` quand l'appariement a été abandonné (liste plate) — à ne pas confondre avec `[]`,
      // qui est une réponse sans segment.
      citations_par_segment: r ? apparier(CHAT, r) : null,
    });
  }
  return releves;
}

principal().then(
  (cas) => { process.stdout.write(JSON.stringify({ ok: true, node: process.version, cas })); },
  (e) => {
    process.stdout.write(JSON.stringify({ ok: false, erreur: String(e && e.stack || e) }));
    process.exitCode = 1;
  },
);
