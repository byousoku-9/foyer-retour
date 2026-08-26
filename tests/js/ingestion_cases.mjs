// Harnais sans navigateur de la page d'audit d'ingestion (story 3.5).
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

import { Document } from "./dom_minimal.mjs";

const ICI = path.dirname(fileURLToPath(import.meta.url));
const RACINE = path.resolve(ICI, "..", "..");
const ORIGINE = "https://foyer-retour.example";

function reponse(corps, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: () => Promise.resolve(corps) };
}

function charger(pathname, repondre) {
  const document = new Document();
  document.readyState = "complete";
  const titre = document.createElement("h1");
  titre.id = "titre";
  document.body.appendChild(titre);
  const rapport = document.createElement("main");
  rapport.id = "rapport";
  document.body.appendChild(rapport);
  const appels = [];
  const fetch = (url, fetchOptions = {}) => {
    appels.push(String(url));
    return Promise.resolve(repondre(String(url), fetchOptions));
  };
  const window = {
    location: new URL(ORIGINE + pathname), document, fetch,
    __INGESTION_SANS_DEMARRAGE: true,
  };
  const bac = {
    window, document, fetch, URL, console, JSON, String, Array, Object, Error, Promise,
    setTimeout, clearTimeout, AbortController,
    decodeURIComponent, encodeURIComponent,
  };
  bac.globalThis = bac;
  vm.createContext(bac);
  vm.runInContext(readFileSync(path.join(RACINE, "tools/sinistre/ingestion.js"), "utf8"), bac,
                  { filename: "tools/sinistre/ingestion.js" });
  return { INGESTION: window.INGESTION, document, titre, rapport, appels };
}

const SERVI = {
  doc_id: "cg-mini", title: "<img onerror=alert(1)>", edition: "", kind: "contrat",
  status: "servi", selectionnable: true, raison: null,
  source_url: "https://example.invalid/cg.pdf", source_hash: "source-123",
  ingest_fingerprint: "ingest-456", document_hash: "doc-789", overlay_hash: null,
  report_status: "disponible",
  gate: {
    profile: "vertical", source_hash: "source-123", ingest_fingerprint: "ingest-456",
    cases_hash: "cases", pipeline_digest: "pipeline", prompts_digest: "prompts",
    model_ids: { micro: "modele" }, evals_ok: true, date: "2026-08-26",
    overlay_hash: null, cases: 2, countersigned: false,
  },
};

const QUARANTAINE = {
  doc_id: "cg-quarantaine", title: "cg-quarantaine", edition: null, kind: null,
  status: "quarantaine", selectionnable: false,
  raison: "bloquant_statique : <script>alerte</script>", source_url: "gs://prive/secret.pdf",
  source_hash: "s", ingest_fingerprint: "i", document_hash: "d", overlay_hash: null,
  gate: null, report_status: "absent",
};

const RAPPORT = {
  doc_id: "cg-mini",
  checks: [
    { name: "premier", level: "info", detail: "<b>détail littéral</b>" },
    { name: "second", level: "bloquant", detail: "fin" },
  ],
  stats: {},
};

const SANTE = { thresholds: { client_abort_margin_s: 7 } };

async function main() {
  const cas = {};
  {
    const h = charger("/sinistre/ingestion/cg-mini", (url) => {
      if (url.endsWith("/api/v1/sante")) return reponse(SANTE);
      if (url.endsWith("/api/v1/documents")) return reponse([SERVI, QUARANTAINE]);
      return reponse(RAPPORT);
    });
    await h.INGESTION.demarrer();
    cas.servi = {
      appels: h.appels.map((u) => u.replace(ORIGINE, "")),
      titre: h.titre.textContent,
      texte: h.rapport.textContent,
      lignes: h.rapport.querySelectorAll("tr").slice(1).map((tr) => tr.textContent),
      source: (() => {
        const liens = h.rapport.querySelectorAll("a");
        const a = liens.filter((l) => l.textContent.indexOf("source publique") >= 0)[0];
        return a ? { href: a.href, target: a.target, rel: a.rel } : null;
      })(),
      brut: h.rapport.querySelector(".brut").href,
      busy: h.rapport.getAttribute("aria-busy"),
      borne_ms: h.INGESTION.fetchTimeoutMs(),
      scripts: h.rapport.querySelectorAll("script").length,
    };
  }
  {
    const h = charger("/sinistre/ingestion/cg-quarantaine", (url) => {
      if (url.endsWith("/api/v1/sante")) return reponse(SANTE);
      if (url.endsWith("/api/v1/documents")) return reponse([SERVI, QUARANTAINE]);
      throw new Error("le rapport absent ne doit pas être demandé");
    });
    await h.INGESTION.demarrer();
    cas.quarantaine = {
      appels: h.appels.map((u) => u.replace(ORIGINE, "")),
      texte: h.rapport.textContent,
      liens: h.rapport.querySelectorAll("a").map((a) => a.href),
    };
  }
  {
    const h = charger("/sinistre/ingestion/cg-mini", () => reponse({}));
    cas.valeurs = {
      absente: h.INGESTION.valeur(null), vide: h.INGESTION.valeur(""), faux: h.INGESTION.valeur(false),
      doc_id: h.INGESTION.docIdDepuisChemin("/sinistre/ingestion/cg-mini"),
      doc_id_invalide: h.INGESTION.docIdDepuisChemin("/sinistre/ingestion/a/b"),
    };
    cas.rapport_vide = h.INGESTION.carteRapport(SERVI, { doc_id: "cg-mini", checks: [], stats: {} })
      .textContent;
  }
  for (const statut of ["illisible", "etranger"]) {
    const doc = Object.assign({}, QUARANTAINE, {
      doc_id: "cg-" + statut, report_status: statut,
    });
    const h = charger("/sinistre/ingestion/" + doc.doc_id, (url) => {
      if (url.endsWith("/api/v1/sante")) return reponse(SANTE);
      if (url.endsWith("/api/v1/documents")) return reponse([doc]);
      throw new Error("un rapport non publiable ne doit pas être demandé");
    });
    await h.INGESTION.demarrer();
    cas[statut] = {
      appels: h.appels.map((u) => u.replace(ORIGINE, "")),
      texte: h.rapport.textContent,
    };
  }
  {
    const h = charger("/sinistre/ingestion/cg-mini", (_url, options) =>
      new Promise((_resolve, reject) => {
        if (options.signal) {
          options.signal.addEventListener("abort", () => reject(new Error("abandon test")));
        }
      }));
    await h.INGESTION.demarrer({ timeoutMs: 0 });
    cas.timeout = {
      borne_ms: h.INGESTION.fetchTimeoutMs(),
      appels: h.appels.length,
      busy: h.rapport.getAttribute("aria-busy"),
      texte: h.rapport.textContent,
    };
  }
  process.stdout.write(JSON.stringify({ ok: true, cas }));
}

main().catch((e) => {
  process.stdout.write(JSON.stringify({ ok: false, erreur: e && e.stack ? e.stack : String(e) }));
  process.exitCode = 1;
});
