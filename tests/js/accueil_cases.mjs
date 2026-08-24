// Harnais Node du front de l'accueil (story 1.10) — aucun navigateur, aucun réseau, aucune
// dépendance ajoutée à `pyproject.toml`.
//
// Décalque de `tests/js/sinistre_cases.mjs` : `tools/accueil/accueil.js` est un IIFE posé sur
// `window`, chargé dans un `node:vm` avec `window`, `location`, `document` (le DOM minimal de
// `dom_minimal.mjs`) et `fetch` **doublés**. On exécute les cas de la matrice d'E/S et on écrit sur
// la sortie standard le JSON de ce qui a été **observé**. Il ne juge rien : tout ce qui est affirmé
// l'est en Python (`tests/test_web_accueil.py`).

import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

import { Document, stockage } from "./dom_minimal.mjs";
import { CORPS_PARTAGES } from "./sante_corpus.mjs";

const ICI = path.dirname(fileURLToPath(import.meta.url));
const RACINE = path.resolve(ICI, "..", "..");

const ORIGINE = "https://foyer-retour.example";
const PAGE = ORIGINE + "/";

// Le seul identifiant que `accueil.js` cherche dans la page. Un test Python le vérifie contre
// `tools/accueil/index.html`, pour qu'un renommage ne laisse pas ce harnais peindre dans le vide.
const ELEMENTS = [{ tag: "div", id: "etat" }];

/** Un corps de `/api/v1/sante` conforme, tel que `routes/sante.py` l'écrit. */
function sante(extra) {
  return Object.assign({
    ok: true,
    version: "b79ca1b",
    documents_servis: ["axa-lu-optihome-2017", "lux-guide"],
    gate_profile: "vertical",
    gate_cases: 2,
    // L'état **du dépôt aujourd'hui** : les deux cas sont relus par la boucle, la contresignature
    // humaine reste due (revue Codex 1.10 tour 2, B2). Le cas `contresigne` ci-dessous est celui
    // que l'AC décrit — c'est lui qui doit rendre « 2 cas relus à la main ».
    gate_countersigned: false,
    dictionary: { validated: false },
    alerts: [],
    thresholds: { deadline_s: 55.0 },
  }, extra || {});
}

function reponseHttp({ status = 200, corps = {}, corpsIllisible = false } = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: () => (corpsIllisible
      ? Promise.reject(new Error("corps non JSON"))
      : Promise.resolve(corps)),
  };
}

/** Charge `accueil.js` dans un contexte neuf, avec un DOM minimal monté. */
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
  // Le JSON du harnais sort sur **stdout** : un `console.log` laissé dans `accueil.js` le
  // corromprait. Le `console` du bac à sable écrit donc sur stderr.
  const journal = new console.Console(process.stderr, process.stderr);
  const window = {
    location: new URL(href), document, localStorage, fetch: fetchDouble,
    addEventListener: () => {},
  };
  if (!demarrage) window.__ACCUEIL_SANS_DEMARRAGE = true;
  const bac = {
    window, document, localStorage, fetch: fetchDouble, console: journal, URL,
    setTimeout, clearTimeout, AbortController,
    JSON, Math, Date, Number, String, Array, Object, isFinite, parseInt, Error, Promise, RegExp,
  };
  bac.globalThis = bac;
  vm.createContext(bac);
  vm.runInContext(readFileSync(path.join(RACINE, "tools/accueil/accueil.js"), "utf8"), bac,
                  { filename: "tools/accueil/accueil.js" });
  return { ACCUEIL: window.ACCUEIL, appels, document, elements, localStorage, window };
}

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

function textesDe(vue) {
  return aplatirVue(vue).map((n) => n.texte).filter((t) => t !== undefined && t !== null);
}

/** Peint une vue et rend le relevé du DOM produit. */
function peindreEtRelever(ctx, vue) {
  ctx.ACCUEIL.peindre(vue, ctx.elements.etat);
  return releverNoeud(ctx.elements.etat);
}

function tick() { return new Promise((r) => setTimeout(r, 0)); }

// ---------- les cas ----------

