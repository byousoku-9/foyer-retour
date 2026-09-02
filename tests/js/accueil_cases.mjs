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
import { CORPS_DICTIONNAIRE, CORPS_PARTAGES, santeConforme } from "./sante_corpus.mjs";

const ICI = path.dirname(fileURLToPath(import.meta.url));
const RACINE = path.resolve(ICI, "..", "..");

const ORIGINE = "https://foyer-retour.example";
const PAGE = ORIGINE + "/";

// Le seul identifiant que `accueil.js` cherche dans la page. Un test Python le vérifie contre
// `tools/accueil/index.html`, pour qu'un renommage ne laisse pas ce harnais peindre dans le vide.
const ELEMENTS = [{ tag: "div", id: "etat" }, { tag: "div", id: "evals" }];

/** Un corps de `/api/v1/evals/latest` publié, tel que `routes/evals.py` l'écrit (story 4.5). */
function evalsPublie(extra) {
  return {
    publie: true,
    raison: null,
    publication: Object.assign({
      schema_version: 1,
      profile: "full",
      candidate_revision: "1".repeat(40),
      run_digest: "a".repeat(64),
      report_digest: "b".repeat(64),
      plancher_digest: "c".repeat(64),
      cases_hash: "d".repeat(64),
      date: "2026-08-29T00:00:00Z",
      // L'état que la story publie : un gate **rouge**, publié tel quel. C'est le cas nominal, pas
      // l'exception — la publication est inconditionnelle (FR41).
      evals_ok: false,
      variantes: { outils: 3, local: 2 },
      labels: { bonne_reponse: 3, parsing: 1 },
      // Valeurs volontairement « rondes » : sans formatage partagé, la page écrirait `1` et
      // `0.055` là où le Markdown écrit `1.0000` et `0.0550` (revue 4.5, P5).
      recall: 1.0,
      stabilite: { n: 3, cas_stables: 1, cas_comptabilises: 2 },
      cout: { froid_eur: 0.1649, moyen_eur: 0.055, p95_eur: 0.02 },
      latence: { p50_ms: 14243, p95_ms: 34370 },
      ne_tranche_pas_rate: 0,
      reserves: { countersigned: false, validated_by_expert: false, dictionary_validated: false },
      decisions: [],
      limites: ["décision rouge stabilite_sinistre : 0.0000 < plancher 1.0000 (n=3, scope "
                + "suite:sinistre, producteur orchestrator)"],
      seconde_lecture: { statut: "planifiee", blocs_planifies: 2, blocs_verifies: 0 },
    }, extra || {}),
  };
}

/** L'état « aucun run publié » — normal, jamais une panne. */
function evalsAbsent(raison) {
  return { publie: false, raison: raison === undefined ? "absent" : raison };
}

/** Répond selon la route demandée : les deux sondes de la page sont indépendantes. */
function parRoute({ sante: corpsSante, evals: corpsEvals, statutEvals = 200 }) {
  return (url) => {
    if (String(url).indexOf("/api/v1/evals/latest") !== -1) {
      return reponseHttp({ status: statutEvals, corps: corpsEvals });
    }
    return reponseHttp({ corps: corpsSante });
  };
}

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
    // AD-5 (story 2.1) : les trois booléens d'`EtatDictionnaire`, tels que `routes/sante.py` les
    // sérialise. L'état que le service écrit **aujourd'hui** : `data/dictionary.json` est livré et
    // décrit bien le corpus servi (`corpus_ok: true`), mais personne ne l'a signé — ses variantes
    // élargissent la recherche, seul le refus « zéro hit » dort. Un test Python compare ce corps à
    // celui que la route rend réellement.
    dictionary: { validated: false, corpus_ok: true, refus_zero_hit_actif: false },
    alerts: [],
    thresholds: { deadline_s: 75.0 },
  }, extra || {});
}