async function main() {
  const cas = {};

  // --- AD-12 : une seule origine, une seule route sondée ---------------------
  {
    const { ACCUEIL, appels } = charger(PAGE, () => reponseHttp({ corps: sante() }));
    await ACCUEIL.sonder();
    cas.api_base = ACCUEIL.apiBase();
    cas.sonde_url = appels[0].url;
    cas.sonde_methode = appels[0].options.method || "GET";
    cas.appels_au_demarrage = appels.length;
  }

  // --- état 1 : la sonde répond avec un profil -------------------------------
  {
    const ctx = charger(PAGE, () => reponseHttp({ corps: sante() }));
    const lu = await ctx.ACCUEIL.sonder();
    const vue = ctx.ACCUEIL.vueEtat(lu);
    cas.nominal = {
      validation: ctx.ACCUEIL.libelleValidation(lu),
      textes: textesDe(vue),
      dom: peindreEtRelever(ctx, vue),
      localStorage: ctx.localStorage.entrees(),
    };
  }

  // --- état 1 bis : un seul cas relu (l'accord du pluriel vient du serveur) ---
  {
    const ctx = charger(PAGE, () => reponseHttp({
      corps: sante({ documents_servis: ["lux-guide"], gate_cases: 1 }) }));
    const lu = await ctx.ACCUEIL.sonder();
    cas.un_seul_cas = ctx.ACCUEIL.libelleValidation(lu);
  }

  // --- état 1 ter : la contresignature humaine est faite ----------------------
  // C'est l'état que l'AC décrit (« 2 cas relus à la main ») et le seul où la page l'écrit.
  {
    const ctx = charger(PAGE, () => reponseHttp({ corps: sante({ gate_countersigned: true }) }));
    const lu = await ctx.ACCUEIL.sonder();
    const vue = ctx.ACCUEIL.vueEtat(lu);
    cas.contresigne = { validation: ctx.ACCUEIL.libelleValidation(lu), textes: textesDe(vue) };
  }

  // --- état 1 ter bis : un seul cas, contresigné (l'accord du pluriel) --------
  {
    const ctx = charger(PAGE, () => reponseHttp({
      corps: sante({ documents_servis: ["lux-guide"], gate_cases: 1, gate_countersigned: true }) }));
    const lu = await ctx.ACCUEIL.sonder();
    cas.un_seul_cas_contresigne = ctx.ACCUEIL.libelleValidation(lu);
  }

  // --- état 1 quater : un profil qui ne promet aucune relecture humaine -------
  {
    const ctx = charger(PAGE, () => reponseHttp({ corps: sante({ gate_profile: "full", gate_cases: 47 }) }));
    const lu = await ctx.ACCUEIL.sonder();
    cas.profil_full = ctx.ACCUEIL.libelleValidation(lu);
  }

  // --- état 1 quinquies : le serveur signale un gate périmé -------------------
  {
    const ctx = charger(PAGE, () => reponseHttp({
      corps: sante({ alerts: [{ doc_id: "lux-guide", alerte: "gate_perime", detail: "" }] }) }));
    const lu = await ctx.ACCUEIL.sonder();
    cas.gate_perime = { perime: ctx.ACCUEIL.perime(lu), textes: textesDe(ctx.ACCUEIL.vueEtat(lu)) };
  }

  // --- état 1 sexies : le serveur répond mais `ok: false` ---------------------
  {
    const ctx = charger(PAGE, () => reponseHttp({
      corps: sante({ ok: false, documents_servis: ["axa-lu-optihome-2017"], gate_cases: 1 }) }));
    const lu = await ctx.ACCUEIL.sonder();
    cas.pas_ok = { textes: textesDe(ctx.ACCUEIL.vueEtat(lu)) };
  }

  // --- état 1 ter : les alertes du serveur sont affichées telles qu'elles sont
  {
    const ctx = charger(PAGE, () => reponseHttp({
      corps: sante({
        alerts: [
          { doc_id: "lux-guide", alerte: "gate_perime", detail: "" },
          { doc_id: "*", alerte: "ungated_refuse_en_production", detail: "ALLOW_UNGATED=true" },
          { doc_id: "autre", alerte: "alerte_inconnue_du_front", detail: "détail du serveur" },
          // Les raisons de quarantaine arrivent **ainsi** : nom d'alerte `quarantaine`, raison en
          // préfixe du détail (`api/etat._alertes`). Les mettre dans la table des noms d'alerte
          // en aurait fait du code mort.
          { doc_id: "axa", alerte: "quarantaine",
            detail: "bloquant_statique : page_sans_texte" },
          { doc_id: "vieux", alerte: "quarantaine", detail: "gate_echoue" },
        ] }) }));
    const lu = await ctx.ACCUEIL.sonder();
    const vue = ctx.ACCUEIL.vueEtat(lu);
    cas.alertes = { textes: textesDe(vue), dom: peindreEtRelever(ctx, vue) };
  }

  // --- état 2 : la sonde répond sans profil ----------------------------------
  {
    const ctx = charger(PAGE, () => reponseHttp({
      corps: sante({
        gate_profile: null, gate_cases: null, gate_countersigned: null,
        alerts: [{ doc_id: "lux-guide", alerte: "sans_gate", detail: "" }] }) }));
    const lu = await ctx.ACCUEIL.sonder();
    const vue = ctx.ACCUEIL.vueEtat(lu);
    cas.sans_gate = {
      validation: ctx.ACCUEIL.libelleValidation(lu),
      textes: textesDe(vue),
      dom: peindreEtRelever(ctx, vue),
    };
  }

  // --- état 2 bis : aucun document servi -------------------------------------
  {
    const ctx = charger(PAGE, () => reponseHttp({
      corps: sante({ ok: false, documents_servis: [], gate_profile: null, gate_cases: null,
                     gate_countersigned: null,
                     alerts: [{ doc_id: "lux-guide", alerte: "quarantaine",
                                detail: "document_hash différent du manifest" }] }) }));
    const lu = await ctx.ACCUEIL.sonder();
    cas.aucun_document = { textes: textesDe(ctx.ACCUEIL.vueEtat(lu)) };
  }

  // --- état 3 : la sonde échoue ---------------------------------------------
  {
    cas.sonde_echouee = {};
    const situations = [
      { nom: "reseau", repondre: () => { throw new Error("réseau coupé"); } },
      { nom: "http_500", repondre: () => reponseHttp({ status: 500, corps: {} }) },
      { nom: "http_503", repondre: () => reponseHttp({ status: 503, corps: {} }) },
      { nom: "corps_non_json", repondre: () => reponseHttp({ corpsIllisible: true }) },
    ];
    for (const s of situations) {
      const ctx = charger(PAGE, s.repondre);
      let motif = null;
      try { await ctx.ACCUEIL.sonder(); } catch (e) { motif = e; }
      const vue = ctx.ACCUEIL.vueSondeEchouee(motif);
      cas.sonde_echouee[s.nom] = {
        motif,
        textes: textesDe(vue),
        dom: peindreEtRelever(ctx, vue),
      };
    }
  }

  // --- état 3 bis : page ouverte en file:// ---------------------------------
  {
    const ctx = charger("file:///Users/quelquun/tools/accueil/index.html",
                        () => reponseHttp({ corps: sante() }));
    let motif = null;
    try { await ctx.ACCUEIL.sonder(); } catch (e) { motif = e; }
    cas.hors_ligne = {
      api_base: ctx.ACCUEIL.apiBase(),
      appels_reseau: ctx.appels.length,
      motif,
      textes: textesDe(ctx.ACCUEIL.vueSondeEchouee(motif)),
    };
  }

  // --- lecture stricte du 200 : ce que la page refuse ------------------------
  //
  // Même règle qu'en 1.9 (revue Codex, tours 2 et 3) : une clé **absente** n'est pas « le champ vaut
  // null », et un corps que la route ne pouvait pas écrire est une sonde illisible — donc l'état 3,
  // jamais un niveau peint à moitié.
  {
    const mauvais = {
      ok_absent: (c) => { delete c.ok; },
      ok_non_booleen: (c) => { c.ok = "oui"; },
      version_absente: (c) => { delete c.version; },
      version_nulle: (c) => { c.version = null; },
      documents_absents: (c) => { delete c.documents_servis; },
      documents_non_tableau: (c) => { c.documents_servis = "lux-guide"; },
      document_non_chaine: (c) => { c.documents_servis = [{ doc_id: "lux-guide" }]; },
      gate_profile_absent: (c) => { delete c.gate_profile; },
      gate_profile_non_chaine: (c) => { c.gate_profile = 2; },
      gate_cases_absent: (c) => { delete c.gate_cases; },
      gate_cases_chaine: (c) => { c.gate_cases = "2"; },
      gate_cases_fractionnaire: (c) => { c.gate_cases = 1.5; },
      gate_cases_zero: (c) => { c.gate_cases = 0; },
      gate_cases_negatif: (c) => { c.gate_cases = -3; },
      gate_profile_vide: (c) => { c.gate_profile = ""; },
      profil_sans_compte: (c) => { c.gate_cases = null; },
      compte_sans_profil: (c) => { c.gate_profile = null; c.gate_countersigned = null; },
      // La contresignature décide de « relus à la main » : un corps qui ne la porte pas, ou qui la
      // dissocie du profil, ferait choisir la page entre deux phrases dont l'une affirme une
      // relecture humaine (revue Codex 1.10 tour 2).
      gate_countersigned_absent: (c) => { delete c.gate_countersigned; },
      gate_countersigned_non_booleen: (c) => { c.gate_countersigned = "true"; },
      gate_countersigned_numerique: (c) => { c.gate_countersigned = 1; },
      profil_sans_contresignature: (c) => { c.gate_countersigned = null; },
      contresignature_sans_profil: (c) => { c.gate_profile = null; c.gate_cases = null; },
      alerts_absent: (c) => { delete c.alerts; },
      alerts_non_tableau: (c) => { c.alerts = { doc_id: "x" }; },
      alerte_sans_doc_id: (c) => { c.alerts = [{ alerte: "sans_gate", detail: "" }]; },
      alerte_sans_nom: (c) => { c.alerts = [{ doc_id: "x", detail: "" }]; },
      alerte_sans_detail: (c) => { c.alerts = [{ doc_id: "x", alerte: "sans_gate" }]; },
      alerte_non_objet: (c) => { c.alerts = ["sans_gate"]; },
      corps_tableau: () => null,
    };
    cas.corps_refuses = {};
    for (const [nom, abimer] of Object.entries(mauvais)) {
      const corps = nom === "corps_tableau" ? [] : sante();
      abimer(corps);
      const ctx = charger(PAGE, () => reponseHttp({ corps }));
      let motif = null;
      let lu = null;
      try { lu = await ctx.ACCUEIL.sonder(); } catch (e) { motif = e; }
      // `lu` non nul veut dire que la page a **accepté** le corps : c'est ce que ces cas nient.
      cas.corps_refuses[nom] = { motif, lu: lu === null ? null : lu };
    }
  }

  // --- garde-fou : un 200 **conforme** n'est jamais refusé --------------------
  //
  // Un lecteur trop strict n'afficherait plus jamais de niveau. Les champs `X | None` du contrat
  // valent `null` de plein droit, et une liste vide est une liste.
  {
    const conformes = {
      nominal: sante(),
      contresigne: sante({ gate_countersigned: true }),
      sans_gate: sante({ gate_profile: null, gate_cases: null, gate_countersigned: null }),
      aucun_document: sante({ ok: false, documents_servis: [], gate_profile: null, gate_cases: null,
                              gate_countersigned: null }),
      alerte_sans_detail_vide: sante({ alerts: [{ doc_id: "lux-guide", alerte: "gate_perime", detail: "" }] }),
      version_vide: sante({ version: "" }),
    };
    cas.corps_conformes = {};
    for (const [nom, corps] of Object.entries(conformes)) {
      const ctx = charger(PAGE, () => reponseHttp({ corps }));
      let motif = null;
      let lu = null;
      try { lu = await ctx.ACCUEIL.sonder(); } catch (e) { motif = e; }
      cas.corps_conformes[nom] = { motif, gate_profile: lu && lu.gate_profile,
                                   gate_cases: lu && lu.gate_cases,
                                   gate_countersigned: lu && lu.gate_countersigned };
    }
  }

  // --- démarrage réel : la page se peint toute seule --------------------------
  {
    const ctx = charger(PAGE, () => reponseHttp({ corps: sante() }), { demarrage: true });
    await tick();
    await tick();
    cas.demarrage = {
      dom: releverNoeud(ctx.elements.etat),
      appels: ctx.appels.map((a) => a.url),
      localStorage: ctx.localStorage.entrees(),
    };
  }

  {
    const ctx = charger(PAGE, () => { throw new Error("réseau coupé"); }, { demarrage: true });
    await tick();
    await tick();
    cas.demarrage_sonde_morte = {
      dom: releverNoeud(ctx.elements.etat),
      textes: aplatir(releverNoeud(ctx.elements.etat)).map((n) => n.texte)
        .filter((t) => t !== null && t !== undefined),
    };
  }

  // --- la table partagée avec le front du guide ------------------------------
  //
  // Le verdict de `lireSante()` sur chaque corps de `tests/js/sante_corpus.mjs` ; le harnais du
  // guide relève le sien sur la **même** table, et un test Python compare les deux corps par corps.
  {
    const { ACCUEIL } = charger(PAGE, () => reponseHttp({ corps: sante() }));
    cas.corpus_partage = {};
    for (const [nom, entree] of Object.entries(CORPS_PARTAGES)) {
      cas.corpus_partage[nom] = { lisible: ACCUEIL.lireSante(entree.corps) !== null,
                                  attendu: entree.lisible };
    }
  }

  // --- bornes ---------------------------------------------------------------
  {
    const { ACCUEIL } = charger(PAGE, () => reponseHttp({ corps: sante() }));
    cas.bornes = ACCUEIL.bornes();
    cas.alertes_connues = Object.keys(ACCUEIL.ALERTES);
    cas.raisons_connues = ACCUEIL.RAISONS;
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