/** Une alerte de service, telle qu'`api/etat._alertes_dictionnaire` l'écrit (`doc_id: "*"`). */
function alerteDico(alerte, detail) {
  return { doc_id: "*", alerte: alerte, detail: detail };
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
    cas.routes = ACCUEIL.routes();
  }

  // --- FR41/FR42 : les résultats publiés, sur la seconde sonde ----------------
  {
    const ctx = charger(PAGE, parRoute({ sante: sante(), evals: evalsPublie() }));
    const lu = await ctx.ACCUEIL.sonderEvals();
    const vue = ctx.ACCUEIL.vueEvals(lu);
    cas.evals_publie = {
      url: ctx.appels[0].url,
      methode: ctx.appels[0].options.method || "GET",
      lu,
      textes: textesDe(vue),
      dom: releverNoeud(ctx.ACCUEIL.peindre(vue, ctx.elements.evals) && ctx.elements.evals),
    };
  }

  // --- aucun run publié : une absence rendue comme une absence ---------------
  {
    for (const raison of ["absent", "illisible", "hors_schema", null, "motif_inconnu"]) {
      const ctx = charger(PAGE, parRoute({ sante: sante(), evals: evalsAbsent(raison) }));
      const lu = await ctx.ACCUEIL.sonderEvals();
      cas.evals_absent = cas.evals_absent || {};
      cas.evals_absent[String(raison)] = { lu, textes: textesDe(ctx.ACCUEIL.vueEvals(lu)) };
    }
  }

  // --- la sonde des résultats échoue : dit, jamais peint en « aucun run » ----
  {
    const ctx = charger(PAGE, parRoute({ sante: sante(), evals: {}, statutEvals: 503 }));
    let motif = null;
    try { await ctx.ACCUEIL.sonderEvals(); } catch (e) { motif = e; }
    cas.evals_sonde_morte = {
      motif,
      textes: textesDe(ctx.ACCUEIL.vueEvals(null, motif)),
      // Sans motif propagé, la page rabat toutes les causes sur une seule phrase.
      textes_sans_motif: textesDe(ctx.ACCUEIL.vueEvals(null)),
      textes_hors_ligne: textesDe(ctx.ACCUEIL.vueEvals(null, "hors_ligne")),
      textes_illisible: textesDe(ctx.ACCUEIL.vueEvals(null, "reponse_illisible")),
    };
  }

  // --- lecture stricte du 200 des résultats ----------------------------------
  {
    const mauvais = {
      publie_absent: (c) => { delete c.publie; },
      publie_non_booleen: (c) => { c.publie = "oui"; },
      publication_absente: (c) => { delete c.publication; },
      recall_absent: (c) => { delete c.publication.recall; },
      recall_texte: (c) => { c.publication.recall = "0.6"; },
      stabilite_absente: (c) => { delete c.publication.stabilite; },
      stabilite_incomplete: (c) => { delete c.publication.stabilite.cas_stables; },
      cout_absent: (c) => { delete c.publication.cout; },
      latence_non_entiere: (c) => { c.publication.latence.p50_ms = 12.5; },
      reserves_absentes: (c) => { delete c.publication.reserves; },
      reserve_non_booleenne: (c) => { c.publication.reserves.countersigned = "non"; },
      limites_non_chaines: (c) => { c.publication.limites = [1]; },
      labels_non_entiers: (c) => { c.publication.labels = { bonne_reponse: "trois" }; },
      profile_vide: (c) => { c.publication.profile = ""; },
      run_digest_absent: (c) => { delete c.publication.run_digest; },
    };
    cas.evals_illisibles = {};
    const { ACCUEIL } = charger(PAGE, () => reponseHttp({ corps: sante() }));
    for (const [nom, casser] of Object.entries(mauvais)) {
      const corps = evalsPublie();
      casser(corps);
      cas.evals_illisibles[nom] = ACCUEIL.lireEvals(corps) === null;
    }
    // Le corps nominal, lui, se lit.
    cas.evals_illisibles.nominal_lisible = ACCUEIL.lireEvals(evalsPublie()) !== null;
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

  // --- AD-5 : le dictionnaire des variantes, en trois formulations -----------
  //
  // Ce que la ligne annonce est le sort du **refus** « zéro hit », lu tel quel sur
  // `dictionary.refus_zero_hit_actif`. Les deux formulations désarmées se distinguent par l'alerte
  // que le serveur publie, jamais par un calcul de la page : `corpus_ok` vaut `false` aussi bien
  // pour un fichier absent que pour un fichier d'un autre corpus, et seul le serveur les sépare.
  {
    const situations = {
      // Le dictionnaire est signé et décrit le corpus servi : le refus est armé, et c'est le seul
      // état où la page l'écrit.
      arme: {
        dictionary: { validated: true, corpus_ok: true, refus_zero_hit_actif: true },
        alerts: [],
      },
      // Le nominal du dépôt : le fichier se lit et décrit bien le corpus, mais personne ne l'a
      // signé. Ses variantes servent — seul le refus dort, et la phrase doit le dire.
      non_signe: {
        dictionary: { validated: false, corpus_ok: true, refus_zero_hit_actif: false },
        alerts: [alerteDico("dictionnaire_non_valide",
                            "aucune validation humaine : le refus « zéro hit » d'AD-5 est " +
                            "désactivé (la recherche se poursuit vers *retrouver*)")],
      },
      // Aucun fichier : `corpus_ok` est faux, et rien n'est chargé — ni variantes ni refus. La page
      // ne doit ni le confondre avec le cas précédent (les variantes n'y sont pas), ni annoncer un
      // dictionnaire « périmé » là où il n'y en a aucun.
      absent: {
        dictionary: { validated: false, corpus_ok: false, refus_zero_hit_actif: false },
        alerts: [alerteDico("dictionnaire_non_valide",
                            "aucune validation humaine : le refus « zéro hit » d'AD-5 est " +
                            "désactivé — dictionary.json absent")],
      },
      // Le fichier se lit, mais ses empreintes décrivent un autre corpus : les deux alertes
      // tombent ensemble, comme `api/etat._alertes_dictionnaire` les écrit.
      corpus_perime: {
        dictionary: { validated: false, corpus_ok: false, refus_zero_hit_actif: false },
        alerts: [
          alerteDico("dictionnaire_non_valide", "aucune validation humaine"),
          alerteDico("dictionnaire_corpus_perime",
                     "dictionary.json décrit un autre corpus que celui qui est servi : ni " +
                     "variantes, ni court-circuit"),
        ],
      },
    };
    cas.dictionnaire = {};
    for (const [nom, extra] of Object.entries(situations)) {
      const ctx = charger(PAGE, () => reponseHttp({ corps: sante(extra) }));
      const lu = await ctx.ACCUEIL.sonder();
      const vue = ctx.ACCUEIL.vueEtat(lu);
      cas.dictionnaire[nom] = {
        lu: lu && lu.dictionary,
        libelle: ctx.ACCUEIL.libelleDictionnaire(lu),
        perime: ctx.ACCUEIL.dictionnairePerime(lu),
        textes: textesDe(vue),
        dom: peindreEtRelever(ctx, vue),
      };
    }
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
      // AD-5 : `EtatDictionnaire` a trois champs, tous sérialisés. Un corps qui en ampute un n'a
      // été écrit par aucune route — et « le refus est désactivé » lu sur une clé manquante serait
      // une affirmation sur le système que le serveur n'a pas faite.
      dictionary_absent: (c) => { delete c.dictionary; },
      dictionary_nul: (c) => { c.dictionary = null; },
      dictionary_non_objet: (c) => { c.dictionary = "non validé"; },
      dictionary_tableau: (c) => { c.dictionary = []; },
      dictionary_validated_absent: (c) => { delete c.dictionary.validated; },
      dictionary_validated_chaine: (c) => { c.dictionary.validated = "true"; },
      dictionary_corpus_ok_absent: (c) => { delete c.dictionary.corpus_ok; },
      dictionary_corpus_ok_numerique: (c) => { c.dictionary.corpus_ok = 1; },
      dictionary_refus_absent: (c) => { delete c.dictionary.refus_zero_hit_actif; },
      dictionary_refus_chaine: (c) => { c.dictionary.refus_zero_hit_actif = "oui"; },
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
      // Les trois booléens du dictionnaire valent `true` de plein droit : un lecteur trop strict
      // n'afficherait plus jamais l'état armé, c'est-à-dire précisément celui que la story vise.
      dictionnaire_arme: sante({
        dictionary: { validated: true, corpus_ok: true, refus_zero_hit_actif: true } }),
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
    const ctx = charger(PAGE, parRoute({ sante: sante(), evals: evalsPublie() }),
                        { demarrage: true });
    await tick();
    await tick();
    await tick();
    cas.demarrage = {
      dom: releverNoeud(ctx.elements.etat),
      dom_evals: releverNoeud(ctx.elements.evals),
      appels: ctx.appels.map((a) => a.url).sort(),
      localStorage: ctx.localStorage.entrees(),
    };
  }

  // --- frontière variable : du HTML serveur reste du texte inerte ------------
  //
  // Cette contre-sonde traverse le vrai démarrage (lecture stricte, composition, matérialisation),
  // pas seulement `materialiser()` isolé. Les quatre champs sont ceux que la page affiche
  // réellement. Si l'un d'eux passait un jour par `innerHTML`, le relevé contiendrait un élément
  // actif au lieu du littéral hostile.
  {
    const corps = sante({
      version: '<img src=x onerror="VERSION_ACTIVE">',
      documents_servis: ['<script>DOCUMENT_ACTIF</script>'],
      alerts: [{
        doc_id: '<svg onload="DOC_ACTIF">',
        alerte: "source_absente",
        detail: '<a href="https://tiers.example">DETAIL_ACTIF</a>',
      }],
    });
    const ctx = charger(PAGE, () => reponseHttp({ corps }), { demarrage: true });
    await tick();
    await tick();
    const dom = releverNoeud(ctx.elements.etat);
    cas.demarrage_hostile = {
      dom,
      textes: aplatir(dom).map((n) => n.texte)
        .filter((t) => t !== null && t !== undefined),
      tags: aplatir(dom).map((n) => n.tag),
      appels: ctx.appels.map((a) => a.url),
    };
  }

  // Une sonde morte n'efface pas l'autre : la seconde route échoue, le niveau de validation reste
  // peint, et le bloc des résultats dit son ignorance au lieu d'annoncer « aucun run ».
  {
    const ctx = charger(PAGE, (url) => {
      if (String(url).indexOf("/api/v1/evals/latest") !== -1) throw new Error("réseau coupé");
      return reponseHttp({ corps: sante() });
    }, { demarrage: true });
    await tick();
    await tick();
    await tick();
    cas.demarrage_evals_mort = {
      textes_etat: aplatir(releverNoeud(ctx.elements.etat)).map((n) => n.texte)
        .filter((t) => t !== null && t !== undefined),
      textes_evals: aplatir(releverNoeud(ctx.elements.evals)).map((n) => n.texte)
        .filter((t) => t !== null && t !== undefined),
    };
  }

  {
    const ctx = charger(PAGE, () => { throw new Error("réseau coupé"); }, { demarrage: true });
    await tick();
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

  // --- la table du dictionnaire, rejouée par l'accueil seul -------------------
  //
  // `web/app/chat.js` ne lit pas `dictionary` : cette table n'est donc pas confrontée à un second
  // lecteur, elle amarre le contrat de celui-ci (voir l'en-tête de `sante_corpus.mjs`).
  {
    const { ACCUEIL } = charger(PAGE, () => reponseHttp({ corps: sante() }));
    cas.corpus_dictionnaire = {};
    for (const [nom, entree] of Object.entries(CORPS_DICTIONNAIRE)) {
      const lu = ACCUEIL.lireSante(entree.corps);
      cas.corpus_dictionnaire[nom] = { lisible: lu !== null, attendu: entree.lisible,
                                       dictionary: lu && lu.dictionary };
    }
  }

  // --- les corps de référence eux-mêmes -------------------------------------
  //
  // Ce que les deux harnais **posent** comme corps nominal, relevé tel quel. Un test Python le
  // confronte à ce que `GET /api/v1/sante` rend réellement : le défaut relevé par la revue
  // coordonnée était un corps de référence décrivant un dépôt révolu (`corpus_ok: false` alors que
  // `data/dictionary.json` est livré), qui faisait passer des cas que la production ne voit jamais.
  // Les deux copies sont relevées, parce qu'elles peuvent aussi dériver l'une de l'autre.
  {
    cas.corps_nominal = { accueil: sante(), partage: santeConforme() };
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
